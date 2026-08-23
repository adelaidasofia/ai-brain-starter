#!/usr/bin/env python3
"""SessionStart hook: surface automation runners that have been failing silently.

The 191-file strand of 2026-05-14 happened because auto-snapshot.sh had been
failing every hour for 48+ hours without anyone noticing. The fix has two
layers:

  1. The script itself unstages on failure (auto-snapshot.sh v4, 2026-05-14).
  2. THIS hook surfaces persistent failures at the next session start so
     they can't go undetected for weeks again.

Scans known runner logs for FAIL/ERROR entries in the last 72 hours. If any
runner shows >= threshold failures in window, emit a SystemMessage at session
start. Stays silent when nothing is wrong.

Bypass: SURFACE_STALE_AUTOMATION_BYPASS=1 in env.

Codified 2026-05-14 as the meta-fix for the stranded-files class.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _lib.vault_root import vault_root_for  # noqa: E402
except Exception:  # fail-open: a SessionStart nudge must never break startup
    def vault_root_for(target: Path):  # type: ignore
        return None


def runners(vault: Path | None) -> list[tuple]:
    """Known runner logs to scan: (label, log path, fail-pattern, threshold, hint).

    MYC-3529 — the vault-rooted entries take the vault resolved for THIS
    session's cwd, not a module-level
    `os.environ.get("VAULT_ROOT", str(Path.home() / "vault"))`. Both branches of
    that read were wrong for the job. UNSET, the two vault logs resolved under
    `~/vault/⚙️ Meta/`, which does not exist on any vault not literally named
    "vault", so `fail_count_in_window` short-circuited on `log_path.exists()`
    and returned 0 — this hook reported "nothing wrong" while auto-snapshot
    failed hourly, which is precisely the 2026-05-14 191-file strand it was
    written to make impossible. SET, it watched ONE vault's runners and stayed
    blind to a second vault's.

    `vault is None` (no vault detectable, no $VAULT_ROOT) drops the vault-rooted
    entries entirely; the ~/.claude-rooted runners are machine-global and are
    always scanned.
    """
    machine_global = [
        (
            "hookify-auto-commit",
            HOME / ".claude" / "hooks" / "hookify-auto-commit.log",
            re.compile(r"failed|error", re.IGNORECASE),
            5,
            "on-edit auto-commit of .claude/hookify.*.local.md files",
        ),
        (
            "scrub-session-jsonl",
            HOME / ".claude" / "hooks" / "scrub-log.jsonl",
            re.compile(r'"error"'),
            3,
            "SessionEnd secret-pattern redaction over the closing session's JSONL",
        ),
        # team-broadcast-daily moved to team_broadcast_findings() below: a daily
        # job needs last-run-outcome detection, not a 3-in-72h count (2026-05-22).
        (
            "substack-cookie-refresh",
            HOME / ".claude" / "substack-mcp" / "refresh.log",
            re.compile(r"ERROR"),
            3,
            "12-hourly Substack session-cookie refresh; a logged-out browser or "
            "a flaky cookie-store backend (the comet keychain-decryption class) "
            "shows here. Fix: switch the pub's `browser` field in config.json",
        ),
    ]
    if vault is None:
        return machine_global
    return [
        (
            "auto-snapshot",
            vault / "⚙️ Meta" / ".auto-snapshot.log",
            re.compile(r"FAIL|errored"),
            3,
            "hourly vault commit; mirror-drift between CLAUDE.md and AGENTS.md "
            "is the common blocker",
        ),
        (
            "vault-safe-commit",
            vault / "⚙️ Meta" / ".vault-snapshot.log",
            re.compile(r"FAIL"),
            3,
            "the wrapper Claude uses for explicit vault commits",
        ),
    ] + machine_global

WINDOW_SECONDS = 72 * 3600  # 72-hour rolling window
# Optional leading bracket: auto-snapshot.log uses "[2026-...]", refresh.log
# uses bare "2026-...". Match both so bracket-less logs aren't silently skipped.
TS_PATTERN = re.compile(r"\[?(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def fail_count_in_window(
    log_path: Path,
    pattern: re.Pattern[str],
    window_seconds: int,
) -> int:
    """Return the number of distinct failure-minutes within the rolling window.

    Reads the whole file (these logs are small — auto-snapshot.log is ~2K
    lines after months of hourly runs). Lines without a parseable timestamp
    are NOT counted toward the window — we only flag time-localized
    failures, not historical noise.

    Counts distinct YYYY-MM-DDTHH:MM keys, not raw lines: one bad run logs
    the same failure across several lines (and some runners double-log every
    line), so a line count turns a single incident into a false "persistent"
    signal. Distinct failure-minutes ≈ distinct incidents.
    """
    if not log_path.exists():
        return 0
    cutoff_unix = time.time() - window_seconds

    # mtime pre-filter: if the file hasn't been written in window_seconds,
    # nothing recent is in it.
    try:
        if log_path.stat().st_mtime < cutoff_unix:
            return 0
    except OSError:
        return 0

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0

    fail_minutes: set[str] = set()
    for line in text.splitlines():
        ts_match = TS_PATTERN.search(line)
        if not ts_match:
            continue
        ts_str = ts_match.group(1)
        try:
            ts = datetime.datetime.fromisoformat(ts_str).timestamp()
        except ValueError:
            continue
        if ts < cutoff_unix:
            continue
        if pattern.search(line):
            fail_minutes.add(ts_str[:16])  # YYYY-MM-DDTHH:MM
    return len(fail_minutes)


def user_launchd_labels() -> set[str]:
    """Labels of the launchd agents THIS user installed.

    Scoped BY PROPERTY, not by a hardcoded namespace. A plist in the user's
    own LaunchAgents directory is, by definition, a job they installed; its
    filename stem is the label by the platform's own convention. That covers
    every operator on every machine, which a hardcoded reverse-DNS prefix
    could never do — a prefix naming one person's namespace makes this pass
    permanently DEAD for everyone else, and this file ships in a public repo
    that other people install.

    Empty set off macOS (no such directory), which makes the caller a no-op
    exactly as it already is where `launchctl` does not exist.
    """
    agents = HOME / "Library" / "LaunchAgents"
    try:
        return {p.stem for p in agents.glob("*.plist")}
    except OSError:
        return set()


def _launchd_print_liveness(label: str, uid: int, timeout: float = 3.0) -> tuple[int, str] | None:
    """Probe `launchctl print gui/<uid>/<label>` for the job's run count.

    Disambiguates the case `launchd_failures()` cannot resolve from
    `launchctl list` alone: last-exit-status 0 means both "ran and exited
    clean" and "has never run at all" (see that function's docstring).
    `launchctl print`'s `runs` field tells the two apart — 0 means the job
    has never executed since it was loaded.

    Returns `(runs, last_exit_text)` on a clean parse. Returns None on ANY
    failure — binary missing, non-zero return, timeout, or output that does
    not contain a parseable `runs = N` line — so the caller can degrade to
    the pre-probe reading (silence) instead of raising. This hook runs at
    every session start and must never crash or hang on a probe that a given
    macOS version, sandbox, or permission set does not support.

    `launchctl print` is slower per-label than the one-shot `list` table, so
    callers should only reach this for labels already narrowed down to
    "mine, loaded, ambiguous" — see MAX_LAUNCHD_PRINT_PROBES.
    """
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    runs_match = re.search(r"^\s*runs\s*=\s*(\d+)\s*$", result.stdout, re.MULTILINE)
    if not runs_match:
        return None
    exit_match = re.search(r"^\s*last exit code\s*=\s*(.+?)\s*$", result.stdout, re.MULTILINE)
    last_exit = exit_match.group(1) if exit_match else ""
    return int(runs_match.group(1)), last_exit


def launchd_failures() -> list[str]:
    """Flag the user's own launchd jobs that are failing OR that are HOLLOW.

    `launchctl list` column 2 is the last exit status: 0 = clean, a positive
    int = non-zero exit, a negative int = killed by signal, '-' in the PID
    column = not currently running (irrelevant to this check). Measured
    2026-08-20: a job launchd has REGISTERED but has NEVER EXECUTED also
    reads 0 in this column — byte-identical to a genuinely healthy job. That
    blind spot let a dead job sit unnoticed for days with real downstream
    damage — this function used to treat status 0 as simply "clean", which
    is exactly the misread.

    So status 0 is now AMBIGUOUS, not clean, and is disambiguated by a
    second, slower probe: `_launchd_print_liveness()`, whose `runs` field is
    0 only when the job has truly never run. That is reported as a DISTINCT
    "hollow" finding, because the remedy differs — a hollow job needs
    `bootout` + `bootstrap` (reload it), a failing job needs its error log
    read. A non-zero status is unambiguous already (you cannot have a real
    exit code without having executed) and is flagged directly, exactly as
    before, with no extra probe.

    The print probe is bounded three ways so this stays fast at every
    session start: (1) only reached for labels the cheap `list` pass already
    narrowed to "mine, loaded, status==0"; (2) capped at
    MAX_LAUNCHD_PRINT_PROBES total calls per run; (3) each call carries its
    own short timeout. Any failure of the probe itself — `launchctl print`
    unavailable, non-zero exit, timeout, unparseable output, no UID — silently
    degrades that label to the pre-probe reading (not flagged), never raises.

    This auto-covers every job — including ones not in RUNNERS, the gap that
    let a health-check and a reconcile job fail unnoticed. Restricted to
    labels the user installed (see user_launchd_labels), so the machine's
    Apple and third-party agents stay out of the report.
    """
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    mine = user_launchd_labels()
    if not mine:
        return []
    try:
        uid = os.getuid()  # type: ignore[attr-defined]
    except AttributeError:
        uid = None  # no os.getuid on Windows — the print probe degrades below
    out: list[str] = []
    probes_used = 0
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        _pid, status, label = parts
        if label not in mine:
            continue
        if any(label.endswith(suffix) for suffix in BESPOKE_LAUNCHD_SUFFIXES):
            continue  # has a dedicated finder with recovery guidance
        try:
            code = int(status)
        except ValueError:
            continue  # not a documented `launchctl list` status column value
        if code != 0:
            out.append(
                f"  - {label}: last run exited {code} (launchctl status) — "
                f"check the job's log / plist"
            )
            continue
        # code == 0 is ambiguous (see docstring above) — disambiguate via the
        # slower print probe, bounded so a large fleet can't slow down every
        # session start.
        if uid is None or probes_used >= MAX_LAUNCHD_PRINT_PROBES:
            continue
        probes_used += 1
        liveness = _launchd_print_liveness(label, uid)
        if liveness is None:
            continue  # probe unavailable/erroring — degrade to pre-probe silence
        runs, last_exit = liveness
        if runs == 0:
            plist_path = HOME / "Library" / "LaunchAgents" / f"{label}.plist"
            exit_note = f", last exit code {last_exit}" if last_exit else ""
            out.append(
                f"  - {label}: loaded but has NEVER RUN (runs=0{exit_note}, "
                f"launchctl print) — a hollow job, not a healthy one; "
                f"`launchctl list` alone cannot tell the two apart. Reload it: "
                f"launchctl bootout gui/{uid}/{label}; "
                f"launchctl bootstrap gui/{uid} {plist_path}"
            )
    return out


def receipts_reconcile_findings(vault: Path | None) -> list[str]:
    """Surface receipts-reconcile data findings the launchd pass can't see.

    daily_reconcile.py exits 0 on data findings (only operational errors exit
    non-zero), so a stale pipeline heartbeat or bad receipt row slips past the
    launchctl-status pass. Read the newest note: flag hard_violations > 0, or
    flag the note being > 48h stale (the reconcile job itself stopped).

    `vault is None` → nothing to read; the notes live inside a vault.
    """
    if vault is None:
        return []
    notes_dir = vault / "⚙️ Meta" / "Receipts Reconcile"
    if not notes_dir.exists():
        return []
    notes = sorted(notes_dir.glob("*.md"), reverse=True)
    if not notes:
        return []
    newest = notes[0]
    out: list[str] = []
    try:
        age_h = (time.time() - newest.stat().st_mtime) / 3600.0
    except OSError:
        age_h = 0.0
    if age_h > 48:
        out.append(
            f"  - receipts-reconcile: newest note is {int(age_h)}h old "
            f"({newest.name}) — the daily reconcile job may have stopped"
        )
    try:
        text = newest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    m = re.search(r"^hard_violations:\s*(\d+)", text, re.MULTILINE)
    if m and int(m.group(1)) > 0:
        out.append(
            f"  - receipts-reconcile: {m.group(1)} hard violation(s) in "
            f"{newest.name} — receipts pipeline may be stalled, read the note"
        )
    return out


# Matched as a SUFFIX so any operator's reverse-DNS namespace works
# (`com.<whoever>.team-broadcast-daily`). These jobs have a dedicated finder
# below that gives better recovery guidance than the generic launchd pass.
BESPOKE_LAUNCHD_SUFFIXES = (".team-broadcast-daily",)

# `launchctl print` is slow enough per-label that an unbounded fleet could slow
# down every session start. This caps total probes per run regardless of how
# many "mine, loaded, status==0" candidates exist.
MAX_LAUNCHD_PRINT_PROBES = 25


def team_broadcast_findings(log_path: Path | None = None) -> list[str]:
    """Surface a failed daily team-broadcast per workspace, with a fix command.

    The generic RUNNERS pass uses a 3-failures-in-72h threshold tuned for the
    hourly auto-snapshot job. The daily 18:00 broadcast fails intermittently
    (a claude-router auth blip, a venv break) and never trips 3-in-72h, so it
    slipped past silently for 10 days. That gap is why "I'm not seeing the
    daily updates" surfaced (2026-05-22). A daily job needs last-run-outcome
    detection: one failed run is one lost broadcast.

    Reads the LAST exit code per workspace from the append-only log. Surfaces
    a finding only while the most recent run for a workspace is still failed,
    so a recovered failure self-clears instead of nagging for 72h. Also flags
    the cron going stale (no run in 48h).
    """
    if log_path is None:
        log_path = HOME / ".claude" / "logs" / "team-broadcast-daily.log"
    if not log_path.exists():
        return []
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    last_exit: dict[str, int] = {}
    last_ts: str | None = None
    # Workspace token is generic: hardcoding one operator's workspace names
    # here made the parser silently match nothing for everyone else, so a
    # failed broadcast never surfaced on any other install.
    exit_re = re.compile(r"^\[([^\]]+)\]\s+([A-Za-z0-9][\w.-]*)\s+exit=(\d+)")
    for line in text.splitlines():
        m = exit_re.match(line)
        if not m:
            continue
        last_exit[m.group(2)] = int(m.group(3))
        ts_m = TS_PATTERN.search(m.group(1))
        if ts_m:
            last_ts = ts_m.group(1)

    if not last_exit:
        return []

    out: list[str] = []

    # Cron itself stopped firing. launchd runs the job whether or not a Claude
    # session is open, so a stale last-run means the schedule broke.
    if last_ts:
        try:
            age_h = (time.time()
                     - datetime.datetime.fromisoformat(last_ts).timestamp()) / 3600.0
            if age_h > 48:
                out.append(
                    f"  - team-broadcast-daily: no run in {int(age_h)}h. The "
                    f"daily cron may have stopped (launchctl list | grep "
                    f"team-broadcast-daily)"
                )
        except ValueError:
            pass

    failed = sorted(ws for ws, code in last_exit.items() if code != 0)
    if failed:
        named = ", ".join(failed)
        out.append(
            f"  - team-broadcast-daily: last run FAILED for {named}. That "
            f"stand-up never posted.\n"
            f"    Re-send now (from a terminal where the claude CLI is logged "
            f"in): bash ~/.local/bin/team-broadcast-daily.sh\n"
            f"    log: {log_path}"
        )
    return out


def main() -> int:
    if os.environ.get("SURFACE_STALE_AUTOMATION_BYPASS"):
        return 0

    # SessionStart payload arrives on stdin (JSON). Drain so we don't block.
    # `cwd` on it is how we know WHICH vault's runners to scan.
    raw = ""
    try:
        raw = sys.stdin.read()
    except Exception:
        pass
    cwd = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
        if isinstance(payload, dict):
            cwd = payload.get("cwd") or ""
    except Exception:
        pass
    vault = vault_root_for(Path(cwd) if cwd else Path.cwd())

    findings: list[str] = []
    for label, log_path, pattern, threshold, hint in runners(vault):
        try:
            n = fail_count_in_window(log_path, pattern, WINDOW_SECONDS)
        except Exception:
            continue
        if n >= threshold:
            findings.append(
                f"  - {label}: {n} failure(s) in last 72h ({hint})\n"
                f"    log: {log_path}"
            )

    # launchd exit-status pass — auto-covers every job this user installed,
    # including ones not in RUNNERS (the gap behind the silent failures).
    try:
        findings.extend(launchd_failures())
    except Exception:
        pass

    # receipts-reconcile exits 0 on data findings, so its stale-pipeline
    # signal won't show in the launchd pass — read the note directly.
    try:
        findings.extend(receipts_reconcile_findings(vault))
    except Exception:
        pass

    # team-broadcast-daily: a daily job needs last-run-outcome detection,
    # not the generic 3-in-72h count tuned for hourly runners.
    try:
        findings.extend(team_broadcast_findings())
    except Exception:
        pass

    if not findings:
        return 0

    msg = (
        "⚠️  Automation health: persistent silent-failure(s) detected\n\n"
        + "\n".join(findings)
        + "\n\nThe 2026-05-14 191-file strand had exactly this signature: "
        "auto-snapshot.sh failed every hour for 48+ hours unnoticed. "
        "Investigate the failing runner(s) before they accumulate damage.\n"
        "Bypass: SURFACE_STALE_AUTOMATION_BYPASS=1"
    )

    print(json.dumps({"systemMessage": msg}))
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
