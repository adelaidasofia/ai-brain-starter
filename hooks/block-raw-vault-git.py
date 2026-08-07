#!/usr/bin/env python3
"""
PreToolUse hook: force mutating git ops on the personal Obsidian vault through
vault-safe-commit.sh.

Why: raw `git add/commit/checkout/reset/merge/rebase` inside the 60K-file vault
bypasses the vault-wide mutex and races with other sessions on .git/index.lock.
Documented fallout: index corruption 2026-04-17, 10-min stalls + hundreds of
thousands of tokens burned.

Scope: fires ONLY when the git op targets the personal vault repo itself, or a
worktree of it. The repo is identified by `git rev-parse --git-common-dir`, NOT
by a path-string prefix. A string prefix mis-fires on symlinks that sit in the
vault namespace but point at a SEPARATE repo: `🍄 the user's consulting brand/` is a symlink to
~/dev/mycelium-vault, which has its own GitHub remote and is committed with
plain git. Do NOT revert this to `cwd.startswith(VAULT)` — that was the
2026-05-22 bug that wrongly blocked a mycelium-vault commit. Every ~/dev/* repo
and any other non-vault repo passes straight through.

Value-taking git options whose value is a SEPARATE argument are matched WITH
their value, so a subcommand cannot hide behind the value and a quoted value's
space cannot end the match early. `-C <dir>` and an explicit `--git-dir <dir>`
retarget the op (targeting follows them, not the shell cwd); `-c <name>=<value>`,
`--work-tree` and `--namespace` are consumed so the value is not read as the
subcommand. `git -C "<vault>" add`, `git --git-dir="<vault>/.git" add`, and
`git -c core.hooksPath=/dev/null commit` used to evade the hook entirely.

Blocks these subcommands when the repo is the vault:
    git add / commit / checkout / reset / merge / rebase / restore / switch / stash

Allows read-only ops through:
    git status / diff / log / show / ls-files / rev-parse / branch / config / blame

Allows explicit escapes:
    vault-safe-commit.sh ...   (the sanctioned wrapper)
    GIT_VAULT_BYPASS=1 git ... (emergency escape hatch for the user)
"""
# MYC-3529: REQUIRED, not cosmetic. This module annotates with PEP-604
# `X | None`, which is evaluated at def-time and is a TypeError on Python
# 3.9 -- the floor version scripts/ci.sh's gate actually runs. py_compile
# does NOT catch it (the annotation compiles fine and only blows up when
# the def executes), so the import crash is invisible to the lint gates and
# shows up only as a hook that silently does nothing.
from __future__ import annotations

import os
from pathlib import Path
import json, sys, re, os, subprocess

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _lib.vault_root import vault_root_for  # noqa: E402
except Exception:  # fail-open: never block a git op on an import error
    def vault_root_for(target: Path):  # type: ignore
        return None


def _vault_git_dir_for(target: str) -> str | None:
    """realpath of the `.git` of the vault governing `target`, or None.

    MYC-3529 — resolved PER TARGET. The module used to bind
        VAULT = os.environ.get("VAULT_ROOT", str(Path.home() / "vault"))
        VAULT_GIT_DIR = os.path.realpath(os.path.join(VAULT, ".git"))
    once, at import. That is the #375/#404 shape and it fails silently OPEN in
    both directions: UNSET, VAULT_GIT_DIR was `~/vault/.git`, which exists on
    almost no install, so `_targets_vault_repo` returned False for every op and
    this guard allowed raw `git add`/`commit` in the vault it was written to
    protect. SET, it named exactly ONE vault, so the same raw ops against a
    SECOND vault — with the same 60K-file index.lock contention — passed
    straight through.

    A hook fires on ops against ANY vault, so the vault has to be derived from
    the path the op actually targets (its effective cwd, or an explicit
    --git-dir). vault_root_for detects it from that path and falls back to
    $VAULT_ROOT only when detection finds nothing, which is what keeps the
    previous behavior intact for a vault with no Meta-suffixed folder.

    None = no vault governs this path; the caller must fail open (allow).
    """
    try:
        root = vault_root_for(Path(target) if target else Path.cwd())
    except (OSError, RuntimeError, ValueError):
        return None
    if root is None:
        return None
    return os.path.realpath(os.path.join(str(root), ".git"))


def _effective_cwd(command: str, initial: str) -> str:
    """Resolve cwd after any leading `cd <path>` commands in the command string."""
    cwd = os.path.expanduser(initial) if initial else ""
    for chunk in re.split(r"\s*(?:&&|\|\||;)\s*", command):
        chunk = chunk.strip()
        m = re.match(r"cd\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))", chunk)
        if m:
            new_path = os.path.expanduser(next(g for g in m.groups() if g is not None))
            cwd = new_path if os.path.isabs(new_path) else os.path.normpath(os.path.join(cwd, new_path))
    return cwd


# A git option's value argument:
#   _VAL_SP  -- value as a SEPARATE token: quoted, or a bare non-space run.
#   _VAL_EQ  -- value glued onto `=`: quoted, or a (possibly empty) bare run.
#   _VAL_CFG -- one shell word honouring quotes: bare chars and quoted
#               spans in any mix, so `-c name="value with spaces"` is ONE
#               token (a bare `\S+` would stop at the space inside it).
# (The vault folder name contains a space, so quoted forms matter.)
_VAL_SP = r'(?:"[^"]*"' r"|'[^']*'" r'|\S+)'
_VAL_EQ = r'(?:"[^"]*"' r"|'[^']*'" r'|\S*)'
_VAL_CFG = r'(?:[^\s"\']' r'|"[^"]*"' r"|'[^']*')+"

# One git CLI option token (each ends in trailing whitespace). The
# value-taking options whose value is a SEPARATE argument are matched
# WITH their value, so a subcommand cannot hide behind the value and a
# quoted value's space cannot end the match early:
#   -C <dir> / -c <name>=<value> / --git-dir|--work-tree|--namespace <v>
# (=<v> and spaced <v> forms, quoted or bare). These specific
# alternatives MUST precede the generic `-X` / `--long` fallbacks, whose
# `\S+` stops at the first space.
_GIT_OPT = '|'.join([
    r'-C\s*"[^"]*"\s+',                      # -C "dir" / -C"dir"
    r"-C\s*'[^']*'\s+",                      # -C 'dir' / -C'dir'
    r'-C\s+\S+\s+',                          # -C dir
    r'-c\s+' + _VAL_CFG + r'\s+',            # -c <name>=<value>    (separate arg)
    r'--git-dir=' + _VAL_EQ + r'\s+',        # --git-dir=<dir>
    r'--git-dir\s+' + _VAL_SP + r'\s+',      # --git-dir <dir>      (separate arg)
    r'--work-tree=' + _VAL_EQ + r'\s+',      # --work-tree=<dir>
    r'--work-tree\s+' + _VAL_SP + r'\s+',    # --work-tree <dir>    (separate arg)
    r'--namespace=' + _VAL_EQ + r'\s+',      # --namespace=<ns>
    r'--namespace\s+' + _VAL_SP + r'\s+',    # --namespace <ns>     (separate arg)
    r'-[A-Za-z]\S*\s+',                      # any other short option (incl. -Cdir)
    r'--\S+\s+',                             # any other long option
])
_GIT_OPTS_CAP = r'((?:' + _GIT_OPT + r')*)'   # capturing group: the whole options blob


def _dash_c_target(opts_blob: str, base_cwd: str) -> str:
    """Fold any `git -C <dir>` options from a git options blob onto base_cwd,
    following git's cumulative -C semantics (each -C is relative to the
    previous one). Returns base_cwd unchanged when the blob has no -C."""
    cwd = base_cwd
    for m in re.finditer(r'-C\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))', opts_blob):
        raw = next((g for g in m.groups() if g is not None), None)
        if raw is None:
            continue
        path = os.path.expanduser(raw)
        cwd = path if os.path.isabs(path) else os.path.normpath(os.path.join(cwd, path))
    return cwd


def _targets_vault_repo(cwd: str) -> bool:
    """True iff a git op run from `cwd` would touch the personal vault repo:
    the main repo, or any worktree of it. Resolves the repo by identity, via
    `git rev-parse --git-common-dir`, so a separate repo reached through a
    vault-namespace symlink (e.g. ~/dev/mycelium-vault) returns False.
    Fails open (False) when the repo cannot be determined."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return False
    if out.returncode != 0 or not out.stdout.strip():
        return False
    vault_git_dir = _vault_git_dir_for(cwd)
    if vault_git_dir is None:
        return False
    common_dir = os.path.realpath(os.path.join(cwd, out.stdout.strip()))
    return common_dir == vault_git_dir


def _git_dir_arg(opts_blob: str):
    """Return the last explicit --git-dir value in a git options blob, or
    None. Honors --git-dir=<v> and --git-dir <v>, quoted or bare."""
    val = None
    for m in re.finditer(
        r'--git-dir(?:=|\s+)(?:' r'"([^"]*)"' r"|'([^']*)'" r'|(\S+))',
        opts_blob,
    ):
        g = next((x for x in m.groups() if x is not None), None)
        if g is not None:
            val = g
    return val


def _git_dir_is_vault(git_dir: str) -> bool:
    """True iff an explicit --git-dir points at the personal vault repo --
    its main .git, or a worktree gitdir whose common dir is the vault's.
    Resolves via `git --git-dir=<x> rev-parse --git-common-dir`; falls
    back to a realpath compare of the git dir itself."""
    git_dir = os.path.expanduser(git_dir)
    cands = [git_dir]
    try:
        out = subprocess.run(
            ["git", "--git-dir", git_dir, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
            cwd=git_dir if os.path.isdir(git_dir) else None,
        )
        if out.returncode == 0 and out.stdout.strip():
            cands.append(os.path.join(git_dir, out.stdout.strip()))
    except Exception:
        pass
    vault_git_dir = _vault_git_dir_for(git_dir)
    if vault_git_dir is None:
        return False
    return any(os.path.realpath(c) == vault_git_dir for c in cands)


def _targets_vault(opts_blob: str, base_cwd: str) -> bool:
    """True iff a git invocation carrying this options blob, run from
    base_cwd, would touch the personal vault repo. An explicit --git-dir
    is authoritative; otherwise targeting follows -C / cwd. Fails open
    (False) when the repo cannot be determined."""
    eff_cwd = _dash_c_target(opts_blob, base_cwd)
    git_dir = _git_dir_arg(opts_blob)
    if git_dir is not None:
        path = os.path.expanduser(git_dir)
        if not os.path.isabs(path):
            path = os.path.normpath(os.path.join(eff_cwd, path))
        return _git_dir_is_vault(path)
    return _targets_vault_repo(eff_cwd)


MUTATING = {
    "add", "commit", "checkout", "reset", "merge", "rebase",
    "restore", "switch", "stash", "cherry-pick", "revert", "am",
}

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

command = data.get("tool_input", {}).get("command", "")

if "GIT_VAULT_BYPASS=1" in command or "vault-safe-commit.sh" in command:
    sys.exit(0)

# Find every `git <subcommand>` invocation at a command-start position
# (line start, or after &&, ||, ;, |). Ignore mentions inside quoted strings
# by only matching the first token after separators. Group 1 captures the
# git options blob (incl. any `-C <dir>`); group 2 the subcommand.
pattern = re.compile(
    r'(?:^|&&|\|\|?|;(?!;))\s*'        # command boundary
    r'(?:[A-Z_][A-Z0-9_]*=\S+\s+)*'    # optional env assignments
    r'git\s+'                          # git
    + _GIT_OPTS_CAP +                  # group 1: git options (-C dir, --git-dir=..., ...)
    r'([a-z][a-z-]*)',                 # group 2: subcommand
    re.MULTILINE,
)

hits = [(m.group(1) or "", m.group(2)) for m in pattern.finditer(command)]
mutating = [(opts, sub) for opts, sub in hits if sub in MUTATING]
if not mutating:
    sys.exit(0)

# A mutating git subcommand is present. Resolve which repo each invocation
# targets -- only the vault repo (or a worktree of it) gets funneled through
# the wrapper. `git -C <dir>` retargets the op, so fold it onto the cwd.
cwd = os.environ.get("CLAUDE_CWD", data.get("cwd", ""))
base_cwd = _effective_cwd(command, cwd) or os.getcwd()

blocked = []
seen = {}
for opts, sub in mutating:
    if opts not in seen:
        seen[opts] = _targets_vault(opts, base_cwd)
    if seen[opts]:
        blocked.append(sub)
if not blocked:
    sys.exit(0)

print(
    "BLOCKED by block-raw-vault-git hook:\n"
    f"  Raw `git {blocked[0]}` in the vault races with other sessions on\n"
    "  .git/index.lock and bypasses the vault-wide mutex.\n"
    "  Use the wrapper (commit MESSAGE FIRST, then paths — there is no -m flag):\n"
    "    bash \"⚙️ Meta/scripts/vault-safe-commit.sh\" \\\n"
    "        \"session: <slug> — <one-line summary>\" \"path/one.md\" \"path/two.md\"\n"
    "  From a worktree, commit MAIN-VAULT paths (the wrapper cd's to the\n"
    "  main vault) — worktree-only files must be copied to the main vault first.\n"
    "  Emergency bypass (use sparingly): prefix with GIT_VAULT_BYPASS=1\n"
    "  Rule: ⚙️ Meta/rules/session-close.md Phase 2b",
    file=sys.stderr,
)
sys.exit(2)
