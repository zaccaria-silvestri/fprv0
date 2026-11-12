import sqlite3
import time

import pytest

from fprv0.data.history import HistoryDB, build_history, decimate, insert_gaps
from fprv0.instruments.eip_25b import EIP25B
from fprv0.instruments.swagelok_dp import SwagelokDP

NOW = time.time()


def make_db(tmp_path, **kwargs):
    kwargs.setdefault("extractors", {"eip": EIP25B.history_values})
    return HistoryDB(tmp_path / "h.db", period_s=60, retention_days=30, **kwargs)


def sample_rows(inst, key, start_s, count, step_s, value):
    return [(inst, key, int((start_s + i * step_s) * 1000), value(i)) for i in range(count)]


# --------------------------------------------------------------------------- drivers

def test_default_history_values_are_the_displayed_readings():
    assert EIP25B.history_values({"freq": 5.0, "gpib_ok": 1, "offset": 0.1}) == {"freq": 5.0}


def test_valve_history_is_one_series_per_connected_valve():
    data = {"vannes": [1, 0, -1, 1], "enabled": [1, 1, 1, 0]}
    assert SwagelokDP.history_values(data) == {"vanne_1": 1.0, "vanne_2": 0.0}


# --------------------------------------------------------------------------- cumul et moyennes

def test_flush_stores_the_average_per_period(tmp_path):
    db = make_db(tmp_path)
    for freq in (10.0, 20.0, 30.0):
        db.on_data("eip", {"freq": freq})
    db.flush()
    rows = db.export_rows("eip", 0)
    assert len(rows) == 1 and rows[0][1] == {"freq": 20.0}
    db.flush()  # rien de neuf : pas de ligne en double
    assert len(db.export_rows("eip", 0)) == 1


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), True, "12", [1]])
def test_non_numeric_values_are_ignored(tmp_path, bad):
    db = make_db(tmp_path)
    db.on_data("eip", {"freq": bad})
    db.on_data("eip", {"freq": 4.0})
    db.flush()
    assert db.export_rows("eip", 0)[0][1] == {"freq": 4.0}


def test_unknown_instrument_and_failing_extractor_are_harmless(tmp_path):
    def boom(data):
        raise RuntimeError("extracteur cassé")

    db = make_db(tmp_path, extractors={"bad": boom})
    db.on_data("nobody", {"freq": 1})
    db.on_data("bad", {"freq": 1})
    db.flush()
    assert db.export_rows("bad", 0) == []


def test_close_saves_the_partial_period(tmp_path):
    db = make_db(tmp_path)
    db.start()
    db.on_data("eip", {"freq": 7.0})
    db.close()  # arrêt de l'application : la période en cours n'est pas perdue
    assert db.export_rows("eip", 0)[0][1] == {"freq": 7.0}


def test_writer_thread_flushes_by_itself(tmp_path):
    db = HistoryDB(tmp_path / "h.db", extractors={"eip": EIP25B.history_values}, period_s=1)
    db.start()
    try:
        db.on_data("eip", {"freq": 3.0})
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not db.export_rows("eip", 0):
            time.sleep(0.1)
        assert db.export_rows("eip", 0)[0][1] == {"freq": 3.0}
    finally:
        db.close()


# --------------------------------------------------------------------------- requêtes

def test_buckets_average_per_time_slice(tmp_path):
    db = make_db(tmp_path)
    start = NOW - 3600
    db.insert_samples(sample_rows("eip", "freq", start, 60, 60, lambda i: float(i)))  # 1 point par minute
    db.insert_samples(sample_rows("other", "freq", start, 60, 60, lambda i: 999.0))  # autre instrument : ignoré

    rows = db.buckets("eip", ["freq"], start, NOW, bucket_s=600)  # tranches de 10 minutes
    assert 5 <= len(rows) <= 7
    values = [v["freq"] for _, v in rows]
    assert values == sorted(values) and max(values) < 60 and min(values) >= 0
    assert all(ts_a < ts_b for (ts_a, _), (ts_b, _) in zip(rows, rows[1:], strict=False))


def test_buckets_respect_bounds_and_keys(tmp_path):
    db = make_db(tmp_path)
    db.insert_samples([("eip", "freq", int((NOW - 100) * 1000), 1.0), ("eip", "temp", int((NOW - 100) * 1000), 2.0)])
    assert db.buckets("eip", ["freq"], NOW - 50, NOW, 10) == []
    assert db.buckets("eip", [], NOW - 200, NOW, 10) == []
    rows = db.buckets("eip", ["freq", "temp"], NOW - 200, NOW, 10)
    assert rows[0][1] == {"freq": 1.0, "temp": 2.0}


def test_purge_removes_old_samples_and_journal_but_keeps_audit(tmp_path):
    db = make_db(tmp_path)
    old = int((NOW - 40 * 86400) * 1000)  # au-delà de la rétention (30 j)
    recent = int((NOW - 3600) * 1000)
    db.insert_samples([("eip", "freq", old, 1.0), ("eip", "freq", recent, 2.0)])
    with db._connect() as c:
        c.execute("INSERT INTO journal VALUES (?, 'INFO', 'vieux')", (old,))
        c.execute("INSERT INTO journal VALUES (?, 'INFO', 'récent')", (recent,))
        c.execute("INSERT INTO audit VALUES (?, '1.2.3.4', 'eip', 'x', '{}', 'ok')", (old,))
    db._purge()
    assert [v["freq"] for _, v in db.export_rows("eip", 0)] == [2.0]
    assert [e["message"] for e in db.recent_journal()] == ["récent"]
    assert len(db.recent_audit()) == 1


# --------------------------------------------------------------------------- journal et audit

def test_journal_and_audit_survive_restart(tmp_path):
    first = make_db(tmp_path)
    first.on_log("WARNING", "[eip] hors ligne", NOW - 5)
    first.on_log("INFO", "[eip] connecté", NOW)
    first.record_audit("10.0.0.7", "valves", "toggle_all", {"state": 1}, "ok")
    first.flush()

    second = make_db(tmp_path)  # redémarrage
    journal = second.recent_journal()
    assert [e["message"] for e in journal] == ["[eip] hors ligne", "[eip] connecté"]  # ordre chronologique
    assert journal[0]["level"] == "WARNING" and abs(journal[0]["ts"] - (NOW - 5)) < 0.01
    audit = second.recent_audit()[0]
    assert audit["remote"] == "10.0.0.7" and audit["params"] == {"state": 1} and audit["outcome"] == "ok"


# --------------------------------------------------------------------------- panne disque

def test_failed_write_is_retried_without_losing_averages(tmp_path, monkeypatch):
    db = make_db(tmp_path)
    db.on_data("eip", {"freq": 5.0})

    real = db.insert_samples
    monkeypatch.setattr(db, "insert_samples", lambda rows: (_ for _ in ()).throw(sqlite3.OperationalError("disque plein")))
    db.flush()  # échec : ne lève pas
    assert db.export_rows("eip", 0) == []

    monkeypatch.setattr(db, "insert_samples", real)
    time.sleep(0.01)  # deux périodes réelles sont séparées de plusieurs secondes : pas de collision de ts
    db.on_data("eip", {"freq": 9.0})
    db.flush()
    values = sorted(v["freq"] for _, v in db.export_rows("eip", 0))
    assert values == [5.0, 9.0]  # la moyenne en échec a été rejouée


# --------------------------------------------------------------------------- historique hybride

def mem_points(now, seconds, key="freq"):
    return [{"timestamp": now - seconds + i, key: 100.0 + i} for i in range(seconds)]


def test_short_window_comes_from_memory_only(tmp_path):
    db = make_db(tmp_path)
    result = build_history(mem_points(NOW, 600), db, "eip", ["freq"], since=NOW - 600, now=NOW, max_points=1500)
    assert not result["from_db"] and len(result["points"]) == 600


def test_long_window_completes_memory_with_database(tmp_path):
    db = make_db(tmp_path)
    db.insert_samples(sample_rows("eip", "freq", NOW - 6 * 3600, 300, 60, lambda i: 50.0))  # 5 h en base
    result = build_history(mem_points(NOW, 900), db, "eip", ["freq"], since=NOW - 6 * 3600, now=NOW, max_points=1500)
    points = result["points"]
    assert result["from_db"] and result["bucket_s"] >= 60
    stamps = [p["timestamp"] for p in points]
    assert stamps == sorted(stamps)
    assert any(p["freq"] == 50.0 for p in points) and any((p["freq"] or 0) >= 100.0 for p in points)
    assert len(points) <= 1500
    mem_start = NOW - 900  # pas de doublon : la base ne couvre que ce que la mémoire n'a pas
    assert not any(p["freq"] == 50.0 and p["timestamp"] >= mem_start for p in points)


def test_gap_in_database_breaks_the_curve(tmp_path):
    db = make_db(tmp_path)
    start = NOW - 4 * 3600
    rows = sample_rows("eip", "freq", start, 30, 60, lambda i: 1.0)  # 30 min de données…
    rows += sample_rows("eip", "freq", start + 3 * 3600, 30, 60, lambda i: 2.0)  # …un trou de 2 h 30…
    db.insert_samples(rows)
    result = build_history([], db, "eip", ["freq"], since=NOW - 5 * 3600, now=NOW, max_points=1500)
    assert any(p["freq"] is None for p in result["points"])  # …marqué par un point vide


def test_without_database_only_memory_is_used():
    result = build_history(mem_points(NOW, 100), None, "eip", ["freq"], since=NOW - 3600, now=NOW, max_points=1500)
    assert not result["from_db"]
    assert 0 < len(result["points"]) <= 100  # fenêtre d'1 h sur 1500 points : un point toutes les 3 s au plus


def test_decimate_and_gaps_helpers():
    points = [{"timestamp": float(i), "v": float(i)} for i in range(100)]
    out = decimate(points, ["v"], 10)
    assert len(out) == 10 and out[0]["v"] == pytest.approx(2.0, abs=3)
    assert decimate(points, ["v"], 500) is points
    spaced = [{"timestamp": 0.0, "v": 1.0}, {"timestamp": 100.0, "v": 2.0}]
    assert [p["v"] for p in insert_gaps(spaced, ["v"], 10)] == [1.0, None, 2.0]
    assert [p["v"] for p in insert_gaps(spaced, ["v"], 1000)] == [1.0, 2.0]


def test_timestamp_in_keys_never_overwrites_the_time_axis(tmp_path):
    """Le navigateur demande keys=timestamp,freq : 'timestamp' ne doit pas être traité comme une mesure."""
    db = make_db(tmp_path)
    db.insert_samples(sample_rows("eip", "freq", NOW - 3 * 3600, 60, 60, lambda i: 5.0))
    result = build_history(mem_points(NOW, 300), db, "eip", ["timestamp", "freq"], since=NOW - 4 * 3600, now=NOW, max_points=1500)
    assert result["from_db"]
    assert all(isinstance(p["timestamp"], float) for p in result["points"])
    assert set(result["points"][0]) == {"timestamp", "freq"}
