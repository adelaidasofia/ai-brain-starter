# Google Health API Connector — Design Spec

**Date:** 2026-06-30
**Status:** Approved (pending spec review)
**Author:** Nelly Ortiz (with Claude)
**Component:** `services/health-mcp`
**Supersedes:** the Fitbit Web API path (`fitbit_client.py`), which Google is
sunsetting from **September 2026**.

---

## 1. Problem

The health-mcp server currently pulls wearable data from three sources:
Apple Health (offline export), Oura (v2 cloud API), and **Fitbit (Fitbit Web
API)**. Google is consolidating its health platform: the Fitbit Web API and the
Google Fit REST API are being retired, and the **Google Health API**
(`developers.google.com/health`) is the successor — the same OAuth2 cloud REST
surface, now unifying Fitbit + Pixel Watch + third-party device data.

The Fitbit path expires **September 2026**. We replace it with a Google Health
API connector so `/coach`'s daily auto-sync keeps flowing after the cutoff.

### Not viable (explicitly rejected)
**Health Connect** (Android on-device datastore) has **no cloud API**. A
server-side MCP cannot OAuth into it; it would require an Android companion app.
The buildable successor is the **Google Health API**, not Health Connect.

---

## 2. Optimization Pass (per MCP Build Runbook — mandatory, documented)

| Audit | Decision |
|---|---|
| Stack redundancy | No existing tool reads the Google Health API. Build justified. Reuse the **entire** shared ingest path (`_bulk_insert` → `db.file_already_imported`/`db.log_import`, `folder_sha` dedup, DuckDB HK-type schema). |
| Net-new code | `google_health_client.py` + one `_GUIDES` entry + one import tool + one healthcheck branch. **No new server, DB, registration, or framework.** |
| Frontend | None (CLI/internal only). |
| Database | Reuse existing DuckDB + schema. |
| LLM usage | None — pure REST fetch + deterministic normalization. |
| Shared code | OAuth2 refresh (~25 lines) is near-identical to `fitbit_client._refresh_access_token`. **Deferred extraction:** keep parallel now (don't disturb the working path); extract a shared `oauth2.py` only when a third OAuth vendor appears. |
| Compose vs duplicate | Add tools to the existing `health` MCP server. |
| Deps | **stdlib only** (`urllib`, `json`, `hashlib`, `datetime`) — critical because the server runs as `.venv/bin/python main.py` (not `fastmcp run`), so new third-party imports would need explicit venv install. We avoid that entirely. |

---

## 3. Architecture

Mirrors the existing per-vendor connector pattern exactly.

### 3.1 New module: `google_health_client.py`
Public surface (matches `fitbit_client.py` / `oura_client.py`):

- `healthcheck() -> dict` — `GET /v4/users/me/identity`; returns
  `{ok, user_id?, error?}`. **Also detects an expired/near-expired refresh
  token** and reports it explicitly (see §5).
- `fetch_range(start: date, end: date) -> Iterator[dict]` — yields records
  shaped like `parse_xml.iter_records` output (`_kind: "record"` and
  `_kind: "sleep"` items), normalized to the HK schema.
- `folder_sha(start, end) -> str` — `sha256("google_health|{start}|{end}|{token_suffix}")`.
- Private: `_token()`, `_get(path)`, `_post(path, body)`,
  `_refresh_access_token()` — same 401 → refresh → retry-once logic as Fitbit.

### 3.2 New tool: `health_import_google_health(start, end, force=False)`
A copy of the `health_import_oura` / `health_import_fitbit` tool body, routed
through `google_health_client`, `kind="google_health"`,
`db.log_import(con, sha, "google_health", ...)`. Same batching (`BATCH=1000`),
same idempotency (`db.file_already_imported`), same return shape.

### 3.3 Replace the Fitbit path (deprecated alias)
`health_import_fitbit` is **retained as a thin deprecated alias** that delegates
to `health_import_google_health` and adds
`"deprecated": "Fitbit Web API retired Sept 2026 — now served by the Google
Health API. Use health_import_google_health."` to the result.

Rationale: any existing scheduled auto-sync calling `health_import_fitbit` keeps
working instead of silently breaking at the cutoff. `fitbit_client.py` stays on
disk but unwired from the primary path. Full deletion is a later cleanup.

`health_vendor_healthcheck` gains a `google_health` branch; the `fitbit` branch
is kept (still points at `fitbit_client.healthcheck` for anyone mid-migration).

### 3.4 Setup guide: `_GUIDES["google_health"]` in `vendor_setup.py`
New vendor entry + add `"google_health"` to `SUPPORTED_VENDORS`. Documents the
Google Cloud project + OAuth client + test-user setup, the env vars, **and the
7-day-token Production-publish requirement** (§5).

---

## 4. Auth & fetch

- **Base URL:** `https://health.googleapis.com/v4/`
- **Token endpoint:** `https://oauth2.googleapis.com/token`
- **OAuth client type:** Web Server; Authorized redirect URI `https://www.google.com`
- **Env vars** (mirror Fitbit):
  - `GOOGLE_HEALTH_ACCESS_TOKEN` (short-lived, auto-refreshed)
  - `GOOGLE_HEALTH_REFRESH_TOKEN`
  - `GOOGLE_HEALTH_CLIENT_ID`
  - `GOOGLE_HEALTH_CLIENT_SECRET`
- **Fetch strategy:**
  - Daily numeric metrics: `POST /users/me/dataTypes/{dataType}/dataPoints:dailyRollUp`
    with a `{startTime, endTime}` range → one daily value per metric per day.
  - Sleep: `GET /users/me/dataTypes/sleep/dataPoints` → stage sessions, emitted
    as `_kind: "sleep"` records with stage sessions.
  - Pagination: follow `nextPageToken` until exhausted.
- **Degrade gracefully:** each `dataType` fetched in its own `try/except`; a
  403 (scope not granted) or empty result for one metric skips that metric and
  continues — one missing scope never kills the whole import (same posture as
  `fitbit_client.fetch_range`).

---

## 5. ⚠️ Load-bearing gotcha: Testing-mode refresh tokens expire in 7 days

Google Health API refresh tokens **expire after 7 days while the OAuth app is in
"Testing" status.** Daily `/coach` auto-sync would therefore break every week
until the app is **published to Production** (after which refresh tokens are
long-lived, revoked only on inactivity ~6 months or manual revoke).

Handling:
- `_GUIDES["google_health"]` lists **"Publish the OAuth app to Production"** as a
  required step for anyone who wants ongoing auto-sync, not an optional one.
- `healthcheck()` distinguishes an expired-refresh failure from other auth
  errors and returns a clear
  `{"ok": false, "error": "refresh token expired — publish your OAuth app to
  Production (Testing tokens last 7 days)"}` message.

---

## 6. Metric mapping (Google `dataType` → HK identifier)

Verified against `hk_types.py` — every target below is a real supported id.

### Phase 1 — all daily numeric metrics + sleep (this connector)

| Google `dataType` | HK identifier |
|---|---|
| `steps` | `HKQuantityTypeIdentifierStepCount` |
| `distance` | `HKQuantityTypeIdentifierDistanceWalkingRunning` |
| `active-energy-burned` | `HKQuantityTypeIdentifierActiveEnergyBurned` |
| `total-calories` | `HKQuantityTypeIdentifierBasalEnergyBurned` (= total − active when both present; else skip) |
| `active-minutes` | `HKQuantityTypeIdentifierAppleExerciseTime` |
| `floors` | `HKQuantityTypeIdentifierFlightsClimbed` |
| `heart-rate` | `HKQuantityTypeIdentifierHeartRate` |
| `daily-resting-heart-rate` | `HKQuantityTypeIdentifierRestingHeartRate` |
| `heart-rate-variability` / `daily-heart-rate-variability` | `HKQuantityTypeIdentifierHeartRateVariabilitySDNN` |
| `oxygen-saturation` / `daily-oxygen-saturation` | `HKQuantityTypeIdentifierOxygenSaturation` |
| `daily-respiratory-rate` | `HKQuantityTypeIdentifierRespiratoryRate` |
| `vo2-max` / `daily-vo2-max` | `HKQuantityTypeIdentifierVO2Max` |
| `weight` | `HKQuantityTypeIdentifierBodyMass` |
| `body-fat` | `HKQuantityTypeIdentifierBodyFatPercentage` |
| `height` | `HKQuantityTypeIdentifierHeight` |
| `blood-glucose` | `HKQuantityTypeIdentifierBloodGlucose` |
| `daily-sleep-temperature-derivations` | `HKQuantityTypeIdentifierAppleSleepingWristTemperature` |
| `sleep` | sleep table; stages → `rem` / `deep` / `core` / `awake` / `asleep_unspecified` |

Google sleep-stage → schema-stage mapping (matches `HKCategoryValueSleepAnalysis`
vocabulary in `hk_types.py`): `rem→rem`, `deep→deep`, `light→core`,
`awake/out-of-bed→awake`, unknown→`asleep_unspecified`.

### Phase 2 — separate follow-up work (NOT in this connector)

These need different endpoints/shapes and are explicitly deferred:
- `exercise` — structured workout sessions (workouts table)
- `electrocardiogram` — ECG waveforms + `ecg` scope
- `irregular-rhythm-notification` — IRN scope, category events
- `nutrition-log` / `food` / `hydration-log` — structured nutrition

Phase 2 is its own spec → plan → build cycle.

---

## 7. Error handling

- Network / HTTP errors raise `ValueError` from `_get`/`_post`; the import tool
  catches and returns `{"error": ..., "skipped": True}` (same as Oura/Fitbit).
- Per-metric failures inside `fetch_range` are swallowed and skipped.
- 401 → single refresh + retry; if refresh fails, surface the expired-token
  message from §5.

---

## 8. Testing

- Recorded-fixture unit test (`tests/test_google_health.py`) exercising
  `fetch_range` normalization for: a daily numeric metric, `total-calories`
  basal derivation, and a sleep session with stages. No live network.
- A `healthcheck` test for the expired-refresh-token branch.
- Self-test must not crash when the four `GOOGLE_HEALTH_*` env vars are unset
  (returns the graceful "missing token" error).

---

## 9. Files touched

| File | Change |
|---|---|
| `google_health_client.py` | **new** — connector module |
| `main.py` | new `health_import_google_health` tool; `health_import_fitbit` → deprecated alias; `google_health` branch in `health_vendor_healthcheck`; import the new client |
| `vendor_setup.py` | new `_GUIDES["google_health"]`; add to `SUPPORTED_VENDORS` |
| `.env.example` | document the four `GOOGLE_HEALTH_*` vars |
| `tests/test_google_health.py` | **new** |
| `docs/CHANGELOG.md` | entry noting the migration + optimization decisions |

No change to: `db.py`, `hk_types.py`, `scores.py`, `coach.py`, the DuckDB schema,
or the MCP registration in `~/.claude.json`.

---

## 10. Out of scope

- Health Connect (on-device; no cloud API).
- Phase-2 data types (§6).
- Deleting `fitbit_client.py` (kept for the deprecated alias + mid-migration
  healthcheck).
- Building any scheduler — daily auto-sync reuses the existing scheduled-task
  mechanism calling `health_import_google_health`.
