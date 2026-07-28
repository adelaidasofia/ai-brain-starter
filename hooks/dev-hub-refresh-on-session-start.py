#!/usr/bin/env python3
"""SessionStart hook (MYC-1893): surface bare ~/dev hubs that need a human.

CHEAP by construction — reads the state file the launchd `dev-hub-refresh --apply`
job writes (NO git, NO fetch on the interactive path). The launchd leg owns the
expensive fetch + fast-forward; this leg is the human-visible backstop that says
"these hubs are far behind / dirty / off-branch — the auto-ff couldn't touch them".

Stays SILENT unless something genuinely needs attention (surfaced hub, or a hub
>= THRESHOLD commits behind — a sign the launchd refresh has stalled). Fail-open:
a missing / garbled state file prints nothing (a fresh install seeds it at login;
daemon-liveness is surface-stale-automation-failures.py's job, not this hook's).

Bug class: STALE-BARE-CHECKOUT-READ (MYC-670 / MYC-1127 / MYC-1893).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Drift state written by the daily `dev-drift-report.py --write-state` leg. Read
# here rather than recomputed: the scan is ~15s of fleet-wide git I/O, and this
# hook exists precisely to keep the interactive path free of it (MYC-2070 +
# docs/adr/0004-footprint-sla-gate.md — rendering it here adds NO fan-out, while
# a second SessionStart hook would have added a process to every session start).
DRIFT_STATE_PATH = Path(
    os.environ.get("DEV_DRIFT_STATE")
    or (Path.home() / ".claude" / ".dev-drift-state.json")
)
# Past this, the reading describes a fleet that may have moved on. Say so rather
# than presenting it as current — a stale "all clear" is the failure mode.
DRIFT_STALE_HOURS = 36.0

STATE_PATH = Path(
    os.environ.get("DEV_HUB_REFRESH_STATE")
    or (Path.home() / ".claude" / ".dev-hub-refresh-state.json")
)
THRESHOLD = 50  # a hub >= this many commits behind is "loud" even though it is auto-fixable

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _lib.dev_repo_scan import format_hub_surface_line
except Exception:
    print(json.dumps({}))
    sys.exit(0)


def _emit(message: str | None = None) -> None:
    if message:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": message,
            }
        }))
    else:
        print(json.dumps({}))
    sys.exit(0)


def drift_section() -> str:
    """The un-backed-up-drift section from the daily leg's state file, or "".

    Absent state is SILENT, never "clean": nothing has measured this fleet yet
    (fresh install, or the daily pass has not run), and reporting that as a
    clean bill of health is exactly the false-assurance this surface exists to
    prevent. Daemon liveness is surface-stale-automation-failures.py's job.
    """
    try:
        doc = json.loads(DRIFT_STATE_PATH.read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(doc, dict):
        return ""
    section = doc.get("section") or ""
    if not section:
        return ""
    try:
        age_h = (time.time() - float(doc.get("ts", 0))) / 3600
    except (TypeError, ValueError):
        return section
    if age_h > DRIFT_STALE_HOURS:
        return (f"{section}\n\n_(measured {age_h:.0f}h ago — the daily maintenance "
                f"pass has not run since; refresh with "
                f"`python3 scripts/dev-drift-report.py`.)_")
    return section


def main() -> None:
    try:
        json.load(sys.stdin)
    except Exception:
        pass

    # Compute the drift section FIRST and unconditionally. It is a SEPARATE
    # signal with its own state file, and gating it on the hub state below meant
    # a machine with no hub state — every fresh install, and any box where the
    # refresh job has not run yet — never saw an un-backed-up-work warning even
    # when the daily pass had measured one. Two independent verdicts must not
    # share an early return; that is how a surface silently fails to reach a
    # human, which is the whole failure this hook family exists to prevent.
    sections = []
    drift = drift_section()
    if drift:
        sections.append(drift)

    hub = ""
    try:
        data = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        data = None  # no hub state yet (fresh install / job not run) → hub silent
    summary = data.get("summary") if isinstance(data, dict) else None
    if isinstance(summary, dict):
        hub = format_hub_surface_line(summary, threshold=THRESHOLD) or ""
    if hub:
        sections.append(hub)

    _emit("\n\n".join(sections) if sections else None)


if __name__ == "__main__":
    main()
