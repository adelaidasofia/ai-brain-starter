#!/usr/bin/env python3
"""Validate that the install phase files form one complete, walkable chain.

The install is 12 files under `phases/`, ~3,700 lines, covering phases 0-24.
Until this guard shipped, the ONLY thing sequencing them was the Phase Routing
Table in SKILL.md. Ten of the twelve files contained no reference to any other
phase file anywhere in their body: each one simply ended.

That is a silent failure. A model reads SKILL.md, starts executing, and by the
time it finishes `phase-02-03-plugins-folders.md` it has consumed ~68 KB of
phase content plus the user's interview answers. The routing table is no longer
steering. The file ends, and the model reports the install complete. The user is
left with folders and a skeleton CLAUDE.md, and the phases that build the
context layer, the journal, the panel and the insights never run. Nothing
errors. Nothing is missing from disk. It just stops.

Observed in the wild 2026-08-15: an install stopped dead at the
phase-02-03 -> phase-04 file boundary and reported itself finished. The user's
own CLAUDE.md recorded "phases completed: folder structure (Phase 3) + profile
(Phase 3b)" while 15 phases had never run.

So every phase file now carries a footer that names its successor, and this
checker holds that footer to a contract. The footer is BOTH:

  - model-facing prose  (`phases/<next>.md` referenced in the text) so the model
    executing the install actually knows where to go, and
  - a machine marker    (`<!-- phase-chain: next=<file> -->`) so this checker can
    verify the chain without parsing prose.

Both halves are required. A marker with no prose is a green checker over an
install that still stops; prose with no marker is an unverifiable chain. The
pair is the contract.

Checks (all structural — no false positives possible, no judgement calls):

  1. Every `phases/phase-*.md` carries exactly one phase-chain marker.
  2. Exactly one file is marked `terminal`.
  3. Every `next=` target names a file that EXISTS in phases/.
  4. Non-terminal files also reference `phases/<target>` in their prose, so the
     machine marker and the model-facing instruction cannot drift apart.
  5. Exactly one head (a file nothing else points to).
  6. Walking from the head reaches EVERY phase file exactly once — no orphan, no
     cycle, no fork.

Check 6 is the one that catches the real regression: someone adds
`phase-25-whatever.md`, wires nothing to it, and it never runs for anybody.

Exit 0 = chain intact. Exit 1 = contract violated. Exit 2 = could not check
(fail loud; a checker that cannot read its inputs must never report success).

Pure stdlib. Usage:
    python3 scripts/check-phase-chain.py
    python3 scripts/check-phase-chain.py --phases-dir /path/to/phases
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MARKER_RE = re.compile(
    r"<!--\s*phase-chain:\s*(?:next=(?P<next>[A-Za-z0-9._-]+)|(?P<terminal>terminal))\s*-->"
)


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)


def cannot_check(msg: str) -> int:
    print(f"CANNOT CHECK: {msg}", file=sys.stderr)
    print(
        "  A checker that cannot read its inputs must not report success.",
        file=sys.stderr,
    )
    return 2


def main() -> int:
    # Windows cp1252 consoles raise UnicodeEncodeError on the em dashes this
    # script prints (ai-brain-starter#313). Guard the streams at the entrypoint.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--phases-dir",
        default=None,
        help="directory holding phase-*.md (default: <repo>/phases)",
    )
    ap.add_argument(
        "--skill-md",
        default=None,
        help="SKILL.md to cross-check the routing-table order against "
             "(default: the sibling of --phases-dir; 'none' to skip)",
    )
    args = ap.parse_args()

    if args.phases_dir:
        phases_dir = Path(args.phases_dir)
    else:
        phases_dir = Path(__file__).resolve().parent.parent / "phases"

    if args.skill_md == "none":
        skill_md = None
    elif args.skill_md:
        skill_md = Path(args.skill_md)
    else:
        # Default: the SKILL.md beside the phases dir. Absent (a bare temp
        # fixture) means "nothing to cross-check", not a failure.
        candidate = phases_dir.parent / "SKILL.md"
        skill_md = candidate if candidate.is_file() else None

    if not phases_dir.is_dir():
        return cannot_check(f"phases directory not found: {phases_dir}")

    files = sorted(p for p in phases_dir.glob("phase-*.md") if p.is_file())
    if not files:
        return cannot_check(f"no phase-*.md files found in {phases_dir}")

    violations: list[str] = []

    # ---- 1/2/3/4: per-file marker contract ---------------------------------
    nxt: dict[str, str] = {}          # file -> successor filename
    terminals: list[str] = []

    for path in files:
        name = path.name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return cannot_check(f"cannot read {name}: {exc}")

        markers = list(MARKER_RE.finditer(text))
        if not markers:
            violations.append(
                f"{name}: no phase-chain marker. The file ends without telling the "
                f"model what runs next, which is how an install stops silently. "
                f"Add a footer with <!-- phase-chain: next=<file> --> "
                f"(or 'terminal' for the last phase)."
            )
            continue
        if len(markers) > 1:
            violations.append(
                f"{name}: {len(markers)} phase-chain markers; expected exactly 1. "
                f"Two successors is not a chain."
            )
            continue

        m = markers[0]
        if m.group("terminal"):
            terminals.append(name)
            continue

        target = m.group("next")
        nxt[name] = target

        if not (phases_dir / target).is_file():
            violations.append(
                f"{name}: points to '{target}', which does not exist in "
                f"{phases_dir.name}/. The install would dead-end here."
            )
            continue

        # 4: the machine marker must be backed by model-facing prose.
        if f"phases/{target}" not in text:
            violations.append(
                f"{name}: marker says next={target}, but the body never references "
                f"`phases/{target}`. The checker would pass while the model "
                f"executing the install still has no instruction to continue."
            )

    # ---- 2: exactly one terminal -------------------------------------------
    if not terminals:
        violations.append(
            "no file is marked <!-- phase-chain: terminal -->. The last phase must "
            "declare itself the end, so 'the chain stops here' is a decision on "
            "record and not an omission."
        )
    elif len(terminals) > 1:
        violations.append(
            f"{len(terminals)} files marked terminal ({', '.join(sorted(terminals))}); "
            f"expected exactly 1."
        )

    # ---- 5: exactly one head ------------------------------------------------
    all_names = {p.name for p in files}
    pointed_at = set(nxt.values())
    heads = sorted(all_names - pointed_at)

    if len(heads) != 1:
        violations.append(
            f"expected exactly 1 head (a phase nothing points to); found "
            f"{len(heads)}: {', '.join(heads) if heads else '(none — the chain is a cycle)'}"
        )

    # ---- 6: the walk must cover every file exactly once ---------------------
    # Only meaningful once there is a single unambiguous head.
    if len(heads) == 1 and not violations:
        seen: list[str] = []
        cur: str | None = heads[0]
        while cur is not None:
            if cur in seen:
                violations.append(
                    f"cycle in the phase chain at '{cur}': {' -> '.join(seen)} -> {cur}"
                )
                break
            seen.append(cur)
            cur = nxt.get(cur)

        unreached = sorted(all_names - set(seen))
        if unreached:
            violations.append(
                "these phase files are never reached by walking the chain from "
                f"{heads[0]}: {', '.join(unreached)}. A phase nothing points to "
                "never runs for anybody."
            )

    # ---- 7: the chain must agree with SKILL.md's routing table --------------
    # Two sources of truth for the same order is the drift this repo already
    # learned about on close phases (#400/#415): each file stays internally
    # consistent while they disagree with each other, and nothing errors. The
    # footers now DRIVE execution, so a table that disagrees is documentation
    # that lies -- and a reordered footer would pass every check above.
    if len(heads) == 1 and not violations and skill_md is not None:
        if not skill_md.is_file():
            return cannot_check(f"SKILL.md not found at {skill_md}")
        try:
            skill_text = skill_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return cannot_check(f"cannot read {skill_md}: {exc}")

        # Document order, de-duplicated (23.5 and 24 re-cite the final file).
        table: list[str] = []
        for m in re.finditer(r"phases/(phase-[A-Za-z0-9._-]+\.md)", skill_text):
            name = m.group(1)
            if name not in table:
                table.append(name)

        walked = []
        cur = heads[0]
        while cur is not None:
            walked.append(cur)
            cur = nxt.get(cur)

        if not table:
            violations.append(
                f"{skill_md.name} names no phase files at all; the routing table "
                f"cannot be cross-checked against the chain."
            )
        elif table != walked:
            violations.append(
                "the phase chain and the SKILL.md routing table disagree about "
                "order.\n         chain: " + " -> ".join(walked) +
                "\n         table: " + " -> ".join(table) +
                "\n         The footers drive execution, so the table is the one "
                "describing something that no longer happens."
            )

    if violations:
        print(f"Phase chain contract violated ({len(violations)} problem(s)):\n", file=sys.stderr)
        for v in violations:
            fail(v)
        return 1

    order = []
    cur = heads[0]
    while cur is not None:
        order.append(cur)
        cur = nxt.get(cur)
    print(f"Phase chain intact: {len(order)} phases, {order[0]} -> {order[-1]} (terminal).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
