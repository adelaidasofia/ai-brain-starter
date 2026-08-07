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


    # --- regression tests added after adversarial review of 619ee45 ----------

    # 12. ALIAS DEDUPE (measured 208x amplification on a real machine): many
    #     project keys symlink to ONE memory dir. Auditing per-alias meant a
    #     full second per session start and 208 copies of every line.
    #     Mutation killed: removing _dedup_dirs.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        real = root / "projects" / "real" / "memory"
        real.mkdir(parents=True)
        memo(real, "ghost.md")
        (real / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
        for i in range(6):
            alias = root / "projects" / ("alias%d" % i)
            alias.mkdir(parents=True)
            try:
                (alias / "memory").symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                pass
        seen = memory_index._dedup_dirs(
            [d for d in (root / "projects").glob("*/memory") if d.is_dir()])
        check("aliases of one dir collapse to one", len(seen) == 1,
              "got {}".format([str(x) for x in seen]))

    # 13. LINK FORMS: a memo indexed in any plausible markdown spelling is NOT
    #     reported. Mutation killed: narrowing back to the canonical form only.
    forms = [
        "- [A](alpha.md) - h",
        "- [A](./alpha.md) - h",
        "- [A](<alpha.md>) - h",
        '- [A](alpha.md "title") - h',
        "- [A](alpha.md#anchor) - h",
        "[ref]: alpha.md",
        "see `alpha.md` for detail",
    ]
    for form in forms:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            memo(d, "alpha.md")
            (d / "MEMORY.md").write_text("# Index\n\n" + form + "\n", encoding="utf-8")
            r = report_for(d)
            check("link form indexed: {}".format(form[:28]), r == "", r[:160])

    # 14. ORPHAN TIER-2 must NOT mask an unreachable memo. This is mutation A
    #     from the review: trusting every _index_*.md in the dir, linked or not.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        memo(d, "zeta.md")
        (d / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
        (d / "_index_stale.md").write_text("# Stale\n\n- [Z](zeta.md) - h\n",
                                           encoding="utf-8")
        r = report_for(d)
        check("orphaned tier-2 does not mask an unreachable memo",
              "zeta.md" in r or "not linked from MEMORY.md" in r, r[:250])
        check("orphaned tier-2 is itself reported",
              "_index_stale.md" in r, r[:250])

    # 15. TIER-2 SIZE is measured too. The advice says "move entries to tier 2";
    #     if tier 2 is never measured, the advice relocates data into a blind
    #     spot. Mutation killed: size-checking tier 1 only.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        memo(d, "alpha.md")
        (d / "MEMORY.md").write_text(
            "# Index\n\n- [A](alpha.md) - h\n- [More](_index_big.md) - t2\n",
            encoding="utf-8")
        pad = "\n".join("- [P{}](alpha.md) - {}".format(i, "p" * 60) for i in range(600))
        (d / "_index_big.md").write_text("# Big\n\n" + pad, encoding="utf-8")
        r = report_for(d)
        check("oversized tier-2 index is reported",
              "_index_big.md" in r and "read cliff" in r, r[:250])

    # 16. UNREADABLE tier-2 is announced as unknown, NOT as mass-unreachable.
    #     A failed read is not an empty answer.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        memo(d, "deep.md")
        (d / "MEMORY.md").write_text(
            "# Index\n\n- [More](_index_locked.md) - t2\n", encoding="utf-8")
        locked = d / "_index_locked.md"
        locked.write_text("# L\n\n- [D](deep.md) - h\n", encoding="utf-8")
        try:
            os.chmod(locked, 0o000)
            # Probe whether the mode is actually ENFORCED rather than inferring
            # it from the uid. chmod 000 denies reads only where POSIX
            # permissions bite: root bypasses them, and on Windows chmod only
            # toggles the read-only bit so the file stays readable and there is
            # nothing for the guard to report. The old proxy called
            # os.geteuid(), which does not exist on Windows at all -- it raised
            # AttributeError and took the whole suite down with it.
            try:
                locked.read_text(encoding="utf-8")
                denied = False
            except OSError:
                denied = True
            r = report_for(d)
        except Exception:
            r, denied = "", False
        finally:
            try:
                os.chmod(locked, 0o644)
            except Exception:
                pass
        if denied:
            check("unreadable tier-2 is announced as unknown",
                  "could not be READ" in r, r[:250])
        else:
            check("unreadable tier-2 (skipped: file mode not enforced here)", True)

    # 17. MAX_NAMED truncation path is exercised. Mutation killed: corrupting
    #     the "and N more" arithmetic, which no fixture previously reached.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for i in range(memory_index.MAX_NAMED + 5):
            memo(d, "orphan{:02d}.md".format(i))
        (d / "MEMORY.md").write_text("# Index\n", encoding="utf-8")
        r = report_for(d)
        check("truncation says 'and 5 more'", "and 5 more" in r, r[:250])

    # 18. THRESHOLD BOUNDARIES are pinned. Mutation killed: > -> >=.
    def _size_labels(n):
        return " ".join(memory_index._size_problems("X", n))
    check("size == WARN is silent", _size_labels(memory_index.WARN_BYTES) == "")
    check("size == WARN+1 warns",
          "over the" in _size_labels(memory_index.WARN_BYTES + 1))
    check("size == CLIFF is warn, not loss",
          "over the" in _size_labels(memory_index.READ_CLIFF_BYTES))
    check("size == CLIFF+1 is loss",
          "NOT being loaded" in _size_labels(memory_index.READ_CLIFF_BYTES + 1))

    # 19. The closing advice names ONLY the fix that fired. The report used to
    #     recite every remediation this module knows, so an index well under
    #     budget was still told to "trim the oversized index" -- work that is
    #     not needed, on a reading that is not true. Advice that is wrong for
    #     the finding in front of you teaches you to discount the whole
    #     announcement, which is the silence this module exists to prevent.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        memo(d, "alpha.md")
        memo(d, "unindexed.md")
        (d / "MEMORY.md").write_text(
            "# Index\n\n- [A](alpha.md) - h\n", encoding="utf-8")
        r = report_for(d)
        check("unreachable-only names the indexing fix",
              memory_index.FIX_UNREACHABLE in r, r[:250])
        check("unreachable-only does NOT prescribe trimming",
              memory_index.FIX_SIZE not in r and "oversized" not in r, r[:250])

    # 20. ...and the converse, so 19 cannot pass by the advice going silent.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        memo(d, "alpha.md")
        pad = "\n".join("- [P{}](alpha.md) - {}".format(i, "p" * 60)
                        for i in range(600))
        (d / "MEMORY.md").write_text(
            "# Index\n\n- [A](alpha.md) - h\n" + pad, encoding="utf-8")
        r = report_for(d)
        check("oversized-only names the split fix",
              memory_index.FIX_SIZE in r, r[:250])
        check("oversized-only does NOT prescribe indexing memories",
              memory_index.FIX_UNREACHABLE not in r, r[:250])

    print("\n{} passed, {} failed".format(PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
