#!/usr/bin/env python3
"""
Granola -> Obsidian vault transcript export (official Public API).

Pulls meeting notes from Granola's official Public API and writes each meeting's
full timestamped transcript + AI summary as markdown into your vault's meeting
notes folder.

The API contract (auth, HTTP, paging, transcript formatting, the vault filename
contract, the incremental window, state) lives in scripts/granola_core.py -- the
SINGLE source of truth shared with the private vault's granola_export.py so the
two copies cannot drift. This file is the substrate's zero-config CLI on top.

WHY THE API (history): Granola migrated local storage to an encrypted SQLite DB
around 2026-05-12. The previous version of this script parsed
cache-v6.json["cache"]["state"]["transcripts"], which became an empty {} after
the migration -> it printed "No transcripts in cache." and exited 0 for weeks
(a silent failure: exit 0, zero output, nothing wrong-looking). The Public API
is vendor-supported and survives local-storage migrations.
Bug class: VENDOR-LOCAL-CACHE-SCHEMA-MIGRATION-SILENT-FAILURE.

AUTH: a Granola API key, resolved in order:
  1. GRANOLA_API_KEY environment variable
  2. --key-file PATH (a file with the bare key, or a `GRANOLA_API_KEY=...` line)
  3. ~/.config/granola/api-key (same format)
Generate the key in Granola: Settings > Connectors > API keys (requires a plan
with API access). The key is read at runtime and is never written to the repo.

USAGE:
  python3 granola_sync.py                 # export new transcripts since last run
  python3 granola_sync.py --dry-run       # show what would be exported; write nothing
  python3 granola_sync.py --since 2026-05-01   # override the created_after window
  python3 granola_sync.py --health        # verify key + API connectivity; exit 0/1
  python3 granola_sync.py --vault-root /path/to/vault
  python3 granola_sync.py --meeting-dir "Meeting Notes"

EXIT CODES: 0 ok (including "0 new" -- a legitimate quiet period); 1 hard failure
(missing/invalid key, API unreachable). A run that succeeds but returns 0 notes
prints that plainly -- sustained silence is caught by check-connector-liveness.py
(the 0-vs-0 watchdog), not by failing an individual run.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Shared Granola API core (same directory). Re-export the contract functions so
# `import granola_sync as g; g.load_api_key/safe_filename/format_transcript`
# keeps working for callers and tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from granola_core import (  # noqa: E402
    api_get,
    detect_meeting_dir,
    detect_vault_root,
    format_transcript,
    incremental_created_after,
    list_notes,
    load_api_key,
    load_state,
    safe_filename,
    save_state,
    state_path_for,
    write_transcript_md,
)


def run_health(key: str) -> int:
    try:
        data = api_get("/notes", key)
        n = len(data.get("notes", []))
        print(f"HEALTH OK: API reachable, key valid, {n} note(s) in first page.")
        return 0
    except SystemExit as e:
        print(f"HEALTH FAIL: {e}")
        return 1


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Export Granola meeting transcripts to an Obsidian vault via the Public API"
    )
    ap.add_argument("--vault-root", type=Path)
    ap.add_argument("--meeting-dir", help="Meeting notes folder (relative to vault root)")
    ap.add_argument("--key-file", type=Path, help="File containing the Granola API key")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--since", help="created_after override (YYYY-MM-DD or ISO)")
    ap.add_argument("--health", action="store_true")
    args = ap.parse_args()

    key = load_api_key(args.key_file)
    if args.health:
        sys.exit(run_health(key))

    vault_root = (args.vault_root or detect_vault_root()).resolve()
    meeting_dir = detect_meeting_dir(vault_root, args.meeting_dir)
    state_file = state_path_for(vault_root)
    state = load_state(state_file)

    created_after = incremental_created_after(state, args.since)

    print(f"Granola export | created_after={created_after} | dry_run={args.dry_run}")
    notes = list_notes(key, created_after)
    print(f"API returned {len(notes)} note(s) in window.")
    if not notes:
        print("  0 notes in this window. If meetings should appear, run `--health` to")
        print("  verify the key/connection. Sustained silence is flagged by")
        print("  scripts/check-connector-liveness.py (the 0-vs-0 watchdog).")

    exported = set(state.get("exported", []))
    run_started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n_exported = 0

    for meta in notes:
        nid = meta.get("id")
        if not nid or nid in exported:
            continue
        note = api_get(f"/notes/{nid}?include=transcript", key)
        fp, msg = write_transcript_md(note, meeting_dir, args.dry_run)
        print(f"  [{nid[:12]}] {msg}")
        if fp and not args.dry_run:
            exported.add(nid)
            n_exported += 1
        elif fp and args.dry_run:
            n_exported += 1
        time.sleep(0.25)

    if not args.dry_run:
        state["exported"] = sorted(exported)
        state["last_sync"] = run_started
        save_state(state_file, state)

    print(
        f"Done. {n_exported} exported."
        + ("  [DRY-RUN, nothing written]" if args.dry_run else "")
    )


if __name__ == "__main__":
    # This module's own source is pure ASCII, but the values it prints are not:
    # detect_vault_root() matches on the emoji-prefixed Meta folder,
    # detect_meeting_dir() globs the emoji-prefixed meeting-notes folder, and the
    # per-note line prints filepath.name built from an arbitrary meeting title
    # (accented names, em dashes). On a Windows cp1252 console each of those
    # raises UnicodeEncodeError mid-run (ai-brain-starter#313). Force UTF-8 on
    # the CLI streams; a no-op on an already-UTF-8 console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
