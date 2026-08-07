#!/usr/bin/env python3
"""hallucination-watch.py — aggregate fabrication signal across three substrates.

External measurement of Claude's hallucination rate. Self-introspection is
untrustworthy; the only honest signal is: did a hook catch it, did the user
correct it, did a vault grep disprove it.

Three substrates:
1. ~/.claude/hookify-blocks.log — fabrications CAUGHT before shipping
2. ⚙️ Meta/Critical Failure Inventory.md — fabrications that SHIPPED + were caught after
3. memory/feedback_*fabric*.md, *_fake_*.md, *_verbatim_*.md — codified corrections

Output: ⚙️ Meta/Hallucination Watch.md (overwrites)

Run weekly via /weekly. Standalone: `python3 ⚙️ Meta/scripts/hallucination-watch.py`.
Flags: --window-days N (default 30), --json, --quiet.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter, defaultdict

def _derive_memory_dir(vault: Path) -> Path:
    """Claude Code projects dir for a vault: ~/.claude/projects/<sanitized>/memory/

    Encoding via the shared _project_key resolver — single source of truth that
    mirrors Claude Code's exact key (every non [A-Za-z0-9-] char -> '-') with a
    glob fallback. Returns a possibly-nonexistent path; parse_memory_dir handles
    missing.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _project_key import project_dir_for
    return project_dir_for(vault) / "memory"


def _resolve_vault() -> Path:
    """VAULT_ROOT env var, or cwd if it's an Obsidian vault, else fail loud."""
    env = os.environ.get("VAULT_ROOT")
    if env:
        return Path(env)
    cwd = Path.cwd()
    if (cwd / ".obsidian").exists():
        return cwd
    raise SystemExit(
        "VAULT_ROOT not set and cwd is not an Obsidian vault. "
        "Run from inside the vault, or `export VAULT_ROOT=/path/to/vault`."
    )


VAULT = _resolve_vault()
HOOKIFY_LOG = Path(os.path.expanduser("~/.claude/hookify-blocks.log"))
CFI = VAULT / "⚙️ Meta" / "Critical Failure Inventory.md"
MEMORY_DIR = _derive_memory_dir(VAULT)
OUTPUT = VAULT / "⚙️ Meta" / "Hallucination Watch.md"

# Rule-name classifier. Order: most specific first.
# These match the [bracketed-rule-name] in hookify-blocks.log column 4.
FABRICATION_RULES = {
    "warn-fabricated-hook-attribution",
    "check-fabricated-hook-attribution",
    "warn-life-history-fabrication-risk",
    "warn-life-history-fabrication",
    "check-fabricated-panelist",
    "warn-fabricated-panelist",
    "never-fabricate-data",
    "block-personal-data-in-starter",
    "block-uncanonical-onde-number",
    "warn-out-of-scope-check",
    "warn-unread-link-in-user-message",
    "warn-public-repo-create",
    "warn-mit-on-content-repo",
    "warn-github-link-vault-audit",
    "warn-life-history-prose-fabrication",
}
VOICE_RULES = {
    "no-em-dash",
    "warn-exclamation-marks",
    "no-duplicate-h1",
    "compress-claude-docs",
    "humanizer-reminder",
}
SECURITY_RULES = {
    "block-secret-dump-command-class",
    "block-vault-git-fullwalk",
    "block-raw-vault-git",
}
# Everything else gets bucketed as "operational" (nudge-*, warn-*-detection)


def classify_rule(rule: str) -> str:
    if rule in FABRICATION_RULES:
        return "fabrication"
    if rule in VOICE_RULES:
        return "voice"
    if rule in SECURITY_RULES:
        return "security"
    return "operational"


def parse_hookify_log(window_days: int) -> dict:
    """Walk hookify-blocks.log, extract [rule-name] from each WARN/BLOCK line."""
    if not HOOKIFY_LOG.exists():
        return {"total": 0, "by_class": {}, "by_rule": {}, "recent": []}

    cutoff = datetime.now() - timedelta(days=window_days)
    rule_re = re.compile(r"\*\*\[([a-z0-9\-_]+)\]\*\*")
    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})")

    by_class: Counter = Counter()
    by_rule: Counter = Counter()
    by_class_all: Counter = Counter()  # all-time, for context
    recent_fabrications: list = []

    with HOOKIFY_LOG.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            ts_m = ts_re.match(line)
            rule_m = rule_re.search(line)
            if not ts_m or not rule_m:
                continue
            try:
                ts = datetime.strptime(
                    f"{ts_m.group(1)}T{ts_m.group(2)}", "%Y-%m-%dT%H:%M:%S"
                )
            except ValueError:
                continue
            rule = rule_m.group(1)
            kind = "BLOCK" if "\tBLOCK\t" in line else "WARN"
            cls = classify_rule(rule)
            by_class_all[cls] += 1
            if ts >= cutoff:
                by_class[cls] += 1
                by_rule[rule] += 1
                if cls == "fabrication":
                    recent_fabrications.append({
                        "ts": ts.isoformat(),
                        "rule": rule,
                        "kind": kind,
                    })

    return {
        "total_window": sum(by_class.values()),
        "total_all_time": sum(by_class_all.values()),
        "by_class": dict(by_class),
        "by_class_all_time": dict(by_class_all),
        "by_rule": dict(by_rule.most_common(20)),
        "recent_fabrications": recent_fabrications[-25:],
    }


def parse_cfi() -> dict:
    """Count rows in Critical Failure Inventory, group by surface section."""
    if not CFI.exists():
        return {"rows": 0, "by_surface": {}}

    text = CFI.read_text(encoding="utf-8", errors="replace")
    surfaces: dict = defaultdict(int)
    current = "unknown"
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            continue
        # Rows: | DATE | INCIDENT | GUARD | WHERE |
        if line.startswith("| 2026") or line.startswith("| 2025"):
            surfaces[current] += 1
    return {"rows": sum(surfaces.values()), "by_surface": dict(surfaces)}


def parse_memory_dir() -> dict:
    """Count fabrication-class feedback memory files + their slugs."""
    if not MEMORY_DIR.exists():
        return {"files": [], "count": 0}
    pat = re.compile(r"(fabric|hallucin|fake|verbatim|attribut|never_fab|fill_in)", re.I)
    files = []
    for f in sorted(MEMORY_DIR.glob("feedback_*.md")):
        if pat.search(f.name):
            files.append(f.name)
    return {"files": files, "count": len(files)}


def render_report(window_days: int, hookify: dict, cfi: dict, mem: dict) -> str:
    now = datetime.now()
    lines = []
    lines.append("---")
    lines.append(f"generated: {now.isoformat()}")
    lines.append(f"window_days: {window_days}")
    lines.append("source_script: ⚙️ Meta/scripts/hallucination-watch.py")
    lines.append("related: [[Critical Failure Inventory]], [[CLAUDE]]")
    lines.append("---")
    lines.append("")
    lines.append("# Hallucination Watch")
    lines.append("")
    lines.append(
        f"External measure of Claude's fabrication rate. Window: last "
        f"{window_days} days. Self-reported confidence is itself an LLM "
        "output; only external signal counts."
    )
    lines.append("")
    lines.append(f"_Generated {now.strftime('%Y-%m-%d %H:%M')} by `hallucination-watch.py`._")
    lines.append("")

    # Headline numbers
    fab_window = hookify["by_class"].get("fabrication", 0)
    fab_all = hookify["by_class_all_time"].get("fabrication", 0)
    lines.append("## Headline")
    lines.append("")
    lines.append(f"- **Fabrications CAUGHT by hooks ({window_days}d):** {fab_window}")
    lines.append(f"- **Fabrications CAUGHT by hooks (all-time):** {fab_all}")
    lines.append(f"- **Fabrications SHIPPED + caught after the fact (CFI total rows):** {cfi['rows']}")
    lines.append(f"- **Codified corrections (feedback memory files):** {mem['count']}")
    lines.append("")
    lines.append(
        "**Read:** hooks catch nearly-fabricated outputs before you see them; "
        "CFI rows are fabrications that got through and you corrected; memory "
        "files are the codified lessons. A rising hook-catch number with a "
        "flat CFI is GOOD (more pre-ship saves). A rising CFI is BAD (more "
        "post-ship corrections)."
    )
    lines.append("")

    # By class breakdown
    lines.append("## Hook fires by class (this window)")
    lines.append("")
    lines.append("| Class | Count | What it means |")
    lines.append("|---|---|---|")
    for cls, label in [
        ("fabrication", "True hallucination caught"),
        ("voice", "Voice/style violation"),
        ("security", "Personal data / dangerous command"),
        ("operational", "Nudge (not a failure)"),
    ]:
        n = hookify["by_class"].get(cls, 0)
        lines.append(f"| {cls} | {n} | {label} |")
    lines.append("")

    # Top rules
    if hookify["by_rule"]:
        lines.append("## Top hookify rules (this window)")
        lines.append("")
        for rule, n in list(hookify["by_rule"].items())[:15]:
            cls = classify_rule(rule)
            lines.append(f"- `{rule}` — {n}× ({cls})")
        lines.append("")

    # CFI by surface
    if cfi["by_surface"]:
        lines.append("## Critical Failure Inventory (all-time, by surface)")
        lines.append("")
        for surface, n in sorted(cfi["by_surface"].items(), key=lambda x: -x[1]):
            lines.append(f"- **{surface}** — {n} incidents")
        lines.append("")

    # Recent fabrication hits
    if hookify["recent_fabrications"]:
        lines.append("## Recent fabrication-class hook fires")
        lines.append("")
        lines.append("| Timestamp | Kind | Rule |")
        lines.append("|---|---|---|")
        for ev in hookify["recent_fabrications"][-15:]:
            lines.append(f"| {ev['ts']} | {ev['kind']} | `{ev['rule']}` |")
        lines.append("")

    # Memory files
    if mem["files"]:
        lines.append("## Fabrication-class feedback memories")
        lines.append("")
        for f in mem["files"]:
            lines.append(f"- `{f}`")
        lines.append("")

    lines.append("## External baseline")
    lines.append("")
    lines.append(
        "Microsoft DELEGATE-52 ([arxiv 2604.15597](https://arxiv.org/abs/2604.15597), Apr 2026): "
        "frontier models corrupt ~25% of professional content over 20 edits. "
        "Agentic tools add ~6% additional degradation. The Critical Failure "
        "Inventory is this vault's verification harness — the only mitigation "
        "the paper identifies."
    )
    lines.append("")
    lines.append("## How to improve the signal")
    lines.append("")
    lines.append(
        "- **Add hook rules** for any fabrication pattern that recurs 2+ times "
        "and doesn't already have a guard. The `nudge-skillify-on-recurring-issue` "
        "hook surfaces these."
    )
    lines.append(
        "- **File CFI rows** when a fabrication gets through. Pattern: `feedback_permanent_fix_pattern.md`."
    )
    lines.append(
        "- **Sample audit (future):** pick N random factual claims per "
        "session, have a separate Claude session verify each against vault. "
        "Score: claims_verified / claims_sampled. Wire into `/weekly`."
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    hookify = parse_hookify_log(args.window_days)
    cfi = parse_cfi()
    mem = parse_memory_dir()

    if args.json:
        payload = {"window_days": args.window_days, "hookify": hookify, "cfi": cfi, "memory": mem}
        print(json.dumps(payload, indent=2))
        return 0

    report = render_report(args.window_days, hookify, cfi, mem)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(report, encoding="utf-8")

    if not args.quiet:
        fab_window = hookify["by_class"].get("fabrication", 0)
        fab_all = hookify["by_class_all_time"].get("fabrication", 0)
        print(f"wrote {OUTPUT}")
        print(f"fabrication hook fires ({args.window_days}d): {fab_window}")
        print(f"fabrication hook fires (all-time): {fab_all}")
        print(f"CFI rows (all-time): {cfi['rows']}")
        print(f"fabrication-class memory files: {mem['count']}")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
