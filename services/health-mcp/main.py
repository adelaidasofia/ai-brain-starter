"""health-mcp main FastMCP server (v0.2).

32 tools across 7 categories:
  Ingestion (5): XML, CSV, lab CSV, status, schema
  Query (3): schema, query (read-only SQL), metric_series
  Analytics (5): workout_list, sleep_summary, recovery_score, sleep_score,
                 strain_score
  Surface (4): longevity_panel, sleep_regularity, somatic_state, nutrition_summary
  Cycle (3): cycle_context, phase_tagged_metric, phase_means
  Symptoms + ECG + State of mind (4): symptoms_timeline, ecg_list,
                                      state_of_mind_timeline, audio_exposure
  Vault-aware (7): journal_context, journal_body_question, floor_correlation,
                   coaching_context, panel_context, weekly_rollup, long_window
  Live (1): live_query (Health Auto Export TCP)
  Recommendations (1): recommended_labs

Stdio transport. Designed to be registered in .mcp.json as `health` and
launched per-session by Claude Code.
"""
from __future__ import annotations

import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

import coach as coach_mod
import cycle as cycle_mod
import db
import fitbit_client
import google_health_client
import labs as labs_mod
import live_tcp
import oura_client
import analytics
import parse_csv
import parse_xml
import scores
import shortcut_normalize
import vault_aware
import vendor_setup
from hk_types import HK_QUANTITY_TYPES, NUTRITION_TYPES, LONGEVITY_TYPES, CYCLE_TYPES, SYMPTOM_TYPES

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("health-mcp")


@asynccontextmanager
async def lifespan(_app: FastMCP):
    with db.connect() as con:
        s = db.stats(con)
    log.info(
        "health-mcp v0.2 ready: records=%d workouts=%d sleep=%d cycle=%d symptoms=%d ecg=%d state_of_mind=%d labs=%d",
        s["records_count"], s["workouts_count"], s["sleep_count"],
        s["cycle_count"], s["symptoms_count"], s["ecg_count"],
        s["state_of_mind_count"], s["labs_count"],
    )
    yield


mcp = FastMCP("health-mcp", lifespan=lifespan)


def _parse_date(s: str) -> date:
    return datetime.fromisoformat(s.replace("Z", "")).date() if "T" in s else datetime.strptime(s[:10], "%Y-%m-%d").date()


def _bulk_insert(con, batch: list[dict[str, Any]]) -> dict[str, int]:
    """Insert all _kind dicts into their target tables. Returns counts per kind."""
    rec_rows = []
    wk_rows = []
    sl_rows = []
    cy_rows = []
    sy_rows = []
    ecg_rows = []
    som_rows = []
    for item in batch:
        k = item["_kind"]
        if k == "record":
            rec_rows.append((item["type"], item["source_name"], item["unit"], item["start_date"], item["end_date"], item["value"], item["value_str"]))
        elif k == "workout":
            wk_rows.append((item["activity_type"], item["duration_min"], item["distance_km"], item["energy_kcal"], item["start_date"], item["end_date"], item["source_name"]))
        elif k == "sleep":
            sl_rows.append((item["start_date"], item["end_date"], item["stage"], item["source_name"]))
        elif k == "cycle":
            cy_rows.append((item["type"], item["start_date"], item["end_date"], item["value"], item["source_name"]))
        elif k == "symptom":
            sy_rows.append((item["type"], item["start_date"], item["end_date"], item["severity"], item["source_name"]))
        elif k == "ecg":
            ecg_rows.append((item["start_date"], item["classification"], item["average_heart_rate"], item["sampling_frequency"], item["source_name"]))
        elif k == "state_of_mind":
            som_rows.append((item["start_date"], item["end_date"], item["kind"], item["valence"], item["labels"], item["associations"], item["source_name"]))
    if rec_rows:
        con.executemany("INSERT INTO records (type, source_name, unit, start_date, end_date, value, value_str) VALUES (?, ?, ?, ?, ?, ?, ?)", rec_rows)
    if wk_rows:
        con.executemany("INSERT INTO workouts (activity_type, duration_min, distance_km, energy_kcal, start_date, end_date, source_name) VALUES (?, ?, ?, ?, ?, ?, ?)", wk_rows)
    if sl_rows:
        con.executemany("INSERT INTO sleep (start_date, end_date, stage, source_name) VALUES (?, ?, ?, ?)", sl_rows)
    if cy_rows:
        con.executemany("INSERT INTO cycle (type, start_date, end_date, value, source_name) VALUES (?, ?, ?, ?, ?)", cy_rows)
    if sy_rows:
        con.executemany("INSERT INTO symptoms (type, start_date, end_date, severity, source_name) VALUES (?, ?, ?, ?, ?)", sy_rows)
    if ecg_rows:
        con.executemany("INSERT INTO ecg (start_date, classification, average_heart_rate, sampling_frequency, source_name) VALUES (?, ?, ?, ?, ?)", ecg_rows)
    if som_rows:
        con.executemany("INSERT INTO state_of_mind (start_date, end_date, kind, valence, labels, associations, source_name) VALUES (?, ?, ?, ?, ?, ?, ?)", som_rows)
    return {
        "records": len(rec_rows),
        "workouts": len(wk_rows),
        "sleep": len(sl_rows),
        "cycle": len(cy_rows),
        "symptoms": len(sy_rows),
        "ecg": len(ecg_rows),
        "state_of_mind": len(som_rows),
    }


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

@mcp.tool()
def health_import_xml(zip_or_xml_path: str, force: bool = False) -> dict[str, Any]:
    """Import an Apple Health XML export.zip (or unzipped export.xml) into DuckDB.

    Routes records into 7 tables based on type registry: records, workouts,
    sleep, cycle (menstrual + reproductive), symptoms, ecg, state_of_mind.
    Idempotent: re-importing the same file SHA returns skipped=True.
    """
    t0 = datetime.now()
    xml_path, file_sha, _scratch = parse_xml.resolve_xml_path(zip_or_xml_path)
    with db.connect() as con:
        if not force and db.file_already_imported(con, file_sha):
            return {"file_sha": file_sha, "skipped": True, "note": "Already imported. Pass force=True to re-import."}
        batch: list[dict[str, Any]] = []
        totals: dict[str, int] = {k: 0 for k in ("records", "workouts", "sleep", "cycle", "symptoms", "ecg", "state_of_mind")}
        BATCH = 5000
        for item in parse_xml.iter_records(xml_path):
            batch.append(item)
            if len(batch) >= BATCH:
                for k, v in _bulk_insert(con, batch).items():
                    totals[k] += v
                batch.clear()
        if batch:
            for k, v in _bulk_insert(con, batch).items():
                totals[k] += v
        total = sum(totals.values())
        db.log_import(con, file_sha, "xml", str(xml_path), total)
    return {
        "file_sha": file_sha, "kind": "xml", "rows_inserted": total,
        **{f"{k}_count": v for k, v in totals.items()},
        "skipped": False,
        "elapsed_s": round((datetime.now() - t0).total_seconds(), 2),
    }


@mcp.tool()
def health_import_csv(folder_path: str, force: bool = False) -> dict[str, Any]:
    """Import a folder of Simple Health Export CSVs."""
    t0 = datetime.now()
    folder, file_sha = parse_csv.folder_sha(folder_path)
    with db.connect() as con:
        if not force and db.file_already_imported(con, file_sha):
            return {"file_sha": file_sha, "skipped": True, "note": "Already imported."}
        batch: list[dict[str, Any]] = []
        totals: dict[str, int] = {k: 0 for k in ("records", "workouts", "sleep", "cycle", "symptoms", "ecg", "state_of_mind")}
        BATCH = 5000
        for item in parse_csv.iter_records(folder):
            batch.append(item)
            if len(batch) >= BATCH:
                for k, v in _bulk_insert(con, batch).items():
                    totals[k] += v
                batch.clear()
        if batch:
            for k, v in _bulk_insert(con, batch).items():
                totals[k] += v
        total = sum(totals.values())
        db.log_import(con, file_sha, "csv", str(folder), total)
    return {
        "file_sha": file_sha, "kind": "csv", "rows_inserted": total,
        **{f"{k}_count": v for k, v in totals.items()},
        "skipped": False,
        "elapsed_s": round((datetime.now() - t0).total_seconds(), 2),
    }


@mcp.tool()
def health_import_shortcut(payload_path: str, force: bool = False) -> dict[str, Any]:
    """Import an Apple Shortcuts JSON payload (one day's HealthKit data).

    The companion iOS Shortcut writes a single JSON file per day to iCloud
    Drive at `~/Library/Mobile Documents/com~apple~CloudDocs/health-mcp/<YYYY-MM-DD>.json`.
    This tool reads that file, normalizes via shortcut_normalize.iter_payload,
    and writes to the same DuckDB tables as the XML / CSV / Oura / Fitbit
    importers. Idempotent via file SHA (re-importing the same payload
    returns skipped=True).

    Apple Shortcuts cannot read every HealthKit type. ECG records and a few
    niche types are not exposed; users who want full coverage should run
    `health_import_xml` periodically alongside this auto-sync.
    """
    t0 = datetime.now()
    file_sha = shortcut_normalize.payload_sha(payload_path)
    with db.connect() as con:
        if not force and db.file_already_imported(con, file_sha):
            return {"file_sha": file_sha, "skipped": True, "note": "Already imported. Pass force=True to re-import."}
        try:
            payload = shortcut_normalize.load_payload_file(payload_path)
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
            return {"error": f"{type(e).__name__}: {e}", "skipped": True}
        batch: list[dict[str, Any]] = []
        totals: dict[str, int] = {k: 0 for k in ("records", "workouts", "sleep", "cycle", "symptoms", "ecg", "state_of_mind")}
        BATCH = 5000
        for item in shortcut_normalize.iter_payload(payload):
            batch.append(item)
            if len(batch) >= BATCH:
                for k, v in _bulk_insert(con, batch).items():
                    totals[k] += v
                batch.clear()
        if batch:
            for k, v in _bulk_insert(con, batch).items():
                totals[k] += v
        total = sum(totals.values())
        db.log_import(con, file_sha, "shortcut", str(payload_path), total)
    return {
        "file_sha": file_sha, "kind": "shortcut", "rows_inserted": total,
        **{f"{k}_count": v for k, v in totals.items()},
        "skipped": False,
        "schema_version": payload.get("schema_version"),
        "payload_date": payload.get("date"),
        "elapsed_s": round((datetime.now() - t0).total_seconds(), 2),
    }


@mcp.tool()
def health_sweep_shortcut_inbox(inbox_path: str | None = None, archive: bool = True) -> dict[str, Any]:
    """Process every JSON payload in the iCloud Drive inbox folder.

    Default inbox: `~/Library/Mobile Documents/com~apple~CloudDocs/health-mcp/`.
    Each `<YYYY-MM-DD>.json` file is imported via `health_import_shortcut`
    and (if `archive=True`) moved to `<inbox>/processed/<YYYY-MM-DD>.json`.
    Idempotent: previously-imported files are skipped without error.

    Returns a per-file summary list. Designed for the daily journal-Stop
    chain hook: one call drains everything new since yesterday.
    """
    import shutil
    inbox = Path(inbox_path) if inbox_path else shortcut_normalize.default_inbox()
    if not inbox.is_dir():
        return {"inbox": str(inbox), "files_processed": 0, "note": "Inbox folder does not exist yet."}
    processed_dir = inbox / "processed"
    if archive:
        processed_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for f in sorted(inbox.glob("*.json")):
        if f.parent != inbox:
            continue
        res = health_import_shortcut(str(f))
        results.append({"file": f.name, **{k: v for k, v in res.items() if k in ("rows_inserted", "skipped", "error", "payload_date")}})
        if archive and not res.get("error"):
            shutil.move(str(f), str(processed_dir / f.name))
    return {
        "inbox": str(inbox),
        "files_processed": len(results),
        "results": results,
    }


@mcp.tool()
def health_import_labs(csv_path: str, lab_format: str = "auto", force: bool = False) -> dict[str, Any]:
    """Import lab results from LabCorp / Quest / Function Health / generic CSV.

    Args:
        csv_path: path to the CSV exported from your patient portal.
        lab_format: "auto" (detect by header shape), "labcorp", "quest",
                    "function", or "generic".
        force: re-import even if file SHA matches.

    Why: Apple Health doesn't capture clinical lab panels. Manually importing
    labs (ApoB, fasting insulin, hs-CRP, full thyroid, sex hormones) lets the
    journal / coaching / panel / insights skills pull lab context alongside
    biometrics. Without labs, the recovery-score formula misses chronic
    inflammation and metabolic dysfunction.
    """
    t0 = datetime.now()
    p, sha, rows = labs_mod.parse_labs_csv(csv_path, lab_format=lab_format)
    if not rows:
        return {"file_sha": sha, "rows": 0, "note": "No parseable rows in CSV. Check format with lab_format='generic' to inspect headers."}
    with db.connect() as con:
        if not force and db.file_already_imported(con, sha):
            return {"file_sha": sha, "skipped": True, "note": "Already imported."}
        con.executemany(
            "INSERT INTO labs (test_date, panel, marker, value, unit, range_low, range_high, status, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(r["test_date"], r["panel"], r["marker"], r["value"], r["unit"], r["range_low"], r["range_high"], r["status"], r["source"]) for r in rows],
        )
        db.log_import(con, sha, "labs", str(p), len(rows))
    return {
        "file_sha": sha, "kind": "labs", "rows_inserted": len(rows),
        "skipped": False, "elapsed_s": round((datetime.now() - t0).total_seconds(), 2),
        "format_detected": rows[0]["source"],
    }


@mcp.tool()
def health_status() -> dict[str, Any]:
    """Database stats: row counts per table, last import timestamp, top metric types."""
    with db.connect(read_only=True) as con:
        s = db.stats(con)
        s["top_types"] = db.types_with_counts(con)[:20]
    return s


@mcp.tool()
def health_import_oura(start: str, end: str, force: bool = False) -> dict[str, Any]:
    """Import Oura Ring data via the Oura v2 Cloud API.

    Requires OURA_PERSONAL_ACCESS_TOKEN in env. Generate one at
    https://cloud.ouraring.com/personal-access-tokens (free).

    Pulls daily sleep + sleep sessions (stages) + readiness + activity +
    workouts in [start, end] and normalizes into the same DuckDB schema as
    Apple Health imports. HRV / RHR / steps / active kcal map to the same
    HKQuantityType ids so health_recovery_score and all vault-aware tools
    work without modification.
    """
    t0 = datetime.now()
    sd = _parse_date(start)
    ed = _parse_date(end)
    try:
        sha = oura_client.folder_sha(sd, ed)
    except ValueError as e:
        return {"error": str(e), "skipped": True}
    with db.connect() as con:
        if not force and db.file_already_imported(con, sha):
            return {"file_sha": sha, "skipped": True, "note": "This date range already imported. Pass force=True to re-import."}
        batch: list[dict[str, Any]] = []
        totals: dict[str, int] = {k: 0 for k in ("records", "workouts", "sleep", "cycle", "symptoms", "ecg", "state_of_mind")}
        BATCH = 1000
        try:
            for item in oura_client.fetch_range(sd, ed):
                batch.append(item)
                if len(batch) >= BATCH:
                    for k, v in _bulk_insert(con, batch).items():
                        totals[k] += v
                    batch.clear()
            if batch:
                for k, v in _bulk_insert(con, batch).items():
                    totals[k] += v
            total = sum(totals.values())
            db.log_import(con, sha, "oura", f"oura:{sd.isoformat()}..{ed.isoformat()}", total)
        except ValueError as e:
            return {"error": str(e), "skipped": True}
    return {
        "file_sha": sha, "kind": "oura", "rows_inserted": total,
        **{f"{k}_count": v for k, v in totals.items()},
        "skipped": False,
        "elapsed_s": round((datetime.now() - t0).total_seconds(), 2),
    }


@mcp.tool()
def health_import_google_health(start: str, end: str, force: bool = False) -> dict[str, Any]:
    """Import wearable data via the Google Health API (successor to the Fitbit
    Web API + Google Fit REST API, both retired from Sept 2026).

    Requires a Google Cloud project with the Google Health API enabled, an OAuth
    Web-Server client, and GOOGLE_HEALTH_ACCESS_TOKEN in env (plus
    GOOGLE_HEALTH_REFRESH_TOKEN + GOOGLE_HEALTH_CLIENT_ID +
    GOOGLE_HEALTH_CLIENT_SECRET for auto-refresh). See
    health_vendor_setup_guide('google_health'). NOTE: while the OAuth app is in
    Testing status Google expires refresh tokens after 7 days — publish to
    Production for ongoing daily sync.

    Pulls daily activity + heart rate + HRV + SpO2 + respiratory rate + VO2max +
    body metrics + sleep stages in [start, end] and normalizes into the shared
    DuckDB schema (same HK type ids as Apple Health), so recovery/sleep scores
    and all vault-aware tools work unchanged. Workouts / ECG / nutrition are a
    planned Phase 2.
    """
    t0 = datetime.now()
    sd = _parse_date(start)
    ed = _parse_date(end)
    try:
        sha = google_health_client.folder_sha(sd, ed)
    except ValueError as e:
        return {"error": str(e), "skipped": True}
    with db.connect() as con:
        if not force and db.file_already_imported(con, sha):
            return {"file_sha": sha, "skipped": True, "note": "This date range already imported. Pass force=True to re-import."}
        batch: list[dict[str, Any]] = []
        totals: dict[str, int] = {k: 0 for k in ("records", "workouts", "sleep", "cycle", "symptoms", "ecg", "state_of_mind")}
        BATCH = 1000
        try:
            for item in google_health_client.fetch_range(sd, ed):
                batch.append(item)
                if len(batch) >= BATCH:
                    for k, v in _bulk_insert(con, batch).items():
                        totals[k] += v
                    batch.clear()
            if batch:
                for k, v in _bulk_insert(con, batch).items():
                    totals[k] += v
            total = sum(totals.values())
            db.log_import(con, sha, "google_health", f"google_health:{sd.isoformat()}..{ed.isoformat()}", total)
        except ValueError as e:
            return {"error": str(e), "skipped": True}
    return {
        "file_sha": sha, "kind": "google_health", "rows_inserted": total,
        **{f"{k}_count": v for k, v in totals.items()},
        "skipped": False,
        "elapsed_s": round((datetime.now() - t0).total_seconds(), 2),
    }


@mcp.tool()
def health_import_fitbit(start: str, end: str, force: bool = False) -> dict[str, Any]:
    """DEPRECATED: the Fitbit Web API is retired by Google from Sept 2026.

    This now delegates to health_import_google_health (the successor API) so
    existing scheduled syncs keep working. Migrate your setup to the Google
    Health API — see health_vendor_setup_guide('google_health'). Set
    GOOGLE_HEALTH_* env vars; your old FITBIT_* vars no longer power this tool.
    """
    result = health_import_google_health(start, end, force=force)
    result["deprecated"] = (
        "health_import_fitbit is deprecated (Fitbit Web API retired Sept 2026). "
        "Now served by the Google Health API — use health_import_google_health "
        "and set GOOGLE_HEALTH_* env vars."
    )
    return result


@mcp.tool()
def health_vendor_setup_guide(vendor: str, os_kind: str = "macos") -> dict[str, Any]:
    """Return setup instructions for a wearable vendor on a given OS.

    Args:
        vendor: 'apple_health' | 'oura' | 'fitbit' | 'garmin' | 'whoop'
        os_kind: 'macos' | 'windows' | 'linux'

    Returns a structured dict with: display_name, summary, free/paid paths,
    common_steps, transfer_steps (OS-specific), env_vars (with what each
    one is for), the tool to call after setup, ongoing-cadence guidance,
    and any vendor-specific notes (rate limits, premium gates, etc.).
    """
    return vendor_setup.vendor_setup_guide(vendor, os_kind)


@mcp.tool()
def health_vendor_healthcheck(vendor: str) -> dict[str, Any]:
    """Verify that a vendor's API credentials work end-to-end.

    Args:
        vendor: 'oura' | 'fitbit' (Apple Health is offline; n/a)

    Returns {ok: bool, user_id?: str, error?: str}. Use after setting up
    env vars to confirm before running a full import.
    """
    v = vendor.lower().strip().replace("-", "_").replace(" ", "_")
    if v == "oura":
        return oura_client.healthcheck()
    if v in {"google_health", "google", "googlehealth"}:
        return google_health_client.healthcheck()
    if v == "fitbit":
        return {"ok": False, "error": "Fitbit Web API is retired by Google (Sept 2026). Migrate to the Google Health API: health_vendor_healthcheck('google_health').", "deprecated": True}
    if v in {"apple", "apple_health"}:
        return {"ok": True, "note": "Apple Health is offline-only. No healthcheck needed. Run health_status() to see what's imported."}
    return {"ok": False, "error": f"Unknown vendor '{vendor}'. Supported: oura, google_health, apple_health."}


@mcp.tool()
def health_recommended_labs() -> list[dict[str, Any]]:
    """Return the recommended-labs reference list with the WHY for each marker.

    Use this to tell users which lab markers would benefit them and the rationale.
    Sourced from longevity-medicine consensus (Attia, Boham, Hyman) per the
    advisory-panel pass on 2026-05-10. Each entry has marker, category, why,
    suggested frequency, and rough cost band.
    """
    return labs_mod.RECOMMENDED_PANEL


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

@mcp.tool()
def health_schema() -> list[dict[str, Any]]:
    """List every record type in the DB with row counts + date range."""
    with db.connect(read_only=True) as con:
        return db.types_with_counts(con)


_SQL_FORBIDDEN = {"INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE", "ATTACH", "DETACH", "COPY", "PRAGMA", "SET", "INSTALL", "LOAD"}


@mcp.tool()
def health_query(sql: str, max_rows: int = 1000) -> list[dict[str, Any]]:
    """Run a read-only SQL query against DuckDB.

    Tables: records, workouts, sleep, cycle, symptoms, ecg, state_of_mind,
            labs, imports.
    """
    upper = sql.strip().upper()
    first = upper.split()[0] if upper else ""
    if first in _SQL_FORBIDDEN:
        raise ValueError(f"Read-only query layer. Rejected first word: {first}")
    for tok in _SQL_FORBIDDEN:
        if f" {tok} " in f" {upper} ":
            raise ValueError(f"Read-only query layer. Rejected keyword: {tok}")
    with db.connect(read_only=True) as con:
        rows = con.execute(sql).fetchmany(max_rows)
        cols = [d[0] for d in con.description] if con.description else []
    return [dict(zip(cols, [str(v) if isinstance(v, datetime) else v for v in r])) for r in rows]


@mcp.tool()
def health_metric_series(metric: str, start: str, end: str, aggregation: str = "daily") -> list[dict[str, Any]]:
    """Time series for a quantity-type metric. aggregation: 'daily' | 'hourly' | 'raw'."""
    sd = datetime.fromisoformat(start.replace("Z", ""))
    ed = datetime.fromisoformat(end.replace("Z", ""))
    sum_metrics = {t for t, m in HK_QUANTITY_TYPES.items() if m["aggregation"] == "sum"}
    agg = "SUM" if metric in sum_metrics else "AVG"
    with db.connect(read_only=True) as con:
        if aggregation == "raw":
            rows = con.execute(
                "SELECT start_date, value, source_name FROM records WHERE type = ? AND start_date >= ? AND start_date < ? ORDER BY start_date",
                [metric, sd, ed],
            ).fetchall()
            return [{"timestamp": str(r[0]), "value": float(r[1]) if r[1] is not None else None, "source": r[2]} for r in rows]
        bucket = "hour" if aggregation == "hourly" else "day"
        rows = con.execute(
            f"SELECT DATE_TRUNC('{bucket}', start_date) AS bucket, {agg}(value), COUNT(*) "
            "FROM records WHERE type = ? AND start_date >= ? AND start_date < ? GROUP BY bucket ORDER BY bucket",
            [metric, sd, ed],
        ).fetchall()
        return [{"bucket": str(r[0]), "value": round(float(r[1]), 3) if r[1] is not None else None, "n": int(r[2])} for r in rows]


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

@mcp.tool()
def health_workout_list(start: str, end: str, activity_type: str | None = None) -> list[dict[str, Any]]:
    """List workouts in a date range, optionally filtered by activity_type."""
    sd = datetime.fromisoformat(start.replace("Z", ""))
    ed = datetime.fromisoformat(end.replace("Z", ""))
    where = "start_date >= ? AND start_date < ?"
    params: list[Any] = [sd, ed]
    if activity_type:
        where += " AND activity_type = ?"
        params.append(activity_type)
    with db.connect(read_only=True) as con:
        rows = con.execute(
            f"SELECT activity_type, start_date, end_date, duration_min, distance_km, energy_kcal, source_name "
            f"FROM workouts WHERE {where} ORDER BY start_date",
            params,
        ).fetchall()
    return [{"activity_type": r[0], "start": str(r[1]), "end": str(r[2]), "duration_min": round(float(r[3]), 1) if r[3] is not None else None, "distance_km": round(float(r[4]), 2) if r[4] is not None else None, "energy_kcal": round(float(r[5])) if r[5] is not None else None, "source": r[6]} for r in rows]


@mcp.tool()
def health_sleep_summary(start: str, end: str) -> list[dict[str, Any]]:
    """Per-night sleep summary in a date range."""
    sd = _parse_date(start)
    ed = _parse_date(end)
    out: list[dict[str, Any]] = []
    with db.connect(read_only=True) as con:
        cur = sd
        while cur <= ed:
            sleep = scores._sleep_for_date(con, cur)
            if sleep["asleep_min"] > 0 or sleep["in_bed_min"] > 0:
                out.append({
                    "night_of": cur.isoformat(), "asleep_min": round(sleep["asleep_min"]),
                    "in_bed_min": round(sleep["in_bed_min"]), "rem_min": round(sleep["rem_min"]),
                    "deep_min": round(sleep["deep_min"]), "core_min": round(sleep["core_min"]),
                    "awake_min": round(sleep["awake_min"]), "efficiency": round(sleep["efficiency"], 3),
                })
            cur += timedelta(days=1)
    return out


@mcp.tool()
def health_recovery_score(date_str: str) -> dict[str, Any]:
    """Recovery score 0-100. Open formula: 40% HRV + 20% RHR + 25% sleep duration + 15% efficiency."""
    with db.connect(read_only=True) as con:
        return scores.recovery_score(con, _parse_date(date_str))


@mcp.tool()
def health_sleep_score(date_str: str) -> dict[str, Any]:
    """Sleep score 0-100. 40% duration + 25% efficiency + 20% REM% + 15% deep%."""
    with db.connect(read_only=True) as con:
        return scores.sleep_score(con, _parse_date(date_str))


@mcp.tool()
def health_strain_score(date_str: str) -> dict[str, Any]:
    """Strain score 0-21 (Whoop-shape, log compression)."""
    with db.connect(read_only=True) as con:
        return scores.strain_score(con, _parse_date(date_str))


@mcp.tool()
def health_sleep_regularity(start: str, end: str) -> dict[str, Any]:
    """Sleep regularity index over a window: bed/wake variance, latency, naps.

    Chronic sleep debt (Winter, panel 2026-05-10) is the actual predictor —
    a single night isn't the signal. Returns regularity_score 0-100, plus
    bed-time + wake-time + duration stdev, mean latency, and nap count.
    """
    with db.connect(read_only=True) as con:
        return scores.sleep_regularity(con, _parse_date(start), _parse_date(end))


@mcp.tool()
def health_longevity_panel(date_str: str) -> dict[str, Any]:
    """Surface VO2Max, walking speed, walking steadiness, lean mass, body fat,
    Zone-2 minutes (last 30d), and 6-minute walk distance.

    The single most predictive longevity markers (Attia + Patrick, panel
    2026-05-10) bundled into one call. Apple Watch records most of these
    automatically.
    """
    with db.connect(read_only=True) as con:
        return scores.longevity_panel(con, _parse_date(date_str))


@mcp.tool()
def health_somatic_state(date_str: str, lookback_min: int = 30) -> dict[str, Any]:
    """Recent HR/HRV volatility + body_says_slow_down boolean.

    Coaching skill should call this BEFORE emotional inquiry. If body_says_slow_down
    is True (recent HR volatility, HR peak, or HRV crash), regulate first
    (breath / body scan / slow check-in) before reframe work. Sympathetic
    activation makes coaching counterproductive (Levine, panel 2026-05-10).
    """
    with db.connect(read_only=True) as con:
        return scores.somatic_state(con, _parse_date(date_str), lookback_min=lookback_min)


@mcp.tool()
def health_nutrition_summary(start: str, end: str) -> dict[str, Any]:
    """Daily kcal / macros / fiber / water / caffeine / alcohol averages over
    the window, plus an under-fuel detector.

    Under-fuel rule (Braddock, panel 2026-05-10): kcal_consumed < 0.7 *
    (basal + active). If 30%+ of days are under-fueled, the recovery-score
    'rest more' advice is mis-framed — the actual signal is 'eat enough'.
    Requires a paired nutrition app (MyFitnessPal, Cronometer, etc.) writing
    HKQuantityTypeIdentifierDietary* records.
    """
    with db.connect(read_only=True) as con:
        return scores.nutrition_summary(con, _parse_date(start), _parse_date(end))


@mcp.tool()
def health_long_window(metric: str, years: int = 2, aggregation: str = "avg") -> dict[str, Any]:
    """Year-over-year same-month comparison + persistent-asymmetry detector.

    Trauma signatures and seasonal Floor-body coupling (van der Kolk, panel
    2026-05-10) don't show up in 30-day windows. They show up as the same
    metric trending the wrong direction every spring, every anniversary date.
    """
    with db.connect(read_only=True) as con:
        return scores.long_window(con, metric, years=years, aggregation=aggregation)


# ---------------------------------------------------------------------------
# Cycle (Sims + Briden, panel 2026-05-10)
# ---------------------------------------------------------------------------

@mcp.tool()
def health_cycle_context(date_str: str) -> dict[str, Any]:
    """Current cycle phase + cycle-day + length variance + irregularity flag.

    Phases: menstrual / follicular / ovulation / luteal. Refines band-based
    guess with ovulation test results when available.

    A low-HRV day in mid-luteal is normal physiology, not a recovery deficit.
    The substrate gaslights half its users until the journal / coaching /
    panel skills include phase context. Pair with phase_tagged_metric for
    series and phase_means for per-phase aggregates.
    """
    with db.connect(read_only=True) as con:
        return cycle_mod.cycle_context(con, _parse_date(date_str))


@mcp.tool()
def health_phase_tagged_metric(metric: str, start: str, end: str, aggregation: str = "avg") -> list[dict[str, Any]]:
    """Daily metric series with cycle-phase tag on each day.

    Returns list of {date, value, cycle_day, phase}. Use to plot HRV /
    sleep / RHR / etc. by cycle phase or to feed correlation analysis.
    """
    with db.connect(read_only=True) as con:
        return cycle_mod.phase_tagged_metric(con, metric, _parse_date(start), _parse_date(end), aggregation=aggregation)


@mcp.tool()
def health_phase_means(metric: str, days: int = 90, aggregation: str = "avg") -> dict[str, Any]:
    """Mean of a metric segmented by cycle phase over the last N days.

    Confirms with the user's own data the 'low HRV in luteal is normal'
    pattern. Or surfaces the opposite if it's NOT normal for them.
    """
    with db.connect(read_only=True) as con:
        return cycle_mod.phase_means_for_metric(con, metric, days=days, aggregation=aggregation)


# ---------------------------------------------------------------------------
# Symptoms / ECG / State of mind / Audio
# ---------------------------------------------------------------------------

@mcp.tool()
def health_symptom_timeline(start: str, end: str, symptom_type: str | None = None) -> list[dict[str, Any]]:
    """List symptom log entries in a date range. Optionally filter to a
    specific HKCategoryTypeIdentifier* type."""
    sd = datetime.fromisoformat(start.replace("Z", ""))
    ed = datetime.fromisoformat(end.replace("Z", ""))
    where = "start_date >= ? AND start_date < ?"
    params: list[Any] = [sd, ed]
    if symptom_type:
        where += " AND type = ?"
        params.append(symptom_type)
    with db.connect(read_only=True) as con:
        rows = con.execute(
            f"SELECT type, start_date, end_date, severity, source_name FROM symptoms WHERE {where} ORDER BY start_date",
            params,
        ).fetchall()
    return [{"type": r[0], "start": str(r[1]), "end": str(r[2]), "severity": r[3], "source": r[4]} for r in rows]


@mcp.tool()
def health_ecg_list(start: str, end: str) -> list[dict[str, Any]]:
    """List ECG entries in a date range with classification (sinus / afib /
    inconclusive)."""
    sd = datetime.fromisoformat(start.replace("Z", ""))
    ed = datetime.fromisoformat(end.replace("Z", ""))
    with db.connect(read_only=True) as con:
        rows = con.execute(
            "SELECT start_date, classification, average_heart_rate, sampling_frequency, source_name "
            "FROM ecg WHERE start_date >= ? AND start_date < ? ORDER BY start_date",
            [sd, ed],
        ).fetchall()
    return [{"timestamp": str(r[0]), "classification": r[1], "avg_hr_bpm": round(float(r[2]), 1) if r[2] is not None else None, "sampling_frequency_hz": float(r[3]) if r[3] is not None else None, "source": r[4]} for r in rows]


@mcp.tool()
def health_state_of_mind_timeline(start: str, end: str) -> list[dict[str, Any]]:
    """iOS 17+ State of Mind mood logs. Returns entries with valence (-1 to +1),
    kind (momentary or daily), labels, and associations."""
    sd = datetime.fromisoformat(start.replace("Z", ""))
    ed = datetime.fromisoformat(end.replace("Z", ""))
    with db.connect(read_only=True) as con:
        rows = con.execute(
            "SELECT start_date, end_date, kind, valence, labels, associations, source_name "
            "FROM state_of_mind WHERE start_date >= ? AND start_date < ? ORDER BY start_date",
            [sd, ed],
        ).fetchall()
    return [{"start": str(r[0]), "end": str(r[1]), "kind": r[2], "valence": float(r[3]) if r[3] is not None else None, "labels": r[4], "associations": r[5], "source": r[6]} for r in rows]


@mcp.tool()
def health_audio_exposure(start: str, end: str, threshold_db: float = 80.0) -> dict[str, Any]:
    """Environmental + headphone audio exposure summary, with hours over the
    safe-listening threshold. Default threshold 80 dB matches WHO guidance.
    """
    sd = datetime.fromisoformat(start.replace("Z", ""))
    ed = datetime.fromisoformat(end.replace("Z", ""))
    with db.connect(read_only=True) as con:
        env = con.execute(
            "SELECT AVG(value), MAX(value), COUNT(*) FROM records "
            "WHERE type = 'HKQuantityTypeIdentifierEnvironmentalAudioExposure' "
            "AND start_date >= ? AND start_date < ?",
            [sd, ed],
        ).fetchone()
        head = con.execute(
            "SELECT AVG(value), MAX(value), COUNT(*) FROM records "
            "WHERE type = 'HKQuantityTypeIdentifierHeadphoneAudioExposure' "
            "AND start_date >= ? AND start_date < ?",
            [sd, ed],
        ).fetchone()
        env_over = con.execute(
            "SELECT COALESCE(SUM(EXTRACT(EPOCH FROM (end_date - start_date))) / 3600.0, 0) FROM records "
            "WHERE type = 'HKQuantityTypeIdentifierEnvironmentalAudioExposure' "
            "AND value > ? AND start_date >= ? AND start_date < ?",
            [threshold_db, sd, ed],
        ).fetchone()
        head_over = con.execute(
            "SELECT COALESCE(SUM(EXTRACT(EPOCH FROM (end_date - start_date))) / 3600.0, 0) FROM records "
            "WHERE type = 'HKQuantityTypeIdentifierHeadphoneAudioExposure' "
            "AND value > ? AND start_date >= ? AND start_date < ?",
            [threshold_db, sd, ed],
        ).fetchone()
    return {
        "start": start, "end": end, "threshold_db": threshold_db,
        "environmental": {
            "avg_db": round(float(env[0]), 1) if env and env[0] is not None else None,
            "max_db": round(float(env[1]), 1) if env and env[1] is not None else None,
            "n_samples": int(env[2]) if env else 0,
            "hours_over_threshold": round(float(env_over[0]), 1) if env_over else 0,
        },
        "headphone": {
            "avg_db": round(float(head[0]), 1) if head and head[0] is not None else None,
            "max_db": round(float(head[1]), 1) if head and head[1] is not None else None,
            "n_samples": int(head[2]) if head else 0,
            "hours_over_threshold": round(float(head_over[0]), 1) if head_over else 0,
        },
        "interpretation_hint": "WHO recommends < 1hr/day at >85 dB. Cumulative hearing damage is dose-dependent and irreversible.",
    }


@mcp.tool()
def health_lab_panel(date_str: str | None = None, lookback_days: int = 365) -> list[dict[str, Any]]:
    """Most recent lab values per marker within lookback window.

    Returns one row per marker with the most recent test_date, value, range,
    and status (low / in_range / high). If date_str is provided, returns the
    panel at that date — useful for time-traveling lab reads.
    """
    cutoff_dt = (
        _parse_date(date_str) - timedelta(days=lookback_days)
        if date_str else date.today() - timedelta(days=lookback_days)
    )
    end_dt = _parse_date(date_str) if date_str else date.today()
    with db.connect(read_only=True) as con:
        rows = con.execute(
            """
            SELECT marker, test_date, value, unit, range_low, range_high, status, panel, source
            FROM labs WHERE test_date >= ? AND test_date <= ?
            ORDER BY marker, test_date DESC
            """,
            [cutoff_dt, end_dt],
        ).fetchall()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        marker = r[0]
        if marker in seen:
            continue
        seen.add(marker)
        out.append({
            "marker": marker, "test_date": str(r[1]),
            "value": float(r[2]) if r[2] is not None else None,
            "unit": r[3], "range_low": float(r[4]) if r[4] is not None else None,
            "range_high": float(r[5]) if r[5] is not None else None,
            "status": r[6], "panel": r[7], "source": r[8],
        })
    return out


# ---------------------------------------------------------------------------
# Vault-aware (substrate differentiator)
# ---------------------------------------------------------------------------

@mcp.tool()
def health_journal_context(date_str: str, voice_profile: str = "curious") -> dict[str, Any]:
    """24h health roll-up for daily-journal, in the voice profile of choice.

    voice_profile: 'clinical' | 'warm' | 'curious'.
      clinical: exact numbers + percent deltas. For data export.
      warm: narrative sentences. Default for daily-journal default.
      curious: open-ended observation + a question. Default for coaching.

    Returns both the structured data AND a `prompt_text` rendered in the
    chosen register so the host skill can paste it directly into its prompt
    without breaking voice.
    """
    target = _parse_date(date_str)
    with db.connect(read_only=True) as con:
        ctx = vault_aware.journal_context(con, target)
        # Compute 30-day HRV baseline for delta phrasing.
        baseline_row = con.execute(
            "SELECT AVG(daily) FROM (SELECT DATE_TRUNC('day', start_date) AS d, AVG(value) AS daily "
            "FROM records WHERE type = 'HKQuantityTypeIdentifierHeartRateVariabilitySDNN' "
            "AND start_date >= ? AND start_date < ? GROUP BY d)",
            [datetime.combine(target - timedelta(days=30), datetime.min.time()), datetime.combine(target, datetime.min.time())],
        ).fetchone()
        baseline = float(baseline_row[0]) if baseline_row and baseline_row[0] is not None else None
    from voice_bridge import render_journal_context_with_baseline
    ctx["hrv_baseline_30d_ms"] = round(baseline, 1) if baseline is not None else None
    ctx["voice_profile"] = voice_profile
    ctx["prompt_text"] = render_journal_context_with_baseline(ctx, baseline, profile=voice_profile)
    return ctx


@mcp.tool()
def health_journal_body_question(date_str: str) -> dict[str, Any]:
    """Body literacy prompt: returns a context-aware question (not a number)
    for the daily-journal skill. The question lands differently depending on
    what the body did — hard night, deep rest, low HRV, etc.
    """
    with db.connect(read_only=True) as con:
        return vault_aware.journal_body_question(con, _parse_date(date_str))


@mcp.tool()
def health_floor_correlation(metric: str, days: int = 30, vault_root: str = "") -> dict[str, Any]:
    """Pearson r between numeric floor_level and a daily biometric.

    vault_root: absolute path to your personal vault. Required.
    """
    if not vault_root:
        return {"metric": metric, "n": 0, "note": "vault_root is required."}
    vr = Path(vault_root).expanduser().resolve()
    if not vr.is_dir():
        return {"metric": metric, "n": 0, "note": f"vault_root {vr} not found."}
    with db.connect(read_only=True) as con:
        return vault_aware.floor_correlation(con, metric, days, vr)


@mcp.tool()
def health_symptom_correlation(symptom_type: str, days: int = 90, vault_root: str = "") -> dict[str, Any]:
    """Correlate occurrences of a specific symptom with Floor tags. Returns
    per-floor symptom-incidence percentages.
    """
    if not vault_root:
        return {"symptom": symptom_type, "n": 0, "note": "vault_root is required."}
    vr = Path(vault_root).expanduser().resolve()
    if not vr.is_dir():
        return {"symptom": symptom_type, "n": 0, "note": f"vault_root {vr} not found."}
    with db.connect(read_only=True) as con:
        return vault_aware.symptom_correlation(con, symptom_type, days, vr)


@mcp.tool()
def health_coaching_context(start: str, end: str, theme: str | None = None, vault_root: str = "") -> dict[str, Any]:
    """Recovery-vs-stress markers + Floor distribution over a coaching window."""
    sd = _parse_date(start)
    ed = _parse_date(end)
    vr = Path(vault_root).expanduser().resolve() if vault_root else Path("/")
    with db.connect(read_only=True) as con:
        return vault_aware.coaching_context(con, sd, ed, vr)


@mcp.tool()
def health_panel_context(date_str: str, vault_root: str = "") -> dict[str, Any]:
    """Same-day stress/recovery snapshot for advisory-panel decision moments."""
    target = _parse_date(date_str)
    vr = Path(vault_root).expanduser().resolve() if vault_root else Path("/")
    with db.connect(read_only=True) as con:
        return vault_aware.panel_context(con, target, vr)


@mcp.tool()
def health_weekly_rollup(week_start: str) -> dict[str, Any]:
    """Feeds /insights weekly review."""
    with db.connect(read_only=True) as con:
        return vault_aware.weekly_rollup(con, _parse_date(week_start))


@mcp.tool()
def health_long_window_with_journal(metric: str, years: int = 2, vault_root: str = "") -> dict[str, Any]:
    """Pair scores.long_window YoY + persistent-asymmetry analysis with Floor
    tags from the same months. Surfaces seasonal Floor-body coupling
    (van der Kolk's anniversary-pattern hypothesis)."""
    if not vault_root:
        return {"metric": metric, "note": "vault_root is required."}
    vr = Path(vault_root).expanduser().resolve()
    if not vr.is_dir():
        return {"metric": metric, "note": f"vault_root {vr} not found."}
    with db.connect(read_only=True) as con:
        return vault_aware.long_window_with_journal(con, metric, years, vr)


# ---------------------------------------------------------------------------
# Live (Health Auto Export TCP)
# ---------------------------------------------------------------------------

@mcp.tool()
def health_live_query(metric: str, host: str = "localhost", port: int = 9000, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    """Query the Health Auto Export iOS app over TCP for live data."""
    params: dict[str, Any] = {"metric": metric}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    return live_tcp.query("health_metrics", params, host=host, port=port)


# ---------------------------------------------------------------------------
# Coach (v0.4) — longevity + fitness coach state layer
# ---------------------------------------------------------------------------

@mcp.tool()
def health_coach_prescribe(date_str: str, profile_json: str = "{}") -> dict[str, Any]:
    """Issue a daily workout prescription.

    Reads recovery + sleep + cycle + somatic state from health-mcp, applies
    the profile (days_per_week, equipment, level, started_iso, etc.), and
    returns a structured prescription with workout_type + intensity +
    difficulty + why_today + deload flag.

    profile_json: JSON string of the user's coach profile. The /coach skill
    loads it from <vault>/Meta/coach-profile.yaml and passes it in.

    The actual exercise list + sets/reps/weights is rendered by the /coach
    skill's prompt using this prescription + the per-lift progression state
    from health_coach_lift_state.
    """
    try:
        profile = json.loads(profile_json) if profile_json else {}
    except json.JSONDecodeError:
        profile = {}
    target = _parse_date(date_str)
    with db.connect() as con:
        recovery = scores.recovery_score(con, target)
        sleep_s = scores.sleep_score(con, target)
        cycle_ctx = cycle_mod.cycle_context(con, target)
        if cycle_ctx.get("phase") == "unknown":
            cycle_ctx = None
        somatic = scores.somatic_state(con, target, lookback_min=30)
        decision = coach_mod.decide_workout_type(
            con, target, profile, recovery, sleep_s, cycle_ctx, somatic,
        )
        rx_id = coach_mod.prescription_id(target.isoformat(), decision["workout_type"])
        # Idempotent: skip writing if same id exists today.
        existing = con.execute(
            "SELECT prescription_id FROM coach_prescriptions WHERE prescription_id = ?",
            [rx_id],
        ).fetchone()
        if not existing:
            con.execute(
                "INSERT INTO coach_prescriptions "
                "(prescribed_for, prescribed_at, workout_type, difficulty, duration_min, body_focus, exercises_json, why_today, prescription_id) "
                "VALUES (?, NOW(), ?, ?, ?, ?, ?, ?, ?)",
                [
                    target, decision["workout_type"], decision["difficulty"],
                    int(profile.get("session_minutes", 45)),
                    profile.get("body_focus", ""),
                    "",  # exercises_json filled by skill output if persisted
                    decision["why_today"], rx_id,
                ],
            )
    return {
        "prescription_id": rx_id,
        "date": target.isoformat(),
        "workout_type": decision["workout_type"],
        "intensity_factor": decision["intensity_factor"],
        "difficulty": decision["difficulty"],
        "deload_week": decision["deload_week"],
        "why_today": decision["why_today"],
        "recovery_score": recovery.get("score") if recovery else None,
        "sleep_score": sleep_s.get("score") if sleep_s else None,
        "cycle_phase": cycle_ctx.get("phase") if cycle_ctx else None,
        "body_says_slow_down": somatic.get("body_says_slow_down") if somatic else None,
        "interpretation_hint": (
            "Use workout_type to pick the template. Use intensity_factor to "
            "scale loads off the user's progression state (health_coach_lift_state). "
            "Cite why_today verbatim in the user-facing block. If deload_week is True, "
            "cut volume 40% and intensity 20%."
        ),
    }


@mcp.tool()
def health_coach_lift_state(lift_name: str) -> dict[str, Any]:
    """Get progression state for a major lift (squat, deadlift, bench, etc.).

    Returns {last_weight_kg, consecutive_full_sets, consecutive_failures,
    current_top_set_kg, recommended_next_load (with action + weight + note)}.

    Use this to program the next session's loads. Apply fail-twice-drop-10%,
    complete-twice-add-2.5kg-upper / 5kg-lower, hold-on-single-fail.
    """
    with db.connect(read_only=True) as con:
        state = coach_mod.get_last_lift(con, lift_name)
    next_load = coach_mod.next_lift_load(state, prescribed_reps=5, prescribed_sets=3)
    return {
        "lift_name": lift_name,
        "state": state,
        "recommended_next_load": next_load,
    }


@mcp.tool()
def health_coach_log_completion(
    prescription_id: str,
    rpe: int | None = None,
    notes: str | None = None,
    lift_actuals_json: str = "[]",
) -> dict[str, Any]:
    """Log a completed session + update per-lift progression.

    lift_actuals_json: JSON array. Each entry: {lift_name, weight_kg,
    sets_completed, reps_completed_per_set (list), prescribed_sets,
    prescribed_reps}.

    Updates coach_lift_progress so the next prescription reads the new state.
    """
    try:
        actuals = json.loads(lift_actuals_json) if lift_actuals_json else []
    except json.JSONDecodeError:
        actuals = []
    with db.connect() as con:
        result = coach_mod.log_completion(con, prescription_id, rpe, notes, actuals)
    return result


@mcp.tool()
def health_coach_recent_prescriptions(days: int = 7) -> list[dict[str, Any]]:
    """List recent prescriptions with their completion status."""
    cutoff = date.today() - timedelta(days=days)
    with db.connect(read_only=True) as con:
        rows = con.execute(
            """
            SELECT p.prescribed_for, p.workout_type, p.difficulty, p.why_today,
                   p.prescription_id,
                   (SELECT COUNT(*) FROM coach_completions c WHERE c.prescription_id = p.prescription_id) AS completed
            FROM coach_prescriptions p
            WHERE p.prescribed_for >= ?
            ORDER BY p.prescribed_for DESC
            """,
            [cutoff],
        ).fetchall()
    return [
        {
            "date": str(r[0]), "workout_type": r[1], "difficulty": int(r[2] or 0),
            "why_today": r[3], "prescription_id": r[4],
            "completed": int(r[5] or 0) > 0,
        }
        for r in rows
    ]


@mcp.tool()
def health_coach_summary(days: int = 28) -> dict[str, Any]:
    """Coach summary for the last N days: completion rate, deload weeks,
    workout-type distribution, average RPE, top-set progress on tracked lifts.

    Use for weekly + monthly coach reviews.
    """
    cutoff = date.today() - timedelta(days=days)
    with db.connect(read_only=True) as con:
        prescribed = con.execute(
            "SELECT COUNT(*) FROM coach_prescriptions WHERE prescribed_for >= ?",
            [cutoff],
        ).fetchone()[0]
        completed = con.execute(
            """
            SELECT COUNT(DISTINCT c.prescription_id)
            FROM coach_completions c JOIN coach_prescriptions p
              ON c.prescription_id = p.prescription_id
            WHERE p.prescribed_for >= ?
            """,
            [cutoff],
        ).fetchone()[0]
        by_type = con.execute(
            """
            SELECT workout_type, COUNT(*) FROM coach_prescriptions
            WHERE prescribed_for >= ? GROUP BY workout_type ORDER BY 2 DESC
            """,
            [cutoff],
        ).fetchall()
        avg_rpe_row = con.execute(
            """
            SELECT AVG(c.rpe) FROM coach_completions c JOIN coach_prescriptions p
              ON c.prescription_id = p.prescription_id
            WHERE p.prescribed_for >= ?
            """,
            [cutoff],
        ).fetchone()
        avg_rpe = float(avg_rpe_row[0]) if avg_rpe_row and avg_rpe_row[0] is not None else None
        lifts = con.execute(
            "SELECT lift_name, current_top_set_kg, last_session_date FROM coach_lift_progress "
            "WHERE last_session_date >= ? ORDER BY current_top_set_kg DESC",
            [cutoff],
        ).fetchall()
    return {
        "days_window": days,
        "prescribed": int(prescribed or 0),
        "completed": int(completed or 0),
        "completion_rate_pct": round(100 * (completed or 0) / (prescribed or 1), 1),
        "workout_type_distribution": {r[0]: int(r[1]) for r in by_type},
        "average_rpe": round(avg_rpe, 1) if avg_rpe is not None else None,
        "tracked_lifts": [
            {"lift_name": r[0], "current_top_set_kg": float(r[1]) if r[1] is not None else None, "last_session_date": str(r[2])}
            for r in lifts
        ],
    }




# ---------------------------------------------------------------------------
# v0.7 Analytics surface (multi-year correlation + Floor/Loop fingerprints)
# ---------------------------------------------------------------------------

@mcp.tool()
def health_correlate(
    metric_a: str,
    metric_b: str,
    group_by: str | None = None,
    vault_root: str = "",
    lookback_days: int = 365,
) -> dict[str, Any]:
    """Pearson correlation between two metrics over the lookback window.

    metric_a / metric_b accept friendly names ('hrv', 'rhr', 'steps',
    'vo2max', 'mindful_minutes', ...) or HK identifiers.
    group_by: None | 'floor' | 'cycle_phase' | 'day_of_week'.
    When group_by='floor', vault_root is required.

    Returns r, n, and signal_strength ('strong'|'moderate'|'weak'|'noise')
    so callers can filter noise. Stdlib Pearson, no scipy dep.
    """
    vr = Path(vault_root) if vault_root else None
    with db.connect(read_only=True) as con:
        return analytics.correlate(con, metric_a, metric_b, group_by=group_by, vault_root=vr, lookback_days=lookback_days)


@mcp.tool()
def health_floor_body_fingerprint(
    floor: str,
    vault_root: str,
    lookback_days: int = 365,
) -> dict[str, Any]:
    """Body fingerprint of a Floor: mean HRV / RHR / steps / VO2max /
    mindful / sleep efficiency on days tagged with this Floor vs all other
    days. Reports delta_pct so the surface answers 'what does Anger feel
    like in the body.' Cycle phase distribution included if cycle data
    exists.

    floor: Floor name (e.g. 'Acceptance') or numeric floor_level as string
    (e.g. '23' resolves to floor_level=23).
    """
    try:
        floor_val: str | int = int(floor)
    except ValueError:
        floor_val = floor
    with db.connect(read_only=True) as con:
        return analytics.floor_body_fingerprint(con, Path(vault_root), floor_val, lookback_days=lookback_days)


@mcp.tool()
def health_loop_signature(
    loop_dates_iso: list[str],
    vault_root: str = "",
    lookback_days: int = 365,
) -> dict[str, Any]:
    """Body fingerprint of a named loop. Caller passes the list of dates
    detected as a loop (e.g. Founder Exhaustion Loop cluster) by the
    /patterns skill. Baseline = all other days in the window.

    loop_dates_iso: list of ISO date strings (YYYY-MM-DD).
    """
    loop_dates: list[date] = []
    for s in loop_dates_iso:
        try:
            loop_dates.append(_parse_date(s))
        except ValueError:
            continue
    vr = Path(vault_root) if vault_root else Path("")
    with db.connect(read_only=True) as con:
        return analytics.loop_signature(con, vr, loop_dates, lookback_days=lookback_days)


@mcp.tool()
def health_sleep_architecture(start: str, end: str) -> dict[str, Any]:
    """Per-night sleep architecture summary over [start, end).

    Returns REM%, Deep%, Core%, Awake% (aggregated across nights),
    mean sleep efficiency, mean fragmentation (Awake segment count per
    night). Chris Winter / Stacy Sims surface.
    """
    sd = _parse_date(start)
    ed = _parse_date(end)
    with db.connect(read_only=True) as con:
        return analytics.sleep_architecture(con, sd, ed)


@mcp.tool()
def health_longitudinal_summary(
    start: str,
    end: str,
    granularity: str = "month",
) -> dict[str, Any]:
    """Month / quarter / year aggregation of longevity markers: HRV baseline,
    RHR baseline, VO2max, lean body mass, body mass, walking steadiness,
    active energy mean, steps mean, sleep efficiency. Peter Attia surface
    for tracking the markers that compound over years.

    granularity: 'month' | 'quarter' | 'year'.
    """
    sd = _parse_date(start)
    ed = _parse_date(end)
    with db.connect(read_only=True) as con:
        return analytics.longitudinal_summary(con, sd, ed, granularity=granularity)


@mcp.tool()
def health_symptom_correlate(
    symptom_type: str | None = None,
    vault_root: str = "",
    lookback_days: int = 365,
) -> dict[str, Any]:
    """Body fingerprint of symptom-present days vs symptom-absent days.

    symptom_type: a HK symptom type id (e.g. 'HKCategoryTypeIdentifierHeadache').
    If None, aggregates across all symptom types found in the window.
    Pagliano / Briden surface for finding what predicts a symptom flare.
    """
    vr = Path(vault_root) if vault_root else None
    with db.connect(read_only=True) as con:
        return analytics.symptom_correlate(con, symptom_type=symptom_type, vault_root=vr, lookback_days=lookback_days)


@mcp.tool()
def health_top_signals(
    vault_root: str = "",
    lookback_days: int = 365,
    min_strength: str = "moderate",
) -> dict[str, Any]:
    """Briden-honoring noise filter: scan curated metric pairs + Floor x HRV
    pairings, return ONLY signals at or above min_strength.

    min_strength: 'weak' | 'moderate' | 'strong'.
    This is the entrypoint the /longitudinal skill uses to surface only
    actionable patterns and avoid drowning the user in correlations.
    """
    vr = Path(vault_root) if vault_root else None
    with db.connect(read_only=True) as con:
        return analytics.top_signals(con, vault_root=vr, lookback_days=lookback_days, min_strength=min_strength)


if __name__ == "__main__":
    mcp.run()
