#!/usr/bin/env python3
"""
context-audit.py -- health check for the vault's context optimization setup.

Checks file sizes, aggregator markers, stale memories, zombie worktrees,
rules completeness, and CLAUDE.md sync status.

Usage:
  python3 context-audit.py          # human-readable terminal output
  python3 context-audit.py --json   # machine-readable JSON output

Auto-detects VAULT_ROOT from script location (same pattern as
aggregate-sessions.py: script lives in Meta/scripts/, vault root is
2 levels up). Override with VAULT_ROOT env var.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", str(_SCRIPT_DIR.parent.parent)))
META_DIR = VAULT_ROOT / "\u2699\ufe0f Meta"
RULES_DIR = META_DIR / "rules"
LAST_SESSION = META_DIR / "Last Session.md"
ROOT_CLAUDE_MD = VAULT_ROOT / "CLAUDE.md"

# Memory directory for the vault project — auto-detect from vault path
# Claude Code encodes the vault path as the project directory name
# Claude Code encodes paths: / → -, spaces → -, leading dash kept
_vault_path_encoded = str(VAULT_ROOT).replace("/", "-").replace(" ", "-")
MEMORY_DIR = Path.home() / ".claude" / "projects" / _vault_path_encoded / "memory"

AGGREGATOR_MARKER = "<!-- aggregate-sessions:BEGIN -->"

# Expected rules files
REQUIRED_RULES = [
    "obsidian.md",
    "graphify.md",
    "tool-routing.md",
    "efficiency.md",
    "advisory-panel.md",
    "meeting-workflow.md",
    "session-end-cascade.md",
    "session-start-checks.md",
]

# File size caps (bytes)
SIZE_CAPS: dict[str, tuple[Path, int]] = {
    "CLAUDE.md (vault root)": (ROOT_CLAUDE_MD, 10_000),
    "\U0001f4d3 Journals/CLAUDE.md": (
        VAULT_ROOT / "\U0001f4d3 Journals" / "CLAUDE.md", 15_000
    ),
    "\u270d\ufe0f Writing/CLAUDE.md": (
        VAULT_ROOT / "\u270d\ufe0f Writing" / "CLAUDE.md", 8_000
    ),
    "\u2699\ufe0f Meta/Last Session.md": (LAST_SESSION, 15_000),
}

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

class AuditResult:
    """Collects pass/warn outcomes for each check."""

    def __init__(self) -> None:
        self.checks: list[dict] = []

    def passed(self, name: str, detail: str = "") -> None:
        self.checks.append({"name": name, "status": "pass", "detail": detail})

    def warn(self, name: str, detail: str) -> None:
        self.checks.append({"name": name, "status": "warn", "detail": detail})

    @property
    def pass_count(self) -> int:
        return sum(1 for c in self.checks if c["status"] == "pass")

    @property
    def warn_count(self) -> int:
        return sum(1 for c in self.checks if c["status"] == "warn")

    @property
    def total(self) -> int:
        return len(self.checks)

    def summary_line(self) -> str:
        return (
            f"{self.pass_count}/{self.total} checks passed"
            + (f", {self.warn_count} warning(s)" if self.warn_count else "")
        )

    def print_human(self) -> None:
        print("\n=== Context Optimization Audit ===\n")
        for c in self.checks:
            icon = "\u2705" if c["status"] == "pass" else "\u26a0\ufe0f"
            line = f"  {icon} {c['name']}"
            if c["detail"]:
                line += f"  --  {c['detail']}"
            print(line)
        print(f"\n{self.summary_line()}\n")

    def to_json(self) -> str:
        return json.dumps(
            {"checks": self.checks, "summary": self.summary_line()},
            indent=2,
        )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_file_sizes(result: AuditResult) -> None:
    """Check tracked files against their size caps."""
    warnings: list[str] = []

    for label, (path, cap) in SIZE_CAPS.items():
        if not path.exists():
            continue
        size = path.stat().st_size
        if size > cap:
            kb = size / 1024
            cap_kb = cap / 1024
            warnings.append(f"{label}: {kb:.1f}KB (cap {cap_kb:.0f}KB)")

    # Rules directory: each file capped at 20KB
    if RULES_DIR.exists():
        for f in sorted(RULES_DIR.glob("*.md")):
            size = f.stat().st_size
            if size > 20_000:
                kb = size / 1024
                warnings.append(f"rules/{f.name}: {kb:.1f}KB (cap 20KB)")

    if warnings:
        result.warn("File sizes", "; ".join(warnings))
    else:
        result.passed("File sizes", "all tracked files within caps")


def check_aggregator_health(result: AuditResult) -> None:
    """Check Last Session.md for duplicate aggregator markers."""
    if not LAST_SESSION.exists():
        result.warn("Aggregator health", "Last Session.md does not exist")
        return

    content = LAST_SESSION.read_text(encoding="utf-8")
    count = content.count(AGGREGATOR_MARKER)

    if count == 1:
        result.passed("Aggregator health", "exactly 1 BEGIN marker")
    elif count == 0:
        result.warn("Aggregator health", "no aggregator BEGIN marker found")
    else:
        result.warn(
            "Aggregator health",
            f"found {count} BEGIN markers (expected 1, possible duplicate bug)",
        )


def check_stale_memories(result: AuditResult) -> None:
    """List memory files referencing dates more than 30 days old."""
    if not MEMORY_DIR.exists():
        result.warn("Stale memories", f"memory dir not found: {MEMORY_DIR}")
        return

    cutoff = dt.date.today() - dt.timedelta(days=30)
    # Build a pattern for YYYY-MM or YYYY-MM-DD that falls before the cutoff
    # We check year-month combos that are definitely older than cutoff
    stale_files: list[str] = []
    date_pattern = re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])(?:-(0[1-9]|[12]\d|3[01]))?\b")

    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue

        for m in date_pattern.finditer(content):
            year = int(m.group(1))
            month = int(m.group(2))
            day = int(m.group(3)) if m.group(3) else 1
            try:
                found_date = dt.date(year, month, day)
            except ValueError:
                continue
            if found_date < cutoff:
                stale_files.append(f.name)
                break

    if stale_files:
        result.warn(
            "Stale memories",
            f"{len(stale_files)} file(s) reference dates >30 days old: "
            + ", ".join(stale_files[:5])
            + ("..." if len(stale_files) > 5 else ""),
        )
    else:
        result.passed("Stale memories", "no files reference dates >30 days old")


def check_zombie_worktrees(result: AuditResult) -> None:
    """Count git worktrees and warn if more than 5."""
    try:
        out = subprocess.run(
            ["git", "worktree", "list"],
            capture_output=True,
            text=True,
            cwd=str(VAULT_ROOT),
            timeout=10,
        )
        if out.returncode != 0:
            result.warn("Zombie worktrees", f"git worktree list failed: {out.stderr.strip()}")
            return
        lines = [l for l in out.stdout.strip().splitlines() if l.strip()]
        count = len(lines)
        if count > 5:
            result.warn("Zombie worktrees", f"{count} worktrees active (threshold: 5)")
        else:
            result.passed("Zombie worktrees", f"{count} worktree(s) active")
    except FileNotFoundError:
        result.warn("Zombie worktrees", "git not found on PATH")
    except subprocess.TimeoutExpired:
        result.warn("Zombie worktrees", "git worktree list timed out")


def check_rules_completeness(result: AuditResult) -> None:
    """Verify all expected rules files exist."""
    missing: list[str] = []
    for name in REQUIRED_RULES:
        if not (RULES_DIR / name).exists():
            missing.append(name)

    if missing:
        result.warn("Rules completeness", f"missing: {', '.join(missing)}")
    else:
        result.passed(
            "Rules completeness",
            f"all {len(REQUIRED_RULES)} required rules files present",
        )


def check_claude_md_sync(result: AuditResult) -> None:
    """Verify root CLAUDE.md references all rules files."""
    if not ROOT_CLAUDE_MD.exists():
        result.warn("CLAUDE.md sync", "root CLAUDE.md does not exist")
        return

    content = ROOT_CLAUDE_MD.read_text(encoding="utf-8")
    unreferenced: list[str] = []
    for name in REQUIRED_RULES:
        if name not in content:
            unreferenced.append(name)

    if unreferenced:
        result.warn(
            "CLAUDE.md sync",
            f"root CLAUDE.md does not reference: {', '.join(unreferenced)}",
        )
    else:
        result.passed(
            "CLAUDE.md sync",
            "root CLAUDE.md references all rules files",
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_audit() -> AuditResult:
    """Run all checks and return the result object."""
    result = AuditResult()
    check_file_sizes(result)
    check_aggregator_health(result)
    check_stale_memories(result)
    check_zombie_worktrees(result)
    check_rules_completeness(result)
    check_claude_md_sync(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Health check for vault context optimization setup"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON instead of terminal formatting",
    )
    args = parser.parse_args()

    result = run_audit()

    if args.json:
        print(result.to_json())
    else:
        result.print_human()

    return 1 if result.warn_count > 0 else 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
