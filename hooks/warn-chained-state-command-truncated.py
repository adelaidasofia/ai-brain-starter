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

# UTF-8 console guard (ai-brain-starter#313 cp1252 crash class). This file's
# advisory messages carry non-ASCII (em dashes, arrows), and on a Windows
# console defaulting to cp1252 printing them raises UnicodeEncodeError — which
# would crash the hook on the very call it is trying to warn about.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # Python 3.7+
    except (AttributeError, ValueError):
        pass

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
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:typecheck|type-check|lint|test|build|ci)\b"
    r"|\btsc\b|\beslint\b|\bvitest\b|\bjest\b|\bpytest\b|\bmypy\b|\bruff\b"
    r"|\bcargo\s+(?:test|clippy|check|build)\b|\bgo\s+(?:test|vet)\b"
    # A repo's CANONICAL full-gate wrapper, and the CI status read. Omitting the
    # wrapper is how this guard missed the incident that produced detector C:
    # every individual gate was listed, but the ONE command an agent actually
    # runs pre-push -- the wrapper around them -- was not, so the guard stayed
    # silent on the exact call that reported a fake green.
    r"|\bci-test\b|\bmake\s+(?:ci|test|lint|check)\b|\bgh\s+pr\s+checks\b"
    # Generic gate scripts: <name>-gate.sh, <name>-check.py, verify-<x>.sh ...
    r"|\b\S*(?:-gate|-check|-audit)\.(?:sh|py)\b|\bverify-\S+\.(?:sh|py)\b",
    re.IGNORECASE,
)
# A truncating filter on the OUTPUT of the pipeline.
TRUNCATE = re.compile(r"\|\s*(?:tail|head)\b", re.IGNORECASE)
# `&&` chaining (not `&` backgrounding, not `||`).
CHAIN = re.compile(r"(?<!&)&&(?!&)")
# Reading an exit status back out of a pipeline (detector C).
STATUS_READ = re.compile(r"\$\?|\$\{\?\}|PIPESTATUS|pipestatus", re.IGNORECASE)
# The two spellings that make the read CORRECT, so a fixed command stays quiet.
# zsh's array is lowercase and 1-INDEXED; `set -o pipefail` moves the failure
# into `$?` itself.
STATUS_READ_OK = re.compile(r"\$\{pipestatus\[1\]\}|pipefail")


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

    if not TRUNCATE.search(cmd):
        sys.exit(0)

    # Detector C: a gate or state command piped to tail/head, whose exit status
    # is then READ BACK in the same call. No `&&` required — and that absence is
    # the whole point: A and B both key off `&&`, so the commonest fake-green of
    # all slipped past both.
    #
    #   ci-test 2>&1 | tail -80; echo "EXIT=$?"        -> EXIT=0, always
    #   gh pr merge 911 --squash | tail -8; echo "$?"  -> 0, always
    #
    # `$?` after a pipeline is the LAST stage's status, and tail/head essentially
    # always succeed, so a RED gate reports GREEN and the agent ships on a verdict
    # it never saw. Worse in zsh (the agent shell): the bash spelling
    # `${PIPESTATUS[0]}` expands to EMPTY, so the popular `${PIPESTATUS[0]:-$?}`
    # idiom silently falls back to the pager's status too.
    #
    # Observed twice in ONE session (2026-07-29, MYC-3446) — on the ci-test gate
    # and again on the merge — after the operating rule for it already existed in
    # the shared brain. A retrieval-tier doc reaches you AFTER you have written
    # the command; this fires while you are writing it.
    if STATUS_READ.search(cmd) and not STATUS_READ_OK.search(cmd):
        masked_c = sorted(
            {m.group(0).strip() for m in VERIFY_CMD.finditer(cmd)}
            | {m.group(0).strip() for m in STATE_CMD.finditer(cmd)}
        )
        if masked_c:
            cmsg = (
                "This reads a PAGER's exit code, not the gate's: "
                f"{', '.join(masked_c)}.\n"
                "A pipeline exits with its LAST stage's status. `tail`/`head` "
                "essentially always succeed, so `$?` here is ~always 0 — a RED gate "
                "would report GREEN and you would ship on a verdict you never saw.\n"
                "In zsh the bash spelling `${PIPESTATUS[0]}` expands to EMPTY, so "
                "`${PIPESTATUS[0]:-$?}` reports the pager too. Use one of:\n"
                "  1. Do not pipe the gate:  ci-test > /tmp/gate.log 2>&1; echo \"EXIT=$?\"\n"
                "  2. zsh, LOWERCASE + 1-INDEXED:  ... | tail -80; echo \"${pipestatus[1]}\"\n"
                "  3. set -o pipefail  (then `$?` is the pipeline's real status)\n"
                "Bypass: CHAINED_STATE_CMD_BYPASS=1."
            )
            log_fire("warn-chained-state-command-truncated", status="warned",
                     detector="pager-exit-code-read", cmds=",".join(masked_c))
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": cmsg,
                },
                "systemMessage": cmsg,
            }))
            sys.exit(0)

    if not CHAIN.search(cmd):
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
