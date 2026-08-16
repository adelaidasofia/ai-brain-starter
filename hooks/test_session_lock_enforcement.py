#!/usr/bin/env python3
"""Unit regression suite for session-lock.py's PreToolUse enforcement resolver.

Run: python3 hooks/test_session_lock_enforcement.py   (exit 0 = pass)

Exercises ``_is_home_repo_git_mutation`` across the matrix the
SIBLING-SESSION-FALSE-BLOCK ticket family hardened:
  * read-only vs mutating git subcommands (commit/push/reset vs status/log/diff)
  * cross-repo attribution via ``-C`` and the git-dir (``--git-dir`` flag or
    ``GIT_DIR=`` env) — the git-dir is the collision surface (HEAD/index/refs),
    so a cross-repo git-dir must NOT false-block and a home git-dir MUST block.
    ``--work-tree`` is deliberately NOT a redirect: a ``--work-tree=/other``
    mutation still writes the home git-dir, so it must still block (false-ALLOW
    avoidance)
  * in-command ``cd`` tracking (``cd /other && git commit`` is attributed to
    /other; a ``cd`` back into home re-attributes)
  * unresolved ``$VAR`` redirects + the unbalanced-quote coarse fallback that a
    multi-line ``-m`` message triggers

Pure logic — no git, no filesystem, no live sibling needed. The end-to-end exit
codes (BLOCKED / HEADS-UP / bypass) are covered by the bash wrapper in
tests/integration/test_session_coordination_guards.sh.
"""
import importlib.util
import os
import shlex
import sys

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session-lock.py")
_spec = importlib.util.spec_from_file_location("session_lock_under_test", HOOK)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

HOME = "/home/proj"
OTHER = "/home/other"
ML = 'git commit -m "line one\nline two"'          # multi-line msg => coarse branch (home)
ML_C = 'git -C /home/other commit -m "l1\nl2"'      # multi-line + -C at other repo
ML_VAR_C = 'git -C "$d" commit -m "l1\nl2"'         # multi-line + $VAR -C (must let through)

fails = []


def check(name, got, want):
    ok = (got == want)
    print(("PASS" if ok else "FAIL"), name, "" if ok else f":: got {got!r} want {want!r}")
    if not ok:
        fails.append(name)


def mut(cmd, cwd=HOME, home=HOME):
    return mod._is_home_repo_git_mutation(cmd, cwd, home)


# (label, command, cwd, home, expected)
CASES = [
    # --- bare mutations attributed to cwd / home ---
    ("bare commit in home -> block", "git commit -m x", HOME, HOME, True),
    ("bare push in home -> block", "git push", HOME, HOME, True),
    ("reset --hard in home -> block", "git reset --hard HEAD~1", HOME, HOME, True),
    ("bare commit in a non-home cwd -> allow", "git commit -m x", OTHER, HOME, False),
    # --- read-only git is never a mutation ---
    ("status -> allow", "git status", HOME, HOME, False),
    ("log -> allow", "git log --oneline", HOME, HOME, False),
    ("diff -> allow", "git diff", HOME, HOME, False),
    ("fetch -> allow", "git fetch origin", HOME, HOME, False),
    # --- -C redirect ---
    ("-C cross-repo -> allow", "git -C /home/other commit -m x", HOME, HOME, False),
    ("-C within home -> block", "git -C /home/proj/sub commit -m x", HOME, HOME, True),
    ("-C unresolved $VAR -> allow", 'git -C "$VAR" commit', HOME, HOME, False),
    # --- git-dir IS the collision surface; --work-tree is NOT a redirect ---
    ("--git-dir cross-repo -> allow", "git --git-dir=/home/other/.git commit -m x", HOME, HOME, False),
    ("--git-dir home -> block", "git --git-dir=/home/proj/.git commit -m x", HOME, HOME, True),
    ("--git-dir space-form cross-repo -> allow",
     "git --git-dir /home/other/.git commit -m x", HOME, HOME, False),
    ("GIT_DIR= env cross-repo -> allow", "GIT_DIR=/home/other/.git git commit -m x", HOME, HOME, False),
    ("GIT_DIR= env home -> block", "GIT_DIR=/home/proj/.git git commit -m x", HOME, HOME, True),
    ("--work-tree=/other but home git-dir -> BLOCK (false-ALLOW avoided)",
     "git --work-tree=/home/other commit", HOME, HOME, True),
    ("--work-tree home -> block", "git --work-tree=/home/proj commit", HOME, HOME, True),
    ("--git-dir cross + --work-tree home -> allow (git-dir wins)",
     "git --git-dir=/home/other/.git --work-tree=/home/proj commit", HOME, HOME, False),
    # --- cd tracking across compound commands ---
    ("cd other && commit -> allow", "cd /home/other && git commit -m x", HOME, HOME, False),
    ("cd other then back home && commit -> block",
     "cd /home/other && cd /home/proj && git commit -m x", HOME, HOME, True),
    ("cd unknown $VAR && bare commit -> allow", 'cd "$X" && git commit -m y', HOME, HOME, False),
    ("cd other && -C home commit -> block (-C wins)",
     "cd /home/other && git -C /home/proj commit", HOME, HOME, True),
    ("subshell ( cd other && commit ) -> allow",
     "( cd /home/other && git commit -m x )", HOME, HOME, False),
    # --- branch / stash / tag mutation classification ---
    ("branch -D -> block", "git branch -D feature", HOME, HOME, True),
    ("branch list -> allow", "git branch", HOME, HOME, False),
    ("branch --list -> allow", "git branch --list", HOME, HOME, False),
    ("stash (push) -> block", "git stash", HOME, HOME, True),
    ("stash list -> allow", "git stash list", HOME, HOME, False),
    ("tag create -> block", "git tag v1.0", HOME, HOME, True),
    ("tag -l -> allow", "git tag -l", HOME, HOME, False),
    # --- wrappers + config opt ---
    ("env wrapper commit in home -> block", "env FOO=1 git commit -m x", HOME, HOME, True),
    ("sudo wrapper + -C cross-repo -> allow", "sudo git -C /home/other commit", HOME, HOME, False),
    ("-c config (not a redirect) + home commit -> block",
     "git -c user.name=x commit -m y", HOME, HOME, True),
    # --- non-git noise ---
    ("ls -> allow", "ls -la", HOME, HOME, False),
    ("echo containing 'git commit' -> allow", "echo git commit", HOME, HOME, False),
    # --- unbalanced-quote coarse fallback (multi-line -m) ---
    ("multi-line -m commit in home -> block (coarse)", ML, HOME, HOME, True),
    ("multi-line -m + -C cross-repo -> allow (coarse -C escape)", ML_C, HOME, HOME, False),
    ("multi-line -m + $VAR -C -> allow (coarse $VAR escape)", ML_VAR_C, HOME, HOME, False),
    # --- GIT_DIR= env form in the coarse multi-line -m branch ---
    ("GIT_DIR= env + multi-line, cross-repo -> allow (coarse mge)",
     'GIT_DIR=/home/other/.git git commit -m "l1\nl2"', HOME, HOME, False),
    ("GIT_DIR= env + multi-line, home -> block (coarse)",
     'GIT_DIR=/home/proj/.git git commit -m "l1\nl2"', HOME, HOME, True),
    ("FOO=bar GIT_DIR= prefix + multi-line, cross-repo -> allow",
     'FOO=bar GIT_DIR=/home/other/.git git commit -m "l1\nl2"', HOME, HOME, False),
    ("GIT_DIR= only INSIDE the message -> still blocks home commit (no false-ALLOW)",
     'git commit -m "ref GIT_DIR=/home/other/.git here\nl2"', HOME, HOME, True),
]

for label, cmd, cwd, home, want in CASES:
    check(label, mut(cmd, cwd, home), want)

# --- _git_mutation_target (cdir, gdir) unit checks ---
# These assert the REDIRECT fields only. _git_mutation_target also carries the
# subcommand and its remaining tokens (the scoped-commit exemption needs both), so
# comparing the whole tuple made all six fail on a change that did not touch
# redirect detection at all. Slicing to [:3] keeps them testing what they are named
# for instead of tracking the tuple's width; the carried fields get their own check.
def redirect(cmd):
    res = mod._git_mutation_target(shlex.split(cmd))
    return res if res is False else tuple(res[:3])


check("gmt: -C captured as cdir, --work-tree ignored",
      redirect("git -C /x --work-tree /y commit"), (True, "/x", None))
check("gmt: --git-dir captured as gdir, --work-tree ignored",
      redirect("git --work-tree /y --git-dir /z/.git commit"), (True, None, "/z/.git"))
check("gmt: --git-dir= form -> gdir",
      redirect("git --git-dir=/z/.git commit"), (True, None, "/z/.git"))
check("gmt: GIT_DIR= env -> gdir",
      redirect("GIT_DIR=/z/.git git commit"), (True, None, "/z/.git"))
check("gmt: -C + --git-dir both captured",
      redirect("git -C /x --git-dir /z/.git commit"), (True, "/x", "/z/.git"))
check("gmt: no redirect -> (True,None,None)",
      redirect("git commit -m x"), (True, None, None))
check("gmt: read-only -> False", redirect("git status"), False)
check("gmt: carries the subcommand + its remaining tokens",
      mod._git_mutation_target(shlex.split("git commit -o A.txt -m x"))[3:],
      ("commit", ["-o", "A.txt", "-m", "x"]))

# --- scoped-commit exemption: `git commit -o <literal paths>` with an empty index -
# `-o`/`--only` commits the working-tree contents of the NAMED paths and ignores the
# index, so it cannot sweep a sibling's staged work — the harm this gate exists to
# stop. Everything else about commit stays blocked. The index probe is injected so
# these stay pure; empty=False simulates a sibling having staged something.
def blocked(cmd, empty=True, cwd=HOME):
    return mod._is_home_repo_git_mutation(cmd, cwd, HOME, index_probe=lambda d: empty)


SCOPED_ALLOWED = [
    "git commit -o A.txt -m x",
    "git commit --only A.txt -m x",
    "git commit -o A.txt B.txt C.txt -F /tmp/msg",
    "git commit -o -- A.txt",
    "git commit -oq A.txt -m x",
    'git commit -om x A.txt',                 # -om == -o -m, so x is the MESSAGE
    "git commit -o notes/ -m x",
    "git commit -o A.txt -uno -m x",
]
# Each of these would be ALLOWED by a parser that reads an option's VALUE as a
# pathspec — the one catastrophic direction, so most of the matrix pins it.
SCOPED_BLOCKED = [
    "git commit -m x",                        # bare
    "git commit -o -m x",                     # -m eats the only positional
    "git commit -om x",                       # cluster: x is the message
    "git commit -o -F /tmp/msg",
    "git commit -o -C HEAD",
    "git commit -o --message x",
    "git commit -o --pathspec-from-file /tmp/l",
    "git commit -o",
    "git commit -o -a A.txt -m x",            # -a widens to the whole tree
    "git commit -o -i A.txt -m x",            # --include ADDS the index
    "git commit -o --amend A.txt -m x",       # measured: --amend takes the index
    "git commit -o -p A.txt",
    "git commit -o . -m x",                   # sweeps files you did not name
    "git commit -o '*.md' -m x",
    "git commit -o ':/' -m x",
    "git commit -o A.txt . -m x",             # one bad pathspec poisons it
    "git commit A.txt -m x",                  # pathspecs but no explicit -o
    "git commit -o --frobnicate A.txt",       # unclassified option -> cannot prove
    "git push -o ci.skip",                    # -o is a PUSH option, not scoping
    "git reset --hard",                       # no other verb is ever exempt
    'git commit -o A.txt -m "l1\nl2"',        # multi-line -m: unparseable
]
for _c in SCOPED_ALLOWED:
    check(f"scoped ALLOWED: {_c!r}", blocked(_c), False)
for _c in SCOPED_BLOCKED:
    check(f"scoped BLOCKED: {_c!r}", blocked(_c), True)
# the index condition, both directions on the SAME command
check("scoped commit BLOCKED when the index is not empty",
      blocked("git commit -o A.txt -m x", empty=False), True)
check("scoped commit ALLOWED when the index is empty",
      blocked("git commit -o A.txt -m x", empty=True), False)
# an explicit git-dir withholds the exemption: the index we probe may not be the
# one git commits against
check("scoped commit with explicit --git-dir at home BLOCKED",
      blocked(f"git --git-dir={HOME}/.git commit -o A.txt -m x"), True)
check("scoped commit at ANOTHER repo still allowed",
      blocked(f"git -C {OTHER} commit -o A.txt -m x"), False)

# --- gate liveness: can the enforcement arm fire in this repo at all? ------------
# A mirror / --separate-git-dir layout puts main_root OUTSIDE the checkout, so
# nothing ever attributes as "this repo" and the gate is dormant while the
# once-per-session heads-up still fires, making it look alive.
check("gate active: in-tree .git (cwd == main_root)", mod._git_gate_active(HOME, HOME), True)
check("gate active: cwd below main_root", mod._git_gate_active(HOME + "/src", HOME), True)
check("gate DORMANT: git dir outside the checkout",
      mod._git_gate_active("/home/proj", "/home/mirrors/proj"), False)
check("gate DORMANT: sibling-prefix path is not containment",
      mod._git_gate_active(HOME + "-other/src", HOME), False)
check("gate active: unknown inputs never claim breakage", mod._git_gate_active("", ""), True)

print(f"\n=== {len(CASES) + 8 + len(SCOPED_ALLOWED) + len(SCOPED_BLOCKED) + 9} "
      f"assertions, {len(fails)} failed ===")
sys.exit(1 if fails else 0)
