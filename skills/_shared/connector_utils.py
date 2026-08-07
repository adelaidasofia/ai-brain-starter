#!/usr/bin/env python3
"""
connector_utils.py: shared helpers for ingest-* and synth-* skills.

Six core helpers (per Build Standards #5):
  - write_typed_memory(...) -> str
  - sha8(text) -> str
  - normalize_for_vault(items, source_type, scope_id) -> list[str]
  - entity_ids_for(source_type, ids) -> dict
  - read_existing_or_none(path) -> dict | None
  - write_external_input(...) -> str

Secondary helpers extracted from duplication across the 6 skills:
  - yaml_escape, yaml_int_array, yaml_str_array
  - parse_iso, to_local_str, to_local_date, to_local_sortkey
  - excerpt, fence_text, truncate_body
  - slugify, slug_repo
  - split_frontmatter, render_frontmatter
  - now_iso, today_iso, date_range_strs

Stdlib + PyYAML only.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError:
    yaml = None  # synth-* skills require yaml; ingest-* skills do not.


_SLUGIFY_RE = re.compile(r"[^a-z0-9]+")
_SLUG_BAD_UNICODE = re.compile(r"[^\w\-]+", flags=re.UNICODE)


# ---------------------------------------------------------------------------
# Core helpers (the six required by the spec)
# ---------------------------------------------------------------------------

def sha8(text: str) -> str:
    """8-char SHA-1 hex digest of a UTF-8 string. Deterministic, stable across
    runs. Used as the idempotency key for synth-* outputs (one input = one
    output filename).
    """
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def read_existing_or_none(path: Path | str) -> dict[str, Any] | None:
    """Read an existing markdown file's frontmatter if the file exists. Return
    None if the file does not exist OR the file has no frontmatter OR the YAML
    fails to parse. Never raises on a missing file.

    Used by synth-* skills to detect hand-edited outputs (`hand_edited: true`)
    that must not be overwritten without --force.
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, _ = split_frontmatter(text)
    return meta or None


def write_typed_memory(
    vault_root: Path | str,
    memory_type: str,
    content: str,
    frontmatter: dict[str, Any],
    idempotency_key: str,
) -> str:
    """Write a typed-memory file under <vault_root>/Meta/<TypeFolder>/<sha8>.md.

    `memory_type` must be one of: workflow, decision, exception. The folder is
    Workflows/Decisions/Exceptions respectively. `idempotency_key` is hashed
    with sha8 to produce the filename, so re-running with the same key
    overwrites the same file.

    `frontmatter` is rendered as YAML. `content` is appended after `---\\n\\n`.

    Returns the absolute path of the written file as a string.
    """
    if memory_type not in ("workflow", "decision", "exception"):
        raise ValueError(
            f"memory_type must be 'workflow', 'decision', or 'exception', got: {memory_type!r}"
        )
    folder = {
        "workflow": "Workflows",
        "decision": "Decisions",
        "exception": "Exceptions",
    }[memory_type]

    file_sha = sha8(idempotency_key)
    out_dir = Path(vault_root) / "Meta" / folder
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{file_sha}.md"

    rendered = render_frontmatter(frontmatter) + content
    out_path.write_text(rendered, encoding="utf-8")
    return str(out_path)


def normalize_for_vault(
    items: list[dict[str, Any]],
    source_type: str,
    scope_id: str,
) -> list[str]:
    """Generic normalizer that returns one markdown block per item. Skills
    with rich, source-specific rendering (PR bodies, Notion props, Linear
    state transitions) keep their own normalizers for fidelity. This helper
    is for callers that want a uniform fallback shape.

    Each block is a heading + meta lines + body excerpt, no trailing blank.
    `source_type` and `scope_id` are baked into the heading line so the
    output reads correctly when concatenated.
    """
    blocks: list[str] = []
    for item in items or []:
        title = (
            item.get("title")
            or item.get("subject")
            or item.get("identifier")
            or item.get("id")
            or "(untitled)"
        )
        when = (
            item.get("updated_at")
            or item.get("internal_date")
            or item.get("merged_at")
            or item.get("created_at")
            or item.get("last_edited_time")
            or ""
        )
        url = item.get("url") or ""
        body = item.get("body") or item.get("body_text") or item.get("description") or ""

        lines = [f"## {title}", ""]
        lines.append(f"- **Source:** {source_type} / {scope_id}")
        if when:
            lines.append(f"- **When:** {to_local_str(when) or when}")
        if url:
            lines.append(f"- **URL:** {url}")
        lines.append("")
        lines.append(excerpt(body))
        blocks.append("\n".join(lines))
    return blocks


def entity_ids_for(source_type: str, ids: list[Any]) -> dict[str, list[Any] | str]:
    """Build the `entity_ids` dict used in external-input frontmatter.

    Returns a one-key dict whose key is the source_type and whose value is
    the list of ids (or [] if empty). For sources where the id space has a
    sub-type (github_pr vs github_issue), the caller passes the typed key
    explicitly via the helper's `source_type` argument (e.g. "github_pr").

    The shape is flow-style YAML compatible (list of strings or ints).
    """
    cleaned = [i for i in (ids or []) if i is not None and i != ""]
    return {source_type: cleaned}


def write_external_input(
    vault_root: Path | str,
    source: str,
    scope: str,
    date: str,
    items: list[dict[str, Any]] | None,
    entity_ids_extra: dict[str, Any] | None = None,
    body: str | None = None,
    frontmatter_extra: dict[str, Any] | None = None,
) -> str:
    """Write a vault file at:
        <vault_root>/External Inputs/<Source>/<scope>/<YYYY-MM-DD>.md

    `source` is the directory name (GitHub, Notion, Linear, Gmail). `scope`
    is the per-source slug (owner-repo, label slug, team key, root id slug).
    `date` is YYYY-MM-DD. `items` is the raw list (used for item_count and
    a generic fallback body if `body` is not supplied). `entity_ids_extra`
    is folded into the entity_ids block of the frontmatter. `body` overrides
    the generic body so a skill can ship a richer rendering.

    Returns the absolute path as a string.

    This is the generic fallback. Skills that ship today use their own
    write_vault_file because their frontmatter shape is source-specific
    (date_range, root_kind, scope_kind, etc.). This helper exists for
    future skills that want a one-call contract.
    """
    src_dir = Path(vault_root) / "External Inputs" / source / scope
    src_dir.mkdir(parents=True, exist_ok=True)
    out_path = src_dir / f"{date}.md"

    items = items or []
    fm: dict[str, Any] = {
        "type": "external-input",
        "source": source.lower(),
        "scope": scope,
        "date": date,
        "item_count": len(items),
        "ingested_at": now_iso(),
        "entity_ids": entity_ids_extra or {},
    }
    if frontmatter_extra:
        fm.update(frontmatter_extra)

    rendered_fm = render_frontmatter(fm)
    rendered_body = body if body is not None else "\n\n".join(
        normalize_for_vault(items, source.lower(), scope)
    )
    if not rendered_body.strip():
        rendered_body = "_No items in scope._\n"
    if not rendered_body.endswith("\n"):
        rendered_body += "\n"
    out_path.write_text(rendered_fm + rendered_body, encoding="utf-8")
    return str(out_path)


# ---------------------------------------------------------------------------
# YAML helpers (used by ingest-* skills; no PyYAML dep)
# ---------------------------------------------------------------------------

def yaml_escape(value: Any) -> str:
    """Escape a scalar for safe YAML inclusion. Returns the string 'null' for
    None so the caller can render `field: null` directly.
    """
    if value is None:
        return "null"
    s = str(value)
    if any(c in s for c in [':', '#', '\n', '"', "'", '[', ']', '{', '}']):
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return s


def yaml_int_array(items: Iterable[int]) -> str:
    """Render an iterable of ints as a YAML flow-style array."""
    items = list(items or [])
    if not items:
        return "[]"
    return "[" + ", ".join(str(i) for i in items) + "]"


def yaml_str_array(items: Iterable[Any]) -> str:
    """Render an iterable of values as a YAML flow-style array of strings."""
    items = list(items or [])
    if not items:
        return "[]"
    return "[" + ", ".join(yaml_escape(str(i)) for i in items) + "]"


def render_frontmatter(meta: dict[str, Any]) -> str:
    """Render a frontmatter dict using PyYAML if available, hand-crafted
    otherwise. Always returns a string ending with `---\\n\\n` so the caller
    can append the body directly.

    Used by synth-* skills (which already require PyYAML for split_frontmatter).
    """
    if yaml is None:
        # Hand-craft a minimal renderer for the no-yaml path. ingest-* skills
        # never call this (they build frontmatter as a string directly), so
        # this branch is only a safety net.
        lines = ["---"]
        for k, v in meta.items():
            if isinstance(v, list):
                lines.append(f"{k}: {yaml_str_array(v)}")
            elif isinstance(v, dict):
                lines.append(f"{k}:")
                for sk, sv in v.items():
                    if isinstance(sv, list):
                        lines.append(f"  {sk}: {yaml_str_array(sv)}")
                    else:
                        lines.append(f"  {sk}: {yaml_escape(sv)}")
            else:
                lines.append(f"{k}: {yaml_escape(v)}")
        lines.append("---")
        lines.append("")
        return "\n".join(lines) + "\n"
    body = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{body}\n---\n\n"


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file's YAML frontmatter from its body. Returns
    ({}, text) when there is no frontmatter or when the YAML is malformed.
    Requires PyYAML. Used by synth-* skills only.
    """
    if yaml is None:
        return {}, text
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2]


# ---------------------------------------------------------------------------
# ISO 8601 timestamp helpers
# ---------------------------------------------------------------------------

def parse_iso(value: str) -> datetime | None:
    """Parse an ISO 8601 timestamp. Accepts trailing Z. Returns None on any
    parse failure (caller decides whether to surface as raw string).
    """
    if not value:
        return None
    s = value.rstrip("Z")
    # Notion sometimes returns 2026-04-29T12:34:56.000Z; strip the millis.
    if "." in s:
        head, _, tail = s.partition(".")
        # Keep only digits in tail until a non-digit; rest is timezone (if any).
        i = 0
        while i < len(tail) and tail[i].isdigit():
            i += 1
        s = head + tail[i:]
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) if value.endswith("Z") else datetime.fromisoformat(s)
    except ValueError:
        try:
            # GitHub style: assume UTC if naive.
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def to_local_str(value: str) -> str:
    """ISO 8601 in -> 'YYYY-MM-DD HH:MM' in local time. Falls back to the
    raw string on parse failure.
    """
    dt = parse_iso(value)
    if not dt:
        return value or ""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def to_local_date(value: str) -> str:
    """ISO 8601 in -> 'YYYY-MM-DD' in local time. Falls back to the first
    10 chars of the raw string on parse failure (matches the GitHub format
    that already includes a date prefix).
    """
    dt = parse_iso(value)
    if not dt:
        return (value or "")[:10]
    return dt.astimezone().strftime("%Y-%m-%d")


def to_local_sortkey(value: str) -> datetime:
    """Returns a datetime suitable for sort(key=...). Missing/unparseable
    values sort to datetime.min so they float to the top of an ascending
    sort and the bottom of a descending sort.
    """
    return parse_iso(value) or datetime.min.replace(tzinfo=timezone.utc)


def now_iso() -> str:
    """Local-time ISO 8601 with seconds precision (no millis)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_iso() -> str:
    """Local-time YYYY-MM-DD."""
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def date_range_strs(target_date: str, days: int) -> tuple[str, str]:
    """Compute (start_date, end_date) as YYYY-MM-DD given a target end date
    and a lookback window in days. days <= 1 returns (target, target).
    """
    end = target_date
    if days <= 1:
        return end, end
    start = (datetime.fromisoformat(target_date) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    return start, end


# ---------------------------------------------------------------------------
# Body / text helpers
# ---------------------------------------------------------------------------

def excerpt(text: str, limit: int = 800) -> str:
    """Truncate body text to a readable excerpt. Defends against unclosed
    fenced code blocks by replacing triple backticks with backtick-space.
    """
    if not text:
        return "_(no body)_"
    cleaned = text.replace("```", "` ` `").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + " ..."


def fence_text(text: str) -> str:
    """Same as excerpt() but never truncates. Use for quoted bodies that
    must stay verbatim but cannot break the outer markdown's fences.
    """
    if not text:
        return "_(empty)_"
    return text.replace("```", "` ` `")


def truncate_body(text: str, limit: int = 500, marker: str = "\n\n[...truncated]") -> str:
    """Truncate to `limit` chars and append a marker if anything was cut.
    Used by the ingest connectors to cap PII volume.
    """
    if not text:
        return "_(body unavailable)_"
    cleaned = text.replace("```", "` ` `")
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + marker


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

def slugify(value: str, fallback: str = "unknown") -> str:
    """Lowercase, replace non-alphanumeric runs with a hyphen, strip leading/
    trailing hyphens. Returns `fallback` if the result is empty.
    """
    s = _SLUGIFY_RE.sub("-", (value or "").lower()).strip("-")
    return s or fallback


def slugify_unicode(title: str, fallback: str = "unknown", max_len: int = 60) -> str:
    """Slugify that preserves unicode word characters. Used where source
    titles may contain accented characters that we want to keep.
    """
    if not title or not title.strip():
        return fallback
    slug = _SLUG_BAD_UNICODE.sub("-", title.strip().lower()).strip("-")
    return slug[:max_len] or fallback


def slug_repo(repo: str) -> str:
    """'owner/repo' -> 'owner-repo' for filesystem use."""
    return repo.replace("/", "-")


# ---------------------------------------------------------------------------
# Entity alias helpers (consume Meta/.entity-aliases.json built by
# scripts/entity-disambiguator.py). Synth-* skills use this to resolve a
# raw_mention to a canonical_entity in entity-mention frontmatter.
# ---------------------------------------------------------------------------

def find_meta_dir(vault_root: Path | str) -> Path | None:
    """Locate the Meta folder under the vault. Supports plain and
    emoji-prefixed names. Returns None when not found.
    """
    root = Path(vault_root)
    if not root.is_dir():
        return None
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name.endswith("Meta"):
            return child
    return None


def load_entity_aliases(vault_root: Path | str) -> dict[str, str]:
    """Read Meta/.entity-aliases.json and return {variant: canonical}.

    Returns an empty dict if the index is missing or unparseable. Operator
    overrides at Meta/entity-aliases-overrides.json are also folded in here
    so callers do not need to know the override file exists.
    """
    import json
    meta_dir = find_meta_dir(vault_root)
    if meta_dir is None:
        return {}
    out: dict[str, str] = {}
    idx_path = meta_dir / ".entity-aliases.json"
    if idx_path.is_file():
        try:
            data = json.loads(idx_path.read_text(encoding="utf-8"))
            aliases = data.get("aliases") if isinstance(data, dict) else None
            if isinstance(aliases, dict):
                for k, v in aliases.items():
                    if isinstance(k, str) and isinstance(v, str):
                        out[k] = v
        except (OSError, json.JSONDecodeError):
            pass
    override_path = meta_dir / "entity-aliases-overrides.json"
    if override_path.is_file():
        try:
            data = json.loads(override_path.read_text(encoding="utf-8"))
            aliases = data.get("aliases") if isinstance(data, dict) else None
            if isinstance(aliases, dict):
                for k, v in aliases.items():
                    if isinstance(k, str) and isinstance(v, str):
                        out[k] = v
        except (OSError, json.JSONDecodeError):
            pass
    return out


def canonicalize_entity(raw_mention: str, aliases: dict[str, str]) -> str:
    """Look up a raw mention in the alias index. Returns the canonical form
    if found, else the raw mention untouched. Case-insensitive fallback so
    minor capitalization drift still resolves.
    """
    if not raw_mention:
        return raw_mention
    if raw_mention in aliases:
        return aliases[raw_mention]
    folded = raw_mention.casefold()
    for variant, canonical in aliases.items():
        if variant.casefold() == folded:
            return canonical
    return raw_mention


def extract_entity_mentions(text: str) -> list[str]:
    """Pull capitalized noun phrases from a body for entity-mention scanning.
    Scoped to single capitalized tokens or two-word phrases, length >= 4.
    """
    import re
    pattern = re.compile(r"\b([A-Z][a-z0-9]+(?:[ \-]?[A-Z][a-z0-9]+){0,2})\b")
    seen: set[str] = set()
    out: list[str] = []
    for m in pattern.finditer(text or ""):
        candidate = m.group(1).strip()
        if len(candidate) < 4:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
    return out
