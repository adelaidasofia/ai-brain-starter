#!/usr/bin/env python3
"""Stop hook: after a journal session, run the full daily-once chain:
  1. Sync wearable data (Oura, Fitbit) if > 24h stale
  2. Backfill yesterday's journal with body-track context
  3. Prescribe today's workout (if not yet prescribed)

This is the SINGLE entry point for the daily auto-chain (codified
2026-05-10 after the panel flagged per-SessionStart firing was wasteful).
Tying everything to /journal — the user's existing daily habit — has
three benefits:

  - Fires once per day, not once per session (user has ~20 sessions/day)
  - Honors the substrate philosophy: if you don't journal, the chain
    quietly waits. The user opts into engagement, the substrate doesn't
    nag.
  - Single failure surface: if /journal doesn't fire, /health doctor will
    surface "your wearable data is N days stale". One observability
    surface, not multiple.

The chain Bainbridge approved (panel 2026-05-10): auto-trigger the
ANALYSIS, never auto-trigger the ACTION. This hook prepares the workout +
drops it to the user's calendar (if profile.calendar_drop is set), but
NEVER auto-logs completion. Completion remains explicit via /coach log.

When this fires:
  - Stop hook, matcher: tool_name matches one of {daily-journal, journal}
    or the assistant just wrote a file under [VAULT]/Journals/ for today

What it does:
  1. Wearable sync — Oura + Fitbit, only the vendors with env-var
     credentials set, only if the last import is > 24h old. Multi-day
     catch-up if user has been absent.
  2. Backfill yesterday's journal entry via
     scripts/backfill-journal-body-context.py.
  3. Look up today's coach prescription; if none, create one via the
     coach.decide_workout_type decision tree.
  4. Surface a one-line summary as additionalContext.

Failure modes:
  - No coach profile saved → skip prescription, still try sync + backfill
  - health-mcp missing → skip silently
  - Wearable API down → log skip reason, exit silently (next /journal retries)
  - DuckDB locked → skip silently
  - User absent for 7 days → sync pulls 7 days catch-up at next /journal

Bypass: COACH_AUTO_PRESCRIBE_BYPASS=1 in env (skips ALL three steps).
Granular bypass: HEALTH_AUTO_SYNC_BYPASS=1 (skips only the sync step).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


def _find_health_mcp() -> Path | None:
    for p in (
        Path.home() / ".claude" / "health-mcp",
        Path.home() / "dev" / "ai-brain-starter" / "services" / "health-mcp",
    ):
        if p.is_dir() and (p / "main.py").exists():
            return p
    return None


def _find_repo_root() -> Path | None:
    """Find the ai-brain-starter clone for the backfill script path."""
    for p in (
        Path.home() / "dev" / "ai-brain-starter",
        Path.home() / ".claude" / "skills" / "ai-brain-starter",
    ):
        if p.is_dir() and (p / "scripts" / "backfill-journal-body-context.py").is_file():
            return p
    return None


def _read_profile() -> dict | None:
    """Read coach-profile.yaml from VAULT_ROOT/Meta/coach-profile.yaml."""
    vroot = os.environ.get("VAULT_ROOT")
    if not vroot:
        return None
    candidates = [
        Path(vroot) / "Meta" / "coach-profile.yaml",
        Path(vroot) / "⚙️ Meta" / "coach-profile.yaml",
    ]
    for path in candidates:
        if path.is_file():
            try:
                import yaml
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                if text.startswith("---"):
                    parts = text.split("---", 2)
                    if len(parts) >= 3:
                        return yaml.safe_load(parts[1]) or {}
                return yaml.safe_load(text) or {}
            except Exception:
                return None
    return None


def _emit_silent() -> None:
    print(json.dumps({"continue": True, "suppressOutput": True}))


def _emit_context(line: str) -> None:
    print(json.dumps({
        "continue": True,
        "suppressOutput": False,
        "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": f"[coach-auto] {line}"},
    }))


def _maybe_sync_wearables(db, today: date) -> list[str]:
    """Run Oura + Fitbit sync if either is > 24h stale and credentials present.
    Returns a list of summary lines (one per vendor synced)."""
    if os.environ.get("HEALTH_AUTO_SYNC_BYPASS") == "1":
        return []
    parts: list[str] = []
    yesterday = today - timedelta(days=1)
    have_oura = bool(os.environ.get("OURA_PERSONAL_ACCESS_TOKEN") or os.environ.get("OURA_PAT"))
    have_fitbit = bool(os.environ.get("FITBIT_ACCESS_TOKEN"))
    if not (have_oura or have_fitbit):
        return parts

    if have_oura:
        try:
            with db.connect(read_only=True) as con:
                row = con.execute("SELECT MAX(imported_at) FROM imports WHERE kind = 'oura'").fetchone()
            last_iso = row[0] if row and row[0] else None
            stale = True
            if last_iso:
                last_dt = last_iso if isinstance(last_iso, datetime) else datetime.fromisoformat(str(last_iso))
                stale = (datetime.now() - last_dt) > timedelta(hours=24)
            if stale:
                import oura_client
                with db.connect() as con:
                    sha = oura_client.folder_sha(yesterday, today)
                    if not db.file_already_imported(con, sha):
                        rows_total = 0
                        for item in oura_client.fetch_range(yesterday, today):
                            kind = item["_kind"]
                            if kind == "record":
                                con.execute(
                                    "INSERT INTO records (type, source_name, unit, start_date, end_date, value, value_str) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (item["type"], item["source_name"], item.get("unit", ""), item["start_date"], item["end_date"], item.get("value"), item.get("value_str")),
                                )
                            elif kind == "workout":
                                con.execute(
                                    "INSERT INTO workouts (activity_type, duration_min, distance_km, energy_kcal, start_date, end_date, source_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (item["activity_type"], item["duration_min"], item.get("distance_km"), item.get("energy_kcal"), item["start_date"], item["end_date"], item.get("source_name", "Oura")),
                                )
                            elif kind == "sleep":
                                con.execute(
                                    "INSERT INTO sleep (start_date, end_date, stage, source_name) VALUES (?, ?, ?, ?)",
                                    (item["start_date"], item["end_date"], item["stage"], item.get("source_name", "Oura")),
                                )
                            rows_total += 1
                        db.log_import(con, sha, "oura", f"oura:{yesterday.isoformat()}..{today.isoformat()}", rows_total)
                        parts.append(f"Oura +{rows_total}")
        except Exception as e:
            parts.append(f"Oura skip: {type(e).__name__}")

    # Apple Shortcuts bridge: sweep iCloud Drive inbox for any new <YYYY-MM-DD>.json
    # payloads written by the iOS Shortcut. No credentials needed; the iOS
    # Shortcut runs as a personal automation and writes to iCloud Drive,
    # which Mac mounts at ~/Library/Mobile Documents/com~apple~CloudDocs/.
    try:
        import shortcut_normalize
        inbox = shortcut_normalize.default_inbox()
        if inbox.is_dir():
            payloads = sorted(p for p in inbox.glob("*.json") if p.parent == inbox)
            if payloads:
                import shutil
                processed_dir = inbox / "processed"
                processed_dir.mkdir(parents=True, exist_ok=True)
                imported = 0
                rows_total = 0
                for f in payloads:
                    file_sha = shortcut_normalize.payload_sha(f)
                    with db.connect() as con:
                        if db.file_already_imported(con, file_sha):
                            shutil.move(str(f), str(processed_dir / f.name))
                            continue
                        try:
                            payload = shortcut_normalize.load_payload_file(f)
                        except Exception:
                            continue
                        n = 0
                        for item in shortcut_normalize.iter_payload(payload):
                            kind = item["_kind"]
                            if kind == "record":
                                con.execute(
                                    "INSERT INTO records (type, source_name, unit, start_date, end_date, value, value_str) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (item["type"], item["source_name"], item.get("unit", ""), item["start_date"], item["end_date"], item.get("value"), item.get("value_str")),
                                )
                            elif kind == "workout":
                                con.execute(
                                    "INSERT INTO workouts (activity_type, duration_min, distance_km, energy_kcal, start_date, end_date, source_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (item["activity_type"], item["duration_min"], item.get("distance_km"), item.get("energy_kcal"), item["start_date"], item["end_date"], item.get("source_name", "Apple Watch")),
                                )
                            elif kind == "sleep":
                                con.execute(
                                    "INSERT INTO sleep (start_date, end_date, stage, source_name) VALUES (?, ?, ?, ?)",
                                    (item["start_date"], item["end_date"], item["stage"], item.get("source_name", "Apple Watch")),
                                )
                            elif kind == "cycle":
                                con.execute(
                                    "INSERT INTO cycle (type, start_date, end_date, value, source_name) VALUES (?, ?, ?, ?, ?)",
                                    (item["type"], item["start_date"], item["end_date"], item.get("value"), item.get("source_name", "Cycle Tracking")),
                                )
                            n += 1
                        db.log_import(con, file_sha, "shortcut", str(f), n)
                    shutil.move(str(f), str(processed_dir / f.name))
                    imported += 1
                    rows_total += n
                if imported:
                    parts.append(f"Apple Shortcut +{rows_total} ({imported}d)")
    except Exception as e:
        parts.append(f"Apple Shortcut skip: {type(e).__name__}")

    if have_fitbit:
        try:
            with db.connect(read_only=True) as con:
                row = con.execute("SELECT MAX(imported_at) FROM imports WHERE kind = 'fitbit'").fetchone()
            last_iso = row[0] if row and row[0] else None
            stale = True
            if last_iso:
                last_dt = last_iso if isinstance(last_iso, datetime) else datetime.fromisoformat(str(last_iso))
                stale = (datetime.now() - last_dt) > timedelta(hours=24)
            if stale:
                import fitbit_client
                with db.connect() as con:
                    sha = fitbit_client.folder_sha(yesterday, today)
                    if not db.file_already_imported(con, sha):
                        rows_total = 0
                        for item in fitbit_client.fetch_range(yesterday, today):
                            kind = item["_kind"]
                            if kind == "record":
                                con.execute(
                                    "INSERT INTO records (type, source_name, unit, start_date, end_date, value, value_str) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (item["type"], item["source_name"], item.get("unit", ""), item["start_date"], item["end_date"], item.get("value"), item.get("value_str")),
                                )
                            elif kind == "sleep":
                                con.execute(
                                    "INSERT INTO sleep (start_date, end_date, stage, source_name) VALUES (?, ?, ?, ?)",
                                    (item["start_date"], item["end_date"], item["stage"], item.get("source_name", "Fitbit")),
                                )
                            rows_total += 1
                        db.log_import(con, sha, "fitbit", f"fitbit:{yesterday.isoformat()}..{today.isoformat()}", rows_total)
                        parts.append(f"Fitbit +{rows_total}")
        except Exception as e:
            parts.append(f"Fitbit skip: {type(e).__name__}")

    return parts


def main() -> int:
    if os.environ.get("COACH_AUTO_PRESCRIBE_BYPASS") == "1":
        _emit_silent()
        return 0

    health_mcp = _find_health_mcp()
    if not health_mcp:
        _emit_silent()
        return 0

    sys.path.insert(0, str(health_mcp))
    venv_site = health_mcp / ".venv" / "lib"
    if venv_site.is_dir():
        for py_dir in venv_site.glob("python*/site-packages"):
            sys.path.insert(0, str(py_dir))

    try:
        import db
    except Exception:
        _emit_silent()
        return 0

    today = date.today()
    summary_parts: list[str] = []

    # 1. Wearable sync (Oura + Fitbit if > 24h stale, only vendors with credentials set).
    summary_parts.extend(_maybe_sync_wearables(db, today))

    # 2. Coach prescription + journal backfill require the coach profile.
    profile = _read_profile()
    if not profile:
        # No profile yet -> emit sync summary if any, otherwise silent.
        if summary_parts:
            _emit_context("; ".join(summary_parts))
        else:
            _emit_silent()
        return 0

    try:
        import coach as coach_mod
        import scores
        import cycle as cycle_mod
    except Exception:
        if summary_parts:
            _emit_context("; ".join(summary_parts))
        else:
            _emit_silent()
        return 0

    # 3. Has today already been prescribed?
    try:
        with db.connect(read_only=True) as con:
            row = con.execute(
                "SELECT prescription_id FROM coach_prescriptions WHERE prescribed_for = ? LIMIT 1",
                [today],
            ).fetchone()
        already_prescribed = bool(row)
    except Exception:
        already_prescribed = True  # Don't double-prescribe on error

    if not already_prescribed:
        try:
            with db.connect() as con:
                recovery = scores.recovery_score(con, today)
                sleep_s = scores.sleep_score(con, today)
                cycle_ctx = cycle_mod.cycle_context(con, today)
                if cycle_ctx.get("phase") == "unknown":
                    cycle_ctx = None
                somatic = scores.somatic_state(con, today, lookback_min=30)
                decision = coach_mod.decide_workout_type(con, today, profile, recovery, sleep_s, cycle_ctx, somatic)
                rx_id = coach_mod.prescription_id(today.isoformat(), decision["workout_type"])
                con.execute(
                    "INSERT INTO coach_prescriptions (prescribed_for, prescribed_at, workout_type, difficulty, "
                    "duration_min, body_focus, exercises_json, why_today, prescription_id) "
                    "VALUES (?, NOW(), ?, ?, ?, ?, '', ?, ?)",
                    [today, decision["workout_type"], decision["difficulty"],
                     int(profile.get("session_minutes", 45)), profile.get("body_focus", ""),
                     decision["why_today"], rx_id],
                )
            summary_parts.append(f"prescribed today: {decision['workout_type']} (diff {decision['difficulty']}/10)")
        except Exception as e:
            summary_parts.append(f"prescribe skipped: {type(e).__name__}")

    # 2. Backfill yesterday's journal entry only.
    repo_root = _find_repo_root()
    if repo_root:
        script = repo_root / "scripts" / "backfill-journal-body-context.py"
        if script.is_file():
            yesterday = today - timedelta(days=1)
            vroot = os.environ.get("VAULT_ROOT", "")
            if vroot:
                try:
                    proc = subprocess.run(
                        # sys.executable, not /usr/bin/python3: that absolute
                        # path exists only on macOS/Linux.
                        [sys.executable or "python3", str(script),
                         "--start", yesterday.isoformat(), "--end", yesterday.isoformat(),
                         "--vault-root", vroot, "--llm-model", "python"],
                        capture_output=True, text=True, timeout=30, check=False,
                    )
                    if proc.returncode == 0 and "backfilled" in proc.stdout:
                        last_line = [l for l in proc.stdout.splitlines() if "Done" in l]
                        if last_line:
                            summary_parts.append("yesterday backfilled")
                except (subprocess.SubprocessError, OSError):
                    pass

    if summary_parts:
        _emit_context("; ".join(summary_parts))
    else:
        _emit_silent()
    return 0


if __name__ == "__main__":
    sys.exit(main())
