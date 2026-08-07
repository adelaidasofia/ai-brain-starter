#!/usr/bin/env python3
"""Auto-remediation: reap clearly-orphaned runaway processes — SessionStart.

The session-start hooks SURFACE drift (unpushed commits, stashes, orphan
snapshots) but nothing FIXES it. This is the FIX side of that pair, for two
safe, high-value classes of runaway process.

CLASS 1 — orphaned no-op processes (PPID == 1).
The documented incident (2026-05-29): 16 orphaned `yes` processes — real parent
dead, reparented to launchd/init (PPID 1) — pinned the CPU for ~20h across a
session. Pure waste, zero recoverable output. A process is reaped here only
when it is ALL of:
  * orphaned (PPID == 1): its real parent already died, so nothing is reading
    its output and no shell will ever reap it;
  * an exact match for a known-noop runaway command (default: `yes`), whose
    whole purpose is to emit forever and whose output is never valuable;
  * older than RUNAWAY_MIN_AGE_MIN minutes (default 5) — never touches a
    just-spawned process a live pipe might still be consuming.

CLASS 2 — stuck hook processes (added after the 2026-06-05 freeze).
A SessionStart hook that walks a large corpus piled up several concurrent
multi-minute copies — they had LIVE parents and comm "Python", so the
orphan-`yes` reaper above never saw them, and they pegged the CPU until the
machine froze (load 36). A process running one of OUR OWN hooks
(`~/.claude/hooks/`) for many minutes at high CPU is stuck by construction —
hooks finish in seconds and are idempotent (they re-run next session), so
killing one loses nothing. Reaped here only when it is ALL of:
  * its command line is under `~/.claude/hooks/` (path-scoped — never a user
    program or a busy compiler);
  * it is a python process (the hook runtime);
  * older than RUNAWAY_HOOK_MIN_AGE_MIN minutes (default 12 — safely above any
    self-bounded hook's own budget) AND at/above RUNAWAY_HOOK_MIN_CPU percent
    CPU (default 50). The dual age-AND-cpu gate means a normal fast hook, a
    legitimately-bounded scan, or an idle process is never touched.

It NEVER kills by CPU-share guesswork on arbitrary processes, never touches a
process with a living parent outside the hook path, and never touches anything
off these two narrow classes.

Config:
  RUNAWAY_PROC_NAMES        comma-separated exact comm basenames (default "yes")
  RUNAWAY_MIN_AGE_MIN       min elapsed minutes before reaping class 1 (default 5)
  RUNAWAY_HOOK_MIN_AGE_MIN  min elapsed minutes before reaping a stuck hook (default 12)
  RUNAWAY_HOOK_MIN_CPU      min %CPU before reaping a stuck hook (default 50)
Bypass: RUNAWAY_REMEDIATE_BYPASS=1

WIRING (SessionStart):
  "SessionStart": [
    {"hooks": [{
      "type": "command",
      "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/remediate-runaway-procs.py 2>/dev/null || echo '{\"continue\":true,\"suppressOutput\":true}'"
    }]}
  ]
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys


def _emit(ctx: str | None) -> int:
    print(
        json.dumps({"continue": True, "additionalContext": ctx})
        if ctx
        else json.dumps({"continue": True, "suppressOutput": True})
    )
    return 0


def _etime_to_min(etime: str) -> float:
    """Parse `ps` etime ([[dd-]hh:]mm:ss) into minutes."""
    etime = etime.strip()
    days = 0
    if "-" in etime:
        d, etime = etime.split("-", 1)
        days = int(d)
    parts = [int(p) for p in etime.split(":")]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    else:
        h, m, s = 0, 0, parts[0]
    return days * 1440 + h * 60 + m + s / 60.0


def _should_reap_hook(
    command: str,
    age_min: float,
    cpu: float,
    *,
    hooks_dir: str,
    pid: int,
    self_pid: int,
    min_age: float,
    min_cpu: float,
) -> bool:
    """Pure decision for CLASS 2 (stuck hook process). Returns True only when a
    process is a stuck `~/.claude/hooks/` python process per the dual age-AND-cpu
    gate. Factored out so the pos/neg control test can exercise every branch
    deterministically without spawning or killing real processes.

    A process is reaped ONLY when it is ALL of:
      * not this very hook (pid != self_pid);
      * its command line is under hooks_dir (path-scoped);
      * a python process;
      * age_min >= min_age AND cpu >= min_cpu (a fast hook OR a low-CPU/idle
        process is never reaped — the AND is what makes this safe).
    """
    if pid == self_pid:
        return False
    if hooks_dir not in command:
        return False
    if "python" not in command.lower():
        return False
    if age_min < min_age:
        return False
    if cpu < min_cpu:
        return False
    return True


def main() -> int:
    if os.environ.get("RUNAWAY_REMEDIATE_BYPASS") == "1":
        return _emit(None)

    # POSIX-only mechanism (`ps` + SIGKILL). On Windows there is nothing safe
    # to reap this way — exit silently rather than error on every session.
    if os.name != "posix":
        return _emit(None)

    names = {
        n.strip()
        for n in os.environ.get("RUNAWAY_PROC_NAMES", "yes").split(",")
        if n.strip()
    }
    try:
        min_age = float(os.environ.get("RUNAWAY_MIN_AGE_MIN", "5"))
    except ValueError:
        min_age = 5.0

    # --- CLASS 1: orphaned no-op runaway processes (PPID == 1) ---------------
    reaped: list[tuple[int, str, float]] = []
    if names:
        try:
            out = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,etime=,comm="],
                capture_output=True, text=True, timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError):
            out = None
        if out is not None and out.returncode == 0:
            for line in out.stdout.splitlines():
                parts = line.split(None, 3)
                if len(parts) < 4:
                    continue
                pid_s, ppid_s, etime, comm = parts
                try:
                    pid, ppid = int(pid_s), int(ppid_s)
                except ValueError:
                    continue
                if ppid != 1 or os.path.basename(comm) not in names:
                    continue
                try:
                    age = _etime_to_min(etime)
                except (ValueError, IndexError):
                    continue
                if age < min_age:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                    reaped.append((pid, os.path.basename(comm), age))
                except (ProcessLookupError, PermissionError):
                    continue

    # --- CLASS 2: runaway HOOK processes (live parent, comm "Python") --------
    # The class that froze the machine 2026-06-05 — a SessionStart hook piling
    # up concurrent multi-minute copies. The orphan-`yes` reaper above never
    # saw them (live parent, comm "Python"). Path-scoped to ~/.claude/hooks/
    # with a dual age AND cpu gate so a normal fast hook is never touched.
    hooks_dir = os.path.expanduser("~/.claude/hooks/")
    try:
        hook_age = float(os.environ.get("RUNAWAY_HOOK_MIN_AGE_MIN", "12"))
    except ValueError:
        hook_age = 12.0
    try:
        hook_cpu = float(os.environ.get("RUNAWAY_HOOK_MIN_CPU", "50"))
    except ValueError:
        hook_cpu = 50.0
    hook_reaped: list[tuple[int, str, float]] = []
    try:
        out2 = subprocess.run(
            ["ps", "-axww", "-o", "pid=,etime=,pcpu=,command="],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        out2 = None
    if out2 is not None and out2.returncode == 0:
        self_pid = os.getpid()
        for line in out2.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            pid_s, etime, cpu_s, command = parts
            try:
                pid, cpu = int(pid_s), float(cpu_s)
            except ValueError:
                continue
            try:
                age = _etime_to_min(etime)
            except (ValueError, IndexError):
                continue
            if not _should_reap_hook(
                command, age, cpu,
                hooks_dir=hooks_dir, pid=pid, self_pid=self_pid,
                min_age=hook_age, min_cpu=hook_cpu,
            ):
                continue
            script = next((t for t in command.split() if t.endswith(".py")), command)
            try:
                os.kill(pid, signal.SIGKILL)
                hook_reaped.append((pid, os.path.basename(script), age))
            except (ProcessLookupError, PermissionError):
                continue

    if not reaped and not hook_reaped:
        return _emit(None)

    msgs: list[str] = []
    if reaped:
        by_name: dict[str, int] = {}
        for _, c, _a in reaped:
            by_name[c] = by_name.get(c, 0) + 1
        summary = ", ".join(f"{c}×{n}" for c, n in sorted(by_name.items()))
        oldest_h = max(r[2] for r in reaped) / 60.0
        msgs.append(
            f"Reaped {len(reaped)} orphaned runaway process(es) "
            f"({summary}; oldest {oldest_h:.1f}h, all PPID=1 — pure CPU waste)."
        )
    if hook_reaped:
        by_script: dict[str, int] = {}
        for _, s, _a in hook_reaped:
            by_script[s] = by_script.get(s, 0) + 1
        hsum = ", ".join(f"{s}×{n}" for s, n in sorted(by_script.items()))
        oldest_m = max(r[2] for r in hook_reaped)
        msgs.append(
            f"Reaped {len(hook_reaped)} stuck hook process(es) "
            f"({hsum}; oldest {oldest_m:.0f}min at >={hook_cpu:.0f}% CPU — runaway "
            f"~/.claude/hooks process, idempotent so it re-runs next session). "
            f"This is the SessionStart pile-up class that can freeze a machine."
        )
    return _emit(
        "[auto-remediate] " + " ".join(msgs) +
        " Tune RUNAWAY_PROC_NAMES / RUNAWAY_MIN_AGE_MIN / RUNAWAY_HOOK_MIN_AGE_MIN /"
        " RUNAWAY_HOOK_MIN_CPU; bypass with RUNAWAY_REMEDIATE_BYPASS=1."
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print(json.dumps({"continue": True, "suppressOutput": True}))
        sys.exit(0)
