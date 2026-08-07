#!/usr/bin/env python3
"""check-context-load.py — MYC-630: automated first-run context-load self-test.

Phase 19's "ask me what you know about me" test relies on the user launching
Claude from the *right* working directory. Claude Code loads a vault's CLAUDE.md
by walking UP from the launch cwd to the filesystem root — so a vault launched
from the wrong folder loads only the global ~/.claude/CLAUDE.md, answers
generically, and the user concludes "this doesn't work" and churns. Nothing
in the install actually proved the personalized context would load.

This script is that proof. It does NOT just re-check that context files exist
(diagnose.sh and context-audit.py already do existence). It simulates Claude
Code's CLAUDE.md ancestor-walk and answers one deterministic question:

    "If Claude is launched from <launch-dir>, will the vault's personalized
     context load on the first run?"

It also emits the exact `cd "<vault>" && claude` command, so the install can
tell the user precisely where to launch instead of a vague "your vault folder."

Verdicts (--porcelain prints the single most-severe token):
  OK_WILL_LOAD               personalized CLAUDE.md will load from launch-dir
  FAIL_NO_CLAUDE_MD          no CLAUDE.md at the vault root
  FAIL_TEMPLATE_UNFILLED:<d> CLAUDE.md is the unfilled template or a stub
                             (d = "stub" or the placeholder count)
  FAIL_WRONG_CWD:<dir>       launch-dir is not inside the vault, so the vault
                             CLAUDE.md is NOT on the cwd->root path (won't load)
  WARN_MISSING_CONTEXT:<n>   CLAUDE.md references session-start files that are
                             absent (loads, but the first answer will be thin)

Exit codes (match diagnose.sh): 0 = will load, 1 = will NOT load (a FAIL_*
verdict — the install gate should not declare "done"), 2 = loads with warnings.

Usage:
  python3 check-context-load.py [VAULT] [--launch-dir DIR] [--porcelain|--json]

VAULT defaults to $VAULT_PATH or the current directory. --launch-dir defaults to
VAULT (the recommended launch dir); pass the live session cwd to catch a vault
that is being run from the wrong folder right now.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Minimum size below which a CLAUDE.md is treated as a stub, not a real
# personalized memory file. Matches diagnose.sh's "tiny" threshold.
MIN_PERSONALIZED_BYTES = 200

# Substrings that only ever appear in the UNFILLED claude-md template
# (templates/generated/claude-md-template.md) — a real personalized CLAUDE.md
# substitutes every one of them. Any survivor means the install wrote the
# template but never filled it in. Curated to be zero-false-positive against a
# genuinely filled file.
TEMPLATE_PLACEHOLDERS = (
    "[Name]. [What they do]",
    "[What they do]",
    "[Key context from their answers",
    "[Priority 1",
    "[Priority 2]",
    "[Priority 3]",
    "[from their answer]",
    "[who they are]",
    "[FILL THIS IN",
    "[VAULT_PATH]",
    "[TEAM_VAULT_PATH]",
    "{{CUSTOMIZE}}",
    "{{VAULT_ROOT}}",
    "<your name>",
    "PRIMARY_LANGUAGE",
)

# Canonical session-start context files the substrate's CLAUDE.md tells Claude
# to read on every session. Checked by BASENAME and only when CLAUDE.md actually
# references them — so a vault whose CLAUDE.md never mentions a file is never
# faulted for it (portable across vault layouts).
CANONICAL_CONTEXT_FILES = (
    "Last Session.md",
    "Current Priorities.md",
    "About Me.md",
)


def _resolve(p: Path) -> Path:
    try:
        return p.resolve()
    except OSError:
        return p.absolute()


def _is_within(child: Path, parent: Path) -> bool:
    """True if `child` is `parent` or a descendant — i.e. `parent` lies on the
    cwd->root walk from `child`, which is exactly when Claude Code loads
    parent/CLAUDE.md."""
    child, parent = _resolve(child), _resolve(parent)
    return child == parent or parent in child.parents


def _find_basename(vault: Path, basename: str) -> bool:
    """True if a file with this basename exists in the vault. Layout-agnostic
    (works whether About Me lives in '🏠 Home/' or elsewhere) but shallow-first:
    the canonical context files live at depth 1-2 ('⚙️ Meta/…', '🏠 Home/…'),
    so check those before falling back to a full walk — a mature vault can hold
    10k+ files and the repo's own rule forbids gratuitous full-tree walks."""
    # Depth 1 (vault root) then depth 2 (one level down) — covers every real
    # layout in O(top-level entries) without descending into .git.
    if (vault / basename).is_file():
        return True
    if any(p.is_file() for p in vault.glob("*/" + basename)):
        return True
    # Fallback: deeper nesting. Prune .git so the walk can't blow up on it.
    for f in vault.rglob(basename):
        if ".git" in f.parts:
            continue
        if f.is_file():
            return True
    return False


def _ancestor_claude_mds(vault: Path) -> list[Path]:
    """CLAUDE.md files in STRICT ancestors of the vault. If the user launches
    from one of these, that file loads instead of (or merged ahead of) the
    vault's. Informational only — it never flips the verdict, because launching
    from the vault itself still loads the vault CLAUDE.md."""
    found: list[Path] = []
    for anc in _resolve(vault).parents:
        cm = anc / "CLAUDE.md"
        if cm.is_file():
            found.append(cm)
    return found


def evaluate(vault: Path, launch_dir: Path) -> dict:
    """Run every check and return a structured result. The porcelain verdict is
    the single most-severe finding; `checks` carries the full picture."""
    vault = _resolve(vault)
    launch_dir = _resolve(launch_dir)
    checks: list[dict] = []
    claude_md = vault / "CLAUDE.md"
    launch_command = 'cd "%s" && claude' % vault

    # --- Check 1: CLAUDE.md exists at the vault root -------------------------
    if not claude_md.is_file():
        checks.append({"name": "CLAUDE.md present", "status": "fail",
                       "detail": "no CLAUDE.md at vault root %s" % vault})
        return {"verdict": "FAIL_NO_CLAUDE_MD", "exit": 1,
                "vault": str(vault), "launch_dir": str(launch_dir),
                "launch_command": launch_command, "checks": checks}
    text = claude_md.read_text(encoding="utf-8", errors="replace")
    size = len(text.encode("utf-8"))
    checks.append({"name": "CLAUDE.md present", "status": "pass",
                   "detail": "%d bytes" % size})

    # --- Check 2: it is personalized, not the unfilled template / a stub -----
    placeholders = [p for p in TEMPLATE_PLACEHOLDERS if p in text]
    if size < MIN_PERSONALIZED_BYTES:
        checks.append({"name": "CLAUDE.md personalized", "status": "fail",
                       "detail": "stub: %d bytes (< %d)" % (size, MIN_PERSONALIZED_BYTES)})
        return {"verdict": "FAIL_TEMPLATE_UNFILLED:stub", "exit": 1,
                "vault": str(vault), "launch_dir": str(launch_dir),
                "launch_command": launch_command, "checks": checks}
    if placeholders:
        checks.append({"name": "CLAUDE.md personalized", "status": "fail",
                       "detail": "%d template placeholder(s) left unfilled: %s"
                       % (len(placeholders), ", ".join(placeholders[:4]))})
        return {"verdict": "FAIL_TEMPLATE_UNFILLED:%d" % len(placeholders),
                "exit": 1, "vault": str(vault), "launch_dir": str(launch_dir),
                "launch_command": launch_command, "checks": checks}
    checks.append({"name": "CLAUDE.md personalized", "status": "pass",
                   "detail": "no template placeholders"})

    # --- Check 3: the load path — is the vault on the launch cwd's walk? -----
    if not _is_within(launch_dir, vault):
        checks.append({"name": "Launch cwd loads the vault", "status": "fail",
                       "detail": "launch dir %s is outside the vault; the vault "
                       "CLAUDE.md is not on its cwd->root path and will not load"
                       % launch_dir})
        return {"verdict": "FAIL_WRONG_CWD:%s" % launch_dir.name, "exit": 1,
                "vault": str(vault), "launch_dir": str(launch_dir),
                "launch_command": launch_command, "checks": checks}
    checks.append({"name": "Launch cwd loads the vault", "status": "pass",
                   "detail": "launch dir is the vault or a descendant"})

    # --- Soft: a CLAUDE.md in an ancestor could shadow on a wrong-cwd launch -
    shadows = _ancestor_claude_mds(vault)
    if shadows:
        checks.append({"name": "Ancestor CLAUDE.md", "status": "info",
                       "detail": "non-vault CLAUDE.md above the vault: %s "
                       "(loads if launched from there)"
                       % ", ".join(str(s) for s in shadows)})

    # --- Check 4 (soft): referenced session-start context files resolve ------
    referenced = [c for c in CANONICAL_CONTEXT_FILES if c in text]
    missing = [c for c in referenced if not _find_basename(vault, c)]
    if missing:
        checks.append({"name": "Context layer resolves", "status": "warn",
                       "detail": "CLAUDE.md references but the vault is missing: "
                       + ", ".join(missing)})
        return {"verdict": "WARN_MISSING_CONTEXT:%d" % len(missing), "exit": 2,
                "vault": str(vault), "launch_dir": str(launch_dir),
                "launch_command": launch_command, "checks": checks,
                "missing_context": missing}
    checks.append({"name": "Context layer resolves", "status": "pass",
                   "detail": "%d referenced session-start file(s) present"
                   % len(referenced)})

    return {"verdict": "OK_WILL_LOAD", "exit": 0, "vault": str(vault),
            "launch_dir": str(launch_dir), "launch_command": launch_command,
            "checks": checks}


def _print_human(result: dict) -> None:
    icons = {"pass": "✅", "fail": "❌", "warn": "⚠️", "info": "ℹ️"}
    print("\n=== First-run context-load self-test ===\n")
    print("Vault:      %s" % result["vault"])
    print("Launch dir: %s\n" % result["launch_dir"])
    for c in result["checks"]:
        line = "  %s %s" % (icons.get(c["status"], "•"), c["name"])
        if c.get("detail"):
            line += "  —  %s" % c["detail"]
        print(line)
    verdict = result["verdict"]
    print()
    if result["exit"] == 0:
        print("Context WILL load. Launch the vault with:")
        print("    %s" % result["launch_command"])
    elif result["exit"] == 2:
        print("Context will load, with warnings (%s)." % verdict)
        print("Launch the vault with:")
        print("    %s" % result["launch_command"])
    else:
        print("Context will NOT load on first run (%s)." % verdict)
        print("Fix the failure above, then launch the vault with:")
        print("    %s" % result["launch_command"])
    print()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Prove the vault's personalized context will load on first run")
    parser.add_argument("vault", nargs="?",
                        default=os.environ.get("VAULT_PATH", os.getcwd()),
                        help="vault root (default: $VAULT_PATH or cwd)")
    parser.add_argument("--launch-dir", default=None,
                        help="directory Claude would launch from "
                             "(default: the vault; pass the live cwd to catch a "
                             "wrong-folder launch)")
    parser.add_argument("--porcelain", action="store_true",
                        help="print only the single most-severe verdict token")
    parser.add_argument("--json", action="store_true",
                        help="print the full structured result as JSON")
    args = parser.parse_args(argv)

    vault = Path(args.vault)
    launch_dir = Path(args.launch_dir) if args.launch_dir else vault
    result = evaluate(vault, launch_dir)

    if args.json:
        print(json.dumps(result, indent=2))
    elif args.porcelain:
        print(result["verdict"])
    else:
        _print_human(result)
    return result["exit"]


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main(sys.argv[1:]))
