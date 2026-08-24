#!/usr/bin/env python3
"""test_sessionstart_audit_verdict.py - the SessionStart auditor must never
report OK over a set it never looked at.

THE BUG CLASS. scripts/audit-sessionstart-boundedness.py answers "is every
SessionStart hook bounded?" by resolving each wired command to a file on disk
and reading it. Every hook it CANNOT resolve is skipped. Both verdict paths then
printed a bare OK and exited 0 over whatever survived:

    Effective SessionStart set: 6 resolved hook(s) - 6 clean, 0 unguarded;
                                19 unresolved.
    All resolved SessionStart corpus walks are guarded or declared-bounded. OK.

That is a real run against a real settings.json (Windows, 2026-08-13): 19 of 25
wired commands were never opened, and the exit code said PASS. A freeze-class
hook sitting in any of those 19 would have been invisible - the auditor would
have kept printing OK forever. "Nothing was checked" is not "everything is
bounded", and only the exit code decides whether CI or /diagnose believes it.

Two independent holes produced that number, and this suite pins both:

  A1  RESOLUTION. --all resolved a wired basename under <repo>/hooks only.
      heal-journal-guard.py is wired on SessionStart and lives in
      <repo>/scripts, so the canonical audit silently skipped it. Resolution now
      walks every root given to --hooks-dir (default: hooks/ AND scripts/).

  A2  VERDICT. Anything skipped now yields UNEVALUATED and exit 2, matching
      scripts/audit-guard-activation-roots.py, which already refuses to call an
      empty comparison a pass. Test 9 asserts the two auditors agree, so a
      future edit cannot quietly re-split them.

Also pinned: the detector itself must not be weakened to reach green. Test 1
runs --selftest, whose positive/negative controls prove evaluate() still bites
unguarded corpus walks. Deleting a detector rule would make every fixture below
"clean" and this suite would still be green without it.

PREMISE GUARDS. Every case that could pass vacuously asserts its own setup
first: the auditors exist, --selftest passes, and heal-journal-guard.py really
is wired on SessionStart, really is in scripts/, and really is NOT in hooks/. If
the repo moves that file, this suite fails LOUD as a premise break instead of
quietly testing nothing - which is the same failure mode it exists to prevent.

Stdlib only, no pytest (repo convention). Hermetic: every fixture is built in a
tmpdir, no file outside it is written, and the two real-repo cases only READ.
Exit non-zero on any failure.

Provenance: MYC-3879. Sibling guard: scripts/test_hook_parity.py.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AUDIT = REPO / "scripts" / "audit-sessionstart-boundedness.py"
ROOTS_AUDIT = REPO / "scripts" / "audit-guard-activation-roots.py"
HOOKS_JSON = REPO / "hooks.json"

# The hook that exposed A1: wired on SessionStart, shipped in scripts/, invisible
# to a hooks/-only resolver.
CROSS_DIR_HOOK = "heal-journal-guard.py"

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
    """A premise break is not a soft failure: the suite cannot mean anything
    without it, so say so and stop rather than report green."""
    if cond:
        print(f"premise  {msg}")
        return
    print(f"PREMISE BROKEN: {msg}")
    print("This suite would be testing nothing. Fix the premise, not the assert.")
    raise SystemExit(3)


def run(args: list[str]) -> tuple[int, str]:
    """Run the auditor. encoding pinned so a cp1252 console cannot eat output
    (scripts/check-utf8-subprocess.py enforces this repo-wide)."""
    p = subprocess.run([sys.executable, *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


CLEAN_HOOK = """#!/usr/bin/env python3
from pathlib import Path
def main():
    for _p in Path('~/.claude/state').expanduser().iterdir():  # one level: bounded
        pass
"""
EVIL_HOOK = """#!/usr/bin/env python3
import os
def main():
    for _r, _d, _f in os.walk(os.path.expanduser('~')):  # unbounded corpus walk
        pass
"""


def wire(path: Path, commands: list[str]) -> None:
    path.write_text(json.dumps(
        {"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": c} for c in commands]}]}}),
        encoding="utf-8")


def main() -> int:
    print("=== premises ===")
    premise(AUDIT.is_file(), f"auditor present: {AUDIT}")
    premise(ROOTS_AUDIT.is_file(), f"sibling auditor present: {ROOTS_AUDIT}")
    premise(HOOKS_JSON.is_file(), f"canonical hooks.json present: {HOOKS_JSON}")

    wired = HOOKS_JSON.read_text(encoding="utf-8")
    premise(CROSS_DIR_HOOK in wired,
            f"{CROSS_DIR_HOOK} is still wired in hooks.json")
    premise((REPO / "scripts" / CROSS_DIR_HOOK).is_file(),
            f"{CROSS_DIR_HOOK} still lives in scripts/")
    premise(not (REPO / "hooks" / CROSS_DIR_HOOK).exists(),
            f"{CROSS_DIR_HOOK} is still ABSENT from hooks/ (otherwise A1 is moot)")

    print("\n=== 1. the detector is not weakened (--selftest) ===")
    rc, out = run([str(AUDIT), "--selftest"])
    if rc == 0 and "SELFTEST PASS" in out:
        ok("--selftest still prints SELFTEST PASS")
    else:
        bad("--selftest still passes",
            f"rc={rc}; the fix must not soften evaluate(). out={out.strip()[:300]}")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # ---- A1: resolution across MULTIPLE roots ---------------------------
        print("\n=== 2. A1 hermetic: a wired hook resolves from the SECOND root ===")
        root_a = tmp / "root_a"
        root_b = tmp / "root_b"
        root_a.mkdir()
        root_b.mkdir()
        (root_b / "only-in-b.py").write_text(CLEAN_HOOK, encoding="utf-8")
        premise(not (root_a / "only-in-b.py").exists(),
                "fixture hook is absent from the first resolution root")
        j = tmp / "hooks_multiroot.json"
        wire(j, ["python3 ~/.claude/hooks/only-in-b.py"])
        rc, out = run([str(AUDIT), "--all", "--hooks-json", str(j),
                       "--hooks-dir", str(root_a), str(root_b), "--json"])
        try:
            data = json.loads(out[out.index("{"):out.rindex("}") + 1])
        except (ValueError, json.JSONDecodeError):
            data = {}
        if rc == 0 and data.get("audited") == ["only-in-b.py"] and not data.get("unresolved"):
            ok("--hooks-dir accepts several roots and resolves from any of them")
        else:
            bad("multi-root resolution",
                f"rc={rc} audited={data.get('audited')} "
                f"unresolved={data.get('unresolved')}; out={out.strip()[:300]}")

        print("\n=== 3. A1 live: the real SessionStart set resolves scripts/ hooks ===")
        rc, out = run([str(AUDIT), "--all", "--json"])
        try:
            data = json.loads(out[out.index("{"):out.rindex("}") + 1])
        except (ValueError, json.JSONDecodeError):
            data = {}
        if CROSS_DIR_HOOK in (data.get("audited") or []):
            ok(f"{CROSS_DIR_HOOK} is actually audited, not skipped")
        else:
            bad(f"{CROSS_DIR_HOOK} is audited",
                f"still unresolved={data.get('unresolved')}; a WIRED SessionStart "
                f"hook in scripts/ is never opened. out={out.strip()[:300]}")
        if rc == 0 and not (data.get("unresolved") or []):
            ok("the shipped SessionStart set is fully resolved and exits 0")
        else:
            bad("shipped set fully resolved",
                f"rc={rc} unresolved={data.get('unresolved')}")

        # ---- A2: a skip must never read as a pass ---------------------------
        print("\n=== 4. A2 --all: an unresolved hook yields UNEVALUATED, not OK ===")
        gap = tmp / "gap"
        gap.mkdir()
        (gap / "present.py").write_text(CLEAN_HOOK, encoding="utf-8")
        premise(not (gap / "ghost.py").exists(),
                "the unresolvable fixture hook is genuinely absent from disk")
        j = tmp / "hooks_gap.json"
        wire(j, ["python3 ~/.claude/hooks/present.py",
                 "python3 ~/.claude/hooks/ghost.py"])
        rc, out = run([str(AUDIT), "--all", "--hooks-json", str(j),
                       "--hooks-dir", str(gap)])
        if rc == 2:
            ok("--all exits 2 (UNEVALUATED) when a wired hook was skipped")
        else:
            bad("--all exits 2 on a skipped hook",
                f"rc={rc} - a bare exit 0 over a partly-unread set IS the bug. "
                f"out={out.strip()[:300]}")
        if "UNEVALUATED" in out and "ghost.py" in out:
            ok("--all names the skipped hook and says UNEVALUATED")
        else:
            bad("--all reports the skip",
                f"expected UNEVALUATED + ghost.py; out={out.strip()[:400]}")
        if not re.search(r"guarded or declared-bounded\. OK\.", out):
            ok("--all does NOT print the clean-verdict line over a partial set")
        else:
            bad("--all suppresses the OK line",
                "it still claims every corpus walk is guarded")

        print("\n=== 5. A2 --all: a fully resolved clean set still exits 0 ===")
        j = tmp / "hooks_full.json"
        wire(j, ["python3 ~/.claude/hooks/present.py"])
        rc, out = run([str(AUDIT), "--all", "--hooks-json", str(j),
                       "--hooks-dir", str(gap)])
        if rc == 0 and "OK." in out:
            ok("--all still passes a set it fully evaluated")
        else:
            bad("--all passes a fully resolved set",
                f"rc={rc} out={out.strip()[:300]} - the fix must not fail everything")

        print("\n=== 6. A2 --all --json: the machine verdict agrees ===")
        j = tmp / "hooks_gap2.json"
        wire(j, ["python3 ~/.claude/hooks/ghost.py"])
        rc, out = run([str(AUDIT), "--all", "--hooks-json", str(j),
                       "--hooks-dir", str(gap), "--json"])
        if rc == 2:
            ok("--json exits 2 on an unresolved set (same verdict as text mode)")
        else:
            bad("--json exits 2 on an unresolved set",
                f"rc={rc} - a machine consumer would read this as clean")

        # ---- A2 on the effective-set mode (the shape seen on real hardware) --
        print("\n=== 7. A2 --settings: an unevaluated command is not a pass ===")
        good = tmp / "good-eff.py"
        good.write_text(CLEAN_HOOK, encoding="utf-8")
        missing_abs = tmp / "never-created.py"
        premise(not missing_abs.exists(), "the absolute-but-missing hook is absent")
        s_partial = tmp / "settings_partial.json"
        s_partial.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": f'python3 "{good}"'},
            {"type": "command", "command": f'python3 "{missing_abs}"'},
            {"type": "command", "command": "python3 relative/cwd-dependent.py"},
        ]}]}}), encoding="utf-8")
        rc, out = run([str(AUDIT), "--settings", str(s_partial)])
        if rc == 2:
            ok("--settings exits 2 when commands were never evaluated")
        else:
            bad("--settings exits 2 on unevaluated commands",
                f"rc={rc} - this is the exact real-hardware false OK. "
                f"out={out.strip()[:400]}")
        if "UNEVALUATED" in out:
            ok("--settings says UNEVALUATED instead of OK")
        else:
            bad("--settings says UNEVALUATED", f"out={out.strip()[:400]}")
        rc, out = run([str(AUDIT), "--settings", str(s_partial), "--porcelain"])
        if rc == 2 and out.strip().startswith("PARTIAL:"):
            ok("porcelain emits PARTIAL:<clean>:<bounded>:<skipped> for diagnose")
        else:
            bad("porcelain PARTIAL shape",
                f"rc={rc} out={out.strip()[:200]} - diagnose.sh would print a "
                f"green line over an unread fleet")

        print("\n=== 8. A2 --settings: clean stays 0, unguarded stays 1 ===")
        s_ok = tmp / "settings_ok.json"
        s_ok.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": f'python3 "{good}"'},
        ]}]}}), encoding="utf-8")
        rc, out = run([str(AUDIT), "--settings", str(s_ok), "--porcelain"])
        if rc == 0 and out.strip().startswith("OK:"):
            ok("a fully evaluated clean effective set still exits 0 / OK:")
        else:
            bad("clean effective set exits 0", f"rc={rc} out={out.strip()[:200]}")

        evil = tmp / "evil-eff.py"
        evil.write_text(EVIL_HOOK, encoding="utf-8")
        s_bad = tmp / "settings_bad.json"
        s_bad.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": f'python3 "{evil}"'},
            {"type": "command", "command": "python3 relative/cwd-dependent.py"},
        ]}]}}), encoding="utf-8")
        rc, out = run([str(AUDIT), "--settings", str(s_bad), "--porcelain"])
        if rc == 1 and out.strip().startswith("UNGUARDED:"):
            ok("a real unguarded walker still outranks the skip verdict (exit 1)")
        else:
            bad("unguarded outranks unevaluated",
                f"rc={rc} out={out.strip()[:200]} - a found freeze-class hook must "
                f"never be downgraded to 'could not evaluate'")

        # ---- consistency with the auditor whose pattern this copies ---------
        print("\n=== 9. both auditors treat 'nothing compared' the same way ===")
        fake_root = tmp / "primary"
        fake_root.mkdir()
        (fake_root / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
            {"hooks": [{"type": "command", "command": "python3 x.py"}]}]}}),
            encoding="utf-8")
        env = dict(os.environ)
        env.pop("CLAUDE_CONFIG_DIR", None)  # else it discovers a real alternate root
        p = subprocess.run(
            [sys.executable, str(ROOTS_AUDIT), "--primary", str(fake_root)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env)
        sibling = (p.returncode, (p.stdout or "") + (p.stderr or ""))
        if sibling[0] == 2 and "UNEVALUATED" in sibling[1]:
            ok("audit-guard-activation-roots.py: nothing compared -> exit 2")
        else:
            bad("sibling auditor exits 2 on an empty comparison",
                f"rc={sibling[0]} out={sibling[1].strip()[:200]} - if THIS changed, "
                f"the consistency this suite pins moved; re-derive, do not relax")

    print(f"\n=== summary: {PASSED} passed, {FAILED} failed ===")
    return 1 if FAILED else 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print
    # cannot crash the suite before it reports.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
