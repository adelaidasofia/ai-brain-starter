#!/usr/bin/env python3
"""A single-branch clone must not be reported as un-backed-up work.

Every backup check in dev_repo_scan is `git log <ref> --not --remotes`, which
asks "is this commit reachable from a LOCAL remote-tracking ref". On a
single-branch clone (`+refs/heads/main:refs/remotes/origin/main` — what
`git clone --single-branch`, `gh repo clone` of a big repo, and most CI
checkouts produce) `git fetch` updates origin/main and nothing else. So every
branch except the default has no tracking ref, no matter how thoroughly it was
pushed, and the check answers "unpushed" when the truth is "this repo cannot
tell you locally".

Found live: two repos each reported 1 un-backed-up commit; `git ls-remote`
showed both commits sitting on origin. 13 of 56 repos on that machine — 23% of
the fleet — had the narrow refspec, so this was not an edge case.

"Unknown" and "at risk" are different messages. Merging them is exactly the
cry-wolf failure MYC-683 exists to prevent, so the fix reports the repo in its
own advisory with a one-command remedy instead of inflating the risk count.

Run: python3 hooks/test_narrow_refspec_falsealarm.py
"""
from __future__ import annotations

import importlib
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


def main() -> int:
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        dev = tmp / "code"
        dev.mkdir()
        os.environ["ABS_DEV_ROOT"] = str(dev)
        import _lib.dev_repo_scan as drs
        importlib.reload(drs)
        drs.CONFIG_PATH = tmp / "cfg.json"

        old = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 3 * 86400))
        env = dict(os.environ, GIT_AUTHOR_DATE=old, GIT_COMMITTER_DATE=old)

        # An origin holding BOTH main and a topic branch — i.e. the topic branch
        # really is backed up.
        origin = tmp / "origin.git"
        origin.mkdir()
        git(origin, "init", "-q", "--bare")

        seed = tmp / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        git(seed, "config", "user.email", "t@example.com")
        git(seed, "config", "user.name", "t")
        (seed / "a.txt").write_text("a", encoding="utf-8")
        git(seed, "add", "a.txt")
        git(seed, "commit", "-q", "-m", "base", env=env)
        git(seed, "remote", "add", "origin", str(origin))
        git(seed, "push", "-q", "origin", "main")
        git(seed, "checkout", "-q", "-b", "claude/topic")
        (seed / "b.txt").write_text("b", encoding="utf-8")
        git(seed, "add", "b.txt")
        git(seed, "commit", "-q", "-m", "pushed topic work", env=env)
        git(seed, "push", "-q", "origin", "claude/topic")

        # NARROW clone: only main is fetched.
        narrow = dev / "narrow"
        assert git(tmp, "clone", "-q", "--single-branch", "--branch", "main",
                   str(origin), str(narrow)).returncode == 0, "narrow clone failed"
        git(narrow, "config", "user.email", "t@example.com")
        git(narrow, "config", "user.name", "t")
        # The topic branch exists locally (checked out from the pushed one) but
        # has NO tracking ref, because the refspec never fetches it.
        git(narrow, "fetch", "-q", "origin", "claude/topic:claude/topic")

        # FULL clone with a genuinely unpushed branch — the true positive that
        # must keep firing, so the fix cannot be "suppress everything".
        full = dev / "full"
        assert git(tmp, "clone", "-q", str(origin), str(full)).returncode == 0
        git(full, "config", "user.email", "t@example.com")
        git(full, "config", "user.name", "t")
        git(full, "checkout", "-q", "-b", "claude/never-pushed")
        (full / "c.txt").write_text("c", encoding="utf-8")
        git(full, "add", "c.txt")
        git(full, "commit", "-q", "-m", "genuinely unpushed", env=env)

        # --- the refspec discriminator itself --------------------------------
        check(not drs.has_full_fetch_refspec(narrow),
              "a --single-branch clone was read as fetching all branches")
        check(drs.has_full_fetch_refspec(full),
              "a normal clone was read as single-branch — this would suppress "
              "REAL un-backed-up findings, the dangerous direction")

        # --- the narrow repo is NOT counted as risk --------------------------
        repos = drs.discover_primary_repos()
        check({p.name for p in repos} == {"narrow", "full"},
              f"discovery unexpected: {sorted(p.name for p in repos)}")

        genuine, n_topic = drs.collect_drift(repos)
        fired = {r for r, _b, _n, _c in genuine}
        check("narrow" not in fired,
              "a single-branch clone whose branch IS on origin was reported as "
              "un-backed-up work — the false alarm this closes")
        # Measure the NARROW repo's own contribution. The fleet total is
        # legitimately 1 here — the full clone really does carry an unpushed
        # branch — so asserting on the total would conflate the two repos.
        _g_narrow, n_topic_narrow = drs._repo_drift(narrow, 24 * 60)
        check(n_topic_narrow == 0,
              f"the narrow clone's PUSHED topic branch was counted as drift "
              f"(its n_topic={n_topic_narrow}); fleet total was {n_topic}")

        # --- the true positive still fires -----------------------------------
        raw = drs._count_topic_branches_with_unpushed(full, "main")
        check(raw >= 1,
              "a genuinely unpushed branch on a FULL clone stopped being "
              "counted — the fix over-suppressed")

        # --- and the repo is surfaced, with a remedy -------------------------
        narrow_repos = drs.narrow_refspec_repos(repos)
        check([p.name for p in narrow_repos] == ["narrow"],
              f"narrow_refspec_repos returned {[p.name for p in narrow_repos]}")
        section = drs.format_narrow_refspec_section(narrow_repos)
        check("narrow" in section, "the advisory does not name the repo")
        check("set-branches" in section,
              "the advisory carries no remedy — a warning without a fix is noise")
        check("unknown" in section.lower(),
              "the advisory does not say the state is UNKNOWN; it must not read "
              "as a risk claim")
        check(drs.format_narrow_refspec_section([]) == "",
              "a healthy fleet still emitted the advisory")

    if failures:
        print("FAILED — narrow-refspec false alarm:")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("OK - narrow refspec: single-branch clone not counted as risk, real "
          "unpushed work still fires, repo surfaced with a one-command remedy")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
