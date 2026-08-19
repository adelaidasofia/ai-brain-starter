#!/usr/bin/env python3
"""
Negative-control tests for the graphify report sanitizer + label seeder.

Guards the bug class GENERATED-REPORT-FLOODS-VAULT-GRAPH-WITH-GHOST-NODES: the
upstream graphify report links one [[_COMMUNITY_*]] hub per community, those notes
exist only in the opt-in Obsidian export, and Obsidian draws every unresolved link
as a node -- so a default run buries the user's real graph under thousands of grey
placeholder dots named "Community 412".

Run: python3 scripts/test-graphify-report-sanitize.py
"""

from __future__ import annotations  # PEP 604 annotations; gate pins Python 3.9

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "graphify" / "scripts"
SANITIZE = SCRIPTS / "graphify_report_sanitize.py"
sys.path.insert(0, str(SCRIPTS))

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def make_report(sizes: dict[int, int], labels: dict[int, str] | None = None) -> str:
    """Rebuild the upstream report shape: hub links for every community + detail section."""
    labels = labels or {}
    lab = lambda c: labels.get(c, f"Community {c}")
    lines = ["# Graph Report", "", "## Summary", f"- {sum(sizes.values())} nodes", ""]
    lines.append("## Community Hubs (Navigation)")
    for c in sizes:
        lines.append(f"- [[_COMMUNITY_{lab(c)}|{lab(c)}]]")
    lines += ["", "## God Nodes (most connected - your core abstractions)", "1. `x` - 3 edges", ""]
    lines.append("## Communities")
    for c, n in sizes.items():
        lines += ["", f'### Community {c} - "{lab(c)}"', "Cohesion: 0.4",
                  f"Nodes ({n}): a, b, c (+{max(n - 3, 0)} more)"]
    lines += ["", "## Suggested Questions", "- what now?"]
    return "\n".join(lines) + "\n"


def run(*args, cwd=None):
    return subprocess.run([sys.executable, str(SANITIZE), *map(str, args)],
                          capture_output=True, text=True, cwd=cwd,
                          encoding="utf-8", errors="replace")


def main():
    print("\n== sanitizer ==")
    sizes = {0: 300, 1: 40, 2: 12, 3: 5, 4: 4, 5: 2, 6: 1, 7: 1}

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "graphify-out"
        out.mkdir()
        rp = out / "GRAPH_REPORT.md"
        rp.write_text(make_report(sizes), encoding="utf-8")

        # NEGATIVE CONTROL: the checker must fail on the un-sanitized upstream shape.
        pre = run("--check", rp)
        check("--check fails on un-sanitized report", pre.returncode == 1, pre.stdout)
        check("--check counts every ghost", "8 unresolved" in pre.stdout, pre.stdout)

        r = run(rp, "--no-index-fix")
        check("sanitize exits 0", r.returncode == 0, r.stderr)
        text = rp.read_text(encoding="utf-8")
        check("no wikilinks survive without an export", "[[" not in text,
              [l for l in text.splitlines() if "[[" in l][:2])
        check("--check passes after sanitize", run("--check", rp).returncode == 0)

        # Floor: 4 communities are >=5 nodes; 4 are below.
        nav = text.split("## Community Hubs (Navigation)")[1].split("\n## ")[0]
        check("floor keeps only >=5-node communities", nav.count("\n- ") == 4, nav)
        check("largest community listed first", nav.index("Community 0") < nav.index("Community 3"), nav)
        check("node counts shown", "(300 notes)" in nav, nav)
        check("omitted tail is disclosed, not silent", "4 further communities" in nav, nav)
        check("detail section untouched", text.count("### Community ") == len(sizes))
        check("file left POSIX-clean", text.endswith("\n") and not text.endswith("\n\n\n"))

        # Idempotency.
        run(rp, "--no-index-fix")
        check("idempotent", rp.read_text(encoding="utf-8") == text)

    # Wikilinks SURVIVE when the export really exists.
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "graphify-out"
        (out / "obsidian").mkdir(parents=True)
        rp = out / "GRAPH_REPORT.md"
        rp.write_text(make_report(sizes), encoding="utf-8")
        for c in (0, 1, 2, 3):
            (out / "obsidian" / f"_COMMUNITY_Community {c}.md").write_text("hub", encoding="utf-8")
        run(rp, "--no-index-fix")
        text = rp.read_text(encoding="utf-8")
        check("real hub notes stay linked", text.count("[[_COMMUNITY_") == 4, text)
        check("--check passes on resolvable links", run("--check", rp).returncode == 0)

    print("\n== obsidian index self-heal ==")
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "MyVault"
        (vault / ".obsidian").mkdir(parents=True)
        (vault / ".obsidian" / "app.json").write_text(
            json.dumps({"userIgnoreFilters": ["Archive/"], "newFileLocation": "folder"}), encoding="utf-8")
        out = vault / "graphify-out"
        out.mkdir()
        rp = out / "GRAPH_REPORT.md"
        rp.write_text(make_report(sizes), encoding="utf-8")
        r = run(rp)
        cfg = json.loads((vault / ".obsidian" / "app.json").read_text(encoding="utf-8"))
        check("graphify-out/ excluded from index", "graphify-out/" in cfg["userIgnoreFilters"], cfg)
        check("existing user filters preserved", "Archive/" in cfg["userIgnoreFilters"], cfg)
        check("unrelated settings preserved", cfg.get("newFileLocation") == "folder", cfg)
        check("vault auto-detected without --vault", "userIgnoreFilters" in r.stdout, r.stdout)
        run(rp)
        cfg2 = json.loads((vault / ".obsidian" / "app.json").read_text(encoding="utf-8"))
        check("exclusion not duplicated on re-run", cfg2["userIgnoreFilters"].count("graphify-out/") == 1, cfg2)

    # Real vaults carry the slashless form; an exact-match check would double it up.
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "MyVault"
        (vault / ".obsidian").mkdir(parents=True)
        (vault / ".obsidian" / "app.json").write_text(
            json.dumps({"userIgnoreFilters": ["graphify-out"]}), encoding="utf-8")
        out = vault / "graphify-out"
        out.mkdir()
        rp = out / "GRAPH_REPORT.md"
        rp.write_text(make_report(sizes), encoding="utf-8")
        run(rp)
        cfg = json.loads((vault / ".obsidian" / "app.json").read_text(encoding="utf-8"))
        check("slashless existing exclusion is recognized",
              cfg["userIgnoreFilters"] == ["graphify-out"], cfg)

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "graphify-out"
        out.mkdir()
        rp = out / "GRAPH_REPORT.md"
        rp.write_text(make_report(sizes), encoding="utf-8")
        r = run(rp)
        check("non-vault corpus writes no config", "userIgnoreFilters" not in r.stdout, r.stdout)

    print("\n== retroactive relabel (--relabel-from) ==")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "graphify-out"
        out.mkdir()
        rp = out / "GRAPH_REPORT.md"
        # cid 0 is a placeholder and must be renamed; cid 1 already carries a real
        # semantic label and must survive untouched.
        rp.write_text(make_report({0: 6, 1: 5}, {1: "Hand Written Label"}), encoding="utf-8")
        gj = out / "graph.json"
        gj.write_text(json.dumps({
            "nodes": [{"id": f"n{i}", "label": lbl, "community": c} for i, (lbl, c) in enumerate(
                [("2026-05-28", 0), ("Money", 0), ("a", 0), ("b", 0), ("c", 0), ("d", 0),
                 ("Fear", 1), ("e", 1), ("f", 1), ("g", 1), ("h", 1)])],
            "links": [{"source": "n0", "target": "n2"}, {"source": "n0", "target": "n3"},
                      {"source": "n0", "target": "n4"}, {"source": "n1", "target": "n2"},
                      {"source": "n1", "target": "n3"}, {"source": "n6", "target": "n7"}],
        }), encoding="utf-8")
        r = run(rp, "--relabel-from", gj, "--no-index-fix")
        text = rp.read_text(encoding="utf-8")
        check("placeholder renamed from graph anchor", '"Money"' in text, r.stdout)
        check("date hub not chosen as the name", '"2026-05-28"' not in text, r.stdout)
        check("existing semantic label untouched", '"Hand Written Label"' in text, r.stdout)
        check("rename count reported", "renamed 1 placeholder" in r.stdout, r.stdout)
        check("navigation shows the new name", "- Money (6 notes)" in text, text)
        # \s*$ under MULTILINE is greedy across newlines: a heading at EOF loses the
        # blank line the replacement never puts back.
        tail = rp.read_text(encoding="utf-8")
        check("relabel preserves trailing structure", tail.endswith("\n"), repr(tail[-40:]))
        check("no heading collapsed into the next line",
              "\n### Community" in tail and '"Money"\n' in tail, repr(tail[-200:]))

        r2 = run(rp, "--relabel-from", Path(td) / "missing.json", "--no-index-fix")
        check("missing graph.json fails loud", r2.returncode == 1 and "cannot relabel" in r2.stderr, r2.stderr)

    print("\n== fail loud, never silent no-op ==")
    with tempfile.TemporaryDirectory() as td:
        rp = Path(td) / "GRAPH_REPORT.md"
        rp.write_text("# Graph Report\n\n## Summary\n- 3 nodes\n", encoding="utf-8")
        r = run(rp, "--no-index-fix")
        check("unrecognized shape says so on stderr", "unrecognized" in r.stderr, r.stderr)
        rp.write_text("## Community Hubs (Navigation)\n- [[_COMMUNITY_A|A]]\n", encoding="utf-8")
        r = run(rp, "--no-index-fix")
        check("hub without detail section refuses to guess", "left untouched" in r.stderr, r.stderr)
        r = run(Path(td) / "nope.md", "--no-index-fix")
        check("missing report is not an error", r.returncode == 0 and "not found" in r.stderr, r.stderr)

    print("\n== label seeder ==")
    from graphify_seed_labels import anchor_label, is_low_signal, seed_labels

    class G:
        def __init__(self, d):
            self.nodes = {k: {"label": v[0]} for k, v in d.items()}
            self._d = {k: v[1] for k, v in d.items()}

        def degree(self, n):
            return self._d[n]

    check("bare date is low signal", is_low_signal("2026-05-28"))
    check("timestamp is low signal", is_low_signal("2014-10-19T12:58"))
    check("phone is low signal", is_low_signal("+119048591392886"))
    check("attachment is low signal", is_low_signal("at_1_9E0-PNG image.png"))
    check("real topic is not low signal", not is_low_signal("Money"))

    g = G({"a": ("2026-05-28", 90), "b": ("Money", 40), "c": ("x", 5)})
    check("date hub loses to a real topic", anchor_label(g, list(g.nodes)) == "Money")

    g2 = G({"a": ("2026-05-30T03-36-concurrency-at-scale", 90), "b": ("browser-automation-trust", 60)})
    check("dated slug loses to a plain name", anchor_label(g2, list(g2.nodes)) == "browser-automation-trust")

    g3 = G({"a": ("2026-05-28", 90), "b": ("2026-06-01", 40)})
    check("all-dates cluster still names itself", anchor_label(g3, list(g3.nodes)) == "2026-05-28")

    g4 = G({"a": ("Money", 90), "b": ("Money", 80), "c": ("Fear", 10)})
    seeded = seed_labels(g4, {0: ["a", "c"], 1: ["b"]})
    check("no placeholder 'Community N' names", not any(v.startswith("Community ") for v in seeded.values()), seeded)
    check("collisions deduped", len(set(seeded.values())) == 2, seeded)

    g5 = G({"a": ("x" * 120, 5)})
    check("long labels truncated", len(anchor_label(g5, ["a"])) <= 40)
    check("empty community is safe", anchor_label(g5, []) == "unnamed")

    # Collisions among LONG names: appending " (2)" and truncating afterwards cuts the
    # suffix straight back off, so distinct communities collapse onto one name -- and that
    # name is a _COMMUNITY_<name>.md filename, so one hub note silently overwrites another.
    long_a, long_b, long_c = ("2026-06-14T21-34-concurrency-primitive-alpha",
                              "2026-06-14T21-34-concurrency-primitive-beta",
                              "2026-06-14T21-34-concurrency-primitive-gamma")
    g6 = G({"a": (long_a, 9), "b": (long_b, 8), "c": (long_c, 7)})
    seeded6 = seed_labels(g6, {0: ["a"], 1: ["b"], 2: ["c"]})
    check("long colliding names stay distinct", len(set(seeded6.values())) == 3, seeded6)
    check("disambiguated names still bounded",
          all(len(v) <= 40 for v in seeded6.values()), {k: len(v) for k, v in seeded6.items()})

    # The stand-in class above is not what Step 4 passes. Prove the real seam.
    try:
        import networkx as nx
    except ImportError:
        print("  SKIP  real networkx seam (networkx not installed)")
    else:
        real = nx.Graph()
        for i, lbl in enumerate(["Money", "2026-05-28", "Fear", "b", "c", "d"]):
            real.add_node(f"n{i}", label=lbl)
        real.add_edges_from([("n1", "n3"), ("n1", "n4"), ("n1", "n5"), ("n0", "n3"), ("n0", "n4")])
        got = seed_labels(real, {0: ["n0", "n1", "n3", "n4", "n5"], 1: ["n2"]})
        check("real networkx graph labels correctly", got[0] == "Money" and got[1] == "Fear", got)
        check("real networkx graph yields no placeholders",
              not any(v.startswith("Community ") for v in got.values()), got)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
