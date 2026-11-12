# fprv0

Pilotage et acquisition pour un banc de mesure (fréquencemètre EIP 25B, multimètre Keithley 2000,
vannes pneumatiques SwagelokDP/ESP32), avec un tableau de bord **web** temps réel.

Version 0.3 : base de données historique (SQLite), historique par période et export CSV dans le tableau de bord.
Version 0.2 : l'interface Qt a été supprimée, le cœur est indépendant de toute bibliothèque graphique.

## Installation

```bash
python -m venv .venv && source .venv/bin/activate     # Python >= 3.11
pip install -e ".[dev]"
```

## Lancement

```bash
fprv0 --sim                 # tous les instruments simulés, sans matériel
fprv0                       # selon config/app.toml et le « transport » de chaque instrument
fprv0 --no-sim              # force « pas de simulation globale » même si app.toml dit true
```

Puis ouvrir <http://127.0.0.1:5000>. Options : `--config-dir`, `--host`, `--port`, `--log-level`.
Ctrl+C ou SIGTERM arrêtent proprement les workers et ferment les fichiers CSV.

## Configuration (`config/`)

`app.toml` (section `[app]`) : `simulation_mode`, `web_host`, `web_port`, `api_token`, `data_dir`,
`save_csv`, `history_points`, `history_db`, `db_file`, `db_period_s`, `db_retention_days`. `instruments.toml` : un bloc par instrument.

```toml
[eip_counter]                 # l'identifiant sert d'URL, de nom de fichier CSV et d'ID d'API
type = "eip_25b"              # driver
transport = "http"            # "http" (réel) ou "sim" (simulateur)
address = "http://192.168.0.103/api"
poll_interval_s = 1.0
# optionnels : label, enabled, timeout_s, retry_min_s, retry_max_s, max_failures
```

**Simulation, ordre de priorité** : `--sim` / `--no-sim` > `simulation_mode` d'`app.toml` > `transport`
de chaque instrument. On peut donc simuler un seul instrument en mettant `transport = "sim"` sur lui.
Toute clé inconnue ou mal typée est refusée au démarrage avec un message explicite.

Le dossier de config est cherché dans : `--config-dir`, `$FPRV0_CONFIG_DIR`, `./config`, puis le dossier du dépôt.

## Sécurité

- Écoute sur `127.0.0.1` par défaut. Écouter ailleurs (`0.0.0.0`…) **est refusé sans jeton d'API**
  (`api_token` ou variable `FPRV0_API_TOKEN`, cette dernière étant prioritaire).
- `POST /api/action` exige ce jeton (en-tête `X-API-Token` ou `Authorization: Bearer`) quand il est défini ;
  le navigateur le demande une fois et le garde pour la session. Les lectures (`GET`) restent ouvertes.
- **Liste blanche** : seules les méthodes décorées `@command` d'un driver sont appelables ; tout le reste → 404.
  Les paramètres sont validés d'après leurs annotations de type (ex. index de vanne 0 à 7).
- Les commandes `dangerous=True` (tout ouvrir/fermer, reboot) exigent `"confirmed": true` côté API et
  une modale de confirmation côté interface.
- Corps JSON obligatoire (anti-CSRF par preflight CORS), en-têtes `Content-Security-Policy`, `X-Frame-Options`.
- Une commande n'est jamais mise en attente : si l'instrument est hors ligne, elle est refusée (HTTP 409)
  plutôt qu'exécutée plus tard à une reconnexion inattendue.

Exemple d'appel :

```bash
curl -X POST http://127.0.0.1:5000/api/action -H 'Content-Type: application/json' -H "X-API-Token: $FPRV0_API_TOKEN" \
     -d '{"instrument_id":"swagelok_valves","action":"toggle_all","kwargs":{"state":0},"confirmed":true}'
```

## Résilience

Chaque instrument a son thread (`core/worker.py`), avec les états `connecting → connected ⇄ degraded → disconnected`.
Un échec de mesure passe en *degraded* ; après `max_failures` échecs consécutifs, l'instrument est déconnecté
puis reconnecté avec un backoff exponentiel (`retry_min_s` → `retry_max_s`, avec gigue). Mesures et commandes
sont sérialisées dans ce thread, les attentes sont interruptibles (arrêt immédiat) et la cadence ne dérive pas.
L'état de chaque instrument est visible dans l'interface et le journal.

## Enregistrement CSV

Un fichier par instrument et par jour : `data/<instrument>_AAAA-MM-JJ.csv` (jour = heure locale, rotation à minuit).
Colonnes aplaties (`vannes` → `vannes_1 … vannes_8`), `timestamp` en ISO 8601 avec décalage horaire et `epoch_s`.
Après un redémarrage le fichier du jour est complété en conservant son en-tête. Une erreur disque est journalisée
sans interrompre l'acquisition. Désactivable avec `save_csv = false`.

## Base de données historique

Fichier SQLite `data/history.db` (mode WAL), complémentaire de la mémoire (1 h à pleine cadence) et des CSV (trace brute) :

| Table | Contenu |
|---|---|
| `samples` | une **moyenne** par instrument, par mesure et par période (`db_period_s`, 60 s) ; format « long » : ajouter un instrument ne change jamais le schéma |
| `journal` | les messages du journal de l'interface, rechargés au démarrage |
| `audit` | chaque commande tentée : date, adresse IP, instrument, action, paramètres, résultat |

- Les écritures passent par **un thread dédié** : les workers d'acquisition n'attendent jamais le disque. Une écriture qui échoue est
  retentée au cycle suivant sans perdre les moyennes ; à l'arrêt, la période en cours est enregistrée.
- Purge automatique des moyennes et du journal au-delà de `db_retention_days` (90 j). L'audit est conservé.
- Chaque driver choisit ce qu'il historise via `history_values()` (par défaut : ses `readings` ; les vannes : une série `vanne_N`
  dont la moyenne est la fraction de temps ouverte).
- Si la base est inaccessible au démarrage, l'acquisition continue (mémoire + CSV) et l'erreur est journalisée.
- Taille indicative : 3 instruments, 60 s, 90 jours ≈ 1 à 2 millions de lignes (quelques dizaines de Mo).
- Sauvegarde à chaud : `sqlite3 data/history.db ".backup sauvegarde.db"` (ne copiez pas le fichier seul : le mode WAL en a d'autres).

Dans le tableau de bord, chaque graphique propose une **période** (En direct, 1 h, 6 h, 24 h, 7 j, 30 j) et un bouton **Exporter CSV**
(séparateur `;`, virgule décimale, BOM UTF-8 : s'ouvre directement dans Excel). Une période longue combine la base (anciennes
moyennes) et la mémoire (récent, pleine cadence) ; les trous (instrument hors ligne) coupent la courbe au lieu d'être reliés.

## API

| Route | Rôle |
|---|---|
| `GET /api/instruments` | description de chaque instrument : carte à afficher (`ui`) et commandes disponibles |
| `GET /api/status`, `/api/latest`, `/api/journal` | états, dernières mesures, journal |
| `GET /api/history?instrument=ID&limit=N&since=T&keys=a,b` | historique récent en mémoire (`history_points`) |
| `GET /api/history?instrument=ID&hours=H&max_points=N` | historique sur une période : base + mémoire, décimé à ~N points |
| `GET /api/export.csv?instrument=ID&hours=H` | moyennes enregistrées, au format Excel FR |
| `GET /api/audit?limit=N` | dernières commandes tentées (qui, quand, quoi, résultat) |
| `GET /api/stream` | flux SSE (`data`, `status`, `log`), un flux indépendant par navigateur |
| `POST /api/action` | `{instrument_id, action, kwargs, confirmed}` : exécute une commande déclarée |

## Ajouter un instrument

1. Décrire la réponse de l'appareil dans `instruments/models.py` (modèle pydantic).
2. Créer le driver en sous-classant `HTTPJSONInstrument` : `model`, `ui` (carte déclarative), commandes `@command`.
3. Écrire son simulateur (surcharger `connect`, `disconnect`, `fetch_raw` et les méthodes `_xxx` des commandes).
4. Enregistrer avec `@register_instrument("mon_type")` / `@register_sim("mon_type")` et l'importer dans `instruments/__init__.py`.
5. Ajouter un bloc dans `instruments.toml`.

L'interface web n'a rien à modifier : `ui["kind"]` vaut `readings` (valeurs + graphique + diagnostic) ou `valves`.
Un nouveau type de carte se code dans `static/app.js` (`buildReadings` / `buildValves` servent de modèles).

```python
@register_instrument("mon_type")
class MonInstrument(HTTPJSONInstrument):
    model = MaLecture
    ui = {"kind": "readings", "title": "Mon instrument", "icon": "📟",
          "readings": [{"key": "valeur", "label": "Valeur", "format": "fixed", "decimals": 3, "unit": "V", "color": "#1c7ed6"}],
          "plot": {"key": "valeur", "label": "Valeur (V)", "color": "#1c7ed6"}}

    @command("Remettre à zéro", dangerous=True)
    def zero(self):
        return self._get(f"{self.address}/zero")
```

## Architecture

```
transports/   accès bas niveau (HTTP livré ; VISA et série prévus, extras optionnels, sans driver)
instruments/  drivers + simulateurs, validation pydantic, commandes @command
core/         bus d'événements, worker résilient, manager, store (historique), registre
data/         enregistreur CSV (trace brute) et base de données historique (SQLite)
web/          Flask (API + SSE) et tableau de bord (static/, sans CDN : Chart.js et GridStack sont embarqués)
```

## Tests

```bash
pytest          # 109 tests : config, commandes, drivers, worker, CSV, base historique, API web, SSE
ruff check src tests
```
