#!/usr/bin/env python3
"""Controls for the recognition layer of the two LIVE vault git guards.

block-raw-vault-git.py and block-vault-git-fullwalk.py are the hooks a user
actually runs: both activate through the phase-doc channel and are wired in
hooks.json. vault-command-nudges.py, which carries the same two rules, is
documented-dormant. So this suite is the one that covers the deployed surface,
and hooks/test_vault_command_nudges_lead.py covers the dormant sibling.

Every BLOCK leg here was measured EXITING 0 before the fix, each against a
proven positive control (a bare `git commit -m x` and a bare `git add -A` from
a vault cwd both exit 2). Both hooks recognised their verb as a BARE token at a
boundary alternation that admitted only UPPERCASE assignments, and both resolved
cwd with a quote-blind split whose `cd` match anchored at chunk start:

    env / sudo / command / /usr/bin/git <verb>      -> 0
    foo=1 git <verb>          (lowercase assign)    -> 0
    (git <verb>)              (subshell)            -> 0
    cd /nonexistent || git add -A                   -> 0

The ALLOW legs are half the point. These guards sit in front of the commands
people run constantly, and the fail-closed cwd candidate SET is exactly the kind
of change that starts over-blocking -- which is what teaches people to reach for
the bypass. A hook that returned 2 unconditionally fails this suite.

A vault is "a directory with a Meta-suffixed folder above it"
(hooks/_lib/vault_root.py), so a tmpdir with `Meta/` and a `git init` is a real
one. VAULT_ROOT is pointed somewhere ELSE on purpose: these rules must resolve
the vault PER TARGET, and a regression that only reads $VAULT_ROOT must not pass.

Stdlib only. Exit 0 = all pass.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HOOKS = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HOOKS, "block-raw-vault-git.py")
FULLWALK = os.path.join(HOOKS, "block-vault-git-fullwalk.py")

PASS = 0
FAIL = 0


def run(hook, command, cwd, env=None):
    """Exit code of one hook for one crafted PreToolUse payload.

    The hook only PARSES the string -- nothing is executed by a shell, which is
    why a `git add -A` case is safe to assert on.
    """
    payload = json.dumps({"tool_name": "Bash", "cwd": cwd,
                          "tool_input": {"command": command}})
    # USERPROFILE is set alongside HOME, never instead of it: Path.home() reads
    # USERPROFILE on Windows, so a HOME-only redirect would silently run the
    # child against the REAL ~/.claude there while looking sandboxed on POSIX.
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or tempfile.gettempdir()
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": home,
        "USERPROFILE": home,
        "VAULT_ROOT": os.path.join(tempfile.gettempdir(), "lvg-not-a-vault"),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),   # git needs it on Windows
    }
    if env:
        base.update(env)
    # encoding pinned, not left to the locale: vault paths can carry non-ASCII
    # folder names. Enforced repo-wide by scripts/check-utf8-subprocess.py.
    proc = subprocess.run([sys.executable, hook], input=payload,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=base, timeout=60)
    return proc.returncode


def check(want_block, label, hook, command, cwd, env=None):
    global PASS, FAIL
    rc = run(hook, command, cwd, env=env)
    got = (rc == 2)
    if got == want_block:
        PASS += 1
        print(f"PASS  {label} :: {'BLOCK' if got else 'allow'}")
    else:
        FAIL += 1
        print(f"FAIL  {label} :: got rc={rc} ({'BLOCK' if got else 'allow'}), "
              f"wanted {'BLOCK' if want_block else 'allow'}")


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                   encoding="utf-8", errors="replace", check=False)


TMP = tempfile.mkdtemp(prefix="lvg-lead-")
try:
    VAULT = os.path.join(TMP, "brain")
    os.makedirs(os.path.join(VAULT, "Meta"))
    git("init", "-q", cwd=VAULT)

    PLAIN = os.path.join(TMP, "plain", "repo")   # no Meta anywhere above it
    os.makedirs(PLAIN)
    git("init", "-q", cwd=PLAIN)

    print("--- block-raw-vault-git: recognition layer ---")
    check(True,  "01. bare `git commit`     [POSITIVE CONTROL]", RAW,
          "git commit -m x", VAULT)
    check(True,  "02. env wrapper", RAW, "env git commit -m x", VAULT)
    check(True,  "03. sudo wrapper", RAW, "sudo git commit -m x", VAULT)
    check(True,  "04. wrapper with its own flag+value", RAW,
          "sudo -u root git commit -m x", VAULT)
    check(True,  "05. absolute path to the binary", RAW,
          "/usr/bin/git commit -m x", VAULT)
    check(True,  "06. command wrapper", RAW, "command git commit -m x", VAULT)
    check(True,  "07. LOWERCASE assignment prefix", RAW,
          "foo=1 git commit -m x", VAULT)
    check(True,  "08. subshell", RAW, "(git commit -m x)", VAULT)
    check(True,  "09. brace group (runs in the CURRENT shell)", RAW,
          "{ git commit -m x; }", VAULT)
    check(True,  "10. `$VAR` cd, never expanded before", RAW,
          f'W={VAULT}; cd "$W" && git add .', TMP)
    check(True,  "11. `cd` inside a QUOTED string is not a cd", RAW,
          'echo "hi; cd /tmp" && git commit -m x', VAULT)

    print("--- block-raw-vault-git: ALLOW legs ---")
    check(False, "12. non-vault repo         [NEGATIVE CONTROL]", RAW,
          "git commit -m x", PLAIN)
    check(False, "13. read-only git is not mutating", RAW,
          "git log --oneline -1", VAULT)
    check(False, "14. the sanctioned wrapper is still allowed", RAW,
          'bash "Meta/scripts/vault-safe-commit.sh" "msg" a.md', VAULT)
    check(False, "15. inline bypass on the segment", RAW,
          "GIT_VAULT_BYPASS=1 git commit -m x", VAULT)
    check(False, "16. exported bypass", RAW, "git commit -m x", VAULT,
          env={"GIT_VAULT_BYPASS": "1"})
    check(False, "17. verb quoted as an argument", RAW,
          'echo "later; git commit -m x"', VAULT)
    check(False, "18. verb inside a heredoc BODY", RAW,
          'cat > n.md <<\'XX\'\ngit commit -m x\nXX\n', VAULT)
    # A bare trailing assignment sets only a SHELL variable -- unexported, so it
    # never reaches a child's environment and cannot be a bypass for one. The
    # op it "bypasses" has already run by the time the shell reaches the token.
    check(True,  "19. trailing bypass does NOT disarm a prior op", RAW,
          "git commit -m x ; GIT_VAULT_BYPASS=1", VAULT)

    print("--- block-vault-git-fullwalk: recognition layer ---")
    check(True,  "20. bare `git add -A`     [POSITIVE CONTROL]", FULLWALK,
          "git add -A", VAULT)
    check(True,  "21. env wrapper", FULLWALK, "env git add -A", VAULT)
    check(True,  "22. sudo wrapper", FULLWALK, "sudo git add -A", VAULT)
    check(True,  "23. wrapper with its own flag+value", FULLWALK,
          "sudo -u root git add -A", VAULT)
    check(True,  "24. absolute path to the binary", FULLWALK,
          "/usr/bin/git add -A", VAULT)
    check(True,  "25. LOWERCASE assignment prefix", FULLWALK,
          "foo=1 git add -A", VAULT)
    check(True,  "26. subshell", FULLWALK, "(git add -A)", VAULT)
    check(True,  "27. brace group", FULLWALK, "{ git add -A; }", VAULT)
    check(True,  "28. `||` is not an unconditional cd", FULLWALK,
          "cd /nonexistent-xyz || git add -A", VAULT)
    check(True,  "29. `git add --all`", FULLWALK, "git add --all", VAULT)
    check(True,  "30. lone dot", FULLWALK, "git add .", VAULT)
    check(True,  "31. lone dot with a redirect", FULLWALK,
          "git add . 2>/dev/null", VAULT)

    print("--- block-vault-git-fullwalk: ALLOW legs ---")
    check(False, "32. non-vault repo         [NEGATIVE CONTROL]", FULLWALK,
          "git add -A", PLAIN)
    check(False, "33. `git add ./relative/path` is not a lone dot", FULLWALK,
          "git add ./relative/path", VAULT)
    check(False, "34. `git add .gitignore` is not a lone dot", FULLWALK,
          "git add .gitignore", VAULT)
    check(False, "35. explicit paths are the recommended form", FULLWALK,
          'git add "Meta/file.md"', VAULT)
    check(False, "36. mention inside a commit message", FULLWALK,
          'git commit -m "ran git add -A once"', VAULT)
    check(False, "37. inside a heredoc BODY", FULLWALK,
          'cat > n.md <<\'XX\'\ngit add -A\nXX\n', VAULT)
    check(False, "38. quoted as an argument", FULLWALK,
          'echo "later; git add -A"', VAULT)
    check(False, "39. `cd <non-vault> && git add -A` from a vault cwd", FULLWALK,
          f'cd "{PLAIN}" && git add -A', VAULT)

    print("--- degraded mode: a missing _lib must FAIL CLOSED, both hooks ---")
    degraded = os.path.join(TMP, "degraded")
    os.makedirs(os.path.join(degraded, "_lib"))
    # Copied file-by-file, NOT with shutil.copytree: copytree owns its own
    # recursion and reads content internally, which
    # scripts/check-cloud-safe-file-walkers.py flags fleet-wide unless it
    # reaches the shared safe_read primitive. Naming the files is also the more
    # precise fixture -- it proves shell_parse.py is the ONLY thing absent.
    for rel in ("block-raw-vault-git.py", "block-vault-git-fullwalk.py",
                "_lib/__init__.py", "_lib/vault_root.py"):
        parts = rel.split("/")
        shutil.copy2(os.path.join(HOOKS, *parts), os.path.join(degraded, *parts))
    d_raw = os.path.join(degraded, "block-raw-vault-git.py")
    d_full = os.path.join(degraded, "block-vault-git-fullwalk.py")
    check(True,  "40. raw guard still blocks in the vault", d_raw,
          "git commit -m x", VAULT)
    check(False, "41. raw guard still allows outside it", d_raw,
          "git commit -m x", PLAIN)
    check(True,  "42. fullwalk still blocks in the vault", d_full,
          "git add -A", VAULT)
    check(False, "43. fullwalk still allows outside it", d_full,
          "git add -A", PLAIN)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
print(f"=== summary: {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
