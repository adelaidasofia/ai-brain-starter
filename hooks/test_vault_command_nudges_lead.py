#!/usr/bin/env python3
"""Controls for vault-command-nudges.py's command-recognition layer (_LEAD).

Every BLOCK case here was measured EXITING 0 -- silently unguarded -- against
the revision this suite landed with. The rm rule had been generalised to
multi-vault targeting, but the push / status / grep / find rules still matched a
bare `git` / `rm` token at a regex-alternation boundary, and the cwd walk was a
quote-blind `re.split`. So a transparent wrapper, a one-shot `VAR=` assignment,
an explicit path to the binary, a subshell, or a `cd` hidden inside a quoted
string was enough to walk straight past all four.

The ALLOW legs are not decoration. A guard that over-blocks teaches people to
reach for the bypass, and the fail-closed cwd candidate SET introduced alongside
_LEAD is exactly the kind of change that over-blocks. Every BLOCK leg is paired
with an ALLOW leg that must stay allowed, so a hook that simply returned 2 for
everything would fail this suite rather than pass it.

Fixtures are built here, not borrowed from the developer's machine: a vault is
"a directory with a Meta-suffixed folder above it" (hooks/_lib/vault_root.py),
so a tmpdir with `Meta/` and a `git init` is a real one. VAULT_ROOT is pointed
somewhere ELSE on purpose -- these rules must resolve the vault PER TARGET, and
pointing the env var at the fixture would let a regression that only ever reads
$VAULT_ROOT pass.

Stdlib only. Exit 0 = all pass.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "vault-command-nudges.py")

PASS = 0
FAIL = 0


def run(command, cwd, hook=HOOK, env=None):
    """Exit code of the hook for one crafted PreToolUse payload.

    The hook only PARSES the command string -- nothing here is ever executed by
    a shell, which is why a `rm -rf <vault>` case is safe to assert on.
    """
    payload = json.dumps({"tool_name": "Bash", "cwd": cwd,
                          "tool_input": {"command": command}})
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        # Deliberately NOT the fixture vault: per-target detection must win.
        "VAULT_ROOT": os.path.join(tempfile.gettempdir(), "vcn-not-a-vault"),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),   # git needs it on Windows
    }
    if env:
        base.update(env)
    # encoding pinned, not left to the locale: a vault path can carry non-ASCII
    # folder names, and on a non-UTF-8 console the default decode would raise
    # for a reason unrelated to the hook. Enforced repo-wide by
    # scripts/check-utf8-subprocess.py.
    proc = subprocess.run([sys.executable, hook], input=payload,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=base, timeout=60)
    return proc.returncode


def check(want_block, label, command, cwd, hook=HOOK, env=None):
    global PASS, FAIL
    rc = run(command, cwd, hook=hook, env=env)
    got_block = (rc == 2)
    if got_block == want_block:
        PASS += 1
        print(f"PASS  {label} :: {'BLOCK' if got_block else 'allow'}")
    else:
        FAIL += 1
        print(f"FAIL  {label} :: got rc={rc} "
              f"({'BLOCK' if got_block else 'allow'}), "
              f"wanted {'BLOCK' if want_block else 'allow'}")


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                   encoding="utf-8", errors="replace", check=False)


TMP = tempfile.mkdtemp(prefix="vcn-lead-")
try:
    # A vault: a Meta-suffixed folder makes vault_root_for() claim it.
    VAULT = os.path.join(TMP, "brain")
    os.makedirs(os.path.join(VAULT, "Meta", "rules"))
    os.makedirs(os.path.join(VAULT, "Journals"))
    git("init", "-q", cwd=VAULT)

    # A SECOND vault, to prove the rules are not pinned to one root.
    VAULT2 = os.path.join(TMP, "other-brain")
    os.makedirs(os.path.join(VAULT2, "Meta"))
    git("init", "-q", cwd=VAULT2)

    # A normal repo with NO Meta folder anywhere above it: the negative control
    # that keeps every "does it block?" answer falsifiable.
    PLAIN = os.path.join(TMP, "plain", "repo")
    os.makedirs(PLAIN)
    git("init", "-q", cwd=PLAIN)

    print("--- git push: the command-recognition layer (all measured rc=0 before) ---")
    check(True,  "01. bare `git push`            [POSITIVE CONTROL]", "git push", VAULT)
    check(True,  "02. `env git push`", "env git push", VAULT)
    check(True,  "03. `sudo git push`", "sudo git push", VAULT)
    check(True,  "04. `command git push`", "command git push", VAULT)
    check(True,  "05. absolute path to the binary", "/usr/bin/git push", VAULT)
    check(True,  "06. one-shot assignment prefix", "FOO=bar git push", VAULT)
    check(True,  "07. lowercase assignment prefix", "foo=bar git push", VAULT)
    check(True,  "08. subshell `(git push)`", "(git push)", VAULT)
    check(True,  "09. stacked wrapper + assignment", "sudo env A=1 git push", VAULT)

    print("--- cwd resolution: a wrong guess used to point off the vault ---")
    check(True,  "10. `$VAR` cd, never expanded before",
          f'W={VAULT}; cd "$W" && git push', TMP)
    check(True,  "11. `cd` inside a QUOTED string is not a cd",
          'echo "hi; cd /tmp" && git push', VAULT)
    check(True,  "12. `||` is not an unconditional cd",
          "cd /nonexistent-xyz || git push", VAULT)
    check(True,  "13. subshell cd does not escape its parens",
          "(cd /tmp; true) && git push", VAULT)

    print("--- unscoped git status ---")
    check(True,  "14. `env git status`", "env git status", VAULT)
    check(True,  "15. `command git status`", "command git status", VAULT)

    print("--- rm -rf: _RM_LEAD covered wrappers but NOT these two ---")
    check(True,  "16. assignment prefix on rm",
          f'FOO=bar rm -rf "{VAULT}"', TMP)
    check(True,  "17. absolute path to rm",
          f'/bin/rm -rf "{VAULT}"', TMP)
    check(True,  "18. vault ROOT, plain          [kept from previous rev]",
          f'rm -rf "{VAULT}"', TMP)
    check(True,  "19. top-level folder, absolute [kept]",
          f'rm -rf "{VAULT}/Meta"', TMP)
    check(True,  "20. a SECOND vault on the machine [kept]",
          f'rm -rf "{VAULT2}"', TMP)
    check(True,  "21. sudo rm -rf vault root     [kept]",
          f'sudo rm -rf "{VAULT}"', TMP)
    check(True,  "22. relative target from a vault cwd",
          "rm -rf Meta", VAULT)
    check(True,  "23. flags AFTER the operand    [kept]",
          f'rm "{VAULT}/Meta" -rf', TMP)

    print("--- grep / find namespace rules ---")
    check(True,  "24. `env grep`", "env grep -r foo .", VAULT)
    check(True,  "25. `sudo find -name`", 'sudo find . -name "*.md"', VAULT)

    print("--- ALLOW legs: a hook that blocked everything would fail here ---")
    check(False, "26. push from a NON-vault repo [NEGATIVE CONTROL]",
          "git push", PLAIN)
    check(False, "27. `env git push` outside any vault", "env git push", PLAIN)
    check(False, "28. scoped `git status -- <path>`",
          'git status -- "Meta/"', VAULT)
    check(False, "29. rm DEEPER than a top-level folder",
          f'rm -rf "{VAULT}/Meta/rules/foo.md"', TMP)
    check(False, "30. `cd <non-vault> && git push` from a vault cwd",
          f'cd "{PLAIN}" && git push', VAULT)
    check(False, "31. grep entirely outside any vault", "grep -r foo .", PLAIN)
    check(False, "32. a push MENTIONED in a comment tail",
          'git status -- "Meta/" # git push', VAULT)
    check(False, "33. a push quoted as an ARGUMENT, not run",
          'echo "later; git push"', PLAIN)
    check(False, "34. documented inline bypass works",
          "VAULT_VALIDATOR_BYPASS=1 git push", VAULT)
    check(False, "35. exported bypass disarms the guard",
          "git push", VAULT, env={"VAULT_VALIDATOR_BYPASS": "1"})

    print("--- heredoc: a BODY is data, never a command ---")
    check(False, "36. `git push` inside a heredoc body",
          'cat > notes.md <<\'BODY\'\ngit push\nBODY\n', PLAIN)
    check(True,  "37. a REAL push after a heredoc closes",
          'cat > notes.md <<\'BODY\'\nhello\nBODY\ngit push\n', VAULT)

    print("--- degraded mode: _lib missing must FAIL CLOSED, not fail open ---")
    # The whole point of the fail-closed policy: if the shared parser cannot be
    # imported, the guard must still stop a vault push rather than wave it
    # through. Proven by running a COPY with shell_parse.py removed.
    degraded = os.path.join(TMP, "degraded")
    os.makedirs(os.path.join(degraded, "_lib"))
    hooks_dir = os.path.dirname(HOOK)
    # Copied file-by-file, NOT with shutil.copytree. copytree owns its own
    # recursion and opens file content internally, which
    # scripts/check-cloud-safe-file-walkers.py flags fleet-wide unless the call
    # surface reaches the shared safe_read primitive. Naming the three files the
    # hook actually needs is also the more precise fixture: it proves
    # shell_parse.py is the ONLY thing absent, rather than copying the whole
    # directory and deleting one entry back out of it.
    for rel in ("vault-command-nudges.py", "_lib/__init__.py", "_lib/vault_root.py"):
        parts = rel.split("/")
        shutil.copy2(os.path.join(hooks_dir, *parts), os.path.join(degraded, *parts))
    dhook = os.path.join(degraded, "vault-command-nudges.py")
    check(True,  "38. degraded parse still blocks a vault push",
          "git push", VAULT, hook=dhook)
    check(False, "39. degraded parse still allows a non-vault push",
          "git push", PLAIN, hook=dhook)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
print(f"=== summary: {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
