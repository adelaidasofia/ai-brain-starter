#!/usr/bin/env python3
"""Tests for hooks/session-start-context.py's _resolve_vault_paths().

_resolve_vault_paths() rewrites the CONTEXT block's relative `Meta/...`
references (`Meta/Last Session.md` etc.) into absolute paths so the model can
actually read them, since neither "cwd is the vault" nor "the Meta folder is
literally named Meta" holds on a real install. It fails OPEN by design: any
resolver error, or no vault found at all, silently leaves the text exactly as
shipped. That means a regression which disabled resolution entirely -- an
import that always raises, a resolver call that always returns None, a typo
in the replaced substring -- would still exit 0 and print valid JSON, so
nothing else in this repo's test suite would ever notice. This suite is the
only thing that would.

Drives the hook by subprocess (mirrors hooks/test_memory_index.py's
end-to-end pattern) rather than importing the function directly, on purpose:
the module lives at hooks/session-start-context.py, a filename with hyphens
that cannot be `import`ed as an identifier, and a subprocess run also proves
the SAME code path Claude Code itself executes -- argv, stdin contract, JSON
payload shape included -- not just a bare Python call.

Stdlib only. Run: python3 hooks/test_session_start_context.py
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
LOADER = HOOKS / "session-start-context.py"

# The three literal references the CONTEXT block carries today. If this ever
# changes, these three strings are also the ones _resolve_vault_paths() is
# looking to replace, so read from the same shipped text rather than a copy.
REFS = ("Meta/Last Session.md", "Meta/Current Priorities.md",
        "Meta/rules/efficiency.md")

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


def _write_meta(meta_dir: Path) -> None:
    """A Meta folder carrying all three files _resolve_vault_paths() looks for."""
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / "Last Session.md").write_text("last session\n", encoding="utf-8")
    (meta_dir / "Current Priorities.md").write_text("priorities\n", encoding="utf-8")
    (meta_dir / "rules").mkdir(parents=True, exist_ok=True)
    (meta_dir / "rules" / "efficiency.md").write_text("efficiency\n", encoding="utf-8")


def _run(label: str, cwd: Path, env_extra: dict) -> str:
    """The real loader's additionalContext for a run rooted at `cwd`.

    Registers its own PASS/FAIL for "the loader ran cleanly" so a crashed
    subprocess is a recorded failure, never a raised exception that aborts
    every scenario after it -- same discipline as test_memory_index.py's
    end-to-end checks (#10/#11).

    AGENT_MEMORY_DIR is pinned to a nonexistent path under `cwd` (memory_index's
    own "nonexistent dir -> silent" contract) so this suite's assertions never
    depend on whatever the real machine's memory index happens to contain.
    VAULT_ROOT is stripped unless the scenario supplies its own, so a
    machine-wide default some installs export can never leak into a case that
    is deliberately testing its absence.
    """
    env = dict(os.environ)
    env.pop("VAULT_ROOT", None)
    env["AGENT_MEMORY_DIR"] = str(cwd / "_no_memory_index_here")
    env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(LOADER)], input="{}",
        capture_output=True, text=True, env=env, cwd=str(cwd),
        # Pin the child decode: a vault path is arbitrary Unicode, and on a
        # non-UTF-8 Windows console the locale decode raises UnicodeDecodeError.
        # This repo's check-utf8-subprocess.py guard enforces it.
        encoding="utf-8", errors="replace",
    )
    check("{}: loader exits 0".format(label), proc.returncode == 0, proc.stderr[:300])
    try:
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    except Exception as exc:
        check("{}: loader payload parses".format(label), False,
              "{}: stdout={!r}".format(exc, proc.stdout[:200]))
        return ""


def main() -> int:
    print("session-start-context: _resolve_vault_paths")

    # 1. cwd IS the vault, Meta folder named with the shipped template's real
    #    name: an emoji prefix ("⚙️ Meta"), not plain "Meta".
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault"
        meta = vault / "⚙️ Meta"
        _write_meta(meta)
        ctx = _run("cwd-is-vault, emoji Meta", vault, {})
        check("emoji Meta: Last Session resolved to an absolute path",
              str(meta / "Last Session.md") in ctx, ctx[:300])
        check("emoji Meta: Current Priorities resolved to an absolute path",
              str(meta / "Current Priorities.md") in ctx, ctx[:300])
        check("emoji Meta: rules/efficiency resolved to an absolute path",
              str(meta / "rules" / "efficiency.md") in ctx, ctx[:300])

    # 2. cwd is a subdirectory several levels under the vault, Meta folder
    #    named plainly "Meta" (no emoji) -- the walk-up has to climb, and a
    #    plain-named folder has to be recognized too.
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault2"
        meta = vault / "Meta"
        _write_meta(meta)
        sub = vault / "some" / "deeply" / "nested" / "cwd"
        sub.mkdir(parents=True)
        ctx = _run("walk-up from nested subdir, plain Meta", sub, {})
        check("plain Meta via walk-up: Last Session resolved",
              str(meta / "Last Session.md") in ctx, ctx[:300])
        check("plain Meta via walk-up: Current Priorities resolved",
              str(meta / "Current Priorities.md") in ctx, ctx[:300])

    # 3. cwd is elsewhere entirely: no Meta-suffixed folder anywhere in its
    #    ancestry, and no VAULT_ROOT set. This is the fail-open floor -- a
    #    correct "nothing found" and a silently-broken resolver both produce
    #    an unchanged string, so leaving every reference untouched is the
    #    strongest thing this case can assert, and it is exactly the
    #    behavior a regression must not accidentally satisfy by doing nothing.
    with tempfile.TemporaryDirectory() as td:
        elsewhere = Path(td) / "nowhere_near_a_vault"
        elsewhere.mkdir()
        ctx = _run("cwd elsewhere, no VAULT_ROOT", elsewhere, {})
        check("no vault found: every literal Meta/... reference kept as-is",
              all(ref in ctx for ref in REFS), ctx[:300])

    # 4. A genuinely ABSENT target file: the Meta folder is found, but only
    #    ONE of the three files actually exists in it. Proves per-file
    #    os.path.isfile gating, not "found a Meta folder, rewrite everything
    #    it names" -- the missing two must stay literal while the one that
    #    exists is still resolved in the very same run.
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault4"
        meta = vault / "⚙️ Meta"
        meta.mkdir(parents=True)
        (meta / "Last Session.md").write_text("last session\n", encoding="utf-8")
        # Current Priorities.md and rules/efficiency.md deliberately absent.
        ctx = _run("Meta found but two of three files absent", vault, {})
        check("absent Current Priorities.md left as the literal reference",
              "Meta/Current Priorities.md" in ctx, ctx[:300])
        check("absent rules/efficiency.md left as the literal reference",
              "Meta/rules/efficiency.md" in ctx, ctx[:300])
        check("no fabricated path for either absent file",
              str(meta / "Current Priorities.md") not in ctx
              and str(meta / "rules" / "efficiency.md") not in ctx, ctx[:300])
        check("the ONE sibling file that DOES exist is still resolved",
              str(meta / "Last Session.md") in ctx, ctx[:300])

    # 5. VAULT_ROOT fallback: cwd has no Meta-suffixed ancestor at all, but
    #    $VAULT_ROOT names a real vault. This is the one behavioral path a
    #    hand-rolled walk-up and the shared vault_root_for() resolver could
    #    plausibly disagree on, so it earns its own case rather than folding
    #    into case 1 or 3.
    with tempfile.TemporaryDirectory() as td:
        elsewhere = Path(td) / "nowhere2"
        elsewhere.mkdir()
        vault = Path(td) / "vault5"
        meta = vault / "Meta"
        _write_meta(meta)
        ctx = _run("VAULT_ROOT fallback", elsewhere, {"VAULT_ROOT": str(vault)})
        check("VAULT_ROOT fallback resolves when cwd has no vault ancestor",
              str(meta / "Last Session.md") in ctx, ctx[:300])

    print("\n{} passed, {} failed".format(PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    # UTF-8 console guard (ai-brain-starter#313 cp1252 crash class). This file's
    # own source carries non-ASCII (the emoji-Meta fixtures in scenarios 1 and
    # 4), and a failed check() prints `detail`, which can contain a path built
    # from those fixtures -- so a Windows cp1252 console needs this reconfigure
    # exactly as much as session-start-context.py's own entrypoint does.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
