#!/usr/bin/env python3
"""
detect-closing-signal.py — UserPromptSubmit hook.

Detects when the user is signaling session close ("bye", "thanks that's all",
"good night", "/wrap-up", emoji-only farewells, multilingual variants), then
runs all FAST deterministic prep work BEFORE the model sees the prompt and
injects a structured system block telling the model exactly what to capture.

Layered defense (this is Layer 1 of 3):
  - Layer 1: this hook (UserPromptSubmit) — pre-execution + signal detection
  - Layer 2: model writes captures using injected paths/inputs
  - Layer 3: session-end-hook.sh (Stop) — aggregators, git, retention, fallback

Why this hook exists: the prior architecture relied entirely on the model
"noticing" closing signals and choosing to read the cascade rule file.
Three brittle steps that failed silently. This hook makes detection
deterministic and pre-resolves all paths so the model only does the
irreducibly creative work (conversation scan + verbatim capture).

Behavior contract:
  - Reads user prompt from stdin (Claude Code hook contract)
  - Detects close signal via language-pack regex + optional Haiku fallback
  - Runs FAST prep (timestamp, paths, marker file, decisions-with-empty-outcome
    list, recently-touched-files list, session-file shell pre-build)
  - Returns additionalContext that injects all of the above plus the cascade
    instructions inline (no separate rule-file read needed)
  - Adds Phase 4b when this session set a `/goal`: that installs a
    session-scoped Stop hook which would block the close, and only the user
    can run `/goal clear`
  - Skips entirely if session is trivial (<5 user messages, no captures detected)
  - Fails open: any error returns empty context, never blocks the user

Performance budget: < 500ms total. Heavy work (aggregators, git, retention)
runs in the Stop hook AFTER the model finishes.

Usage in hooks.json:
  "UserPromptSubmit": [{"hooks": [{"type": "command",
    "command": "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/detect-closing-signal.py"
  }]}]

Or via absolute path during install:
  python3 ~/.claude/skills/ai-brain-starter/hooks/detect-closing-signal.py

Environment variables (all optional):
  VAULT_ROOT — fallback vault path, used only when cwd is not inside a repo
               that declares its own Session End/Close cascade (see
               _lib/vault_root.resolve_vault_root). Defaults to cwd.
               Worktree paths are collapsed to the main vault root so
               session artifacts never strand on a worktree.
  CLOSING_SIGNAL_LANGS — comma-separated language packs to load (default: en,es,pt)
  CLOSING_SIGNAL_DETECTION — "regex" (default) or "hybrid" (regex + Haiku fallback)
  CLOSING_SIGNAL_DEBUG — set to 1 for stderr trace
  ANTHROPIC_API_KEY — required only if CLOSING_SIGNAL_DETECTION=hybrid

User config (CLAUDE.md at the resolved vault root, all optional):
  closingSignals.custom: ["...", ...]   — ADD literal phrases that always fire
                                          (highest-authority positive tier).
  closingSignals.suppress: ["...", ...] — SUBTRACT literal phrases that never
                                          fire, even if a pack matches.
  closingSignals.customOnly: true       — fire ONLY on `custom`; skip the shared
                                          packs entirely (deliberate-close-only).

Output marker file:
  ~/.claude/.closing-signal-{session_id}.json
  Contains: timestamp, matched signal, language, confidence, pre-resolved paths
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _lib.vault_root import resolve_vault_root  # noqa: E402
except Exception:  # fail-open: if the lib cannot load, behave as before
    def resolve_vault_root(cwd: Path, env_vault_root: str | None) -> Path:  # type: ignore
        text = str(Path(env_vault_root) if env_vault_root else cwd)
        norm = text.replace("\\", "/")  # marker must match Windows paths too
        marker = "/.claude/worktrees/"
        if marker in norm:
            return Path(norm.split(marker, 1)[0])
        return Path(text)


def log_debug(msg: str) -> None:
    if os.environ.get("CLOSING_SIGNAL_DEBUG") == "1":
        print(f"[detect-closing-signal] {msg}", file=sys.stderr)


def read_hook_input() -> dict:
    """Read JSON from stdin (Claude Code hook contract)."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except (json.JSONDecodeError, OSError) as e:
        log_debug(f"failed to read hook input: {e}")
        return {}


def emit_passthrough() -> None:
    """Return nothing-special — let the model process normally."""
    print(json.dumps({"continue": True, "suppressOutput": True}))


def emit_context(context: str) -> None:
    """Return injected context to the model."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }))


def find_repo_root() -> Path:
    """Locate this script's repo root (where templates/ lives)."""
    here = Path(__file__).resolve().parent
    # hooks/ is 1 level deep
    candidate = here.parent
    if (candidate / "templates" / "closing-signals").is_dir():
        return candidate
    # Fall back: walk up looking for templates/closing-signals
    for ancestor in here.parents:
        if (ancestor / "templates" / "closing-signals").is_dir():
            return ancestor
    return candidate


def load_language_packs(langs: list[str]) -> dict:
    """Load JSON language packs and merge into a single rule set.

    strict_guards (codified 2026-05-25): override ALL tiers including
    explicit + high_confidence. Used for unambiguous non-close contexts
    (meta-discussion of close cascade, debugging, technical references)
    where false positives are very costly. The original false_positive_guards
    only suppress weak tiers (ambiguous / emoji_only).
    """
    repo = find_repo_root()
    pack_dir = repo / "templates" / "closing-signals"
    merged = {
        "explicit": [],
        "high_confidence": [],
        "ambiguous": [],
        "emoji_only": [],
        "false_positive_guards": [],
        "strict_guards": [],
    }
    for lang in langs:
        path = pack_dir / f"{lang.strip()}.json"
        if not path.is_file():
            log_debug(f"language pack not found: {path}")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log_debug(f"failed to load {path}: {e}")
            continue
        for key in merged:
            merged[key].extend(data.get(key, []))
    return merged


def _extract_quoted_phrases(raw: str) -> list[str]:
    r"""Extract phrases from an inline array like ["a", "b's done", 'c'].

    Handles BOTH double- and single-quoted entries and, crucially, allows an
    apostrophe INSIDE a double-quoted entry ("i'm done", "that's all"). The
    previous extractor (``[\"']([^\"']+)[\"']``) treated every quote char as a
    delimiter, so it fragmented on an inner apostrophe — silently corrupting
    any phrase containing one (e.g. "let's close this session" became the stray
    token "let", which then matched "let me" everywhere, and "i'm done" became
    "i", which matched the "i" in "this"). Returns each phrase re.escaped for
    literal substring matching.
    """
    items: list[str] = []
    for dq, sq in re.findall(r'"([^"]*)"|\'([^\']*)\'', raw):
        phrase = (dq or sq).strip()
        if phrase:
            items.append(re.escape(phrase))
    return items


def load_user_custom_signals(vault_root: Path) -> list[str]:
    """Read user's CLAUDE.md for closingSignals.custom: [...]."""
    claude_md = vault_root / "CLAUDE.md"
    if not claude_md.is_file():
        return []
    try:
        text = claude_md.read_text(encoding="utf-8")
    except OSError:
        return []
    # Look for either YAML frontmatter array or inline list
    match = re.search(
        r"closingSignals\.custom\s*[:=]\s*\[([^\]]*)\]",
        text,
        re.IGNORECASE,
    )
    if not match:
        return []
    return _extract_quoted_phrases(match.group(1))


def load_user_suppress_signals(vault_root: Path) -> list[str]:
    """Read user's CLAUDE.md for closingSignals.suppress: [...].

    SUBTRACTIVE counterpart to closingSignals.custom. Each entry is a literal
    phrase that must NEVER fire a close for this user — even when a shared
    language pack would otherwise match it. Checked right after strict_guards
    (so it overrides both custom and the packs). This lets a user narrow the
    shared default ("i'm done" / "good night" stop closing *for them*) without
    weakening the public pack for everyone. Opt-in: an absent key yields an
    empty list, so default behavior is byte-identical to before.
    """
    claude_md = vault_root / "CLAUDE.md"
    if not claude_md.is_file():
        return []
    try:
        text = claude_md.read_text(encoding="utf-8")
    except OSError:
        return []
    match = re.search(
        r"closingSignals\.suppress\s*[:=]\s*\[([^\]]*)\]",
        text,
        re.IGNORECASE,
    )
    if not match:
        return []
    return _extract_quoted_phrases(match.group(1))


def load_user_custom_only(vault_root: Path) -> bool:
    """Read user's CLAUDE.md for closingSignals.customOnly: true|false.

    When true, ONLY the user's closingSignals.custom patterns participate in
    positive detection — the shared en/es/pt language-pack tiers are skipped
    entirely. The always-on negative tiers (strict_guards, suppress) still
    apply. This is the "deliberate close only" mode: natural sign-offs (bye /
    good night / i'm done / ya está / thanks that's all) no longer fire for
    this user; only their explicit custom phrases do. Opt-in: absent or false
    leaves the shared packs participating exactly as before.

    Footgun: customOnly true with an EMPTY closingSignals.custom list means
    nothing fires at all (only-my-zero-phrases). Pair it with a populated
    custom list.
    """
    claude_md = vault_root / "CLAUDE.md"
    if not claude_md.is_file():
        return False
    try:
        text = claude_md.read_text(encoding="utf-8")
    except OSError:
        return False
    match = re.search(
        r"closingSignals\.customOnly\s*[:=]\s*(true|false|yes|no|on|off|1|0)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return False
    return match.group(1).lower() in ("true", "yes", "on", "1")


def is_false_positive(prompt: str, guards: list) -> bool:
    """Check if any false-positive guard matches; if so, skip detection."""
    for guard in guards:
        if isinstance(guard, dict):
            pattern = guard.get("pattern", "")
        else:
            pattern = str(guard)
        if not pattern:
            continue
        try:
            if re.search(pattern, prompt, re.IGNORECASE | re.MULTILINE):
                return True
        except re.error as e:
            log_debug(f"bad guard regex '{pattern}': {e}")
            continue
    return False


def classify_signal(
    prompt: str,
    packs: dict,
    custom: list[str],
    suppress: list[str] | None = None,
    custom_only: bool = False,
) -> tuple[str | None, str | None]:
    """Return (confidence, matched_pattern) or (None, None) if no match.

    Confidence levels: explicit > high > ambiguous > emoji.

    Negative tiers (both default off, so behavior is unchanged when unset):
      suppress    — literal phrases that NEVER fire for this user; overrides
                    custom + packs. From closingSignals.suppress in CLAUDE.md.
      custom_only — when True, only `custom` participates as a positive tier;
                    the shared en/es/pt pack tiers are skipped. From
                    closingSignals.customOnly in CLAUDE.md.
    """
    if not prompt or not prompt.strip():
        return (None, None)

    text = prompt.strip()

    # Strong-tier matches (custom, explicit, high_confidence) OVERRIDE
    # false-positive guards. The guard is meant to catch "okay let's continue"
    # mid-conversation transitions — NOT to suppress "okay let's close this
    # session" which contains a clear close verb. Fix shipped 2026-05-12 after
    # an "Okay, let's close this session" prompt was silently suppressed by the
    # `^ok(ay)?[,.\s]+(let'?s|now)...` guard despite high_confidence pattern
    # `\b(let'?s\s+)?close\s+(this|the)\s+session\b` also matching.

    # STRICT guards (codified 2026-05-25): override EVERYTHING, including
    # explicit/high_confidence/custom. Use only for unambiguous non-close
    # contexts where false positives are very costly. Checked FIRST so
    # meta-discussion of close cascade never fires a stub. The original
    # FP guards remain for weak-tier-only suppression — strict guards do
    # NOT override the user's custom patterns, those still win.
    if is_false_positive(text, packs.get("strict_guards", [])):
        log_debug("strict guard matched, suppressing ALL tiers")
        return (None, None)

    # SUPPRESS (personal, subtractive): phrases the user has declared are
    # NEVER a close for them. Checked right after strict_guards — before
    # custom and the shared packs — so it overrides every positive tier.
    # Opt-in via closingSignals.suppress; empty by default (no-op).
    if suppress and is_false_positive(text, suppress):
        log_debug("user suppress phrase matched, suppressing ALL tiers")
        return (None, None)

    # User custom patterns are highest authority — always win, FP guard skipped
    for pattern in custom:
        try:
            if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                return ("explicit", pattern)
        except re.error:
            continue

    # customOnly (personal): the user opted into deliberate-close-only mode.
    # Do NOT fall through to the shared language-pack tiers — only their custom
    # phrases (checked just above) plus the always-on negative tiers
    # (strict_guards, suppress) participate. Natural sign-offs no longer fire.
    # Opt-in via closingSignals.customOnly; default false (no-op).
    if custom_only:
        log_debug("customOnly set and no custom match — skipping shared pack tiers")
        return (None, None)

    # Strong tiers (explicit, high_confidence) override FP guards
    for level in ("explicit", "high_confidence"):
        for pattern in packs.get(level, []):
            try:
                if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                    log_debug(f"matched [{level}] (FP guard bypassed): {pattern}")
                    return (level, pattern)
            except re.error as e:
                log_debug(f"bad pattern '{pattern}': {e}")
                continue

    # For weaker tiers (emoji_only, ambiguous), apply FP guards
    if is_false_positive(text, packs.get("false_positive_guards", [])):
        log_debug("false-positive guard matched (no strong-tier match), skipping")
        return (None, None)

    for level in ("emoji_only", "ambiguous"):
        for pattern in packs.get(level, []):
            try:
                if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                    log_debug(f"matched [{level}]: {pattern}")
                    return (level, pattern)
            except re.error as e:
                log_debug(f"bad pattern '{pattern}': {e}")
                continue

    return (None, None)


def ambiguous_haiku_check(prompt: str) -> bool:
    """Optional: ask Haiku 'is this a session-close intent?' for ambiguous cases.

    Only fires if CLOSING_SIGNAL_DETECTION=hybrid AND ANTHROPIC_API_KEY set.
    Returns True if Haiku says yes.
    """
    if os.environ.get("CLOSING_SIGNAL_DETECTION") != "hybrid":
        return False
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # type: ignore
    except ImportError:
        log_debug("anthropic SDK not installed; hybrid detection unavailable")
        return False
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=(
                "You classify whether a user message is a session-close intent. "
                "Reply with exactly 'yes' or 'no'. A session-close intent is a "
                "natural sign-off: saying goodbye, signaling done for now, "
                "ending the conversation. Mid-conversation transitions like "
                "'ok now do X' or 'great, what's next' are NOT session-close."
            ),
            messages=[{"role": "user", "content": prompt[:500]}],
        )
        answer = (resp.content[0].text or "").strip().lower() if resp.content else ""
        return answer.startswith("yes")
    except Exception as e:
        log_debug(f"hybrid haiku check failed: {e}")
        return False


def derive_worktree(cwd: Path) -> str:
    """Derive worktree slug from path or .git file."""
    cwd_str = str(cwd)
    m = re.search(r"/\.claude/worktrees/([^/]+)", cwd_str)
    if m:
        return m.group(1)
    git_file = cwd / ".git"
    if git_file.is_file():
        try:
            text = git_file.read_text(encoding="utf-8")
            m2 = re.search(r"worktrees/([^/\s]+)", text)
            if m2:
                return m2.group(1).strip()
        except OSError:
            pass
    return "main"


def find_meta_dir(vault_root: Path) -> Path:
    """Auto-detect Meta folder. Returns the canonical dir for THIS vault.

    Resolution order:
      1. Common explicit prefixes first (emoji + plain). Cheap stat checks
         beat directory iteration on large vaults and avoid relying on
         iterdir order.
      2. Iteration fallback for any other suffix-"Meta" dir name.
      3. Final fallback: plain `Meta` under vault_root. Caller MUST verify
         the returned path exists before emitting it (see verify_meta_dir).

    The emoji-first probe fixes a class of bugs where iterdir() either
    returned partial results, hit a NFD/NFC normalization mismatch on
    macOS APFS, or failed silently. When `⚙️ Meta` exists on disk, this
    function now returns it deterministically without iterating.
    """
    # 1. Explicit prefix probes (deterministic, cheap, robust).
    for candidate_name in ("⚙️ Meta", "Meta"):
        candidate = vault_root / candidate_name
        if candidate.is_dir():
            return candidate

    # 2. Iterate as fallback for unconventional suffixes.
    try:
        for child in sorted(vault_root.iterdir()):
            if child.is_dir() and child.name.endswith("Meta"):
                return child
    except OSError:
        pass

    # 3. Last resort. Caller verifies existence.
    return vault_root / "Meta"


def verify_meta_dir(meta_dir: Path) -> tuple[bool, str]:
    """Confirm the resolved Meta dir exists and contains the expected layout.

    Returns (ok, reason). The cascade caller should refuse to emit pre-resolved
    paths when ok is False — better to skip the cascade than to send the model
    to a phantom directory where its writes will be silently lost.
    """
    if not meta_dir.exists():
        return False, f"resolved meta_dir does not exist: {meta_dir}"
    if not meta_dir.is_dir():
        return False, f"resolved meta_dir is not a directory: {meta_dir}"
    sessions = meta_dir / "Sessions"
    if not sessions.exists():
        # Auto-create the standard subdirs the cascade will write into.
        try:
            sessions.mkdir(parents=True, exist_ok=True)
            (meta_dir / "Decisions").mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return False, f"could not create Sessions/ subdir: {e}"
    return True, ""


def count_user_messages(transcript_path: str | None) -> int:
    """Count user messages in the transcript to enforce skip-if-trivial rule."""
    if not transcript_path:
        return 99  # unknown — assume substantive
    p = Path(transcript_path)
    if not p.is_file():
        return 99
    count = 0
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "user" or (
                    isinstance(rec.get("message"), dict)
                    and rec["message"].get("role") == "user"
                ):
                    count += 1
    except OSError:
        return 99
    return count


# /goal renders in the transcript as a client-side command record:
#   <command-name>/goal</command-name> ... <command-args>CONDITION</command-args>
# Bare `/goal` (no args) is a status query; `/goal clear` clears. Anything else
# sets the condition.
_GOAL_CMD = "<command-name>/goal</command-name>"
_GOAL_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)


def _record_text(rec: dict) -> str:
    """Flatten a transcript record's message content to plain text."""
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            blk.get("text", "") for blk in content if isinstance(blk, dict)
        )
    return ""


def active_session_goal(transcript_path: str | None) -> str | None:
    """Return the condition of a /goal still set on this session, else None.

    `/goal <condition>` installs a session-scoped Stop hook that blocks stopping
    until the condition holds, so a goal that is still set will fight a
    deliberate close: the cascade finishes, Stop fires, the goal hook blocks,
    and the model is re-invoked with nothing left to do.

    The condition lives only in Claude Code's in-memory session state — there is
    no goal state file — so the transcript is the sole readable record. Replay
    the /goal invocations in order and keep the last one that SET a goal.

    Blind spot, handled model-side: a goal auto-clears once its condition is
    met, and that leaves no transcript marker. So a True here means "a goal was
    set and never explicitly cleared", NOT "a goal is definitely still live".
    The injected phase tells the model to stay silent when the condition was in
    fact met — nagging after success is the one case the harness rules out.
    """
    if not transcript_path:
        return None
    p = Path(transcript_path)
    if not p.is_file():
        return None
    goal: str | None = None
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                if _GOAL_CMD not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = _record_text(rec)
                if _GOAL_CMD not in text:
                    continue  # matched inside some other field, not a command
                m = _GOAL_ARGS_RE.search(text)
                args = (m.group(1).strip() if m else "")
                if not args:
                    continue  # bare /goal is a status query
                if args.lower() == "clear":
                    goal = None
                else:
                    goal = args
    except OSError:
        return None
    return goal


def resolve_todo_files(vault_root: Path, meta_dir: Path) -> list[Path]:
    """Return the vault's canonical to-do file(s), most-specific first.

    Codified 2026-08-07 after a session's to-dos landed only in the session
    file and never reached the to-do list, so the next morning's /rise could
    not see them. The cascade now gets the destination path pre-resolved
    instead of leaving the model to guess where to-dos live.

    Source of truth is `todo_files:` in rise-config.md (the same list /rise
    ranks from), so close and morning always agree. Falls back to the common
    filenames when there is no config.
    """
    found: list[Path] = []
    config = meta_dir / "rise-config.md"
    if config.is_file():
        try:
            text = config.read_text(encoding="utf-8")
        except OSError:
            text = ""
        block = re.search(r"^\s*todo_files:\s*$((?:\n\s*-\s*.+)+)",
                          text, re.MULTILINE)
        if block:
            for line in block.group(1).splitlines():
                item = line.strip().lstrip("-").strip().strip('"').strip("'")
                if not item:
                    continue
                candidate = vault_root / item
                if candidate.is_file():
                    found.append(candidate)
    if not found:
        for name in ("Por hacer.md", "To do.md", "Get to-do.md",
                     "Team to-do.md", "Current Priorities.md",
                     "Prioridades Actuales.md"):
            for base in (vault_root, meta_dir):
                candidate = base / name
                if candidate.is_file() and candidate not in found:
                    found.append(candidate)
    return found[:4]


def list_decisions_with_empty_outcome(meta_dir: Path) -> list[str]:
    """Return relative paths of Decisions/ files where Outcome: is blank."""
    decisions_dir = meta_dir / "Decisions"
    if not decisions_dir.is_dir():
        return []
    out = []
    for path in sorted(decisions_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        # Match `Outcome:` followed only by whitespace or a placeholder marker
        if re.search(r"^\s*outcome:\s*(\(pending\)|tbd|—|-|)\s*$",
                     text, re.IGNORECASE | re.MULTILINE):
            out.append(path.name)
    return out[:20]  # cap to keep injected context small


def pre_build_session_shell(
    sessions_dir: Path,
    timestamp_file: str,
    worktree: str,
    timestamp_human: str,
) -> Path:
    """Pre-create session file with frontmatter + section headers.

    Model only fills in the body. Returns the absolute path.
    """
    sessions_dir.mkdir(parents=True, exist_ok=True)
    path = sessions_dir / f"{timestamp_file}-{worktree}.md"
    if path.exists() and path.stat().st_size > 0:
        # Don't clobber a partial session file from earlier in the same minute
        return path
    shell = f"""---
creationDate: {timestamp_human}
type: session
worktree: {worktree}
session_date: {timestamp_human[:10]}
session_label: "update pending"
---

# Session — {timestamp_human}

## What happened

<!-- Brief summary of the session: what was worked on, what was produced. -->

## Decisions

<!-- New decisions logged this session. Per-decision files in ⚙️ Meta/Decisions/. -->

## Captures

<!-- Journal seeds (verbatim), writing notes, actionable content. -->

## To-dos filed

<!-- Personal vs team, with destination file. -->

## Delegations

<!-- Items handed to others, with drafted message. -->

## Pending / incomplete

<!-- Background tasks, killed runs, items deferred to next session. -->
"""
    try:
        path.write_text(shell, encoding="utf-8")
    except OSError as e:
        log_debug(f"failed to write session shell: {e}")
    return path


def write_marker(
    session_id: str,
    payload: dict,
) -> Path:
    """Write closing-signal marker file for the Stop hook to find."""
    home = Path.home()
    marker_dir = home / ".claude"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f".closing-signal-{session_id}.json"
    try:
        marker.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        log_debug(f"failed to write marker: {e}")
    return marker


def build_injected_context(
    confidence: str,
    matched: str,
    timestamp_human: str,
    timestamp_file: str,
    worktree: str,
    vault_root: Path,
    meta_dir: Path,
    session_file: Path,
    decisions_dir: Path,
    captures_file: Path,
    pending_outcomes: list[str],
    is_trivial: bool,
    is_ambiguous: bool,
    goal_condition: str | None = None,
    todo_files: list[Path] | None = None,
) -> str:
    """Compose the system block injected into the model's context.

    Kept under ~600 tokens. All paths pre-resolved. Cascade phases summarized
    inline so the model doesn't need to read a separate rule file.
    """
    if is_trivial:
        # A live /goal blocks stopping regardless of how small the session was,
        # so the goal note survives the skip-if-trivial rule.
        return (
            f"SESSION CLOSE detected (signal: {matched!r}, confidence: {confidence}). "
            f"This session is trivial (<5 user messages). Per the skip rule, "
            f"skip the cascade and just say a clean goodbye. The Stop hook will "
            f"still log a timestamp."
            + _goal_clear_block(goal_condition, trivial=True)
        )

    if is_ambiguous:
        # User said "ok" or similar — confirm before running the heavy cascade
        return (
            f"POSSIBLE SESSION CLOSE detected (signal: {matched!r}, confidence: ambiguous). "
            f"This could be a close, or it could be a mid-conversation transition. "
            f"Before running the cascade, confirm in ONE short sentence: "
            f"\"Wrapping up, or keeping going?\" — and then act on the answer. "
            f"If they confirm close, follow the FULL cascade instructions below. "
            f"If they keep going, just continue normally.\n\n"
            + _full_cascade_block(
                timestamp_human, timestamp_file, worktree, vault_root,
                meta_dir, session_file, decisions_dir, captures_file,
                pending_outcomes, None, todo_files,
            )
        )

    return (
        f"SESSION CLOSE detected (signal: {matched!r}, confidence: {confidence}, "
        f"language pack matched). Run the FULL cascade.\n\n"
        + _full_cascade_block(
            timestamp_human, timestamp_file, worktree, vault_root,
            meta_dir, session_file, decisions_dir, captures_file,
            pending_outcomes, goal_condition, todo_files,
        )
    )


def _goal_clear_block(goal_condition: str | None, trivial: bool = False) -> str:
    """Tail block telling the model to hand `/goal clear` back to the user.

    Empty string when this session never set a goal — no goal, no nag.
    """
    if not goal_condition:
        return ""
    cond = " ".join(goal_condition.split())
    if len(cond) > 160:
        cond = cond[:157] + "..."
    if trivial:
        instruction = "End your goodbye with one plain line asking them to type:"
    else:
        instruction = (
            'Add one plain line to the PHASE 4 summary, after "Anything I '
            'missed?", asking them to type:'
        )
    return f"""

PHASE 4b — CLEAR THE SESSION GOAL (fires only because this session set one):
  Goal on record: "{cond}"

`/goal` installed a session-scoped Stop hook that blocks stopping until that
condition holds. A close is a deliberate EARLY stop, so unless the condition is
actually met the goal hook will block this close and re-invoke you with nothing
left to do — the loop the user is trying to end.

You CANNOT clear it yourself: `/goal` is a client-side command with no tool
behind it. Only the user can. {instruction}

  /goal clear

EXCEPTION — if the goal's condition was genuinely MET this session, it already
auto-cleared. Say nothing. Telling the user to clear a goal that succeeded is
the one case the harness explicitly rules out."""


def _full_cascade_block(
    timestamp_human: str,
    timestamp_file: str,
    worktree: str,
    vault_root: Path,
    meta_dir: Path,
    session_file: Path,
    decisions_dir: Path,
    captures_file: Path,
    pending_outcomes: list[str],
    goal_condition: str | None = None,
    todo_files: list[Path] | None = None,
) -> str:
    """The reusable cascade-instruction block."""
    pending = ", ".join(pending_outcomes) if pending_outcomes else "(none)"
    # Pre-resolve the to-do destination the same way the Time Tracking surface
    # is resolved: the model trusts the injected path instead of inferring it.
    if todo_files:
        todo_line = "  To-do file(s):    " + "\n                    ".join(
            str(p) for p in todo_files
        )
        todo_phase2 = f"""  • APPEND every new to-do to the to-do file(s) resolved above, under a
    dated heading ("## <source> — YYYY-MM-DD"). Writing a to-do ONLY into
    the session file does not count as filing it: /rise ranks from the
    to-do file, so anything that stops at the session file is invisible
    the next morning. Each line self-contained (context prefix in
    brackets + wikilink or path) and marked 🔴 if it blocks something.
    Deferred/incomplete items from Phase 0b go here too, with their reason."""
    else:
        todo_line = ("  To-do file(s):    (none found — create one at the vault root "
                     "and list it under todo_files: in rise-config.md)")
        todo_phase2 = ("""  • No to-do file resolved. Write the to-dos into the session file AND
    tell the user their to-dos have nowhere durable to land.""")
    # Pre-resolve the Time Tracking surface (codified 2026-05-14). The Phase 1
    # bullet used to say "if vault uses it" and require the model to infer
    # presence; that produced a false-negative when the model checked the
    # wrong directory. Resolving here mirrors the pattern for session_file +
    # captures_file: the model trusts the injected path, no inference.
    time_tracking_file = meta_dir / "Time Tracking.md"
    if time_tracking_file.is_file():
        tt_line = (
            f"  Time tracking:    {time_tracking_file}  "
            f"(append today's entry under a ## YYYY-MM-DD heading; create the heading if missing)"
        )
    else:
        tt_line = "  Time tracking:    (not in this vault, skip the time-tracking step)"

    # Phase 0a is self-healing: the deterministic runner is an OPTIONAL,
    # separately-installed vault script. Reference it only when it actually
    # exists; otherwise tell the model to run the aggregators by hand. Before
    # 2026-06-07 the cascade hard-referenced session-close-runner.sh as mandatory
    # and claimed skipping it "guaranteed a Stop-hook block" — but the runner
    # shipped without its script and the verify hook is opt-in, so every vault
    # without the script was pointed at a missing file as the "single most
    # important step."
    runner = meta_dir / "scripts" / "session-close-runner.sh"
    if runner.is_file():
        phase_0a = f"""PHASE 0a — RUN THE CANONICAL RUNNER FIRST. One bash call runs the
deterministic aggregation (Phases 0c-0e + the session/decision aggregators) and
writes the report the optional verify-session-close-cascade Stop hook checks. It
runs whichever sub-scripts are installed and skips the rest (never fatal):

  bash "{runner}"

After it finishes, walk the remaining Phases (0b -> 1 -> 2 -> 2b -> 3) below. Do
NOT re-walk 0c/0d/0e by hand — the runner already did those."""
    else:
        phase_0a = f"""PHASE 0a — DETERMINISTIC AGGREGATION (run by hand; the canonical
runner is not installed in this vault — expected on vaults that predate it or
have not re-synced scripts, and NOT a blocker). Run whichever of these exist
under "{meta_dir}/scripts/", each non-fatal, with VAULT_ROOT set:
  - aggregate-sessions.py    (refreshes Last Session.md)
  - aggregate-decisions.py   (refreshes the Decision Log)
Then walk Phases 0b -> 1 -> 2 -> 2b -> 3 below."""
    return f"""SESSION CLOSE — pre-resolved context (use these exact values):

  Timestamp:        {timestamp_human}
  Worktree:         {worktree}
  Vault root:       {vault_root}
  Session file:     {session_file}  (already pre-built with frontmatter + headers; fill in the body)
  Decisions dir:    {decisions_dir}  (write per-decision files here, slug-named)
  Captures file:    {captures_file}
{todo_line}
{tt_line}
  Decisions with empty Outcome (review for backfill): {pending}

{phase_0a}

PHASE 0b — Incomplete-work gate (DO THIS AFTER 0a, before any writes):
Surface any background tasks still running, pipeline phases killed mid-run,
errors not retried. Ask the user "finish now, or defer?" Wait for their call.
If nothing incomplete: say "No incomplete work" and proceed.

PHASE 0c / 0d / 0e — covered in Phase 0a (by the runner if installed, else the
manual aggregation). Listed below only as reference for what 0a covered; do not
re-execute these by hand.
  Phase 0c — Consumable-artifact cleanup: scans Handoffs/ + consumes_when frontmatter.
  Phase 0d — Orphan task-list guard: runs check-orphan-task-lists.py.
  Phase 0e — Launchd health check: runs check-launchd-health.sh.

PHASE 1 — Single-pass conversation scan (compose all in memory before writing):
  • Belief shifts: did the user end the session believing something different?
  • Journal seeds: VERBATIM quotes where they revealed a belief, observation,
    or change of mind. Tag emotional ones [emotional]. Never reword.
  • Writing note candidates (if user has a Substack/blog setup): apply kill
    conditions before drafting (no "I did today", no LinkedIn-thought-leader
    tone, no diary, must read as universal observation). Bilingual pair if
    configured.
  • Actionable content: strategy, product insights, partnership leads. File
    to canonical location per the vault map; default to Captures.
  • To-dos: separate personal vs team. Apply self-contained capture rule —
    each task must include a context prefix in brackets, a wikilink, a URL,
    or a file path so it stands alone when surfaced out of session context.
  • To-do reconciliation: check off completed items in Get to-do, team to-do,
    Current Priorities. Match by substance, not exact wording.
  • Decision logging: any new decisions get a per-decision file in Decisions/
    with What/Why/Floor/Stakes/Speed/Outcome.
  • Decision outcome backfill: review the empty-outcome list above; fill in
    Outcomes where this session resolved them.
  • Delegations: items for others get @Name + drafted Slack/WhatsApp/email
    message ready to send.
  • Time tracking entry: append to the Time tracking file resolved above
    (path is authoritative, do NOT search for it or claim it doesn't exist).
    One line, format "HH:MMam - HH:MMam | Category | Brief", inferred from
    conversation. If the resolved path says "(not in this vault, skip)",
    skip this step. Otherwise: append under today's date heading; create
    the heading if missing.

PHASE 2 — Batch writes (one tool-call block, append never overwrite):
  • Fill in the pre-built session file (paths above).
{todo_phase2}
  • Create per-decision files in Decisions/ as needed.
  • Append journal seeds to Captures.
  • Personal vs team firewall: never let personal content leak to a team vault.

PHASE 2b — vault-safe-commit IS YOUR MANUAL JOB (codified 2026-05-25
after the cascade double-fire incident). You MUST run vault-safe-commit.sh
with explicit paths for every session-close artifact you wrote (session
file + decisions + captures + time-tracking + any rule/inventory edits)
BEFORE drafting the goodbye. The verify-session-close-cascade Stop hook
checks for uncommitted session-close artifacts and BLOCKS the close if
any are present — earlier docs said the post-Stop hook would auto-commit
this, which was wrong: the verify hook fires BEFORE the post-Stop hook,
so trusting auto-commit causes a hard block + forced re-run.

The post-Stop hook handles ONLY: aggregators (aggregate-sessions.py /
aggregate-decisions.py), retention cleanup (stubs >7d), worktree-archive
preparation. It does NOT auto-commit; that is Phase 2b above and it is
your job before goodbye.

Phases 0c/0d/0e and the Phase 3 audit are MODEL-SIDE. The post-Stop hook
does NOT run them.

PHASE 1.8 — TEAM BROADCAST (per workspace, MODEL-SIDE, OPT-IN; physically
here in run order because broadcasts must follow Phase 2 writes — the
"1.8" name matches the canonical reference in chief-of-staff/SKILL.md):
SKIP this phase entirely unless the OPTIONAL companion skill is installed:
  ~/.claude/skills/team-broadcast/scripts/auto-send.py
If absent (the common case for solo / personal-vault users): do nothing,
do not narrate, move on to Phase 3. The companion skill is install-it-
yourself; ai-brain-starter does NOT auto-install it.

If installed AND this session shipped, decided, or unblocked anything
the team should know about, broadcast via:

  python ~/.claude/skills/team-broadcast/scripts/auto-send.py \\
    {{onde|mycelium}} --trigger session-close --body-file <file>

Use --body-file with the plain 5th-grader recap THIS conversation already
showed the user (do NOT re-derive or re-scan; --allow-rescan is an escape
hatch, not the default). Body lives in scratchpad; the script consumes
+ removes it after posting.

Per-workspace rule: post to EACH workspace where the work is materially
relevant. A typical product-ticket ship -> that product's workspace only.
A change to a shared substrate (an *-mcp, a public skill) -> the
substrate's workspace. Cross-umbrella work -> both.

SKIP conditions (broadcast nothing):
  - Tiny / housekeeping session (close cascade alone, no substantive ship)
  - Personal-only work (journaling, coaching, calendar, finance)
  - Already broadcast earlier this session

Dry-run first (--dry-run) when uncertain about content or audience; go
live once the dry-run output reads clean. Audit log is automatic.

If a workspace's Slack creds are missing, the script fails loud — do NOT
silently swallow. Report the failure in Phase 4 + add a to-do to fix the
creds.

PHASE 3 — Functional audit (conditional, MODEL-SIDE):
If this session shipped code or docs to a PUBLIC REPO that users download
(ai-brain-starter, humanizer, mycelium-site, any *-mcp, etc.), run the
6-step audit before close:
  1. Syntax: `python3 -m py_compile` every new/modified .py;
     `bash -n` every new/modified .sh;
     `python3 -c "import json; json.load(open(...))"` every new/modified .json.
  2. Path resolution: grep every `~/.claude/skills/...` path in docs/templates
     against the actual filesystem. Every path must resolve.
  3. Orphan scan: grep every new file under hooks/, scripts/, templates/
     against the docs that should reference it. Unreferenced = invisible.
  4. Smoke test: invoke each new script with `--help` or minimal args.
  5. Misleading copy: search shipped docs for "this hook blocks X" /
     "three gates enforce Y" that imply auto-installation. If the artifact
     isn't auto-installed, rewrite to "opt-in, install via:".
  6. Relative link check: resolve every `](...)` relative link in modified
     README/docs against the filesystem.
Report: "Audit: N python OK, M bash OK, K JSON OK, P paths resolved,
Q orphans found + fixed, R smoke tests passed."
If session shipped to a public repo and you SKIP this audit, you've left
the close cascade incomplete. CI covers the bulk-mechanical checks
(syntax, em-dash, cross-reference) but does NOT cover smoke tests,
misleading copy, or unreferenced files. The audit catches what CI can't.

PHASE 4 — Final summary (one line, after audit if Phase 3 fired):
"Filed X seeds, Y to-dos (yours: A, delegations: B), Z decisions, checked
off M items[, broadcast to <workspaces> if Phase 1.8 fired]. Anything I
missed?"

Then say goodbye in the user's primary language, warm, no machinery
narration.{_goal_clear_block(goal_condition)}"""


def main() -> int:
    start = time.time()
    try:
        hook_input = read_hook_input()
        prompt = (hook_input.get("prompt") or "").strip()
        session_id = hook_input.get("session_id") or "unknown"
        transcript_path = hook_input.get("transcript_path")
        cwd = Path(hook_input.get("cwd") or os.getcwd())

        if not prompt:
            emit_passthrough()
            return 0

        # Skip detection on Stop-hook-feedback prompts. Claude Code injects
        # blocking-hook stderr output as the next "user message" so the model
        # can self-correct. These are NOT user-initiated close signals; letting
        # them through creates a retry-loop where every Stop-hook block spawns
        # a new pre-built session file at a new timestamp.
        #
        # Codified 2026-05-24 after verify-session-close-cascade kept blocking
        # the session close (legitimately — uncommitted session artifacts), and
        # its feedback message contained "claims to close the session" which
        # re-fired this hook's `\bclose\s+(this|the)\s+session\b` regex,
        # spawning stubs at T10-55, T11-00 on each retry. Same bug class as
        # CASCADE-PHASE-SILENT-SKIP: hook-feedback re-injection is not a
        # user-prompt event and must be filtered before any signal detection.
        #
        # Expanded 2026-05-24 (T14 round): the original `startswith` check
        # missed re-injections that arrived with prepended system-reminder
        # context, and `BLOCKED by` was bounded to the first 300 chars which
        # also missed wrapped messages. Now we scan the first ~2KB for any
        # of the distinctive markers, including verifier-hook names that
        # uniquely identify Claude Code feedback re-injection. Same session
        # spawned 6 spurious stubs (T14-16, T14-20, T14-23, T14-25, T14-27,
        # T14-30) before the broader filter shipped.
        prefix = prompt[:2000]
        feedback_markers = (
            "Stop hook feedback:",
            "Hook feedback:",
            "BLOCKED by ",
            "verify-session-close-cascade",
            "verify-discoverability-on-close",
            "verify-cascade",
        )
        if any(m in prefix for m in feedback_markers):
            log_debug("Stop-hook-feedback prompt, skipping close detection")
            emit_passthrough()
            return 0

        # Resolve the vault root. Priority: (1) the nearest ancestor of cwd
        # that declares its own Session End/Close cascade — even when a
        # global VAULT_ROOT default is configured, so a session rooted in
        # its own vault-shaped repo resolves to itself, not an unrelated
        # default vault; (2) VAULT_ROOT env var; (3) cwd, worktree-collapsed
        # so a close cascade firing inside a worktree never strands
        # artifacts on its throwaway claude/<slug> branch. See
        # _lib/vault_root.py for the full contract.
        vault_root = resolve_vault_root(cwd, os.environ.get("VAULT_ROOT"))

        langs = [
            x.strip() for x in
            os.environ.get("CLOSING_SIGNAL_LANGS", "en,es,pt").split(",")
            if x.strip()
        ]
        packs = load_language_packs(langs)
        custom = load_user_custom_signals(vault_root)
        suppress = load_user_suppress_signals(vault_root)
        custom_only = load_user_custom_only(vault_root)

        confidence, matched = classify_signal(
            prompt, packs, custom, suppress, custom_only
        )

        # Hybrid: ambiguous matches OR no match get a Haiku second look
        if confidence in (None, "ambiguous"):
            if ambiguous_haiku_check(prompt):
                confidence = confidence or "high_confidence"
                matched = matched or prompt[:60]

        if confidence is None:
            log_debug(f"no signal match in: {prompt[:80]!r}")
            emit_passthrough()
            return 0

        # Resolve all paths up front. Verify the resolution lands on a real
        # directory before emitting the cascade — a phantom meta_dir sends
        # the model writes that the aggregator will never pick up.
        meta_dir = find_meta_dir(vault_root)
        ok, reason = verify_meta_dir(meta_dir)
        if not ok:
            log_debug(f"meta_dir verification failed, skipping cascade: {reason}")
            emit_passthrough()
            return 0
        sessions_dir = meta_dir / "Sessions"
        decisions_dir = meta_dir / "Decisions"
        captures_file = meta_dir / "Session Captures.md"
        todo_files = resolve_todo_files(vault_root, meta_dir)

        worktree = derive_worktree(cwd)
        now = datetime.now()
        timestamp_human = now.strftime("%Y-%m-%d %H:%M")
        timestamp_file = now.strftime("%Y-%m-%dT%H-%M")

        user_msg_count = count_user_messages(transcript_path)
        is_trivial = user_msg_count < 5

        is_ambiguous = (confidence == "ambiguous")

        # Pre-build session file shell unless trivial
        session_file = sessions_dir / f"{timestamp_file}-{worktree}.md"
        if not is_trivial:
            session_file = pre_build_session_shell(
                sessions_dir, timestamp_file, worktree, timestamp_human,
            )

        # Pre-fetch decisions with empty outcomes
        pending_outcomes = []
        if not is_trivial:
            pending_outcomes = list_decisions_with_empty_outcome(meta_dir)

        # Write marker for Stop hook
        write_marker(session_id, {
            "timestamp": timestamp_human,
            "timestamp_file": timestamp_file,
            "matched_signal": matched,
            "confidence": confidence,
            "language_packs": langs,
            "worktree": worktree,
            "vault_root": str(vault_root),
            "meta_dir": str(meta_dir),
            "session_file": str(session_file),
            "user_msg_count": user_msg_count,
            "is_trivial": is_trivial,
            "is_ambiguous": is_ambiguous,
            "elapsed_ms": int((time.time() - start) * 1000),
        })

        context = build_injected_context(
            confidence=confidence,
            matched=matched or "",
            timestamp_human=timestamp_human,
            timestamp_file=timestamp_file,
            worktree=worktree,
            vault_root=vault_root,
            meta_dir=meta_dir,
            session_file=session_file,
            decisions_dir=decisions_dir,
            captures_file=captures_file,
            pending_outcomes=pending_outcomes,
            is_trivial=is_trivial,
            is_ambiguous=is_ambiguous,
            goal_condition=active_session_goal(transcript_path),
            todo_files=todo_files,
        )
        emit_context(context)
        log_debug(f"injected context for {confidence} signal in {int((time.time() - start) * 1000)}ms")
        return 0
    except Exception as e:  # never block the user on hook errors
        log_debug(f"unexpected error: {e!r}")
        emit_passthrough()
        return 0


if __name__ == "__main__":
    # The injected cascade carries non-ASCII (em dashes, bullets, the ⚙️ Meta
    # path). On a cp1252 console that print raises UnicodeEncodeError and the
    # close cascade dies silently — the SEV-1 class this hook was pinned under
    # in utf8-stdout-baseline.txt. Guard the streams before main() writes.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
