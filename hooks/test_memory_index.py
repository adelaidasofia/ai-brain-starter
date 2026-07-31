#!/usr/bin/env python3
"""Tests for hooks/_lib/memory_index.py and the loader that consumes it.

A guard earns trust only by FAILING on the thing it catches, so every positive
control is paired with a negative one: a healthy index must stay silent, or the
guard is noise that trains the reader to ignore it.

Stdlib only. Run: python3 hooks/test_memory_index.py
Exit 0 = all pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS / "_lib"))
import memory_index  # noqa: E402

LOADER = HOOKS / "session-start-context.py"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  " + name)
    else:
        FAIL += 1
        print("  FAIL  " + name + (("\n        " + detail) if detail else ""))


def memo(d: Path, name: str) -> None:
    (d / name).write_text(
        "---\nname: {}\n---\n\nbody\n".format(name[:-3]), encoding="utf-8")


def report_for(d: Path, env_extra=None) -> str:
    old = os.environ.get("AGENT_MEMORY_DIR")
    old_bypass = os.environ.get("MEMORY_INDEX_TRUNCATION_BYPASS")
    os.environ["AGENT_MEMORY_DIR"] = str(d)
    if env_extra:
        os.environ.update(env_extra)
    try:
        return memory_index.report()
    finally:
        if old is None:
            os.environ.pop("AGENT_MEMORY_DIR", None)
        else:
            os.environ["AGENT_MEMORY_DIR"] = old
        if old_bypass is None:
            os.environ.pop("MEMORY_INDEX_TRUNCATION_BYPASS", None)
        else:
            os.environ["MEMORY_INDEX_TRUNCATION_BYPASS"] = old_bypass


def main() -> int:
    print("memory_index")

    # 1. NEGATIVE CONTROL: a healthy flat index says nothing.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        memo(d, "alpha.md")
        memo(d, "beta.md")
        (d / "MEMORY.md").write_text(
            "# Index\n\n- [Alpha](alpha.md) - hook\n- [Beta](beta.md) - hook\n",
            encoding="utf-8")
        r = report_for(d)
        check("healthy flat index is silent", r == "", r[:200])

    # 2. POSITIVE: an unindexed memo is reported, BY NAME.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        memo(d, "alpha.md")
        memo(d, "ghost.md")
        (d / "MEMORY.md").write_text("# Index\n\n- [Alpha](alpha.md) - hook\n",
                                     encoding="utf-8")
        r = report_for(d)
        check("unreachable memo is reported", r != "")
        check("unreachable memo is named", "ghost.md" in r, r[:200])

    # 3. TWO-TIER: a memo indexed only in tier 2 is NOT reported. This is the
    #    regression that makes a split vault unusable if it is missed.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        memo(d, "alpha.md")
        memo(d, "deep.md")
        (d / "MEMORY.md").write_text(
            "# Index\n\n- [Alpha](alpha.md) - h\n- [More](_index_misc.md) - tier 2\n",
            encoding="utf-8")
        (d / "_index_misc.md").write_text("# Misc\n\n- [Deep](deep.md) - h\n",
                                          encoding="utf-8")
        r = report_for(d)
        check("tier-2-only memo is NOT reported", r == "", r[:300])

    # 4. TWO-TIER negative control: an orphan is still caught when tier 2 exists.
    #    Guards against "accept everything once any _index_ file is present".
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        memo(d, "deep.md")
        memo(d, "ghost.md")
        (d / "MEMORY.md").write_text("# Index\n\n- [More](_index_misc.md) - tier 2\n",
                                     encoding="utf-8")
        (d / "_index_misc.md").write_text("# Misc\n\n- [Deep](deep.md) - h\n",
                                          encoding="utf-8")
        r = report_for(d)
        check("orphan still caught alongside tier 2",
              "ghost.md" in r and "deep.md" not in r, r[:300])

    # 5. OVER-CLIFF: says entries are already gone, not merely at risk.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        memo(d, "alpha.md")
        pad = "\n".join("- [Pad{}](alpha.md) - {}".format(i, "p" * 60)
                        for i in range(600))
        (d / "MEMORY.md").write_text("# Index\n\n- [Alpha](alpha.md) - h\n" + pad,
                                     encoding="utf-8")
        r = report_for(d)
        check("over-cliff is reported", r != "")
        check("over-cliff says NOT being loaded", "NOT being loaded" in r, r[:300])

    # 6. WARN band: over budget, under cliff -> warns without claiming loss.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        memo(d, "alpha.md")
        pad = "\n".join("- [Pad{}](alpha.md) - {}".format(i, "p" * 60)
                        for i in range(250))
        (d / "MEMORY.md").write_text("# Index\n\n- [Alpha](alpha.md) - h\n" + pad,
                                     encoding="utf-8")
        size = (d / "MEMORY.md").stat().st_size
        r = report_for(d)
        in_band = memory_index.WARN_BYTES < size <= memory_index.READ_CLIFF_BYTES
        check("warn band is over budget but under cliff", in_band, "size={}".format(size))
        check("warn band does not claim entries are lost",
              r != "" and "NOT being loaded" not in r, r[:200])

    # 7. BYPASS silences it.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        memo(d, "ghost.md")
        (d / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
        r = report_for(d, {"MEMORY_INDEX_TRUNCATION_BYPASS": "1"})
        check("bypass silences the guard", r == "", r[:200])

    # 8. No MEMORY.md -> silent; not our gap to report.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        memo(d, "alpha.md")
        check("missing MEMORY.md is silent", report_for(d) == "")

    # 9. Nonexistent dir -> silent, never raises.
    check("nonexistent dir is silent",
          report_for(Path("/nonexistent/agent/memory")) == "")

    # 10. END-TO-END through the REAL loader: the warning reaches the payload
    #     the harness actually reads, on stdout. A guard that computed the right
    #     answer but never reached a reader would be green and mute.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        memo(d, "ghost.md")
        (d / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
        env = dict(os.environ, AGENT_MEMORY_DIR=str(d))
        proc = subprocess.run([sys.executable, str(LOADER)], input="{}",
                              capture_output=True, text=True, env=env)
        ok = proc.returncode == 0
        ctx = ""
        try:
            ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        except Exception:
            pass
        check("loader exits 0", ok, proc.stderr[:200])
        check("loader payload carries the warning", "ghost.md" in ctx, ctx[-200:])
        check("loader still carries its own context",
              "SESSION START" in ctx or "SESSION CLOSE" in ctx, ctx[:200])

    # 11. END-TO-END negative control: healthy dir -> loader emits context only.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        memo(d, "alpha.md")
        (d / "MEMORY.md").write_text("# Index\n\n- [Alpha](alpha.md) - h\n",
                                     encoding="utf-8")
        env = dict(os.environ, AGENT_MEMORY_DIR=str(d))
        proc = subprocess.run([sys.executable, str(LOADER)], input="{}",
                              capture_output=True, text=True, env=env)
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        check("healthy dir adds nothing to the loader payload",
              "memory-index" not in ctx, ctx[-200:])

    print("\n{} passed, {} failed".format(PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
