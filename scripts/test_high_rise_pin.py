#!/usr/bin/env python3
"""Drift gate for the vendored High-Rise framework.

The framework files under vendor/high-rise/ are pinned copies of upstream
(Fundacion-Lontananza/high-rise), byte-identical to the tagged release recorded
in vendor/high-rise/PIN.json. They must never be hand-edited: a local patch
would silently fork the framework ai-brain-starter claims to merely consume.

This runs the real offline drift guard (scripts/sync-high-rise.py --check) on
the committed vendored files, and a NEGATIVE control proving that guard returns
non-zero when a vendored file is tampered.

Auto-discovered by scripts/ci.sh via the scripts/test_*.py glob.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYNC = ROOT / "scripts" / "sync-high-rise.py"


def _load_sync():
    spec = importlib.util.spec_from_file_location("sync_high_rise", SYNC)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def positive_real_check() -> bool:
    """The committed vendored files must match the pin (real --check exits 0)."""
    res = subprocess.run(
        [sys.executable, str(SYNC), "--check"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        print("FAIL: sync-high-rise.py --check reported drift on committed vendor/:")
        print((res.stdout + res.stderr).strip())
        return False
    print("OK (positive): committed vendored files match PIN.json")
    return True


def negative_control() -> bool:
    """A tampered vendored file MUST make check() return 2 (guard is load-bearing).

    Redirects the module's paths at a temp tree so the real vendor/ is untouched.
    """
    mod = _load_sync()
    with tempfile.TemporaryDirectory(prefix="high-rise-pin-test-") as tmp:
        vendor = Path(tmp) / "high-rise"
        (vendor / "methodology").mkdir(parents=True)
        # Lay down the exact vendored file set with real content...
        good = {}
        for rel in mod.VENDORED_FILES:
            content = f"canonical stand-in for {rel}\n".encode()
            (vendor / rel).write_bytes(content)
            good[rel] = hashlib.sha256(content).hexdigest()
        pin = {"repo": mod.UPSTREAM_SLUG, "tag": "v0.0.0-test",
               "commit": "0" * 40, "files": good}
        (vendor / "PIN.json").write_text(json.dumps(pin), encoding="utf-8")

        # ...then TAMPER one file so its sha no longer matches the pin.
        tampered = mod.VENDORED_FILES[0]
        (vendor / tampered).write_bytes(b"a local hand-edit that forks the framework\n")

        orig_vendor, orig_pin = mod.VENDOR_DIR, mod.PIN_FILE
        try:
            mod.VENDOR_DIR = vendor
            mod.PIN_FILE = vendor / "PIN.json"
            rc = mod.check()
        finally:
            mod.VENDOR_DIR, mod.PIN_FILE = orig_vendor, orig_pin

    if rc == 0:
        print("FAIL (negative control): --check passed a TAMPERED vendored file — the guard is not load-bearing")
        return False
    print("OK (negative): --check returns non-zero on a tampered vendored file")
    return True


def main() -> int:
    ok = positive_real_check()
    ok = negative_control() and ok
    if not ok:
        return 1
    print("OK: vendored High-Rise pin is intact and the drift guard is load-bearing")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
