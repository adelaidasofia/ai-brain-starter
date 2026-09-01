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
                          capture_output=True, text=True, encoding="utf-8", **kw)


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
        # The condenser's state decides whether the hook renders the FULL
        # section or a one-line digest, so leaving it unpinned makes this
        # assertion a property of the machine rather than of the code (#539):
        # green on a clean CI runner, where no prior state exists so every run
        # is a "changed" first render, and red on a developer box, where a
        # matching prior render collapses the section to a digest and the
        # per-repo enumeration below is gone. Pinning it here is what makes both
        # the full-render and digest assertions deterministic everywhere.
        sr_state = tmp / "standing-reports"
        # The claim-branch scan is CACHED at ~/.claude/.claim-branch-cache.json
        # with a 30-minute TTL, and that cache is read BEFORE ABS_DEV_ROOT is
        # ever consulted. Unpinned, a warm cache splices the operator's real
        # fleet into this fixture's render — measured 2026-09-01: 35 claim
        # branches from `memory-runtime-pro` appeared in a run whose dev root
        # was a 6-repo temp dir, which is what made the digest assertion below
        # fail here and pass on a clean runner. The write leg matters just as
        # much: on a cold cache this test would otherwise persist the FIXTURE's
        # scan into the real cache and blank the operator's live stranded-work
        # verdict for the next half hour.
        claim_cache = tmp / "claim-cache.json"
        # Same hazard, WRITE side: the 24h trend samples. Unpinned, this
        # suite appends fixture counts to the operator's real history and
        # skews the "Δ over 24h" line on a surface a human acts on.
        trend_state = tmp / "drift-trend.json"
        # Third of three, and the one that does real DAMAGE when it leaks: the
        # fetch timestamp is read under a 4h cap, so an unpinned run makes the
        # operator's next real scan skip its fetch and call a PUSHED branch
        # "backed up nowhere" for up to four hours.
        fetch_state = tmp / "drift-fetch.json"
        env = dict(os.environ, ABS_DEV_ROOT=str(dev), DEV_DRIFT_STATE=str(state),
                   DEV_HUB_REFRESH_STATE=str(tmp / "no-such-hub-state.json"),
                   DEV_REPO_SCAN_DIR=str(HOOKS), CLAIM_BRANCH_CACHE_PATH=str(claim_cache),
                   DEV_DRIFT_TREND_STATE=str(trend_state),
                   DEV_DRIFT_FETCH_STATE=str(fetch_state),
                   STANDING_REPORT_STATE_DIR=str(sr_state))
        rep = subprocess.run(
            [sys.executable, str(HOOKS.parent / "scripts" / "dev-drift-report.py"),
             "--write-state"], capture_output=True, text=True, encoding="utf-8", env=env)
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
            input="{}", capture_output=True, text=True, encoding="utf-8", env=env)
        rendered = json.loads(hook.stdout or "{}")
        ctx = (rendered.get("hookSpecificOutput") or {}).get("additionalContext", "")
        check("no-remote" in ctx,
              f"the SessionStart surface did not render the drift section with "
              f"NO hub state present (got {ctx[:200]!r}) — on a fresh install the "
              f"verdict would never reach a human")
        # POSITIVE CONTROL for the pin above. If STANDING_REPORT_STATE_DIR were
        # ignored, the condenser would have written the developer's real
        # ~/.claude/.standing-reports instead and every assertion in this block
        # would once again be decided by unpinned machine state — passing here
        # while proving nothing.
        check((sr_state / "dev-hub-refresh.json").is_file(),
              "the condenser did not write the PINNED state dir — the override "
              "is not binding, so this block is reading live machine state")
        # POSITIVE CONTROL for the claim-cache pin, same reasoning. A miss
        # always writes, so an absent file here means CLAIM_BRANCH_CACHE_PATH was
        # ignored and the real cache was the one consulted.
        check(claim_cache.is_file(),
              "the claim scan did not write the PINNED cache — CLAIM_BRANCH_CACHE_PATH "
              "is not binding, so the render is the operator's real fleet")
        check(trend_state.is_file(),
              "the trend samples did not land in the PINNED file — DEV_DRIFT_TREND_STATE "
              "is not binding, so this run mutated the operator's real history")
        check(fetch_state.is_file(),
              "the fetch cap did not land in the PINNED file — DEV_DRIFT_FETCH_STATE is "
              "not binding, so this run just suppressed the operator's next real fetch")
        check("[claimed-work-stranded]" not in (doc.get("section") or ""),
              "a claim section appeared for a fixture whose branches carry no "
              "ticket ID — the operator's real cached scan leaked into this run")

        # The same finding-set a second time must CONDENSE, not vanish and not
        # repeat in full. This is the behaviour that used to reach the assertion
        # above by accident: the digest drops the per-repo enumeration, so
        # `no-remote` (the repo NAME) disappears while the header sentence still
        # says "no remote" with a space. Asserting it deliberately means CI now
        # exercises the path that was previously only reachable on a dev box.
        hook_again = subprocess.run(
            [sys.executable, str(HOOKS / "dev-hub-refresh-on-session-start.py")],
            input="{}", capture_output=True, text=True, encoding="utf-8", env=env)
        ctx_d = (json.loads(hook_again.stdout or "{}").get(
            "hookSpecificOutput") or {}).get("additionalContext", "")
        check("LOST-WORK" in ctx_d,
              f"an unchanged finding-set went SILENT instead of condensing — "
              f"condensing must never trade coverage for silence (got {ctx_d[:200]!r})")
        check(len(ctx_d) < len(ctx),
              f"the second render did not condense (full {len(ctx)} chars vs "
              f"{len(ctx_d)}) — the digest path is not being exercised")

        # REGRESSION: a render carrying SEVERAL independent sections must keep
        # every one of them in the digest. The hook joins the claim-branch,
        # unpushed-drift and backup-state verdicts into one string, and the
        # condenser summarised only the opener — so on a real machine the
        # LOST-WORK verdict this whole surface exists to deliver went silent on
        # every session after the first, while the digest still looked healthy
        # because the FIRST section was intact. Driven through the hook rather
        # than the condenser directly, because the join happens in the hook.
        # Each section carries a realistic ENUMERATION. A toy fixture is shorter
        # than any 3-line digest, so the never-grow guard hands it back whole
        # and every assertion below passes against the FULL text — vacuous, and
        # indistinguishable from a real pass. The `len(m_dig) < len(m_full)`
        # check on the next lines is what keeps that honest.
        def _rows(label, n):
            return "\n".join(
                f"- `repo-{i}` :: `claude/{label}-{i}` (3 commits, idle 41.2d)"
                for i in range(n))
        multi = ("[claimed-work-stranded]\n\n"
                 "at least 4 branches carry CLAIMED work no other session can act on.\n\n"
                 + _rows("claim", 8) + "\n\n"
                 "[unpushed-commits-surface]\n\n"
                 "2 item(s) at real LOST-WORK risk — push/back-up now.\n\n"
                 + _rows("drift", 8) + "\n\n"
                 "[unverifiable-backup-state]\n\n"
                 "1 replica has not been restore-drilled in 63 days.\n\n"
                 + _rows("backup", 8) + "\n")
        state.write_text(json.dumps({"section": multi, "ts": time.time()}),
                         encoding="utf-8")
        multi_env = dict(env, STANDING_REPORT_STATE_DIR=str(tmp / "sr-multi"))

        def render(e):
            h = subprocess.run(
                [sys.executable, str(HOOKS / "dev-hub-refresh-on-session-start.py")],
                input="{}", capture_output=True, text=True, encoding="utf-8", env=e)
            return (json.loads(h.stdout or "{}").get(
                "hookSpecificOutput") or {}).get("additionalContext", "")

        m_full = render(multi_env)
        m_dig = render(multi_env)
        check(len(m_dig) < len(m_full),
              f"the multi-section render did not condense ({len(m_full)} vs "
              f"{len(m_dig)}) — the digest path is not being exercised")
        for tag in ("[claimed-work-stranded]", "[unpushed-commits-surface]",
                    "[unverifiable-backup-state]"):
            check(tag in m_dig,
                  f"condensing dropped the whole {tag} section — a digest may "
                  f"drop the ENUMERATION, never a verdict")
        check("LOST-WORK" in m_dig,
              f"the LOST-WORK verdict did not survive condensing (got "
              f"{m_dig[:300]!r}) — coverage was traded for silence")

        # An ABSENT state file must be SILENT, never reported as a clean fleet.
        state.unlink()
        hook = subprocess.run(
            [sys.executable, str(HOOKS / "dev-hub-refresh-on-session-start.py")],
            input="{}", capture_output=True, text=True, encoding="utf-8", env=env)
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
