#!/usr/bin/env python3
"""
extractors/whatsapp_chat.py — structured metadata for WhatsApp chat exports
written by the bridge wrapper at ~/.local/bin/whatsapp-export-vault.sh.

Type: `whatsapp-chat` (hyphenated in source; dispatcher normalizes to
`whatsapp_chat` for module lookup).

Each chat file shape:
    ---
    type: whatsapp-chat
    contact: "<display name>"
    phone: "<E.164>"
    jid: "<wa-jid>"                   # optional, group chats end with @g.us
    chat_type: "direct" | "group"     # missing on older exports
    message_count: <int>
    first_message: <YYYY-MM-DD>
    last_message: <YYYY-MM-DD>
    last_sync: <YYYY-MM-DD>
    ---
    # WhatsApp: <contact>

    ## YYYY-MM-DD
    **HH:MM AM/PM** <Sender>: <text>       # outgoing sender == "You" (or its localized label)
    **HH:MM AM/PM** <Sender>: [Voice note] <transcript>
    **HH:MM AM/PM** <Sender>: [Reaction: 😂]
    **HH:MM AM/PM** <Sender>: [Sticker]
    **HH:MM AM/PM** <Sender>: [Location]

Zero LLM. All fields are regex / count / passthrough / arithmetic.
"""
import datetime as _dt
import os
import re
import unicodedata

from _base import (
    count_words, iso_date_from, wikilinks_in,
    ExtractionResult,
)

AUTO_FIELDS = (
    "whatsapp_chat_kind",
    "whatsapp_contact_name",
    "whatsapp_message_count",
    "whatsapp_my_msg_count",
    "whatsapp_their_msg_count",
    "whatsapp_first_iso",
    "whatsapp_last_iso",
    "whatsapp_days_active",
    "whatsapp_dormancy_days",
    "whatsapp_density_per_day",
    "whatsapp_voice_note_count",
    "whatsapp_voice_note_transcribed_count",
    "whatsapp_reaction_count",
    "whatsapp_concepts_extracted",
    "whatsapp_people_mentioned",
    "whatsapp_decision_signal",
    "word_count",
)

# **HH:MM AM/PM** Sender: rest
MSG_LINE_RE = re.compile(
    r"^\*\*(\d{1,2}:\d{2}(?:\s*[AP]M)?)\*\*\s+([^:]+):\s*(.*)$",
    re.MULTILINE,
)

# Outgoing-sender label. The exporter writes it in the phone's own language
# ("Yo" on a Spanish handset, "Eu" on Portuguese), so a hardcoded English
# "You" counts every message the user SENT as one they received: `msgs_mine`
# lands on 0 and the reciprocity ratio inverts. That is a wrong field, not a
# missing one — nothing downstream can tell it apart from a genuinely
# one-sided chat. Set WHATSAPP_SELF_LABEL to pin a label the list misses.
_SELF_ENV = os.environ.get("WHATSAPP_SELF_LABEL")
SELF_LABELS = (
    {_SELF_ENV.strip()} if _SELF_ENV
    else {"You", "Yo", "Eu", "Moi", "Ich", "Io"}
)
# ## YYYY-MM-DD day header
DAY_HEADER_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)
# Voice notes (with or without transcript)
VOICE_NOTE_RE = re.compile(r"\[Voice note\]")
# Voice note followed by transcript on the SAME line. \s matches \n, so the
# original `\s+\S` over-counted by matching the next message line. Pin to
# horizontal whitespace + non-newline content.
VOICE_NOTE_TRANSCRIBED_RE = re.compile(r"\[Voice note\][ \t]+[^\s\n]")
# Reaction marker
REACTION_RE = re.compile(r"\[Reaction:")
# Decision-stub trigger words (mirrors the bridge auto-stub rule in CLAUDE.md).
#
# This list was English-only, which makes the field close to useless on a
# non-English vault. Measured on a 736-chat Spanish corpus: the English list
# fired on 6 chats (1%); with the Spanish terms below it fires on 100 (14%) —
# a signal that discriminates rather than one that is almost always False.
#
# Set WHATSAPP_DECISION_TERMS (comma-separated) to replace this list wholesale
# for any other language.
_DEFAULT_DECISION_TERMS = (
    # English
    "exception", "incident", "pricing", "escalation", "outage", "edge case",
    "refund", "invoice", "contract", "discount", "quote",
    # Spanish
    "excepci\u00f3n", "excepcion", "incidente", "incidencia", "precio",
    "tarifa", "cotizaci\u00f3n", "cotizacion", "escalamiento",
    "ca\u00edda", "caida", "reembolso", "devoluci\u00f3n", "devolucion",
    "contrato", "factura", "facturaci\u00f3n", "facturacion", "descuento",
    "presupuesto", "anticipo", "mora",
)
_env_terms = os.environ.get("WHATSAPP_DECISION_TERMS", "").strip()
_DECISION_TERMS = tuple(
    t.strip() for t in _env_terms.split(",") if t.strip()
) or _DEFAULT_DECISION_TERMS
DECISION_KEYWORDS = re.compile(
    r"(?i)\b(?:"
    + "|".join(re.escape(t).replace(r"\ ", r"\s*") for t in _DECISION_TERMS)
    + r")\b"
)

# Private-use codepoints leak into exported contact names and group subjects
# (Apple ships emoji in this range). They render as nothing, but they are NOT
# whitespace, so .strip() leaves them and every wikilink or Dataview equality
# test against the visible name silently fails to match.
_PUA_RE = re.compile(
    "[\uE000-\uF8FF\U000F0000-\U000FFFFD\U00100000-\U0010FFFD]"
)

# Group filename markers, for exports carrying neither `chat_type` nor `jid`.
# Localized: the bridge names the file after the group subject.
_GROUP_NAME_MARKERS = ("(group)", "(grupo)", "(gruppo)", "(gruppe)", "(groupe)")


def _clean_name(raw):
    """Visible, queryable form of a contact or group name. None if empty."""
    s = _PUA_RE.sub("", str(raw or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _chat_kind(fm, filepath=""):
    """direct | group, most explicit signal first.

    `chat_type` and `jid` are both absent on exports written by older versions
    of the bridge wrapper, and those two were the only signals consulted. On
    such an export EVERY group is reported as `direct` — measured at 113 of 113
    groups in a 736-chat vault, which silently doubles as a wrong my/their
    ratio for each one.

    Two fallbacks close that gap, both drawn from what those exports DO carry:
    the group subject in the filename, and a group JID in `phone`, which
    carries a `-<discriminator>` suffix that an E.164 number never has.

    Deliberately NOT inferred from JID length: exports name direct chats after
    15-digit LID identifiers, so a length heuristic mislabels them.
    """
    raw = (fm.get("chat_type") or "").strip().lower()
    if raw in ("direct", "group"):
        return raw

    jid = " ".join(str(fm.get(k) or "") for k in ("jid", "chat_jid"))
    if "@g.us" in jid:
        return "group"

    name = os.path.basename(str(filepath or "")).lower()
    if any(m in name for m in _GROUP_NAME_MARKERS):
        return "group"

    if re.match(r"^\+?\d+-\d+$", str(fm.get("phone") or "").strip()):
        return "group"

    return "direct"


def _fold(value):
    """Accent- and case-insensitive comparison key."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", str(value))
        if not unicodedata.combining(c)
    ).casefold().strip()


def _match_crm_contact(contact, crm_names):
    """Resolve a phone-book label to a CRM note name, or None.

    Phone-book labels are not note filenames. Real misses this closes, all of
    them substantive chats that linked to nothing:
      "<Name> 30X"  -> note "<Name>"          (org suffix on the contact)
      "<Name> ABI"  -> note "<Name>"
      "Angela ..."  -> note "\u00c1ngela ..."      (accents dropped in the phone book)

    Fold accents, then allow the note name to be a leading whole-token prefix
    of the label, which absorbs trailing org tags. The prefix must be at least
    two tokens, so a bare first name never resolves to a full-name note.
    """
    if not contact:
        return None
    table = {_fold(n): n for n in crm_names}
    key = _fold(contact)
    if key in table:
        return table[key]

    best = None
    for folded, real in table.items():
        if len(folded.split()) < 2:
            continue
        if key.startswith(folded + " "):
            if best is None or len(folded) > len(_fold(best)):
                best = real
    return best


def _people_mentioned(body, contact_crm, crm_names, cap=20):
    """CRM names in this chat, the chat's own counterpart first.

    `match_people()` from _base only matches [[wikilinks]]. Chat bodies are
    verbatim message logs and never contain any, so that helper returns an
    empty list for every chat file — measured at 0 of 736. Plain-text matching
    is the only thing that can work here.

    Only multi-token CRM names are searched in the body: a single-token note
    ("Elizabeth") would match any casual use of the word.
    """
    found = []
    if contact_crm:
        found.append(contact_crm)
    for name in crm_names:
        if name in found or " " not in name:
            continue
        if re.search(r"\b" + re.escape(name) + r"\b", body, re.I):
            found.append(name)
        if len(found) >= cap:
            break
    return found[:cap]


def _msg_counts(body):
    """(total, mine, theirs) from **HH:MM** Sender: lines."""
    total = mine = theirs = 0
    for m in MSG_LINE_RE.finditer(body):
        total += 1
        sender = m.group(2).strip()
        if sender in SELF_LABELS:
            mine += 1
        else:
            theirs += 1
    return total, mine, theirs


def _days_active(body):
    return len(set(DAY_HEADER_RE.findall(body)))


def _dormancy(last_iso):
    if not last_iso:
        return None
    try:
        last = _dt.date.fromisoformat(last_iso)
        delta = (_dt.date.today() - last).days
        return delta if delta >= 0 else 0
    except Exception:
        return None


def _density(msg_count, days_active):
    if not msg_count or not days_active:
        return None
    return round(msg_count / days_active, 2)


def _safe_int(v, default=0):
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return default


def extract(filepath, body, fm, context):
    crm_names = context.get("crm_names", set())
    contact = _clean_name(fm.get("contact"))

    first_iso = iso_date_from(fm.get("first_message"))
    last_iso = iso_date_from(fm.get("last_message"))

    scan_total, mine, theirs = _msg_counts(body)
    fm_msg_count = _safe_int(fm.get("message_count"), default=scan_total)
    # Bridge frontmatter is authoritative when present; otherwise trust the scan.
    message_count = fm_msg_count if fm_msg_count > 0 else scan_total

    days = _days_active(body)
    voice_total = len(VOICE_NOTE_RE.findall(body))
    voice_transcribed = len(VOICE_NOTE_TRANSCRIBED_RE.findall(body))
    reactions = len(REACTION_RE.findall(body))

    fields = {
        "whatsapp_chat_kind": _chat_kind(fm, filepath),
        "whatsapp_contact_name": contact,
        "whatsapp_message_count": message_count,
        "whatsapp_my_msg_count": mine,
        "whatsapp_their_msg_count": theirs,
        "whatsapp_first_iso": first_iso,
        "whatsapp_last_iso": last_iso,
        "whatsapp_days_active": days,
        "whatsapp_dormancy_days": _dormancy(last_iso),
        "whatsapp_density_per_day": _density(message_count, days),
        "whatsapp_voice_note_count": voice_total,
        "whatsapp_voice_note_transcribed_count": voice_transcribed,
        "whatsapp_reaction_count": reactions,
        "whatsapp_concepts_extracted": wikilinks_in(body)[:25],
        "whatsapp_people_mentioned": _people_mentioned(
            body, _match_crm_contact(contact, crm_names), crm_names, cap=20
        ),
        "whatsapp_decision_signal": bool(DECISION_KEYWORDS.search(body)),
        "word_count": count_words(body),
    }
    return ExtractionResult(fields, AUTO_FIELDS, auto_fields=AUTO_FIELDS)
