#!/usr/bin/env python3
"""Convert a NetworkX-format graph.json into Neo4j-compatible import files.

Outputs:
  - neo4j-nodes.csv   (for neo4j-admin import or LOAD CSV)
  - neo4j-edges.csv
  - neo4j-import.cypher  (LOAD CSV Cypher script)

Usage:
  python3 graph-to-neo4j.py
  python3 graph-to-neo4j.py --input /path/to/graph.json --output-dir /path/to/neo4j/
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path


def detect_vault_root() -> Path:
    """Detect vault root from $VAULT_ROOT env var or script location."""
    env_root = os.environ.get("VAULT_ROOT")
    if env_root:
        return Path(env_root)
    # Fall back to script location (expects <vault>/⚙️ Meta/scripts/)
    script_dir = Path(__file__).resolve().parent
    candidate = script_dir.parent.parent
    if (candidate / "⚙️ Meta").is_dir():
        return candidate
    # Last resort: current directory
    return Path.cwd()


def parse_args():
    vault = detect_vault_root()
    default_input = vault / "graphify-out" / "graph.json"
    default_output = vault / "graphify-out" / "neo4j"

    parser = argparse.ArgumentParser(
        description="Convert NetworkX graph.json to Neo4j import files."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=default_input,
        help="Path to graph.json (default: %(default)s)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output,
        help="Directory for output CSVs and Cypher script (default: %(default)s)",
    )
    return parser.parse_args()


def capitalise_label(file_type: str) -> str:
    """Capitalise a file_type for use as a Neo4j :LABEL.

    'document' -> 'Document', 'rationale' -> 'Rationale', etc.
    """
    if not file_type:
        return "Unknown"
    return file_type.strip().capitalize()


def relation_to_type(relation: str) -> str:
    """Convert a relation string to a Neo4j relationship :TYPE.

    Uppercase, spaces replaced with underscores.
    """
    if not relation:
        return "RELATED_TO"
    return relation.strip().upper().replace(" ", "_")


def write_nodes_csv(nodes: list[dict], path: Path) -> None:
    fieldnames = [
        "id:ID",
        "label",
        "file_type",
        "source_file",
        "mention_count:int",
        "community:int",
        ":LABEL",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for node in nodes:
            writer.writerow(
                {
                    "id:ID": node.get("id", ""),
                    "label": node.get("label", ""),
                    "file_type": node.get("file_type", ""),
                    "source_file": node.get("source_file", ""),
                    "mention_count:int": node.get("mention_count", 0),
                    "community:int": node.get("community", 0),
                    ":LABEL": capitalise_label(node.get("file_type", "")),
                }
            )


def write_edges_csv(links: list[dict], path: Path) -> None:
    fieldnames = [
        ":START_ID",
        ":END_ID",
        ":TYPE",
        "confidence",
        "confidence_score:float",
        "source_file",
        "weight:float",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for link in links:
            writer.writerow(
                {
                    ":START_ID": link.get("source", ""),
                    ":END_ID": link.get("target", ""),
                    ":TYPE": relation_to_type(link.get("relation", "")),
                    "confidence": link.get("confidence", ""),
                    "confidence_score:float": link.get("confidence_score", 0.0),
                    "source_file": link.get("source_file", ""),
                    "weight:float": link.get("weight", 0.0),
                }
            )


def write_cypher_script(path: Path) -> None:
    cypher = """\
// Neo4j LOAD CSV import script
// Place neo4j-nodes.csv and neo4j-edges.csv in the Neo4j import/ directory,
// then run this script in the Neo4j Browser or via cypher-shell.

// -- Create constraint for fast lookups --
CREATE CONSTRAINT node_id_unique IF NOT EXISTS
FOR (n:Node) REQUIRE n.id IS UNIQUE;

// -- Load nodes --
LOAD CSV WITH HEADERS FROM 'file:///neo4j-nodes.csv' AS row
CREATE (n:Node {
  id:             row.`id:ID`,
  label:          row.label,
  file_type:      row.file_type,
  source_file:    row.source_file,
  mention_count:  toInteger(row.`mention_count:int`),
  community:      toInteger(row.`community:int`)
})
WITH n, row
CALL apoc.create.addLabels(n, [row.`:LABEL`]) YIELD node
RETURN count(node);

// -- Load edges --
LOAD CSV WITH HEADERS FROM 'file:///neo4j-edges.csv' AS row
MATCH (src:Node {id: row.`:START_ID`})
MATCH (tgt:Node {id: row.`:END_ID`})
CALL apoc.create.relationship(src, row.`:TYPE`, {
  confidence:       row.confidence,
  confidence_score: toFloat(row.`confidence_score:float`),
  source_file:      row.source_file,
  weight:           toFloat(row.`weight:float`)
}, tgt) YIELD rel
RETURN count(rel);
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(cypher)


def print_stats(nodes: list[dict], links: list[dict]) -> None:
    file_types = sorted(set(n.get("file_type", "") for n in nodes))
    relation_types = sorted(set(relation_to_type(l.get("relation", "")) for l in links))
    communities = set(n.get("community") for n in nodes if n.get("community") is not None)

    print()
    print("=== Neo4j Export Stats ===")
    print(f"  Nodes:            {len(nodes):,}")
    print(f"  Edges:            {len(links):,}")
    print(f"  Unique file types: {len(file_types)} {file_types}")
    print(f"  Unique relations:  {len(relation_types)} {relation_types}")
    print(f"  Communities:       {len(communities)}")
    print()


def main():
    args = parse_args()

    if not args.input.exists():
        print(f"Error: input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Reading {args.input} ...")
    with open(args.input, "r", encoding="utf-8") as f:
        graph = json.load(f)

    nodes = graph.get("nodes", [])
    links = graph.get("links", [])

    nodes_path = args.output_dir / "neo4j-nodes.csv"
    edges_path = args.output_dir / "neo4j-edges.csv"
    cypher_path = args.output_dir / "neo4j-import.cypher"

    print(f"Writing {nodes_path} ...")
    write_nodes_csv(nodes, nodes_path)

    print(f"Writing {edges_path} ...")
    write_edges_csv(links, edges_path)

    print(f"Writing {cypher_path} ...")
    write_cypher_script(cypher_path)

    print_stats(nodes, links)
    print("Done. Copy the CSV files to your Neo4j import/ directory and run neo4j-import.cypher.")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
