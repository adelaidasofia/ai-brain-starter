#!/usr/bin/env python3
"""SessionStart hook: surface a git operation that has been IN FLIGHT too long.

WHY. `block-git-mutation-mid-operation.py` refuses commits/pushes while a repo
has a paused rebase / merge / cherry-pick. That guard is correct and stays. But
it is registered PreToolUse ONLY, so it is a WALL, never a SIGNAL:

  - You learn the operation exists only when you try to mutate.
  - The block correctly says the abort/continue call belongs to whoever owns
    the operation -- so a passing session, correctly, does not resolve it.
  - Nothing anywhere reports HOW LONG it has been stalled.

Net effect: every session hits the wall, each defers to an owner who is not
coming back, and the repo stays frozen. Measured five times on mycelium-vault
(MYC-3777 7 days, MYC-3451 22 hours, MYC-3982 ~6 hours, MYC-3781, and a
5-hour stall on 2026-08-24 that held 19 commits from several sessions). Each
was recovered as a one-off INCIDENT; the detector was never built. This is it.

WHAT IT DOES. At session start, for the CURRENT repo only (O(1) -- no tree
walk, no worktree sweep), it checks for an in-flight operation and reports its
AGE. Silent when clean, and silent for a young operation, because a rebase you
started ninety seconds ago is not an incident.

It deliberately does NOT tell you to abort. Which of --abort / --continue is
right is a real judgement about someone's in-flight work, and this hook has no
standing to make it. It reports the fact, the age, and what is blocked.

Bypass: STALLED_GIT_OP_BYPASS=1
Self-test: --test
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# An operation younger than this is someone actively working. Only past it does
# a paused operation become "nobody is coming back".
STALL_MINUTES = int(os.environ.get("STALLED_GIT_OP_MINUTES", "30"))

# marker path (relative to git dir) -> human name. Ordered: first match wins.
OPERATIONS = (
    ("rebase-merge", "interactive/merge-backend rebase"),
    ("rebase-apply", "am-backend rebase"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
    ("MERGE_HEAD", "merge"),
    ("BISECT_LOG", "bisect"),
    ("sequencer", "sequencer (multi-commit rebase/cherry-pick)"),
)


def _git(args: list[str], cwd: str) -> str | None:
    """Run git, returning stripped stdout, or None on any failure.

    Never raises: a hook that crashes at SessionStart is worse than one that
    stays quiet.
    """
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10
        )
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def find_operation(git_dir: Path) -> tuple[str, float] | None:
    """Return (operation name, age in seconds) for the first marker present."""
    for marker, name in OPERATIONS:
        p = git_dir / marker
        if not p.exists():
            continue
        try:
            age = time.time() - p.stat().st_mtime
        except OSError:
            age = 0.0
        return name, age
    return None


def _fmt_age(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 60:
        return f"{m} min"
    h, rem = divmod(m, 60)
    if h < 24:
        return f"{h}h {rem}m"
    d, rh = divmod(h, 24)
    return f"{d}d {rh}h"


def build_report(cwd: str) -> str | None:
    """The systemMessage, or None when there is nothing worth saying."""
    git_dir_s = _git(["rev-parse", "--absolute-git-dir"], cwd)
    if not git_dir_s:
        return None
    git_dir = Path(git_dir_s)

    found = find_operation(git_dir)
    if not found:
        return None
    op, age = found
    if age < STALL_MINUTES * 60:
        return None

    toplevel = _git(["rev-parse", "--show-toplevel"], cwd) or cwd
    detached = _git(["symbolic-ref", "-q", "HEAD"], cwd) is None
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd) or "?"

    lines = [
        f"[stalled-git-op] {Path(toplevel).name}: {op} has been IN FLIGHT for "
        f"{_fmt_age(age)}.",
        "",
        "  Every session in this repo is blocked from committing or pushing until",
        "  it ends -- block-git-mutation-mid-operation.py refuses git mutations while",
        "  an operation is paused, and it will not tell you the operation is stale.",
        f"  repo:      {toplevel}",
        f"  git-dir:   {git_dir}",
    ]
    if detached:
        lines.append(
            "  HEAD:      DETACHED -- commits made here land on the operation's "
            "temporary base, NOT on a branch."
        )
    else:
        lines.append(f"  HEAD:      attached to {branch}")

    # How much is waiting behind it. Best-effort; omitted when unknown.
    ahead = _git(["rev-list", "--count", "@{u}..HEAD"], cwd)
    if ahead and ahead.isdigit() and int(ahead) > 0:
        lines.append(f"  unpushed:  {ahead} commit(s) waiting behind this operation")

    lines += [
        "",
        "  Inspect before deciding (all read-only):",
        f"    git -C {toplevel} status",
        f"    git -C {toplevel} log --oneline -5",
        "",
        "  Ending it is a real choice between --abort and --continue and it belongs",
        "  to whoever owns the operation. Before either, anchor anything at risk so",
        "  no path can lose it:",
        f"    git -C {toplevel} tag rescue/$(date +%Y%m%d-%H%M)-head HEAD",
        "",
        "  Bypass this notice: STALLED_GIT_OP_BYPASS=1",
    ]
    return "\n".join(lines)


def _self_test() -> int:
    """Negative control: the check must FIRE on a planted operation, and stay
    silent on a clean repo. A guard that has only ever seen the clean path is
    unproven."""
    import tempfile

    failures = []
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["git", "init", "-q", td], check=True)
        subprocess.run(["git", "-C", td, "commit", "-q", "--allow-empty",
                        "-m", "seed"], check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t",
                            "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@t"})

        # 1. clean repo -> silent
        if build_report(td) is not None:
            failures.append("clean repo produced a report (should be silent)")

        gd = Path(subprocess.run(["git", "-C", td, "rev-parse", "--absolute-git-dir"],
                                 capture_output=True, text=True).stdout.strip())

        # 2. YOUNG operation -> still silent (not an incident yet)
        (gd / "rebase-merge").mkdir(parents=True, exist_ok=True)
        if build_report(td) is not None:
            failures.append("a fresh operation produced a report (should be silent)")

        # 3. STALE operation -> MUST fire. This is the negative control: the
        #    thing the hook exists to catch.
        old = time.time() - (STALL_MINUTES + 5) * 60
        os.utime(gd / "rebase-merge", (old, old))
        rep = build_report(td)
        if rep is None:
            failures.append("a STALE operation produced NO report -- the guard is inert")
        elif "IN FLIGHT" not in rep:
            failures.append("report did not name the condition")

        # 4. a different operation shape is also caught
        (gd / "rebase-merge").rmdir()
        (gd / "MERGE_HEAD").write_text("x")
        os.utime(gd / "MERGE_HEAD", (old, old))
        if build_report(td) is None:
            failures.append("a stale MERGE_HEAD was not caught")

    for f in failures:
        print(f"FAIL: {f}")
    print("self-test: PASS" if not failures else f"self-test: {len(failures)} FAILURE(S)")
    return 1 if failures else 0


def main() -> int:
    if "--test" in sys.argv:
        return _self_test()
    if os.environ.get("STALLED_GIT_OP_BYPASS") == "1":
        return 0
    try:
        msg = build_report(os.getcwd())
    except Exception:
        return 0  # never break session start
    if msg:
        print(json.dumps({"systemMessage": msg}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
