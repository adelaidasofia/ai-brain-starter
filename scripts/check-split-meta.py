#!/usr/bin/env python3
"""check-split-meta.py - detect a vault whose session/traffic data leaked into a
plain "Meta/" folder instead of the human "⚙️ Meta/" (the ai-brain-starter#176
bug). The resolver fix prevents NEW leaks; this finds vaults already split by the
old naive glob so diagnose.sh can surface the one-time reconcile.

Verdicts (--porcelain):
  OK_NO_META        no Meta-suffixed folder
  OK_SINGLE_META    exactly one Meta-suffixed folder
  OK_PARTITIONED    plain "Meta/" holds only machine memory; human "⚙️ Meta/" is
                    separate (the correct, healthy layout)
  SPLIT_META:<n>    plain "Meta/" holds <n> human session/traffic item(s) that
                    belong in "⚙️ Meta/" -- the leak happened
"""
from __future__ import annotations

import sys
from pathlib import Path

# Files/dirs the five buggy scripts wrote (session-end-hook, vault-daily-
# maintenance, traffic-digest/snapshot). Their presence in a PLAIN "Meta/" while
# a decorated "⚙️ Meta/" also exists means the human/machine split was breached.
HUMAN_LEAK_MARKERS = (
    "Sessions",
    "Session Log.md",
    "Last Session.md",
    "Session Captures.md",
    "Decision Log.md",
    "Repo Traffic Dashboard.md",
    "logs",
)


def verdict(vault_root: Path) -> str:
    if not vault_root.is_dir():
        return "OK_NO_META"
    metas = sorted(
        c for c in vault_root.iterdir()
        if c.is_dir() and c.name.endswith("Meta")
    )
    if not metas:
        return "OK_NO_META"
    if len(metas) == 1:
        return "OK_SINGLE_META"
    # Two+ Meta dirs. A bare "Meta" is the machine folder; a decorated one (emoji
    # prefix) is the human folder. Contamination = human markers inside bare Meta.
    plain = next((m for m in metas if m.name == "Meta"), None)
    decorated = [m for m in metas if m.name != "Meta"]
    if plain is None or not decorated:
        return "OK_PARTITIONED"
    leaked = [m for m in HUMAN_LEAK_MARKERS if (plain / m).exists()]
    if leaked:
        return "SPLIT_META:%d" % len(leaked)
    return "OK_PARTITIONED"


def main(argv: list[str]) -> int:
    porcelain = "--porcelain" in argv
    args = [a for a in argv if a != "--porcelain"]
    vault = Path(args[0]) if args else Path.cwd()
    result = verdict(vault)
    print(result if porcelain else "split-meta check: %s" % result)
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main(sys.argv[1:]))
