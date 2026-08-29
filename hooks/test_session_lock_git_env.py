#!/usr/bin/env python3
"""Negative control: session-lock's git probes must ignore ambient git env vars.

Run: python3 hooks/test_session_lock_git_env.py   (exit 0 = pass)

git honors the GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE / GIT_COMMON_DIR family
OVER an explicit ``git -C <path>``. session-lock resolves the identity that its
ENTIRE mechanism keys off -- ``_main_root``, via ``_git_common_dir`` -- with
exactly such a ``-C`` call, and ``_current_branch`` compares a branch name across
sessions the same way. So a single leaked var (a git hook context exports GIT_DIR;
a concurrent worktree session can leak one in) silently retargets the probe at a
DIFFERENT repo: the SessionStart sibling warning and the PreToolUse git-mutation
DENY then decide, and write, against the wrong worktree. Bug class
RESOLVES-WRONG-REPO-FROM-AMBIENT-GIT-ENV.

Each case sets a bogus var pointing at a DECOY repo and asserts the probe still
resolves the REAL repo named by ``-C``. Vars are set BEFORE the module is
imported, which is the real-world ordering (the leak predates the hook process)
and the only ordering that also proves the module-level env snapshot is clean.

Pure stdlib. Creates throwaway repos under a temp dir; touches no user state.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session-lock.py")

fails = []


def check(name, got, want):
    ok = (got == want)
    print(("PASS" if ok else "FAIL"), name, "" if ok else ":: got %r want %r" % (got, want))
    if not ok:
        fails.append(name)


def _git(*args, **kw):
    cwd = kw.pop("cwd")
    subprocess.run(
        ["git"] + list(args), cwd=cwd, check=True,
        capture_output=True, encoding="utf-8", errors="replace",
    )


def make_repo(path, branch):
    """A real repo on a known branch, with an identity so commit works anywhere."""
    os.makedirs(path)
    _git("init", "-q", "-b", branch, ".", cwd=path)
    _git("config", "user.email", "t@example.com", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    _git("commit", "-q", "--allow-empty", "-m", "seed", cwd=path)
    return os.path.realpath(path)


class ambient_git_env(object):
    """Set ambient git vars, import a FRESH session-lock, keep the vars set.

    The var must still be set when the probe RUNS -- that is when subprocess
    inherits the environment -- not merely when the module is imported. An
    earlier draft restored the env in a ``finally`` around the import, which
    silently removed the leak before any probe ran and made all 16 cases pass
    against the unfixed file: a control that cannot vary. Restoring only on
    __exit__ keeps the leak live across the assertions.

    Importing per case (module_from_spec yields an isolated object) also proves
    the module-level env snapshot is clean under the realistic ordering, where
    the leak predates the hook process.
    """

    def __init__(self, **env):
        self.env = env
        self.saved = {}

    def __enter__(self):
        for k, v in self.env.items():
            self.saved[k] = os.environ.get(k)
            os.environ[k] = v
        spec = importlib.util.spec_from_file_location("session_lock_git_env_case", HOOK)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def __exit__(self, *exc):
        for k, old in self.saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
        return False


def main():
    tmp = tempfile.mkdtemp(prefix="session-lock-gitenv-")
    try:
        real = make_repo(os.path.join(tmp, "real"), "real-branch")
        decoy = make_repo(os.path.join(tmp, "decoy"), "decoy-branch")
        decoy_git = os.path.join(decoy, ".git")

        # Positive control: with a CLEAN env the probes must already be correct.
        # If this leg fails the harness itself is broken and every result below
        # is meaningless -- a wrong answer here is not evidence about the fix.
        with ambient_git_env() as clean:
            check("control: _git_common_dir resolves -C target (clean env)",
                  clean._git_common_dir(real), os.path.join(real, ".git"))
            check("control: _main_root resolves -C target (clean env)",
                  clean._main_root(real), real)
            check("control: _current_branch resolves -C target (clean env)",
                  clean._current_branch(real), "real-branch")
            # And the decoy must be genuinely distinguishable, or a "correct"
            # answer above could just be both repos looking identical.
            check("control: decoy is distinguishable",
                  clean._current_branch(decoy), "decoy-branch")

        # Harness control: the leak must actually BITE raw git, or the cases
        # below could pass because the var was never effective. This asserts the
        # vulnerability exists in the tool, independently of our code.
        leaked = dict(os.environ, GIT_DIR=decoy_git)
        raw = subprocess.run(
            ["git", "-C", real, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, encoding="utf-8", errors="replace", env=leaked,
        )
        check("control: a leaked GIT_DIR does retarget raw git",
              (raw.stdout or "").strip(), "decoy-branch")

        # The real cases: each leaked var must NOT retarget the probe.
        for var, val in (
            ("GIT_DIR", decoy_git),
            ("GIT_COMMON_DIR", decoy_git),
            ("GIT_WORK_TREE", decoy),
            ("GIT_INDEX_FILE", os.path.join(decoy_git, "index")),
        ):
            with ambient_git_env(**{var: val}) as mod:
                check("%s leak does not retarget _git_common_dir" % var,
                      mod._git_common_dir(real), os.path.join(real, ".git"))
                check("%s leak does not retarget _main_root" % var,
                      mod._main_root(real), real)
                check("%s leak does not retarget _current_branch" % var,
                      mod._current_branch(real), "real-branch")

        # _index_is_empty is the call site that ALREADY passed env=. Cover it so
        # a future "cleanup" cannot quietly drop the one strip that was correct.
        # GIT_INDEX_FILE is the var that bites this probe (GIT_DIR does not
        # meaningfully, since the index lives under the git dir either way).
        _git("commit", "-q", "--allow-empty", "-m", "base", cwd=decoy)
        with open(os.path.join(decoy, "staged.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")
        _git("add", "staged.txt", cwd=decoy)
        decoy_index = os.path.join(decoy_git, "index")

        # Harness control: prove the leak bites raw git here too, else the
        # assertion below is decorative. Asserted as "not clean" rather than a
        # literal rc: measured rc is 128 ("fatal: unable to read <sha>"), not the
        # 1 you would expect, because the decoy index names objects that live in
        # the DECOY's object store. Either way the probe stops reporting clean,
        # which is the property that matters -- pinning 1 here would have made
        # this control fail for the wrong reason.
        leaked_idx = dict(os.environ, GIT_INDEX_FILE=decoy_index)
        raw_idx = subprocess.run(
            ["git", "-C", real, "diff", "--cached", "--quiet"],
            capture_output=True, encoding="utf-8", errors="replace", env=leaked_idx,
        )
        check("control: a leaked GIT_INDEX_FILE stops raw git reporting a clean index",
              raw_idx.returncode != 0, True)

        with ambient_git_env(GIT_INDEX_FILE=decoy_index) as mod:
            check("GIT_INDEX_FILE leak does not retarget _index_is_empty",
                  mod._index_is_empty(real), True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print("FAILED %d case(s): %s" % (len(fails), ", ".join(fails)))
        return 1
    print("all git-env isolation cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
