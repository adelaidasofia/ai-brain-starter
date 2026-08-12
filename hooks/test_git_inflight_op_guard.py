#!/usr/bin/env python3
"""Controls for block-git-mutation-mid-operation.py.

THE NEGATIVE CONTROL IS THE POINT. A guard earns trust only by failing on the
thing it catches, so every legunder "BLOCKS" below builds a REAL git repository
with a REAL stalled operation (an actual conflicted rebase, an actual conflicted
merge) and asserts the hook refuses. Green tests on the clean path alone would
prove nothing: a hook that returned 0 unconditionally would pass them all.

Reproduces the 2026-07-28 incident shape exactly, including the layout that made
it hard to see -- a working tree whose `.git` is a FILE pointing at a git-dir in
a separate mirror directory, so a naive `<cwd>/.git` lookup finds nothing.

Legs:
   1. BLOCKS `git commit` into a stalled rebase                  <- the incident
   2. SILENT on a clean repo                                     <- no false positive
   3. BLOCKS with a separated git-dir (`.git` is a file)         <- incident layout
   4. BLOCKS `git commit` during a conflicted merge (MERGE_HEAD)
   5. BLOCKS a cherry-pick stopped at conflicts
   6. ALLOWS the resolution verbs (--abort/--continue/--skip)    <- or the repo is trapped
   7. ALLOWS `git tag` / `git branch`                            <- what preserved work
   8. ALLOWS reads (status/log/fetch) mid-operation
   9. Honors `-C <path>`: a stalled repo targeted from a clean cwd still blocks
  10. Detached HEAD alone WARNS, never blocks, with a DIFFERENT message
  11. Bypass works, env and inline
  12. Ignores non-git commands and non-Bash tools
  13. Refusal message carries the required safety text (no rm -rf, --abort is
      the operation owner's call)

Stdlib only. Exit 0 = all pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "block-git-mutation-mid-operation.py")

PASS = 0
FAIL = 0


def ok(msg):
    global PASS
    PASS += 1
    print("PASS  " + msg)


def bad(msg, detail=""):
    global FAIL
    FAIL += 1
    print("FAIL  " + msg + (" :: " + detail if detail else ""))


def git(repo, *args, **kw):
    """Run git in `repo`. check=False by default; returns CompletedProcess."""
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "t")
    env.setdefault("GIT_AUTHOR_EMAIL", "t@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "t")
    env.setdefault("GIT_COMMITTER_EMAIL", "t@example.com")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return subprocess.run(["git", "-C", repo] + list(args),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, **kw)


def run_hook(command, cwd, env=None, tool_name="Bash"):
    """Invoke the hook exactly as Claude Code would. Returns (rc, stdout, stderr)."""
    payload = json.dumps({
        "tool_name": tool_name,
        "cwd": cwd,
        "tool_input": {"command": command},
    })
    e = dict(os.environ)
    e.pop("GIT_INFLIGHT_OP_BYPASS", None)
    if env:
        e.update(env)
    p = subprocess.run([sys.executable, HOOK], input=payload,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=e)
    return p.returncode, p.stdout or "", p.stderr or ""


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def init_repo(path, separate_git_dir=None):
    os.makedirs(path, exist_ok=True)
    args = ["git", "init", "-q"]
    if separate_git_dir:
        args.append("--separate-git-dir=" + separate_git_dir)
    args.append(path)
    subprocess.run(args, capture_output=True, text=True,
                   encoding="utf-8", errors="replace", check=True)
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    git(path, "config", "commit.gpgsign", "false")
    # Deterministic default branch name across git versions.
    git(path, "checkout", "-q", "-B", "main")
    return path


def base_commit(repo, content="1\n"):
    write(os.path.join(repo, "f.txt"), content)
    git(repo, "add", "f.txt")
    git(repo, "commit", "-q", "-m", "base")


def stall_rebase(repo):
    """Leave `repo` with a genuinely paused, conflicted rebase.

    Not a fake marker file: git itself stops here, exactly as it did on
    2026-07-27 23:07. Returns True iff an in-flight marker actually appeared.
    """
    base_commit(repo)
    git(repo, "checkout", "-q", "-b", "topic")
    write(os.path.join(repo, "f.txt"), "topic\n")
    git(repo, "commit", "-q", "-am", "topic change")
    git(repo, "checkout", "-q", "main")
    write(os.path.join(repo, "f.txt"), "main\n")
    git(repo, "commit", "-q", "-am", "main change")
    git(repo, "rebase", "topic")  # conflicts -> stops, HEAD detached
    gd = git(repo, "rev-parse", "--git-dir").stdout.strip()
    gd = os.path.normpath(os.path.join(repo, gd))
    return (os.path.exists(os.path.join(gd, "rebase-merge"))
            or os.path.exists(os.path.join(gd, "rebase-apply")))


def stall_merge(repo):
    base_commit(repo)
    git(repo, "checkout", "-q", "-b", "topic")
    write(os.path.join(repo, "f.txt"), "topic\n")
    git(repo, "commit", "-q", "-am", "topic change")
    git(repo, "checkout", "-q", "main")
    write(os.path.join(repo, "f.txt"), "main\n")
    git(repo, "commit", "-q", "-am", "main change")
    git(repo, "merge", "topic")  # conflicts -> MERGE_HEAD
    gd = os.path.normpath(os.path.join(repo, git(repo, "rev-parse", "--git-dir").stdout.strip()))
    return os.path.exists(os.path.join(gd, "MERGE_HEAD"))


def stall_cherry_pick(repo):
    base_commit(repo)
    git(repo, "checkout", "-q", "-b", "topic")
    write(os.path.join(repo, "f.txt"), "topic\n")
    git(repo, "commit", "-q", "-am", "topic change")
    git(repo, "checkout", "-q", "main")
    write(os.path.join(repo, "f.txt"), "main\n")
    git(repo, "commit", "-q", "-am", "main change")
    git(repo, "cherry-pick", "topic")  # conflicts -> CHERRY_PICK_HEAD
    gd = os.path.normpath(os.path.join(repo, git(repo, "rev-parse", "--git-dir").stdout.strip()))
    return os.path.exists(os.path.join(gd, "CHERRY_PICK_HEAD"))


def stall_sequencer_mid_run(repo):
    """A multi-commit cherry-pick stopped on a conflict, then RESOLVED AND
    COMMITTED. That commit clears CHERRY_PICK_HEAD while the remaining picks
    stay queued, leaving .git/sequencer as the only in-flight evidence.
    Returns True only if that exact state was produced -- sequencer present
    AND every per-stop marker absent -- so the leg cannot pass vacuously."""
    base_commit(repo)
    git(repo, "checkout", "-q", "-b", "topic")
    write(os.path.join(repo, "f.txt"), "one\n")
    git(repo, "commit", "-q", "-am", "pick one")
    write(os.path.join(repo, "f.txt"), "two\n")
    git(repo, "commit", "-q", "-am", "pick two")
    git(repo, "checkout", "-q", "main")
    write(os.path.join(repo, "f.txt"), "main\n")
    git(repo, "commit", "-q", "-am", "main change")
    git(repo, "cherry-pick", "topic~1", "topic")      # stops on the first pick
    write(os.path.join(repo, "f.txt"), "resolved\n")  # resolve + commit it
    git(repo, "add", "f.txt")
    git(repo, "commit", "-q", "-m", "resolved")
    gd = os.path.normpath(os.path.join(repo, git(repo, "rev-parse", "--git-dir").stdout.strip()))
    per_stop = ("CHERRY_PICK_HEAD", "REVERT_HEAD", "MERGE_HEAD",
                "rebase-merge", "rebase-apply", "BISECT_LOG")
    return (os.path.exists(os.path.join(gd, "sequencer"))
            and not any(os.path.exists(os.path.join(gd, m)) for m in per_stop))


def main():
    tmp = tempfile.mkdtemp(prefix="inflight-guard-")

    # ---------------------------------------------------------------- leg 1+2
    # The pair that makes this a control: the SAME command, the SAME hook, one
    # repo stalled and one clean. If both answered the same way the guard would
    # be worthless, and only running both can show that.
    stalled = init_repo(os.path.join(tmp, "stalled"))
    if not stall_rebase(stalled):
        bad("setup", "could not produce a stalled rebase; the control is void")
        return 1
    ok("setup: a real conflicted rebase is paused in the scratch repo")

    rc, out, err = run_hook("git commit -m 'session close'", cwd=stalled)
    if rc == 2 and "IN FLIGHT" in err:
        ok("1. BLOCKS `git commit` into a stalled rebase (the incident)")
    else:
        bad("1. stalled-rebase commit", "rc=%s err=%r" % (rc, err[:200]))

    clean = init_repo(os.path.join(tmp, "clean"))
    base_commit(clean)
    rc, out, err = run_hook("git commit -m 'ordinary work'", cwd=clean)
    if rc == 0 and not out.strip() and not err.strip():
        ok("2. SILENT on a clean repo (no false positive)")
    else:
        bad("2. clean repo", "rc=%s out=%r err=%r" % (rc, out[:120], err[:120]))

    # ------------------------------------------------------------------ leg 3
    # The incident layout: working tree here, git-dir somewhere else entirely.
    sep_wt = os.path.join(tmp, "sep-worktree")
    sep_gd = os.path.join(tmp, "mirrors", "sep")
    os.makedirs(os.path.dirname(sep_gd), exist_ok=True)
    init_repo(sep_wt, separate_git_dir=sep_gd)
    dot_git_is_file = os.path.isfile(os.path.join(sep_wt, ".git"))
    if stall_rebase(sep_wt) and dot_git_is_file:
        rc, out, err = run_hook("git commit -m x", cwd=sep_wt)
        if rc == 2 and sep_gd in err:
            ok("3. BLOCKS with a separated git-dir, and names the real git-dir")
        else:
            bad("3. separated git-dir", "rc=%s err=%r" % (rc, err[:200]))
    else:
        bad("3. separated git-dir setup", "no `.git` file or no stall produced")

    # ---------------------------------------------------------------- leg 4+5
    merged = init_repo(os.path.join(tmp, "merging"))
    if stall_merge(merged):
        rc, _, err = run_hook("git commit -m x", cwd=merged)
        if rc == 2 and "MERGE_HEAD" in err:
            ok("4. BLOCKS during a conflicted merge (MERGE_HEAD)")
        else:
            bad("4. merge", "rc=%s err=%r" % (rc, err[:200]))
    else:
        bad("4. merge setup", "no MERGE_HEAD produced")

    picking = init_repo(os.path.join(tmp, "picking"))
    if stall_cherry_pick(picking):
        rc, _, err = run_hook("git commit -m x", cwd=picking)
        if rc == 2 and "CHERRY_PICK_HEAD" in err:
            ok("5. BLOCKS during a stopped cherry-pick")
        else:
            bad("5. cherry-pick", "rc=%s err=%r" % (rc, err[:200]))
    else:
        bad("5. cherry-pick setup", "no CHERRY_PICK_HEAD produced")

    # ------------------------------------------------------------------ leg 6
    # Blocking these would trap the repo with the bypass as the only exit,
    # which trains everyone to disable the guard.
    trapped = []
    for cmd in ("git rebase --abort", "git rebase --continue", "git rebase --skip",
                "git merge --abort", "git cherry-pick --abort", "git am --skip",
                "git bisect reset"):
        rc, _, err = run_hook(cmd, cwd=stalled)
        if rc != 0:
            trapped.append(cmd)
    if not trapped:
        ok("6. ALLOWS every resolution verb (the repo is never trapped)")
    else:
        bad("6. resolution verbs blocked", ", ".join(trapped))

    # ------------------------------------------------------------------ leg 7
    # A tag anchored an orphan commit and a pushed branch is the only reason
    # six stranded commits survived the incident. Never block either.
    blocked_safe = []
    for cmd in ("git tag anchor/recover-me", "git branch rescue/work"):
        rc, _, err = run_hook(cmd, cwd=stalled)
        if rc != 0:
            blocked_safe.append(cmd)
    if not blocked_safe:
        ok("7. ALLOWS `git tag` / `git branch` (the work-preserving moves)")
    else:
        bad("7. preservation moves blocked", ", ".join(blocked_safe))

    # ------------------------------------------------------------------ leg 8
    blocked_reads = []
    for cmd in ("git status", "git log --oneline -5", "git fetch origin",
                "git branch --contains HEAD", "git diff"):
        rc, _, _ = run_hook(cmd, cwd=stalled)
        if rc != 0:
            blocked_reads.append(cmd)
    if not blocked_reads:
        ok("8. ALLOWS reads mid-operation (recovery starts with reading)")
    else:
        bad("8. reads blocked", ", ".join(blocked_reads))

    # ------------------------------------------------------------------ leg 9
    rc, _, err = run_hook("git -C %s commit -m x" % stalled, cwd=clean)
    if rc == 2:
        ok("9. Honors `-C`: a stalled repo targeted from a clean cwd still blocks")
    else:
        bad("9. -C targeting", "rc=%s (a cross-repo mutation slipped through)" % rc)

    rc, _, _ = run_hook("git -C %s commit -m x" % clean, cwd=stalled)
    if rc == 0:
        ok("9b. Honors `-C`: a CLEAN repo targeted from a stalled cwd is allowed")
    else:
        bad("9b. -C targeting", "rc=%s (blocked on the wrong repo's state)" % rc)

    # 9c. Found by the pre-push doubt-pass, not by a failing test: an inline
    # `GIT_DIR=` retargets git at another repo entirely. Resolving from cwd
    # alone cleared the WRONG repository and ALLOWED the commit -- the
    # false-allow direction, the one that loses work.
    gd_clean = os.path.join(clean, ".git")
    gd_stalled = os.path.join(stalled, ".git")
    rc, _, _ = run_hook("GIT_DIR=%s git commit -m x" % gd_stalled, cwd=clean)
    if rc == 2:
        ok("9c. Honors an inline `GIT_DIR=` retarget (no false allow)")
    else:
        bad("9c. GIT_DIR retarget", "rc=%s -- a mutation into a stalled repo slipped through" % rc)

    rc, _, _ = run_hook("GIT_DIR=%s git commit -m x" % gd_clean, cwd=stalled)
    if rc == 0:
        ok("9d. An inline `GIT_DIR=` pointing at a CLEAN repo is allowed")
    else:
        bad("9d. GIT_DIR retarget", "rc=%s (blocked on the cwd repo, not the target)" % rc)

    # ----------------------------------------------------------------- leg 10
    det = init_repo(os.path.join(tmp, "detached"))
    base_commit(det)
    write(os.path.join(det, "f.txt"), "2\n")
    git(det, "commit", "-q", "-am", "second")
    head = git(det, "rev-parse", "HEAD~1").stdout.strip()
    git(det, "checkout", "-q", head)  # plain detached HEAD, no operation
    rc, out, err = run_hook("git commit -m x", cwd=det)
    if rc == 0 and "DETACHED" in out and "WARNING" in out:
        ok("10. Detached HEAD alone WARNS and does not block (survivable case)")
    else:
        bad("10. detached HEAD", "rc=%s out=%r" % (rc, out[:200]))

    # The two messages must not be interchangeable -- flattening them is what
    # cost six commits.
    _, det_out, _ = run_hook("git commit -m x", cwd=det)
    _, _, stall_err = run_hook("git commit -m x", cwd=stalled)
    if det_out.strip() and stall_err.strip() and det_out.strip() != stall_err.strip():
        if "IN FLIGHT" in stall_err and "IN FLIGHT" not in det_out:
            ok("10b. The detached and in-flight messages are distinct")
        else:
            bad("10b. message distinctness", "the two messages are not clearly separated")
    else:
        bad("10b. message distinctness", "messages identical or empty")

    # ----------------------------------------------------------------- leg 11
    rc, _, _ = run_hook("git commit -m x", cwd=stalled,
                        env={"GIT_INFLIGHT_OP_BYPASS": "1"})
    env_ok = (rc == 0)
    rc, _, _ = run_hook("GIT_INFLIGHT_OP_BYPASS=1 git commit -m x", cwd=stalled)
    inline_ok = (rc == 0)
    if env_ok and inline_ok:
        ok("11. Bypass honored from both the env and an inline prefix")
    else:
        bad("11. bypass", "env=%s inline=%s" % (env_ok, inline_ok))

    # ----------------------------------------------------------------- leg 12
    noise = []
    for cmd in ("ls -la", "echo 'git commit'", "python3 -c 'print(1)'"):
        rc, out, err = run_hook(cmd, cwd=stalled)
        if rc != 0:
            noise.append(cmd)
    # `echo 'git commit'` contains the word git but invokes no git subcommand;
    # a naive substring guard would block it.
    rc, _, _ = run_hook("git commit -m x", cwd=stalled, tool_name="Write")
    if not noise and rc == 0:
        ok("12. Ignores non-git commands, quoted git text, and non-Bash tools")
    else:
        bad("12. noise", "blocked=%s non-bash-rc=%s" % (noise, rc))

    # 12b. Cross-platform: on Windows the command is `git.exe`. A bare
    # basename(tok) != "git" test made the guard silently inert on an entire
    # platform -- worse than not shipping it there. Runs on every OS: the
    # parser is pure string work, so the Windows shape is testable from here.
    # Covers what Git Bash / WSL / PATH invocations actually look like. A
    # Windows absolute path written with BACKSLASHES is a documented gap (the
    # command arrives as a Bash string, where `\` is legitimately an escape);
    # asserting it here would be asserting a behavior the hook deliberately
    # does not have.
    win_forms = [
        "git.exe commit -m x",
        "GIT.EXE commit -m x",
        '"/c/Program Files/Git/cmd/git.exe" commit -m x',
    ]
    missed = []
    for cmd in win_forms:
        rc, _, _ = run_hook(cmd, cwd=stalled)
        if rc != 2:
            missed.append(cmd)
    if not missed:
        ok("12b. Catches `git.exe` / absolute Windows git paths (not inert on Windows)")
    else:
        bad("12b. windows git.exe", "not blocked: %s" % missed)

    # And the negative side: a program merely NAMED like git must not match.
    rc, _, _ = run_hook("gitk --all", cwd=stalled)
    rc2, _, _ = run_hook("git-foo commit", cwd=stalled)
    if rc == 0 and rc2 == 0:
        ok("12c. `gitk` / `git-foo` are not mistaken for git")
    else:
        bad("12c. git-lookalike", "gitk rc=%s git-foo rc=%s" % (rc, rc2))

    # ----------------------------------------------------------------- leg 13
    _, _, err = run_hook("git commit -m x", cwd=stalled)
    required = [
        ("rm -rf warning", "rm -rf"),
        ("autostash named", "AUTOSTASH"),
        ("abort-vs-continue is the owner's call", "belongs to whoever owns"),
        ("bypass documented", "GIT_INFLIGHT_OP_BYPASS=1"),
    ]
    missing = [name for name, needle in required if needle not in err]
    if not missing:
        ok("13. Refusal message carries every required safety line")
    else:
        bad("13. refusal message", "missing: " + ", ".join(missing))

    # 13b. Regression: the resolution hint must name the verb that owns the
    # IN-FLIGHT operation, not the verb that got blocked. The first cut of this
    # hook interpolated the blocked subcommand and printed `git commit --abort`
    # -- a command that does not exist -- to someone mid-incident.
    if "git rebase --abort" in err and "git commit --abort" not in err:
        ok("13b. Resolution hint names the in-flight op's verb, not the blocked one")
    else:
        bad("13b. resolution hint", "wrong verb in hint: %r" % err[-260:])

    _, _, merr = run_hook("git commit -m x", cwd=merged)
    if "git merge --abort" in merr and "git rebase --abort" not in merr:
        ok("13c. Resolution hint tracks the operation (merge -> `git merge --abort`)")
    else:
        bad("13c. resolution hint (merge)", "hint did not follow the marker")

    # 14. Regression: a multi-commit cherry-pick sequence whose conflict has
    # been RESOLVED AND COMMITTED. git clears CHERRY_PICK_HEAD at that commit
    # but leaves the remaining picks queued in .git/sequencer, so every
    # per-stop marker is gone while the operation is still in flight. Before
    # `sequencer` joined IN_FLIGHT_MARKERS the guard went silent here and a
    # commit could land inside someone else's unfinished sequence.
    seq = init_repo(os.path.join(tmp, "sequencer"))
    if stall_sequencer_mid_run(seq):
        ok("14. setup: sequence mid-run, CHERRY_PICK_HEAD already cleared")
        code, _, serr = run_hook("git commit -m x", cwd=seq)
        if code == 2:
            ok("14a. Guard still BLOCKS mid-sequence (sequencer alone)")
        else:
            bad("14a. mid-sequence", "guard went silent: exit %s" % code)
        if "cherry-pick" in serr:
            ok("14b. Hint names cherry-pick as the in-flight op")
        else:
            bad("14b. mid-sequence hint", "wrong verb: %r" % serr[-200:])
    else:
        bad("14. setup", "could not produce a mid-run sequencer state")

    subprocess.run(["rm", "-rf", tmp], capture_output=True)

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
