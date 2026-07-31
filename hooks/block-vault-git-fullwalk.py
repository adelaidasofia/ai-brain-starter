#!/usr/bin/env python3
"""
PreToolUse hook: block full-tree git staging on the personal Obsidian vault.

Dangerous patterns (all walk a 60K-file tree, lock .git/index.lock 10+ min):
  git add -A
  git add --all
  git add .          (whole-tree, not git add ./path/to/file)

Safe patterns (pass through):
  git add "specific/file.md"
  git add CLAUDE.md "⚙️ Meta/rules/foo.md"
  git diff --cached --name-only

Scope: fires ONLY when the git op targets the personal vault repo itself, or a
worktree of it. The repo is identified by `git rev-parse --git-common-dir`, NOT
by a path-string prefix. A string prefix mis-fires on symlinks that sit in the
vault namespace but point at a SEPARATE repo: `🍄 the user's consulting brand/` is a symlink to
~/dev/mycelium-vault. Do NOT revert this to `cwd.startswith(VAULT)` — a 60K-file
walk only hurts the vault; `git add -A` in a standard-sized ~/dev/* repo is
instant and must pass straight through.

Value-taking git options whose value is a SEPARATE argument are matched WITH
their value, so the `add` cannot hide behind the value and a quoted value's
space cannot end the match early. `-C <dir>` and an explicit `--git-dir <dir>`
retarget the op; `-c <name>=<value>`, `--work-tree` and `--namespace` are
consumed too. `git -C "<vault>" add -A`, `git --git-dir="<vault>/.git" add -A`,
and `git -c core.hooksPath=/dev/null add -A` are all detected.
"""
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
    both directions: UNSET, the comparison target was `~/vault/.git`, which
    exists on almost no install, so `_targets_vault_repo` returned False for
    every op and `git add -A` walked the 60K-file vault unimpeded — the exact
    10-minute index.lock stall this hook exists to prevent, with no signal that
    the guard had done nothing. SET, it named exactly ONE vault, so the same
    full-tree stage against a SECOND vault passed straight through.

    A hook fires on ops against ANY vault, so the vault has to be derived from
    the path the op actually targets (its effective cwd, or an explicit
    --git-dir). vault_root_for detects it from that path and falls back to
    $VAULT_ROOT only when detection finds nothing, which is what keeps the
    previous behavior intact for a vault with no Meta-suffixed folder.

    None = no vault governs this path; the caller must fail open (allow), which
    is what keeps `git add -A` in a standard-sized ~/dev/* repo instant.
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


try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

command = data.get("tool_input", {}).get("command", "")

# Block git add -A / --all / bare dot.
# Only match at command-start positions (line start, after &&, ;, or |)
# to avoid false positives inside commit messages, heredocs, or comments.
# Group 1 captures the git options blob (incl. any `-C <dir>`); group 2
# the dangerous argument.
# Does NOT match: git add ./relative/path, git add .gitignore, or
#   mentions of "git add -A" inside quoted strings/heredocs.
DANGEROUS = re.compile(
    r'(?:^|&&|;(?!;)|\|\|?)\s*git\s+'
    + _GIT_OPTS_CAP +                       # group 1: git options blob
    r'add\s+('                              # group 2: the dangerous argument
    r'-A\b'
    r'|--all\b'
    r'|\.\s*(?:$|&&|;|2>|>>|>|\|)'          # lone dot (not ./path or .gitignore)
    r')',
    re.MULTILINE
)

matches = list(DANGEROUS.finditer(command))
if not matches:
    sys.exit(0)

# A full-tree `git add` is present. Resolve which repo each invocation
# targets -- the 60K walk only hurts the vault repo; ~/dev/* repos stage
# instantly. `git -C <dir>` retargets the op, so fold it onto the cwd.
cwd = os.environ.get("CLAUDE_CWD", data.get("cwd", ""))
base_cwd = _effective_cwd(command, cwd) or os.getcwd()

if not any(_targets_vault(m.group(1) or "", base_cwd) for m in matches):
    sys.exit(0)

print(
    "BLOCKED by block-vault-git-fullwalk hook:\n"
    "  git add -A / --all / . walks 60,000+ files in the vault.\n"
    "  That locks .git/index.lock for 10+ minutes and burns context.\n"
    "  Use explicit paths instead:\n"
    "    git add \"⚙️ Meta/Sessions/file.md\" \"⚙️ Meta/rules/foo.md\"\n"
    "  Rule: CLAUDE.md §'Git in this vault'",
    file=sys.stderr
)
sys.exit(2)
