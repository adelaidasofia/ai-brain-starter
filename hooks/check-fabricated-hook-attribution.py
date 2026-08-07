#!/usr/bin/env python3
"""Stop hook. Block claims like "[X] hook fired" when X can't be verified.

Two verification sources — a claim passes if EITHER confirms it:
  1. hookify-blocks.log — for hookify RULES (they log there as **[name]**).
  2. This turn's transcript tool errors — for STANDALONE Python hooks, which
     never touch hookify-blocks.log but DO appear verbatim in the tool error as
     a `.../hooks/<name>.py` path. Only NON-assistant records (tool results,
     system) count, so the model can't self-verify by writing the path itself.

Without source 2 the guard false-flags every truthful attribution to a
standalone hook, and a guard that punishes honesty gets uninstalled.

Bypass: FAB_HOOK_CHECK_BYPASS=1.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
from pathlib import Path

# Fire telemetry (MYC-285). Fail-open: a missing _lib must never break the
# guard or its tests.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "_lib"))
    from guard_telemetry import log_fire
except Exception:  # pragma: no cover - telemetry is never load-bearing
    def log_fire(*_a, **_k):
        return

WINDOW_SEC = 600
LOG_PATH = Path.home() / ".claude" / "hookify-blocks.log"

CLAIM_PATTERNS = [
    re.compile(
        r"(?:^|[^a-zA-Z`/-])"
        r"(?:the\s+)?"
        r"([a-z][a-z0-9]*(?:[-_][a-z0-9]+)+|em.dash|em-dash)"
        r"\s+(?:hook|rule)\s+"
        r"(fired|caught|blocked|flagged|prevented|stopped|triggered)\b",
        re.IGNORECASE,
    ),
]


def main() -> None:
    if os.environ.get("FAB_HOOK_CHECK_BYPASS") == "1":
        log_fire("check-fabricated-hook-attribution", status="bypassed")
        sys.exit(0)
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if data.get("stop_hook_active"):
        sys.exit(0)
    transcript_path = data.get("transcript_path", "")
    if not transcript_path or not Path(transcript_path).exists():
        sys.exit(0)
    last_text = _last_assistant_text(transcript_path)
    if not last_text:
        sys.exit(0)
    claimed = _extract_claims(last_text)
    if not claimed:
        sys.exit(0)
    recent = _recent_rule_names()
    fired_standalone = _standalone_hook_evidence(transcript_path, WINDOW_SEC)
    fabricated = sorted(
        c for c in claimed
        if not _matches_any(c, recent) and not _matches_any(c, fired_standalone)
    )
    if not fabricated:
        sys.exit(0)
    msg = (
        f"Hook attribution check: assistant claims {fabricated} fired, but that name is "
        f"neither in ~/.claude/hookify-blocks.log nor present as a `.../hooks/<name>.py` "
        f"path in this turn's tool errors, within the last {WINDOW_SEC // 60} minutes. "
        f"Recent hookify fires: {sorted(recent) or 'none'}; standalone-hook fires: "
        f"{sorted(fired_standalone) or 'none'}. Either quote the verbatim rule/hook name "
        f"from the actual tool error in this turn, or say 'a hook blocked this' without "
        f"naming a specific one. Bypass for forensic discussion: FAB_HOOK_CHECK_BYPASS=1."
    )
    log_fire("check-fabricated-hook-attribution", status="blocked",
             names=",".join(fabricated))
    print(json.dumps({"decision": "block", "reason": msg}))
    sys.exit(0)


def _last_assistant_text(transcript_path: str) -> str:
    text = ""
    try:
        with open(transcript_path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "assistant":
                    continue
                msg = rec.get("message", {})
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue
                parts = [
                    c.get("text", "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                if parts:
                    text = "\n".join(parts)
    except Exception:
        return ""
    return text


def _extract_claims(text: str) -> set:
    claims = set()
    for pat in CLAIM_PATTERNS:
        for m in pat.finditer(text):
            if _is_quoted(text, m.start(1)):
                continue
            name = m.group(1).lower().replace("em.dash", "em-dash").replace(" ", "-")
            claims.add(name)
    return claims


def _is_quoted(text: str, span_start: int) -> bool:
    window = text[max(0, span_start - 120) : span_start]
    quotes = (
        window.count('"')
        + window.count("'")
        + window.count("“")
        + window.count("”")
        + window.count("‘")
        + window.count("’")
        + window.count("`")
    )
    return quotes % 2 == 1


def _recent_rule_names() -> set:
    if not LOG_PATH.exists():
        return set()
    cutoff = time.time() - WINDOW_SEC
    names = set()
    try:
        with open(LOG_PATH) as f:
            for line in f:
                parts = line.split("\t")
                if len(parts) < 5:
                    continue
                try:
                    ts = datetime.datetime.fromisoformat(parts[0]).timestamp()
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                msg_field = "\t".join(parts[4:])
                m = re.search(r"\*\*\[([a-z0-9][a-z0-9_-]*)\]\*\*", msg_field)
                if m:
                    names.add(m.group(1).lower())
    except Exception:
        return set()
    return names


def _standalone_hook_evidence(transcript_path: str, window_sec: int) -> set:
    """Base-names of STANDALONE hooks that actually fired this turn, detected by
    a `.../hooks/<name>.py` token in a NON-assistant transcript record (tool
    result / system). Standalone hooks never write to hookify-blocks.log, so
    this is the second verification source. Assistant records are skipped so the
    model can't self-verify by writing the path in its own message."""
    names: set = set()
    cutoff = time.time() - window_sec
    path_re = re.compile(r"/hooks/([a-z0-9][a-z0-9_-]*)\.py")
    try:
        with open(transcript_path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "assistant":
                    continue
                ts = rec.get("timestamp")
                if isinstance(ts, str):
                    try:
                        rec_ts = datetime.datetime.fromisoformat(
                            ts.replace("Z", "+00:00")
                        ).timestamp()
                        if rec_ts < cutoff:
                            continue
                    except ValueError:
                        pass
                blob = json.dumps(rec.get("message", rec))
                for m in path_re.finditer(blob):
                    names.add(m.group(1).lower())
    except Exception:
        return set()
    return names


def _matches_any(claim: str, recent: set) -> bool:
    if claim in recent:
        return True
    for r in recent:
        if r == claim or r.endswith(f"-{claim}") or claim.endswith(f"-{r}"):
            return True
        if claim in r or r in claim:
            return True
    return False


if __name__ == "__main__":
    main()
