#!/usr/bin/env python3
"""ai-brain-auto-update.py — UserPromptSubmit auto-update for the deployed
ai-brain-starter checkout. Prints ONE Claude-Code hook JSON object on stdout
and ALWAYS exits 0 (a UserPromptSubmit hook must never block the turn).

This is the cross-platform (macOS / Linux / Windows) successor to
ai-brain-auto-update.sh, which is now a thin delegator to this file. The bash
version could not run on native Windows (no bash, no `timeout`, no `nice`,
no `find -mtime`), which left every Windows install permanently stale — the
exact silent-drift class the auto-update exists to prevent.

THE REACH GUARANTEE (MYC-720): when the pull moves HEAD, this DEPLOYS the new
hooks itself (runs scripts/install-hooks-user-level.py, bounded) instead of
only asking the model to.

Safety, preserved from the shell version:
  - Pinnable:      ~/.claude/.ai-brain-starter-pinned present => no-op.
  - Rate-limited:  runs at most once per ABS_UPDATE_INTERVAL_DAYS (default 6).
  - Single-flight: atomic mkdir lock so concurrent sessions never double-run.
  - ff-ONLY:       fetch + `merge --ff-only`. A dirty tree or divergent fork is
                   REFUSED and surfaced for manual merge — never given a
                   surprise merge commit.
  - Bounded:       every subprocess runs under a wall-clock timeout
                   (subprocess timeout= — portable, unlike GNU `timeout`), so a
                   hung git or installer can never wedge the user's prompt.
  - Fail-open:     any unexpected error emits a valid silent JSON object.

Hermetically testable via env overrides (tests/integration/
test_ai_brain_auto_update.sh runs through the .sh delegator): ABS_SKILL_DIR,
ABS_UPDATE_STATE_DIR, ABS_UPDATE_INTERVAL_DAYS, ABS_UPDATE_DEPLOY_TIMEOUT.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

GIT_TIMEOUT = 60  # seconds per git call; network hangs must not wedge the prompt


def _state_dir() -> Path:
    return Path(os.environ.get("ABS_UPDATE_STATE_DIR") or (Path.home() / ".claude"))


def _skill_dir() -> Path:
    return Path(os.environ.get("ABS_SKILL_DIR")
                or (Path.home() / ".claude" / "skills" / "ai-brain-starter"))


def silent() -> None:
    """The no-op form — a UserPromptSubmit hook must always print valid JSON."""
    print('{"continue":true,"suppressOutput":true}')
    raise SystemExit(0)


def emit_ctx(message: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": message,
    }}))
    raise SystemExit(0)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        timeout=GIT_TIMEOUT,
    )


def _reclaim_stale_lock(lock: Path) -> None:
    """A SIGKILL mid-run strands the lock and would silently disable updates
    forever — reclaim one older than any run could take (60 min >> timeouts)."""
    try:
        if lock.is_dir() and (time.time() - lock.stat().st_mtime) > 3600:
            lock.rmdir()
    except OSError:
        pass


# Abandoned-git-lock reclaim (MYC-3175). ONE canonical implementation in
# hooks/_lib/git_locks.py, shared with the ~/dev hub fleet — a second copy would
# rot the moment one is fixed. Fail-open: a missing _lib must never break the
# updater, which is the thing that would repair it.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks" / "_lib"))
    from git_locks import reclaim_stale_git_locks as _reclaim_stale_git_locks
except Exception:  # pragma: no cover - heal is best-effort, never load-bearing
    def _reclaim_stale_git_locks(_repo):
        return []


def _stamp(path: Path) -> None:
    """Record 'this happened now'. Never raises — a stamp failure must not
    break the update it is only observing."""
    try:
        path.touch()
        os.utime(path, None)
    except OSError:
        pass


def _install_fix_cmd() -> str:
    """The manual re-install command, phrased for the user's actual platform."""
    py = "python" if os.name == "nt" else "python3"
    return (f"{py} \"{_skill_dir() / 'scripts' / 'install-hooks-user-level.py'}\" "
            "--quiet --fail-on-missing")


def run() -> None:
    state = _state_dir()
    skill = _skill_dir()
    pin = state / ".ai-brain-starter-pinned"
    last = state / ".ai-brain-starter-last-update"
    # Distinct from `last`, and the distinction IS the signal (MYC-3175).
    # `last` records that an ATTEMPT happened; this records that the clone was
    # confirmed CURRENT with origin. A frozen clone keeps stamping `last`
    # forever while this one stops moving — the only reliable freeze signal,
    # since "behind origin" is precisely what a clone that cannot fetch
    # under-reports.
    last_ok = state / ".ai-brain-starter-last-successful-pull"
    lock = state / ".ai-brain-starter-update.lock"
    interval_days = float(os.environ.get("ABS_UPDATE_INTERVAL_DAYS", "6"))
    deploy_timeout = float(os.environ.get("ABS_UPDATE_DEPLOY_TIMEOUT", "120"))

    # 0. Pinned -> no-op (the escape hatch; must win before any fetch).
    if pin.exists():
        silent()

    # 0b. Reclaim abandoned git locks BEFORE the rate limit (MYC-3175 recurrence,
    # 2026-07-23). Healing used to sit at step 2b, AFTER step 1 -- which gated the
    # cure behind the disease. A stranded .git/index.lock fails every git
    # operation forever, and step 1 claims the interval up-front, so a lock
    # appearing just after a run cannot be healed for a full interval: every
    # session in that window returns at step 1 without ever reaching the healer.
    #
    # Observed: a 0-byte lock dated Jul 21 18:32 survived ~30h of sessions on a
    # machine where this healer was already deployed AND wired, and had to be
    # cleared by hand. That is exactly the MYC-2453 cooldown-locks-out-retries
    # trap that MYC-3175 named as a sibling it was not repeating.
    #
    # Safe to hoist: the reclaim is stdlib, network-free, and returns immediately
    # when no lock file exists, so the common path costs one stat. It stays
    # conservative (age threshold + liveness check) exactly as before.
    if (skill / ".git").exists():
        _reclaim_stale_git_locks(skill)

    # 1. Rate-limit: only once per interval. Absent LAST means "never ran".
    try:
        if last.is_file() and (time.time() - last.stat().st_mtime) < interval_days * 86400:
            silent()
    except OSError:
        silent()

    # 2. Single-flight: atomic mkdir lock, with stale-lock reclaim.
    _reclaim_stale_lock(lock)
    try:
        lock.mkdir()
    except OSError:
        silent()  # a held-and-fresh lock is a real concurrent session
    try:
        try:
            last.touch()  # claim this interval up-front (matches prior behavior)
            # Seed the success stamp on the FIRST run so staleness is measured
            # from real data. Without a seed, a clone that never once pulled
            # successfully would have no stamp to age — and the freeze it is
            # meant to catch would be the exact case that stays invisible.
            if not last_ok.exists():
                last_ok.touch()
        except OSError:
            pass

        if not (skill / ".git").exists():
            silent()

        # 2b. Reclaim abandoned git locks BEFORE any git call. A stranded
        # .git/index.lock fails every fetch/merge forever, so without this the
        # install freezes permanently and silently (MYC-3175).
        reclaimed_locks = _reclaim_stale_git_locks(skill)

        # 3. Fetch. Network down -> gentle note, never crash the turn.
        try:
            fetch = _git(["fetch", "origin", "main", "--quiet"], skill)
        except (subprocess.TimeoutExpired, OSError):
            fetch = None
        if fetch is None or fetch.returncode != 0:
            err = (fetch.stderr or "") if fetch is not None else ""
            if "lock" in err.lower():
                # Not a network problem. Saying "couldn't reach the internet"
                # here sends the user to debug wifi while a held lock blocks
                # every update (MYC-3175).
                emit_ctx(
                    "AI Brain Starter could not check for updates: a git lock file "
                    f"in {skill} is being held. If another git process is running "
                    "there, this clears itself; otherwise the updater auto-clears "
                    "locks older than an hour on the next check. Verbatim git "
                    f"error: {err.strip()[:300]}")
            emit_ctx(
                "AI Brain Starter checked for updates but couldn't reach the "
                "internet (or the repository). Nothing is wrong — it will try "
                "again in a few days. No action needed.")

        try:
            head = _git(["rev-parse", "HEAD"], skill).stdout.strip()
            origin = _git(["rev-parse", "origin/main"], skill).stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            silent()
        if not head or head == origin:
            # Confirmed current with origin: the fetch reached the remote and
            # HEAD matches it. That is a SUCCESSFUL pull for freeze-detection
            # purposes even though nothing moved.
            _stamp(last_ok)
            # Surface a heal even when there is nothing to pull. This is the
            # case that was invisible before: the clone had been frozen for
            # days by a stranded lock, and going silent here would hide both
            # the freeze and the repair (MYC-3175).
            if reclaimed_locks:
                emit_ctx(
                    "AI Brain Starter cleared an abandoned git lock "
                    f"({', '.join(reclaimed_locks)}) in {skill} that a crashed git "
                    "process had left behind. Every update had been failing since "
                    "then, silently. Updates work again — your copy is now current. "
                    "No action needed.")
            silent()  # already current

        # 4. ff-ONLY. Tracked-file edits or a divergent fork refuse the pull.
        try:
            status = _git(["status", "--porcelain", "--untracked-files=no"], skill)
            if status.stdout.strip():
                emit_ctx(
                    "AI Brain Starter auto-update is BLOCKED (safely): your copy at "
                    f"{skill} has local edits to tracked files, so it will not "
                    "auto-pull — your edits are preserved. To update when you're "
                    f"ready: cd \"{skill}\" && git stash && git pull --ff-only "
                    "origin main && git stash pop (or discard the local changes "
                    "first). Everything else keeps working in the meantime.")
            merge = _git(["merge", "--ff-only", "origin/main", "--quiet"], skill)
            if merge.returncode != 0:
                # Distinguish the two causes. A stuck lock is NOT a fork, and
                # telling the user "you have a local fork" sends them to fix
                # the wrong thing while the real cause (an abandoned
                # .git/*.lock a crashed git left behind) persists forever.
                # A lock still present HERE survived 2b, so it is either fresh
                # (a real concurrent git) or genuinely held.
                if "lock" in (merge.stderr or "").lower():
                    emit_ctx(
                        "AI Brain Starter auto-update is BLOCKED: a git lock file in "
                        f"{skill} is being held, so the pull cannot run. If another "
                        "git process is working there right now, this clears itself. "
                        "If nothing else is running, a crashed git left the lock "
                        "behind and every future update will keep failing until it "
                        "is removed — the updater auto-clears locks older than an "
                        "hour, so this should resolve on the next check. Verbatim "
                        f"git error: {(merge.stderr or '').strip()[:300]}")
                emit_ctx(
                    "AI Brain Starter auto-update is BLOCKED (safely): your copy at "
                    f"{skill} has diverged from the official version (a local "
                    "fork), so it cannot fast-forward. Your fork is preserved. To "
                    f"merge manually: cd \"{skill}\" && git pull --rebase origin "
                    "main (or your preferred strategy).")
        except (subprocess.TimeoutExpired, OSError):
            silent()

        # The ff-only merge succeeded: fetched from origin and moved HEAD onto
        # it. Every emit_ctx above exits, so reaching here means a real pull.
        _stamp(last_ok)

        try:
            log = _git(["log", "--oneline", f"{head}..HEAD"], skill)
            changes = ";".join(log.stdout.splitlines()[:20])
        except (subprocess.TimeoutExpired, OSError):
            changes = "(unavailable)"

        # 5. Propagate skill content (backs up customizations before overwrite).
        #    sync-skills.py is canonical; the .sh stub survives for old fixtures.
        sync_py = skill / "scripts" / "sync-skills.py"
        sync_sh = skill / "scripts" / "sync-skills.sh"
        sync_env = {**os.environ, "ABS_SYNC_STARTER_DIR": str(skill)}
        sync_output = ""
        try:
            if sync_py.is_file():
                sync = subprocess.run([sys.executable, str(sync_py)],
                                      capture_output=True, text=True,
                                      timeout=deploy_timeout, env=sync_env)
                sync_output = "\n".join((sync.stdout + sync.stderr).splitlines()[-20:])
            elif os.name != "nt" and sync_sh.is_file():
                sync = subprocess.run(["bash", str(sync_sh)],
                                      capture_output=True, text=True,
                                      timeout=deploy_timeout)
                sync_output = "\n".join((sync.stdout + sync.stderr).splitlines()[-20:])
        except (subprocess.TimeoutExpired, OSError):
            sync_output = "(skill sync did not finish; it will retry next update)"

        # 6. THE REACH GUARANTEE: deploy the freshly-pulled hooks NOW, bounded.
        installer = skill / "scripts" / "install-hooks-user-level.py"
        deploy_note = "Hooks were rewired automatically."
        try:
            deploy = subprocess.run(
                [sys.executable, str(installer), "--quiet", "--fail-on-missing"],
                capture_output=True, text=True, timeout=deploy_timeout)
            rc = deploy.returncode
        except subprocess.TimeoutExpired:
            rc = 124
        except OSError:
            rc = 1
        if rc == 124:
            deploy_note = (
                "One follow-up needed: the hook re-install step ran out of time, "
                "so the newest hooks may not be active yet. To finish it, run: "
                f"{_install_fix_cmd()}")
        elif rc != 0:
            deploy_note = (
                "One follow-up needed: the hook re-install step didn't finish "
                "cleanly, so the newest hooks may not be active yet. To finish "
                f"it, run: {_install_fix_cmd()}")

        emit_ctx(
            f"AI Brain Starter was auto-updated and hooks were redeployed. "
            f"Commits: {changes} Skill sync: {sync_output} {deploy_note} "
            "Any changed file was backed up to <file>.bak-YYYY-MM-DD-HHMM first, "
            "so local customizations are recoverable. Now, briefly and casually "
            "(not a changelog dump, no jargon, nothing alarming): 1) Read "
            "docs/CHANGELOG.md in the ai-brain-starter skill folder (top entry "
            "only) and tell the user in 1-2 plain sentences what changed and "
            "why it helps them. 2) If the update added rules to the Obsidian "
            "Rules or Session Protocol sections of SKILL.md, read the user's "
            "vault CLAUDE.md and, for each new or changed rule not already "
            "there, offer to merge it: show a short diff, explain the benefit "
            "in plain words, ask one yes/no question, and on yes back up "
            "CLAUDE.md to CLAUDE.md.bak-YYYY-MM-DD-HHMM before editing. 3) If "
            "the skill sync backed up any files, mention it so the user knows "
            "their customizations are recoverable.")
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def main() -> None:
    try:
        run()
    except SystemExit:
        raise
    except Exception:
        # Fail-open backstop: never break the user's prompt.
        print('{"continue":true,"suppressOutput":true}')
        raise SystemExit(0)


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
