#!/usr/bin/env python3
"""Negative controls for the shared cloud-safe regular-file reader."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib.safe_read import (  # noqa: E402
    MAX_LINGERING_WORKERS,
    SafeBytesRead,
    _offline_reason,
    active_read_workers,
    lingering_read_workers,
    safe_read_bytes,
    safe_read_text,
)


FAILURES: list[str] = []


def check(label: str, condition: bool) -> None:
    print("PASS" if condition else "FAIL", label)
    if not condition:
        FAILURES.append(label)


def wait_for_workers(expected: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if active_read_workers() == expected:
            return True
        time.sleep(0.01)
    return active_read_workers() == expected


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="safe-read-") as tmp:
        root = Path(tmp)
        clean = root / "clean.txt"
        clean.write_text("bounded regular content\n", encoding="utf-8")

        result = safe_read_text(clean, timeout=1.0, max_bytes=1024)
        check("clean regular text is returned", result.ok and result.text == "bounded regular content\n")
        bytes_result = safe_read_bytes(clean, timeout=1.0, max_bytes=1024)
        check(
            "descriptor-observed source mode travels with bounded bytes",
            bytes_result.ok
            and bytes_result.mode is not None
            and stat.S_ISREG(bytes_result.mode),
        )
        with ThreadPoolExecutor(max_workers=12) as pool:
            burst = list(pool.map(lambda _n: safe_read_bytes(clean, timeout=1.0), range(12)))
        check("ordinary concurrent reads queue without false busy skips", all(r.ok for r in burst))

        healthy_release = threading.Event()

        def healthy_slow(path: Path, max_bytes: int) -> SafeBytesRead:
            healthy_release.wait(1.0)
            return SafeBytesRead(path, "ok", data=b"healthy")

        with ThreadPoolExecutor(max_workers=5) as pool:
            first = [pool.submit(safe_read_bytes, clean, timeout=1.0, _reader=healthy_slow)
                     for _ in range(MAX_LINGERING_WORKERS)]
            check("four healthy slow readers occupy the bounded pool", wait_for_workers(4))
            fifth = pool.submit(safe_read_bytes, clean, timeout=1.0, _reader=healthy_slow)
            time.sleep(0.4)
            healthy_release.set()
            slow_burst = [f.result() for f in first] + [fifth.result()]
        check("healthy 400ms reads honor the declared 1s timeout", all(r.ok for r in slow_burst))

        large = root / "large.txt"
        large.write_bytes(b"x" * 12)
        check("oversized source is skipped", safe_read_bytes(large, max_bytes=8).status == "too-large")
        invalid_limits = (
            safe_read_bytes(clean, timeout=float("inf")),
            safe_read_bytes(clean, timeout=float("nan")),
            safe_read_bytes(clean, timeout=10**1000),
            safe_read_bytes(clean, max_bytes=1.5),
            safe_read_bytes(clean, max_bytes=True),
        )
        check(
            "non-finite and non-integral limits fail closed without starting workers",
            all(result.status == "invalid-limit" for result in invalid_limits)
            and active_read_workers() == 0,
        )

        binary = root / "binary.dat"
        binary.write_bytes(b"abc\x00def")
        check("text helper skips binary content", safe_read_text(binary).status == "binary")

        if hasattr(os, "mkfifo"):
            fifo = root / "blocker"
            os.mkfifo(str(fifo))
            started = time.monotonic()
            fifo_result = safe_read_bytes(fifo, timeout=0.2)
            elapsed = time.monotonic() - started
            check("FIFO is rejected as non-regular", fifo_result.status == "not-regular")
            check("FIFO negative control returns promptly", elapsed < 1.0)
        else:
            check("FIFO unavailable on this platform", True)

        release = threading.Event()

        def stalled(path: Path, max_bytes: int) -> SafeBytesRead:
            release.wait(5.0)
            return SafeBytesRead(path, "ok", data=b"eventually")

        started = time.monotonic()
        timed = safe_read_bytes(clean, timeout=0.05, _reader=stalled)
        elapsed = time.monotonic() - started
        check("stalled read returns timeout", timed.status == "timeout")
        check("timeout is a hard caller-side bound", elapsed < 0.5)
        release.set()
        check("timed-out worker releases its slot when unstuck", wait_for_workers(0))

        release.clear()
        timed_results = [
            safe_read_bytes(clean, timeout=0.02, _reader=stalled)
            for _ in range(MAX_LINGERING_WORKERS)
        ]
        check("negative control fills only the fixed worker budget",
              all(r.status == "timeout" for r in timed_results)
              and wait_for_workers(MAX_LINGERING_WORKERS)
              and lingering_read_workers() == MAX_LINGERING_WORKERS)
        overflow = safe_read_bytes(clean, timeout=0.02, _reader=stalled)
        check("additional reads fail fast at lingering-worker cap", overflow.status == "busy")
        check("lingering worker count never exceeds cap", active_read_workers() <= MAX_LINGERING_WORKERS)
        release.set()
        check("all synthetic stalled workers drain", wait_for_workers(0))
        check("lingering timeout count drains to zero", lingering_read_workers() == 0)

        dataless = int(getattr(stat, "SF_DATALESS", 0) or 0)
        if dataless:
            fake = SimpleNamespace(st_flags=dataless, st_file_attributes=0)
            check("macOS dataless flag is detectable", _offline_reason(fake) == "macos-dataless")
        else:
            check("macOS dataless flag unavailable on this platform", True)

        offline = int(getattr(stat, "FILE_ATTRIBUTE_OFFLINE", 0) or 0)
        if offline:
            fake = SimpleNamespace(st_flags=0, st_file_attributes=offline)
            check("Windows offline flag is detectable", _offline_reason(fake) == "windows-offline")
        else:
            check("Windows offline flag unavailable on this platform", True)

    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
