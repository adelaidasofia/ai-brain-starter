"""Tests for the Apple Shortcuts bridge: normalizer + import tool + sweep."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import shortcut_normalize as sn  # noqa: E402


# --- Normalizer ----------------------------------------------------------

def _sample_payload():
    return {
        "schema_version": 1,
        "exported_at": "2026-05-10T06:00:00-05:00",
        "date": "2026-05-09",
        "device": "iPhone",
        "samples": {
            "HKQuantityTypeIdentifierHeartRate": [
                {"start": "2026-05-09T08:00:00-05:00", "end": "2026-05-09T08:00:30-05:00",
                 "value": 62, "unit": "count/min", "source": "Apple Watch"},
                {"start": "2026-05-09T08:01:00-05:00", "end": "2026-05-09T08:01:30-05:00",
                 "value": 65, "unit": "count/min", "source": "Apple Watch"},
            ],
            "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": [
                {"start": "2026-05-09T03:00:00-05:00", "end": "2026-05-09T03:00:00-05:00",
                 "value": 38.2, "unit": "ms", "source": "Apple Watch"},
            ],
            "HKQuantityTypeIdentifierStepCount": [
                {"start": "2026-05-09T00:00:00-05:00", "end": "2026-05-09T23:59:59-05:00",
                 "value": 8421, "unit": "count", "source": "iPhone"},
            ],
        },
        "sleep": [
            {"start": "2026-05-08T23:00:00-05:00", "end": "2026-05-09T00:30:00-05:00",
             "stage": "Core", "source": "Apple Watch"},
            {"start": "2026-05-09T00:30:00-05:00", "end": "2026-05-09T02:00:00-05:00",
             "stage": "REM",  "source": "Apple Watch"},
            {"start": "2026-05-09T02:00:00-05:00", "end": "2026-05-09T03:30:00-05:00",
             "stage": "Deep", "source": "Apple Watch"},
        ],
        "workouts": [
            {"activity_type": "Running", "start": "2026-05-09T07:00:00-05:00",
             "end": "2026-05-09T07:32:00-05:00",
             "duration_min": 32, "distance_km": 5.2, "energy_kcal": 320,
             "source": "Apple Watch"},
        ],
        "mindful": [
            {"start": "2026-05-09T06:00:00-05:00", "end": "2026-05-09T06:10:00-05:00",
             "duration_min": 10, "source": "Mindfulness"},
        ],
        "cycle": [
            {"type": "MenstrualFlow", "start": "2026-05-09T00:00:00-05:00",
             "value": "medium", "source": "Cycle Tracking"},
        ],
        "state_of_mind": [
            {"start": "2026-05-09T20:00:00-05:00", "end": "2026-05-09T20:00:00-05:00",
             "kind": "momentary", "valence": 0.4,
             "labels": "calm,focused", "associations": "writing",
             "source": "Mindfulness"},
        ],
    }


def test_iter_payload_yields_all_kinds():
    items = list(sn.iter_payload(_sample_payload()))
    by_kind: dict[str, int] = {}
    for it in items:
        by_kind[it["_kind"]] = by_kind.get(it["_kind"], 0) + 1
    # 4 records (2 HR, 1 HRV, 1 steps) + 1 mindful (as record) = 5 records
    assert by_kind["record"] == 5
    assert by_kind["sleep"] == 3
    assert by_kind["workout"] == 1
    assert by_kind["cycle"] == 1
    assert by_kind["state_of_mind"] == 1


def test_iter_payload_empty_payload_yields_nothing():
    assert list(sn.iter_payload({})) == []
    assert list(sn.iter_payload({"samples": {}, "sleep": [], "workouts": []})) == []


def test_iter_payload_handles_missing_optional_fields():
    p = {
        "samples": {"HKQuantityTypeIdentifierHeartRate": [{"start": "2026-05-09T08:00:00-05:00", "value": 62}]},
    }
    items = list(sn.iter_payload(p))
    assert len(items) == 1
    item = items[0]
    assert item["_kind"] == "record"
    assert item["unit"] == ""  # default
    assert item["end_date"] == item["start_date"]  # default end = start
    assert item["source_name"] == "Apple Health"  # default source


def test_iter_payload_string_values_go_to_value_str():
    p = {
        "samples": {"HKCategoryTypeIdentifierMenstrualFlow": [
            {"start": "2026-05-09T00:00:00-05:00", "value": "medium", "source": "Cycle Tracking"}
        ]},
    }
    items = list(sn.iter_payload(p))
    assert items[0]["value"] is None
    assert items[0]["value_str"] == "medium"


def test_sleep_stage_aliases_normalize():
    p = {"sleep": [
        {"start": "a", "end": "b", "stage": "AsleepREM"},
        {"start": "c", "end": "d", "stage": "AsleepDeep"},
        {"start": "e", "end": "f", "stage": "AsleepCore"},
        {"start": "g", "end": "h", "stage": "In Bed"},
    ]}
    items = list(sn.iter_payload(p))
    stages = [it["stage"] for it in items]
    assert stages == ["REM", "Deep", "Core", "InBed"]


def test_workout_numeric_coercion():
    p = {"workouts": [{"activity_type": "Running", "start": "a", "end": "b",
                       "duration_min": "32", "distance_km": "5.2", "energy_kcal": 320}]}
    items = list(sn.iter_payload(p))
    assert items[0]["duration_min"] == 32.0
    assert items[0]["distance_km"] == 5.2
    assert items[0]["energy_kcal"] == 320.0


def test_mindful_session_emitted_as_record():
    p = {"mindful": [{"start": "a", "end": "b", "duration_min": 10, "source": "Mindfulness"}]}
    items = list(sn.iter_payload(p))
    assert len(items) == 1
    assert items[0]["_kind"] == "record"
    assert items[0]["type"] == "HKCategoryTypeIdentifierMindfulSession"
    assert items[0]["unit"] == "min"
    assert items[0]["value"] == 10.0


def test_skips_malformed_entries_silently():
    p = {
        "samples": {"HKQuantityTypeIdentifierHeartRate": ["not a dict", None, {"start": "a", "value": 62}]},
        "sleep": ["not a dict", {"start": "a", "end": "b", "stage": "REM"}],
    }
    items = list(sn.iter_payload(p))
    # Malformed entries dropped; valid ones kept
    assert sum(1 for it in items if it["_kind"] == "record") == 1
    assert sum(1 for it in items if it["_kind"] == "sleep") == 1


# --- File I/O + SHA ------------------------------------------------------

def test_load_payload_file_round_trip(tmp_path):
    p = _sample_payload()
    f = tmp_path / "2026-05-09.json"
    f.write_text(json.dumps(p), encoding="utf-8")
    loaded = sn.load_payload_file(f)
    assert loaded["date"] == "2026-05-09"
    assert loaded["schema_version"] == 1


def test_load_payload_file_rejects_non_object(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        sn.load_payload_file(f)


def test_payload_sha_stable(tmp_path):
    f = tmp_path / "x.json"
    f.write_text('{"a": 1}', encoding="utf-8")
    a = sn.payload_sha(f)
    b = sn.payload_sha(f)
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_payload_sha_changes_on_content(tmp_path):
    f = tmp_path / "x.json"
    f.write_text('{"a": 1}', encoding="utf-8")
    a = sn.payload_sha(f)
    f.write_text('{"a": 2}', encoding="utf-8")
    b = sn.payload_sha(f)
    assert a != b


def test_default_inbox_returns_icloud_path():
    p = sn.default_inbox()
    assert "Mobile Documents" in str(p)
    assert "com~apple~CloudDocs" in str(p)
    assert p.name == "health-mcp"


# --- End-to-end: import + sweep -----------------------------------------

def test_health_import_shortcut_e2e(tmp_path, monkeypatch):
    """Write a payload, call health_import_shortcut, verify rows in DB."""
    # Isolate DB to a tmp path so the test doesn't touch the real ~/.claude/health-mcp/
    monkeypatch.setenv("HOME", str(tmp_path))
    # USERPROFILE as well, or this isolation is a no-op on Windows: db.db_path()
    # reads Path.home(), and ntpath.expanduser resolves that from USERPROFILE and
    # ignores HOME entirely — so HOME alone pointed this test at the developer's
    # REAL health.duckdb and wrote fixture rows into it (MYC-3536).
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # db.connect() creates the dir on first use.

    # Re-import db with the new HOME so the path is recomputed.
    if "db" in sys.modules:
        del sys.modules["db"]
    if "main" in sys.modules:
        del sys.modules["main"]
    # main imports fastmcp; if not available skip the e2e
    try:
        import main as health_main  # noqa: F401
    except ImportError:
        pytest.skip("fastmcp not available for end-to-end tool test")

    payload = _sample_payload()
    f = tmp_path / "2026-05-09.json"
    f.write_text(json.dumps(payload), encoding="utf-8")

    # Import via the MCP tool (callable as plain function in-process)
    res = health_main.health_import_shortcut(str(f))
    assert res["skipped"] is False
    assert res["rows_inserted"] > 0
    assert res["records_count"] >= 4
    assert res["sleep_count"] == 3
    assert res["workouts_count"] == 1

    # Idempotency: re-importing same file is a no-op
    res2 = health_main.health_import_shortcut(str(f))
    assert res2["skipped"] is True


def test_health_sweep_inbox_processes_and_archives(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows reads "~" from this (MYC-3536)
    if "db" in sys.modules:
        del sys.modules["db"]
    if "main" in sys.modules:
        del sys.modules["main"]
    try:
        import main as health_main  # noqa: F401
    except ImportError:
        pytest.skip("fastmcp not available")

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    p1 = _sample_payload()
    p2 = {**_sample_payload(), "date": "2026-05-08"}
    (inbox / "2026-05-09.json").write_text(json.dumps(p1), encoding="utf-8")
    (inbox / "2026-05-08.json").write_text(json.dumps(p2), encoding="utf-8")

    res = health_main.health_sweep_shortcut_inbox(str(inbox), archive=True)
    assert res["files_processed"] == 2
    # Files moved to processed/
    assert not list(inbox.glob("*.json")) or all(p.parent.name == "processed" for p in inbox.rglob("*.json"))
    assert (inbox / "processed" / "2026-05-09.json").exists()
    assert (inbox / "processed" / "2026-05-08.json").exists()


def test_health_sweep_returns_zero_when_inbox_missing(tmp_path):
    if "db" in sys.modules:
        del sys.modules["db"]
    if "main" in sys.modules:
        del sys.modules["main"]
    try:
        import main as health_main
    except ImportError:
        pytest.skip("fastmcp not available")
    res = health_main.health_sweep_shortcut_inbox(str(tmp_path / "does-not-exist"))
    assert res["files_processed"] == 0
