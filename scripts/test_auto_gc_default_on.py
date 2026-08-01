#!/usr/bin/env python3
"""Controls for default-ON auto-GC (MYC-2363).

Every reclaim tool the substrate ships was OPT-IN: `install-vault-daily-
maintenance.sh` was documented and never invoked, so on a default install
NOTHING ever pruned a worktree, a cache, or a merged branch, and the machine
only ever got fuller. "Ships a GC" and "the GC runs" are different claims, and
only the second one matters — so these controls assert ACTIVATION and COVERAGE,
never file presence:

  * the hook installer CALLS the scheduler (not: the scheduler exists)
  * the daily pass covers all five reclaim legs
  * ABS_NO_AUTO_GC=1 turns it OFF, and nothing else does — no opt-in gates the
    value, because an opt-in default IS the bug
  * the run is gated on battery and on someone being at the keyboard, not just
    on loadavg
  * a non-macOS install still gets a schedule (cron), else the whole pass is
    silently macOS-only while looking installed

Run: python3 scripts/test_auto_gc_default_on.py
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAINT = REPO / "scripts" / "vault-daily-maintenance.sh"
SCHED = REPO / "scripts" / "install-vault-daily-maintenance.sh"
HOOK_INSTALLER = REPO / "scripts" / "install-hooks-user-level.py"

# The reclaim legs the daily pass must drive. Each existed and each was opt-in.
REQUIRED_LEGS = [
    ("worktree-prune.sh", "vault scratch worktrees + orphan dirs/branches"),
    ("graphify_prune_stale_cache.py", "stale graph-cache entries"),
    ("dev-repo-reaper.py", "merged branches / worktrees / checkpoint stashes"),
    ("dev-worktree-prune.py", "orphaned <dev-root>/<repo>-<slug> worktrees"),
    ("dev-hub-refresh.py", "bare-hub freshness (stale-checkout reads)"),
    ("dev-drift-report.py", "un-backed-up drift -> state the SessionStart surface renders"),
]


def main() -> int:
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    maint = MAINT.read_text(encoding="utf-8")
    sched = SCHED.read_text(encoding="utf-8")
    inst = HOOK_INSTALLER.read_text(encoding="utf-8")

    # --- ACTIVATION: the installer actually calls the scheduler --------------
    check("install_auto_gc" in inst and re.search(r"^\s*install_auto_gc\(", inst, re.M),
          "install-hooks-user-level.py does not CALL install_auto_gc — the GC "
          "would ship as a file and never be scheduled (ARTIFACT-WITHOUT-ACTIVATION)")
    check("install-vault-daily-maintenance.sh" in inst,
          "the installer does not reference the scheduler script at all")

    # --- COVERAGE: every reclaim leg is driven by the daily pass -------------
    for leg, what in REQUIRED_LEGS:
        check(leg in maint,
              f"the daily pass does not run {leg} ({what}) — that reclaim tier "
              f"stays opt-in, so it never runs on a default install")

    # --- the reaper legs must APPLY, not dry-run ----------------------------
    # A GC pass that only ever plans is a GC pass that reclaims nothing, and it
    # looks healthy in the log either way.
    # Non-destructive legs apply unconditionally: a fast-forward destroys
    # nothing, so there is no reason to stage it.
    line = next((ln for ln in maint.splitlines() if 'run "dev-hub-refresh"' in ln), "")
    check("--apply" in line,
          "dev-hub-refresh is invoked without --apply — it would plan forever "
          "and refresh nothing, and a fast-forward has nothing to stage")

    # The two DESTRUCTIVE legs are staged: report on run 1, apply from run 2.
    # Deleting someone's git branches on day one, before they have seen what
    # this pass does, is a trust event even when every deletion is provably
    # safe and reversible.
    for label in ("dev-repo-reaper", "dev-worktree-prune"):
        line = next((ln for ln in maint.splitlines()
                     if f'run "{label}"' in ln), "")
        check(line, f"no `run \"{label}\"` invocation in the daily pass")
        check("$DESTRUCTIVE_MODE" in line,
              f"{label} is hardwired instead of using $DESTRUCTIVE_MODE — it "
              f"would delete on a client's very first run")
        check("--apply" not in line,
              f"{label} still carries a literal --apply")

    # ...and the staging must ESCALATE, not stall forever: a marker banked after
    # the first pass is what turns run 2 into an applying run. Without it the
    # reclaim never happens and the machine still only gets fuller.
    check("DESTRUCTIVE_MODE=\"--apply\"" in maint,
          "no path sets DESTRUCTIVE_MODE to --apply — reclaim would never apply")
    check("GC_SEEN_MARKER" in maint and "ABS_GC_APPLY_NOW" in maint,
          "no first-report marker / no immediate-apply override")
    marker_write = next((i for i, ln in enumerate(maint.splitlines())
                         if "GC_SEEN_MARKER" in ln and "date -u" in ln), -1)
    first_leg = next((i for i, ln in enumerate(maint.splitlines())
                      if 'run "dev-repo-reaper"' in ln), -1)
    check(marker_write > first_leg > 0,
          "the first-report marker is banked BEFORE the legs run — a crashed "
          "first pass would then escalate to delete on a run nobody saw")

    # --- OPT-OUT ONLY: no opt-in may gate the value -------------------------
    check("ABS_NO_AUTO_GC" in maint and "ABS_NO_AUTO_GC" in sched,
          "no documented opt-out — a power user must be able to turn GC off")
    for src, name in ((maint, "vault-daily-maintenance.sh"), (sched, "install-vault-daily-maintenance.sh")):
        for m in re.finditer(r'"\$\{(ABS_[A-Z_]*GC[A-Z_]*)(?::-([^}]*))?\}"?\s*(?:=|==)\s*"?1"?', src):
            var, default = m.group(1), m.group(2)
            check(default in ("0", ""),
                  f"{name}: {var} defaults to {default!r} — if the GC only runs "
                  f"when a flag is SET, the default install still gets no GC")

    # --- IDLE-AWARE: more than loadavg --------------------------------------
    check("on_battery" in maint,
          "no battery gate — a GC pass would burn a laptop's remaining charge")
    check("user_idle_seconds" in maint or "HIDIdleTime" in maint,
          "no active-user gate — GC churn landing mid-task is exactly the "
          "'the install made my machine slower' experience")
    check("close_resource_high" in maint, "the load gate was dropped")

    # --- the gates DEFER, they do not skip forever --------------------------
    for gate in ("on battery", "at the keyboard"):
        line = next((ln for ln in maint.splitlines() if gate in ln), "")
        check("DEFERRED" in line and "catch" in line.lower(),
              f"the '{gate}' gate does not announce itself as a DEFERRAL with a "
              f"catch-up — a permanent silent skip reads the same as 'ran clean'")

    # --- non-macOS still gets a schedule ------------------------------------
    check("crontab" in sched,
          "no cron path — on Linux the whole daily pass would be unscheduled "
          "while the install reports success")
    check(re.search(r"grep -vF .*MARKER", sched) is not None,
          "the cron install is not idempotent — re-running would stack duplicate "
          "entries and run the GC N times a night")

    # --- the scheduler honors the opt-out for real (behavior, not grep) -----
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run(["/bin/bash", str(SCHED), td, "--quiet"],
                           # encoding pinned: text=True alone decodes with the
                           # console code page, and every vault path carries the
                           # gear emoji whose 0x8F byte is unmapped in cp1252.
                           capture_output=True, text=True, encoding="utf-8",
                           # USERPROFILE too: Windows resolves "~" from it and
                           # ignores HOME, so HOME alone leaves the child on the
                           # real profile (MYC-3536).
                           env={"PATH": "/usr/bin:/bin", "HOME": td,
                                "USERPROFILE": td,
                                "ABS_NO_AUTO_GC": "1"}, timeout=60)
        check(r.returncode == 0,
              f"the opt-out path exited {r.returncode}: {r.stderr[-300:]}")
        check(not (Path(td) / "Library" / "LaunchAgents").exists(),
              "ABS_NO_AUTO_GC=1 still installed a launch agent — the opt-out "
              "does not actually opt out")

    # --- the installer must NOT schedule for a temp/fixture vault -----------
    # Scheduling is a HOST side effect. Every integration test that runs this
    # installer builds its vault in $TMPDIR, so without this guard the test
    # suite itself writes a real launchd agent / crontab entry on the machine
    # running it, pointing at a directory that is about to be deleted.
    import os
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run(
            [sys.executable, str(HOOK_INSTALLER), "--vault-path", td, "--quiet",
             "--settings", str(Path(td) / "settings.json")],
            capture_output=True, text=True, encoding="utf-8",
            # USERPROFILE too — see above (MYC-3536).
            env=dict(os.environ, HOME=td, USERPROFILE=td), timeout=120)
        agents = Path(td) / "Library" / "LaunchAgents"
        check(not agents.exists() or not list(agents.glob("com.abs.*")),
              "installing with a $TMPDIR vault scheduled a launchd agent — the "
              "test suite would mutate the host's scheduler")

    if failures:
        print("FAILED — default-ON auto-GC (MYC-2363):")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("OK - auto-GC: installer activates the schedule, all 5 reclaim legs "
          "covered and applying, opt-out only, battery + user-idle + load gated "
          "with catch-up, cron path for non-macOS, opt-out verified by behavior")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): the failure output carries non-ASCII
    # (em dashes, the check glyph). An unguarded print raises UnicodeEncodeError
    # there, and a test whose FAILURE output cannot print reads as a pass.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
