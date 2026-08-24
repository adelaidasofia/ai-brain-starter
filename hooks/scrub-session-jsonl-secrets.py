#!/usr/bin/env python3
"""SessionEnd hook: scrub secrets from this session's JSONL on close.

Reads ~/.claude/hooks/_lib/secret_patterns.py for the registry. Applies
every pattern to every line of the session JSONL. Idempotent (re-running
on a scrubbed file is a no-op). Backs up to .bak.{ts} before writing.

Two outputs:

1. The scrubbed JSONL replaces the original in place.
2. A per-run summary line at ~/.claude/hooks/scrub-log.jsonl with the
   counts of each pattern that hit (helps tell whether the upstream
   detector + PreToolUse block are working or whether secrets keep
   landing on disk).

Pairs with:
- PreToolUse hookify `block-secret-dump-command-class` (input block)
- PostToolUse `detect-secrets-in-bash-output.py` (in-session alert)
- SessionStart `scan-prior-sessions-for-secrets.py` (warns next session)

Codified 2026-05-13. Critical Failure Inventory: the user's primary org production infra
surface row 2.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))

from _lib.secret_patterns import redact, scan  # noqa: E402

SCRUB_LOG = HOOK_DIR / "scrub-log.jsonl"


def _read_payload() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _resolve_session_jsonl(payload: dict) -> Path | None:
    """The hook framework passes transcript_path; fall back to session_id."""
    p = payload.get("transcript_path")
    if p:
        candidate = Path(p)
        if candidate.exists():
            return candidate

    session_id = payload.get("session_id")
    if not session_id:
        return None

    # Walk known project dirs looking for `<session_id>.jsonl`.
    projects = Path.home() / ".claude" / "projects"
    if not projects.exists():
        return None
    for proj_dir in projects.iterdir():
        candidate = proj_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate
    return None


def _scrub_file(path: Path) -> tuple[int, list[tuple[str, int]]]:
    """Read path, redact, write back. Return (bytes_changed, hits_pre_scrub).

    Idempotent: if no patterns match, file is not rewritten.
    """
    original = path.read_text()
    hits_pre = scan(original)
    if not hits_pre:
        return 0, []

    # Per-line redaction so the JSONL structure stays valid.
    out_lines: list[str] = []
    for line in original.splitlines(keepends=True):
        redacted_line, _ = redact(line)
        out_lines.append(redacted_line)
    out_text = "".join(out_lines)

    if out_text == original:
        return 0, hits_pre  # scan caught a substring that survives redact (edge case)

    backup = path.with_suffix(path.suffix + f".bak.{int(time.time())}")
    shutil.copy2(path, backup)

    path.write_text(out_text)
    return abs(len(out_text) - len(original)), hits_pre


def _log(summary: dict) -> None:
    try:
        with SCRUB_LOG.open("a") as f:
            f.write(json.dumps(summary, separators=(",", ":")) + "\n")
    except OSError:
        pass


def main() -> int:
    payload = _read_payload()
    jsonl = _resolve_session_jsonl(payload)
    if jsonl is None or not jsonl.exists():
        print(json.dumps({"continue": True, "suppressOutput": True}))
        return 0

    try:
        bytes_changed, hits_pre = _scrub_file(jsonl)
    except OSError as exc:
        # Don't break session close on filesystem errors.
        _log(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "session_id": payload.get("session_id", "unknown"),
                "path": str(jsonl),
                "error": str(exc),
            }
        )
        print(json.dumps({"continue": True, "suppressOutput": True}))
        return 0

    _log(
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "session_id": payload.get("session_id", "unknown"),
            "path": str(jsonl),
            "bytes_changed": bytes_changed,
            "hits_pre_scrub": [
                {"pattern": n, "count": c} for n, c in hits_pre
            ],
        }
    )
    if hits_pre:
        names = ", ".join(f"{n}×{c}" for n, c in hits_pre)
        print(
            f"[scrub-session-jsonl-secrets] redacted {names} from {jsonl.name}",
            file=sys.stderr,
        )

    print(json.dumps({"continue": True, "suppressOutput": True}))
    return 0


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
    raise SystemExit(main())
