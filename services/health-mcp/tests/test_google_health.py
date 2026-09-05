"""Tests for the Google Health API connector (google_health_client).

Network is never touched: we monkeypatch _iter_data_points to return recorded
fixture dataPoints, then assert the normalization into the shared HK schema.
These fixtures pin the assumed response contract — if the live API differs, a
failing test here points at the exact _METRIC_SPECS entry to adjust.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import google_health_client as ghc  # noqa: E402


def _pt(type_key: str, field: str, value, start="2026-01-01T08:00:00Z",
        end="2026-01-01T08:01:00Z"):
    return {type_key: {field: value, "interval": {"startTime": start, "endTime": end}}}


def _install_points(monkeypatch, mapping: dict[str, list[dict]]):
    """Route _iter_data_points(data_type,...) to the fixture list for that type."""
    def fake_iter(data_type, start, end):
        yield from mapping.get(data_type, [])
    monkeypatch.setattr(ghc, "_iter_data_points", fake_iter)


def test_steps_summed_to_daily_record(monkeypatch):
    _install_points(monkeypatch, {
        "steps": [_pt("steps", "count", 1000), _pt("steps", "count", 500)],
    })
    recs = [r for r in ghc.fetch_range(date(2026, 1, 1), date(2026, 1, 1))
            if r["_kind"] == "record" and r["type"] == "HKQuantityTypeIdentifierStepCount"]
    assert len(recs) == 1
    assert recs[0]["value"] == 1500.0            # summed within the civil day
    assert recs[0]["source_name"] == "GoogleHealth"
    assert recs[0]["unit"] == "count"


def test_distance_scaled_mm_to_km(monkeypatch):
    _install_points(monkeypatch, {
        "distance": [_pt("distance", "distanceMillimeters", 5_000_000)],  # 5 km
    })
    recs = [r for r in ghc.fetch_range(date(2026, 1, 1), date(2026, 1, 1))
            if r["type"] == "HKQuantityTypeIdentifierDistanceWalkingRunning"]
    assert recs and recs[0]["value"] == pytest.approx(5.0)
    assert recs[0]["unit"] == "km"


def test_total_calories_derives_basal(monkeypatch):
    _install_points(monkeypatch, {
        "active-energy-burned": [_pt("activeEnergyBurned", "energyKcal", 300)],
        "total-calories": [_pt("totalCalories", "energyKcal", 2000)],
    })
    out = list(ghc.fetch_range(date(2026, 1, 1), date(2026, 1, 1)))
    basal = [r for r in out if r["type"] == "HKQuantityTypeIdentifierBasalEnergyBurned"]
    active = [r for r in out if r["type"] == "HKQuantityTypeIdentifierActiveEnergyBurned"]
    assert active and active[0]["value"] == 300.0
    assert basal and basal[0]["value"] == 1700.0   # total - active


def test_total_calories_without_active_yields_no_basal(monkeypatch):
    _install_points(monkeypatch, {
        "total-calories": [_pt("totalCalories", "energyKcal", 2000)],
    })
    out = list(ghc.fetch_range(date(2026, 1, 1), date(2026, 1, 1)))
    assert not [r for r in out if r["type"] == "HKQuantityTypeIdentifierBasalEnergyBurned"]


def test_hrv_and_rhr_daily_records(monkeypatch):
    _install_points(monkeypatch, {
        "daily-heart-rate-variability": [_pt("dailyHeartRateVariability", "hrvMs", 65)],
        "daily-resting-heart-rate": [_pt("dailyRestingHeartRate", "bpm", 52)],
    })
    out = list(ghc.fetch_range(date(2026, 1, 1), date(2026, 1, 1)))
    hrv = [r for r in out if r["type"] == "HKQuantityTypeIdentifierHeartRateVariabilitySDNN"]
    rhr = [r for r in out if r["type"] == "HKQuantityTypeIdentifierRestingHeartRate"]
    assert hrv and hrv[0]["value"] == 65.0 and hrv[0]["unit"] == "ms"
    assert rhr and rhr[0]["value"] == 52.0


def test_sleep_stages_mapped(monkeypatch):
    sleep_pt = {"sleep": {"interval": {"startTime": "2026-01-01T23:00:00Z", "endTime": "2026-01-02T06:00:00Z"},
                          "stages": [
                              {"stage": "deep", "interval": {"startTime": "2026-01-01T23:00:00Z", "endTime": "2026-01-01T23:45:00Z"}},
                              {"stage": "light", "interval": {"startTime": "2026-01-01T23:45:00Z", "endTime": "2026-01-02T01:00:00Z"}},
                              {"stage": "rem", "interval": {"startTime": "2026-01-02T01:00:00Z", "endTime": "2026-01-02T02:00:00Z"}},
                              {"stage": "awake", "interval": {"startTime": "2026-01-02T02:00:00Z", "endTime": "2026-01-02T02:10:00Z"}},
                          ]}}
    _install_points(monkeypatch, {"sleep": [sleep_pt]})
    stages = [r for r in ghc.fetch_range(date(2026, 1, 1), date(2026, 1, 2)) if r["_kind"] == "sleep"]
    got = [s["stage"] for s in stages]
    assert got == ["deep", "core", "rem", "awake"]   # light -> core mapping
    assert all(s["source_name"] == "GoogleHealth" for s in stages)


def test_one_failing_metric_does_not_kill_import(monkeypatch):
    def fake_iter(data_type, start, end):
        if data_type == "heart-rate":
            raise ValueError("simulated 403 missing scope")
        if data_type == "steps":
            yield _pt("steps", "count", 900)
    monkeypatch.setattr(ghc, "_iter_data_points", fake_iter)
    recs = [r for r in ghc.fetch_range(date(2026, 1, 1), date(2026, 1, 1))
            if r["type"] == "HKQuantityTypeIdentifierStepCount"]
    assert recs and recs[0]["value"] == 900.0        # steps survived the HR failure


def test_healthcheck_reports_refresh_expired(monkeypatch):
    def boom(path, params=None):
        raise ghc.RefreshExpiredError("refresh token expired — publish to Production")
    monkeypatch.setattr(ghc, "_get", boom)
    out = ghc.healthcheck()
    assert out["ok"] is False
    assert out.get("refresh_expired") is True


def test_folder_sha_requires_token(monkeypatch):
    monkeypatch.delenv("GOOGLE_HEALTH_ACCESS_TOKEN", raising=False)
    with pytest.raises(ValueError):
        ghc.folder_sha(date(2026, 1, 1), date(2026, 1, 2))


def test_folder_sha_stable_and_range_sensitive(monkeypatch):
    monkeypatch.setenv("GOOGLE_HEALTH_ACCESS_TOKEN", "abcd1234")
    a = ghc.folder_sha(date(2026, 1, 1), date(2026, 1, 31))
    b = ghc.folder_sha(date(2026, 1, 1), date(2026, 1, 31))
    c = ghc.folder_sha(date(2026, 2, 1), date(2026, 2, 28))
    assert a == b and a != c
