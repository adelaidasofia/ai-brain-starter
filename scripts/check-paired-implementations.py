#!/usr/bin/env python3
"""check-paired-implementations.py - a behaviour implemented twice must SAY so,
and a cross-repo pointer must never assert the other side's state.

THE BUG CLASS. One contract, several hand-written implementations, nothing
pairing them. Someone fixes one; the rest stay wrong; every check on both sides
stays green, because each implementation is internally consistent and no gate has
ever seen another. machinery-sidecar/1 drifted three times this way: cloud-sync
Mirror roots (MYC-1088), the vault-target guard (MYC-4035), and the Python
interpreter probe - which the Windows leg had already solved correctly while the
POSIX leg kept the weaker check.

WHY HEADER COMMENTS ALONE FAILED. They were tried and they half-worked. Every
one of them points from the NEWER implementation to the older: the .ps1 names the
.sh, vault_safety.rs names the .sh, and the .sh names nothing at all. So the file
whose edit STARTS every drift is the one file with no pointer. This gate fixes
the direction (G3) rather than trusting people to keep writing the comment.

WHY A POINTER AND NOT AN ENUMERATION. A header listing its siblings is itself a
copy of the list, and rots exactly like one. Each paired file carries ONE durable
pointer to scripts/paired-implementations.json; that file holds the enumeration.
Adding a fourth implementation edits one place, not four.

G4 IS THE ONE THAT PAYS FOR ITSELF. A sentence in this repo asserting the CURRENT
STATE of code in another repo cannot stay true - nothing here runs when that repo
merges. ADR-0007 said the Rust twin "does not yet carry this decision"; it was
written at 04:55:58Z and refuted at 05:25:12Z the same morning, then sat false for
eight days while the file it described was edited again. Naming a counterpart is
durable. Describing its state is not. G4 permits the first and fails the second.

WHAT THIS DELIBERATELY DOES NOT DO. It does not check out another repo, diff the
implementations, or claim they agree - public CI cannot read a private paid repo,
and a gate that pretends to verify what it cannot see is worse than none. Remote
entries are declarations. The verification this DOES perform is entirely local.

Usage:
    python3 scripts/check-paired-implementations.py             # the gate
    python3 scripts/check-paired-implementations.py --json      # machine-readable
    python3 scripts/check-paired-implementations.py --self-test # negative controls
    python3 scripts/check-paired-implementations.py --changed <paths...>
    python3 scripts/check-paired-implementations.py --changed-from FILE
                                                    # advisory: name counterparts

Exit codes:
    0  every declared pair is well-formed, present, pointing at the list, and
       free of cross-repo state claims (or: advisory mode, which never fails)
    1  a malformed entry, a missing local file, a paired file with no pointer,
       or a cross-repo state claim
    2  UNEVALUATED - the list could not be read or parsed. Never 0: nothing
       compared is not parity.

Provenance: MYC-4036. Sibling gate: scripts/check-hook-parity.py (which asks
whether one behaviour reaches both PLATFORMS; this asks whether one contract's
several IMPLEMENTATIONS know about each other).
"""
# exit-contract: ENFORCING


from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIST_PATH = REPO / "scripts" / "paired-implementations.json"

# The durable pointer every paired file must carry.
POINTER = "paired-implementations.json"

# Repos whose state this repo must not assert.
FOREIGN_REPOS = ("mycelium-studio", "memory-runtime-pro", "mycelium-site")
FOREIGN_TOKENS = FOREIGN_REPOS + ("vault_safety.rs", "src-tauri")

# Prose that asserts the CURRENT state of code living somewhere else. Naming a
# counterpart is fine and encouraged; describing what it does or does not do
# right now is the half that rots. Derived from the real ADR-0007 sentence.
STATE_CLAIM = re.compile(
    r"\b("
    r"do(?:es)?\s+not\s+yet|doesn't\s+yet|"
    r"not\s+yet\s+(?:carry|carried|ported|have|has|implement)|"
    r"ha(?:s|ve)\s+not\s+been\s+ported|hasn't\s+been\s+ported|"
    r"is\s+still\s+blind|remains?\s+blind|is\s+blind\s+to|stayed?\s+blind|"
    r"currently\s+(?:lacks|does\s+not|has\s+no|is\s+not)|"
    r"until\s+it\s+does|"
    r"does\s+not\s+carry|do\s+not\s+carry"
    r")\b",
    re.IGNORECASE,
)

# A cross-repo mention and its state claim often sit on different lines (ADR-0007
# split across two). Join a small window so the pair is visible.
WINDOW = 2


class Unevaluated(Exception):
    """The gate could not run. Never reported as a pass."""


def load_contracts(list_path: Path) -> list[dict]:
    try:
        raw = json.loads(list_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise Unevaluated(f"{list_path} is missing") from exc
    except json.JSONDecodeError as exc:
        raise Unevaluated(f"{list_path} is not valid JSON: {exc}") from exc
    contracts = raw.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        raise Unevaluated(f"{list_path} declares no contracts")
    return contracts


def _adr_files(repo: Path) -> list[Path]:
    adr = repo / "docs" / "adr"
    return sorted(adr.glob("*.md")) if adr.is_dir() else []


def check(repo: Path, list_path: Path) -> list[str]:
    """Pure verdict: returns a list of failure strings (empty == pass).

    Separated from argument plumbing so --self-test can drive it against
    synthetic trees without touching the real repo.
    """
    contracts = load_contracts(list_path)
    failures: list[str] = []
    seen_ids: set[str] = set()
    local_declared: list[Path] = []

    for idx, c in enumerate(contracts):
        cid = c.get("id")
        where = f"contracts[{idx}]"

        # G1 SHAPE
        if not cid or not isinstance(cid, str):
            failures.append(f"G1 SHAPE: {where} has no 'id'")
            continue
        if cid in seen_ids:
            failures.append(f"G1 SHAPE: duplicate contract id '{cid}'")
        seen_ids.add(cid)
        if not (c.get("what") or "").strip():
            failures.append(f"G1 SHAPE: '{cid}' has no 'what' (state the behaviour, not the files)")

        impls = c.get("implementations")
        if not isinstance(impls, list) or len(impls) < 2:
            # G5 NO-ORPHAN: one implementation is not a pair. Either a sibling was
            # deleted (the pairing is stale) or this never belonged here.
            failures.append(
                f"G5 NO-ORPHAN: '{cid}' declares {len(impls) if isinstance(impls, list) else 0} "
                f"implementation(s); a contract with fewer than 2 is not a pair"
            )
            continue

        for j, impl in enumerate(impls):
            tag = f"'{cid}'.implementations[{j}]"
            path = impl.get("path")
            if not path:
                failures.append(f"G1 SHAPE: {tag} has no 'path'")
                continue
            if not (impl.get("role") or "").strip():
                failures.append(f"G1 SHAPE: {tag} ({path}) has no 'role'")
            if not isinstance(impl.get("local"), bool):
                failures.append(f"G1 SHAPE: {tag} ({path}) has no boolean 'local'")
                continue

            if not impl["local"]:
                # Remote: a declaration only. Must name the repo it lives in, so
                # the reader can find it; must NOT be verified here.
                if not impl.get("repo"):
                    failures.append(f"G1 SHAPE: {tag} is local=false but names no 'repo'")
                continue

            # G2 EXISTS
            target = repo / path
            if not target.exists():
                failures.append(
                    f"G2 EXISTS: '{cid}' declares {path}, which does not exist "
                    f"(renamed or deleted? the pairing is now wrong)"
                )
                continue
            local_declared.append(target)

            # G3 DECLARED - the direction fix. Every paired file points at the list.
            try:
                body = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                failures.append(f"G2 EXISTS: could not read {path}: {exc}")
                continue
            if POINTER not in body:
                failures.append(
                    f"G3 DECLARED: {path} is a declared implementation of '{cid}' but "
                    f"contains no pointer to {POINTER}. A reader editing this file "
                    f"cannot discover it has counterparts."
                )

    # G4 NO-STATE-CLAIM - across declared local files AND every ADR.
    for f in sorted(set(local_declared) | set(_adr_files(repo))):
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel = f.relative_to(repo)
        for i, line in enumerate(lines):
            if not any(tok in line for tok in FOREIGN_TOKENS):
                continue
            window = " ".join(lines[i : i + 1 + WINDOW])
            m = STATE_CLAIM.search(window)
            if m:
                failures.append(
                    f"G4 NO-STATE-CLAIM: {rel}:{i + 1} asserts the current state of "
                    f"another repo (\"{m.group(0)}\"). Name the counterpart; do not "
                    f"describe it. Nothing here runs when that repo merges, so the "
                    f"sentence rots silently. Point at {POINTER} instead."
                )
    return failures


def advise(repo: Path, list_path: Path, changed: list[str]) -> list[str]:
    """Advisory: for each changed file that is a declared implementation, name its
    counterparts. Never fails - the repos have different release cadences and a
    hard cross-repo gate would only get bypassed."""
    contracts = load_contracts(list_path)
    notes: list[str] = []
    changed_set = {c.strip().lstrip("./") for c in changed if c.strip()}
    for c in contracts:
        impls = c.get("implementations") or []
        for impl in impls:
            if impl.get("path") not in changed_set or not impl.get("local"):
                continue
            others = [i for i in impls if i is not impl]
            lines = [
                f"{impl['path']} implements '{c['id']}', which has "
                f"{len(others)} other implementation(s):"
            ]
            for o in others:
                loc = o["path"] if o.get("local") else f"{o.get('repo')}/{o['path']}"
                sym = f"::{o['symbol']}" if o.get("symbol") else ""
                lines.append(f"  - {loc}{sym}  ({o.get('role', '')})")
            hist = c.get("drift_history") or []
            if hist:
                lines.append(f"  This contract has drifted before: {', '.join(hist)}.")
            lines.append("  Does your change need to land there too?")
            notes.append("\n".join(lines))
    return notes


# ---------------------------------------------------------------- self-test

def _fixture(tmp: Path, list_obj: dict, files: dict[str, str]) -> Path:
    (tmp / "scripts").mkdir(parents=True, exist_ok=True)
    lp = tmp / "scripts" / "paired-implementations.json"
    lp.write_text(json.dumps(list_obj), encoding="utf-8")
    for rel, body in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return lp


def _base_list() -> dict:
    return {
        "contracts": [
            {
                "id": "demo/1",
                "what": "a demo behaviour",
                "implementations": [
                    {"path": "scripts/a.sh", "role": "posix", "local": True},
                    {"repo": "mycelium-studio", "path": "src/b.rs", "role": "native", "local": False},
                ],
            }
        ]
    }


def self_test() -> int:
    import tempfile

    cases: list[tuple[str, dict, dict[str, str], str]] = []

    ok_files = {"scripts/a.sh": f"# see scripts/{POINTER}\n"}
    cases.append(("control (must PASS)", _base_list(), ok_files, ""))

    missing = _base_list()
    cases.append(("G2 missing local file", missing, {}, "G2 EXISTS"))

    cases.append(("G3 no pointer", _base_list(), {"scripts/a.sh": "# nothing\n"}, "G3 DECLARED"))

    orphan = _base_list()
    orphan["contracts"][0]["implementations"] = [
        {"path": "scripts/a.sh", "role": "posix", "local": True}
    ]
    cases.append(("G5 single implementation", orphan, ok_files, "G5 NO-ORPHAN"))

    noid = {"contracts": [{"what": "x", "implementations": []}]}
    cases.append(("G1 no id", noid, {}, "G1 SHAPE"))

    nowhat = _base_list()
    nowhat["contracts"][0]["what"] = ""
    cases.append(("G1 no what", nowhat, ok_files, "G1 SHAPE"))

    # G4, in the exact shape that actually shipped: the repo token and the state
    # claim on DIFFERENT lines.
    split = {
        "scripts/a.sh": (
            f"# see scripts/{POINTER}\n"
            "# A second implementation exists in `mycelium-studio`\n"
            "# (`vault_safety.rs`) and does not yet carry this decision.\n"
        )
    }
    cases.append(("G4 split-line state claim", _base_list(), split, "G4 NO-STATE-CLAIM"))

    # G4 must NOT fire on a bare, durable counterpart NAME.
    named = {
        "scripts/a.sh": (
            f"# see scripts/{POINTER}\n"
            "# Paired with mycelium-studio apps/desktop/src-tauri/.../vault_safety.rs.\n"
        )
    }
    cases.append(("G4 bare name is allowed (must PASS)", _base_list(), named, ""))

    failed = 0
    for name, lst, files, expect in cases:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            lp = _fixture(tmp, lst, files)
            try:
                found = check(tmp, lp)
            except Unevaluated as exc:
                found = [f"UNEVALUATED: {exc}"]
            hit = any(expect in f for f in found) if expect else not found
            if hit:
                print(f"  ok    {name}")
            else:
                failed += 1
                print(f"  FAIL  {name}")
                print(f"        expected: {expect or '(no failures)'}")
                print(f"        got:      {found or '(no failures)'}")

    # UNEVALUATED control: a corrupt list must never read as a pass.
    with tempfile.TemporaryDirectory() as td:
        lp = Path(td) / "bad.json"
        lp.write_text("{ not json", encoding="utf-8")
        try:
            check(Path(td), lp)
            failed += 1
            print("  FAIL  corrupt list must raise Unevaluated")
        except Unevaluated:
            print("  ok    corrupt list -> UNEVALUATED (never 0)")

    print()
    if failed:
        print(f"{failed} negative control(s) did not bite. The gate is not proven.")
        return 1
    print("all negative controls bit.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--self-test", action="store_true", help="run negative controls")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--changed", nargs="*", default=None,
                    help="advisory mode: name counterparts of these changed paths")
    ap.add_argument("--changed-from", default=None, metavar="FILE",
                    help="advisory mode, reading newline-separated paths from FILE "
                         "(avoids shell word-splitting on paths with spaces)")
    ap.add_argument("--list", default=str(LIST_PATH))
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    list_path = Path(args.list)

    if args.changed_from:
        try:
            args.changed = Path(args.changed_from).read_text(
                encoding="utf-8").splitlines()
        except OSError as exc:
            print(f"UNEVALUATED: cannot read {args.changed_from}: {exc}",
                  file=sys.stderr)
            return 2

    if args.changed is not None:
        try:
            notes = advise(REPO, list_path, args.changed)
        except Unevaluated as exc:
            print(f"UNEVALUATED: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps({"advisories": notes}, indent=2))
        else:
            for n in notes:
                # GitHub renders this inline on the PR; harmless locally.
                print(f"::notice::{n}")
                print(n, file=sys.stderr)
        return 0

    try:
        failures = check(REPO, list_path)
    except Unevaluated as exc:
        if args.json:
            print(json.dumps({"status": "unevaluated", "reason": str(exc)}, indent=2))
        else:
            print(f"UNEVALUATED: {exc}", file=sys.stderr)
            print("Nothing compared is not parity.", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"status": "fail" if failures else "pass",
                          "failures": failures}, indent=2))
        return 1 if failures else 0

    if failures:
        print("Paired-implementation check FAILED:\n", file=sys.stderr)
        for f in failures:
            print(f"  - {f}\n", file=sys.stderr)
        return 1
    print("paired implementations: every declared pair is present, pointed at the "
          "list, and free of cross-repo state claims.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
