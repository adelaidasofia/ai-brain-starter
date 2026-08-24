#!/usr/bin/env python3
"""
test_graphify_stage_select_cache_key.py — stdlib-only regression tests for the
cache-key computation in skills/graphify/scripts/graphify_stage_select.py.

Run: python3 tests/test_graphify_stage_select_cache_key.py
No pytest dependency. Exits non-zero on any failure. Touches no real vault.

Why this file exists
--------------------
graphify_stage_select.py is the sizer: it decides which files still need a paid
LLM extraction and prints the cost estimate the operator approves. It answered
that question by re-deriving graphify's cache key by hand, and the hand-rolled
version disagreed with graphify.cache.file_hash() in four ways at once:

  * it looked in graphify-out/cache/{h}.json, but semantic entries live in
    graphify-out/cache/semantic/{h}.json -- wrong directory, so every lookup
    missed even when the digest was right;
  * it fed the ABSOLUTE path into the digest; the library uses the path
    relative to the vault root, lowercased, precisely so a cache stays valid
    across machines and checkout directories;
  * it hashed the WHOLE .md file; the library hashes only the body below the
    YAML frontmatter, so that a metadata-only rewrite does not invalidate an
    expensive extraction;
  * `except Exception: miss` swallowed every error into the miss pile, which is
    what kept all of the above invisible.

Net effect on a real 437-file vault: 0 cache hits reported against 1,113 valid
entries. The sizer told the operator the corpus needed 185 files and ~401K
tokens; the honest numbers were 74 files and ~250K. A sizer that under-reports
its own cache does not fail loudly -- it just quietly bills you twice.

The invariants below are what "agrees with the library" means in practice.
test_matches_library is skipped when graphify is not importable (it is an
optional runtime dependency); the other four hold regardless and are the ones
that actually encode the four bugs.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "skills/graphify/scripts/graphify_stage_select.py"
_spec = importlib.util.spec_from_file_location("graphify_stage_select", _SCRIPT)
gss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gss)


def _write(root: Path, rel: str, front: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{front}\n---\n\n{body}", encoding="utf-8")
    return p


def test_matches_library() -> None:
    """The whole point: the sizer's key must equal the library's, byte for byte."""
    try:
        from graphify.cache import file_hash
    except Exception:
        print("       (graphify not importable -- skipped)")
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        p = _write(root, "Notes/Concept.md", "type: note\ntags: [a]", "# Title\n\nBody text.\n")
        assert gss.cache_key(p, root) == file_hash(p, root=root), (
            "cache_key disagrees with graphify.cache.file_hash -- every lookup will miss"
        )


def test_frontmatter_only_change_keeps_the_key() -> None:
    """Bug 3. A metadata rewrite must not invalidate a paid extraction.

    This is not hypothetical: a nightly metadata pass rewrote frontmatter across
    a whole CRM folder, and the sizer then billed every one of those files as
    changed.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        p = _write(root, "Notes/C.md", "type: note\nstatus: draft", "# T\n\nBody.\n")
        before = gss.cache_key(p, root)
        p.write_text(
            "---\ntype: note\nstatus: done\nreviewed: 2026-08-23\n---\n\n# T\n\nBody.\n",
            encoding="utf-8",
        )
        assert gss.cache_key(p, root) == before, "a frontmatter-only edit invalidated the key"


def test_body_change_breaks_the_key() -> None:
    """The other half of bug 3: real edits MUST invalidate, or we serve stale nodes."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        p = _write(root, "Notes/C.md", "type: note", "# T\n\nBody.\n")
        before = gss.cache_key(p, root)
        p.write_text("---\ntype: note\n---\n\n# T\n\nDifferent body.\n", encoding="utf-8")
        assert gss.cache_key(p, root) != before, "a body edit did NOT invalidate the key"


def test_key_is_root_relative_not_absolute() -> None:
    """Bug 2. Same content at the same relative path under two roots = same key.

    Absolute paths make a cache non-portable: move the vault, lose every hit.
    """
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        pa = _write(Path(a), "Notes/C.md", "type: note", "# T\n\nBody.\n")
        pb = _write(Path(b), "Notes/C.md", "type: note", "# T\n\nBody.\n")
        assert gss.cache_key(pa, Path(a)) == gss.cache_key(pb, Path(b)), (
            "the key depends on the absolute path -- a moved vault loses every cache hit"
        )


def test_find_cache_entry_searches_prompt_fingerprint_subdirs() -> None:
    """Bug 1. Entries live under cache/semantic/, and newer ones nest one level
    deeper under a p{fingerprint}/ directory. A flat-only lookup misses those."""
    with tempfile.TemporaryDirectory() as td:
        semantic = Path(td) / "semantic"
        (semantic / "pabc123").mkdir(parents=True)
        (semantic / "deadbeef.json").write_text("{}", encoding="utf-8")
        (semantic / "pabc123" / "cafe1234.json").write_text("{}", encoding="utf-8")
        assert gss.find_cache_entry(semantic, "deadbeef") is not None, "missed a flat entry"
        assert gss.find_cache_entry(semantic, "cafe1234") is not None, (
            "missed an entry nested under a p{fingerprint}/ subdirectory"
        )
        assert gss.find_cache_entry(semantic, "0000none") is None, "invented a hit"


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  [ok]   {t.__name__}")
        except Exception as exc:  # noqa: BLE001 - test harness reports everything
            failed += 1
            print(f"  [FAIL] {t.__name__}: {exc}")
    print("ALL PASS" if not failed else f"{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
