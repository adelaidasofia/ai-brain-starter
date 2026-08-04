#!/usr/bin/env python3
"""Find foreign git checkouts nested inside a vault — by PROPERTY, not by name.

WHY THIS IS NOT A NAME LIST
---------------------------
`vault-backup.sh` excludes machine-exhaust by name, and that list included
`./.claude/worktrees` — the per-session worktree directory the Claude Desktop
app creates. That is a denylist against an open set, and it loses to the next
tool that picks a different name.

Measured on a real install: a second agent CLI created its worktrees in a
directory the list did not name, parked eleven checkouts of three unrelated
repositories inside the vault, and added 37 GB to the backup source. Nothing
noticed for weeks; the first symptom was the offsite backup provider refusing
uploads because the storage cap was exceeded, which left the offsite copy
silently stale for 50+ days.

So this keys on the CHARACTERISTIC — a directory under the vault that carries
its own `.git` — which catches any tool regardless of what it names its
directory. Keep the named excludes too; they are cheap and they cover the
non-repo exhaust this cannot see.

WHY IT DOES NOT JUST EXCLUDE EVERY NESTED REPO
----------------------------------------------
A vault can legitimately contain a project directory that is a git repo with **no
remote** — a writing project, a rendered media folder, a scratch experiment. Its
bulk is usually working-tree content, not `.git`. Excluding "any nested repo"
would silently drop irreplaceable files from the only backup that exists, which
is a worse bug than the one being fixed.

So the classification is RECOVERABILITY, not repo-ness:

  RECOVERABLE   the checkout (or, for a linked worktree, its owning repo) has a
                git remote -> its content exists on a server -> EXCLUDE it.
  UNRECOVERABLE no remote anywhere -> the vault may be its only copy -> NEVER
                exclude. Report it loudly instead, so it gets a remote or moves
                out of the vault, but keep backing it up until a human decides.

Fail-open by construction: anything unclassifiable stays IN the backup. For a
backup, backing up too much costs storage; backing up too little loses data.

Usage:
  vault-nested-repos.py --vault-root DIR [--relative] [--exclude-file OUT] [--json]

  --relative      emit vault-relative `./path` entries (for `tar --exclude=`)
                  instead of absolute paths (for tools that take absolute).
Exit: 0 when it produced a usable answer (fail-open); 2 on bad usage.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Deliberately SHORT. Only directories that can never themselves be a checkout
# root belong here: dependency and byte-cache dirs. Do not add `dist`, `build`,
# `target` or an editor config dir — plugin managers really do `git clone` into
# editor config directories, and pruning there would recreate the same blind
# spot somewhere new. Finding a checkout prunes its whole subtree anyway, so a
# nested repo's own dependency dirs are never walked regardless.
PRUNE_NAMES = {"node_modules", "__pycache__", ".venv", "venv", ".Trash", ".trash"}

_GLOB_META = "*?[]\\"


def _escape_glob(p: str) -> str:
    """Escape glob metacharacters so a literal path stays literal."""
    return "".join("\\" + c if c in _GLOB_META else c for c in p)


def _git(args: list[str], cwd: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args], cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def classify(checkout: Path) -> dict:
    """Decide whether `checkout` is recoverable from a git remote.

    Handles a primary clone (`.git` is a directory) and a linked worktree
    (`.git` is a file holding a `gitdir:` pointer); `git remote` inside a
    worktree already resolves through to the owning repo.
    """
    d = str(checkout)
    info: dict = {"path": d, "kind": "worktree" if (checkout / ".git").is_file() else "clone",
                  "remote": None, "recoverable": False, "reason": ""}

    remote = _git(["remote"], d)
    if remote is None:
        info["reason"] = "could not query git; left IN the backup (fail-open)"
        return info
    if not remote:
        info["reason"] = "no git remote — the vault may be its only copy"
        return info

    info["remote"] = _git(["remote", "get-url", remote.splitlines()[0]], d) or remote.splitlines()[0]
    info["recoverable"] = True
    info["reason"] = "content lives on a remote; a vault backup is a duplicate"
    return info


def scan(vault_root: Path) -> list[dict]:
    found: list[dict] = []
    root = str(vault_root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_NAMES]
        if dirpath == root:
            # The vault's OWN .git must never be excluded — it is the history.
            dirnames[:] = [d for d in dirnames if d != ".git"]
            continue
        if ".git" in dirnames or ".git" in filenames:
            found.append(classify(Path(dirpath)))
            dirnames[:] = []          # do not descend into a classified checkout
    return sorted(found, key=lambda i: i["path"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault-root", required=True)
    ap.add_argument("--exclude-file")
    ap.add_argument("--relative", action="store_true",
                    help="emit ./vault-relative paths (tar --exclude= form)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    vault = Path(a.vault_root)
    if not vault.is_dir():
        print(f"vault-nested-repos: vault not found: {vault}", file=sys.stderr)
        return 2
    vault = vault.resolve()

    items = scan(vault)
    excl = [i for i in items if i["recoverable"]]
    keep = [i for i in items if not i["recoverable"]]

    def render(p: str) -> str:
        if not a.relative:
            return _escape_glob(p)
        try:
            return _escape_glob("./" + str(Path(p).relative_to(vault)))
        except ValueError:
            return _escape_glob(p)

    if a.exclude_file:
        lines = ["# GENERATED by vault-nested-repos.py — do not edit.",
                 "# Foreign checkouts recoverable from a git remote, found by property."]
        lines += [render(i["path"]) for i in excl]
        Path(a.exclude_file).write_text("\n".join(lines) + "\n")

    for i in excl:
        print(f"vault-nested-repos: EXCLUDE {render(i['path'])}  ({i['kind']}, remote {i['remote']})")

    for i in keep:
        print(
            f"vault-nested-repos: WARN nested repo with NO REMOTE kept in the backup: {i['path']}\n"
            f"vault-nested-repos:      {i['reason']}. It inflates every backup AND exists\n"
            f"vault-nested-repos:      nowhere else. Give it a remote and push, or move it\n"
            f"vault-nested-repos:      out of the vault.",
            file=sys.stderr,
        )

    if a.json:
        print(json.dumps({"excluded": excl, "kept_no_remote": keep}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:   # fail-open: never break a backup
        print(f"vault-nested-repos: ERROR {e} — excluding nothing (fail-open)", file=sys.stderr)
        sys.exit(0)
