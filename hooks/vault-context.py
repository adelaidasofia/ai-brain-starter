#!/usr/bin/env python3
"""
UserPromptSubmit hook: detect strategic topics, read key vault files, inject as additionalContext.
Fires before Claude responds — no instructions needed, context is just there.

Vault resolution (MYC-3529, same defect as #375/#404): this hook used to bind
`VAULT = os.environ.get("VAULT_ROOT", str(Path.home() / "vault"))` at import.
Both branches were wrong. UNSET, it read `~/vault/⚙️ Meta/Current Priorities.md`,
which does not exist on any vault not literally named "vault" — every read
returned None, `parts` stayed length 1, and the hook exited 0 injecting nothing,
silently, forever. SET, it named ONE vault, so a session working in a SECOND
vault got the FIRST vault's priorities injected as if they were its own — worse
than nothing, because the model cannot tell injected context is from the wrong
place.

Now: resolve per invocation from the session's cwd (vault_root_for — detection
from the target first, $VAULT_ROOT as fallback). No vault identified → inject
nothing, which is the honest answer.

SIGNALS (2026-08-15): the trigger list used to be hardcoded English, and half of
it was one person's vocabulary — `accenture`, `substack`, `nyc`, `high-rise`,
`after the shock`, plus a TOPIC_MAP pointing at `🚀 team-vault/…` paths that
exist in exactly one vault. Two separate defects wearing one coat:

  * A vault kept in Spanish (or any non-English language) never matched a single
    signal. The hook resolved the vault correctly, read nothing, and exited 0 —
    the same silent no-op the vault-resolution fix above was written to kill,
    reached by a different road. Measured on a real Spanish vault: "qué
    prioridades tengo esta semana y cómo va la estrategia" → zero matches; the
    same sentence in English → full injection.
  * Personal terms shipped as defaults are dead weight in every other install,
    and worse, they teach the reader that this file is not theirs to hold.

Signals now come from `templates/vault-context/<lang>.json` (same pack shape
detect-closing-signal.py uses), merged across VAULT_CONTEXT_LANGS (default
`en,es`), plus an optional per-user override at
`~/.claude/.vault-context-signals.json` for personal vocabulary and topic→file
maps — which is where anything naming YOUR company, city, or vault paths goes.
BUILTIN_STRATEGIC_SIGNALS remains as a floor so a partial install (hook copied
without templates/) still fires on the obvious English cases.
"""
# MYC-3529: REQUIRED, not cosmetic. This module annotates with PEP-604
# `X | None`, which is evaluated at def-time and is a TypeError on Python
# 3.9 -- the floor version scripts/ci.sh's gate actually runs. py_compile
# does NOT catch it (the annotation compiles fine and only blows up when
# the def executes), so the import crash is invisible to the lint gates and
# shows up only as a hook that silently does nothing.
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _lib.vault_root import vault_root_for  # noqa: E402
except Exception:  # fail-open: a context-injector must never break a prompt
    def vault_root_for(target: Path):  # type: ignore
        return None

MAX_CHARS = 4000  # per file truncation limit
DEFAULT_LANGS = "en,es"  # override with VAULT_CONTEXT_LANGS

# Always load these for strategic questions
CORE_FILES = [
    "⚙️ Meta/Current Priorities.md",
    "⚙️ Meta/Open Loops.md",
]

# Floor, used ONLY when no language pack loads (hook copied without templates/).
# Deliberately English and deliberately small: a fallback that pretends to be a
# full signal set is how the hardcoded list survived this long.
BUILTIN_STRATEGIC_SIGNALS = [
    r"\bstrateg", r"\bdecision\b", r"\bprioritiz", r"\bpriorities\b",
    r"\bplan\b", r"\bfocus\b", r"\bnext step", r"\bopen loop",
    r"\bwhat should (i|we)\b", r"\bpending\b",
]

# Where a user's OWN vocabulary and topic→file map live. Personal by nature:
# company names, city names, vault-specific paths. Never shipped in a pack.
USER_SIGNALS_FILE = os.environ.get(
    "VAULT_CONTEXT_SIGNALS_FILE",
    str(Path.home() / ".claude" / ".vault-context-signals.json"),
)


def find_repo_root() -> Path:
    """Locate the repo root that owns templates/vault-context.

    UNLIKE detect-closing-signal.py, which runs from inside the clone, THIS
    hook is DEPLOYED — install-hooks-user-level.py copies it to
    ~/.claude/hooks/, where walking up finds ~/.claude and no templates/ at
    all. Without the canonical-clone fallback below, every deployed copy would
    silently fall back to the English floor and the Spanish packs would ship to
    everyone and load for no one: the same defect this change exists to fix,
    one directory to the left. The clone path is the one other deployed hooks
    already key on (inject-meeting-workflow-on-trigger.py).
    """
    here = Path(__file__).resolve().parent
    candidate = here.parent  # hooks/ is 1 level deep inside the clone
    if (candidate / "templates" / "vault-context").is_dir():
        return candidate
    for ancestor in here.parents:
        if (ancestor / "templates" / "vault-context").is_dir():
            return ancestor
    clone = Path.home() / ".claude" / "skills" / "ai-brain-starter"
    if (clone / "templates" / "vault-context").is_dir():
        return clone
    return candidate


def _read_json(path: Path) -> dict:
    """Fail-open: an unreadable or malformed pack contributes nothing."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def load_signals(langs: list[str], user_file: str = USER_SIGNALS_FILE):
    """Merge language packs + the per-user override into (signals, topic_map).

    Returns the BUILTIN floor only when zero packs AND no user signals loaded,
    so a hook deployed without templates/ still fires on the obvious cases
    instead of going silent — the failure mode this whole file is about.
    """
    pack_dir = find_repo_root() / "templates" / "vault-context"
    signals: list[str] = []
    topic_map: list[tuple[list[str], list[str]]] = []

    for lang in langs:
        lang = lang.strip()
        if not lang:
            continue
        path = pack_dir / f"{lang}.json"
        if path.is_file():
            signals.extend(_read_json(path).get("strategic_signals", []))

    user = _read_json(Path(user_file)) if user_file else {}
    signals.extend(user.get("strategic_signals", []))
    for entry in user.get("topic_map", []):
        if isinstance(entry, dict) and entry.get("signals") and entry.get("files"):
            topic_map.append((list(entry["signals"]), list(entry["files"])))

    if not signals:
        signals = list(BUILTIN_STRATEGIC_SIGNALS)

    # Drop patterns that do not compile rather than letting one bad line in a
    # hand-edited override take the whole hook down.
    valid = []
    for pat in signals:
        try:
            re.compile(pat)
            valid.append(pat)
        except re.error:
            continue
    return valid, topic_map


def read_file(vault: Path, rel_path: str) -> str | None:
    full = os.path.join(str(vault), rel_path)
    try:
        with open(full, "r", encoding="utf-8") as f:
            content = f.read()
        if len(content) > MAX_CHARS:
            content = content[:MAX_CHARS] + "\n...[truncated — read full file if needed]"
        return content
    except Exception:
        return None


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    prompt = payload.get("prompt", "") or ""
    p = prompt.lower().strip()

    langs = os.environ.get("VAULT_CONTEXT_LANGS", DEFAULT_LANGS).split(",")
    strategic_signals, topic_map = load_signals(langs)

    if not any(re.search(sig, p) for sig in strategic_signals):
        sys.exit(0)

    # Resolve the vault from THIS session's cwd before doing any I/O. None means
    # no vault is identifiable here (a plain code repo, no $VAULT_ROOT set) —
    # inject nothing rather than guessing at ~/vault and reading a phantom tree.
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_CWD") or ""
    vault = vault_root_for(Path(cwd) if cwd else Path.cwd())
    if vault is None:
        sys.exit(0)

    files_to_load = list(CORE_FILES)
    for signals, extra_files in topic_map:
        if any(re.search(sig, p) for sig in signals):
            files_to_load.extend(extra_files)

    # Deduplicate while preserving order
    seen = set()
    unique_files = []
    for f in files_to_load:
        if f not in seen:
            seen.add(f)
            unique_files.append(f)

    parts = [f"[vault-context] Vault files auto-loaded for this query (vault: {vault}):\n"]
    for rel_path in unique_files:
        content = read_file(vault, rel_path)
        if content:
            parts.append(f"\n=== {rel_path} ===\n{content}")

    if len(parts) <= 1:
        sys.exit(0)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(parts),
        }
    }
    print(json.dumps(out))
    sys.exit(0)


if __name__ == "__main__":
    # Windows cp1252-console safety (ai-brain-starter#313; hooks/ sweep #314).
    # This hook now carries non-ASCII in its own source AND injects vault file
    # contents verbatim, so a Spanish/emoji-path vault printing through a
    # cp1252 console would raise UnicodeEncodeError and take the prompt with
    # it. Idempotent; a no-op on an already-UTF-8 console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
