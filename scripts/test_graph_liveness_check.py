#!/usr/bin/env python3
"""Behavior tests for graph-liveness-check.py — the STAMP-GREEN-WHILE-ARTIFACT-GONE guard.

Proves the guard fires on the real failure (LOST) AND stays quiet on the clean
path (HEALTHY) — a guard that always-screams is as useless as one that never does.

Run standalone:  python3 scripts/test_graph_liveness_check.py
Run under pytest: pytest scripts/test_graph_liveness_check.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CHECK = Path(__file__).with_name("graph-liveness-check.py")


def _vault(tmp: Path, name: str) -> Path:
    root = tmp / name
    (root / "⚙️ Meta" / "graphify-out").mkdir(parents=True, exist_ok=True)
    return root


def _state(root: Path, **kw):
    (root / "⚙️ Meta" / ".second-brain-mapping-state.json").write_text(json.dumps(kw))


def _graph(root: Path, nodes: int = 40, old: bool = False):
    p = root / "⚙️ Meta" / "graphify-out" / "graph.json"
    p.write_text(json.dumps({
        "nodes": [{"id": i, "label": f"entity_{i}", "type": "concept"} for i in range(nodes)],
        "edges": [{"s": i, "t": (i + 1) % nodes, "kind": "MENTIONS"} for i in range(nodes)],
    }))
    if old:
        past = time.time() - 60 * 24 * 3600  # 60 days
        os.utime(p, (past, past))


def _run(root: Path, *extra):
    r = subprocess.run(
        [sys.executable, str(CHECK), "--vault", str(root), "--json", *extra],
        capture_output=True, text=True,
    )
    return r.returncode, json.loads(r.stdout)


def test_absent_unstamped_is_not_an_error():
    with tempfile.TemporaryDirectory() as d:
        root = _vault(Path(d), "absent"); _state(root)
        code, out = _run(root)
        assert code == 0 and out["status"] == "ABSENT_UNSTAMPED", out


def test_lost_graph_with_stamp_fires_loud():
    # THE NEGATIVE CONTROL: the exact incident — stamp present, artifact gone.
    with tempfile.TemporaryDirectory() as d:
        root = _vault(Path(d), "lost")
        _state(root, phase_2_graphify="2026-05-27T15:36:55-05:00",
               phase_3_wikilinks="2026-05-27T15:37:35-05:00")
        code, out = _run(root)
        assert code == 3 and out["status"] == "LOST", out


def test_healthy_graph_is_silent():
    with tempfile.TemporaryDirectory() as d:
        root = _vault(Path(d), "healthy"); _graph(root)
        _state(root, phase_2_graphify="2026-07-09T00:00:00-05:00")
        code, out = _run(root)
        assert code == 0 and out["status"] == "HEALTHY", out


def test_stale_graph_flags_refresh():
    with tempfile.TemporaryDirectory() as d:
        root = _vault(Path(d), "stale"); _graph(root, old=True)
        _state(root, phase_2_graphify="2026-05-01T00:00:00-05:00")
        code, out = _run(root)
        assert code == 4 and out["status"] == "STALE", out


def test_empty_graph_counts_as_lost():
    # existence is not enough — a truncated 0-byte graph.json is "present but not populated"
    with tempfile.TemporaryDirectory() as d:
        root = _vault(Path(d), "truncated")
        (root / "⚙️ Meta" / "graphify-out" / "graph.json").write_text("")
        _state(root, phase_2_graphify="2026-05-27T00:00:00-05:00")
        code, out = _run(root)
        assert code == 3 and out["status"] == "LOST", out


def test_heal_nulls_graph_dependent_stamps():
    with tempfile.TemporaryDirectory() as d:
        root = _vault(Path(d), "heal")
        _state(root, phase_1_metadata="2026-06-24T00:00:00-05:00",
               phase_2_graphify="2026-05-27T15:36:55-05:00",
               phase_3_wikilinks="2026-05-27T15:37:35-05:00")
        _run(root, "--heal")
        st = json.loads((root / "⚙️ Meta" / ".second-brain-mapping-state.json").read_text())
        assert st["phase_2_graphify"] is None, st
        assert st["phase_3_wikilinks"] is None, st
        # unrelated phases must be preserved
        assert st["phase_1_metadata"] == "2026-06-24T00:00:00-05:00", st


def test_deep_counts_nodes():
    with tempfile.TemporaryDirectory() as d:
        root = _vault(Path(d), "deep"); _graph(root, nodes=40)
        _state(root, phase_2_graphify="2026-07-09T00:00:00-05:00")
        code, out = _run(root, "--deep")
        assert out["node_count"] == 40, out


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
    print("ALL PASS" if not failed else f"{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(_main())
