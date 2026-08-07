"""Cloud-safe, bounded reads for filesystem walkers.

Recursive scanners must assume that an ordinary-looking path can be a cloud
placeholder, a stalled mount, or a special file.  ``safe_read_bytes`` makes the
caller-side guarantee explicit:

* only regular files are opened;
* detectable macOS dataless and Windows offline placeholders are skipped;
* size and wall-clock limits bound each read; and
* at most ``MAX_LINGERING_WORKERS`` timed-out daemon workers can remain alive.

The worker cap matters because Python cannot cancel a thread blocked inside an
OS read.  Once every slot is occupied, later calls fail fast instead of creating
an unbounded thread pile-up.  Path/provider denylists may still be useful as an
optimization, but this primitive is the provider-independent safety boundary.

Pure stdlib and Python 3.9 compatible.
"""
from __future__ import annotations

import math
import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


DEFAULT_TIMEOUT_S = 5.0
DEFAULT_MAX_BYTES = 1_000_000
MAX_TIMEOUT_S = 3_600.0
MAX_LINGERING_WORKERS = 4
_READ_CHUNK = 64 * 1024

_READ_SLOTS = threading.BoundedSemaphore(MAX_LINGERING_WORKERS)
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_WORKERS = 0
_TIMED_OUT_WORKERS = 0


@dataclass(frozen=True)
class SafeBytesRead:
    """Result of a bounded byte read.  ``data`` exists only when status is ok."""

    path: Path
    status: str
    data: bytes | None = None
    detail: str = ""
    mode: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.data is not None


@dataclass(frozen=True)
class SafeTextRead:
    """Decoded text result with the same status taxonomy as ``SafeBytesRead``."""

    path: Path
    status: str
    text: str | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.text is not None


def active_read_workers() -> int:
    """Current worker count, exposed for health checks and negative controls."""
    with _ACTIVE_LOCK:
        return _ACTIVE_WORKERS


def lingering_read_workers() -> int:
    """Timed-out workers still blocked in the OS."""
    with _ACTIVE_LOCK:
        return _TIMED_OUT_WORKERS


def _offline_reason(st: os.stat_result) -> str | None:
    """Return the detectable provider-placeholder class, if any."""
    flags = int(getattr(st, "st_flags", 0) or 0)
    dataless = int(getattr(stat, "SF_DATALESS", 0) or 0)
    if dataless and flags & dataless:
        return "macos-dataless"

    attrs = int(getattr(st, "st_file_attributes", 0) or 0)
    offline = int(getattr(stat, "FILE_ATTRIBUTE_OFFLINE", 0) or 0)
    if offline and attrs & offline:
        return "windows-offline"
    return None


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    """Best-effort race check between lstat and the opened descriptor."""
    before_ino = int(getattr(before, "st_ino", 0) or 0)
    after_ino = int(getattr(after, "st_ino", 0) or 0)
    if before_ino and after_ino and before_ino != after_ino:
        return False
    before_dev = int(getattr(before, "st_dev", 0) or 0)
    after_dev = int(getattr(after, "st_dev", 0) or 0)
    if before_dev and after_dev and before_dev != after_dev:
        return False
    return True


def _read_once(path: Path, max_bytes: int) -> SafeBytesRead:
    """Worker-side stat/open/read.  The public wrapper supplies the deadline."""
    try:
        before = os.lstat(str(path))
    except FileNotFoundError:
        return SafeBytesRead(path, "missing")
    except OSError as exc:
        return SafeBytesRead(path, "error", detail=exc.__class__.__name__)

    if not stat.S_ISREG(before.st_mode):
        return SafeBytesRead(path, "not-regular", detail=stat.filemode(before.st_mode))
    offline = _offline_reason(before)
    if offline:
        return SafeBytesRead(path, "offline-placeholder", detail=offline)
    if before.st_size > max_bytes:
        return SafeBytesRead(path, "too-large", detail=str(before.st_size))

    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0) or 0)
    flags |= int(getattr(os, "O_NONBLOCK", 0) or 0)
    flags |= int(getattr(os, "O_NOFOLLOW", 0) or 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        return SafeBytesRead(path, "error", detail=exc.__class__.__name__)

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            return SafeBytesRead(path, "not-regular", detail=stat.filemode(opened.st_mode))
        offline = _offline_reason(opened)
        if offline:
            return SafeBytesRead(path, "offline-placeholder", detail=offline)
        if not _same_file(before, opened):
            return SafeBytesRead(path, "changed", detail="path changed before open")
        if opened.st_size > max_bytes:
            return SafeBytesRead(path, "too-large", detail=str(opened.st_size))

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(_READ_CHUNK, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                return SafeBytesRead(path, "too-large", detail=f">{max_bytes}")

        after = os.fstat(fd)
        if after.st_size != opened.st_size or getattr(after, "st_mtime_ns", None) != getattr(
            opened, "st_mtime_ns", None
        ):
            return SafeBytesRead(path, "changed", detail="file changed during read")
        return SafeBytesRead(
            path,
            "ok",
            data=b"".join(chunks),
            mode=opened.st_mode,
        )
    except OSError as exc:
        return SafeBytesRead(path, "error", detail=exc.__class__.__name__)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def safe_read_bytes(
    path: str | Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
    _reader: Callable[[Path, int], SafeBytesRead] | None = None,
) -> SafeBytesRead:
    """Read one regular file without blocking the caller past ``timeout``.

    ``busy`` means the process already has the maximum number of timed-out
    workers still blocked in the OS.  The private reader seam exists so the
    timeout/cap negative controls do not need a real stalled network mount.
    """
    global _TIMED_OUT_WORKERS
    p = Path(path)
    try:
        timeout_value = float(timeout)
    except (OverflowError, TypeError, ValueError):
        return SafeBytesRead(p, "invalid-limit")
    if (
        isinstance(timeout, bool)
        or not math.isfinite(timeout_value)
        or timeout_value <= 0
        or timeout_value > MAX_TIMEOUT_S
        or isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes < 0
    ):
        return SafeBytesRead(p, "invalid-limit")
    timeout = timeout_value
    started = time.monotonic()
    with _ACTIVE_LOCK:
        saturated_by_timeouts = _TIMED_OUT_WORKERS >= MAX_LINGERING_WORKERS
    if saturated_by_timeouts:
        return SafeBytesRead(p, "busy", detail="lingering-worker-cap")
    # Queue behind healthy concurrent reads for the caller's remaining budget.
    # Once four callers have actually timed out, the check above fails fast.
    # Waiting less than the declared deadline would silently skip healthy slow
    # files and turn the safety boundary into a false-negative generator.
    if not _READ_SLOTS.acquire(timeout=timeout):
        return SafeBytesRead(p, "busy", detail="active-worker-cap")

    box: dict[str, SafeBytesRead] = {}
    state = {"done": False, "timed_out": False}
    reader = _reader or _read_once

    def _worker() -> None:
        global _ACTIVE_WORKERS, _TIMED_OUT_WORKERS
        with _ACTIVE_LOCK:
            _ACTIVE_WORKERS += 1
        try:
            box["result"] = reader(p, max_bytes)
        except Exception as exc:  # a safety boundary never propagates worker faults
            box["result"] = SafeBytesRead(p, "error", detail=exc.__class__.__name__)
        finally:
            with _ACTIVE_LOCK:
                state["done"] = True
                if state["timed_out"]:
                    _TIMED_OUT_WORKERS -= 1
                _ACTIVE_WORKERS -= 1
            _READ_SLOTS.release()

    worker = threading.Thread(target=_worker, name="cloud-safe-read", daemon=True)
    try:
        worker.start()
    except Exception as exc:
        _READ_SLOTS.release()
        return SafeBytesRead(p, "error", detail=exc.__class__.__name__)
    remaining = max(0.0, timeout - (time.monotonic() - started))
    worker.join(remaining)
    with _ACTIVE_LOCK:
        if not state["done"]:
            state["timed_out"] = True
            _TIMED_OUT_WORKERS += 1
            timed_out = True
        else:
            timed_out = False
    if timed_out:
        return SafeBytesRead(p, "timeout", detail=f">{timeout:g}s")
    return box.get("result", SafeBytesRead(p, "error", detail="worker-no-result"))


def safe_read_text(
    path: str | Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
    encoding: str = "utf-8",
    errors: str = "strict",
    skip_binary: bool = True,
    _reader: Callable[[Path, int], SafeBytesRead] | None = None,
) -> SafeTextRead:
    """Bounded regular-file read plus text decoding and optional binary skip."""
    result = safe_read_bytes(
        path, timeout=timeout, max_bytes=max_bytes, _reader=_reader
    )
    if not result.ok:
        return SafeTextRead(result.path, result.status, detail=result.detail)
    data = result.data or b""
    if skip_binary and b"\x00" in data[:4096]:
        return SafeTextRead(result.path, "binary")
    try:
        return SafeTextRead(result.path, "ok", text=data.decode(encoding, errors))
    except (LookupError, UnicodeDecodeError) as exc:
        return SafeTextRead(result.path, "decode-error", detail=exc.__class__.__name__)
