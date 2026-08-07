#!/usr/bin/env python3
"""
passive-capture.py — Engram-inspired passive observation primitive.

Scans recent Claude Code session transcripts for user utterances that
look like rules, decisions, or lessons but were NOT filed via /journal
or /decision. Writes each match as a markdown stub in `⚙️ Meta/Passive
Captures/{date}-{slug}.md` for triage during /sunday-review.

Engram's `mem_capture_passive` exposes this as an MCP tool the agent
calls. We invert: the script scans transcripts post-hoc, so the user
(not the agent) drives what gets surfaced. The capture is a candidate,
never a commit. The user triages weekly; adopted captures move to the
matching canonical surface (CLAUDE.md, rules/*.md, Decision Log), the
rest go to Passive Captures/Archive/.

Idempotent via state file at `~/.claude/passive-capture-state.json`:
per-session-id last-processed-uuid, so re-running doesn't double-capture.

Modes:
    --scan-today       (default) Scan transcripts modified today
    --scan-since DATE  Scan from YYYY-MM-DD to now
    --dry-run          Show what would be captured, write nothing
    --report           Show count of pending captures by pattern type
    --triage           List untriaged captures with file paths

Output: `⚙️ Meta/Passive Captures/YYYY-MM-DD-{slug}.md`
State: `~/.claude/passive-capture-state.json`

Usage:
    python3 "⚙️ Meta/scripts/passive-capture.py" --scan-today
    python3 "⚙️ Meta/scripts/passive-capture.py" --scan-since 2026-05-01
    python3 "⚙️ Meta/scripts/passive-capture.py" --dry-run --scan-today
    python3 "⚙️ Meta/scripts/passive-capture.py" --self-test
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

# --- Configuration ---------------------------------------------------

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT") or os.getcwd())


def _resolve_meta_dir():
    """Vaults vary in convention — pick the first existing Meta-like folder."""
    for name in ("⚙️ Meta", "Meta", "_meta", "meta"):
        if (VAULT_ROOT / name).is_dir():
            return VAULT_ROOT / name
    return VAULT_ROOT / "Meta"


META_DIR = _resolve_meta_dir()
CAPTURES_DIR = META_DIR / "Passive Captures"
ARCHIVE_DIR = CAPTURES_DIR / "Archive"
STATE_FILE = Path.home() / ".claude" / "passive-capture-state.json"


def _resolve_transcripts_dir():
    """Locate the per-account Claude Code transcript dir for this vault.

    Pattern: ~/.claude/projects/<encoded-vault-path>/ where the encoded
    path replaces `/` and `.` with `-` and prepends `-`. Returns None if
    not found (Claude Code not installed, different account, etc.).
    """
    encoded = "-" + str(VAULT_ROOT).replace("/", "-").replace(".", "-")
    candidate = Path.home() / ".claude" / "projects" / encoded
    return candidate if candidate.is_dir() else None


TRANSCRIPTS_DIR = _resolve_transcripts_dir()

# --- Pattern matching -----------------------------------------------

# Rule-like: imperative directives ("from now on", "always when", explicit
# rule-flagging language). High-signal — these are the strongest passive-
# capture candidates because they're verbalized rules.
RULE_PATTERNS = [
    (r"\bfrom now on\b", "explicit_rule_flag"),
    (r"\bevery time\b.{0,80}", "habit_rule"),
    (r"\banytime\b.{0,80}", "habit_rule"),
    (r"\bwhenever\b.{0,80}", "habit_rule"),
    (r"\bmake (?:this|that|it) a rule\b", "explicit_rule_flag"),
    (r"\bshould be a rule\b", "explicit_rule_flag"),
    (r"\bcodify (?:this|that)\b", "explicit_rule_flag"),
    (r"\balways (?:do|use|run|check|read|write|prefer|avoid)\b", "imperative"),
    (r"\bnever (?:do|use|run|check|skip|forget|miss|ignore)\b", "imperative"),
    (r"\bmust (?:always|never|read|run|check|enforce)\b", "imperative"),
]

# Decision-like: choice-making language. The user is committing to a
# direction. Medium-signal — many decisions are casual, but a Decision
# Log entry might still belong.
DECISION_PATTERNS = [
    (r"\b(?:let'?s|we'?re going to|i'?m going to) switch to\b", "switching_decision"),
    (r"\b(?:i've|we'?ve) decided\b", "decision_made"),
    (r"\bdecision[:\s]+", "decision_flag"),
    (r"\bgoing forward,?\b", "direction_change"),
    (r"\bmoving forward,?\b", "direction_change"),
    (r"\bno longer (?:doing|using|going to)\b", "stop_doing"),
    (r"\bstop (?:doing|using)\b", "stop_doing"),
]

# Lesson-like: reflective, retrospective, pattern-noticing. These often
# belong in journal entries or in the Decision Log's outcome section.
LESSON_PATTERNS = [
    (r"\bthe lesson (?:is|here is|i learned)\b", "lesson_explicit"),
    (r"\bwhat i learned\b", "lesson_explicit"),
    (r"\bi realized\b", "realization"),
    (r"\bi keep (?:doing|making|getting)\b", "recurring_pattern"),
    (r"\bthis is the (?:second|third|fourth|fifth|nth) time\b", "recurring_pattern"),
    (r"\bwe'?ve been doing (?:this|that|it) wrong\b", "anti_pattern"),
    (r"\bturns out\b", "discovery"),
]

ALL_PATTERNS = (
    [(re.compile(p, re.IGNORECASE), "rule", tag) for p, tag in RULE_PATTERNS] +
    [(re.compile(p, re.IGNORECASE), "decision", tag) for p, tag in DECISION_PATTERNS] +
    [(re.compile(p, re.IGNORECASE), "lesson", tag) for p, tag in LESSON_PATTERNS]
)

# Skip if message starts with these (explicit channel — already captured)
SKIP_PREFIXES = (
    "/journal",
    "/decision",
    "/deconstruct",
    "/plan",
    "/patterns",
    "/weekly",
    "/monthly",
    "/sunday",
    "/onde-weekly",
    "/sunday-review",
)

# Skip if message contains these (cancellation / negation)
SKIP_TOKENS = (
    "ignore this",
    "ignore that",
    "forget what i said",
    "forget that",
    "scratch that",
    "nevermind",
    "never mind",
)

MIN_MESSAGE_LENGTH = 30
MAX_QUOTE_LENGTH = 600  # truncate long captures


# --- Data ------------------------------------------------------------

@dataclass
class Capture:
    pattern_type: str  # "rule" | "decision" | "lesson"
    pattern_tag: str  # specific pattern (e.g. "explicit_rule_flag")
    quote: str
    full_message: str
    session_id: str
    user_uuid: str
    timestamp: str
    matched_phrase: str

    def to_dict(self):
        return asdict(self)


# --- State management ------------------------------------------------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"sessions": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"sessions": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# --- Transcript scanning ---------------------------------------------

def find_transcripts(since: dt.datetime) -> list[Path]:
    """Return JSONL transcripts modified at or after `since`."""
    if TRANSCRIPTS_DIR is None or not TRANSCRIPTS_DIR.exists():
        return []
    out = []
    since_ts = since.timestamp()
    for path in TRANSCRIPTS_DIR.glob("*.jsonl"):
        try:
            if path.stat().st_mtime >= since_ts:
                out.append(path)
        except OSError:
            continue
    return sorted(out, key=lambda p: p.stat().st_mtime)


def extract_user_messages(transcript: Path, last_uuid: str | None) -> list[dict]:
    """Yield user-role messages from a transcript, after last_uuid if given."""
    out = []
    seen_last = last_uuid is None
    try:
        with transcript.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "user":
                    continue
                # Skip tool_result echoes (those are tool-output, not user-typed)
                msg = rec.get("message") or {}
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                # User-typed messages have content as a string OR a list of
                # text-blocks (possibly mixed with tool_result blocks).
                # We only want the user-typed text, not tool results.
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
                    if not text_blocks:
                        # All tool_result, no user-typed text → skip
                        continue
                    text = "\n".join(b.get("text", "") for b in text_blocks)
                if not text or not text.strip():
                    continue
                uuid = rec.get("uuid", "")
                if not seen_last:
                    if uuid == last_uuid:
                        seen_last = True
                    continue
                out.append({
                    "uuid": uuid,
                    "session_id": rec.get("sessionId", transcript.stem),
                    "timestamp": rec.get("timestamp", ""),
                    "text": text,
                })
    except OSError:
        return []
    return out


# --- Pattern detection -----------------------------------------------

def should_skip(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < MIN_MESSAGE_LENGTH:
        return True
    lowered = stripped.lower()
    for prefix in SKIP_PREFIXES:
        if lowered.startswith(prefix):
            return True
    for token in SKIP_TOKENS:
        if token in lowered:
            return True
    return False


def detect_captures(message: dict) -> list[Capture]:
    text = message["text"]
    if should_skip(text):
        return []
    out = []
    seen_phrases = set()  # Don't double-capture if multiple regex match same span
    for regex, ptype, tag in ALL_PATTERNS:
        for m in regex.finditer(text):
            phrase = m.group(0).strip()
            phrase_key = (ptype, phrase.lower())
            if phrase_key in seen_phrases:
                continue
            seen_phrases.add(phrase_key)
            # Pull surrounding sentence as the quote
            start = max(0, m.start() - 100)
            end = min(len(text), m.end() + 200)
            quote = text[start:end].strip()
            if start > 0:
                quote = "…" + quote
            if end < len(text):
                quote = quote + "…"
            quote = quote[:MAX_QUOTE_LENGTH]
            out.append(Capture(
                pattern_type=ptype,
                pattern_tag=tag,
                quote=quote,
                full_message=text[:MAX_QUOTE_LENGTH * 2],
                session_id=message["session_id"],
                user_uuid=message["uuid"],
                timestamp=message["timestamp"],
                matched_phrase=phrase,
            ))
    return out


# --- Capture file writing --------------------------------------------

def slugify(text: str, max_len: int = 40) -> str:
    """Generate a stable slug from text (first words, lowercased, hyphenated)."""
    cleaned = re.sub(r"[^\w\s]", "", text.lower())
    words = cleaned.split()[:6]
    slug = "-".join(words)
    return slug[:max_len].strip("-") or "capture"


def write_capture(capture: Capture, dry_run: bool = False) -> Path | None:
    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

    date_part = capture.timestamp.split("T")[0] if capture.timestamp else dt.date.today().isoformat()
    # Use uuid hash + matched phrase for stable, unique filename
    phrase_slug = slugify(capture.matched_phrase)
    uuid_short = hashlib.md5(capture.user_uuid.encode("utf-8")).hexdigest()[:6]
    filename = f"{date_part}-{capture.pattern_type}-{phrase_slug}-{uuid_short}.md"
    target = CAPTURES_DIR / filename

    if target.exists():
        return None  # Idempotent: already captured

    if dry_run:
        print(f"WOULD WRITE: {target.relative_to(VAULT_ROOT)}")
        print(f"  pattern: {capture.pattern_type}/{capture.pattern_tag}")
        print(f"  phrase: {capture.matched_phrase}")
        print(f"  quote: {capture.quote[:120]}")
        return target

    body = [
        "---",
        f"creationDate: {date_part}",
        "type: passive-capture",
        f"pattern_type: {capture.pattern_type}",
        f"pattern_tag: {capture.pattern_tag}",
        f"session_id: {capture.session_id}",
        f"user_uuid: {capture.user_uuid}",
        f"matched_phrase: \"{capture.matched_phrase}\"",
        "triage_status: pending",
        "---",
        "",
        f"# Passive capture: {capture.pattern_type}/{capture.pattern_tag}",
        "",
        f"*Captured: {capture.timestamp} from session `{capture.session_id[:8]}`. Matched phrase: `{capture.matched_phrase}`.*",
        "",
        "## Quote (verbatim)",
        "",
        f"> {capture.quote}",
        "",
        "## Full message",
        "",
        capture.full_message,
        "",
        "## Triage",
        "",
        "Choose one of:",
        "- [ ] **Adopt as rule** → move to `CLAUDE.md` or `⚙️ Meta/rules/<file>.md`. Mark this file `triage_status: adopted-rule`.",
        "- [ ] **Adopt as decision** → file via `/decision` workflow into Decision Log. Mark `triage_status: adopted-decision`.",
        "- [ ] **Adopt as lesson** → file into journal entry or panel learning. Mark `triage_status: adopted-lesson`.",
        "- [ ] **Reject** → not load-bearing. Mark `triage_status: rejected` and move to `Passive Captures/Archive/`.",
        "",
        "## Why this surfaced",
        "",
        f"The phrase `{capture.matched_phrase}` matched pattern `{capture.pattern_type}/{capture.pattern_tag}`. Passive-capture is a triage queue, not a commit — only adopted captures should propagate.",
    ]
    target.write_text("\n".join(body), encoding="utf-8")
    return target


# --- Self-test --------------------------------------------------------

SELF_TEST_FIXTURE = [
    {
        "uuid": "test-1",
        "session_id": "test",
        "timestamp": "2026-05-03T10:00:00.000Z",
        "text": "From now on, every time we run the aggregator, we should also check for stale handoffs.",
        "expected": [("rule", "explicit_rule_flag"), ("rule", "habit_rule")],
    },
    {
        "uuid": "test-2",
        "session_id": "test",
        "timestamp": "2026-05-03T10:01:00.000Z",
        "text": "Let's switch to Postgres for new persistence work. The SQLite path is causing lock contention.",
        "expected": [("decision", "switching_decision")],
    },
    {
        "uuid": "test-3",
        "session_id": "test",
        "timestamp": "2026-05-03T10:02:00.000Z",
        "text": "I realized I keep doing the same thing wrong: I forget to check the canonical facts file before quoting numbers.",
        "expected": [("lesson", "realization"), ("lesson", "recurring_pattern")],
    },
    {
        "uuid": "test-4",
        "session_id": "test",
        "timestamp": "2026-05-03T10:03:00.000Z",
        "text": "/journal",  # explicit slash command — should be skipped
        "expected": [],
    },
    {
        "uuid": "test-5",
        "session_id": "test",
        "timestamp": "2026-05-03T10:04:00.000Z",
        "text": "scratch that, ignore this",  # cancellation — should be skipped
        "expected": [],
    },
    {
        "uuid": "test-6",
        "session_id": "test",
        "timestamp": "2026-05-03T10:05:00.000Z",
        "text": "ok",  # too short — should be skipped
        "expected": [],
    },
    {
        "uuid": "test-7",
        "session_id": "test",
        "timestamp": "2026-05-03T10:06:00.000Z",
        "text": "What's the weather like in Bogotá today? I'm planning a walk.",  # no pattern match
        "expected": [],
    },
]


def run_self_test() -> int:
    print("--- self-test ---")
    failures = 0
    for tc in SELF_TEST_FIXTURE:
        captures = detect_captures(tc)
        actual = sorted([(c.pattern_type, c.pattern_tag) for c in captures])
        expected = sorted(tc["expected"])
        if actual != expected:
            failures += 1
            print(f"FAIL ({tc['uuid']}): expected {expected}, got {actual}")
            print(f"  text: {tc['text'][:120]}")
        else:
            print(f"PASS ({tc['uuid']}): {len(captures)} captures, {actual}")
    print(f"\n{len(SELF_TEST_FIXTURE) - failures}/{len(SELF_TEST_FIXTURE)} fixtures pass")
    return 1 if failures else 0


# --- Main flow --------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--scan-today", action="store_true", help="Scan today's transcripts (default)")
    group.add_argument("--scan-since", metavar="YYYY-MM-DD", help="Scan from this date forward")
    group.add_argument("--report", action="store_true", help="Show count of pending captures by type")
    group.add_argument("--triage", action="store_true", help="List untriaged captures")
    group.add_argument("--self-test", action="store_true", help="Run self-test on synthetic fixture")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be captured, write nothing")
    args = parser.parse_args()

    if args.self_test:
        sys.exit(run_self_test())

    if args.report:
        if not CAPTURES_DIR.exists():
            print("No captures directory.")
            return
        counts = {"rule": 0, "decision": 0, "lesson": 0, "other": 0}
        triaged = 0
        for path in CAPTURES_DIR.glob("*.md"):
            try:
                head = path.read_text(encoding="utf-8")[:600]
            except OSError:
                continue
            for ptype in counts:
                if f"pattern_type: {ptype}" in head:
                    counts[ptype] += 1
                    break
            else:
                counts["other"] += 1
            if "triage_status: pending" not in head:
                triaged += 1
        total = sum(counts.values())
        print(f"Total captures: {total} (pending: {total - triaged}, triaged: {triaged})")
        for k, v in counts.items():
            if v:
                print(f"  {k}: {v}")
        return

    if args.triage:
        if not CAPTURES_DIR.exists():
            print("No captures directory.")
            return
        pending = []
        for path in sorted(CAPTURES_DIR.glob("*.md")):
            try:
                head = path.read_text(encoding="utf-8")[:1000]
            except OSError:
                continue
            if "triage_status: pending" in head:
                pending.append(path)
        if not pending:
            print("No pending captures.")
            return
        print(f"Pending captures ({len(pending)}):")
        for path in pending:
            rel = path.relative_to(VAULT_ROOT)
            print(f"  {rel}")
        return

    if args.scan_since:
        try:
            since = dt.datetime.strptime(args.scan_since, "%Y-%m-%d")
        except ValueError:
            print(f"Invalid date: {args.scan_since}", file=sys.stderr)
            sys.exit(2)
    else:
        # default scan-today: from start of today
        today = dt.date.today()
        since = dt.datetime(today.year, today.month, today.day)

    state = load_state()
    transcripts = find_transcripts(since)
    if not transcripts:
        print(f"No transcripts modified since {since.isoformat()}.")
        return

    total_msgs = 0
    total_captures = 0
    written = 0

    for transcript in transcripts:
        sid = transcript.stem
        last_uuid = state["sessions"].get(sid, {}).get("last_uuid")
        messages = extract_user_messages(transcript, last_uuid)
        total_msgs += len(messages)
        for msg in messages:
            captures = detect_captures(msg)
            total_captures += len(captures)
            for cap in captures:
                target = write_capture(cap, dry_run=args.dry_run)
                if target and not args.dry_run:
                    written += 1
            if not args.dry_run:
                state["sessions"].setdefault(sid, {})["last_uuid"] = msg["uuid"]
                state["sessions"][sid]["last_seen"] = msg["timestamp"]

    if not args.dry_run:
        save_state(state)

    mode = "DRY RUN" if args.dry_run else "wrote"
    print(f"Scanned {len(transcripts)} transcript(s), {total_msgs} new user message(s).")
    print(f"{total_captures} capture candidate(s) detected; {mode}: {written}.")
    if not args.dry_run and written:
        print(f"\nReview pending captures: python3 \"{Path(__file__).relative_to(VAULT_ROOT)}\" --triage")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
