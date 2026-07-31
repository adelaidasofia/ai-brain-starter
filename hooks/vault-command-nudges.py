#!/usr/bin/env python3
"""
PreToolUse Bash hook: vault-wide command nudges + blocks.

Adapted from anthropics/claude-code examples/hooks/bash_command_validator_example.py.

Enforces CLAUDE.md rules that were codified but not hook-blocked:
- Blocks `git push` against the personal vault repo (no remote configured)
- Blocks unscoped `git status` against the vault repo (60K-file walk)
- Blocks `rm -rf` against the vault root or top-level emoji folders
- Nudges `grep` -> Grep tool / rg, `find -name` -> Glob tool

Two scoping models, tagged per rule:

- repo-scoped (git push / git status): fire ONLY when the git op targets
  the personal vault repo itself, or a worktree of it. Repo identity is
  resolved via `git rev-parse --git-common-dir`, NOT a path-string
  prefix. A prefix mis-fires on `🍄 the user's consulting brand/`, a symlink that lives
  inside the vault namespace but points at ~/dev/mycelium-vault -- a
  separate repo that HAS a GitHub remote and is a normal size. Targeting
  follows `git -C <dir>` and an explicit `git --git-dir <dir>`; the
  value-taking options (`-C`, `-c`, `--git-dir`, `--work-tree`,
  `--namespace`) are matched WITH their separate-argument value, so a
  subcommand cannot hide behind the value or a quoted value's space.

- namespace-scoped (rm -rf / grep / find): fire whenever the command
  touches the vault path namespace (cwd under it, or the literal path in
  the command). `rm -rf` through the 🍄 symlink still destroys real data,
  and the grep/find slow-walk concern is about the filesystem tree, not
  git, so these keep the path-prefix gate.

Escape hatch: prefix the command with VAULT_VALIDATOR_BYPASS=1.
"""
import os
from pathlib import Path
import json, sys, re, os, subprocess

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _lib.vault_root import vault_root_for  # noqa: E402
except Exception:  # fail-open: never block a command on an import error
    def vault_root_for(target: Path):  # type: ignore
        return None

# MYC-3529 — every vault root used below is resolved PER TARGET. The module
# used to bind
#     VAULT = os.environ.get("VAULT_ROOT", str(Path.home() / "vault"))
#     VAULT_GIT_DIR = os.path.realpath(os.path.join(VAULT, ".git"))
# once, at import. That is the #375/#404 shape, and BOTH scoping models here
# inherited it. UNSET, every rule compared against `~/vault`, which exists on
# almost no install: `git push`/`git status` in the vault were never blocked,
# and `rm -rf` against a top-level vault folder was never blocked either — a
# destructive-command guard that was inert and silent about it. SET, all five
# rules protected exactly ONE vault while every other vault on the machine was
# unguarded.

# Cap on how many absolute path tokens a single command may be probed for.
# Namespace scoping has to consider paths NAMED in the command (`cd /tmp &&
# rm -rf "<abs vault path>"`), and each probe is a bounded filesystem walk-up.
# Real commands name a handful of paths; the cap keeps a pathological one-liner
# from turning a PreToolUse hook into a stat storm.
_MAX_PATH_TOKENS = 12
_ABS_PATH_TOKEN_RE = re.compile(
    r'"([^"]{2,})"' r"|'([^']{2,})'" r'|((?:~|/|[A-Za-z]:[\\/])[^\s;|&]+)'
)


def _vault_git_dir_for(target: str) -> str | None:
    """realpath of the `.git` of the vault governing `target`, or None.

    None = no vault governs this path; the caller must fail open (allow), which
    is what keeps every ~/dev/* repo and non-vault repo passing straight
    through.
    """
    try:
        root = vault_root_for(Path(target) if target else Path.cwd())
    except (OSError, RuntimeError, ValueError):
        return None
    if root is None:
        return None
    return os.path.realpath(os.path.join(str(root), ".git"))


def _in_vault_namespace(command: str, cwd: str) -> bool:
    """True iff this command touches SOME vault's path namespace.

    Namespace-scoped rules (rm -rf / grep / find) are about the filesystem
    tree, not git identity, so the question is "is a vault path involved" —
    for ANY vault, not just the one $VAULT_ROOT happens to name. Two signals,
    matching the pre-MYC-3529 intent:
      - the effective cwd sits inside a vault, or
      - the command NAMES a path that sits inside a vault (so
        `cd /tmp && rm -rf "<abs vault path>"` is still caught).
    """
    if cwd:
        root = vault_root_for(Path(cwd))
        if root is not None and _is_under(cwd, root):
            return True

    seen: set[str] = set()
    for m in _ABS_PATH_TOKEN_RE.finditer(command):
        raw = next((g for g in m.groups() if g is not None), None)
        if raw is None:
            continue
        token = os.path.expanduser(raw.strip())
        if not os.path.isabs(token) or token in seen:
            continue
        seen.add(token)
        if len(seen) > _MAX_PATH_TOKENS:
            break
        root = vault_root_for(Path(token))
        if root is not None and _is_under(token, root):
            return True
    return False


def _is_under(path: str, root: Path) -> bool:
    """True iff `path` is `root` or sits beneath it. Lexical, no existence
    requirement — `rm -rf <vault>/x` must be caught before x exists."""
    try:
        a = os.path.normcase(os.path.abspath(str(path)))
        b = os.path.normcase(os.path.abspath(str(root)))
    except (OSError, ValueError):
        return False
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


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


# (regex, severity, message, repo_scoped).
#   severity 'block' -> exit 2 ; 'nudge' -> exit 2 with softer wording.
#   repo_scoped True  -> fire only when the op targets the vault repo. The
#                        regex captures the git options blob as group 1 so
#                        targeting can follow any `git -C <dir>`.
#   repo_scoped False -> fire whenever the command is in the vault namespace.
RULES = [
    (
        r'(?:^|&&|\|\|?|;(?!;))\s*git\s+' + _GIT_OPTS_CAP + r'push\b',
        'block',
        "git push in the vault: no remote is configured. This is a local-only snapshot repo. "
        "If you truly need to push, set up a remote first and confirm with the user. "
        "Rule: CLAUDE.md §'Git in this vault'.",
        True,
    ),
    (
        r'(?:^|&&|\|\|?|;(?!;))\s*git\s+' + _GIT_OPTS_CAP + r'status\s*(?:$|&&|;|\|)',
        'block',
        "Unscoped `git status` in a 60K-file vault walks the full tree (~10min, locks .git/index.lock). "
        "Pass explicit paths: git status -- \"⚙️ Meta/\" \"path/to/file.md\" "
        "Or use `git status --short --untracked-files=no -- <path>`.",
        True,
    ),
    (
        r'(?:^|&&|\|\|?|;(?!;))\s*rm\s+(?:-[A-Za-z]*[rRf][A-Za-z]*\s+)+"?(?:$HOME/vault|⚙️ Meta|✍️ Writing|📓 Journals|🚀 team-vault)',
        'block',
        "rm -rf against the vault root or a top-level emoji folder would destroy live work. "
        "Use explicit file paths or move to Archive/ instead.",
        False,
    ),
    (
        r'(?:^|&&|\|\|?|;(?!;)|\|)\s*grep\b(?!\s+--?version|\s+--?help)',
        'nudge',
        "Prefer the Grep tool (or `rg`) over `grep` — faster, proper ignores, no full-tree walks. "
        "If you really need plain grep (e.g. piping fixed stdin), prefix with VAULT_VALIDATOR_BYPASS=1.",
        False,
    ),
    (
        r'(?:^|&&|\|\|?|;(?!;)|\|)\s*find\s+\S+\s+-name\b',
        'nudge',
        "Prefer the Glob tool over `find -name` — faster and respects vault ignores. "
        "If you really need find, prefix with VAULT_VALIDATOR_BYPASS=1.",
        False,
    ),
]


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if data.get("tool_name", "") != "Bash":
        sys.exit(0)

    command = data.get("tool_input", {}).get("command", "")
    if not command:
        sys.exit(0)

    if "VAULT_VALIDATOR_BYPASS=1" in command:
        sys.exit(0)

    cwd = os.environ.get("CLAUDE_CWD", data.get("cwd", ""))
    cwd = _effective_cwd(command, cwd)

    # Namespace-scoped rules fire when the command touches SOME vault's path
    # namespace: cwd under a vault, or a vault path named in the command (so
    # `cd /tmp && rm -rf <abs vault path>` is still caught). Computed lazily
    # and once — every probe is a filesystem walk-up, and the overwhelming
    # majority of Bash calls never match a namespace-scoped rule at all.
    namespace_cache: list[bool] = []

    def in_vault_namespace() -> bool:
        if not namespace_cache:
            namespace_cache.append(_in_vault_namespace(command, cwd))
        return namespace_cache[0]

    # Repo-scoped rules consult `git rev-parse` -- run it lazily, and
    # cache by the captured git-options blob.
    base = cwd or os.getcwd()
    repo_cache = {}

    def opts_target_vault(opts_blob: str) -> bool:
        if opts_blob not in repo_cache:
            repo_cache[opts_blob] = _targets_vault(opts_blob, base)
        return repo_cache[opts_blob]

    hits = []
    for pattern, severity, message, repo_scoped in RULES:
        matches = list(re.finditer(pattern, command, re.MULTILINE))
        if not matches:
            continue
        if repo_scoped:
            fired = any(
                opts_target_vault(m.group(1) or "")
                for m in matches
            )
        else:
            fired = in_vault_namespace()
        if fired:
            hits.append((severity, message))

    if hits:
        for severity, message in hits:
            tag = "BLOCKED" if severity == "block" else "NUDGE"
            print(f"{tag} by vault-command-nudges hook:\n  {message}", file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
