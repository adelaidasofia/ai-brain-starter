#!/usr/bin/env python3
"""
session-close-fallback.py — Layer 3 graceful degradation.

If the closing-signal marker shows a session-close fired BUT the model
bailed without writing the captures (empty session file body), this script
calls Haiku 4.5 with the conversation transcript and fills the file. This
guarantees no silent loss even when the model didn't run the cascade.

Behavior:
  1. Read marker at ~/.claude/.closing-signal-{session_id}.json
  2. Check if session_file body is still empty (only frontmatter + headers)
  3. If empty AND ANTHROPIC_API_KEY available, call Haiku with transcript
  4. Write Haiku output to session file under existing headers
  5. Flag fallback-fired so user can review next session
  6. If no API key: write a "fallback unavailable" notice to the file

Inputs (CLI):
  --session-id <id>          required
  --transcript-path <path>   required (path to .jsonl transcript)

Exits 0 on any path (graceful degradation never blocks).

Why this script exists: the prior architecture had a hole — the Stop hook
fires AFTER the model's turn ends, and a model that bails on the cascade
leaves an empty session file and no recovery path. This script is the
backstop. Haiku is fast + cheap + sufficient for verbatim extraction.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


def log(msg: str) -> None:
    if os.environ.get("CLOSING_SIGNAL_DEBUG") == "1":
        print(f"[session-close-fallback] {msg}", file=sys.stderr)


def is_session_body_empty(session_file: Path) -> bool:
    """Check if a session file has only frontmatter + empty section headers."""
    if not session_file.is_file():
        return True
    try:
        text = session_file.read_text(encoding="utf-8")
    except OSError:
        return True
    # Strip frontmatter
    body_match = re.split(r"^---\s*$", text, maxsplit=2, flags=re.MULTILINE)
    body = body_match[2] if len(body_match) >= 3 else text
    # Remove header lines and HTML comments
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        lines.append(stripped)
    return len(lines) == 0


def read_transcript_messages(transcript_path: Path, max_chars: int = 60000) -> str:
    """Read the transcript JSONL into a compact text blob for Haiku."""
    if not transcript_path.is_file():
        return ""
    out = []
    try:
        with transcript_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                content = msg.get("content")
                if isinstance(content, list):
                    text = "\n".join(
                        c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text"
                    )
                elif isinstance(content, str):
                    text = content
                else:
                    text = ""
                if role and text:
                    out.append(f"[{role}]\n{text}")
    except OSError as e:
        log(f"failed to read transcript: {e}")
        return ""
    blob = "\n\n".join(out)
    # Cap from the END (most recent context most relevant)
    if len(blob) > max_chars:
        blob = "...[earlier conversation truncated]...\n\n" + blob[-max_chars:]
    return blob


def call_haiku(transcript_blob: str) -> str | None:
    """Ask Haiku 4.5 to scan the transcript and produce session-file body sections."""
    try:
        import anthropic  # type: ignore
    except ImportError:
        log("anthropic SDK not installed")
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("ANTHROPIC_API_KEY not set")
        return None
    try:
        client = anthropic.Anthropic()
        prompt = f"""You are filling in a session-close summary because the primary model bailed.
Read the conversation transcript below and produce content for these sections:

## What happened
[Brief summary of what was worked on, what was produced. 2-4 sentences.]

## Decisions
[New decisions logged this session, if any. Or "None this session."]

## Captures
[Verbatim quotes from the user that revealed beliefs, observations, or new ideas.
Tag emotional ones [emotional]. If none, write "None this session."]

## To-dos filed
[New to-dos with their destination file path. Or "None this session."]

## Delegations
[Items handed to others, with a drafted message ready to send. Or "None this session."]

## Pending / incomplete
[Background tasks still running, items deferred. Or "No incomplete work."]

OUTPUT FORMAT: Just the six section bodies in order, with the ## headers.
Do not add any preamble or commentary. Be terse.

TRANSCRIPT:

{transcript_blob}
"""
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        if not resp.content:
            return None
        return resp.content[0].text
    except Exception as e:
        log(f"haiku call failed: {e!r}")
        return None


def write_fallback_into_session_file(session_file: Path, body: str, source: str) -> None:
    """Replace the empty section bodies with the fallback content."""
    if not session_file.is_file():
        log(f"session file vanished: {session_file}")
        return
    try:
        existing = session_file.read_text(encoding="utf-8")
    except OSError as e:
        log(f"failed to read session file: {e}")
        return

    # Preserve frontmatter, replace section_label with "fallback"
    fm_match = re.match(r"^(---\n.*?\n---\n)", existing, re.DOTALL)
    frontmatter = fm_match.group(1) if fm_match else ""
    if frontmatter:
        frontmatter = re.sub(
            r'session_label:\s*"[^"]*"',
            f'session_label: "fallback ({source})"',
            frontmatter,
        )

    # Top-level title from existing if present
    title_match = re.search(r"^# .*$", existing, re.MULTILINE)
    title = title_match.group(0) if title_match else "# Session — fallback"

    notice = f"""
> **Note:** This session file was filled in by the {source} fallback because
> the primary cascade did not complete. Review and adjust as needed; flagged
> for next-session review.
"""

    new_text = f"{frontmatter}\n{title}\n{notice}\n{body.strip()}\n"
    try:
        session_file.write_text(new_text, encoding="utf-8")
        log(f"wrote fallback content to {session_file}")
    except OSError as e:
        log(f"failed to write fallback: {e}")


def write_partial_flag(session_id: str, session_file: Path, reason: str) -> None:
    """Drop a flag so the next session-start surfaces 'last close didn't complete'."""
    home = Path.home()
    flag_dir = home / ".claude"
    flag_dir.mkdir(parents=True, exist_ok=True)
    flag = flag_dir / f".cascade-partial-{session_id}.json"
    try:
        flag.write_text(
            json.dumps({
                "session_id": session_id,
                "session_file": str(session_file),
                "reason": reason,
            }, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--transcript-path", required=True)
    args = ap.parse_args()

    home = Path.home()
    marker = home / ".claude" / f".closing-signal-{args.session_id}.json"
    if not marker.is_file():
        log("no marker — nothing to do")
        return 0

    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log(f"marker unreadable: {e}")
        return 0

    if payload.get("is_trivial"):
        log("marker says trivial — skip")
        return 0

    session_file = Path(payload.get("session_file", ""))
    if not session_file or not session_file.parent.is_dir():
        log("no session file path in marker")
        return 0

    if not is_session_body_empty(session_file):
        log("session body has content — model wrote it, no fallback needed")
        return 0

    transcript_path = Path(args.transcript_path)
    blob = read_transcript_messages(transcript_path)
    if not blob:
        log("empty transcript blob")
        write_partial_flag(
            args.session_id, session_file,
            "session body empty + transcript unreadable",
        )
        return 0

    haiku_body = call_haiku(blob)
    if haiku_body:
        write_fallback_into_session_file(session_file, haiku_body, "haiku")
    else:
        # No API key or Haiku failed — leave a notice + flag for next session
        notice = (
            "## Notice\n\nFallback unavailable (no ANTHROPIC_API_KEY or Haiku call failed). "
            "The session ended with a close signal but the cascade did not run. "
            "Review the conversation transcript manually and fill in this file, "
            "or set ANTHROPIC_API_KEY and run "
            "`scripts/recover-last-close.py` to retry.\n"
        )
        write_fallback_into_session_file(session_file, notice, "no-api-key")
        write_partial_flag(
            args.session_id, session_file,
            "session body empty + fallback unavailable",
        )

    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
