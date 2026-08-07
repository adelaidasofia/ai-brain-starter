"""closing_claim — single source of truth for "is this assistant message
claiming to close the session?"

Used by the Stop-side verify hooks (verify-session-close-cascade.py,
verify-discoverability-on-close.py), which previously each carried their own
DRIFTED copy of CLOSING_PATTERNS / NEGATION_PATTERNS / is_closing_claim.

Hazard this guards (MYC-791, promoted to High after it tore down a worktree
mid-session 2026-06-19): a guard that scans assistant text for close PHRASES
false-fires when the assistant is MENTIONING those phrases rather than USING
them — quoting a sign-off as an example, discussing the close machinery, or
defining what does/doesn't count as a close. The distinction is USE vs MENTION.
The live incident: a report that listed "good night" / "buenas noches" as
quoted examples of phrases that should NOT auto-close was itself read as a
close claim, blocked the turn, and triggered worktree archive-prep.

Resolution (USE beats MENTION):
  1. Strip MENTION spans before matching — fenced code, inline backticks,
     markdown blockquote lines, and double-quote spans. A close phrase that
     survives ONLY inside a mention is a reference, not a claim. (Genuine
     closes are never quoted, so they survive stripping and still fire.)
  2. If an explicit DISCUSSION / DEFINITIONAL marker is present (terms that do
     not appear in a genuine first-person sign-off), it is not a claim.
  3. Otherwise, a CLOSING_PATTERN in the stripped text IS a claim.

Single-quote spans are intentionally NOT stripped: apostrophes in ordinary
prose ("don't", "it's") would fragment the span and over-strip. Double-quote /
backtick / code / blockquote stripping covers the realistic mention forms.

Pure stdlib, no imports beyond re. Fail-open is the caller's job (a caller that
cannot read the transcript passes "" and gets False).
"""

from __future__ import annotations

import re

__all__ = ["is_closing_claim", "strip_mentions", "CLOSING_PATTERNS", "DISCUSSION_MARKERS"]

# Union of the two hooks' previously-duplicated pattern lists, de-drifted.
# Matched case-insensitively (re.IGNORECASE | re.MULTILINE) against the
# MENTION-stripped text.
CLOSING_PATTERNS = [
    # English — first-person closure claims
    r"\bclosing the session\b",
    r"\bclosing this session\b",
    r"\bsession (?:is )?closed\b",
    r"\bclosing now\b",
    r"\bwrapping up\b",
    r"\bwrapping (?:the|this) session\b",
    r"\bsession (?:summary|wrap[- ]?up|recap)\b",
    r"\bcascade complete\b",
    r"\brunning the (?:close )?cascade\b",
    r"\bwriting the session artifact\b",
    r"\bsession[- ]end cascade (?:hook should pick up|will handle)\b",
    r"\bsafe to archive\b.*\bworktree\b",
    r"\bdogfood install (?:complete|done)\b.*\bsession\b",
    r"\bsigning off\b",
    r"\bgood ?night\b",
    r"^closing\.?\s*$",
    r"^##\s+.*[—-]\s*final summary",
    r"\bfinal summary\b.*\bsession\b",
    # Spanish — late-night / closure phrasings
    r"\bbuenas noches\b",
    r"\bque (?:descanses|tengas|duermas)\b",
    r"\bhasta (?:mañana|luego|pronto)\b",
    r"\bdulces sueños\b",
    r"\bnos vemos mañana\b",
    r"\bcerrando la sesión\b",
    r"\bcierro la sesión\b",
    r"\bsesión cerrada\b",
    r"\bchao\b",
]

# Discussion / definitional context — if any appears, the message is ABOUT
# closing, not claiming it. Deliberately conservative: every term here is one
# that does NOT appear in a genuine first-person sign-off, so this never
# suppresses a real close (which would re-open the gallant-kalam
# incomplete-close gap). The MENTION stripping above does the heavy lifting;
# this is the secondary net for unquoted meta-discussion.
DISCUSSION_MARKERS = [
    # original NEGATION terms (union of both hooks)
    r"how do we make sure",
    r"did you run",
    r"did(?:n't| not) run",
    r"keeps not happening",
    r"\bI should have\b",
    r"\bhow to\b",
    r"why did(?:n't| not)",
    r"\bthe fix is\b",
    r"don't (?:want to|need to) close",
    r"\bnot closing\b",
    r"\bbefore closing\b",
    r"\bdiscussing\b",
    r"\bthe rule\b",
    # mention-vs-use / definitional — absent from a genuine sign-off
    r"false[- ]?fire",
    r"false[- ]?positive",
    r"is(?:n't| not) a close",
    r"\bthe (?:close[- ]?signal )?detector\b",
    r"\bclosing[- ]?claim\b",
    r"\bno longer auto[- ]?close",
]

_CODE_FENCE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE = re.compile(r"`[^`]*`")
_BLOCKQUOTE = re.compile(r"(?m)^\s{0,3}>.*$")
_DQUOTE = re.compile(r"\"[^\"]*\"")
_SMART_DQUOTE = re.compile(r"[“”][^“”]*[“”]")


def strip_mentions(text: str) -> str:
    """Remove spans the assistant is QUOTING/SHOWING rather than asserting:
    fenced code, inline backticks, blockquote lines, and double-quote spans
    (straight + smart). Order matters — fences before inline code."""
    if not text:
        return ""
    text = _CODE_FENCE.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    text = _BLOCKQUOTE.sub(" ", text)
    text = _DQUOTE.sub(" ", text)
    text = _SMART_DQUOTE.sub(" ", text)
    return text


def is_closing_claim(text: str) -> bool:
    """True iff `text` is a genuine first-person session-close claim — not a
    mention, quote, or discussion of one."""
    if not text:
        return False
    # Discussion/definitional context is checked on the ORIGINAL text so an
    # explicit "discussing the close cascade" suppresses even unstripped prose.
    for pat in DISCUSSION_MARKERS:
        if re.search(pat, text, re.IGNORECASE):
            return False
    # Close patterns are checked on the MENTION-stripped text so a sign-off
    # quoted as an example does not count.
    stripped = strip_mentions(text)
    for pat in CLOSING_PATTERNS:
        if re.search(pat, stripped, re.IGNORECASE | re.MULTILINE):
            return True
    return False
