#!/usr/bin/env python3
"""Controls for lint-vault-frontmatter.py's frontmatter delimiter gate.

THE NEGATIVE CONTROL IS THE POINT. This hook's failure mode is not "it crashes",
it is "it allows everything" -- it is fail-open by design, so a broken build, a
missing PyYAML, or an over-permissive pattern all look identical to a green run
that only asserts allows. Every leg below that expects ALLOW is therefore paired
with a leg that expects DENY on the same code path.

The regression this locks: #409 fixed the CRLF total-denial incident in
scripts/vault-schema-validator.py, and #431 locked it there with a test. The
delimiter gate in THIS file kept an LF-only pattern. It runs BEFORE the
validator subprocess, so on the Write path -- where `projected` is
tool_input["content"] verbatim, with no universal-newline translation anywhere --
CRLF content is still rejected as "'---' delimiter not properly closed", and the
validator's fix is never reached. Same incident, one layer out.

Legs:
   1. LF frontmatter          -> ALLOW   <- baseline
   2. CRLF frontmatter        -> ALLOW   <- the regression; LF-only pattern fails HERE
   3. Malformed YAML          -> DENY    <- negative control: gate still bites
   4. Unclosed delimiter      -> DENY    <- negative control on this exact pattern
   5. CRLF + malformed YAML   -> DENY    <- the fix widens line endings, not the schema
   6. Non-vault path          -> ALLOW   <- scoped; not every .md is linted
   7. Bypass env              -> ALLOW   <- escape hatch intact

Negative control, verified: reverting the pattern to the LF-only form fails
exactly leg 2 and exits 1; restoring it returns 7/7.

Stdlib only. Exit 0 = all pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "lint-vault-frontmatter.py")

# A path the hook's detect_type() classifies as a journal. Kept ASCII on purpose:
# the emoji-prefixed folder is the real-world shape, but a test that depends on
# console encoding fails for reasons that have nothing to do with the gate.
VAULT_MD = os.path.join(os.sep, "vault", "Journals", "2026-04-30.md")
OUTSIDE_MD = os.path.join(os.sep, "vault", "README.md")

BODY = "creationDate: 2026-04-30\nfloor: 16"
GOOD_LF = "---\n" + BODY + "\n---\n\nbody\n"
GOOD_CRLF = GOOD_LF.replace("\n", "\r\n")
BAD_YAML_LF = "---\ncreationDate: 2026-04-30\nfloor: [unclosed\n---\n\nbody\n"
BAD_YAML_CRLF = BAD_YAML_LF.replace("\n", "\r\n")
UNCLOSED_LF = "---\n" + BODY + "\n\nbody with no closing delimiter\n"


def decide(content: str, path: str = VAULT_MD, env_extra: dict | None = None) -> str:
    """Run the hook and return 'allow', 'deny', or a diagnostic string."""
    env = dict(os.environ)
    env.pop("VAULT_LINT_BYPASS", None)
    if env_extra:
        env.update(env_extra)
    payload = json.dumps(
        {"tool_name": "Write", "tool_input": {"file_path": path, "content": content}}
    )
    proc = subprocess.run(
        [sys.executable, HOOK], input=payload, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=30,
    )
    try:
        out = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return f"NON-JSON stdout={proc.stdout!r} stderr={proc.stderr!r}"
    return out.get("hookSpecificOutput", {}).get("permissionDecision", "MISSING")


def main() -> int:
    legs = [
        ("LF frontmatter allowed", lambda: decide(GOOD_LF), "allow"),
        ("CRLF frontmatter allowed (the regression)", lambda: decide(GOOD_CRLF), "allow"),
        ("malformed YAML denied", lambda: decide(BAD_YAML_LF), "deny"),
        ("unclosed delimiter denied", lambda: decide(UNCLOSED_LF), "deny"),
        ("CRLF + malformed YAML still denied", lambda: decide(BAD_YAML_CRLF), "deny"),
        ("non-vault path allowed", lambda: decide(BAD_YAML_LF, path=OUTSIDE_MD), "allow"),
        ("bypass honored", lambda: decide(BAD_YAML_LF,
                                          env_extra={"VAULT_LINT_BYPASS": "1"}), "allow"),
    ]

    # PyYAML absent means every deny leg silently becomes an allow, which would
    # report a broken gate as a failing fix. Say so instead of guessing.
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("SKIP: PyYAML not installed; the deny legs cannot be exercised.")
        return 0

    failures = 0
    for desc, run, expected in legs:
        actual = run()
        if actual == expected:
            print(f"PASS [{desc}]")
        else:
            failures += 1
            print(f"FAIL [{desc}]: expected {expected}, got {actual}")

    total = len(legs)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
