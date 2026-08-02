#!/usr/bin/env python3
"""Controls for warn-chained-state-command-truncated.py.

Fires on the exact shape that hid the incident: a state-changing command
chained after `&&` in one Bash call whose output is piped through tail/head.
Warn-only, so the assertion is on the presence of the advisory message, and on
the decision NEVER being a deny.

Stdlib only. Exit 0 = all pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "warn-chained-state-command-truncated.py")

PASS = 0
FAIL = 0


def run(command: str, tool_name: str = "Bash", env=None):
    payload = json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})
    proc = subprocess.run([sys.executable, HOOK], input=payload,
                          capture_output=True, text=True, env=env)
    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def expect_warn(label: str, command: str):
    global PASS, FAIL
    d = run(command)
    if not d:
        FAIL += 1
        print(f"FAIL  {label} :: no warning emitted")
        return
    decision = d.get("hookSpecificOutput", {}).get("permissionDecision")
    if decision != "allow":
        FAIL += 1
        print(f"FAIL  {label} :: warn hook must never deny (got {decision!r})")
        return
    PASS += 1
    print(f"PASS  {label}")


def expect_silent(label: str, command: str, tool_name: str = "Bash"):
    global PASS, FAIL
    d = run(command, tool_name=tool_name)
    if d:
        FAIL += 1
        print(f"FAIL  {label} :: false positive")
        return
    PASS += 1
    print(f"PASS  {label}")


# --- THE INCIDENT SHAPE ------------------------------------------------------
expect_warn(
    "1. the incident: add && commit && push && gh pr create | tail -3",
    "git add src/ && git commit -m 'fix' && git push origin br "
    "&& gh pr create --title x --body y | tail -3",
)
expect_warn(
    "2. push after && piped to tail",
    "npm test && git push origin main | tail -5",
)
expect_warn(
    "3. gh pr merge after && piped to head",
    "gh pr checks 144 && gh pr merge 144 --squash | head -2",
)
expect_warn(
    "4. gh release create after && piped to tail",
    "npm run build && gh release create v1.2.3 | tail -1",
)

# --- MUST NOT FIRE -----------------------------------------------------------
expect_silent(
    "5. chained push with NO truncation (full output visible)",
    "npm test && git push origin main",
)
expect_silent(
    "6. truncation but NO chaining (single command)",
    "git log --oneline | tail -20",
)
expect_silent(
    "7. state command FIRST, nothing inferred after it",
    "git push origin main | tail -3",
)
expect_silent(
    "8. read-only chain piped to tail",
    "git fetch && git log --oneline | tail -5",
)
expect_silent(
    "9. non-Bash tool",
    "git add . && git push origin main | tail -3",
    tool_name="Read",
)

# --- DETECTOR C: reading a PAGER's exit code ---------------------------------
# The shape A and B both MISS, because both require `&&`. Observed twice in one
# session (2026-07-29, MYC-3446): a gate and a merge, each piped to tail with
# `$?` read after a `;`. `$?` is the LAST stage's status, tail ~always succeeds,
# so a RED gate reported GREEN. In zsh `${PIPESTATUS[0]}` expands to EMPTY, so
# the `${PIPESTATUS[0]:-$?}` idiom reports the pager too.
expect_warn(
    "11. the incident: ci-test piped to tail, $? read after ;",
    'ci-test 2>&1 | tail -80; echo "CI_TEST_EXIT=$?"',
)
expect_warn(
    "12. the incident: gh pr merge piped to tail, $? read after ;",
    'gh pr merge 911 --squash 2>&1 | tail -8; echo "MERGE_EXIT=$?"',
)
expect_warn(
    "13. cargo test piped to tail with $? read",
    'cargo test --lib 2>&1 | tail -50; echo "EXIT=$?"',
)
expect_warn(
    "14. bash ${PIPESTATUS[0]} spelling (empty in zsh, falls back to pager)",
    'pnpm test 2>&1 | tail -20; echo "${PIPESTATUS[0]:-$?}"',
)
expect_warn(
    "15. make ci piped to head with $? read",
    "make ci 2>&1 | head -30; echo $?",
)

# Detector C must NOT fire once the read is CORRECT, or it would punish a fix.
expect_silent(
    "16. zsh lowercase 1-indexed pipestatus is the correct read",
    'ci-test 2>&1 | tail -80; echo "${pipestatus[1]}"',
)
expect_silent(
    "17. set -o pipefail makes $? the pipeline's real status",
    'set -o pipefail; ci-test 2>&1 | tail -80; echo "$?"',
)
expect_silent(
    "18. gate piped to tail but status never read",
    "cargo test --lib 2>&1 | tail -50",
)
expect_silent(
    "19. status read with NO pipe on the gate (the recommended form)",
    'ci-test > /tmp/gate.log 2>&1; echo "EXIT=$?"',
)
expect_silent(
    "20. non-gate command piped with $? read",
    'ls -la | tail -5; echo "$?"',
)

# --- BYPASS ------------------------------------------------------------------
_env = dict(os.environ, CHAINED_STATE_CMD_BYPASS="1")
if run("git add . && git push origin br && gh pr create | tail -3", env=_env):
    FAIL += 1
    print("FAIL  10. CHAINED_STATE_CMD_BYPASS=1 :: still warned")
else:
    PASS += 1
    print("PASS  10. CHAINED_STATE_CMD_BYPASS=1 disables the warn")

print()
print(f"=== summary: {PASS} passed, {FAIL} failed ===")
sys.exit(1 if FAIL else 0)
