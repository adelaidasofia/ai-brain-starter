#!/usr/bin/env python3
"""SessionStart: surface the sync-folder guard's LAST verdict.

WHY THIS EXISTS: detection was never the gap — ROUTING was. In the field the
guard ran daily for three months and correctly reported a dozen git repos
churning inside a cloud-synced folder on every single run. Its only sink was a
log file, so nothing changed until a human happened to run it by hand. A guard
whose findings land somewhere nobody opens is indistinguishable from no guard.
Bug class GUARD-REPORTS-INTO-A-LOG-NOBODY-READS.

BOUNDED BY CONSTRUCTION: this reads one small JSON snapshot written by the
scheduled scan. It must NEVER re-walk the cloud roots. Per-session work that
scales with user data becomes N concurrent full walks under N sessions, which
is the very resource storm the guard exists to prevent.

Also reports STALENESS: a snapshot that stopped updating means the scheduled job
died, and a dead guard and a clean one otherwise emit identical silence.

Advisory only, always exits 0. Bypass: SYNC_GUARD_SURFACE_BYPASS=1
"""
import json
import os
import sys
import time

STATE_PATH = os.path.join(os.path.expanduser("~"), ".claude", "state",
                          "sync-guard-last.json")
STALE_AFTER_H = 48          # daily job -> >2 missed runs means it stopped
MAX_SHOWN = 8


def build_report(snap, now=None):
    """Return the report lines for a snapshot, or [] when genuinely clean.

    Split out from main() so the self-test can drive every branch directly
    without needing to plant a file on disk.
    """
    now = time.time() if now is None else now
    age_h = (now - snap.get("scanned_at", 0)) / 3600.0
    high = [f for f in snap.get("findings", []) if f.get("severity") == "high"]
    partial = snap.get("truncated_roots") or []

    if not high and not partial and age_h <= STALE_AFTER_H:
        return []                                   # quiet when genuinely clean

    out = ["[sync-guard]"]
    if high:
        out.append(
            f"{len(high)} machinery director{'y' if len(high) == 1 else 'ies'} "
            f"still inside a cloud-synced folder — the sync-daemon storm + "
            f"silent-corruption class."
        )
        for f in high[:MAX_SHOWN]:
            out.append(f"  • [{f.get('provider')}] {f.get('path')}")
        if len(high) > MAX_SHOWN:
            out.append(f"  … and {len(high) - MAX_SHOWN} more")
        out.append("  FIX: move it to a home-root path (~/dev, ~/Repos — never a "
                   "synced folder), keep git remote + snapshot as the backup.")
        out.append("  Check for an identical twin FIRST — a repo already copied "
                   "outside the synced folder makes this one redundant churn.")
    if partial:
        out.append(f"  ⚠ {len(partial)} root(s) timed out mid-walk and were NOT "
                   f"fully checked — 'no findings' there is unproven, not clean:")
        for p in partial[:MAX_SHOWN]:
            out.append(f"      {p}")
    if age_h > STALE_AFTER_H:
        out.append(f"  ⚠ last scan was {age_h/24:.1f}d ago (daily job) — the guard "
                   f"itself may have stopped.")
    out.append("  Re-scan now: python3 "
               "~/.claude/skills/ai-brain-starter/hooks/check-sync-folder-machinery.py")
    return out


def self_test():
    """Negative controls: prove each branch FIRES, and that clean stays silent."""
    now = 1_000_000.0
    fresh = now - 3600                      # 1h old
    stale = now - (100 * 3600)              # 100h old -> past STALE_AFTER_H

    high_snap = {"scanned_at": fresh, "truncated_roots": [],
                 "findings": [{"severity": "high", "provider": "iCloud",
                               "path": "/x/repo/.git"}]}
    partial_snap = {"scanned_at": fresh, "truncated_roots": ["/x/big"],
                    "findings": []}
    stale_snap = {"scanned_at": stale, "truncated_roots": [], "findings": []}
    clean_snap = {"scanned_at": fresh, "truncated_roots": [], "findings": []}
    # A finding that is NOT high must not be reported as machinery.
    info_snap = {"scanned_at": fresh, "truncated_roots": [],
                 "findings": [{"severity": "info", "provider": "iCloud",
                               "path": "/x/note"}]}

    got_high = any("/x/repo/.git" in ln for ln in build_report(high_snap, now))
    got_partial = any("/x/big" in ln for ln in build_report(partial_snap, now))
    got_stale = any("may have stopped" in ln for ln in build_report(stale_snap, now))
    clean_silent = build_report(clean_snap, now) == []
    info_silent = build_report(info_snap, now) == []

    print(f"[self-test] high finding surfaces:            {got_high}")
    print(f"[self-test] truncated root warns PARTIAL:     {got_partial}")
    print(f"[self-test] stale snapshot warns job died:    {got_stale}")
    print(f"[self-test] clean + fresh stays silent:       {clean_silent}")
    print(f"[self-test] info-only finding stays silent:   {info_silent}")
    ok = got_high and got_partial and got_stale and clean_silent and info_silent
    print("[self-test] PASS" if ok else "[self-test] FAIL")
    return 0 if ok else 1


def main():
    if "--self-test" in sys.argv:
        return self_test()
    if os.environ.get("SYNC_GUARD_SURFACE_BYPASS") == "1":
        return 0
    try:
        with open(STATE_PATH) as fh:
            snap = json.load(fh)
    except FileNotFoundError:
        # Never ran, or ran only on a version predating snapshots. Silent:
        # nagging about a file that has never existed trains people to ignore.
        return 0
    except Exception:
        return 0

    lines = build_report(snap)
    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (ai-brain-starter#313; hooks/ sweep #314).
    # This hook prints non-ASCII (• and ⚠); without the reconfigure it raises
    # UnicodeEncodeError on a cp1252 console and the surfacer dies silently.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)          # fail-open: a surfacer must never block a session
