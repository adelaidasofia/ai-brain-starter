#!/usr/bin/env python3
"""
graphify_seed_labels.py -- deterministic, zero-LLM provisional names for communities.

The report is generated BEFORE the model semantically labels anything, so whatever
label the first pass supplies is what a user sees if labeling never finishes. Seeding
"Community 412" produces a report -- and, via the community hub notes, a graph -- where
every cluster is an anonymous number.

Instead, name each community after its highest-degree member: the node the rest of the
cluster actually hangs off. Measured on a 14,046-node vault this turns "Community 0"
into "Money", "Community 2" into "Fear", "Community 4" into "Learning".

Journal-heavy corpora hub on daily-note nodes, so a bare date wins on degree while
saying nothing about the topic. Skip low-signal labels (dates, phone numbers,
attachment stubs, bare numerals) and take the best real node beneath them; fall back
to the raw top node only when a cluster is genuinely nothing else.

These are PROVISIONAL. A semantic label from the model beats an anchor every time --
this only guarantees the floor is a real word instead of an index.
"""

import re

MAX_LABEL_CHARS = 40

_LOW_SIGNAL = re.compile(
    r"""^(
        \d{4}-\d{2}-\d{2}([T ][\d:\-]+)?   # 2026-05-28, 2014-10-19T12:58
      | \+?\d[\d\s()\-]{6,}                # phone numbers
      | at_\d+_.*                          # attachment stubs
      | [\d\s.,%$-]+                       # bare numerals
      | .{0,2}                             # 1-2 char noise
    )$""",
    re.VERBOSE,
)

_MEDIA_SUFFIX = (".png", ".jpg", ".jpeg", ".heic", ".pdf", ".mp4", ".mov", ".webp", ".gif")

# Session/journal slugs like "2026-05-30T03-36-concurrency-at-scale". Not low-signal --
# the tail is the topic -- but a sibling note that leads with the topic reads better.
_DATE_PREFIX = re.compile(r"^\d{4}[-_]?\d{2}[-_]?\d{2}")


def is_low_signal(label: str) -> bool:
    """True when a label identifies a record rather than a topic."""
    text = (label or "").strip()
    return bool(_LOW_SIGNAL.match(text)) or text.lower().endswith(_MEDIA_SUFFIX)


def _truncate(label: str) -> str:
    label = " ".join(label.split())
    return label if len(label) <= MAX_LABEL_CHARS else label[: MAX_LABEL_CHARS - 1].rstrip() + "…"


def anchor_label(G, node_ids: list[str]) -> str:
    """Best-reading high-degree member: topic beats record, plain name beats dated slug."""
    if not node_ids:
        return "unnamed"

    def rank(node):
        text = str(G.nodes[node].get("label", node))
        # Tiers first, degree only inside a tier, then label for a stable tie-break.
        return (is_low_signal(text), bool(_DATE_PREFIX.match(text)), -G.degree(node), text)

    best = min(node_ids, key=rank)
    return _truncate(str(G.nodes[best].get("label", best)))


def seed_labels(G, communities: dict) -> dict:
    """{cid: provisional name}. Names are unique -- collisions would merge hub notes."""
    seen: dict[str, int] = {}
    out = {}
    # Largest first so the biggest cluster keeps the clean name on a collision.
    for cid in sorted(communities, key=lambda c: (-len(communities[c]), c)):
        name = anchor_label(G, communities[cid])
        if name in seen:
            seen[name] += 1
            name = _truncate(f"{name} ({seen[name]})")
        else:
            seen[name] = 1
        out[cid] = name
    return {cid: out[cid] for cid in communities}


if __name__ == "__main__":
    import argparse
    import collections
    import json
    import sys

    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Preview seeded community names from a graph.json.")
    ap.add_argument("graph_json", nargs="?", default="graphify-out/graph.json")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    with open(args.graph_json, encoding="utf-8") as fh:
        raw = json.load(fh)

    class _G:  # minimal stand-in so the CLI needs no networkx
        def __init__(self, data):
            self.nodes = {n["id"]: n for n in data["nodes"]}
            self._deg = collections.Counter()
            for e in data.get("links", data.get("edges", [])):
                self._deg[e["source"]] += 1
                self._deg[e["target"]] += 1

        def degree(self, n):
            return self._deg[n]

    G = _G(raw)
    comms = collections.defaultdict(list)
    for n in raw["nodes"]:
        if n.get("community") is not None:
            comms[n["community"]].append(n["id"])

    names = seed_labels(G, dict(comms))
    ordered = sorted(comms, key=lambda c: -len(comms[c]))
    print(f"{len(ordered)} communities; showing top {args.top}\n")
    for cid in ordered[: args.top]:
        print(f"  {len(comms[cid]):>5} nodes  community {cid:<5} -> {names[cid]!r}")
