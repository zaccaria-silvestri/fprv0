"use strict";

/* Tableau de bord fprv0.
 * Toutes les cartes sont construites à partir de GET /api/instruments : ajouter un instrument
 * dans instruments.toml (et son driver côté Python) suffit, rien n'est codé en dur ici.
 * Le texte venant des appareils (noms de vannes, messages de défaut) est toujours inséré via
 * textContent, jamais via innerHTML.
 */

const LAYOUT_KEY = "fprv0.layout.v1";
const TOKEN_KEY = "fprv0.token";
const DEFAULT_LIMIT = 5000;
const JOURNAL_MAX = 200;
const CONNECTED_STATES = ["connected", "degraded"];
const STATE_LABELS = {
    connected: "Connecté",
    degraded: "Instable",
    connecting: "Connexion…",
    disconnected: "Hors ligne",
    stopped: "Arrêté",
};

const app = {
    info: null,
    cards: new Map(), // id -> carte
    statuses: {},
    linkOk: true,
    token: safeStorage("get", TOKEN_KEY) || "",
};

/* ------------------------------------------------------------------ utilitaires */

const $ = (selector, root = document) => root.querySelector(selector);

function h(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
        if (value == null || value === false) continue;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
        else if (value === true) node.setAttribute(key, "");
        else node.setAttribute(key, value);
    }
    for (const child of children.flat()) if (child != null) node.append(child);
    return node;
}

function safeStorage(op, key, value) {
    try {
        const store = key === TOKEN_KEY ? window.sessionStorage : window.localStorage;
        if (op === "get") return store.getItem(key);
        if (op === "set") store.setItem(key, value);
        if (op === "del") store.removeItem(key);
    } catch (_) {
        /* stockage indisponible (navigation privée, etc.) : on continue sans */
    }
    return null;
}

async function api(path, options = {}) {
    const response = await fetch(path, options);
    let body = {};
    try {
        body = await response.json();
    } catch (_) {
        /* corps vide ou non JSON */
    }
    if (!response.ok) {
        const error = new Error(body.detail || body.error || `${response.status} ${response.statusText}`);
        error.status = response.status;
        throw error;
    }
    return body;
}

const fmtTime = (epoch) => new Date(epoch * 1000).toLocaleTimeString("fr-FR");
const fmtDateTime = (epoch) =>
    new Date(epoch * 1000).toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

// Périodes proposées pour l'historique : [heures, libellé] ; null = temps réel.
const PERIODS = [[null, "En direct"], [1, "1 h"], [6, "6 h"], [24, "24 h"], [168, "7 jours"], [720, "30 jours"]];

function toast(message, isError = false) {
    const node = h("div", { class: isError ? "toast error" : "toast", text: message });
    $("#toasts").append(node);
    setTimeout(() => node.remove(), 5000);
}

/* ------------------------------------------------------------------ formats d'affichage */

const READING_FORMATS = {
    frequency(value) {
        const abs = Math.abs(value);
        if (abs >= 1e9) return `${(value / 1e9).toFixed(6)} GHz`;
        if (abs >= 1e6) return `${(value / 1e6).toFixed(6)} MHz`;
        if (abs >= 1e3) return `${(value / 1e3).toFixed(3)} kHz`;
        return `${value.toFixed(2)} Hz`;
    },
    fixed(value, spec) {
        return `${value.toFixed(spec.decimals ?? 2)}${spec.unit ? " " + spec.unit : ""}`;
    },
};

function formatReading(spec, value) {
    if (typeof value !== "number") return "—";
    return (READING_FORMATS[spec.format] || READING_FORMATS.fixed)(value, spec);
}

function formatDuration(seconds) {
    const s = Math.floor(seconds);
    if (s < 60) return `${s} s`;
    if (s < 3600) return `${Math.floor(s / 60)} min ${s % 60} s`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
    return `${Math.floor(s / 86400)} j ${Math.floor((s % 86400) / 3600)} h`;
}

// Chaque format retourne { text, cls }.
const DIAGNOSTIC_FORMATS = {
    ok: (v) => (v == null ? { text: "—" } : v === 1 ? { text: "OK", cls: "good" } : { text: "Défaillant", cls: "bad" }),
    seconds: (v) => ({ text: typeof v === "number" ? formatDuration(v) : "—" }),
    bytes: (v) => ({ text: typeof v === "number" ? `${v.toLocaleString("fr-FR")} o` : "—" }),
    text: (v) => ({ text: v || "—" }),
    fault: (v, spec, data) =>
        v == null ? { text: "—" } : v === 0 ? { text: "Aucun", cls: "good" } : { text: data[spec.msg_key] || `Code ${v}`, cls: "bad" },
};

/* ------------------------------------------------------------------ dialogues */

function confirmDialog(message) {
    return new Promise((resolve) => {
        const dialog = $("#confirm-dialog");
        $("#confirm-msg").textContent = message;
        dialog.returnValue = "";
        dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
        dialog.showModal();
    });
}

function askToken() {
    return new Promise((resolve) => {
        const dialog = $("#token-dialog");
        const input = $("#token-input");
        input.value = "";
        dialog.returnValue = "";
        dialog.addEventListener(
            "close",
            () => {
                const value = dialog.returnValue === "ok" ? input.value.trim() : "";
                if (value) {
                    app.token = value;
                    safeStorage("set", TOKEN_KEY, value);
                }
                resolve(value || null);
            },
            { once: true }
        );
        dialog.showModal();
        input.focus();
    });
}

/* ------------------------------------------------------------------ commandes */

async function sendCommand(inst, name, kwargs = {}, actionLabel = null) {
    const spec = inst.commands.find((c) => c.name === name);
    if (!spec) {
        toast(`Commande inconnue pour ${inst.label} : ${name}`, true);
        return false;
    }
    let confirmed = false;
    if (spec.dangerous) {
        const ok = await confirmDialog(`${actionLabel || spec.label} sur « ${inst.label} » ?`);
        if (!ok) return false;
        confirmed = true;
    }
    for (let attempt = 0; attempt < 2; attempt++) {
        const headers = { "Content-Type": "application/json" };
        if (app.token) headers["X-API-Token"] = app.token;
        try {
            await api("/api/action", {
                method: "POST",
                headers,
                body: JSON.stringify({ instrument_id: inst.id, action: name, kwargs, confirmed }),
            });
            return true;
        } catch (error) {
            if (error.status === 401 && attempt === 0) {
                safeStorage("del", TOKEN_KEY);
                app.token = "";
                if (!(await askToken())) return false;
                continue; // un seul nouvel essai avec le jeton saisi
            }
            toast(error.message, true);
            return false;
        }
    }
    return false;
}

/* ------------------------------------------------------------------ cartes */

function buildCard(inst) {
    const badge = h("span", { class: "badge", "data-state": "connecting", text: STATE_LABELS.connecting });
    const notice = h("p", { class: "notice", hidden: true });
    const header = h(
        "div",
        { class: "card-header" },
        h("span", { "aria-hidden": "true", text: inst.ui.icon || "" }),
        h("h2", { class: "card-title", text: inst.label }),
        inst.transport === "sim" ? h("span", { class: "tag", text: "Simulé" }) : null,
        badge
    );
    const content = h("div", { class: "grid-stack-item-content card" }, header, notice);
    const item = h("div", { class: "grid-stack-item", "gs-id": inst.id }, content);

    const card = {
        inst,
        item,
        content,
        badge,
        notice,
        lastTs: -Infinity,
        onData: () => {},
        setStatus(status) {
            const state = status.state;
            badge.dataset.state = state;
            badge.textContent = STATE_LABELS[state] || state;
            badge.title = status.detail || "";
            content.classList.toggle("offline", !CONNECTED_STATES.includes(state));
            const message = state === "connected" ? "" : status.detail || "";
            notice.textContent = message;
            notice.hidden = !message;
        },
    };

    const builder = { readings: buildReadings, valves: buildValves }[inst.ui.kind];
    if (builder) {
        item.setAttribute("gs-w", inst.ui.kind === "valves" ? "12" : "6");
        item.setAttribute("gs-h", inst.ui.kind === "valves" ? "4" : "7");
        builder(inst, card);
    } else {
        content.append(h("p", { class: "meta-line", text: `Type d'affichage inconnu : ${inst.ui.kind}` }));
    }
    return card;
}

function buildReadings(inst, card) {
    const ui = inst.ui;
    const values = [];

    const readouts = h("div", { class: "readouts" });
    for (const spec of ui.readings || []) {
        const value = h("span", { class: "value", text: "—" });
        const box = h("div", { class: "readout" }, h("span", { class: "label", text: spec.label }), value);
        if (spec.color) box.style.setProperty("--rule", spec.color);
        readouts.append(box);
        values.push({ spec, value });
    }
    card.content.append(readouts);

    let chart = null;
    let limit = DEFAULT_LIMIT;
    if (ui.plot) {
        const canvas = h("canvas");
        const wrap = h("div", { class: "chart-wrap" }, canvas);
        const toggle = h("input", { type: "checkbox", checked: true, onchange: () => (wrap.hidden = !toggle.checked) });
        const select = h(
            "select",
            {
                class: "btn",
                "aria-label": "Historique affiché",
                onchange: () => {
                    limit = parseInt(select.value, 10);
                    trim();
                    chart.update("none");
                },
            },
            [["50", "50 points"], ["500", "500 points"], [String(DEFAULT_LIMIT), `Long terme (${DEFAULT_LIMIT})`]].map(([v, t]) =>
                h("option", { value: v, text: t, selected: v === String(DEFAULT_LIMIT) })
            )
        );
        const clear = h("button", {
            class: "btn btn-danger",
            type: "button",
            text: "Effacer le graphique",
            onclick: () => {
                chart.data.datasets[0].data.length = 0;
                chart.update("none");
            },
        });

        // Période : « En direct » (flux temps réel) ou historique (mémoire + base de données, rafraîchi toutes les 30 s).
        const history = app.info.app.history;
        const periods = PERIODS.filter(([hours]) => hours === null || (history.enabled ? hours <= history.retention_hours : hours <= 1));
        const periodSelect = h(
            "select",
            {
                class: "btn",
                "aria-label": "Période affichée",
                onchange: () => setPeriod(periodSelect.value === "" ? null : parseFloat(periodSelect.value)),
            },
            periods.map(([hours, label]) => h("option", { value: hours ?? "", text: label }))
        );
        const caption = h("p", { class: "meta-line chart-caption" });
        const exportUrl = (hours) => `/api/export.csv?instrument=${encodeURIComponent(inst.id)}&hours=${hours}`;
        const exportLink = history.enabled
            ? h("a", { class: "btn", href: exportUrl(24), download: "", text: "Exporter CSV", title: "Moyennes enregistrées (Excel)" })
            : null;

        card.hours = null;
        let ticket = 0;
        let timer = null;

        async function loadPeriod() {
            const mine = ++ticket;
            const params = new URLSearchParams({
                instrument: inst.id,
                hours: String(card.hours),
                max_points: "1500",
                keys: `timestamp,${ui.plot.key}`,
            });
            try {
                const body = await api(`/api/history?${params}`);
                if (mine !== ticket || card.hours == null) return; // période changée entre-temps
                chart.data.datasets[0].data = body.points.map((p) => ({ x: p.timestamp, y: p[ui.plot.key] }));
                caption.textContent = body.from_db
                    ? `Moyenne par tranche de ${formatDuration(body.bucket_s)}`
                    : `${body.points.length} points, pleine cadence`;
                chart.update("none");
            } catch (error) {
                caption.textContent = `Historique indisponible : ${error.message}`;
            }
        }

        function setPeriod(hours) {
            card.hours = hours;
            ticket++;
            clearInterval(timer);
            timer = null;
            select.disabled = clear.disabled = hours != null; // ces réglages ne concernent que le direct
            if (exportLink) exportLink.href = exportUrl(hours ?? 24);
            chart.data.datasets[0].data = [];
            card.lastTs = -Infinity;
            caption.textContent = "";
            if (hours == null) {
                loadHistory(card).catch(() => {});
            } else {
                loadPeriod();
                timer = setInterval(loadPeriod, 30000);
            }
            chart.update("none");
        }

        card.content.append(
            h(
                "div",
                { class: "chart-controls" },
                h("label", {}, toggle, "Afficher le graphique"),
                periodSelect,
                select,
                clear,
                exportLink
            ),
            caption,
            wrap
        );

        chart = new Chart(canvas, {
            type: "line",
            data: {
                datasets: [
                    {
                        label: ui.plot.label,
                        data: [],
                        borderColor: ui.plot.color,
                        backgroundColor: `${ui.plot.color}14`,
                        fill: true,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                parsing: false,
                normalized: true,
                interaction: { mode: "nearest", axis: "x", intersect: false },
                elements: { point: { radius: 0 }, line: { borderWidth: 2, tension: 0.1 } },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            title: (items) =>
                                card.hours ? new Date(items[0].parsed.x * 1000).toLocaleString("fr-FR") : fmtTime(items[0].parsed.x),
                        },
                    },
                },
                scales: {
                    x: {
                        type: "linear",
                        ticks: { maxTicksLimit: 6, callback: (v) => (card.hours && card.hours > 20 ? fmtDateTime(v) : fmtTime(v)) },
                    },
                    y: { title: { display: true, text: ui.plot.label } },
                },
            },
        });
    }

    function trim() {
        const points = chart.data.datasets[0].data;
        if (limit > 0 && points.length > limit) points.splice(0, points.length - limit);
    }

    let diagnostics = null;
    const diagCells = [];
    if (ui.diagnostics && ui.diagnostics.length) {
        diagnostics = h("dl", { class: "diagnostics" });
        for (const spec of ui.diagnostics) {
            const dd = h("dd", { text: "—" });
            diagnostics.append(h("div", {}, h("dt", { text: spec.label }), dd));
            diagCells.push({ spec, dd });
        }
        card.content.append(diagnostics);
    }

    card.addPoints = (points) => {
        if (!chart || card.hours != null) return; // en mode historique, le graphique ne reçoit pas le direct
        const dataset = chart.data.datasets[0].data;
        const maxGap = Math.max(5, 5 * inst.poll_interval_s);
        for (const p of points) {
            const x = p.timestamp;
            const y = p[ui.plot.key];
            if (typeof x !== "number" || typeof y !== "number" || x <= card.lastTs) continue;
            if (Number.isFinite(card.lastTs) && x - card.lastTs > maxGap) dataset.push({ x: card.lastTs + 0.001, y: null }); // trou : courbe coupée
            dataset.push({ x, y });
            card.lastTs = x;
        }
        trim();
        chart.update("none");
    };

    card.onData = (data) => {
        for (const { spec, value } of values) value.textContent = formatReading(spec, data[spec.key]);
        for (const { spec, dd } of diagCells) {
            const { text, cls } = (DIAGNOSTIC_FORMATS[spec.format] || DIAGNOSTIC_FORMATS.text)(data[spec.key], spec, data);
            dd.textContent = text;
            dd.className = cls || "";
        }
        card.addPoints([data]);
    };
}

function buildValves(inst, card) {
    const ui = inst.ui;
    const grid = h("div", { class: "valve-grid" });
    const empty = h("p", { class: "valve-empty", text: "En attente des vannes connectées…" });
    const meta = h("p", { class: "meta-line" });
    const buttons = [];

    const actions = h(
        "div",
        { class: "actions" },
        (ui.actions || []).map((action) =>
            h("button", {
                class: `btn ${action.style || ""}`,
                type: "button",
                text: action.label,
                onclick: () => sendCommand(inst, action.command, action.kwargs || {}, action.label),
            })
        )
    );
    card.content.append(grid, empty, actions, meta);

    function ensureButton(index) {
        if (buttons[index]) return buttons[index];
        const button = h("button", { class: "valve-btn", type: "button", hidden: true });
        button.addEventListener("click", async () => {
            button.disabled = true;
            try {
                await sendCommand(inst, ui.toggle_command, { index, state: button.dataset.state === "1" ? 0 : 1 });
            } finally {
                button.disabled = false;
            }
        });
        grid.append(button);
        buttons[index] = button;
        return button;
    }

    card.onData = (data) => {
        const states = data.vannes || [];
        const enabled = data.enabled || [];
        const names = data.names || [];
        let visible = 0;
        states.forEach((state, i) => {
            const button = ensureButton(i);
            const shown = enabled[i] === 1 && state !== -1; // seules les vannes raccordées sont affichées
            button.hidden = !shown;
            if (!shown) return;
            visible++;
            const name = names[i] || `Vanne ${i + 1}`;
            const signature = `${name}|${state}`;
            if (button.dataset.sig === signature) return; // rien n'a changé : on ne touche pas au DOM
            button.dataset.sig = signature;
            button.dataset.state = String(state);
            button.className = `valve-btn ${state === 1 ? "open" : "closed"}`;
            button.replaceChildren(name, h("small", { text: state === 1 ? "Ouverte" : "Fermée" }));
        });
        empty.hidden = visible > 0;
        empty.textContent = states.length ? "Aucune vanne actuellement raccordée." : "En attente des vannes connectées…";
        meta.textContent = typeof data.uptime === "number" ? `Contrôleur en marche depuis ${formatDuration(data.uptime)}` : "";
    };
}

/* ------------------------------------------------------------------ disposition (GridStack) */

function loadLayout() {
    try {
        return JSON.parse(safeStorage("get", LAYOUT_KEY) || "{}") || {};
    } catch (_) {
        return {};
    }
}

function initGrid(cards) {
    const saved = loadLayout();
    const container = $("#grid");
    for (const card of cards) {
        const layout = saved[card.inst.id];
        if (layout) {
            for (const key of ["x", "y", "w", "h"]) {
                if (Number.isInteger(layout[key])) card.item.setAttribute(`gs-${key}`, String(layout[key]));
            }
        }
        container.append(card.item);
    }
    const grid = GridStack.init({
        float: true,
        cellHeight: 80,
        margin: 8,
        draggable: { handle: ".card-header" },
        resizable: { handles: "e, se, s, sw, w" },
    });
    grid.on("change", () => {
        const layout = {};
        for (const w of grid.save(false)) if (w.id) layout[w.id] = { x: w.x, y: w.y, w: w.w, h: w.h };
        safeStorage("set", LAYOUT_KEY, JSON.stringify(layout));
    });
}

/* ------------------------------------------------------------------ état global et journal */

function updateGlobalStatus() {
    const node = $("#global-status");
    const states = [...app.cards.keys()].map((id) => (app.statuses[id] || {}).state || "connecting");
    if (!app.linkOk) {
        node.dataset.level = "bad";
        node.textContent = "Serveur injoignable — reconnexion…";
        return;
    }
    const connected = states.filter((s) => s === "connected").length;
    node.textContent = `${connected} / ${states.length} instruments connectés`;
    if (connected === states.length) node.dataset.level = "ok";
    else if (states.some((s) => s === "disconnected" || s === "stopped")) node.dataset.level = "bad";
    else node.dataset.level = "warn";
}

function dispatchData(id, data) {
    const card = app.cards.get(id);
    if (!card) return;
    try {
        card.onData(data);
    } catch (error) {
        console.error(`Affichage de « ${id} » en échec :`, error); // une carte défaillante n'empêche pas les autres
    }
}

function applyStatus(id, status) {
    app.statuses[id] = status;
    const card = app.cards.get(id);
    if (card) card.setStatus(status);
    updateGlobalStatus();
}

function addJournal(entry, { prepend = true } = {}) {
    const list = $("#journal");
    const line = h(
        "li",
        { class: entry.level },
        h("time", { text: fmtTime(entry.ts) }),
        h("span", { text: entry.message })
    );
    if (prepend) list.prepend(line);
    else list.append(line);
    while (list.children.length > JOURNAL_MAX) list.lastElementChild.remove();
}

/* ------------------------------------------------------------------ chargement et flux temps réel */

function plotCards() {
    return [...app.cards.values()].filter((card) => card.inst.ui.plot);
}

async function loadHistory(card, { since = null } = {}) {
    const key = card.inst.ui.plot.key;
    const params = new URLSearchParams({ instrument: card.inst.id, limit: String(DEFAULT_LIMIT), keys: `timestamp,${key}` });
    if (since != null && Number.isFinite(since)) params.set("since", String(since));
    const { points } = await api(`/api/history?${params}`);
    card.addPoints(points);
}

async function loadSnapshot({ resync = false } = {}) {
    const [statuses, latest, journal] = await Promise.all([api("/api/status"), api("/api/latest"), api("/api/journal")]);
    for (const [id, status] of Object.entries(statuses)) applyStatus(id, status);
    await Promise.all(plotCards().map((card) => loadHistory(card, { since: resync ? card.lastTs : null }).catch(() => {})));
    for (const [id, data] of Object.entries(latest)) dispatchData(id, data);
    $("#journal").replaceChildren();
    for (const entry of journal) addJournal(entry); // du plus ancien au plus récent, chaque ligne passe en tête
}

function openStream() {
    let everOpened = false;
    const source = new EventSource("/api/stream");
    source.addEventListener("open", () => {
        app.linkOk = true;
        updateGlobalStatus();
        if (everOpened) loadSnapshot({ resync: true }).catch(() => {}); // rattrape ce qui a été manqué
        everOpened = true;
    });
    source.addEventListener("error", () => {
        app.linkOk = false; // EventSource se reconnecte tout seul
        updateGlobalStatus();
    });
    source.addEventListener("data", (event) => {
        const { instrument, data } = JSON.parse(event.data);
        dispatchData(instrument, data);
    });
    source.addEventListener("status", (event) => {
        const { instrument, status } = JSON.parse(event.data);
        applyStatus(instrument, status);
    });
    source.addEventListener("log", (event) => addJournal(JSON.parse(event.data)));
}

async function boot() {
    try {
        app.info = await api("/api/instruments");
    } catch (_) {
        app.linkOk = false;
        updateGlobalStatus();
        setTimeout(boot, 3000);
        return;
    }
    document.title = `${app.info.app.name} — tableau de bord`;
    $("#app-title").textContent = app.info.app.name;
    $("#sim-tag").hidden = !app.info.app.simulation;

    const cards = app.info.instruments.map(buildCard);
    cards.forEach((card) => app.cards.set(card.inst.id, card));
    initGrid(cards);
    updateGlobalStatus();

    try {
        await loadSnapshot();
    } catch (error) {
        toast(`Chargement initial incomplet : ${error.message}`, true);
    }
    openStream();
}

boot();
