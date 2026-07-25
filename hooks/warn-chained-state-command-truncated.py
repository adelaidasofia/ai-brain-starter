#!/usr/bin/env python3
"""PreToolUse(Bash) warn. Two mirror shapes in which a pipe hides a failure.

A. A state-changing command AFTER `&&`, with the pipeline piped to `tail`/`head`.
B. A VERIFICATION GATE (typecheck / test / lint / build) piped to `tail`/`head`
   BEFORE `&&`. A pipeline exits with its LAST command's status, so `tail`
   returns 0 over a failing gate and `&&` runs the rest as though it passed.
   The truncation compounds it by dropping the very line ("error TS…", "FAIL")
   a later grep would look for. Observed in the wild: a branch that did not
   compile was reported green across typecheck, lint and a full 4,600-test
   suite, because the test runner strips types and never typechecks, so the
   passing tests said nothing about compilation.

That exact shape hides failure. `&&` short-circuits, so an early step's denial
or error kills the rest of the chain — and `tail -3` then shows only the last
few lines, which are the ERROR text of whichever step died. The agent reads a
plausible-looking tail, infers every earlier step succeeded, and reports the
whole chain as landed. Nothing in the visible output says otherwise.

Warn only, never block: the shape is often legitimate (a long build piped to
`tail` for brevity). The point is to make the agent read the exit status and
re-read the remote instead of trusting the tail. The Stop-side guard
(check-fabricated-verification.py, Detector C) is what actually enforces the
read-of-record before a landed-state claim.

Bypass: CHAINED_STATE_CMD_BYPASS=1.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Fire telemetry (MYC-285). Fail-open: a missing _lib must never break the
# guard or its tests.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "_lib"))
    from guard_telemetry import log_fire
except Exception:  # pragma: no cover - telemetry is never load-bearing
    def log_fire(*_a, **_k):
        return

# State-changing commands whose success cannot be inferred from a truncated tail.
STATE_CMD = re.compile(
    r"\bgit\s+commit\b|\bgit\s+push\b|\bgh\s+pr\s+create\b|\bgh\s+pr\s+merge\b"
    r"|\bgh\s+release\s+create\b|\bgit\s+tag\s+-\w*\s*\S|\bgh\s+api\s+.*-X\s*(?:POST|PATCH|PUT|DELETE)",
    re.IGNORECASE,
)
# Verification gates whose ONLY trustworthy verdict is their exit status.
VERIFY_CMD = re.compile(
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:typecheck|type-check|lint|test|build)\b"
    r"|\btsc\b|\beslint\b|\bvitest\b|\bjest\b|\bpytest\b|\bmypy\b|\bruff\b"
    r"|\bcargo\s+(?:test|clippy|check)\b|\bgo\s+(?:test|vet)\b",
    re.IGNORECASE,
)
# A truncating filter on the OUTPUT of the pipeline.
TRUNCATE = re.compile(r"\|\s*(?:tail|head)\b", re.IGNORECASE)
# `&&` chaining (not `&` backgrounding, not `||`).
CHAIN = re.compile(r"(?<!&)&&(?!&)")


def main() -> None:
    if os.environ.get("CHAINED_STATE_CMD_BYPASS") == "1":
        log_fire("warn-chained-state-command-truncated", status="bypassed")
        sys.exit(0)
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if data.get("tool_name") != "Bash":
        sys.exit(0)
    cmd = ""
    tin = data.get("tool_input")
    if isinstance(tin, dict):
        cmd = str(tin.get("command", ""))
    if not cmd:
        sys.exit(0)

    if not CHAIN.search(cmd) or not TRUNCATE.search(cmd):
        sys.exit(0)

    # Detector B: a verification gate piped to tail/head in a segment BEFORE a
    # `&&`. The pipeline's status is tail's, so `&&` cannot short-circuit on the
    # gate's failure — the chain is structurally incapable of stopping there.
    segs_all = CHAIN.split(cmd)
    masked = sorted({
        m.group(0).strip()
        for seg in segs_all[:-1]
        if TRUNCATE.search(seg)
        for m in VERIFY_CMD.finditer(seg)
    })
    if masked:
        vmsg = (
            "A verification gate here is piped to tail/head and then chained with `&&`: "
            f"{', '.join(masked)}.\n"
            "A pipeline exits with its LAST command's status, so `tail` returns 0 even when "
            "the gate FAILS and `&&` runs the rest anyway — this chain cannot stop on a red "
            "gate. The truncation also drops the line ('error TS…', 'FAIL') a later grep "
            "would match, so the run reads clean twice over.\n"
            "Capture each gate's status instead, then assert on it:\n"
            "  npm run typecheck > /tmp/tc.log 2>&1; TC=$?\n"
            "  npm test          > /tmp/ts.log 2>&1; TS=$?\n"
            "  echo \"typecheck=$TC test=$TS\"   # grep the FILES for detail\n"
            "Bypass: CHAINED_STATE_CMD_BYPASS=1."
        )
        log_fire("warn-chained-state-command-truncated", status="warned",
                 detector="verify-gate-exit-masked", cmds=",".join(masked))
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": vmsg,
            },
            "systemMessage": vmsg,
        }))
        sys.exit(0)

    # The state command must sit in a segment AFTER a `&&` — that is the one
    # whose success gets inferred rather than observed.
    segments = CHAIN.split(cmd)
    if len(segments) < 2:
        sys.exit(0)
    hits = sorted({m.group(0).strip() for seg in segments[1:] for m in STATE_CMD.finditer(seg)})
    if not hits:
        sys.exit(0)

    msg = (
        "This one Bash call chains a state-changing command after `&&` and pipes the result "
        f"through tail/head: {', '.join(hits)}.\n"
        "`&&` short-circuits, so if an earlier step fails or a PreToolUse gate denies it, the "
        "later steps NEVER RUN — and the truncated tail you read will be that failure's error "
        "text, which looks like ordinary output. Do not infer the earlier steps succeeded.\n"
        "Either split the chain into separate calls, or read the full output and then confirm "
        "the end state from the system of record: `git rev-parse origin/<branch>` for a push, "
        "`gh pr view <n> --json headRefOid,state` for a PR. Bypass: CHAINED_STATE_CMD_BYPASS=1."
    )
    log_fire("warn-chained-state-command-truncated", status="warned",
             cmds=",".join(hits))
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": msg,
        },
        "systemMessage": msg,
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
