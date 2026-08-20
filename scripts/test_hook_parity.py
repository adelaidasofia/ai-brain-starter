#!/usr/bin/env python3
"""test_hook_parity.py - the cross-platform hook parity gate must bite.

WHY THIS SUITE EXISTS. scripts/check-hook-parity.py is the only thing in the
repo that compares WHICH hooks end up wired on POSIX against which end up wired
on Windows. Before it, four bash hooks were dropped by
platformize_template_for_windows on every Windows install and nothing anywhere
failed: the installer exits 0, --verify passes, check-hook-activation.py reads
the POSIX hooks.json, and CI only asserts the install SUCCEEDS. A Windows job
and a macOS job could differ by three whole features and both stay green.

A gate nobody tests is the same shape as the bug it was written for: it would
keep printing OK after any edit that stopped it from detecting anything. So this
suite drives it from the outside, against fixtures, and asserts it goes RED for
each way a difference can hide:

  * a new drop that is not in the allowlist          (the original bug)
  * an allowlist entry with no written reason        (noticing != deciding)
  * an allowlist entry for a difference that is gone (excuses outliving fixes)
  * an unreadable or missing allowlist               (must be UNEVALUATED, not
                                                      an empty allowlist, which
                                                      would look like it works)
  * a template that wires nothing on a leg           (nothing compared != parity)

It also pins the gate against the installer's own accounting: the set the gate
calls "dropped on Windows" must equal what platformize_template_for_windows
itself reports skipping. Two independent readings of the same function; if they
ever disagree, one of them is lying.

PREMISE GUARDS. hooks.json must still contain at least one `bash <script>.sh`
hook - if every hook became cross-platform, the four allowlisted drops would
vanish, the gate would go red on stale entries, and this suite would be pinning
a risk that no longer exists. That is a premise break, reported as such, not a
silent pass over an empty fixture.

Stdlib only, no pytest (repo convention). Hermetic: fixtures live in a tmpdir,
nothing outside it is written. Exit non-zero on any failure.

Provenance: MYC-3879. Sibling suite: scripts/test_sessionstart_audit_verdict.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "check-hook-parity.py"
ALLOWLIST = REPO / "scripts" / "hook-parity-allowlist.json"
INSTALLER = REPO / "scripts" / "install-hooks-user-level.py"
HOOKS_JSON = REPO / "hooks.json"

PASSED = 0
FAILED = 0


def ok(msg: str) -> None:
    global PASSED
    PASSED += 1
    print(f"PASS  {msg}")


def bad(msg: str, detail: str) -> None:
    global FAILED
    FAILED += 1
    print(f"FAIL  {msg}\n        {detail}")


def premise(cond: bool, msg: str) -> None:
    if cond:
        print(f"premise  {msg}")
        return
    print(f"PREMISE BROKEN: {msg}")
    print("This suite would be testing nothing. Fix the premise, not the assert.")
    raise SystemExit(3)


def run(args: list[str]) -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(GATE), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def run_json(args: list[str]) -> tuple[int, dict]:
    rc, out = run([*args, "--json"])
    try:
        return rc, json.loads(out[out.index("{"):out.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return rc, {}


def main() -> int:
    print("=== premises ===")
    premise(GATE.is_file(), f"parity gate present: {GATE}")
    premise(ALLOWLIST.is_file(), f"committed allowlist present: {ALLOWLIST}")
    premise(INSTALLER.is_file(), f"installer present: {INSTALLER}")
    premise(HOOKS_JSON.is_file(), f"hooks.json present: {HOOKS_JSON}")

    hooks_src = HOOKS_JSON.read_text(encoding="utf-8")
    premise("bash " in hooks_src and ".sh" in hooks_src,
            "hooks.json still wires at least one bash script (the drop risk is real)")

    allow = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    premise(bool(allow.get("posix_only")),
            "the allowlist still records at least one reviewed platform drop")

    print("\n=== 1. the gate's own negative controls ===")
    rc, out = run(["--self-test"])
    if rc == 0 and "SELF-TEST PASS" in out:
        ok("--self-test passes (the gate proves it bites a synthetic mismatch)")
    else:
        bad("--self-test passes", f"rc={rc} out={out.strip()[:400]}")

    print("\n=== 2. the gate is green on this tree ===")
    rc, data = run_json([])
    if rc == 0 and data.get("verdict") == "PASS":
        ok("every current POSIX/Windows difference is reviewed")
    else:
        bad("gate green on main",
            f"rc={rc} unreviewed={data.get('unreviewed')} "
            f"reasonless={data.get('reasonless')} stale={data.get('stale')}")

    print("\n=== 3. the gate agrees with the installer's own skip accounting ===")
    # posix_only is derived from a SET DIFFERENCE of the two legs; the skip list
    # comes from platformize_template_for_windows itself. They must name the
    # same hooks, or one of the two readings is wrong.
    # JSON pairs are [event, hook, args]: arguments joined the comparison key so
    # a dropped ARGUMENT variant is a difference too. Index rather than unpack,
    # so this reading survives the key gaining another dimension later.
    dropped = {p[1] for p in (data.get("posix_only") or [])}
    reported = {s for s in (data.get("installer_reported_skips") or [])}
    unaccounted = [h for h in dropped if not any(h in s for s in reported)]
    if dropped and not unaccounted:
        ok(f"all {len(dropped)} dropped hook(s) appear in the installer's skip list")
    else:
        bad("gate matches installer accounting",
            f"dropped={sorted(dropped)} unaccounted={unaccounted} "
            f"skips={sorted(reported)}")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        print("\n=== 4. a NEW unreviewed drop goes RED ===")
        # Simulates exactly what happens when someone adds a bash hook: the
        # difference exists and the allowlist does not mention it.
        thinned = json.loads(json.dumps(allow))
        removed = thinned["posix_only"].pop(0)
        f = tmp / "allow_thinned.json"
        f.write_text(json.dumps(thinned), encoding="utf-8")
        rc, out = run(["--allowlist", str(f)])
        if rc == 1 and "UNREVIEWED" in out and removed["hook"] in out:
            ok(f"an unreviewed drop of {removed['hook']} fails the gate (exit 1)")
        else:
            bad("unreviewed drop fails",
                f"rc={rc} - the gate would let a silent Windows feature loss land. "
                f"out={out.strip()[:400]}")

        print("\n=== 5. an allowlist entry with NO reason goes RED ===")
        blank = json.loads(json.dumps(allow))
        blank["posix_only"][0]["reason"] = "   "
        f = tmp / "allow_blank.json"
        f.write_text(json.dumps(blank), encoding="utf-8")
        rc, out = run(["--allowlist", str(f)])
        if rc == 1 and "no reason" in out:
            ok("a reasonless entry fails (noticing is not deciding)")
        else:
            bad("reasonless entry fails",
                f"rc={rc} - the allowlist would degrade to a mute skip list. "
                f"out={out.strip()[:400]}")

        print("\n=== 6. a STALE allowlist entry goes RED ===")
        stale = json.loads(json.dumps(allow))
        stale["posix_only"].append({
            "event": "Stop", "hook": "already-fixed.sh",
            "reason": "describes a difference that does not exist"})
        f = tmp / "allow_stale.json"
        f.write_text(json.dumps(stale), encoding="utf-8")
        rc, out = run(["--allowlist", str(f)])
        if rc == 1 and "already-fixed.sh" in out:
            ok("an entry for a non-existent difference fails (excuses expire)")
        else:
            bad("stale entry fails",
                f"rc={rc} - the allowlist could rot into a list nobody re-reads. "
                f"out={out.strip()[:400]}")

        print("\n=== 7. a missing / malformed allowlist is UNEVALUATED, not a pass ===")
        rc, out = run(["--allowlist", str(tmp / "does-not-exist.json")])
        if rc == 2 and "UNEVALUATED" in out:
            ok("a missing allowlist exits 2 (never silently 'no exceptions')")
        else:
            bad("missing allowlist exits 2",
                f"rc={rc} - an absent allowlist read as empty looks like a working "
                f"strict gate. out={out.strip()[:300]}")

        f = tmp / "allow_broken.json"
        f.write_text("{ not json", encoding="utf-8")
        rc, out = run(["--allowlist", str(f)])
        if rc == 2 and "UNEVALUATED" in out:
            ok("an unparseable allowlist exits 2")
        else:
            bad("unparseable allowlist exits 2", f"rc={rc} out={out.strip()[:300]}")

        f = tmp / "allow_shape.json"
        f.write_text(json.dumps({"posix_only": [{"hook": "x.sh", "reason": "no event"}]}),
                     encoding="utf-8")
        rc, out = run(["--allowlist", str(f)])
        if rc == 2 and "UNEVALUATED" in out:
            ok("an entry missing event/hook exits 2 (shape is not guessed at)")
        else:
            bad("malformed entry exits 2", f"rc={rc} out={out.strip()[:300]}")

        print("\n=== 8. a template that wires nothing is UNEVALUATED, not parity ===")
        empty = tmp / "hooks_empty.json"
        empty.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        rc, out = run(["--hooks-json", str(empty)])
        if rc == 2 and "UNEVALUATED" in out:
            ok("an empty template exits 2 (empty == empty is not agreement)")
        else:
            bad("empty template exits 2",
                f"rc={rc} - two empty sets match perfectly and mean nothing. "
                f"out={out.strip()[:300]}")

        print("\n=== 9. a real synthetic bash hook is caught end-to-end ===")
        synth = tmp / "hooks_synth.json"
        synth.write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [
                {"type": "command", "command": "bash '[VAULT_PATH]/brand-new.sh'"},
                {"type": "command", "command": "[PYTHON] ~/.claude/hooks/kept.py"},
            ]}]}}), encoding="utf-8")
        empty_allow = tmp / "allow_empty.json"
        empty_allow.write_text(json.dumps({"posix_only": [], "windows_only": []}),
                               encoding="utf-8")
        rc, data2 = run_json(["--hooks-json", str(synth),
                              "--allowlist", str(empty_allow)])
        # (event, hook) only: this case is a WHOLE hook vanishing, not an
        # argument variant. Pairs now carry args as a third element - a JSON
        # list, so a raw tuple(p) would be unhashable as well as too specific.
        posix_only = {(p[0], p[1]) for p in (data2.get("posix_only") or [])}
        if rc == 1 and ("Stop", "brand-new.sh") in posix_only:
            ok("a newly added bash hook is reported as dropped on Windows")
        else:
            bad("new bash hook caught",
                f"rc={rc} posix_only={sorted(posix_only)} - this is the exact "
                f"shape of the four hooks already lost.")
        if ("Stop", "kept.py") not in posix_only:
            ok("the cross-platform .py hook beside it is NOT falsely reported")
        else:
            bad("no false positive on a .py hook",
                "a gate that flags everything gets muted and then ignored")

    print(f"\n=== summary: {PASSED} passed, {FAILED} failed ===")
    return 1 if FAILED else 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313).
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
