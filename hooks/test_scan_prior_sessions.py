#!/usr/bin/env python3
"""Regression tests for the two secret-scan residuals.

These cover the failures that the in-session freeze fix (single-instance lock +
incremental + budget) did NOT close:

  1. partition_for_scrub() is FAIL-CLOSED. When the active-worktree set is None
     (git error / cannot determine) OR an empty set, scrub NOTHING.
     NEGATIVE CONTROL: the pre-fix code returned an empty set on a git error and
     then scrubbed EVERYTHING (fail-OPEN — it corrupted active-session JSONLs).
     test_partition_fail_closed_{none,empty} assert the OPPOSITE and FAIL loudly
     if the code ever regresses to fail-open.

  2. hex-256bit no longer fires on the Workflow runtime's `"key":"v2:<hex>"`
     content-addressed cache keys, BUT still fires on a bare 64-hex secret
     (both directions). The SAME redact() runs in the SessionEnd scrub, so a
     missing carve-out would rewrite the cache key and break Workflow resume.

Run: python3 hooks/test_scan_prior_sessions.py
(Also wired into CI via tests/integration/test_scan_prior_failclosed_scrub.sh.)
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))


def _load(name: str, filename: str):
    """Load a hyphenated-filename hook module by path (not import-able normally)."""
    spec = importlib.util.spec_from_file_location(name, HOOKS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


worker = _load("secret_scan_worker", "scan-prior-sessions-for-secrets.py")
from _lib.secret_patterns import scan  # noqa: E402

# A synthetic 64-hex value (all hex chars, obviously not a real secret). Used as
# BOTH a bare secret and a Workflow cache key so one assertion proves the split.
HEXV = "d" * 64


# -- 1. fail-closed scrub decision (the bug fix + its negative controls) -------

def test_partition_fail_closed_none():
    """None (git error / unknown) -> 0 scrubs, all -> skipped_unknown."""
    paths = [Path("/x/a.jsonl"), Path("/x/b.jsonl")]
    to_scrub, skipped_active, skipped_unknown = worker.partition_for_scrub(paths, None)
    assert to_scrub == [], f"FAIL-OPEN REGRESSION: scrubbed {to_scrub} on UNKNOWN set"
    assert skipped_active == []
    assert skipped_unknown == paths


def test_partition_fail_closed_empty():
    """Empty set -> 0 scrubs (the Done predicate: 'empty slug set -> 0 scrubs')."""
    paths = [Path("/x/a.jsonl")]
    to_scrub, _skip_active, skipped_unknown = worker.partition_for_scrub(paths, set())
    assert to_scrub == [], f"FAIL-OPEN REGRESSION: scrubbed {to_scrub} on EMPTY set"
    assert skipped_unknown == paths


def test_partition_nonempty_scrubs_inactive_skips_active():
    """Non-empty set: scrub files NOT in an active worktree, skip those that are.
    Proves the fail-closed fix did NOT disable the feature."""
    active = Path("/p/-Users-x--claude-worktrees-active-slug-abc123/sess.jsonl")
    closed = Path("/p/-Users-x--claude-worktrees-closed-slug-def456/sess.jsonl")
    to_scrub, skipped_active, skipped_unknown = worker.partition_for_scrub(
        [active, closed], {"active-slug-abc123"}
    )
    assert active in skipped_active, "active-worktree JSONL was NOT skipped"
    assert closed in to_scrub, "closed-worktree JSONL was NOT scrub-eligible"
    assert skipped_unknown == []


def test_auto_scrub_redacts_secret_backs_up_preserves_v2_key():
    """_auto_scrub on a real file: backs up, redacts the bare secret, and leaves
    the Workflow v2: cache key UNTOUCHED (the scrub-corrupts-resume guard)."""
    d = Path(tempfile.mkdtemp(prefix="secret-scan-scrub-"))
    try:
        j = d / "sess.jsonl"
        j.write_text(f'{{"secret":"API_KEY={HEXV}","key":"v2:{HEXV}"}}\n')
        ok, msg = worker._auto_scrub(j)
        assert ok, f"_auto_scrub did not scrub: {msg}"
        backups = list(d.glob("sess.jsonl.bak.*-secret-scrub"))
        assert backups, "no backup written before scrub"
        scrubbed = j.read_text(encoding="utf-8", errors="replace")
        assert "[REDACTED-hex-256bit]" in scrubbed, "real secret not redacted"
        assert f"v2:{HEXV}" in scrubbed, "v2 cache key was corrupted by the scrub"
        assert f"API_KEY={HEXV}" not in scrubbed, "bare secret survived the scrub"
    finally:
        shutil.rmtree(d, ignore_errors=True)


# -- 2. hex-256bit narrowing (both directions) ---------------------------------

def test_hex_v2_cache_key_skipped_real_secret_caught():
    wf_key = f'{{"type":"started","key":"v2:{HEXV}","agentId":"x"}}'
    assert dict(scan(wf_key)).get("hex-256bit-secret") is None, "v2 cache key WAS flagged"
    bare = f"API_KEY={HEXV}"
    assert dict(scan(bare)).get("hex-256bit-secret") == 1, "real bare-hex secret MISSED"


def main() -> None:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  [OK] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001 — surface any error as a failure
            failed += 1
            print(f"  [ERROR] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    # A cp1252 Windows console raises UnicodeEncodeError the moment this prints a
    # non-ASCII character, and the hook dies with no legible cause
    # (ai-brain-starter#313). Idempotent; a no-op on an already-UTF-8 console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
