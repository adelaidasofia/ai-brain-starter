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

# Always load these for strategic questions
CORE_FILES = [
    "⚙️ Meta/Current Priorities.md",
    "⚙️ Meta/Open Loops.md",
]

# Load for raise/investor/pitch topics
RAISE_FILES = [
    "🚀 team-vault/💰 Raise/Raise Dashboard.md",
    "🚀 team-vault/💰 Raise/Raise Sprint - Apr 2026.md",
]

# Load for the user's primary org product/strategy topics
ONDE_STRATEGY_FILES = [
    "🚀 team-vault/📋 Strategy/the user's primary org.md",
    "🚀 team-vault/📋 Strategy/Strategy Index.md",
]

# Signals → which extra files to load
TOPIC_MAP = [
    (
        [r"\braise\b", r"\binvestor", r"\bpitch\b", r"\bseed\b", r"\bangel\b",
         r"\bvaluation\b", r"\bterm sheet\b", r"\bnyc trip\b", r"\bapr(il)? 24\b",
         r"\bapr(il)? 30\b", r"\bdata room\b"],
        RAISE_FILES,
    ),
    (
        [r"\bonde\b", r"\bproduct\b", r"\baccenture\b", r"\bclient\b",
         r"\bvenue tool\b", r"\bcorporate structure\b", r"\bsales\b",
         r"\bgo.to.market\b", r"\bgtm\b"],
        ONDE_STRATEGY_FILES,
    ),
]

# Broad strategic signals — if any match, inject CORE_FILES
STRATEGIC_SIGNALS = [
    r"\bstrateg", r"\bonde\b", r"\braise\b", r"\binvestor", r"\bpitch\b",
    r"\bdecision\b", r"\bprioritiz", r"\bpriorities\b", r"\bnyc\b",
    r"\bplan\b", r"\bfocus\b", r"\bnext step", r"\bopen loop",
    r"\bwhat should (i|we)\b", r"\bhow (should|do) (i|we)\b",
    r"\bwhat.s (my|our|the) (plan|status|situation)\b",
    r"\bwhere (am|are) (i|we)\b", r"\bpending\b", r"\bwhat (am|are) (i|we) (doing|working)\b",
    r"\brevenue\b", r"\bclient\b", r"\bproduct\b", r"\baccenture\b",
    r"\bseed\b", r"\bangel\b", r"\bsales\b", r"\bconsulting\b",
    r"\bwriting\b", r"\bsubstack\b", r"\bhigh.rise\b", r"\bafter the shock\b",
]


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

    if not any(re.search(sig, p) for sig in STRATEGIC_SIGNALS):
        sys.exit(0)

    # Resolve the vault from THIS session's cwd before doing any I/O. None means
    # no vault is identifiable here (a plain code repo, no $VAULT_ROOT set) —
    # inject nothing rather than guessing at ~/vault and reading a phantom tree.
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_CWD") or ""
    vault = vault_root_for(Path(cwd) if cwd else Path.cwd())
    if vault is None:
        sys.exit(0)

    files_to_load = list(CORE_FILES)
    for signals, extra_files in TOPIC_MAP:
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
    main()
