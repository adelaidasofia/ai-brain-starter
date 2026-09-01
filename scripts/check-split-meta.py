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
# exit-contract: ENFORCING

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


def self_test() -> int:
    """Negative controls: the SPLIT_META verdict must reach a non-zero exit.

    Before the exit change this file had no non-zero exit anywhere in it, so a
    detected split was separated from a clean vault only by a stdout token that
    a non-porcelain caller ignores. Each case builds a real vault layout on
    disk and drives main(), so a control cannot pass while the return stays 0.
    """
    import tempfile

    failures = []
    root = Path(tempfile.mkdtemp())

    def build(name, decorated, plain_children):
        v = root / name
        (v / decorated).mkdir(parents=True, exist_ok=True)
        plain = v / "Meta"
        plain.mkdir(parents=True, exist_ok=True)
        for c in plain_children:
            (plain / c).mkdir(exist_ok=True)
        return v

    # BITE: a human marker leaked into the bare machine Meta/ is the defect.
    leaked = build("leaked", "\u2699\ufe0f Meta", ["Sessions"])
    rc = main([str(leaked)])
    if rc != 1:
        failures.append(
            "a leaked human marker in the bare Meta/ returned {}, expected 1. "
            "SPLIT_META is not reaching the exit.".format(rc))

    # INVERSE: the same two-Meta layout with NO leak must stay clean. Without
    # this, a main() that returned 1 unconditionally would satisfy the case
    # above and the control would prove nothing.
    clean = build("clean", "\u2699\ufe0f Meta", [])
    rc = main([str(clean)])
    if rc != 0:
        failures.append(
            "a partitioned vault with no leak returned {}, expected 0. The "
            "BITE case above may be passing for the wrong reason.".format(rc))

    # A single Meta dir is the ordinary shape and must never fail.
    single = root / "single"
    (single / "\u2699\ufe0f Meta").mkdir(parents=True, exist_ok=True)
    rc = main([str(single)])
    if rc != 0:
        failures.append("a single-Meta vault returned {}, expected 0".format(rc))

    if failures:
        print("SELF-TEST FAIL:")
        for f in failures:
            print("  - {}".format(f))
        return 1
    print("OK - self-test: a leaked human marker exits 1, while a partitioned "
          "vault and a single-Meta vault both exit 0 (so the control is not "
          "vacuous).")
    return 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    porcelain = "--porcelain" in argv
    args = [a for a in argv if a != "--porcelain"]
    vault = Path(args[0]) if args else Path.cwd()
    result = verdict(vault)
    print(result if porcelain else "split-meta check: %s" % result)
    # SPLIT_META is a concrete detected defect with a one-time reconcile
    # remedy, not a status line. Before this, the only thing separating it from
    # OK_PARTITIONED was a stdout token that a non-porcelain caller ignores, and
    # the file carried no non-zero exit at all.
    return 1 if result.startswith("SPLIT_META") else 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main(sys.argv[1:]))
