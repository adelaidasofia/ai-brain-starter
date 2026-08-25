#!/usr/bin/env python3
"""Negative controls for block-raw-vault-git.py's `cd` resolution.

THE NEGATIVE CONTROL IS THE POINT. Every "BLOCKS" leg below builds a REAL git
repository for the vault and a REAL second repository standing in for an
unrelated checkout, then asserts the hook refuses. A hook that returned 0
unconditionally would pass only the ALLOW legs and fail every BLOCK leg.

The regression these legs pin down:

  `_effective_cwd` split statements on `&&`/`||`/`;` but NOT on a newline, and
  its `cd` regex anchors at the chunk start. A newline-separated script was
  therefore ONE chunk, so a `cd` was recognised only when it was the very first
  token of the whole command. Any statement before it -- `set -e`, an echo, a
  variable assignment -- made the `cd` invisible, and the hook resolved the
  HARNESS cwd instead of the directory the command actually runs in.

  That disabled the guard outright: from an unrelated repo, `set -e` then a
  `cd` into the vault then a raw staging command reached the vault with no
  mutex, which is the index race this hook exists to prevent.

  The second clause is equally load-bearing. `_targets_vault_repo` fails OPEN
  when it cannot resolve a repo, so once newlines split statements, a bogus
  `cd /nonexistent` line quoted inside a heredoc body becomes reachable and
  would redirect the cwd -- turning a blocked vault op into an allowed one.
  Splitting on newlines WITHOUT ignoring unresolvable `cd` targets is a net
  regression, so leg 6 fails if that clause is ever dropped.

Legs:
   1. BLOCKS a raw mutating op when cwd IS the vault        <- positive control
   2. BLOCKS when `set -e` precedes the cd                  <- the regression
   3. BLOCKS when an echo precedes the cd
   4. BLOCKS when a variable assignment precedes the cd
   5. BLOCKS when the cd is the first line                  <- worked before too
   6. BLOCKS despite a bogus `cd` inside a heredoc body     <- fail-open guard
   7. BLOCKS via the `&&` form                              <- no regression
   8. ALLOWS an unrelated repo reached by a newline cd      <- no false positive
   9. ALLOWS an unrelated repo reached after `set -e`       <- the false positive
  10. ALLOWS read-only ops in the vault

Stdlib only. Exit 0 = all pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "block-raw-vault-git.py")
MUTATE = "git add -A"


def _init_repo(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q", path], check=True,
                   capture_output=True, text=True,
                   encoding="utf-8", errors="replace")


def _blocked(cmd: str, cwd: str, vault: str) -> bool:
    env = dict(os.environ, VAULT_ROOT=vault)
    env.pop("GIT_VAULT_BYPASS", None)
    p = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": cmd},
                          "cwd": cwd}),
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    return p.returncode != 0 or "BLOCKED" in (p.stdout + p.stderr)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        vault = os.path.realpath(os.path.join(tmp, "vault"))
        other = os.path.realpath(os.path.join(tmp, "other-repo"))
        _init_repo(vault)
        _init_repo(other)

        cases = [
            ("raw mutating op, cwd IS the vault", MUTATE, vault, True),
            ("`set -e` precedes the cd", f"set -e\ncd {vault}\n{MUTATE}", other, True),
            ("an echo precedes the cd", f"echo hi\ncd {vault}\n{MUTATE}", other, True),
            ("a variable assignment precedes the cd", f"X=1\ncd {vault}\n{MUTATE}", other, True),
            ("the cd is the first line", f"cd {vault}\n{MUTATE}", other, True),
            ("a bogus cd inside a heredoc must not unblock",
             f"cd {vault}\ncat <<XX\ncd /definitely/not/a/real/dir\nXX\n{MUTATE}", other, True),
            ("the && form still blocks", f"cd {vault} && {MUTATE}", other, True),
            ("unrelated repo reached by a newline cd", f"cd {other}\ngit add file.txt", vault, False),
            ("unrelated repo reached after `set -e`", f"set -e\ncd {other}\ngit add file.txt", vault, False),
            ("read-only op in the vault", "git status", vault, False),
        ]

        failed = 0
        for label, cmd, cwd, must_block in cases:
            got = _blocked(cmd, cwd, vault)
            ok = got == must_block
            failed += not ok
            print(f"{'PASS' if ok else 'FAIL':5} "
                  f"expected={'BLOCK' if must_block else 'ALLOW':5} "
                  f"got={'BLOCK' if got else 'ALLOW':5}  {label}")

        print("\nALL PASS" if not failed else f"\n{failed} FAILING")
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
