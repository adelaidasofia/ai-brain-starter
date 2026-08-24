#!/usr/bin/env python3
"""
observe-tool-calls.py — PreToolUse hook. The deterministic 100%-capture layer
of the Instinct Engine.

WHY: `/patterns` historically reconstructed "what happened this session" by
re-reading the transcript in-context — probabilistic (~50-80%) and lossy.
A PreToolUse hook fires on EVERY matched tool call, so the observation ledger
is deterministic and complete. `/patterns` reads the ledger instead of
re-scanning the transcript.

CONTRACT (load-bearing):
  - NEVER blocks. Always emits the neutral passthrough, even on its own error.
    A learning ledger must never degrade a real tool call.
  - FAST. No subprocess on the hot path; project key derived by a cheap
    filesystem walk for a `.git` dir. Append is one short line.
  - SENSITIVE-PATH-SAFE. Logs tool + a COARSE action + a scrubbed short
    detail. Never logs file CONTENT. Skips detail entirely for secret-bearing
    paths. Runs every captured string through a secret scrubber.

Ledger: ~/.claude/instinct/observations.jsonl  (one JSON object per line)
  {"ts","session","project","tool","action","detail"}

This is NOT the episodic learnings hook (post-tool-use-learnings.py), which
captures FAILURES + <learning> annotations into Meta/Learnings/. This captures
the full, scrubbed tool-call stream. The two are complementary layers.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

LEDGER = Path(os.environ.get(
    "INSTINCT_OBSERVATIONS",
    str(Path.home() / ".claude" / "instinct" / "observations.jsonl"),
))
MAX_BYTES = 5_000_000        # rotate the ledger past ~5MB
DETAIL_MAX = 80              # scrubbed detail char cap

PASSTHROUGH = '{"continue": true, "suppressOutput": true}'

# Secret patterns (case-sensitive where the token is; case-insensitive labels).
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9_]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,255}"),
    re.compile(r"sk_live_[0-9a-zA-Z]{24,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{40,}"),
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{40,}"),
    re.compile(r"npm_[A-Za-z0-9]{36}"),
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|bearer)\s*[=:]\s*\S+"),
]
# Paths whose mere detail we refuse to log.
SENSITIVE_PATH = re.compile(
    r"(?i)(\.env|admin\.env|secrets?|credential|\.pem|\.key|id_rsa|keychain|\.ssh/|"
    r"\.aws/|\.zsh_secrets|\.netrc)")


def scrub(text: str) -> str:
    if not text:
        return ""
    out = text
    for pat in SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def _project_key(cwd: str) -> str:
    """Cheap, no-subprocess project key. Walk up for a repo/vault root."""
    try:
        p = Path(cwd or os.getcwd()).resolve()
    except Exception:
        return "global"
    probe = p
    for _ in range(12):
        try:
            # vault root carries an "⚙️ Meta" dir
            if any(c.name.endswith("Meta") and "⚙" in c.name
                   for c in probe.iterdir() if c.is_dir()):
                return "personal-vault"
            if (probe / ".git").exists():
                return f"repo:{probe.name}"
        except (OSError, PermissionError):
            pass
        if probe.parent == probe:
            break
        probe = probe.parent
    return "global"


def summarize(tool: str, tinput: dict) -> tuple[str, str]:
    """Return (action, scrubbed_detail). Coarse + safe by construction."""
    if not isinstance(tinput, dict):
        tinput = {}
    if tool == "Bash":
        cmd = str(tinput.get("command", "")).strip()
        binary = (cmd.split() or ["?"])[0].split("/")[-1]
        return f"bash:{binary}", scrub(cmd)[:DETAIL_MAX]
    if tool in ("Write", "Edit", "MultiEdit"):
        fp = str(tinput.get("file_path", tinput.get("filePath", "")))
        if SENSITIVE_PATH.search(fp):
            return f"{tool.lower()}:[sensitive-path]", ""
        ext = Path(fp).suffix or "(noext)"
        return f"{tool.lower()}:{ext}", scrub(Path(fp).name)[:DETAIL_MAX]
    if tool in ("Read", "NotebookEdit"):
        fp = str(tinput.get("file_path", tinput.get("notebook_path", "")))
        if SENSITIVE_PATH.search(fp):
            return f"{tool.lower()}:[sensitive-path]", ""
        return f"{tool.lower()}:{Path(fp).suffix or '(noext)'}", scrub(Path(fp).name)[:DETAIL_MAX]
    if tool in ("Glob", "Grep"):
        return f"{tool.lower()}", scrub(str(tinput.get("pattern", "")))[:DETAIL_MAX]
    if tool in ("Agent", "Task"):
        return "agent", scrub(str(tinput.get("subagent_type", tinput.get("description", ""))))[:DETAIL_MAX]
    if tool == "Skill":
        return "skill", scrub(str(tinput.get("skill", "")))[:DETAIL_MAX]
    if tool.startswith("mcp__"):
        # mcp__server__tool -> "mcp:server:tool" (names only, no payload)
        parts = tool.split("__")
        if len(parts) >= 3:
            return f"mcp:{parts[1]}:{parts[2]}", ""
        return "mcp", ""
    return tool.lower(), ""


def append_observation(record: dict) -> None:
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        # cheap rotation
        try:
            if LEDGER.exists() and LEDGER.stat().st_size > MAX_BYTES:
                LEDGER.replace(LEDGER.with_suffix(".jsonl.prev"))
        except OSError:
            pass
        with open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # never let ledger IO affect the tool call


def build_record(data: dict) -> dict:
    tool = (data.get("tool_name") or data.get("toolName") or "").strip() or "?"
    tinput = data.get("tool_input") or data.get("toolInput") or {}
    session = str(data.get("session_id") or data.get("sessionId") or "unknown")[:8]
    cwd = data.get("cwd") or os.getcwd()
    action, detail = summarize(tool, tinput)
    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "session": session,
        "project": _project_key(cwd),
        "tool": tool,
        "action": action,
        "detail": detail,
    }


def main() -> int:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if isinstance(data, dict) and (data.get("tool_name") or data.get("toolName")):
            append_observation(build_record(data))
    except Exception:
        pass
    print(PASSTHROUGH)
    return 0


def _self_test() -> int:
    """Feed N synthetic tool-call payloads; assert 1 ledger line each + redaction."""
    import tempfile
    global LEDGER
    tmp = Path(tempfile.mkdtemp()) / "obs.jsonl"
    LEDGER = tmp
    payloads = [
        {"tool_name": "Bash", "tool_input": {"command": "git status"}, "session_id": "abc12345"},
        {"tool_name": "Bash", "tool_input": {"command": "export API_KEY=sk-ant-" + "x" * 50}},
        {"tool_name": "Write", "tool_input": {"file_path": "/x/y/notes.md"}},
        {"tool_name": "Write", "tool_input": {"file_path": "/x/.claude/admin.env"}},
        {"tool_name": "Read", "tool_input": {"file_path": "/x/secrets.txt"}},
        {"tool_name": "mcp__slack__send_message", "tool_input": {"text": "ghp_" + "a" * 36}},
        {"tool_name": "Agent", "tool_input": {"subagent_type": "Explore"}},
        {"tool_name": "Skill", "tool_input": {"skill": "patterns"}},
    ]
    for p in payloads:
        append_observation(build_record(p))
    lines = tmp.read_text().strip().split("\n")
    ok = True
    if len(lines) != len(payloads):
        print(f"  [FAIL] expected {len(payloads)} lines, got {len(lines)}"); ok = False
    else:
        print(f"  [PASS] 100% capture: {len(lines)}/{len(payloads)} tool calls logged")
    blob = tmp.read_text()
    if "sk-ant-" in blob or "ghp_aaaa" in blob:
        print("  [FAIL] secret leaked into ledger"); ok = False
    else:
        print("  [PASS] secrets redacted (sk-ant / ghp_ not present)")
    recs = [json.loads(l) for l in lines]
    if recs[3]["action"] == "write:[sensitive-path]" and recs[3]["detail"] == "":
        print("  [PASS] sensitive path (admin.env) detail suppressed")
    else:
        print(f"  [FAIL] sensitive path not suppressed: {recs[3]}"); ok = False
    if recs[5]["action"] == "mcp:slack:send_message":
        print("  [PASS] mcp tool name parsed, payload not logged")
    else:
        print(f"  [FAIL] mcp parse: {recs[5]}"); ok = False
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    # Windows cp1252-console safety (ai-brain-starter#313; hooks/ sweep #314).
    # A hook that print()s non-ASCII raises UnicodeEncodeError on a cp1252
    # console: the gate then fails silently OPEN, or denies the tool call with
    # no legible cause. Idempotent; a no-op on an already-UTF-8 console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
