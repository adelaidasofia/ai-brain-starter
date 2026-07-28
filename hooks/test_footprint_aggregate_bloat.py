#!/usr/bin/env python3
"""Negative + positive controls for the AGGREGATE bloat-dir signal (MYC-577).

The bug this closes: `worktree-footprint-signal.py` observed worktree COUNT
only, so the non-worktree half of the 2026-06-06 ~234K-file melt (.smart-env
regrowth above all) was invisible until the machine fell over. A signal that
silently stopped covering the aggregate looks exactly like a healthy machine,
so the controls here assert BOTH directions:

  * over threshold with ZERO worktrees          -> fires, names the dir, gives a remedy
  * under threshold                             -> silent
  * a dir added via WORKTREE_FOOTPRINT_BLOAT_DIRS -> counted
  * second run inside the TTL                   -> reuses the cache (no re-walk)
  * cached over-threshold reading                -> STILL warns (the cap bounds
    the walk, not the signal — a capped signal that goes quiet between walks
    would reintroduce the invisible-until-catastrophic failure)
  * symlink out of the tree                     -> not followed (the vault
    commonly links back out; following turns a bounded walk into a cycle)

Run: python3 hooks/test_footprint_aggregate_bloat.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parent


def _load():
    """Import the hook by path (its filename has dashes, so no plain import)."""
    spec = importlib.util.spec_from_file_location(
        "worktree_footprint_signal", HOOKS / "worktree-footprint-signal.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_files(d: Path, n: int) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"f{i}").write_text("x", encoding="utf-8")


def _env(tmp: Path, **over) -> None:
    os.environ["WORKTREE_FOOTPRINT_CACHE"] = str(tmp / "cache.json")
    os.environ.pop("WORKTREE_FOOTPRINT_BLOAT_DIRS", None)
    os.environ["WORKTREE_FOOTPRINT_BLOAT_TTL_H"] = "24"
    for k, v in over.items():
        os.environ[k] = str(v)


def main() -> int:
    mod = _load()
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # --- POSITIVE: over threshold, zero worktrees ------------------------
        vault = tmp / "over"
        (vault / ".git").mkdir(parents=True)          # a brain vault
        _make_files(vault / ".smart-env", 60)         # the non-worktree source
        _env(tmp, WORKTREE_FOOTPRINT_FILE_WARN=50)
        sig = mod.bloat_signal(vault)
        check(sig, "over-threshold aggregate did NOT fire (the MYC-577 bug)")
        check(".smart-env" in sig, f"warning names no offending dir: {sig!r}")
        check("worktree-prune" in sig or "rm -rf" in sig,
              f"warning carries no remedy: {sig!r}")
        # ...and it fired with no worktrees at all, which is the whole point.
        check(not (vault / ".claude" / "worktrees").exists(),
              "fixture accidentally created worktrees; the control is void")

        # --- NEGATIVE: under threshold stays silent --------------------------
        under = tmp / "under"
        (under / ".git").mkdir(parents=True)
        _make_files(under / ".smart-env", 10)
        _env(tmp, WORKTREE_FOOTPRINT_FILE_WARN=5000)
        check(mod.bloat_signal(under) == "",
              "under-threshold vault warned (cry-wolf: the signal would be ignored)")

        # --- configurable extra dir is counted -------------------------------
        extra = tmp / "extra"
        (extra / ".git").mkdir(parents=True)
        _make_files(extra / "custom-cache", 60)
        _env(tmp, WORKTREE_FOOTPRINT_FILE_WARN=50)
        check(mod.bloat_signal(extra) == "",
              "an unlisted dir was counted (the dir list is not an allow-list)")
        _env(tmp, WORKTREE_FOOTPRINT_FILE_WARN=50,
             WORKTREE_FOOTPRINT_BLOAT_DIRS="custom-cache")
        os.unlink(tmp / "cache.json")  # force a fresh walk, not the prior verdict
        check("custom-cache" in mod.bloat_signal(extra),
              "WORKTREE_FOOTPRINT_BLOAT_DIRS did not extend the counted set")

        # --- frequency cap: second call inside TTL does not re-walk ----------
        capd = tmp / "capped"
        (capd / ".git").mkdir(parents=True)
        _make_files(capd / ".smart-env", 60)
        _env(tmp, WORKTREE_FOOTPRINT_FILE_WARN=50)
        os.unlink(tmp / "cache.json")
        first = mod.bloat_reading(capd)
        walked = []
        real_measure = mod._measure_bloat
        mod._measure_bloat = lambda *a, **k: (walked.append(1), real_measure(*a, **k))[1]
        second = mod.bloat_reading(capd)
        mod._measure_bloat = real_measure
        check(not walked,
              "a second SessionStart inside the TTL re-walked the tree "
              "(the cap is what keeps this cheap on a fragile machine)")
        check(second.get("total") == first.get("total"),
              "cached reading disagrees with the fresh one")

        # --- a CACHED over-threshold reading still warns ---------------------
        # The cap bounds the WALK. If it also silenced the SIGNAL, a melted
        # machine would be told once and then go quiet — the exact
        # invisible-until-catastrophic failure this ticket closes.
        check(mod.bloat_signal(capd) != "",
              "cached over-threshold reading went silent between walks")

        # --- symlinks are not followed ---------------------------------------
        linked = tmp / "linked"
        (linked / ".git").mkdir(parents=True)
        (linked / ".smart-env").mkdir(parents=True)
        big = tmp / "outside"
        _make_files(big, 60)
        try:
            (linked / ".smart-env" / "out").symlink_to(big, target_is_directory=True)
        except (OSError, NotImplementedError):
            pass  # no symlink support (Windows without dev mode): control skipped
        else:
            _env(tmp, WORKTREE_FOOTPRINT_FILE_WARN=50)
            os.unlink(tmp / "cache.json")
            check(mod.bloat_signal(linked) == "",
                  "the walk followed a symlink out of the vault (unbounded / cyclic)")

    if failures:
        print("FAILED — aggregate bloat signal (MYC-577):")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("OK - aggregate bloat signal: fires over threshold with zero worktrees, "
          "silent under, extendable, frequency-capped, still warns from cache, "
          "does not follow symlinks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
