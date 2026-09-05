"""Google Health API client (OAuth2, cloud REST).

The Google Health API (https://developers.google.com/health) is the successor
to the Fitbit Web API and Google Fit REST API, both of which Google is retiring
from September 2026. It unifies Fitbit + Pixel Watch + third-party device data
under one OAuth2 cloud REST surface.

User sets up a Google Cloud project + OAuth Web-Server client + test user and
runs a one-time OAuth flow to obtain access + refresh tokens. Credentials go in:
  GOOGLE_HEALTH_ACCESS_TOKEN   (short-lived, auto-refreshed here)
  GOOGLE_HEALTH_REFRESH_TOKEN
  GOOGLE_HEALTH_CLIENT_ID
  GOOGLE_HEALTH_CLIENT_SECRET

IMPORTANT (see vendor_setup.py google_health guide): while the OAuth app is in
"Testing" status Google expires refresh tokens after 7 days. For ongoing daily
auto-sync the app must be published to Production.

Endpoints (v4):
  GET  /users/me/identity
  GET  /users/me/dataTypes/{dataType}/dataPoints          (paged via nextPageToken)

Normalization: every datum is mapped to the same DuckDB row shapes emitted by
parse_xml.iter_records (_kind in {record, sleep}) so scores.py / vault_aware and
the rest of the substrate don't need to know the data came from Google.

Response-shape note: the Google Health API returns *typed* dataPoints — each
point carries a nested object keyed by the data type (e.g. a point in the
`steps` collection has a `steps` object, one in `weight` has a `weight` object)
plus an `interval` {startTime, endTime}. The exact inner value field names are
only partially documented publicly. Every inner-field path below is marked
`# VERIFY` where it is doc-informed rather than doc-confirmed; the fixture tests
pin the assumed contract, so a live-API mismatch is a one-line fix in
_METRIC_SPECS, not a rearchitecture.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterator

GH_BASE = "https://health.googleapis.com/v4"
GH_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _token() -> str:
    token = os.environ.get("GOOGLE_HEALTH_ACCESS_TOKEN")
    if not token:
        raise ValueError(
            "Google Health import requires GOOGLE_HEALTH_ACCESS_TOKEN in env. "
            "Create a Google Cloud project, enable the Google Health API, make "
            "an OAuth Web-Server client, run the OAuth flow once, and export "
            "GOOGLE_HEALTH_ACCESS_TOKEN (+ GOOGLE_HEALTH_REFRESH_TOKEN, "
            "GOOGLE_HEALTH_CLIENT_ID, GOOGLE_HEALTH_CLIENT_SECRET for refresh). "
            "See health_vendor_setup_guide('google_health')."
        )
    return token


def _refresh_creds() -> tuple[str | None, str | None, str | None]:
    return (
        os.environ.get("GOOGLE_HEALTH_REFRESH_TOKEN"),
        os.environ.get("GOOGLE_HEALTH_CLIENT_ID"),
        os.environ.get("GOOGLE_HEALTH_CLIENT_SECRET"),
    )


class RefreshExpiredError(ValueError):
    """Raised when the refresh token itself is rejected — almost always because
    the OAuth app is still in Testing status (7-day refresh-token lifetime).
    """


def _refresh_access_token() -> str | None:
    """Exchange the refresh token for a new access token. Returns the new access
    token, or None if refresh creds are not all set. Raises RefreshExpiredError
    if Google rejects the refresh token (expired / revoked)."""
    rt, cid, csec = _refresh_creds()
    if not (rt and cid and csec):
        return None
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": cid,
            "client_secret": csec,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        GH_TOKEN_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        # invalid_grant on the token endpoint == refresh token no longer valid.
        if e.code in (400, 401) and "invalid_grant" in detail:
            raise RefreshExpiredError(
                "Google refused the refresh token (invalid_grant). If your OAuth "
                "app is in Testing status, refresh tokens expire after 7 days — "
                "publish it to Production for long-lived tokens. Otherwise re-run "
                "the OAuth flow to mint a fresh refresh token."
            )
        raise ValueError(f"Google token refresh HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise ValueError(f"Google token refresh network error: {e}")
    new_at = data.get("access_token")
    if new_at:
        os.environ["GOOGLE_HEALTH_ACCESS_TOKEN"] = new_at
        return new_at
    return None


def _request(path: str, method: str = "GET", body: dict[str, Any] | None = None,
             retry_on_401: bool = True) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(GH_BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 401 and retry_on_401:
            new_at = _refresh_access_token()  # may raise RefreshExpiredError
            if new_at:
                return _request(path, method, body, retry_on_401=False)
        msg = e.read().decode("utf-8", errors="replace")[:300]
        raise ValueError(f"Google Health API HTTP {e.code} on {path}: {msg}")
    except urllib.error.URLError as e:
        raise ValueError(f"Google Health API network error on {path}: {e}")


def _get(path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    return _request(path + qs, method="GET")


def _iter_data_points(data_type: str, start: date, end: date) -> Iterator[dict[str, Any]]:
    """Page through /dataTypes/{data_type}/dataPoints for [start, end], yielding
    each raw dataPoint dict. Uses civil-time filtering on the date range."""
    # civil_start_time / civil_end_time are the documented civil-time filters.
    params = {
        "page_size": "1000",
        "civil_start_time": start.isoformat() + "T00:00:00",  # VERIFY filter param name
        "civil_end_time": (end + timedelta(days=1)).isoformat() + "T00:00:00",
    }
    page_token: str | None = None
    while True:
        p = dict(params)
        if page_token:
            p["page_token"] = page_token
        resp = _get(f"/users/me/dataTypes/{data_type}/dataPoints", p)
        for dp in resp.get("dataPoints", []):
            yield dp
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


# --------------------------------------------------------------------------- #
# Metric mapping — Google dataType -> HK identifier
# --------------------------------------------------------------------------- #
def _interval_start(dp: dict[str, Any], type_key: str) -> datetime | None:
    """Pull the start timestamp of a dataPoint. Times live under the typed
    object's `interval` (or `instant`); fall back to top-level."""
    obj = dp.get(type_key, {}) if isinstance(dp.get(type_key), dict) else {}
    interval = obj.get("interval") or dp.get("interval") or {}
    ts = interval.get("startTime") or obj.get("instant") or dp.get("startTime")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_value(dp: dict[str, Any], type_key: str, field: str) -> float | None:
    """Read dp[type_key][field] as a float, tolerant of nesting/absence."""
    obj = dp.get(type_key)
    if not isinstance(obj, dict):
        return None
    v = obj.get(field)
    if isinstance(v, dict):  # some numeric fields wrap as {"value": n} — tolerate
        v = v.get("value")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# aggregation strategies for bucketing raw points into a daily value
def _sum(vals: list[float]) -> float: return sum(vals)
def _avg(vals: list[float]) -> float: return sum(vals) / len(vals)
def _last(vals: list[float]) -> float: return vals[-1]
def _min(vals: list[float]) -> float: return min(vals)


class _Spec:
    __slots__ = ("data_type", "type_key", "field", "hk", "unit", "agg", "scale")

    def __init__(self, data_type: str, type_key: str, field: str, hk: str,
                 unit: str, agg: Callable[[list[float]], float], scale: float = 1.0):
        self.data_type = data_type
        self.type_key = type_key   # nested object key inside each dataPoint
        self.field = field         # numeric field inside that object   # VERIFY
        self.hk = hk
        self.unit = unit
        self.agg = agg
        self.scale = scale         # multiply raw value (e.g. mm -> km)


# Every `field`/`type_key` marked below is doc-informed; pinned by fixtures.
_METRIC_SPECS: list[_Spec] = [
    _Spec("steps", "steps", "count", "HKQuantityTypeIdentifierStepCount", "count", _sum),  # VERIFY field
    _Spec("distance", "distance", "distanceMillimeters", "HKQuantityTypeIdentifierDistanceWalkingRunning", "km", _sum, scale=1e-6),  # VERIFY
    _Spec("active-energy-burned", "activeEnergyBurned", "energyKcal", "HKQuantityTypeIdentifierActiveEnergyBurned", "kcal", _sum),  # VERIFY
    _Spec("active-minutes", "activeMinutes", "minutes", "HKQuantityTypeIdentifierAppleExerciseTime", "min", _sum),  # VERIFY
    _Spec("floors", "floors", "count", "HKQuantityTypeIdentifierFlightsClimbed", "count", _sum),  # VERIFY
    _Spec("heart-rate", "heartRate", "bpm", "HKQuantityTypeIdentifierHeartRate", "count/min", _avg),  # VERIFY
    _Spec("daily-resting-heart-rate", "dailyRestingHeartRate", "bpm", "HKQuantityTypeIdentifierRestingHeartRate", "count/min", _min),  # VERIFY
    _Spec("daily-heart-rate-variability", "dailyHeartRateVariability", "hrvMs", "HKQuantityTypeIdentifierHeartRateVariabilitySDNN", "ms", _avg),  # VERIFY
    _Spec("daily-oxygen-saturation", "dailyOxygenSaturation", "percentage", "HKQuantityTypeIdentifierOxygenSaturation", "%", _avg),  # VERIFY
    _Spec("daily-respiratory-rate", "dailyRespiratoryRate", "breathsPerMinute", "HKQuantityTypeIdentifierRespiratoryRate", "count/min", _avg),  # VERIFY
    _Spec("daily-vo2-max", "dailyVo2Max", "vo2MaxMlPerKgMin", "HKQuantityTypeIdentifierVO2Max", "ml/kg*min", _last),  # VERIFY
    _Spec("weight", "weight", "weightKilograms", "HKQuantityTypeIdentifierBodyMass", "kg", _last),  # VERIFY
    _Spec("body-fat", "bodyFat", "percentage", "HKQuantityTypeIdentifierBodyFatPercentage", "%", _last),  # VERIFY
    _Spec("height", "height", "heightMeters", "HKQuantityTypeIdentifierHeight", "m", _last),  # VERIFY
    _Spec("blood-glucose", "bloodGlucose", "mgPerDl", "HKQuantityTypeIdentifierBloodGlucose", "mg/dL", _avg),  # VERIFY
]

# total-calories handled specially -> BasalEnergyBurned = total - active
_TOTAL_CAL_SPEC = _Spec("total-calories", "totalCalories", "energyKcal", "", "kcal", _sum)  # VERIFY

# Google sleep-stage token -> our schema stage vocabulary (hk_types.py).
_SLEEP_STAGE_MAP = {
    "rem": "rem", "sleep_rem": "rem", "REM": "rem",
    "deep": "deep", "sleep_deep": "deep", "DEEP": "deep",
    "light": "core", "sleep_light": "core", "LIGHT": "core",
    "awake": "awake", "sleep_awake": "awake", "AWAKE": "awake",
    "out_of_bed": "awake", "OUT_OF_BED": "awake",
}


def _civil_day(dt: datetime) -> date:
    """Bucket a timestamp to its civil date (naive/UTC-normalized)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.date()


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #
def healthcheck() -> dict[str, Any]:
    """Verify credentials end-to-end via /users/me/identity.

    Distinguishes an expired refresh token (Testing-mode 7-day expiry) from
    other auth failures so the setup fix is obvious.
    """
    try:
        info = _get("/users/me/identity")
        return {"ok": True, "user_id": info.get("id") or info.get("userId") or info.get("name")}
    except RefreshExpiredError as e:
        return {"ok": False, "error": str(e), "refresh_expired": True}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


def fetch_range(start: date, end: date) -> Iterator[dict[str, Any]]:
    """Pull all supported metrics + sleep in [start, end] and yield records
    shaped like parse_xml.iter_records output (_kind in {record, sleep}).

    Each metric is fetched in its own try/except: a missing scope (403) or an
    empty collection skips that metric and continues — one gap never kills the
    whole import.
    """
    # --- numeric metrics: fetch, bucket by civil day, aggregate ------------- #
    daily_active: dict[date, float] = {}
    daily_total: dict[date, float] = {}
    for spec in _METRIC_SPECS:
        buckets: dict[date, list[float]] = {}
        try:
            for dp in _iter_data_points(spec.data_type, start, end):
                ts = _interval_start(dp, spec.type_key)
                val = _extract_value(dp, spec.type_key, spec.field)
                if ts is None or val is None:
                    continue
                buckets.setdefault(_civil_day(ts), []).append(val * spec.scale)
        except ValueError:
            continue  # skip this metric, keep going
        for day, vals in buckets.items():
            if not vals:
                continue
            value = spec.agg(vals)
            if spec.hk == "HKQuantityTypeIdentifierActiveEnergyBurned":
                daily_active[day] = value
            ts = datetime.combine(day, datetime.min.time())
            yield {
                "_kind": "record",
                "type": spec.hk,
                "source_name": "GoogleHealth",
                "unit": spec.unit,
                "start_date": ts,
                "end_date": ts + timedelta(days=1),
                "value": value,
                "value_str": None,
            }

    # --- total-calories -> BasalEnergyBurned (total - active) --------------- #
    try:
        tbuckets: dict[date, list[float]] = {}
        for dp in _iter_data_points(_TOTAL_CAL_SPEC.data_type, start, end):
            ts = _interval_start(dp, _TOTAL_CAL_SPEC.type_key)
            val = _extract_value(dp, _TOTAL_CAL_SPEC.type_key, _TOTAL_CAL_SPEC.field)
            if ts is None or val is None:
                continue
            tbuckets.setdefault(_civil_day(ts), []).append(val)
        for day, vals in tbuckets.items():
            daily_total[day] = sum(vals)
    except ValueError:
        pass
    for day, total in daily_total.items():
        active = daily_active.get(day)
        if active is None:
            continue
        basal = total - active
        if basal <= 0:
            continue
        ts = datetime.combine(day, datetime.min.time())
        yield {
            "_kind": "record",
            "type": "HKQuantityTypeIdentifierBasalEnergyBurned",
            "source_name": "GoogleHealth",
            "unit": "kcal",
            "start_date": ts,
            "end_date": ts + timedelta(days=1),
            "value": basal,
            "value_str": None,
        }

    # --- sleep sessions -> stage rows --------------------------------------- #
    try:
        yield from _fetch_sleep(start, end)
    except ValueError:
        pass


def _fetch_sleep(start: date, end: date) -> Iterator[dict[str, Any]]:
    """Emit sleep stage rows. A sleep dataPoint carries a `sleep` object with an
    `interval` and a list of stages, each with its own interval + stage token."""
    for dp in _iter_data_points("sleep", start, end):
        sleep = dp.get("sleep")
        if not isinstance(sleep, dict):
            continue
        stages = sleep.get("stages") or sleep.get("sleepStages") or []  # VERIFY
        for st in stages:
            interval = st.get("interval", {}) if isinstance(st, dict) else {}
            raw_start = interval.get("startTime")
            raw_end = interval.get("endTime")
            token = (st.get("stage") or st.get("type") or "") if isinstance(st, dict) else ""
            stage = _SLEEP_STAGE_MAP.get(token, _SLEEP_STAGE_MAP.get(str(token).lower(), "asleep_unspecified"))
            if not (raw_start and raw_end):
                continue
            try:
                s_dt = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
                e_dt = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
            except ValueError:
                continue
            yield {
                "_kind": "sleep",
                "start_date": s_dt,
                "end_date": e_dt,
                "stage": stage,
                "source_name": "GoogleHealth",
            }


def folder_sha(start: date, end: date) -> str:
    """Idempotency hash: range + token suffix (never the raw token)."""
    import hashlib
    token = _token()  # raises with the setup message if unset
    suffix = token[-4:] if len(token) > 4 else "anon"
    return hashlib.sha256(
        f"google_health|{start.isoformat()}|{end.isoformat()}|{suffix}".encode("utf-8")
    ).hexdigest()
