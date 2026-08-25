#!/usr/bin/env python3
"""
test_windows_crlf_and_list_relationship.py — stdlib-only regression tests for two
Windows-only failures that are invisible on macOS and Linux.

Run: python3 tests/test_windows_crlf_and_list_relationship.py
No pytest dependency. Exits non-zero on any failure. Touches no real vault.

Why this file exists
--------------------
1. CRLF frontmatter was denied at the write boundary.

   hooks/lint-vault-frontmatter.py matched frontmatter with ^---\\n(.*?)\\n---,
   an LF-only pattern. A vault file saved on Windows uses \\r\\n, so the pattern
   does not match and the hook takes the emit_deny branch: "'---' delimiter not
   properly closed". The frontmatter is perfectly valid — only the line endings
   differ. On the box this was measured on, 274 of 400 vault .md files were CRLF,
   so the hook blocked most Write/Edit operations on that vault.

   scripts/vault-schema-validator.py already carries the \\r?\\n tolerance
   (extract_frontmatter). The hook that calls it did not, so the write was
   refused before the validator was ever consulted.

2. A list-valued `relationship:` crashed the person extractor.

   scripts/extractors/person.py called .lower() straight on fm["relationship"].
   YAML frontmatter written as `relationship: [author, mentor]` parses to a list,
   which has no .lower(), so _is_public_figure raised AttributeError and took the
   whole extraction run with it. Both spellings are legal YAML and the schema
   does not forbid the list form.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILS: list[str] = []

VALID_DECISION_FM = (
    "type: decision\n"
    "decision_date: 2026-04-30\n"
    "stakes: high\n"
    "speed: deliberate\n"
    "floor: 16\n"
)


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILS.append(f"{name}{': ' + detail if detail else ''}")
        print(f"  FAIL {name}{': ' + detail if detail else ''}")


def run_hook(file_path: Path, content: str) -> dict:
    """Drive the PreToolUse hook exactly as Claude Code does: JSON on stdin."""
    payload = json.dumps({
        "tool_name": "Write",
        "tool_input": {"file_path": str(file_path), "content": content},
    })
    proc = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "lint-vault-frontmatter.py")],
        input=payload, capture_output=True, text=True,
    )
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"_raw": proc.stdout, "_err": proc.stderr}


def decision_of(out: dict) -> str:
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision", "")


def test_frontmatter_hook_accepts_both_line_endings() -> None:
    """LF and CRLF frontmatter are the same document; both must be allowed."""
    print("test_frontmatter_hook_accepts_both_line_endings")
    with tempfile.TemporaryDirectory() as td:
        decisions = Path(td) / "Decisions"
        decisions.mkdir(parents=True)
        target = decisions / "2026-04-30T10-00-a-decision.md"

        lf = f"---\n{VALID_DECISION_FM}---\n\n# A decision\n\nBody.\n"
        crlf = lf.replace("\n", "\r\n")

        lf_decision = decision_of(run_hook(target, lf))
        check("LF frontmatter is allowed", lf_decision == "allow", f"got {lf_decision!r}")

        crlf_decision = decision_of(run_hook(target, crlf))
        check(
            "CRLF frontmatter is allowed",
            crlf_decision == "allow",
            f"got {crlf_decision!r} — the LF-only regex denies valid Windows files",
        )


def test_public_figure_accepts_list_relationship() -> None:
    """`relationship:` is legal YAML as a scalar or a list; neither may raise."""
    print("test_public_figure_accepts_list_relationship")
    # person.py imports its sibling _base at module scope; put the package
    # directory on sys.path the same way the extractor runner does.
    sys.path.insert(0, str(ROOT / "scripts" / "extractors"))
    spec = importlib.util.spec_from_file_location(
        "person_extractor", ROOT / "scripts" / "extractors" / "person.py"
    )
    person = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(person)

    hint = next(iter(person.PUBLIC_FIGURE_RELATIONSHIP_HINTS))

    try:
        scalar = person._is_public_figure({"relationship": hint})
        check("scalar relationship still detected", scalar is True)
    except Exception as e:  # noqa: BLE001 - the point is that nothing raises
        check("scalar relationship still detected", False, f"raised {e!r}")

    try:
        as_list = person._is_public_figure({"relationship": [hint, "mentor"]})
        check("list relationship does not raise", True)
        check("list relationship is detected", as_list is True, f"got {as_list!r}")
    except Exception as e:  # noqa: BLE001
        check("list relationship does not raise", False, f"raised {e!r}")

    try:
        person._is_public_figure({"relationship": None})
        person._is_public_figure({})
        check("missing / null relationship stays safe", True)
    except Exception as e:  # noqa: BLE001
        check("missing / null relationship stays safe", False, f"raised {e!r}")


def main() -> int:
    test_frontmatter_hook_accepts_both_line_endings()
    test_public_figure_accepts_list_relationship()
    if FAILS:
        print(f"\n{len(FAILS)} failure(s):")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
