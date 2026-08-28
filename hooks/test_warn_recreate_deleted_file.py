#!/usr/bin/env python3
"""Tests for warn-recreate-deleted-file.py (Reliability Manifesto Pillar 6 enforcement).

Self-contained: builds throwaway git repos in a temp dir, drives the hook via
subprocess with realistic PreToolUse(Write) payloads, asserts on stdout JSON.
Run: python3 hooks/test_warn_recreate_deleted_file.py   (exit 0 = all pass)
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = str(Path(__file__).with_name("warn-recreate-deleted-file.py"))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True,
                   encoding="utf-8", errors="replace")


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "root")


def run(payload: dict) -> dict:
    r = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, f"hook must always exit 0, got {r.returncode}: {r.stderr}"
    out = r.stdout.strip()
    return json.loads(out) if out else {}


def fired(payload: dict) -> bool:
    """A warning fired iff stdout carries non-empty additionalContext."""
    return bool(run(payload).get("additionalContext"))


def main() -> int:
    passed = failed = 0
    cases = []

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # Repo A: a file was committed then DELETED in a later commit.
        repo = tmp / "repo"
        _init_repo(repo)
        f = repo / "orphan.py"
        f.write_text("print('hi')\n", encoding="utf-8")
        _git(repo, "add", "orphan.py")
        _git(repo, "commit", "-q", "-m", "add orphan")
        f.unlink()
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "cleanup: delete orphan.py as stale")

        # Case 1: recreating the deleted file at its old path → MUST fire.
        cases.append(("recreate deleted file fires", True, fired({
            "tool_name": "Write",
            "tool_input": {"file_path": str(repo / "orphan.py"), "content": "x"},
        })))

        # Case 2: a brand-new file never in history → MUST NOT fire.
        cases.append(("new file silent", False, fired({
            "tool_name": "Write",
            "tool_input": {"file_path": str(repo / "brand_new.py"), "content": "x"},
        })))

        # Case 3: editing/overwriting a file that currently EXISTS → MUST NOT fire.
        live = repo / "live.py"
        live.write_text("v1\n", encoding="utf-8")
        _git(repo, "add", "live.py")
        _git(repo, "commit", "-q", "-m", "add live")
        cases.append(("overwrite live file silent", False, fired({
            "tool_name": "Write",
            "tool_input": {"file_path": str(live), "content": "v2"},
        })))

        # Case 4: non-Write tool (Edit) → MUST NOT fire (Edit targets existing files).
        cases.append(("edit tool silent", False, fired({
            "tool_name": "Edit",
            "tool_input": {"file_path": str(repo / "orphan.py"), "new_string": "x"},
        })))

        # Case 5: path outside any git repo → MUST NOT fire (no history to consult).
        outside = tmp / "no_repo" / "f.py"
        outside.parent.mkdir(parents=True, exist_ok=True)
        cases.append(("non-repo path silent", False, fired({
            "tool_name": "Write",
            "tool_input": {"file_path": str(outside), "content": "x"},
        })))

        # Case 6: malformed payload → MUST NOT crash, MUST NOT fire.
        cases.append(("malformed silent", False, fired({"tool_name": "Write"})))

        # Case 7: the deletion-commit subject appears in the warning (actionable evidence).
        warn = run({
            "tool_name": "Write",
            "tool_input": {"file_path": str(repo / "orphan.py"), "content": "x"},
        }).get("additionalContext", "")
        cases.append(("warning cites deletion commit", True,
                      "cleanup: delete orphan.py" in warn))

    for name, want, got in cases:
        ok = want == got
        print(f"  {'✓' if ok else '✗'} {name}  want={want} got={got}")
        if ok:
            passed += 1
        else:
            failed += 1

    print(f"\n{passed}/{passed+failed} pass")
    return 1 if failed else 0


if __name__ == "__main__":
    # Windows cp1252-console safety (ai-brain-starter#313; hooks/ sweep #314).
    # A hook that print()s non-ASCII raises UnicodeEncodeError on a cp1252
    # console: the gate then fails silently OPEN, or denies the tool call with
    # no legible cause. Idempotent; a no-op on an already-UTF-8 console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
