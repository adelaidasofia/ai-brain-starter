#!/usr/bin/env python3
"""Behavior controls for the unpushed / no-remote drift surface (MYC-2070).

The capability this covers shipped to a PRIVATE personal repo while its four
lighter dev-hygiene siblings were already public, so a client install got the
cheap hooks and not the one that says "you have committed work backed up
nowhere" (Substrate Is Product; ARTIFACT-WITHOUT-ACTIVATION). File presence is
not the assertion — BEHAVIOR on a clean install is:

  * a repo with NO remote and a >24h commit                 -> fires NO_REMOTE
  * a fully-pushed repo                                     -> silent
  * a `.no-remote-ok`-marked repo                           -> silent
  * a repo listed in the config's `no_remote_ok`            -> silent
  * many topic branches with local-only commits             -> DEMOTED to a
    count, never headlined (cry-wolf is the failure MYC-683 exists to prevent)
  * $ABS_DEV_ROOT / config `dev_root`                       -> honored, so the
    surface is not hardcoded to one operator's ~/dev

Run: python3 hooks/test_unpushed_drift_surface.py
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))


def git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, **kw)


def make_repo(path: Path, *, remote: Path = None, age_days: float = 3.0) -> Path:
    """A repo with one commit backdated `age_days`, optionally pushed to `remote`."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    (path / "f.txt").write_text("hello", encoding="utf-8")
    git(path, "add", "f.txt")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.gmtime(time.time() - age_days * 86400))
    env = dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    git(path, "commit", "-q", "-m", "work", env=env)
    if remote is not None:
        remote.mkdir(parents=True, exist_ok=True)
        assert git(remote, "init", "-q", "--bare").returncode == 0, "bare init failed"
        git(path, "remote", "add", "origin", str(remote))
        assert git(path, "push", "-q", "origin", "main").returncode == 0, "push failed"
        git(path, "fetch", "-q", "origin")
        # Assert the fixture is what it claims to be. A silently-failed push
        # would leave a repo with no remote refs, and every "pushed repo stays
        # silent" assertion below would then be testing the NO_REMOTE path and
        # passing for the wrong reason.
        refs = git(path, "for-each-ref", "--format=%(refname)", "refs/remotes/").stdout
        assert refs.strip(), f"fixture {path.name} has no remote-tracking refs"
    return path


def main() -> int:
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        dev = tmp / "code"          # NOT ~/dev — proves the root is configurable
        dev.mkdir()
        cfg = tmp / "dev-hygiene.json"

        os.environ["ABS_DEV_ROOT"] = str(dev)
        import _lib.dev_repo_scan as drs
        importlib.reload(drs)
        drs.CONFIG_PATH = cfg       # keep the operator's real config out of this

        # --- fixtures ----------------------------------------------------
        make_repo(dev / "no-remote")                                  # fires
        make_repo(dev / "pushed", remote=tmp / "pushed.git")          # silent
        marked = make_repo(dev / "deliberate")                        # silent (marker)
        (marked / ".no-remote-ok").write_text("scratch\n", encoding="utf-8")
        make_repo(dev / "by-config")                                  # silent (config)

        # A repo that IS backed up but carries many local-only topic branches:
        # the cry-wolf case. Must never be headlined.
        noisy = make_repo(dev / "noisy", remote=tmp / "noisy.git")
        for i in range(12):
            git(noisy, "checkout", "-q", "-b", f"claude/topic-{i}", "main")
            (noisy / f"t{i}.txt").write_text("x", encoding="utf-8")
            git(noisy, "add", f"t{i}.txt")
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 3 * 86400))
            git(noisy, "commit", "-q", "-m", f"topic {i}",
                env=dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp))
        git(noisy, "checkout", "-q", "main")

        cfg.write_text(json.dumps({"no_remote_ok": ["by-config"]}), encoding="utf-8")

        # --- the dev root is configuration, not a constant -----------------
        check(drs.resolve_dev_root() == dev,
              f"ABS_DEV_ROOT ignored; resolved {drs.resolve_dev_root()} (a client's "
              f"code does not live in the author's ~/dev)")
        repos = drs.discover_primary_repos()
        names = {p.name for p in repos}
        check(names == {"no-remote", "pushed", "deliberate", "by-config", "noisy"},
              f"discovery missed or invented repos: {sorted(names)}")

        # --- the drift verdict --------------------------------------------
        genuine, n_topic = drs.collect_drift(repos)
        fired = {r for r, _b, _n, _c in genuine}

        check("no-remote" in fired,
              "a repo backed up NOWHERE did not fire — this is the whole "
              "capability the substrate was missing")
        check(any(c == drs.BranchClass.NO_REMOTE
                  for r, _b, _n, c in genuine if r == "no-remote"),
              "the un-backed-up repo fired under the wrong class (not NO_REMOTE)")
        check("pushed" not in fired, "a fully-pushed repo was reported as drift")
        check("deliberate" not in fired,
              "a `.no-remote-ok`-marked repo still fired — a deliberately local "
              "repo nagging every session is cry-wolf by construction")
        check("by-config" not in fired,
              "the config `no_remote_ok` list did not suppress its repo")
        check("noisy" not in fired,
              "topic branches with local-only commits were HEADLINED; they must "
              "be demoted to a count (the MYC-683 cry-wolf failure)")
        check(n_topic >= 12,
              f"topic branches were not counted at all (n_topic={n_topic}); the "
              f"demoted pointer is how they stay visible without crying wolf")

        # --- fresh, un-backed-up work is NOT nagged ------------------------
        # A repo mid-first-commit is not stranded work; the age floor is what
        # keeps the surface from firing on work seconds old.
        make_repo(dev / "brand-new", age_days=0)
        fresh_fired = {r for r, _b, _n, _c in
                       drs.collect_drift(drs.discover_primary_repos())[0]}
        check("brand-new" not in fresh_fired,
              "a commit made moments ago was surfaced as stranded (age floor gone)")

        # --- the reaper agrees with the surface ----------------------------
        plan = drs.plan_repo_reap(dev / "no-remote")
        check(any(t.cls == drs.BranchClass.NO_REMOTE for t in plan.surface_branches),
              "the reaper did not SURFACE the un-backed-up repo")
        check(not plan.reap_branches,
              "the reaper marked un-backed-up work REAPABLE — there is no origin "
              "that could prove the content safe")
        marked_plan = drs.plan_repo_reap(marked)
        check(not marked_plan.surface_branches,
              "the reaper surfaced a `.no-remote-ok` repo the surfacer suppresses "
              "(two consumers, one policy — they must not disagree)")

        # --- ACTIVATION: the surface actually reaches a session ------------
        # The capability was missing from client installs, so "the classifier
        # is right" is only half the ticket — the verdict has to arrive
        # somewhere a person reads. The expensive scan runs in the daily pass
        # and writes state; the existing dev-hub SessionStart hook renders it,
        # which is why this adds no fan-out (footprint SLA, ADR-0004).
        state = tmp / "drift-state.json"
        # DEV_HUB_REFRESH_STATE points at a path that does NOT exist on purpose:
        # that is the fresh-install shape (no hub-refresh job has run yet), and
        # it is exactly the state in which the drift verdict used to vanish. The
        # developer box has real hub state, so without pinning this the
        # assertion below passes locally and fails on a clean runner — which is
        # how it was caught.
        env = dict(os.environ, ABS_DEV_ROOT=str(dev), DEV_DRIFT_STATE=str(state),
                   DEV_HUB_REFRESH_STATE=str(tmp / "no-such-hub-state.json"),
                   DEV_REPO_SCAN_DIR=str(HOOKS))
        rep = subprocess.run(
            [sys.executable, str(HOOKS.parent / "scripts" / "dev-drift-report.py"),
             "--write-state"], capture_output=True, text=True, env=env)
        check(rep.returncode == 0, f"dev-drift-report failed: {rep.stderr[-400:]}")
        check(state.is_file(),
              "the daily leg wrote no state file — the SessionStart surface "
              "would have nothing to render")
        doc = json.loads(state.read_text(encoding="utf-8"))
        check("no-remote" in (doc.get("section") or ""),
              f"the persisted section does not name the un-backed-up repo: "
              f"{(doc.get('section') or '')[:200]!r}")

        hook = subprocess.run(
            [sys.executable, str(HOOKS / "dev-hub-refresh-on-session-start.py")],
            input="{}", capture_output=True, text=True, env=env)
        rendered = json.loads(hook.stdout or "{}")
        ctx = (rendered.get("hookSpecificOutput") or {}).get("additionalContext", "")
        check("no-remote" in ctx,
              f"the SessionStart surface did not render the drift section with "
              f"NO hub state present (got {ctx[:200]!r}) — on a fresh install the "
              f"verdict would never reach a human")

        # An ABSENT state file must be SILENT, never reported as a clean fleet.
        state.unlink()
        hook = subprocess.run(
            [sys.executable, str(HOOKS / "dev-hub-refresh-on-session-start.py")],
            input="{}", capture_output=True, text=True, env=env)
        ctx2 = (json.loads(hook.stdout or "{}").get("hookSpecificOutput") or {}).get(
            "additionalContext", "")
        check("no-remote" not in ctx2 and "LOST-WORK" not in ctx2,
              "an absent state file still produced a drift verdict")

    if failures:
        print("FAILED — unpushed/no-remote drift surface (MYC-2070):")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("OK - drift surface: NO_REMOTE fires, pushed silent, marker + config "
          "suppress, topic branches demoted, age floor holds, reaper agrees, "
          "dev root configurable")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (ai-brain-starter#313; hooks/ sweep #314).
    # A hook that print()s non-ASCII raises UnicodeEncodeError on a cp1252
    # console: the gate then fails silently OPEN, or denies the tool call with
    # no legible cause. Idempotent; a no-op on an already-UTF-8 console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
