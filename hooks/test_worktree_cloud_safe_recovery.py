#!/usr/bin/env python3
"""Focused controls for bounded worktree-recovery reads and raw Git paths.

These tests intentionally do not claim concurrent-writer exclusion. The existing
cleanup lifecycle is unchanged; this suite proves only the MYC-673 boundary:
candidate content is read through safe_read, uncertainty refuses cleanup, Git
never reopens candidate paths for hashing, and -z paths round-trip losslessly.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))
# Reaper artifacts now resolve to the REAL vault (see _lib.worktree_safety.artifact_base),
# so redirect them at import time — otherwise this suite writes its fixtures into the
# user's actual vault. Set before importing anything that resolves a snapshot path.
os.environ.setdefault("WORKTREE_ARTIFACT_ROOT", tempfile.mkdtemp(prefix="wt-artifacts-"))


from _lib import worktree_safety as safety  # noqa: E402
from _lib.safe_read import SafeBytesRead  # noqa: E402


FAILURES: list[str] = []


def check(label: str, condition: bool) -> None:
    print("PASS" if condition else "FAIL", label)
    if not condition:
        FAILURES.append(label)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )


def make_main() -> Path:
    root = Path(tempfile.mkdtemp(prefix="recovery-safe-"))
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "test")
    (root / "tracked.md").write_text("tracked\n", encoding="utf-8")
    git(root, "add", "tracked.md")
    git(root, "commit", "-qm", "initial")
    (root / ".claude" / "worktrees").mkdir(parents=True)
    return root


def add_worktree(main: Path, slug: str, branch: str | None = None) -> Path:
    worktree = main / ".claude" / "worktrees" / slug
    result = git(
        main,
        "worktree",
        "add",
        "-q",
        "-b",
        branch or f"claude/test-{time.time_ns()}",
        str(worktree),
        "HEAD",
    )
    assert result.returncode == 0, result.stderr
    return worktree


def main() -> int:
    repo = make_main()
    worktree = add_worktree(repo, "regular")
    unique = worktree / "UNSAVED.md"
    unique.write_text("unique bytes\n", encoding="utf-8")
    os.chmod(unique, 0o600)
    snapped, _recoverable, all_safe = safety.snapshot_unrecoverable(
        repo, worktree, "regular"
    )
    snapshot = safety.snapshot_dir_for(repo) / "regular" / "UNSAVED.md"
    check(
        "regular unique bytes are surfaced from the bounded read",
        all_safe and snapped == 1 and snapshot.read_bytes() == b"unique bytes\n",
    )
    check(
        "recovery copy preserves private source mode",
        stat.S_IMODE(snapshot.stat().st_mode) == 0o600,
    )
    if hasattr(os, "fchmod"):
        observed_before_fchmod: list[int] = []
        original_fchmod = os.fchmod

        def inspect_fchmod(fd: int, mode: int) -> None:
            observed_before_fchmod.append(stat.S_IMODE(os.fstat(fd).st_mode))
            original_fchmod(fd, mode)

        safety.os.fchmod = inspect_fchmod
        private_copy = safety.snapshot_dir_for(repo) / "mode-window" / "secret.bin"
        try:
            wrote_private = safety._write_recovery_copy(
                private_copy,
                b"private bytes",
                stat.S_IFREG | 0o600,
            )
        finally:
            safety.os.fchmod = original_fchmod
        check(
            "private snapshot temp is 0600 before bytes become final",
            wrote_private
            and observed_before_fchmod == [0o600]
            and private_copy.read_bytes() == b"private bytes",
        )
    else:
        check("descriptor mode-window control unavailable on this platform", True)

    committed_copy = worktree / "copy.md"
    committed_copy.write_text("tracked\n", encoding="utf-8")
    scan = safety._recovery_scan(repo, [committed_copy])
    check(
        "local blob hash recognizes content already in Git",
        not scan.unique and not scan.unsafe and scan.recoverable == 1,
    )
    scan = safety._recovery_scan(
        repo,
        [worktree / f"candidate-{index}" for index in range(safety.RECOVERY_MAX_CANDIDATES + 1)],
    )
    check(
        "aggregate candidate cap refuses before starting per-file reads",
        bool(scan.unsafe) and scan.unsafe[0][1] == "candidate-cap",
    )
    original_scandir = safety.os.scandir
    original_candidate_cap = safety.RECOVERY_MAX_CANDIDATES
    yielded = 0

    class FakeEntry:
        def __init__(self, index: int) -> None:
            self.name = f"file-{index}.md"
            self.path = str(worktree / self.name)

        def is_dir(self, *, follow_symlinks: bool = True) -> bool:
            return False

    class FakeScan:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def __iter__(self):
            nonlocal yielded
            for index in range(100):
                yielded += 1
                yield FakeEntry(index)

    safety.os.scandir = lambda _root: FakeScan()
    safety.RECOVERY_MAX_CANDIDATES = 3
    try:
        bounded_files = safety._bounded_recovery_files(worktree)
    finally:
        safety.os.scandir = original_scandir
        safety.RECOVERY_MAX_CANDIDATES = original_candidate_cap
    check(
        "disconnected enumeration stops while consuming the cap, not after full materialization",
        bounded_files is None and yielded == 4,
    )
    original_read = safety.safe_read_bytes
    original_candidate_cap = safety.RECOVERY_MAX_CANDIDATES
    original_total_cap = safety.RECOVERY_MAX_TOTAL_BYTES
    original_file_cap = safety.RECOVERY_MAX_FILE_BYTES
    limits: list[int] = []

    def two_byte_read(path: str | Path, **kwargs) -> SafeBytesRead:
        limits.append(kwargs["max_bytes"])
        return SafeBytesRead(Path(path), "ok", data=b"xx")

    safety.safe_read_bytes = two_byte_read
    safety.RECOVERY_MAX_CANDIDATES = 10
    safety.RECOVERY_MAX_TOTAL_BYTES = 3
    safety.RECOVERY_MAX_FILE_BYTES = 100
    try:
        scan = safety._recovery_scan(
            repo,
            [worktree / f"budget-{index}" for index in range(10)],
        )
    finally:
        safety.safe_read_bytes = original_read
        safety.RECOVERY_MAX_CANDIDATES = original_candidate_cap
        safety.RECOVERY_MAX_TOTAL_BYTES = original_total_cap
        safety.RECOVERY_MAX_FILE_BYTES = original_file_cap
    check(
        "aggregate byte budget narrows the next read and stops immediately",
        limits == [3, 1]
        and bool(scan.unsafe)
        and scan.unsafe[-1][1] == "recovery-total-cap",
    )
    nongit = Path(tempfile.mkdtemp(prefix="not-a-worktree-"))
    check(
        "Git status failure is unknown and refuses snapshot authorization",
        safety.snapshot_unrecoverable(repo, nongit, "nongit") == (0, 0, False),
    )
    shutil.rmtree(nongit, ignore_errors=True)
    shutil.rmtree(repo, ignore_errors=True)

    if hasattr(os, "mkfifo"):
        repo = make_main()
        worktree = add_worktree(repo, "fifo")
        fifo = worktree / "blocked-source"
        os.mkfifo(str(fifo))
        started = time.monotonic()
        scan = safety._recovery_scan(repo, [fifo])
        check(
            "FIFO candidate makes the bounded recovery boundary uncertain",
            bool(scan.unsafe) and scan.unsafe[0][1] == "not-regular",
        )
        check("FIFO recovery returns promptly", time.monotonic() - started < 1.0)
        check("FIFO source remains untouched", stat.S_ISFIFO(fifo.lstat().st_mode))
        shutil.rmtree(repo, ignore_errors=True)
    else:
        check("FIFO recovery control unavailable on this platform", True)

    repo = make_main()
    worktree = add_worktree(repo, "timeout")
    stalled = worktree / "stalled.md"
    stalled.write_text("not materialized\n", encoding="utf-8")
    original_read = safety.safe_read_bytes

    def force_timeout(path: str | Path, **kwargs) -> SafeBytesRead:
        candidate = Path(path)
        if candidate.name == "stalled.md":
            return SafeBytesRead(candidate, "timeout", detail=">0.01s")
        return original_read(path, **kwargs)

    safety.safe_read_bytes = force_timeout
    try:
        _snapped, _recoverable, all_safe = safety.snapshot_unrecoverable(
            repo, worktree, "timeout"
        )
    finally:
        safety.safe_read_bytes = original_read
    check("timed-out placeholder refuses recovery authorization", not all_safe)
    check("timed-out placeholder source remains untouched", stalled.is_file())
    shutil.rmtree(repo, ignore_errors=True)

    if hasattr(os, "mkfifo"):
        with tempfile.TemporaryDirectory(prefix="registry-fifo-") as tmp:
            fifo = Path(tmp) / "obsidian.json"
            os.mkfifo(str(fifo))
            started = time.monotonic()
            check(
                "Obsidian registry FIFO fails open without blocking",
                safety.obsidian_vault_paths(fifo) == [],
            )
            check("registry FIFO returns promptly", time.monotonic() - started < 1.0)

    # Git's -z porcelain is raw bytes. Newlines must not split records, and
    # non-UTF8 POSIX bytes must round-trip through surrogateescape.
    repo = make_main()
    newline = add_worktree(repo, "line\nbreak", "claude/newline-path")
    listed_result = safety.list_worktrees(repo)
    assert listed_result is not None
    listed = {path.resolve() for path in listed_result}
    check("newline worktree path stays one -z record", newline.resolve() in listed)
    check("registered newline path is not misclassified orphan", newline not in safety.list_orphan_dirs(repo))

    unknown_dir = repo / ".claude" / "worktrees" / "must-not-reclaim-on-git-error"
    unknown_dir.mkdir()
    original_git = safety.git

    def failed_worktree_list(base: Path, args: list[str], timeout: int = safety.GIT_TIMEOUT):
        if args[:3] == ["worktree", "list", "--porcelain"]:
            return subprocess.CompletedProcess(args, 1, b"", b"unsupported -z")
        return original_git(base, args, timeout)

    safety.git = failed_worktree_list
    try:
        listed_unknown = safety.list_worktrees(repo)
        orphan_unknown = safety.list_orphan_dirs(repo)
    finally:
        safety.git = original_git
    check("nonzero worktree-list result is UNKNOWN, not empty", listed_unknown is None)
    check("unknown registration state yields zero orphan candidates", orphan_unknown == [])

    if os.name == "posix":
        raw_slug = os.fsdecode(b"nonutf8-\xff")
        parsed = safety._parse_worktree_porcelain(
            b"worktree /tmp/" + os.fsencode(raw_slug) + b"\x00HEAD deadbeef\x00\x00"
        )
        check(
            "non-UTF8 worktree porcelain path round-trips",
            len(parsed) == 1 and os.fsencode(parsed[0].name) == b"nonutf8-\xff",
        )
        dirty = safety._parse_status_porcelain(repo, b"?? dirty-\xfe.md\x00")
        check(
            "non-UTF8 status path round-trips",
            any(os.fsencode(path.name) == b"dirty-\xfe.md" for path in dirty),
        )
    else:
        check("non-UTF8 path controls are POSIX-only", True)
    renamed = safety._parse_status_porcelain(
        repo,
        b"R  renamed.md\x00old-name.md\x00?? other.md\x00",
    )
    check(
        "rename source field is not misparsed as a second status record",
        [path.name for path in renamed] == ["renamed.md", "other.md"],
    )
    shutil.rmtree(repo, ignore_errors=True)

    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
