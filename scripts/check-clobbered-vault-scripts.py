#!/usr/bin/env python3
"""Detect vault scripts broken or silently reverted by a sync.

`sync-vault-scripts.sh` propagates the repo's `scripts/` into a vault's
`<meta>/scripts/`, overwriting whatever is there and keeping a `.bak-<stamp>`
nobody reads. Two failure modes follow, both silent:

  BROKEN-DEP  a synced script sources/imports a sibling that is NOT beside it.
              Consumers ship without their dependency and fall back to a stub.
              Measured: `vault-safe-commit.sh` fell to a fail-closed stub and
              EVERY vault commit refused (it is the only route past the raw-git
              block guard), while `session-end-hook.sh` fell to "defer" and
              silently no-opped every session-end snapshot for two days.
              Prevention now lives in test_vault_script_sync.sh section 1b; this
              is the AT-REST leg for vaults synced before that gate existed.

  REVERTED    a vault script is byte-identical to the repo copy AND differs from
              the vault's own git HEAD. That means a local patch was overwritten
              by the sync. Measured: a committed `--only` scoping fix was reverted
              ~14h after it landed, because it had not been upstreamed yet, and
              nothing said so. Closure gates cannot see this one — the file is
              present and its dependencies resolve; it is simply the wrong version.

Surfacer only: prints findings, never mutates, never auto-restores. Restoring is
the operator's call because the local version is not always the one you want.

Exit: 0 clean (or --surface), 1 findings, 2 could not run.

Usage:
  python3 check-clobbered-vault-scripts.py --vault /path/to/vault
  python3 check-clobbered-vault-scripts.py --vault ... --surface   # always exit 0
"""
# exit-contract: ENFORCING

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

MAX_BYTES = 1_000_000  # bounded read: never slurp a huge or odd file

# Shell: `. "$SCRIPT_DIR/_foo.sh"` / `source "$SCRIPTS/_foo.sh"`
SH_DIRECT = re.compile(
    r'^\s*(?:\.|source)\s+"?\$\{?(?:SCRIPT_DIR|SCRIPTS|SCRIPT_ROOT|HERE)\}?/'
    r'([A-Za-z0-9_.-]+\.sh)"?',
    re.M,
)
# Shell indirect: `GUARD="$SCRIPT_DIR/_foo.sh"` … later `. "$GUARD"`.
# Not a nicety: the indirect form is the one that shipped the outage, and the
# first version of this detector was blind to it — blind to the exact defect it
# existed to catch.
SH_ASSIGN = re.compile(
    r'^\s*[A-Za-z_][A-Za-z0-9_]*=\s*"?\$\{?(?:SCRIPT_DIR|SCRIPTS|SCRIPT_ROOT|HERE)\}?/'
    r'([A-Za-z0-9_.-]+\.sh)"?',
    re.M,
)
# Python: `import _foo` / `from _foo import x`. Local-sibling modules only —
# leading underscore is this repo's convention for a synced shared module.
PY_IMPORT = re.compile(r'^\s*(?:from|import)\s+(_[A-Za-z0-9_]+)', re.M)


def _read_bounded(p: Path) -> str | None:
    # NOT named read_text: shadowing the stdlib Path method makes every call site
    # indistinguishable from an un-encoded `path.read_text()` to the repo's static
    # file-I/O encoding check, which is a conservative check worth keeping sharp.
    try:
        if p.stat().st_size > MAX_BYTES:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def find_meta_dir(vault_root: Path) -> Path | None:
    """Locate the Meta folder. Mirrors _meta_resolver.find_meta_dir, inlined so
    this script runs standalone from a vault that may not have the resolver yet
    (a vault missing its scripts is exactly when you want to run this)."""
    if not vault_root.is_dir():
        return None
    try:
        candidates = [c for c in sorted(vault_root.iterdir())
                      if c.is_dir() and c.name.endswith("Meta")]
    except OSError:
        return None
    for c in candidates:
        if (c / "Decisions").exists():
            return c
    return candidates[0] if candidates else None


def git_head_bytes(repo: Path, rel: str) -> bytes | None:
    try:
        r = subprocess.run(["git", "-C", str(repo), "show", f"HEAD:{rel}"],
                           capture_output=True, timeout=20)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def deps_of(path: Path, text: str) -> set[str]:
    if path.suffix == ".sh":
        return set(SH_DIRECT.findall(text)) | set(SH_ASSIGN.findall(text))
    if path.suffix == ".py":
        return {m + ".py" for m in PY_IMPORT.findall(text)}
    return set()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", required=True)
    ap.add_argument("--repo", default=None,
                    help="repo whose scripts/ is the sync source "
                         "(default: this script's parent dir)")
    ap.add_argument("--surface", action="store_true",
                    help="always exit 0 (fail-open for scheduled runs)")
    args = ap.parse_args()

    vault = Path(args.vault).expanduser()
    src = Path(args.repo).expanduser() / "scripts" if args.repo \
        else Path(__file__).resolve().parent

    meta = find_meta_dir(vault)
    if meta is None:
        print(f"[clobber-check] no Meta dir under {vault} — nothing to check")
        return 0
    dest = meta / "scripts"
    if not dest.is_dir():
        print(f"[clobber-check] no scripts dir at {dest} — nothing to check")
        return 0

    broken: list[tuple[str, str]] = []
    reverted: list[str] = []

    for f in sorted(list(dest.glob("*.sh")) + list(dest.glob("*.py"))):
        text = _read_bounded(f)
        if text is None:
            continue

        for dep in sorted(deps_of(f, text)):
            if not (dest / dep).exists():
                broken.append((f.name, dep))

        twin = src / f.name
        if not twin.is_file():
            continue
        try:
            local, shipped = f.read_bytes(), twin.read_bytes()
        except OSError:
            continue
        if local != shipped:
            continue  # diverged from shipped == normal for a vault-local file
        rel = f"{meta.name}/scripts/{f.name}"
        head = git_head_bytes(vault, rel)
        if head is not None and head != local:
            reverted.append(f.name)

    if not broken and not reverted:
        print("[clobber-check] clean — no broken sibling deps, no reverted scripts")
        return 0

    print("[clobber-check] FINDINGS")
    for name, dep in broken:
        print(f"  BROKEN-DEP  {name} needs sibling '{dep}', which is NOT in "
              f"{dest}.")
        print(f"              It is running its fallback stub. If this script is "
              f"a guard's only sanctioned path, that is an OUTAGE.")
        print(f"              Fix: add '{dep}' to VAULT_SCRIPTS in "
              f"sync-vault-scripts.sh, then re-sync.")
    for name in reverted:
        print(f"  REVERTED    {name} is byte-identical to the shipped copy but "
              f"differs from vault HEAD —")
        print(f"              a sync overwrote a local patch. Check for a "
              f"'{name}.bak-*' beside it.")
        print(f"              If the local version was right, UPSTREAM it — "
              f"otherwise the next sync reverts it again.")

    return 0 if args.surface else 1


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't
    # crash. This script's findings carry em dashes, and a detector that dies while
    # reporting a defect is worse than one that never ran.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
