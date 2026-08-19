#!/usr/bin/env python3
"""
graphify_report_sanitize.py -- make GRAPH_REPORT.md safe to live inside a real vault.

The upstream `graphify` package writes a "Community Hubs (Navigation)" section
containing one [[_COMMUNITY_<name>]] wikilink per detected community. Those links
resolve only inside the OPT-IN Obsidian export (graphify-out/obsidian/), which most
runs never generate. On the default path every hub link is unresolved, and Obsidian
renders each unresolved link as a grey node -- so the report becomes a hub with
thousands of ghost spokes that swamps the user's real graph.

Two independent defects, both fixed here:

  1. GHOST LINKS. Emit a wikilink only when the target note actually exists.
     Otherwise render the same entry as plain text. Zero unresolved links.

  2. PLACEHOLDER NAMES + NOISE. Community detection on a personal vault produces
     mostly micro-communities: measured on a 14,046-node vault, 51% of communities
     were a SINGLE node and 96% had <= 4. Those are extraction noise, not topics,
     and they are what the labeling step leaves named "Community 412". Navigation
     lists communities at or above --min-size only, largest first, and rolls the
     tail into one honest summary line.

Idempotent: safe to re-run on an already-sanitized report.

Usage:
  graphify_report_sanitize.py [REPORT]              # sanitize in place
  graphify_report_sanitize.py --check [REPORT]      # verify only, exit 1 if ghosts remain
  graphify_report_sanitize.py --vault ~/MyVault     # also exclude graphify-out/ from the index
"""

import argparse
import json
import re
import sys
from pathlib import Path

HUB_HEADING = "## Community Hubs (Navigation)"
DETAIL_HEADING = "## Communities"
DEFAULT_MIN_SIZE = 5

# Mirrors graphify.report._safe_community_name / export.safe_name. Kept local so a
# sanitize run never needs the package importable.
_STRIP = re.compile(r'[\\/*?:"<>|#^[\]]')
_MD_EXT = re.compile(r"\.(md|mdx|markdown)$", re.IGNORECASE)


def safe_community_name(label: str) -> str:
    cleaned = label.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    cleaned = _STRIP.sub("", cleaned).strip()
    return _MD_EXT.sub("", cleaned) or "unnamed"


def parse_communities(text: str) -> list[dict]:
    """Read the '## Communities' detail section -> [{cid, label, size}], source of truth for sizes."""
    out = []
    body = text.split(DETAIL_HEADING, 1)
    if len(body) < 2:
        return out
    # '### Community 7 - "Sales Pipeline"' then later 'Nodes (23): a, b, c'
    pattern = re.compile(
        r'^### Community (\d+) - "(.*?)"[ \t]*$(?:\n(?!### ).*)*?\nNodes \((\d+)\):',
        re.MULTILINE,
    )
    for m in pattern.finditer(body[1]):
        out.append({"cid": int(m.group(1)), "label": m.group(2), "size": int(m.group(3))})
    return out


def relabel_placeholders(text: str, graph_json: Path) -> tuple[str, int]:
    """Replace surviving "Community N" names with anchor names from graph.json.

    Retroactive fix: a report generated before label seeding existed is stuck with bare
    indices, and re-running the whole pipeline to rename them costs a full extraction.
    Only PLACEHOLDER names are touched -- a real semantic label is never clobbered.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from graphify_seed_labels import seed_labels, load_graph_json

    G, communities = load_graph_json(graph_json)
    names = seed_labels(G, communities)

    replaced = 0

    def swap(m):
        nonlocal replaced
        cid, label = int(m.group(1)), m.group(2)
        if label != f"Community {cid}" or cid not in names:
            return m.group(0)
        replaced += 1
        return f'### Community {cid} - "{names[cid]}"'

    return re.sub(r'^### Community (\d+) - "(.*?)"[ \t]*$', swap, text, flags=re.MULTILINE), replaced


def find_obsidian_dir(report_path: Path, explicit: str | None) -> Path | None:
    """Where _COMMUNITY_*.md notes live, or None when the export was never generated."""
    candidates = [Path(explicit)] if explicit else [report_path.parent / "obsidian"]
    for c in candidates:
        if c.is_dir() and any(c.glob("_COMMUNITY_*.md")):
            return c
    return None


def build_hub_section(comms: list[dict], obsidian_dir: Path | None, min_size: int) -> tuple[list[str], dict]:
    kept = sorted((c for c in comms if c["size"] >= min_size), key=lambda c: -c["size"])
    dropped = [c for c in comms if c["size"] < min_size]
    unnamed = [c for c in kept if re.fullmatch(rf"Community {c['cid']}", c["label"])]

    lines = [HUB_HEADING, ""]
    if not comms:
        lines.append("_No communities detected._")
    for c in kept:
        noun = "note" if c["size"] == 1 else "notes"
        entry = f"{c['label']} ({c['size']} {noun})"
        if obsidian_dir is not None:
            safe = safe_community_name(c["label"])
            if (obsidian_dir / f"_COMMUNITY_{safe}.md").exists():
                entry = f"[[_COMMUNITY_{safe}|{c['label']}]] ({c['size']} {noun})"
        lines.append(f"- {entry}")

    if dropped:
        covered = sum(c["size"] for c in dropped)
        lines += [
            "",
            f"_{len(dropped):,} further communities under {min_size} nodes "
            f"({covered:,} nodes total) are omitted here. Clusters that small are "
            f"extraction fragments, not topics._",
        ]
    stats = {"kept": len(kept), "dropped": len(dropped), "unnamed": len(unnamed),
             "linkable": obsidian_dir is not None}
    return lines, stats


def replace_section(text: str, heading: str, new_lines: list[str]) -> str:
    """Swap one '## ' section for new_lines. Returns text unchanged if heading absent."""
    start = text.find(heading)
    if start == -1:
        return text
    nxt = text.find("\n## ", start + len(heading))
    end = len(text) if nxt == -1 else nxt + 1
    return text[:start] + "\n".join(new_lines).rstrip() + "\n" + text[end:]


def find_vault_root(start: Path) -> Path | None:
    """Nearest ancestor holding .obsidian/. None means this corpus is not an Obsidian vault."""
    for parent in [start.resolve(), *start.resolve().parents]:
        if (parent / ".obsidian").is_dir():
            return parent
    return None


def ensure_vault_exclusion(vault: Path, folder: str = "graphify-out/") -> str:
    """Merge folder into .obsidian/app.json userIgnoreFilters. Never clobbers user filters."""
    app_file = vault / ".obsidian" / "app.json"
    if not app_file.parent.is_dir():
        return f"no .obsidian/ at {vault} - skipped index exclusion"
    settings = {}
    if app_file.exists():
        try:
            settings = json.loads(app_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return f"could not parse {app_file} - left untouched"
    filters = settings.get("userIgnoreFilters") or []
    # Obsidian accepts the folder with or without a trailing slash and treats both as
    # the same prefix. Compare normalized, or a vault that already excludes
    # "graphify-out" collects a redundant "graphify-out/" on every run.
    if folder.rstrip("/") in {f.rstrip("/") for f in filters}:
        return f"{folder} already excluded from the Obsidian index"
    settings["userIgnoreFilters"] = filters + [folder]
    app_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return f"added {folder} to userIgnoreFilters - reload Obsidian (Cmd+R) to apply"


def count_ghosts(text: str, obsidian_dir: Path | None) -> int:
    n = 0
    for m in re.finditer(r"\[\[_COMMUNITY_([^\]|]+)", text):
        if obsidian_dir is None or not (obsidian_dir / f"_COMMUNITY_{m.group(1)}.md").exists():
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", nargs="?", default="graphify-out/GRAPH_REPORT.md")
    ap.add_argument("--min-size", type=int, default=DEFAULT_MIN_SIZE,
                    help=f"community node floor for navigation (default {DEFAULT_MIN_SIZE})")
    ap.add_argument("--obsidian-dir", default=None, help="where _COMMUNITY_*.md notes live")
    ap.add_argument("--vault", default=None,
                    help="vault root for the index exclusion (default: nearest ancestor with .obsidian/)")
    ap.add_argument("--no-index-fix", action="store_true",
                    help="do not touch .obsidian/app.json")
    ap.add_argument("--relabel-from", default=None, metavar="GRAPH_JSON",
                    help="rename leftover 'Community N' placeholders using graph.json anchors")
    ap.add_argument("--check", action="store_true", help="verify only; exit 1 if unresolved links remain")
    args = ap.parse_args()

    report_path = Path(args.report)
    if not report_path.exists():
        print(f"sanitize: {report_path} not found - nothing to do", file=sys.stderr)
        return 0

    text = report_path.read_text(encoding="utf-8")
    obsidian_dir = find_obsidian_dir(report_path, args.obsidian_dir)

    if args.check:
        ghosts = count_ghosts(text, obsidian_dir)
        if ghosts:
            print(f"FAIL: {ghosts} unresolved [[_COMMUNITY_...]] link(s) in {report_path}")
            print("      Each one renders as a ghost node in Obsidian's graph view.")
            return 1
        print(f"OK: no unresolved community links in {report_path}")
        return 0

    if HUB_HEADING not in text:
        # Fail loud: an unrecognized report shape must not read as a clean pass.
        print(f"sanitize: no '{HUB_HEADING}' section in {report_path} - report shape "
              f"unrecognized, nothing rewritten", file=sys.stderr)
        return 0

    if args.relabel_from:
        graph_json = Path(args.relabel_from)
        if not graph_json.exists():
            print(f"sanitize: {graph_json} not found - cannot relabel", file=sys.stderr)
            return 1
        text, renamed = relabel_placeholders(text, graph_json)
        print(f"sanitize: renamed {renamed} placeholder communities from graph anchors")

    comms = parse_communities(text)
    if not comms:
        print(f"sanitize: found the hub section but no '{DETAIL_HEADING}' detail to size it "
              f"against - left untouched", file=sys.stderr)
        return 0

    new_lines, stats = build_hub_section(comms, obsidian_dir, args.min_size)
    # Upstream generate() returns "\n".join(lines) with no trailing newline; we are
    # rewriting the file anyway, so leave POSIX-clean text behind.
    final = replace_section(text, HUB_HEADING, new_lines)
    report_path.write_text(final if final.endswith("\n") else final + "\n", encoding="utf-8")

    mode = "wikilinks (Obsidian export present)" if stats["linkable"] else "plain text (no export - links would be dead)"
    print(f"sanitize: navigation lists {stats['kept']} communities >= {args.min_size} nodes as {mode}")
    if stats["dropped"]:
        print(f"          {stats['dropped']:,} micro-communities omitted from navigation")
    if stats["unnamed"]:
        print(f"          {stats['unnamed']} listed communities still have placeholder names "
              f"(\"Community N\") - label them in Step 5")

    # graphify-out/ holds a multi-MB graph.json plus this report. Indexed, it dominates
    # search and graph view; the folder is machinery, not notes. Self-healing: vaults
    # installed before this exclusion existed get it on their next run.
    if not args.no_index_fix:
        vault = Path(args.vault) if args.vault else find_vault_root(report_path.parent)
        if vault is not None:
            print(f"          {ensure_vault_exclusion(vault)}")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
