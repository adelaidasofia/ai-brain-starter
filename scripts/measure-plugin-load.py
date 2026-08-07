#!/usr/bin/env python3
"""measure-plugin-load.py — Plugin always-on token-cost measurement.

Loops `claude plugin details` over every installed plugin in parallel,
parses projected always-on tokens + component counts, sums totals, and
flags plugins exceeding a threshold per the token-economics decision tree.

Enforces the rule documented at `docs/token-economics.md`:
- ≤500 tok always-on → keep global
- >500 tok + used ≥ weekly → keep global
- >500 tok + used < weekly → disable + document per-session re-enable

Usage:
    python3 measure-plugin-load.py [--threshold N] [--report PATH] [--json]

Example for quarterly review:
    python3 scripts/measure-plugin-load.py --report plugin-token-report.md
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor


def list_plugins() -> list[str]:
    """Return the list of installed plugin bare names from `claude plugin list --json`.

    Defensive: handles multiple plausible JSON shapes the CLI might return.
    """
    try:
        r = subprocess.run(
            ["claude", "plugin", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        sys.stderr.write(f"could not run `claude plugin list --json`: {e}\n")
        return []
    if r.returncode != 0:
        sys.stderr.write(
            f"`claude plugin list --json` failed (rc={r.returncode}): "
            f"{r.stderr[:200]}\n"
        )
        return []
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        sys.stderr.write("could not parse JSON from `claude plugin list --json`\n")
        return []

    # Try common shapes: {"plugins": [...]}, [...], or {name: {...}, ...}
    if isinstance(data, dict) and "plugins" in data:
        items = data["plugins"]
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = list(data.keys())
    else:
        items = []

    names: list[str] = []
    for item in items:
        if isinstance(item, dict):
            name = item.get("name") or item.get("plugin") or item.get("id")
            if name:
                names.append(str(name).split("@")[0])
        elif isinstance(item, str):
            names.append(item.split("@")[0])
    return sorted(set(names))


def get_details(name: str) -> str:
    """Run `claude plugin details NAME`, return stdout (empty string on failure)."""
    try:
        r = subprocess.run(
            ["claude", "plugin", "details", name],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def parse_count(text: str, kind: str) -> int:
    m = re.search(rf"{kind}\s*\((\d+)\)", text)
    return int(m.group(1)) if m else 0


def parse_alwayson(text: str) -> int:
    m = re.search(r"Always-on:\s*~?([\d,]+)\s*tok", text)
    return int(m.group(1).replace(",", "")) if m else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--threshold",
        type=int,
        default=500,
        help="Flag plugins with always-on cost >N tok (default: 500).",
    )
    ap.add_argument(
        "--report",
        default=None,
        help="Write Markdown report to PATH (in addition to stdout).",
    )
    ap.add_argument("--json", action="store_true", help="Output JSON instead of table.")
    args = ap.parse_args()

    names = list_plugins()
    if not names:
        sys.stderr.write("no plugins found.\n")
        return 1

    sys.stderr.write(f"measuring {len(names)} plugins (parallel)...\n")
    out: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for name, text in zip(names, ex.map(get_details, names)):
            out[name] = text

    rows: list[dict] = []
    tot_ao = 0
    tot_sk = tot_ag = tot_hk = tot_mc = 0
    for name in names:
        t = out[name]
        if not t.strip():
            rows.append(
                {
                    "name": name,
                    "skills": None,
                    "agents": None,
                    "hooks": None,
                    "mcp": None,
                    "always_on": None,
                    "status": "no_data",
                }
            )
            continue
        sk = parse_count(t, "Skills")
        ag = parse_count(t, "Agents")
        hk = parse_count(t, "Hooks")
        mc = parse_count(t, "MCP servers")
        ao = parse_alwayson(t)
        tot_sk += sk
        tot_ag += ag
        tot_hk += hk
        tot_mc += mc
        tot_ao += ao
        flag = "FLAGGED" if ao > args.threshold else "ok"
        rows.append(
            {
                "name": name,
                "skills": sk,
                "agents": ag,
                "hooks": hk,
                "mcp": mc,
                "always_on": ao,
                "status": flag,
            }
        )

    if args.json:
        report = {
            "plugins": rows,
            "totals": {
                "skills": tot_sk,
                "agents": tot_ag,
                "hooks": tot_hk,
                "mcp": tot_mc,
                "always_on": tot_ao,
            },
            "threshold": args.threshold,
        }
        print(json.dumps(report, indent=2))
        return 0

    rows_sorted = sorted(rows, key=lambda r: r["always_on"] or 0, reverse=True)
    lines: list[str] = []
    lines.append(
        f"{'PLUGIN':<35}{'skills':>7}{'agents':>7}{'hooks':>6}"
        f"{'mcp':>5}{'always-on/session':>19}  flag"
    )
    lines.append("-" * 88)
    for r in rows_sorted:
        ao = f"~{r['always_on']:,} tok" if r["always_on"] is not None else "no data"
        flag = "⚠️" if r["status"] == "FLAGGED" else ""
        lines.append(
            f"{r['name']:<35}{str(r['skills']):>7}{str(r['agents']):>7}"
            f"{str(r['hooks']):>6}{str(r['mcp']):>5}{ao:>19}  {flag}"
        )
    lines.append("-" * 88)
    lines.append(
        f"{'TOTALS':<35}{tot_sk:>7}{tot_ag:>7}{tot_hk:>6}{tot_mc:>5}"
        f"{('~' + format(tot_ao, ',') + ' tok'):>19}"
    )
    lines.append("")
    flagged = [r for r in rows if r["status"] == "FLAGGED"]
    if flagged:
        lines.append(
            f"FLAGGED ({len(flagged)} plugins >{args.threshold} tok always-on):"
        )
        for r in flagged:
            lines.append(
                f"  - {r['name']}: ~{r['always_on']:,} tok — "
                f"used >= weekly? keep. Else: disable + per-session enable."
            )
    else:
        lines.append(f"All plugins under {args.threshold} tok threshold.")

    print("\n".join(lines))

    if args.report:
        with open(args.report, "w") as f:
            f.write("# Plugin token-load report\n\n")
            f.write(
                "Generated by `measure-plugin-load.py`. "
                "Re-run quarterly + at every plugin install.\n\n"
            )
            f.write("## Decision rule\n\n")
            f.write(f"- <= {args.threshold} tok always-on -> install global (cheap).\n")
            f.write(
                f"- > {args.threshold} tok + used >= weekly -> install global "
                f"(amortizes).\n"
            )
            f.write(
                f"- > {args.threshold} tok + used < weekly -> disable + "
                f"document re-enable command.\n\n"
            )
            f.write("## Current state\n\n")
            f.write("```\n")
            f.write("\n".join(lines))
            f.write("\n```\n")
        sys.stderr.write(f"wrote report -> {args.report}\n")

    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
