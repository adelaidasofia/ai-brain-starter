#!/usr/bin/env python3
"""
warn-stale-dev-checkout.py — PreToolUse guard.

Bug class: STALE-BARE-CHECKOUT-READ (sibling of WRONG-ARTIFACT-VERIFIED).

Incident 2026-06-08 (MYC-670): the ~/dev/mycelium-studio BARE checkout was 147
commits / 2 weeks behind origin/main. A recon Read of its pr-build.yml returned a
stale, JS-only workflow that contradicted the Linear issue. The miss was caught
only by LUCK (the issue described jobs absent from the stale file). A subtler
2-week drift (a 5-line change to an existing job) would have slipped through and
produced a broken / conflicting edit.

Recurrence 2026-06-21 (MYC-1127 re-audit): a `git grep` recon of the same bare
checkout (282 behind) returned NOTHING for a dir that did not exist at the stale
HEAD — nearly mis-concluding "the consumer doesn't exist". The Read tool warned;
the Bash `git grep` did NOT, because this guard only covered Read/Edit/Write.
Coverage now includes Bash read-class commands (git grep/log/show/diff without a
ref, plus cat/grep/rg/sed/head/tail on a checkout path). A ref-qualified read
(`origin/main`) IS the canonical remedy → stays silent.

Root cause: bare ~/dev/<repo> checkouts ROT — all real work happens in per-session
worktrees (which base on origin/main and are fresh by construction), so nobody
ever pulls the bare checkout. Reading it as if it were "current" is the danger.

Fix: when a file-touching tool OR a read-class Bash command targets a STALE bare
~/dev/<repo> checkout, warn once (per session, per repo) with the canonical-state
remedy. Worktrees (<repo>-<slug>, whose .git is a FILE pointer) are fresh by
construction and are skipped. Fail-open, non-blocking.

Discriminator (Julia Evans, panel 2026-06-08): a bare checkout's .git is a DIR
(can rot); a worktree's .git is a FILE (gitdir pointer, fresh off origin/main).

Noise budget (Charity Majors, panel 2026-06-08): fire ONCE per (session, repo),
only on real drift (>= THRESHOLD behind), remedy copy-pasteable. A guard that
cries wolf teaches its own bypass (over-strict-verification-teaches-bypass.md).
Bash precision: a ref-qualified read (origin/main) is the remedy → never warned;
a command merely MENTIONING a fresh repo never warns (gated on real staleness).

Bypass: STALE_CHECKOUT_BYPASS=1
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

THRESHOLD = 5          # commits behind origin/main before we warn
FETCH_TIMEOUT = 8      # seconds; fail-open past this
STALE_FETCH_DAYS = 3.0 # if last fetch older than this, also flag knowledge-staleness
# STALE_CHECKOUT_DEV_ROOT overrides the scanned root (tests point it at a tempdir).
DEV = Path(os.environ.get("STALE_CHECKOUT_DEV_ROOT") or (Path.home() / "dev"))
SEEN_DIR = Path.home() / ".claude" / ".stale-dev-checkout-seen"

# Telemetry so this guard's fire-count is measurable — the precondition for
# RETIRING it once MYC-427 (merge queue) stops bare checkouts from rotting.
# Fail-open: a missing _lib must never break the guard (or its tests).
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "_lib"))
    from guard_telemetry import log_fire
except Exception:
    def log_fire(*_a, **_k):
        return

# Inline-bypass consult (MYC-772): a `STALE_CHECKOUT_BYPASS=1 <cmd>` prefix lives
# ONLY in the command string, never the hook's os.environ — read both or the
# advertised bypass can never fire (HOOK-READS-SESSION-ENV-NOT-COMMAND-ENV).
try:
    from cmd_env import inline_bypass
except Exception:                          # fail-open: a missing _lib must never break the guard
    def inline_bypass(command, var, value="1"):  # type: ignore
        return False


def _run(args, cwd=None, timeout=10):
    try:
        r = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception:
        return 1, "", ""


def _repo_root(path: Path):
    """Walk up to the dir that contains a .git entry. Returns (root, git_is_dir)."""
    for d in [path, *path.parents]:
        g = d / ".git"
        if g.exists():
            return d, g.is_dir()
    return None, False


def _behind(root: Path):
    """Commits HEAD is behind origin/main (or origin/master). None if no origin ref."""
    for ref in ("origin/main", "origin/master"):
        rc, out, _ = _run(["git", "-C", str(root), "rev-list", "--count", f"HEAD..{ref}"])
        if rc == 0 and out.isdigit():
            return int(out), ref
    return None, None


def _fetch_age_days(root: Path):
    fh = root / ".git" / "FETCH_HEAD"
    if fh.exists():
        return (time.time() - fh.stat().st_mtime) / 86400.0
    return None


def _warn_for_root(root: Path, session_id: str):
    """Once-per-(session,repo) staleness warning for a BARE checkout root.

    Caller guarantees `root` is a bare checkout (.git is a DIR). Returns the
    warning string, or None to stay silent.
    """
    # Fire once per (session, repo).
    try:
        SEEN_DIR.mkdir(parents=True, exist_ok=True)
        marker = SEEN_DIR / f"{session_id}__{root.name}"
        if marker.exists():
            return None
        marker.touch()
    except Exception:
        pass  # marker is best-effort; never block on it

    # Refresh origin refs once (timeout-guarded, fail-open).
    _run(["git", "-C", str(root), "fetch", "--quiet", "origin"], timeout=FETCH_TIMEOUT)

    behind, ref = _behind(root)
    if behind is None:
        return None  # no origin/main to compare against (local-only repo)

    age = _fetch_age_days(root)
    stale_knowledge = age is not None and age > STALE_FETCH_DAYS

    if behind < THRESHOLD and not stale_knowledge:
        return None

    _, head, _ = _run(["git", "-C", str(root), "log", "-1", "--format=%h %cs (%cr)"])
    msg = (
        f"Heads up: {root} is {behind} commit(s) behind {ref} (HEAD {head}), so "
        f"the files on disk here may not reflect the latest version. Before "
        f"editing or relying on a file in this checkout, either:\n"
        f"  • bring it up to date (safe, refuses if it can't fast-forward):\n"
        f"      git -C {root} pull --ff-only\n"
        f"  • or read the up-to-date version directly without touching the tree:\n"
        f"      git -C {root} show {ref}:<path>\n"
        f"      git -C {root} grep <pat> {ref} -- <path>\n"
        f"Until then, don't treat {root.name}'s working tree as current. "
        f"Bypass: STALE_CHECKOUT_BYPASS=1"
    )
    log_fire("warn-stale-dev-checkout", status="warned", repo=root.name, behind=behind)
    return msg


def evaluate(file_path: str, session_id: str):
    """File-tool path (Read/Edit/Write/MultiEdit). Returns a warning or None."""
    if os.environ.get("STALE_CHECKOUT_BYPASS") == "1":
        return None
    try:
        p = Path(file_path).resolve()
    except Exception:
        return None

    # Only ~/dev/<repo> paths.
    try:
        p.relative_to(DEV.resolve())
    except (ValueError, OSError):
        return None

    root, git_is_dir = _repo_root(p)
    if root is None:
        return None
    # Worktrees (.git is a FILE) are fresh off origin/main by construction → skip.
    if not git_is_dir:
        return None

    return _warn_for_root(root, session_id)


def _bash_dev_targets(command: str):
    """Resolve the ~/dev/<repo> dirs a bash command references (best-effort).

    Matches any of DEV's spellings — the resolved absolute path, `~/dev`,
    `$HOME/dev` — followed by `/<repo>`. A literal path in the command string
    (incl. a `R=~/dev/<repo>` assignment) is enough; unexpanded shell variables
    are not chased (precision over completeness — the file-tool path still
    covers Read/Edit/Write).
    """
    prefixes = set()
    try:
        prefixes.add(str(DEV))
        prefixes.add(str(DEV.resolve()))
    except Exception:
        prefixes.add(str(DEV))
    try:
        rel = DEV.resolve().relative_to(Path.home().resolve())
        prefixes.add(f"~/{rel}")
        prefixes.add(f"$HOME/{rel}")
    except Exception:
        pass

    cands = {}
    for pfx in prefixes:
        for m in re.finditer(re.escape(pfx) + r"/([A-Za-z0-9][A-Za-z0-9._-]*)", command):
            cand = DEV / m.group(1)
            cands[str(cand)] = cand
    return list(cands.values())


def evaluate_bash(command: str, session_id: str):
    """Bash path. Warns on a read-class command against a STALE bare checkout.

    A ref-qualified read (`origin/main` / `origin/master`) IS the canonical-state
    remedy — stay silent. Otherwise, any referenced ~/dev/<repo> that is a bare,
    stale checkout warns once. Worktrees / fresh / missing dirs are skipped.
    """
    if os.environ.get("STALE_CHECKOUT_BYPASS") == "1" or inline_bypass(command, "STALE_CHECKOUT_BYPASS"):
        return None
    if not command:
        return None
    # Already reading canonical state → never nag.
    if re.search(r"\borigin/(main|master)\b", command):
        return None

    for cand in _bash_dev_targets(command):
        try:
            g = cand / ".git"
            if not cand.is_dir() or not g.exists() or not g.is_dir():
                continue  # missing, non-repo, or worktree (.git is a FILE) → skip
        except OSError:
            continue
        warning = _warn_for_root(cand, session_id)
        if warning:
            return warning
    return None


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    tool = payload.get("tool_name", "")
    session_id = payload.get("session_id", "nosession")
    ti = payload.get("tool_input") or {}
    try:
        if tool in ("Read", "Edit", "Write", "MultiEdit"):
            fp = ti.get("file_path", "")
            warning = evaluate(fp, session_id) if fp else None
        elif tool == "Bash":
            warning = evaluate_bash(ti.get("command", ""), session_id)
        else:
            sys.exit(0)
    except Exception:
        sys.exit(0)  # fail-open: a freshness nudge must never block a read/edit
    if warning:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": warning,
            }
        }))
    sys.exit(0)


if __name__ == "__main__":
    main()
