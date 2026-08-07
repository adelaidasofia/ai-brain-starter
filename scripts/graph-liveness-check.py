#!/usr/bin/env python3
"""graph-liveness-check.py — verify the graphify knowledge graph ARTIFACT exists,
instead of trusting the second-brain-mapping state-file stamp.

Bug class killed: STAMP-GREEN-WHILE-ARTIFACT-GONE.

`graphify-out/` is gitignored by design (a large, regenerable derived artifact
should not bloat a repo). But gitignored + untracked means it can vanish to ANY
filesystem operation — a folder move, a `git clean`, a disk cleanup — with ZERO
trace, while `.second-brain-mapping-state.json` still records
`phase_2_graphify: <date>`. The stamp says "built"; the file is gone. A recency
check that trusts the stamp is therefore lying. Probe the leaf (the file), never
the proxy (the timestamp). Existence is not enough either — a truncated / empty
graph.json is "present but not populated", so we check size (and, with --deep,
node count).

Two consumers:
  1. second-brain-mapping Step 1 — call with --heal so a lost graph nulls its
     stamp and the pipeline rebuilds instead of skipping on a stale stamp.
  2. A SessionStart gauge — call with --quiet so a healthy graph is silent and a
     lost/stale one screams.

Exit codes:
  0  healthy (graph present + populated), OR absent-and-never-stamped (nothing
     to rebuild yet — not an error)
  3  LOST: graph absent/empty BUT phase_2_graphify is stamped  (loud)
  4  STALE: graph present but older than --max-age-days
  2  usage / unexpected error

Stdlib only, Python 3.10+. No third-party deps (substrate portability).
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys

DEFAULT_MAX_AGE_DAYS = 45
PRESENT_MIN_BYTES = 200  # smaller than this = empty/truncated, treat as not-built
STATE_REL = "⚙️ Meta/.second-brain-mapping-state.json"
GRAPH_RELS = ("⚙️ Meta/graphify-out/graph.json", "graphify-out/graph.json")


def find_vault_root(start: str) -> pathlib.Path:
    """Walk up from `start` looking for the ⚙️ Meta marker dir; fall back to start."""
    p = pathlib.Path(start).resolve()
    for cand in (p, *p.parents):
        if (cand / "⚙️ Meta").is_dir():
            return cand
    return p


def inspect_graph(root: pathlib.Path, deep: bool):
    """Return dict: {path, present, populated, size, node_count, mtime}."""
    for rel in GRAPH_RELS:
        c = root / rel
        if not c.is_file():
            continue
        try:
            st = c.stat()
        except OSError:
            continue
        mtime = datetime.datetime.fromtimestamp(st.st_mtime).astimezone()
        present = st.st_size >= PRESENT_MIN_BYTES
        node_count = None
        populated = present
        if deep and present:
            try:
                data = json.loads(c.read_text(encoding="utf-8", errors="ignore"))
                nodes = data.get("nodes") if isinstance(data, dict) else None
                if isinstance(nodes, list):
                    node_count = len(nodes)
                    populated = node_count > 0
            except Exception:
                node_count = -1  # unparseable — suspect, but don't hard-fail on parse alone
        return {
            "path": str(c),
            "present": present,
            "populated": populated,
            "size": st.st_size,
            "node_count": node_count,
            "mtime": mtime,
        }
    return {
        "path": None,
        "present": False,
        "populated": False,
        "size": 0,
        "node_count": None,
        "mtime": None,
    }


def read_state(root: pathlib.Path):
    sf = root / STATE_REL
    if sf.is_file():
        try:
            return sf, json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            return sf, {}
    return sf, {}


def heal_state(sf: pathlib.Path, state: dict) -> list[str]:
    """Null the graph-dependent phases so the pipeline rebuilds. Returns keys nulled."""
    nulled = []
    for k in ("phase_2_graphify", "phase_3_wikilinks"):
        if state.get(k):
            state[k] = None
            nulled.append(k)
    if nulled:
        sf.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return nulled


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", default=".", metavar="ROOT", help="Vault root (default: auto-detect from cwd)")
    ap.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS, metavar="N")
    ap.add_argument("--deep", action="store_true", help="Parse graph.json and count nodes (slower)")
    ap.add_argument("--heal", action="store_true", help="On LOST, null phase_2/phase_3 stamps so SBM rebuilds")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    ap.add_argument("--quiet", action="store_true", help="Print nothing when healthy")
    args = ap.parse_args()

    root = find_vault_root(args.vault)
    g = inspect_graph(root, deep=args.deep)
    sf, state = read_state(root)
    stamp = state.get("phase_2_graphify")

    now = datetime.datetime.now().astimezone()
    age_days = None
    if g["mtime"] is not None:
        age_days = (now - g["mtime"]).days

    # classify
    if g["present"] and g["populated"]:
        if age_days is not None and age_days > args.max_age_days:
            status, code = "STALE", 4
        else:
            status, code = "HEALTHY", 0
    elif stamp:
        status, code = "LOST", 3
    else:
        status, code = "ABSENT_UNSTAMPED", 0

    nulled = heal_state(sf, state) if (status == "LOST" and args.heal) else []

    if args.json:
        print(json.dumps({
            "status": status, "exit": code, "vault": str(root),
            "graph_path": g["path"], "present": g["present"], "populated": g["populated"],
            "size": g["size"], "node_count": g["node_count"],
            "graph_mtime": g["mtime"].isoformat() if g["mtime"] else None,
            "age_days": age_days, "phase_2_stamp": stamp,
            "healed": nulled, "max_age_days": args.max_age_days,
        }, indent=2))
        return code

    if status == "LOST":
        print("╔═══════════════════════════════════════════════════════════════════╗")
        print("║  ⚠  KNOWLEDGE GRAPH LOST — stamp says built, artifact is GONE       ║")
        print("╚═══════════════════════════════════════════════════════════════════╝")
        print(f"  vault:        {root}")
        print(f"  expected at:  {root / GRAPH_RELS[0]}")
        print(f"  last 'built': phase_2_graphify = {stamp}")
        print("  source is intact — the graph is a DERIVED artifact, rebuild it:")
        print(f'      cd "{root}" && /graphify "." --update')
        if nulled:
            print(f"  healed: nulled {', '.join(nulled)} so second-brain-mapping will rebuild.")
        elif args.heal is False:
            print("  (run with --heal to null the stamp so SBM rebuilds automatically.)")
    elif status == "STALE":
        print(f"[graph-liveness] STALE: graph is {age_days}d old (> {args.max_age_days}d). "
              f"Refresh: /graphify \".\" --update   ({g['path']})")
    elif status == "ABSENT_UNSTAMPED":
        if not args.quiet:
            print("[graph-liveness] no graph yet and none ever built — run /graphify to create one.")
    else:  # HEALTHY
        if not args.quiet:
            nc = f", {g['node_count']} nodes" if g["node_count"] and g["node_count"] > 0 else ""
            print(f"[graph-liveness] HEALTHY: graph present ({g['size']:,} bytes{nc}, {age_days}d old).")

    return code


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
