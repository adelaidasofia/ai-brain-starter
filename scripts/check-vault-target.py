#!/usr/bin/env python3
"""Guard: refuse to treat a path as an AI-brain vault when it is $HOME, a
filesystem root, or not a vault at all.

The relocate helpers take a `<vault-path>` argument and, when that path has no
`.git`, CREATE a repository there (`git init --separate-git-dir`). Before this
guard the only validation was "non-empty" and "is a directory", so pointing a
helper at `$HOME` turned the user's whole home directory into a git working
tree — with `~/.ssh`, `~/.aws`, `~/.netrc` and `~/.config/gh` sitting untracked
inside it and one `git add -A` away from being written into git objects
permanently. Observed on a real machine, not theoretical (MYC-4028).

This is the single source of truth for that check IN THIS REPO —
relocate-vault.sh/.ps1, relocate-machinery-sidecar.sh/.ps1 and the SessionStart
footprint signal all route through it, so those surfaces cannot drift from each
other. Same porcelain shape as check-cloud-sync.py, which the same callers
already use.

It is NOT the only implementation of the contract. `mycelium-studio` reimplements
these rules natively in Rust, because a desktop app cannot assume python is on
PATH. Read scripts/paired-implementations.json (contract `vault-target-refusal`)
before changing the rules here: an earlier version of this paragraph claimed
there was "no drift between surfaces" full stop, and that sentence is how the
Studio twin shipped without this guard at all (MYC-4035).

Usage:
  check-vault-target.py <path>                # human-readable verdict + remedy
  check-vault-target.py --porcelain <path>    # one machine-readable line
  check-vault-target.py --for-init <path>     # ALSO require positive vault
                                              # evidence — the caller is about
                                              # to create a repo at <path>

Exit codes:
  0  OK     — safe to treat as a vault
  1  REFUSE — unsafe target (see the porcelain token for which rule fired)
  2  USAGE  — bad arguments

Porcelain first token:
  OK_VAULT
  REFUSE_HOME:<what>          $HOME, a filesystem root, or a strict ancestor of
                              $HOME. The caller must NOT offer --force here.
  REFUSE_CREDENTIALS:<names>  credential material sits at the top level
  REFUSE_NOT_A_VAULT:<why>    --for-init only: nothing says this is a vault

POLICY SPLIT — the checker REPORTS, the caller ENFORCES. REFUSE_HOME is
absolute: no legitimate setup makes a home directory an AI-brain vault, and a
--force on it is the one an agent reaches for at 2am. The other two are
heuristics and callers may honour --force on them, loudly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII path in an
# error message cannot crash the guard itself.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # Python 3.7+
    except (AttributeError, ValueError):
        pass

# Top-level names that mean "this is somebody's home directory, not a vault".
# Checked as a SECOND net behind the $HOME comparison: it still fires for a
# roaming/redirected profile where $HOME does not match the literal path, and
# for /Users/<someone-else>.
CREDENTIAL_MARKERS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".netrc",
    ".docker",
    ".azure",
    ".config/gh",
    ".config/gcloud",
)

# Roots that are never a vault even if $HOME is unset or nonsense. Compared
# RESOLVED, not as strings: on macOS /tmp is a symlink to /private/tmp, so a
# literal string match reads /tmp as an ordinary directory and waves it through.
SYSTEM_ROOTS = (
    "/", "/Users", "/home", "/tmp", "/var", "/private", "/opt",
    "/usr", "/etc", "/System", "/Library", "/Applications", "/root",
)


def _resolve(p: str) -> Path:
    """Physical resolution. /var vs /private/var (macOS) otherwise makes every
    comparison below silently miss."""
    try:
        return Path(os.path.expanduser(p)).resolve()
    except OSError:
        return Path(os.path.abspath(os.path.expanduser(p)))


def _home() -> Path:
    try:
        return Path.home().resolve()
    except (RuntimeError, OSError):
        return Path(os.path.expanduser("~"))


def _home_verdict(target: Path) -> str | None:
    """Return a REFUSE_HOME reason, or None. Absolute rule — no --force."""
    home = _home()
    if target == home:
        return "the user's home directory ($HOME)"
    # Filesystem root: `/` and `C:\` are their own parent.
    if target == target.parent:
        return "a filesystem root"
    # A strict ancestor of $HOME — /Users, /home, and anything above them.
    if target in home.parents:
        return f"an ancestor of $HOME ({target})"
    for root in SYSTEM_ROOTS:
        try:
            if target == Path(root).resolve():
                return f"a system directory ({target})"
        except OSError:
            continue
    return None


def _credential_markers(target: Path) -> list[str]:
    found = []
    for rel in CREDENTIAL_MARKERS:
        try:
            if (target / rel).exists():
                found.append(rel)
        except OSError:
            continue
    return found


def _vault_evidence(target: Path) -> list[str]:
    """Positive signs this is an AI brain / notes vault. Cache dirs (.codegraph,
    .smart-env) are deliberately NOT evidence: any tool can drop one into any
    directory, which is exactly how $HOME came to look like a vault."""
    found = []
    try:
        if (target / "CLAUDE.md").is_file():
            found.append("CLAUDE.md")
        if (target / ".obsidian").is_dir():
            found.append(".obsidian/")
        for child in target.iterdir():
            if child.is_dir() and child.name.endswith("Meta"):
                found.append(f"{child.name}/")
                break
        for child in target.iterdir():
            if child.is_file() and child.suffix.lower() == ".md":
                found.append("markdown notes")
                break
    except OSError:
        pass
    return found


def main(argv: list[str]) -> int:
    porcelain = "--porcelain" in argv
    for_init = "--for-init" in argv
    rest = [a for a in argv if a not in ("--porcelain", "--for-init")]
    if len(rest) != 1:
        print("usage: check-vault-target.py [--porcelain] [--for-init] <path>",
              file=sys.stderr)
        return 2

    raw = rest[0]
    target = _resolve(raw)
    if not target.is_dir():
        print(f"not a directory: {raw}", file=sys.stderr)
        return 2

    reason = _home_verdict(target)
    if reason is not None:
        if porcelain:
            print(f"REFUSE_HOME:{reason}")
            return 1
        print(f"FAIL  Refusing: {target} is {reason}.")
        print( "      This tool creates or relocates a GIT REPOSITORY at the path you give it.")
        print( "      Doing that here would put your credentials (~/.ssh, ~/.aws, ~/.netrc,")
        print( "      ~/.config/gh) inside a git working tree, one `git add -A` from being")
        print( "      written into git history permanently.")
        print( "      Point it at your actual vault instead, e.g.  ~/Brain  or  ~/vaults/<name>.")
        print( "      There is no override for this one, by design.")
        return 1

    creds = _credential_markers(target)
    if creds:
        if porcelain:
            print(f"REFUSE_CREDENTIALS:{','.join(creds)}")
            return 1
        print(f"FAIL  Refusing: {target} holds credential material at its top level")
        print(f"      ({', '.join(creds)}), so it looks like a home directory rather than a vault.")
        print( "      Turning it into a git repository would place those files inside a working tree.")
        print( "      Point the tool at your vault instead. Override only if you are certain: --force")
        return 1

    if for_init:
        evidence = _vault_evidence(target)
        if not evidence:
            if porcelain:
                print("REFUSE_NOT_A_VAULT:no CLAUDE.md, no Meta folder, no .obsidian, no markdown")
                return 1
            print(f"FAIL  Refusing: {target} has no git repo AND nothing that says it is a vault.")
            print( "      Expected at least one of: CLAUDE.md, a `… Meta` folder, .obsidian/, or a .md file.")
            print( "      This tool would CREATE a repository here. If that is really what you want,")
            print( "      run `git init` yourself first, then re-run this tool.")
            print( "      Override only if you are certain: --force")
            return 1

    if porcelain:
        print("OK_VAULT")
        return 0
    print(f"OK    {target} is a plausible vault target.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
