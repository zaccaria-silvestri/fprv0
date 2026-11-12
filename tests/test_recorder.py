import csv
from datetime import datetime

from fprv0.data.recorder import CSVRecorder, flatten


def read(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def ts(day: int, hour: int = 12) -> float:
    return datetime(2026, 1, day, hour, 0, 0).timestamp()


def test_flatten():
    assert flatten({"a": 1, "v": [0, 1], "n": {"x": 2, "y": [3]}, "none": None}) == {
        "a": 1, "v_1": 0, "v_2": 1, "n_x": 2, "n_y_1": 3, "none": "",
    }


def test_one_file_per_instrument_with_iso_timestamps(tmp_path):
    rec = CSVRecorder(tmp_path)
    rec.on_data("eip", {"timestamp": ts(5), "freq": 1.5, "gpib_ok": 1})
    rec.on_data("eip", {"timestamp": ts(5) + 1, "freq": 2.5, "gpib_ok": 1})
    rec.on_data("valves", {"timestamp": ts(5), "vannes": [0, 1, -1], "uptime": 9})
    rec.close()

    eip = read(tmp_path / "eip_2026-01-05.csv")
    assert [r["freq"] for r in eip] == ["1.5", "2.5"]
    assert eip[0]["timestamp"].startswith("2026-01-05T12:00:00.000")
    assert float(eip[0]["epoch_s"]) == round(ts(5), 3)

    valves = read(tmp_path / "valves_2026-01-05.csv")
    assert valves[0]["vannes_1"] == "0" and valves[0]["vannes_2"] == "1" and valves[0]["vannes_3"] == "-1"
    assert "vannes" not in valves[0]  # plus de liste sérialisée en texte


def test_daily_rotation(tmp_path):
    rec = CSVRecorder(tmp_path)
    rec.on_data("eip", {"timestamp": ts(5, 23), "freq": 1})
    rec.on_data("eip", {"timestamp": ts(6, 1), "freq": 2})
    rec.close()
    assert sorted(p.name for p in tmp_path.glob("*.csv")) == ["eip_2026-01-05.csv", "eip_2026-01-06.csv"]


def test_restart_appends_and_keeps_header(tmp_path):
    first = CSVRecorder(tmp_path)
    first.on_data("eip", {"timestamp": ts(5), "freq": 1, "extra": "a"})
    first.close()

    second = CSVRecorder(tmp_path)
    second.on_data("eip", {"timestamp": ts(5) + 10, "freq": 2, "extra": "b"})
    second.close()

    rows = read(tmp_path / "eip_2026-01-05.csv")
    assert [r["freq"] for r in rows] == ["1", "2"]
    lines = (tmp_path / "eip_2026-01-05.csv").read_text().splitlines()
    assert sum(1 for line in lines if line.startswith("timestamp,")) == 1  # un seul en-tête


def test_new_column_does_not_crash_and_is_ignored(tmp_path, caplog):
    rec = CSVRecorder(tmp_path)
    rec.on_data("eip", {"timestamp": ts(5), "freq": 1})
    rec.on_data("eip", {"timestamp": ts(5) + 1, "freq": 2, "surprise": 9})  # l'ancienne version levait ValueError
    rec.close()
    rows = read(tmp_path / "eip_2026-01-05.csv")
    assert len(rows) == 2 and "surprise" not in rows[1]
    assert "surprise" in caplog.text


def test_missing_column_is_left_empty(tmp_path):
    rec = CSVRecorder(tmp_path)
    rec.on_data("eip", {"timestamp": ts(5), "freq": 1, "gpib_ok": 1})
    rec.on_data("eip", {"timestamp": ts(5) + 1, "freq": 2})
    rec.close()
    assert read(tmp_path / "eip_2026-01-05.csv")[1]["gpib_ok"] == ""


def test_disk_error_never_raises_and_recovers(tmp_path, monkeypatch):
    rec = CSVRecorder(tmp_path)
    rec.on_data("eip", {"timestamp": ts(5), "freq": 1})

    original = rec._write
    monkeypatch.setattr(rec, "_write", lambda *a, **k: (_ for _ in ()).throw(OSError("disque plein")))
    rec.on_data("eip", {"timestamp": ts(5) + 1, "freq": 2})  # ne doit pas lever

    monkeypatch.setattr(rec, "_write", original)
    rec.on_data("eip", {"timestamp": ts(5) + 2, "freq": 3})  # reprise automatique
    rec.close()
    assert [r["freq"] for r in read(tmp_path / "eip_2026-01-05.csv")] == ["1", "3"]


def test_data_is_flushed_before_close(tmp_path):
    rec = CSVRecorder(tmp_path)
    rec.on_data("eip", {"timestamp": ts(5), "freq": 1})
    assert len(read(tmp_path / "eip_2026-01-05.csv")) == 1  # lisible sans attendre close()
    rec.close()
