"""v0.3 smoke tests covering vendor clients (Oura + Fitbit) + vendor setup guide.

Vendor API calls are NOT made in tests (no live network). We test:
  - Module imports
  - Token validation (raises ValueError when env var missing)
  - SHA generation (deterministic on same range)
  - Vendor setup guide returns the right shape per vendor + OS
  - Setup guide handles unsupported vendor + alias normalization
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import fitbit_client  # noqa: E402
import oura_client  # noqa: E402
import vendor_setup  # noqa: E402


# ---------------------------------------------------------------------------
# 01: vendor clients import + token validation
# ---------------------------------------------------------------------------

def test_oura_token_required(monkeypatch):
    monkeypatch.delenv("OURA_PERSONAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("OURA_PAT", raising=False)
    with pytest.raises(ValueError, match="OURA_PERSONAL_ACCESS_TOKEN"):
        oura_client._token()


def test_oura_token_resolves_when_set(monkeypatch):
    monkeypatch.setenv("OURA_PERSONAL_ACCESS_TOKEN", "test-token-abcd")
    assert oura_client._token() == "test-token-abcd"


def test_fitbit_token_required(monkeypatch):
    monkeypatch.delenv("FITBIT_ACCESS_TOKEN", raising=False)
    with pytest.raises(ValueError, match="FITBIT_ACCESS_TOKEN"):
        fitbit_client._token()


def test_oura_folder_sha_deterministic(monkeypatch):
    monkeypatch.setenv("OURA_PERSONAL_ACCESS_TOKEN", "test-token")
    s1 = oura_client.folder_sha(date(2026, 1, 1), date(2026, 5, 10))
    s2 = oura_client.folder_sha(date(2026, 1, 1), date(2026, 5, 10))
    assert s1 == s2
    s3 = oura_client.folder_sha(date(2026, 1, 1), date(2026, 5, 11))
    assert s1 != s3


def test_fitbit_folder_sha_deterministic(monkeypatch):
    monkeypatch.setenv("FITBIT_ACCESS_TOKEN", "test-fb-token")
    s1 = fitbit_client.folder_sha(date(2026, 1, 1), date(2026, 5, 10))
    s2 = fitbit_client.folder_sha(date(2026, 1, 1), date(2026, 5, 10))
    assert s1 == s2


# ---------------------------------------------------------------------------
# 02: vendor setup guide
# ---------------------------------------------------------------------------

def test_apple_health_guide_macos():
    g = vendor_setup.vendor_setup_guide("apple_health", "macos")
    assert g["vendor"] == "apple_health"
    assert g["os"] == "macos"
    assert "AirDrop" in " ".join(g.get("transfer_steps", []))
    assert g["tool_to_run"] == "health_import_xml"


def test_oura_guide_windows():
    g = vendor_setup.vendor_setup_guide("oura", "windows")
    assert g["vendor"] == "oura"
    assert g["os"] == "windows"
    assert "PowerShell" in " ".join(g["transfer_steps"])
    assert "OURA_PERSONAL_ACCESS_TOKEN" in g["env_vars"]


def test_fitbit_guide_linux():
    g = vendor_setup.vendor_setup_guide("fitbit", "linux")
    assert g["vendor"] == "fitbit"
    assert g["os"] == "linux"
    assert "FITBIT_ACCESS_TOKEN" in g["env_vars"]
    assert "FITBIT_REFRESH_TOKEN" in g["env_vars"]


def test_vendor_alias_normalization():
    """'apple' / 'apple watch' / 'iphone' / 'ios' all map to apple_health."""
    for alias in ["apple", "apple_watch", "apple watch", "iphone", "ios"]:
        g = vendor_setup.vendor_setup_guide(alias, "macos")
        assert g["vendor"] == "apple_health"


def test_unsupported_vendor_returns_error():
    g = vendor_setup.vendor_setup_guide("polar_xyz", "macos")
    assert "error" in g
    assert "Unsupported" in g["error"]


def test_garmin_guide_routes_via_apple_health():
    g = vendor_setup.vendor_setup_guide("garmin", "macos")
    assert g["vendor"] == "garmin"
    assert "apple_health" in g["tool_to_run"].lower() or "Apple Health" in g["summary"]


def test_whoop_marked_deferred():
    g = vendor_setup.vendor_setup_guide("whoop", "macos")
    assert "v0.4" in g["tool_to_run"] or "v0.4" in g["summary"]


def test_supported_vendors_set_contains_expected():
    expected = {"apple_health", "oura", "fitbit", "google_health", "garmin", "whoop"}
    assert vendor_setup.SUPPORTED_VENDORS == expected
