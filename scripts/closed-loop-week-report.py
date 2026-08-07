#!/usr/bin/env python3
"""
closed-loop-week-report.py — Weekly visibility into the episodic→procedural
memory loop.

The closed-loop infrastructure (promote-episodic-to-procedural,
demote-stale-procedural, resolver-build) runs on cron and updates files
silently. This script surfaces what changed in the last N days so the
human gets a chance to ratify, sanity-check, or reverse decisions the
loop made automatically.

Designed for invocation from /sunday-review (Step 4f) but also runnable
standalone.

Output is a markdown block printed to stdout. Five sections:
  1. Promotion candidates pending review
  2. Files promoted to Workflows/Exceptions this week
  3. Files demoted (status: superseded) this week
  4. Resolver conflicts (read from RESOLVER.md)
  5. One-line ratification prompt

Stdlib only.

Usage:
    python3 closed-loop-week-report.py --vault-root /path/to/vault
    python3 closed-loop-week-report.py --vault-root /path/to/vault --days 7
"""

from __future__ import annotations
import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path


def git_log_since_files(vault_root: Path, since_days: int, subdirs: list[str]) -> list[str]:
    """Return relative paths of files inside `subdirs` that have git activity
    in the last `since_days` days."""
    if not (vault_root / ".git").exists():
        return []
    args = [
        "git", "-c", "core.quotepath=off", "log",
        f"--since={since_days}.days.ago",
        "--name-only",
        "--pretty=format:",
        "--",
    ] + subdirs
    try:
        r = subprocess.run(
            args, cwd=str(vault_root), capture_output=True, text=True,
            check=False, timeout=30,
        )
    except subprocess.SubprocessError:
        return []
    seen = set()
    out = []
    for line in r.stdout.splitlines():
        s = line.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def files_with_superseded_status_added(vault_root: Path, since_days: int) -> list[str]:
    """Find files where a `status: superseded` line was added to frontmatter
    in the last N days. Uses git log -G to detect the change in patches."""
    if not (vault_root / ".git").exists():
        return []
    args = [
        "git", "-c", "core.quotepath=off", "log",
        f"--since={since_days}.days.ago",
        "-G", r"^status:\s*superseded",
        "--name-only",
        "--pretty=format:",
        "--",
        "Meta/Decisions/",
        "Meta/Workflows/",
        "Meta/Exceptions/",
        "Meta/Facts/",
    ]
    try:
        r = subprocess.run(
            args, cwd=str(vault_root), capture_output=True, text=True,
            check=False, timeout=30,
        )
    except subprocess.SubprocessError:
        return []
    seen = set()
    out = []
    for line in r.stdout.splitlines():
        s = line.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def candidate_files(vault_root: Path, since_days: int) -> list[Path]:
    """Files in Meta/Promotion-Candidates/ modified in the last N days."""
    folder = vault_root / "Meta" / "Promotion-Candidates"
    if not folder.exists():
        return []
    cutoff = datetime.now().timestamp() - since_days * 86400
    out = []
    for p in folder.iterdir():
        if p.suffix != ".md":
            continue
        try:
            if p.stat().st_mtime >= cutoff:
                out.append(p)
        except OSError:
            continue
    return sorted(out, key=lambda p: -p.stat().st_mtime)


def resolver_conflict_count(vault_root: Path) -> int | None:
    """Read conflict count from Meta/RESOLVER.md frontmatter if present."""
    for rel in ["Meta/RESOLVER.md", "⚙️ Meta/RESOLVER.md"]:
        p = vault_root / rel
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r"^conflict_count:\s*(\d+)\s*$", text, re.MULTILINE)
        if m:
            return int(m.group(1))
        return 0
    return None


def render(vault_root: Path, since_days: int) -> str:
    candidates = candidate_files(vault_root, since_days)
    promoted = git_log_since_files(
        vault_root, since_days,
        ["Meta/Workflows/", "Meta/Exceptions/", "Meta/Facts/"],
    )
    demoted = files_with_superseded_status_added(vault_root, since_days)
    conflicts = resolver_conflict_count(vault_root)

    lines: list[str] = []
    lines.append(f"## Closed-loop activity (last {since_days} days)")
    lines.append("")

    # 1. Pending review
    lines.append(f"### Pending your review: {len(candidates)} promotion candidate(s)")
    if candidates:
        for c in candidates[:10]:
            rel = c.relative_to(vault_root)
            lines.append(f"- [[{c.stem}]] — `{rel}`")
        if len(candidates) > 10:
            lines.append(f"- _...{len(candidates) - 10} more in `Meta/Promotion-Candidates/`._")
    else:
        lines.append("_No new candidates this week._")
    lines.append("")

    # 2. Promoted
    lines.append(f"### Promoted to procedural memory: {len(promoted)} file(s)")
    if promoted:
        for f in promoted[:10]:
            stem = Path(f).stem
            lines.append(f"- [[{stem}]] — `{f}`")
        if len(promoted) > 10:
            lines.append(f"- _...{len(promoted) - 10} more (see git log)._")
    else:
        lines.append("_Nothing crossed the promotion threshold this week._")
    lines.append("")

    # 3. Demoted / superseded
    lines.append(f"### Demoted (status: superseded): {len(demoted)} file(s)")
    if demoted:
        for f in demoted[:10]:
            stem = Path(f).stem
            lines.append(f"- [[{stem}]] — `{f}`")
        if len(demoted) > 10:
            lines.append(f"- _...{len(demoted) - 10} more._")
    else:
        lines.append("_No rules superseded this week._")
    lines.append("")

    # 4. Resolver conflicts
    if conflicts is not None:
        lines.append(f"### Resolver conflicts: {conflicts}")
        if conflicts > 0:
            lines.append(f"See [[RESOLVER]] for the conflict groups. Each conflict needs a winner ratified by date or hand.")
        lines.append("")

    # 5. Ratification prompt
    total_human_attention = len(candidates) + len(demoted)
    if total_human_attention > 0:
        lines.append("### One thing to do")
        if candidates:
            lines.append(f"Review the {len(candidates)} pending promotion candidate(s) in `Meta/Promotion-Candidates/`. For each: edit into shape, then move to `Meta/Workflows/` or `Meta/Exceptions/` (or delete if it's noise).")
        elif demoted:
            lines.append(f"Sanity-check the {len(demoted)} rule(s) demoted this week. If any auto-supersession was wrong, restore them with a fresh `last_verified` date.")
    else:
        lines.append("### One thing to do")
        lines.append("_Nothing requires your ratification this week. The closed-loop made no automatic decisions._")
    lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly closed-loop visibility report.")
    parser.add_argument("--vault-root", required=True, help="Path to vault root.")
    parser.add_argument("--days", type=int, default=7, help="Look-back window in days. Default: 7.")
    args = parser.parse_args()

    vault_root = Path(args.vault_root).expanduser().resolve()
    if not vault_root.exists():
        print(f"Vault root does not exist: {vault_root}", file=sys.stderr)
        return 2

    print(render(vault_root, args.days))
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
