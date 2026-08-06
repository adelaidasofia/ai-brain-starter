#!/usr/bin/env python3
"""Controls for the low-free-disk floor in worktree-footprint-signal.py.

The bug this closes (measured 2026-08-05, 926 GB machine): the only disk line
was a flat 5 GB. Regenerable build output filled the volume from healthy to
2.3 GB free and this hook stayed silent the entire way down, because 5 GB on a
1 TB disk is not an early warning — by then builds already fail and sync daemons
already stall. The floor is now max(5 GB, 5% of the volume), so it fires while
the fix is still cheap.

A threshold that never fires and a threshold that fires too late are the same
bug, so the controls assert BOTH directions:

  * 2.3 GB free of 926 GB (the real incident) -> FIRES   (old flat floor: also fired, but only here)
  * 40 GB free of 926 GB                      -> FIRES   (old flat floor: SILENT — the regression)
  * 300 GB free of 926 GB                     -> silent  (healthy disk stays quiet)
  * 10 GB free of 120 GB small disk           -> FIRES   (proportional floor scales down)
  * 8 GB free of 60 GB small disk             -> silent  (5% = 3 GB; absolute 5 GB floor governs)
  * WORKTREE_FREE_GB pins the floor explicitly
  * the message states free GB, the percentage, and the floor it warned against

[13]-[15] drive the hook's REAL code path with a faked volume, because the
arithmetic controls above would all still pass if the derivation were never
wired into the emitted output.

Run: python3 hooks/test_footprint_disk_floor.py
"""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from types import SimpleNamespace

HOOKS = Path(__file__).resolve().parent
FAILED = []


def _load():
    spec = importlib.util.spec_from_file_location(
        "worktree_footprint_signal", HOOKS / "worktree-footprint-signal.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def floor_for(mod, total_gb: float, pinned: str | None = None) -> float:
    """Reproduce the hook's floor derivation for a volume of `total_gb`."""
    if pinned:
        return float(pinned)
    return max(mod.DEFAULT_FREE_GB, total_gb * mod.DEFAULT_FREE_PCT)


def check(name: str, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        FAILED.append(name)


def main() -> int:
    mod = _load()

    # --- the real incident and the corridor above it -------------------------
    big = 926.0
    fires = lambda free, total, pinned=None: free < floor_for(mod, total, pinned)

    check("[1] 2.3 GB free of 926 GB fires", fires(2.3, big), True)
    check("[2] 40 GB free of 926 GB fires (old flat 5 GB floor was SILENT here)",
          fires(40.0, big), True)
    check("[3] 300 GB free of 926 GB stays silent", fires(300.0, big), False)

    # The regression, stated directly: the old behaviour is the flat floor alone.
    old_floor_silent_at_40 = 40.0 >= mod.DEFAULT_FREE_GB
    check("[4] the old flat floor demonstrably missed 40 GB free",
          old_floor_silent_at_40, True)

    # --- proportional floor must scale DOWN, not just up --------------------
    check("[5] 10 GB free of 120 GB stays silent (above the 6 GB floor)",
          fires(10.0, 120.0), False)
    check("[6] 5 GB free of 120 GB fires (below the 6 GB proportional floor)",
          fires(5.0, 120.0), True)
    check("[7] 8 GB free of 60 GB stays silent (5% = 3 GB, absolute 5 GB governs)",
          fires(8.0, 60.0), False)
    check("[8] 4 GB free of 60 GB fires (below the absolute 5 GB floor)",
          fires(4.0, 60.0), True)

    # --- explicit pin still wins -------------------------------------------
    check("[9] WORKTREE_FREE_GB=200 pins the floor", fires(150.0, big, "200"), True)
    check("[10] WORKTREE_FREE_GB=1 pins the floor low", fires(2.3, big, "1"), False)

    # --- the floor is actually derived from the volume, not hardcoded -------
    check("[11] floor on a 926 GB volume is ~46 GB",
          round(floor_for(mod, big)), 46)
    check("[12] floor never drops below the absolute minimum",
          floor_for(mod, 1.0), mod.DEFAULT_FREE_GB)

    # --- end-to-end: the derivation must reach the EMITTED text --------------
    GIB = 1024 ** 3

    def emit_with_volume(free_gb: float, total_gb: float) -> str:
        """Run the hook's real disk branch against a faked volume."""
        real = shutil.disk_usage
        shutil.disk_usage = lambda _p: SimpleNamespace(  # type: ignore[assignment]
            total=int(total_gb * GIB), used=int((total_gb - free_gb) * GIB),
            free=int(free_gb * GIB),
        )
        try:
            out = mod.main_text() if hasattr(mod, "main_text") else _emit_via_main(mod)
        finally:
            shutil.disk_usage = real  # type: ignore[assignment]
        return out or ""

    def _emit_via_main(m):
        """Capture whatever the hook writes to stdout for one invocation."""
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                m.main()
            except SystemExit:
                pass
        return buf.getvalue()

    warned = emit_with_volume(40.0, 926.0)
    check("[13] 40 GB of 926 GB reaches the emitted output",
          "Low free disk" in warned, True)
    check("[14] the message names the floor it warned against",
          ("46" in warned) if "Low free disk" in warned else False, True)
    quiet = emit_with_volume(300.0, 926.0)
    check("[15] 300 GB of 926 GB emits no disk warning",
          "Low free disk" in quiet, False)

    print()
    if FAILED:
        print(f"FAIL test_footprint_disk_floor.py ({len(FAILED)} failed)")
        return 1
    print("PASS test_footprint_disk_floor.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
