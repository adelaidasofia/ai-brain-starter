#!/usr/bin/env python3
"""
PreToolUse Bash hook: vault-wide command nudges + blocks.

Adapted from anthropics/claude-code examples/hooks/bash_command_validator_example.py.

Enforces CLAUDE.md rules that were codified but not hook-blocked:
- Blocks `git push` against a local-only vault repo (no remote configured)
- Blocks unscoped `git status` against a vault repo (full-tree walk)
- Blocks `rm -rf` against a vault root or any of its top-level folders
- Nudges `grep` -> Grep tool / rg, `find -name` -> Glob tool

Three scoping models, tagged per rule:

- repo-scoped (git push / git status): fire ONLY when the git op targets
  a vault repo itself, or a worktree of it. Repo identity is resolved via
  `git rev-parse --git-common-dir`, NOT a path-string prefix. A prefix
  mis-fires on a symlinked folder that LIVES inside the vault namespace
  but POINTS AT a separate repo -- one that has a remote and is a normal
  size, so pushing from it is fine and blocking it is a false positive.
  Targeting follows `git -C <dir>` and an explicit `git --git-dir <dir>`;
  the value-taking options (`-C`, `-c`, `--git-dir`, `--work-tree`,
  `--namespace`) are matched WITH their separate-argument value, so a
  subcommand cannot hide behind the value or a quoted value's space.

- namespace-scoped (grep / find): fire whenever the command touches the
  vault path namespace (cwd under it, or the literal path in the
  command). The grep/find slow-walk concern is about the filesystem tree,
  not git, so these keep the path-prefix gate.

- target-scoped (rm -rf): parse the rm's OPERANDS, resolve each one
  against every candidate cwd, and fire only when a resolved target IS a
  vault root or a direct child of one. See _rm_verdicts.

WHAT THE GUARD CAN SEE comes first (every scoping model above is downstream
of it). Rules are matched PER SEGMENT against a quote-aware split, and each
segment is read through `_LEAD`, which absorbs transparent wrappers, one-shot
`VAR=` assignments and an explicit path to the binary. Matching a bare `git` /
`rm` token at a regex-alternation boundary instead let all of these run
completely unguarded, which no amount of correct cwd reasoning downstream can
recover:

    env git push        sudo git push        /usr/bin/git push
    FOO=bar git push    command git status   (git push)
    FOO=bar rm -rf <vault root>              /bin/rm -rf <vault root>

Escape hatch: prefix the command with VAULT_VALIDATOR_BYPASS=1, or export it
for the session. The inline form is scoped to the command it prefixes, not to
the whole line.
"""
# REQUIRED, not cosmetic. This module annotates with PEP-604 `X | None`, which
# is evaluated at def-time and is a TypeError on Python 3.9 -- the floor version
# the lint gate actually runs. py_compile does NOT catch it (the annotation
# compiles fine and only blows up when the def executes), so the import crash is
# invisible to the lint gates and shows up only as a hook that silently does
# nothing.
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _lib.vault_root import vault_root_for  # noqa: E402
except Exception:  # fail-open: never block a command on an import error
    def vault_root_for(target: Path):  # type: ignore
        return None

# The quote-aware splitter, the `$VAR` expander, the heredoc-body stripper and
# the comment stripper are CANONICAL in _lib.shell_parse. This guard used to
# hand-roll its own `cd` walk and inherited every fail-open that copy had.
# Shared primitive, per-caller policy: the primitives come from _lib, the
# fail-closed candidate-set POLICY below is this guard's own.
try:
    from _lib.shell_parse import (  # noqa: E402
        ASSIGN_RE,
        WRAPPER_PREFIXES,
        expand_vars,
        segment_bypass_flags,
        split_segments_with_seps,
        strip_heredoc_bodies,
        strip_noncode,
        tokens as shell_tokens,
    )
    _LIB_OK = True
except Exception:
    # FAIL CLOSED. Other consumers of these helpers fail OPEN on a missing _lib
    # because their job is to let commands through; this guard's job is to STOP
    # a push to a local-only repo and an rm against a vault, so a degraded parse
    # must keep the session cwd as the only candidate -- which still blocks the
    # common case and, at worst, over-blocks a cross-repo one (visible and
    # bypassable) instead of silently letting a vault push through (invisible).
    _LIB_OK = False

# Scrub the git-LOCATION / index / object / namespace / discovery env family (+
# CDPATH) from the env handed to this hook's git subprocesses. git honors
# GIT_DIR / GIT_WORK_TREE / etc. OVER `git -C <path>` and `cd`, so a leaked one
# (a git-hook context exports GIT_DIR; a concurrent worktree session can leak
# one in) would make this hook resolve a DIFFERENT repo than the one it was
# asked about -- deciding against the wrong worktree.
_GIT_CLEAN_ENV = {
    k: v for k, v in os.environ.items()
    if k not in {
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_PREFIX", "CDPATH",
    }
}

# Every vault root used below is resolved PER TARGET. The module used to bind
#     VAULT = os.environ.get("VAULT_ROOT", str(Path.home() / "vault"))
#     VAULT_GIT_DIR = os.path.realpath(os.path.join(VAULT, ".git"))
# once, at import, and BOTH scoping models inherited it. UNSET, every rule
# compared against `~/vault`, which exists on almost no install: `git push` /
# `git status` in the vault were never blocked, and `rm -rf` against a
# top-level vault folder was never blocked either -- a destructive-command
# guard that was inert and silent about it. SET, all five rules protected
# exactly ONE vault while every other vault on the machine was unguarded.

# Cap on how many absolute path tokens a single command may be probed for.
# Namespace scoping has to consider paths NAMED in the command (`cd /tmp &&
# rm -rf "<abs vault path>"`), and each probe is a bounded filesystem walk-up.
# Real commands name a handful of paths; the cap keeps a pathological one-liner
# from turning a PreToolUse hook into a stat storm.
_MAX_PATH_TOKENS = 12
_ABS_PATH_TOKEN_RE = re.compile(
    r'"([^"]{2,})"' r"|'([^']{2,})'" r'|((?:~|/|[A-Za-z]:[\\/])[^\s;|&]+)'
)

_GLOB_CHARS = set("*?[")


def _vault_git_dir_for(target: str) -> str | None:
    """realpath of the `.git` of the vault governing `target`, or None.

    None = no vault governs this path; the caller must fail open (allow), which
    is what keeps every non-vault repo passing straight through.
    """
    try:
        root = vault_root_for(Path(target) if target else Path.cwd())
    except (OSError, RuntimeError, ValueError):
        return None
    if root is None:
        return None
    return os.path.realpath(os.path.join(str(root), ".git"))


def _is_under(path: str, root: Path) -> bool:
    """True iff `path` is `root` or sits beneath it.

    Compared lexically AND through realpath, for the same reason _same_dir is:
    `vault_root_for` hands back a RESOLVED root, while cwd and the command's
    path tokens arrive spelled exactly as the caller wrote them. A vault
    reached through a symlinked ANCESTOR -- `/var` -> `/private/var`, which is
    macOS's default TMPDIR -- therefore failed on spelling alone, and every
    namespace-scoped rule (grep, find) silently un-scoped itself: no match, no
    message, exit 0.

    The lexical pair is tried FIRST and still carries the deliberate
    unresolved-operand behaviour _resolve_operand documents (a top-level vault
    entry can be a symlink OUT of the vault, and deleting through it still
    destroys the vault's own entry). realpath only ever ADDS a match, so
    nothing that matched before can stop matching.

    No existence requirement either way -- `rm -rf <vault>/x` must be caught
    before x exists, and realpath leaves a missing tail untouched.
    """
    try:
        pairs = (
            (os.path.abspath(str(path)), os.path.abspath(str(root))),
            (os.path.realpath(str(path)), os.path.realpath(str(root))),
        )
    except (OSError, ValueError):
        return False
    for raw_a, raw_b in pairs:
        a = os.path.normcase(raw_a)
        b = os.path.normcase(raw_b)
        if a == b or a.startswith(b.rstrip(os.sep) + os.sep):
            return True
    return False


def _seg_in_vault_namespace(seg: str, bases) -> bool:
    """True iff THIS segment touches SOME vault's path namespace.

    Namespace-scoped rules (grep / find) are about the filesystem tree, not git
    identity, so the question is "is a vault path involved" -- for ANY vault,
    not just one a `$VAULT_ROOT` happens to name. Two signals:
      - a candidate cwd sits inside a vault, or
      - THIS segment NAMES a path that sits inside a vault (so
        `cd /tmp && grep -r x "<abs vault path>"` is still caught).

    Scoped to the SEGMENT, not the whole command: a vault path sitting in an
    unrelated argument elsewhere in the line used to nudge on a grep that never
    touched the vault.
    """
    for b in bases:
        if not b:
            continue
        root = vault_root_for(Path(b))
        if root is not None and _is_under(b, root):
            return True

    seen: set[str] = set()
    for m in _ABS_PATH_TOKEN_RE.finditer(seg):
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


def _cd_operand(rest):
    """The single directory operand of a `cd`, or None when it is unresolvable.

    None means "this cd may have gone somewhere I cannot name", which callers
    must treat as AMBIGUOUS (keep the previous cwd in play), never as "no cd
    happened". `cd -` (OLDPWD) and a multi-operand `cd` land here on purpose.
    """
    if "-" in rest[1:]:
        # `cd -` goes to OLDPWD, which this process cannot know. It must be
        # UNRESOLVABLE; the option filter below would otherwise discard the "-"
        # as a flag and silently resolve the cd to HOME -- and if OLDPWD was the
        # vault, that is a fail-open dressed as a confident answer.
        return None
    operands = [t for t in rest[1:] if not t.startswith("-")]
    if not operands:
        return os.path.expanduser("~")          # bare `cd` -> HOME
    if len(operands) > 1:
        return None
    return operands[0]


def _cwd_candidates(segs, base: str):
    """Every cwd a command could actually run in, plus the literal `VAR=`
    assignments seen along the way.

    FAIL-CLOSED BY CONSTRUCTION. This returns a SET, not a single cwd, and the
    caller blocks when ANY member is a vault. A single "effective cwd" forces a
    guess at each ambiguity, and every wrong guess in the old walk pointed the
    same way -- off the vault, i.e. open. Concretely, the old walk allowed all
    of these vault pushes:

        W=<vault>; cd "$W" && git push       -- `$W` never expanded
        echo "hi; cd /tmp" && git push       -- `cd` cut out of a QUOTED string
        cd /tmp || git push                  -- `||` treated as an unconditional cd

    Ambiguity now UNIONS instead of overwriting, so the vault stays in the set
    and the block stands. The cost is a possible over-block on a genuinely
    ambiguous command, which is loud and bypassable; the old cost was a silent
    allow on the one command the guard exists to stop.
    """
    base = os.path.expanduser(base) if base else ""
    cur = {base} if base else set()
    variables: dict[str, str] = {}
    if not _LIB_OK:
        return cur, variables

    depth = 0
    for idx, (sep, seg) in enumerate(segs):
        # `(` and `)` arrive as separators, so paren depth is just a running
        # count -- and it covers BOTH `( cd X ; ... )` and `$( cd X && ... )`,
        # because a command substitution opens the same paren. A cd below the
        # top level changes only the SUBSHELL's cwd and is invisible to the
        # parent, so it must not move the candidate set. Tracking only the
        # separator IMMEDIATELY after the cd was not enough: in
        # `(cd /tmp; true) && git push` the cd is followed by `;`, so it looked
        # top-level and escaped its own subshell.
        if sep == "(":
            depth += 1
        elif sep == ")":
            depth = max(0, depth - 1)
        if depth > 0:
            continue
        toks = shell_tokens(seg.strip())
        if not toks:
            continue
        k = 1 if toks[0] == "export" else 0
        while k < len(toks) and ASSIGN_RE.match(toks[k]):
            name, _, val = toks[k].partition("=")
            variables[name] = expand_vars(val, variables)
            k += 1
        while k < len(toks) and toks[k] in WRAPPER_PREFIXES:
            k += 1
        rest = toks[k:]
        if not rest or rest[0] != "cd":
            continue

        raw = _cd_operand(rest)
        target = expand_vars(raw, variables) if raw else None
        # An unexpanded `$`, a command substitution or a glob is a target we
        # cannot name -- treat as unresolved rather than resolving it wrongly.
        if target and ("$" in target or set(target) & _GLOB_CHARS):
            target = None

        if target is None:
            moved: set[str] = set()            # unknown destination
        else:
            t = os.path.expanduser(target)
            moved = ({t} if os.path.isabs(t)
                     else {os.path.normpath(os.path.join(b, t)) for b in (cur or {""})})

        # The operator JOINING this cd to what follows decides whether the cd is
        # in effect for it. See split_segments_with_seps for the full table.
        nxt = segs[idx + 1][0] if idx + 1 < len(segs) else ""
        if not moved:
            continue                           # destination unknown -> keep old cwd
        if nxt == "&&":
            # The right-hand side runs IF AND ONLY IF the cd succeeded, so where
            # it runs is not in doubt. No existence check: an earlier segment may
            # legitimately create the directory (`git worktree add W && cd W`).
            cur = moved
        elif nxt in (";", "\n", ""):
            # `cd X ; cmd` runs cmd in X when the cd succeeds and in the OLD cwd
            # when it fails. That is DECIDABLE, not a coin flip: a cd fails when
            # the target is not a directory. Deciding it matters -- unioning
            # unconditionally would keep the vault in the set for the everyday
            # `cd /repo` + newline + `git push` shape and false-block every one
            # of them, which is precisely what teaches the bypass.
            if all(os.path.isdir(m) for m in moved):
                cur = moved
            else:
                cur = cur | {m for m in moved if os.path.isdir(m)}
        # "||", "|", "&", "(", ")" -> the next command runs in the OLD cwd:
        # leave `cur` alone.
    return cur, variables


# ---------------------------------------------------------------------------
# What may sit BETWEEN the start of a command and its verb without changing
# which program actually runs: transparent wrappers, one-shot `VAR=value`
# assignments, and an absolute/relative path to the binary.
#
# Without this the guard matched the bare token `git` / `rm`, so `/usr/bin/git
# push`, `env git push`, `command git status` and `FOO=bar rm -rf <vault root>`
# matched NO rule and ran unguarded. That is a hole in what the guard can SEE,
# upstream of every scoping model below it.
#
# `_lib.shell_parse` already models wrappers this way (WRAPPER_PREFIXES) and
# callers compare `os.path.basename(argv0)`; these regexes were the last place
# that did not.
_ASSIGN_TOK = r'[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|\'[^\']*\'|\S*)'
_WRAPPER_TOK = r'(?:env|command|exec|builtin|nohup|sudo|time)'
_LEAD = r'\s*(?:(?:' + _ASSIGN_TOK + r'|' + _WRAPPER_TOK + r')\s+)*(?:\S*/)?'

# ---------------------------------------------------------------------------
# rm -rf targeting (target-scoped)
#
# The rule this replaces pattern-matched FOLDER NAMES immediately after the
# flags:
#     ...rm\s+(?:-[A-Za-z]*[rRf][A-Za-z]*\s+)+"?(?:$HOME/vault|<emoji folder>|...)
# Two independent defects, and the rule advertised in the docstring above was
# never actually enforced:
#
#   1. `$HOME/vault` is a SHELL string sitting in a PYTHON regex. `$` is an
#      end-of-line anchor, so that branch parses as "end of line, then the
#      literal text HOME/vault" and cannot match any input, ever. The vault
#      ROOT -- the most destructive target of all -- was never blocked.
#   2. The emoji branches are literal names anchored right after the flags, so
#      they only fire on a bare relative `rm -rf "<folder>"`. An absolute
#      `rm -rf "/Users/x/Brain/<folder>"` -- the same deletion, spelled the way
#      an agent actually spells it -- sailed straight through.
#
# Both are fixed by asking the filesystem instead of the string: parse the
# rm's operands, resolve each against the candidate cwds, and compare the
# RESULT to the vault that governs it. Names stop mattering, so a vault whose
# folders are not a hardcoded set is covered too, and depth stops mattering,
# so every spelling of the same target behaves identically.
_RM_FLAG = r'(?:--[A-Za-z][A-Za-z-]*|--|-[A-Za-z]+)'

# Operands run to the END of the segment: the quote-aware splitter has already
# removed every unquoted separator, so a `;` still present here is INSIDE a
# quoted operand and must not truncate it (`rm -rf "a;b"`).
_RM_RE = re.compile(
    _LEAD +
    r'rm\s+'
    r'((?:' + _RM_FLAG + r'\s+)*)'        # 1: leading flag blob
    r'(.*)'                               # 2: operands, to the end of the segment
)

# One shell word: double-quoted, single-quoted, or bare. The bare arm accepts
# a backslash-escaped space (`My\ Folder`) as part of the word, because that is
# the idiomatic unquoted spelling of a folder name with a space. Every OTHER
# backslash stays literal, so a Windows `C:\Users\x\Brain` is not mangled into
# an escape sequence.
_RM_TOKEN_RE = re.compile(
    r'"([^"]*)"'
    r"|'([^']*)'"
    r'|((?:\\ |[^\s])+)'
)


def _is_destructive_rm(flag_blob: str, operand_text: str) -> bool:
    """True iff these rm flags can destroy a directory tree.

    Keeps the `[rRf]` reach (recursive OR force) rather than narrowing to `-r`:
    a guard on the vault root should not be the place we get clever about which
    flag combination happens to succeed. Flags are read from BOTH sides of the
    operands, so `rm dir -rf` counts.
    """
    for blob in (flag_blob, operand_text):
        for tok in re.findall(_RM_FLAG, blob):
            if tok.startswith("--"):
                if tok in ("--recursive", "--force"):
                    return True
            elif any(c in tok for c in "rRf"):
                return True
    return False


def _rm_operands(operand_text: str):
    """Shell words in an rm's operand text that are TARGETS, not flags."""
    targets = []
    for m in _RM_TOKEN_RE.finditer(operand_text):
        dq, sq, bare = m.groups()
        if dq is not None:
            word = dq
        elif sq is not None:
            word = sq
        else:
            word = bare.replace("\\ ", " ")
        if not word or (word.startswith("-") and dq is None and sq is None):
            continue  # a flag, not a target
        targets.append(word)
    return targets


def _resolve_operand(word: str, cwd: str) -> str:
    """An rm operand as an absolute path, WITHOUT resolving symlinks.

    Lexical on purpose, matching _is_under: a top-level vault entry can be a
    symlink to a separate repo, and `rm -rf` through it still destroys the
    vault's own entry. Resolving it first would retarget the check at the
    symlink's destination and lose exactly that case.
    """
    path = os.path.expanduser(word.strip())
    # Trailing separators are cosmetic (`rm -rf "Meta/"`); dirname would
    # otherwise hand back the folder itself instead of its parent.
    stripped = path.rstrip("/\\")
    if stripped and not re.fullmatch(r'[A-Za-z]:', stripped):
        path = stripped
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    return os.path.normpath(os.path.abspath(path))


def _same_dir(a: str, b) -> bool:
    """True iff two path spellings denote the same directory.

    Compared lexically AND through realpath: `vault_root_for` hands back a
    RESOLVED root, while the operand above is deliberately unresolved, so a
    tmpdir that is itself a symlink (`/var` -> `/private/var` on macOS) would
    make an otherwise-correct match fail on spelling alone.
    """
    try:
        pairs = (
            (os.path.abspath(str(a)), os.path.abspath(str(b))),
            (os.path.realpath(str(a)), os.path.realpath(str(b))),
        )
    except (OSError, ValueError):
        return False
    return any(os.path.normcase(x) == os.path.normcase(y) for x, y in pairs)


def _rm_target_verdict(target: str):
    """'root' | 'top-level' | None for one resolved rm target.

    The vault is resolved PER TARGET, so this covers every vault on the
    machine, not one a `$VAULT_ROOT` happens to name.

    The parent is probed SEPARATELY rather than by walking up from the target,
    because vault_root_for() resolves symlinks: asking about a top-level entry
    that is itself a symlink answers for the repo it points AT. Asking about
    its parent answers about the vault whose entry is being deleted.

    Depth beyond a direct child is deliberately NOT blocked -- a file deep
    inside a folder is the "explicit file paths" the message recommends.
    """
    root = vault_root_for(Path(target))
    if root is not None and _same_dir(target, root):
        return "root"
    parent = os.path.dirname(target)
    if parent and parent != target:
        proot = vault_root_for(Path(parent))
        if proot is not None and _same_dir(parent, proot):
            return "top-level"
    return None


def _rm_verdicts(segs_text, bases):
    """Every (target, verdict) a destructive rm in these segments would hit.

    A RELATIVE operand is resolved against EVERY candidate cwd, for the same
    fail-closed reason _cwd_candidates returns a set: if any cwd the command
    could run in puts `rm -rf Meta` on a vault, that is the one that matters.
    """
    found = []
    probed = 0
    seen = set()
    for seg in segs_text:
        m = _RM_RE.match(seg.lstrip())
        if not m:
            continue
        flag_blob, operand_text = m.group(1) or "", m.group(2) or ""
        if not _is_destructive_rm(flag_blob, operand_text):
            continue
        for word in _rm_operands(operand_text):
            for base in (sorted(bases) or [""]):
                probed += 1
                if probed > _MAX_PATH_TOKENS:  # same stat-storm bound as above
                    return found
                target = _resolve_operand(word, base)
                if target in seen:
                    continue
                seen.add(target)
                verdict = _rm_target_verdict(target)
                if verdict is not None:
                    found.append((target, verdict))
    return found


# A git option's value argument:
#   _VAL_SP  -- value as a SEPARATE token: quoted, or a bare non-space run.
#   _VAL_EQ  -- value glued onto `=`: quoted, or a (possibly empty) bare run.
#   _VAL_CFG -- one shell word honouring quotes: bare chars and quoted
#               spans in any mix, so `-c name="value with spaces"` is ONE
#               token (a bare `\S+` would stop at the space inside it).
# (A vault folder name can contain a space, so quoted forms matter.)
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
_GIT_OPTS_CAP = r'((?:' + _GIT_OPT + r')*)'   # capturing group: the options blob


def _dash_c_target(opts_blob: str, base_cwd: str, variables=None) -> str:
    """Fold any `git -C <dir>` options from a git options blob onto base_cwd,
    following git's cumulative -C semantics (each -C is relative to the
    previous one). Returns base_cwd unchanged when the blob has no -C."""
    cwd = base_cwd
    for m in re.finditer(r'-C\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))', opts_blob):
        raw = next((g for g in m.groups() if g is not None), None)
        if raw is None:
            continue
        if _LIB_OK:
            raw = expand_vars(raw, variables or {})
        if "$" in raw:
            # UNRESOLVED target (a var assigned outside this command, a command
            # substitution). Folding it would join a literal `$W` onto the cwd
            # and produce a path that resolves to no repo -- i.e. "not a vault",
            # a silent allow. Ignoring it instead lets the cwd decide, which
            # still blocks when that cwd IS a vault.
            continue
        path = os.path.expanduser(raw)
        cwd = path if os.path.isabs(path) else os.path.normpath(os.path.join(cwd, path))
    return cwd


def _targets_vault_repo(cwd: str) -> bool:
    """True iff a git op run from `cwd` would touch a vault repo: the main
    repo, or any worktree of it. Resolves the repo by identity, via
    `git rev-parse --git-common-dir`, so a separate repo reached through a
    vault-namespace symlink returns False. Fails open (False) when the repo
    cannot be determined."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--git-common-dir"],
            # encoding pinned, not left to the locale: this prints a PATH, and
            # a vault path can carry an emoji folder name (U+FE0F, byte 0x8F
            # unmapped in cp1252). text=True alone would raise
            # UnicodeDecodeError inside subprocess.run on a non-UTF-8 console,
            # before we see a byte.
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, env=_GIT_CLEAN_ENV,
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
    """True iff an explicit --git-dir points at a vault repo -- its main .git,
    or a worktree gitdir whose common dir is the vault's. Resolves via
    `git --git-dir=<x> rev-parse --git-common-dir`; falls back to a realpath
    compare of the git dir itself."""
    git_dir = os.path.expanduser(git_dir)
    cands = [git_dir]
    try:
        out = subprocess.run(
            ["git", "--git-dir", git_dir, "rev-parse", "--git-common-dir"],
            # encoding pinned for the same reason as _targets_vault_repo above:
            # the child prints a path, and vault paths can carry non-cp1252 bytes.
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5,
            cwd=git_dir if os.path.isdir(git_dir) else None,
            env=_GIT_CLEAN_ENV,
        )
        if out.returncode == 0 and out.stdout.strip():
            cands.append(os.path.join(git_dir, out.stdout.strip()))
    except Exception:
        pass
    vault_git_dir = _vault_git_dir_for(git_dir)
    if vault_git_dir is None:
        return False
    return any(os.path.realpath(c) == vault_git_dir for c in cands)


def _targets_vault(opts_blob: str, base_cwd: str, variables=None) -> bool:
    """True iff a git invocation carrying this options blob, run from
    base_cwd, would touch a vault repo. An explicit --git-dir is
    authoritative; otherwise targeting follows -C / cwd. Fails open (False)
    when the repo cannot be determined."""
    eff_cwd = _dash_c_target(opts_blob, base_cwd, variables)
    git_dir = _git_dir_arg(opts_blob)
    if _LIB_OK and git_dir is not None:
        git_dir = expand_vars(git_dir, variables or {})
        if "$" in git_dir:
            git_dir = None      # unresolved -> let the cwd decide (see _dash_c_target)
    if git_dir is not None:
        path = os.path.expanduser(git_dir)
        if not os.path.isabs(path):
            path = os.path.normpath(os.path.join(eff_cwd, path))
        return _git_dir_is_vault(path)
    return _targets_vault_repo(eff_cwd)


# (regex, severity, message, scope).
#   severity 'block' -> exit 2 ; 'nudge' -> exit 2 with softer wording.
#   scope 'repo'      -> fire only when the op targets a vault repo. The
#                        regex captures the git options blob as group 1 so
#                        targeting can follow any `git -C <dir>`.
#   scope 'namespace' -> fire whenever the segment is in a vault namespace.
#   scope 'rm-target' -> fire only when a parsed rm target resolves to a vault
#                        root or a direct child of one. The regex is a cheap
#                        prefilter; _rm_verdicts is what actually decides.
#
# Every pattern starts at `_LEAD`, and is matched with re.match against ONE
# segment of the quote-aware split. "Is this a command start?" is therefore
# answered structurally instead of by a regex alternation over operators, which
# removes a whole class of phantom matches the old `(?:^|&&|...)` prefix could
# not distinguish: `echo "later; git push"`, `git commit -m "notes; git push"`
# and a `git push` sitting in a heredoc BODY all matched it and hard-blocked --
# the exact false-block shape that teaches people to reach for the bypass.
SCOPE_REPO, SCOPE_NAMESPACE, SCOPE_RM_TARGET = 'repo', 'namespace', 'rm-target'

RULES = [
    (
        _LEAD + r'git\s+' + _GIT_OPTS_CAP + r'push\b',
        'block',
        "git push in the vault: no remote is configured. This is a local-only snapshot repo. "
        "If you truly need to push, set up a remote first and confirm with the user. "
        "Rule: CLAUDE.md §'Git in this vault'.",
        SCOPE_REPO,
    ),
    (
        _LEAD + r'git\s+' + _GIT_OPTS_CAP + r'status\s*$',
        'block',
        "Unscoped `git status` in a large vault walks the full tree (slow, locks .git/index.lock). "
        "Pass explicit paths: git status -- \"<folder>/\" \"path/to/file.md\" "
        "Or use `git status --short --untracked-files=no -- <path>`.",
        SCOPE_REPO,
    ),
    (
        _RM_RE.pattern,
        'block',
        "rm -rf against a vault root or one of its top-level folders would destroy live work. "
        "Use explicit file paths (deeper than a top-level folder) or move to Archive/ instead.",
        SCOPE_RM_TARGET,
    ),
    (
        _LEAD + r'grep\b(?!\s+--?version|\s+--?help)',
        'nudge',
        "Prefer the Grep tool (or `rg`) over `grep` — faster, proper ignores, no full-tree walks. "
        "If you really need plain grep (e.g. piping fixed stdin), prefix with VAULT_VALIDATOR_BYPASS=1.",
        SCOPE_NAMESPACE,
    ),
    (
        _LEAD + r'find\s+\S+\s+-name\b',
        'nudge',
        "Prefer the Glob tool over `find -name` — faster and respects vault ignores. "
        "If you really need find, prefix with VAULT_VALIDATOR_BYPASS=1.",
        SCOPE_NAMESPACE,
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

    # A genuinely exported variable in this session disarms everything -- that
    # is a deliberate, standing choice by the operator, not a token in a string.
    if os.environ.get("VAULT_VALIDATOR_BYPASS") == "1":
        sys.exit(0)

    session_cwd = os.environ.get("CLAUDE_CWD", data.get("cwd", "")) or os.getcwd()

    # Match against CODE ONLY: heredoc bodies and comment tails are text the
    # shell never runs, and every operator inside them is a fake boundary.
    if _LIB_OK:
        sanitized = strip_noncode(strip_heredoc_bodies(command))
        segs = split_segments_with_seps(sanitized)
    else:
        segs = [("", command)]
    seg_texts = [t for _, t in segs]

    # A SET of possible cwds, not one guess. Block if ANY member is a vault.
    bases, variables = _cwd_candidates(segs, session_cwd)
    if not bases:
        bases = {os.path.expanduser(session_cwd)}

    # THE BYPASS IS SCOPED TO THE COMMAND IT PREFIXES, not to the whole line.
    # Checking it once over the raw string meant a real assignment ANYWHERE
    # disarmed every rule: `git push ; VAULT_VALIDATOR_BYPASS=1` allowed a vault
    # push that had already run before the shell reached the token, and
    # `git push && VAULT_VALIDATOR_BYPASS=1 <other cmd>` -- bypassing the guard
    # for a DIFFERENT command in the chain -- silently un-gated the push too.
    if _LIB_OK:
        seg_bypass = segment_bypass_flags(seg_texts, "VAULT_VALIDATOR_BYPASS")
    else:
        seg_bypass = ["VAULT_VALIDATOR_BYPASS=1" in s for s in seg_texts]

    # Repo-scoped rules consult `git rev-parse` -- run it lazily, and cache by
    # the captured git-options blob.
    repo_cache: dict[str, bool] = {}

    def opts_target_vault(opts_blob: str) -> bool:
        if opts_blob not in repo_cache:
            repo_cache[opts_blob] = any(
                _targets_vault(opts_blob, b, variables) for b in sorted(bases)
            )
        return repo_cache[opts_blob]

    # Segments carrying the bypass are removed from consideration entirely --
    # including from the rm target scan, which reads segments directly.
    live = [s for i, s in enumerate(seg_texts) if not seg_bypass[i]]

    hits = []
    for pattern, severity, message, scope in RULES:
        fired = False
        detail = ""
        if scope == SCOPE_RM_TARGET:
            # The regex only says "a destructive rm is in here somewhere".
            # Naming the resolved targets is what makes the block actionable --
            # and, when it is wrong, obviously wrong to the reader.
            verdicts = _rm_verdicts(live, bases)
            fired = bool(verdicts)
            if fired:
                detail = "\n  Target(s): " + "; ".join(
                    f"{t} ({'vault root' if v == 'root' else 'top-level folder'})"
                    for t, v in verdicts
                )
        else:
            for seg in live:
                m = re.match(pattern, seg.lstrip())
                if not m:
                    continue
                if scope == SCOPE_REPO:
                    # Repo identity decides: block only when this git op really
                    # targets a vault repo.
                    if opts_target_vault(m.group(1) or ""):
                        fired = True
                else:
                    # Namespace rules fire on a vault path in THIS segment --
                    # the command actually being run -- not anywhere in the
                    # whole string.
                    if _seg_in_vault_namespace(seg, bases):
                        fired = True
                if fired:
                    break
        if fired:
            hits.append((severity, message + detail))

    if hits:
        for severity, message in hits:
            tag = "BLOCKED" if severity == "block" else "NUDGE"
            print(f"{tag} by vault-command-nudges hook:\n  {message}", file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    # Windows cp1252-console safety. This hook's block messages quote the
    # RESOLVED target back to the user, and a vault's top-level folders can be
    # emoji-prefixed, so the crashing value arrives from the filesystem even on
    # a machine whose command was pure ASCII. Without this, printing the reason
    # raises UnicodeEncodeError and the block is reported as a hook error
    # instead of a reason -- on the one code path that only ever runs when live
    # work is about to be deleted.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
