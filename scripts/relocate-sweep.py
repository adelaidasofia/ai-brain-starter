#!/usr/bin/env python3
"""relocate-sweep.py — after a vault relocation, find every place that still
resolves the OLD path, classify each by whether it actually EXECUTES, and decide
whether it is safe to drop the symlink the move left behind.

WHY
---
`relocate-vault.sh` moves a vault out of a cloud-sync folder and leaves a symlink
at the old path so existing references keep resolving. That symlink is a crutch:
while it is there, any tool that still hardcodes the old path "works" — so you
cannot tell which references are migrated and which are silently riding the
symlink. The day the symlink dies, every un-migrated reference breaks (a cron job
mkdir's a phantom folder at the old path; a hook command points at nothing). This
sweep removes the guesswork: it hunts the old path across every surface a vault
touches, classifies each hit, and gives a go/no-go on dropping the symlink.

WHAT IT CHECKS (the lesson from one real, hard relocation tail)
---------------------------------------------------------------
1. ALL surfaces, not just automation: code-repo source (incl. docstrings +
   comments), JSON config, MCP configs, markdown docs, shell rc — auto-discovered
   (resolve the vault's symlinked-in dirs + walk the code-repo root), because a
   hand-kept root list always lags the surface area (SCAN-SCOPE-BLIND-SPOT).
2. CLASSIFY every hit three ways — blind find/replace is banned:
     - EXECUTED   : a load-bearing position — a code line, a markdown code-span /
                    fenced block, a JSON string VALUE under a live key, a shell
                    command. These RECREATE/BREAK and BLOCK the symlink drop.
     - DOC-POINTER: a human-readable mention — a comment, a docstring, markdown
                    prose. Cosmetic; repoint at leisure; never blocks.
     - KEEP       : intentional — the relocate tooling's own OLD= source, a JSON
                    dict KEY (a dead project key, never looked up), an inert
                    permissions matcher, a `relocate-keep`-marked line.
3. Grep the CANONICAL ref (origin/main) per git repo, NOT only the working tree —
   a stale checked-out branch can HIDE a reference that origin/main still ships
   (WRONG-ARTIFACT-VERIFIED). Working-tree-only (uncommitted) hits are reported
   too, labelled by provenance.
4. A residual REPORT grouped by class + a go/no-go: GO only when zero EXECUTED
   references remain.
5. Characterize the ~/.claude.json blast radius before anyone touches it: dict
   KEYS that hold the old path are cosmetic (dead project keys); string VALUES are
   load-bearing. Report the split and confirm the file parses.

This script NEVER edits anything. It classifies and reports. The repoint is yours;
the symlink drop is `relocate-vault.sh --drop-symlink`, which runs this sweep and
obeys its verdict.

TWO MODES, ONE ENGINE
---------------------
  mode 1 (default, --old):  the one-shot SWEEP above — go/no-go on dropping a symlink.
  mode 2 (--watch):         the ongoing WATCHDOG. Reads the install's OWN move(s) from
                            the manifest relocate-vault.sh writes (never a hardcoded
                            literal), reuses the SAME discovery + classification to ask
                            "has anything drifted back to the old path?", and ALARMS
                            (exit 1 + a verdict cache) on an executed residual, a
                            recreated old directory, or a missing relocated vault root
                            (fail-loud). The SessionStart surfacer + the daily-
                            maintenance cron read its cache. Bug class:
                            DESKTOP-PATH-RECREATOR (a hardcoded old path that mkdir's a
                            phantom folder / breaks tooling every run).

Usage:
  relocate-sweep.py --old <old-vault-path> [--new <new-vault-path>] [options]   # mode 1
  relocate-sweep.py --watch [--config-dir <dir>] [scope options]                # mode 2
  relocate-sweep.py --watch-selftest                                            # mode 2 controls

Options:
  --root <dir>          add a scan root (repeatable). Combined with auto-discovery
                        unless --no-auto-discover.
  --dev-root <dir>      parent dir of code repos to auto-scan (default: ~/dev)
  --config-dir <dir>    Claude Code config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude).
                        Mode 2 reads relocations.json + writes relocate-watch-state.json here.
  --claude-json <file>  the big per-host config to characterize (default: ~/.claude.json)
  --no-auto-discover    scan only the explicit --root paths (+ --claude-json if given)
  --watch               mode 2: watch the manifest's recorded move(s) for drift
  --watch-selftest      mode 2 negative/positive controls (planted recreator/residual/
                        missing-root must alarm; a clean tree must not), then exit
  --json                emit a machine-readable report on stdout
  -h, --help            this help

Exit codes: 0 GO / watch-clean · 1 NO-GO / watch-ALARM · 2 usage
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import tokenize
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

HOME = Path.home()
HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

from _lib.safe_read import MAX_TIMEOUT_S, safe_read_text  # noqa: E402

# Per-file read timeout (s). The shared reader rejects special/offline files
# before open; an unknown placeholder or stalled mount is abandoned at this
# deadline. Overridable via --read-timeout.
READ_TIMEOUT = 5.0

# File suffixes worth scanning on a filesystem walk. Everything else is skipped.
# `.env`/`.envrc` are in scope: an `export VAULT=<oldpath>` or a `cd <oldpath>` in a
# config-env file (e.g. an apprise.env) recreates/breaks tooling exactly like an
# in-script default does (file-type parity with the watchdog's lessons).
SCAN_EXTS = {".py", ".sh", ".bash", ".zsh", ".md", ".compressed", ".txt",
             ".json", ".plist", ".yaml", ".yml", ".toml", ".env", ".envrc", ""}
# Path fragments that are never executable config (caches, vcs internals, logs).
SKIP_SUB = ("/.git/", "/.venv/", "/node_modules/", "/.claude/worktrees/",
            ".jsonl", ".log", ".bak", ".pyc", "/.smart-env/", "/graphify-out/")
# Directory names pruned from any filesystem walk — vcs internals + regenerable
# caches. A walk root is only ever a non-git dir (a git repo is grepped instead),
# so this bounds the cost of scanning e.g. a Claude config subtree.
WALK_PRUNE = {".git", ".venv", "node_modules", "worktrees", "__pycache__",
              ".smart-env", "graphify-out", ".codegraph"}
# Cloud-sync containers — reading a placeholder file inside one BLOCKS while the OS
# downloads it (the demand-paging hazard). Never deep-walk these; they hold docs,
# not code path-recreators anyway.
CLOUD_MARKERS = ("/Library/CloudStorage/", "/Library/Mobile Documents/",
                 "/Dropbox/", "/OneDrive", "/.Trash/")


def _under_cloud_sync(path):
    return any(m in (os.path.realpath(path) + "/") for m in CLOUD_MARKERS)
# Files whose OLD-path references are intentional — the relocate tooling itself
# (migration sources, fixtures) and personal-data scrub token lists.
KEEP_BASENAMES = {"relocate-vault.sh", "relocate-sweep.py", "relocate-machinery-sidecar.sh",
                  "test-relocate-vault.sh", "test-relocate-sweep.py"}
KEEP_PATH_TOKENS = ("scrub-or-die", "gh-harden-repos", "personal-pii-scrub", "/docs/superpowers/")
# A line carrying one of these is an intentional KEEP regardless of file type:
#   relocate-keep    → explicit marker;  OLD=/NEW= → a migration source assignment;
#   .exists()/-e     → a guarded ref that no-ops once the path is gone;
#   assert … not in  → an ANTI-recreator: a test asserting the old path is ABSENT from a
#                      default/config is the OPPOSITE of a recreator, so flagging it would
#                      teach bypass. Narrow by construction — a real recreator line is an
#                      assignment / mkdir, never an `assert … not in`.
KEEP_LINE_RE = re.compile(r"relocate-keep|^\s*(OLD|NEW)=|\.exists\(\)|os\.path\.exists|\[\s+-[ed]\s|\bassert\b.*\bnot in\b")
# JSON keys whose subtree is an inert string-MATCHER list, not executable config.
INERT_JSON_KEYS = {"permissions"}

# Markdown executable-context extraction (CommonMark fences + inline code spans).
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
_INLINE_CODE_RE = re.compile(r"`+([^`\n]+?)`+")

WARNINGS = []
INCOMPLETE_SCANS = []


def _incomplete(message):
    WARNINGS.append(message)
    INCOMPLETE_SCANS.append(message)


# --------------------------------------------------------------------------- #
# patterns                                                                     #
# --------------------------------------------------------------------------- #
def derive_patterns(old_abs):
    """The literal + piecewise forms a hardcoded old path takes in real tooling.

    A literal grep for the full path MISSES the piecewise pathlib form
    `Path.home() / "Desktop" / "Vault"`, which is exactly how an in-script default
    is written — so we search the full path, its ~-relative form, the trailing
    `parent/leaf` slash form, and the quoted piecewise form.
    """
    p = Path(old_abs)
    base, parent = p.name, p.parent.name
    pats = [old_abs]
    try:
        pats.append("~/" + str(p.relative_to(HOME)))
    except ValueError:
        pass
    if parent and base:
        pats.append(parent + "/" + base)
        pats.append('"' + parent + '" / "' + base + '"')
    out, seen = [], set()
    for x in pats:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def line_hits(line, patterns):
    return any(pat in line for pat in patterns)


def abbrev(path_str):
    h = str(HOME)
    return path_str.replace(h, "~") if path_str.startswith(h) else path_str


# --------------------------------------------------------------------------- #
# per-file-type classifiers → list of (lineno, klass, reason)                  #
# --------------------------------------------------------------------------- #
def _file_keep_reason(abspath):
    base = os.path.basename(abspath)
    if base in KEEP_BASENAMES:
        return "relocate tooling / fixture (intentional)"
    for tok in KEEP_PATH_TOKENS:
        if tok in abspath:
            return "scrub-token / example list (intentional)"
    return None


def _classify_python(text, patterns):
    """Column- and AST-aware: a hit inside a `#` comment or a docstring is a
    DOC-POINTER; a hit in a code string (assignment, piecewise expression) is
    EXECUTED. Falls back to a line heuristic if the file won't tokenize/parse on
    this interpreter (conservatively EXECUTED, so go/no-go stays safe)."""
    comment_spans = {}   # lineno -> [(startcol, endcol)]
    tok_ok = True
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                comment_spans.setdefault(tok.start[0], []).append((tok.start[1], tok.end[1]))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        tok_ok = False
    docstring_lines = set()
    ast_ok = True
    try:
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant) \
                    and isinstance(node.value.value, str):
                end = getattr(node, "end_lineno", node.lineno) or node.lineno
                for ln in range(node.lineno, end + 1):
                    docstring_lines.add(ln)
    except (SyntaxError, ValueError):
        ast_ok = False

    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        if not line_hits(line, patterns):
            continue
        if KEEP_LINE_RE.search(line):
            out.append((i, "keep", "relocate-keep / migration source / guarded"))
            continue
        if not tok_ok and not ast_ok:
            klass = "doc-pointer" if line.lstrip().startswith("#") else "executed"
            out.append((i, klass, "python (heuristic fallback)"))
            continue
        # Is every hit on this line inside a comment or a docstring?
        in_code = False
        for pat in patterns:
            col = line.find(pat)
            while col != -1:
                in_comment = any(col >= cs for (cs, ce) in comment_spans.get(i, []))
                in_doc = i in docstring_lines
                if not in_comment and not in_doc:
                    in_code = True
                col = line.find(pat, col + 1)
        if in_code:
            out.append((i, "executed", "python code (load-bearing)"))
        elif i in docstring_lines:
            out.append((i, "doc-pointer", "python docstring"))
        else:
            out.append((i, "doc-pointer", "python comment"))
    return out


def _classify_shell(text, patterns):
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line_hits(line, patterns):
            continue
        if KEEP_LINE_RE.search(line):
            out.append((i, "keep", "relocate-keep / migration source / guarded"))
        elif line.lstrip().startswith("#"):
            out.append((i, "doc-pointer", "shell comment"))
        else:
            out.append((i, "executed", "shell command (load-bearing)"))
    return out


def _classify_markdown(text, patterns):
    """A hit inside a fenced block or an inline code span is EXECUTED (it is a
    command/path); a hit in prose is a DOC-POINTER (a descriptive mention)."""
    out = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    for i, line in enumerate(text.splitlines(), 1):
        is_exec_ctx = False
        if in_fence:
            m = _FENCE_RE.match(line)
            if m and m.group(1)[0] == fence_char and len(m.group(1)) >= fence_len \
                    and m.group(2).strip() == "":
                in_fence = False
                continue
            is_exec_ctx = True
        else:
            m = _FENCE_RE.match(line)
            if m:
                in_fence = True
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
                continue
            for span in _INLINE_CODE_RE.findall(line):
                if line_hits(span, patterns):
                    is_exec_ctx = True
                    break
        if not line_hits(line, patterns):
            continue
        if KEEP_LINE_RE.search(line):
            out.append((i, "keep", "relocate-keep marker"))
        elif is_exec_ctx:
            out.append((i, "executed", "markdown code-span / fenced block"))
        else:
            out.append((i, "doc-pointer", "markdown prose"))
    return out


def _classify_other(text, patterns):
    """plist / yaml / toml / txt / dotfiles: conservative — a comment-prefixed line
    is a DOC-POINTER, anything else with the path is EXECUTED."""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line_hits(line, patterns):
            continue
        s = line.lstrip()
        if KEEP_LINE_RE.search(line):
            out.append((i, "keep", "relocate-keep / migration source / guarded"))
        elif s.startswith("#") or s.startswith(";") or s.startswith("//") or s.startswith("<!--"):
            out.append((i, "doc-pointer", "config comment"))
        else:
            out.append((i, "executed", "config value (load-bearing)"))
    return out


def _line_of(raw, needle):
    idx = raw.find(needle)
    return raw[:idx].count("\n") + 1 if idx != -1 else None


def classify_json(text, patterns):
    """Parse JSON and split hits: a dict KEY holding the old path is cosmetic
    (KEEP — e.g. a dead project key); a string VALUE under a live key is
    load-bearing (EXECUTED); a value under an inert matcher subtree (permissions)
    is KEEP. Returns (findings, dict_keys, string_values, valid)."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return [], 0, 0, False

    findings = []
    counts = {"keys": 0, "values": 0}

    def walk(node, inert):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and line_hits(k, patterns):
                    counts["keys"] += 1
                    findings.append((_line_of(text, k), "keep",
                                     "json dict key (cosmetic — dead key, never looked up)"))
                walk(v, inert or (k in INERT_JSON_KEYS))
        elif isinstance(node, list):
            for v in node:
                walk(v, inert)
        elif isinstance(node, str) and line_hits(node, patterns):
            if inert:
                findings.append((_line_of(text, node), "keep",
                                 "json inert permissions matcher"))
            else:
                counts["values"] += 1
                findings.append((_line_of(text, node), "executed",
                                 "json string value (load-bearing)"))

    walk(data, False)
    return findings, counts["keys"], counts["values"], True


def classify_text(abspath, text, patterns):
    """Dispatch by file type. Returns (findings, file_type) where each finding is
    (lineno, klass, reason)."""
    keep_reason = _file_keep_reason(abspath)
    suffix = Path(abspath).suffix.lower()
    base = os.path.basename(abspath)
    if suffix == ".py":
        ftype = "py"
        found = _classify_python(text, patterns)
    elif suffix in (".sh", ".bash", ".zsh") or base.startswith(".z") or base in (".bashrc", ".bash_profile", ".profile"):
        ftype = "sh"
        found = _classify_shell(text, patterns)
    elif suffix in (".md", ".compressed"):
        ftype = "md"
        found = _classify_markdown(text, patterns)
    elif suffix == ".json":
        ftype = "json"
        f, _dk, _sv, _ok = classify_json(text, patterns)
        found = f
    else:
        ftype = "other"
        found = _classify_other(text, patterns)
    if keep_reason:
        found = [(ln, "keep", keep_reason) for (ln, _k, _r) in found]
    return found, ftype


# --------------------------------------------------------------------------- #
# git helpers                                                                  #
# --------------------------------------------------------------------------- #
def _git(args, cwd=None, timeout=None):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout)


# Vendored / build / cache trees never hold a path-recreator and can be enormous;
# excluding them keeps an --untracked grep from walking e.g. node_modules or a
# Rust target/ dir and hanging the whole sweep on one repo.
_GREP_EXCLUDES = [
    ":(exclude,glob)**/node_modules/**", ":(exclude,glob)**/.venv/**",
    ":(exclude,glob)**/venv/**", ":(exclude,glob)**/target/**",
    ":(exclude,glob)**/dist/**", ":(exclude,glob)**/build/**",
    ":(exclude,glob)**/.next/**", ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/worktrees/**",
]


def git_toplevel(path):
    if not os.path.isdir(path):
        return None
    r = _git(["-C", path, "rev-parse", "--show-toplevel"])
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def resolve_canonical_ref(top):
    """First of origin/main, origin/master, main, master, HEAD that resolves.
    origin/* → authoritative ('canonical'); a local branch/HEAD → 'committed-HEAD'."""
    for ref in ("origin/main", "origin/master", "main", "master", "HEAD"):
        if _git(["-C", top, "rev-parse", "--verify", "--quiet", ref + "^{commit}"]).returncode == 0:
            label = "canonical" if ref.startswith("origin/") else "committed-HEAD"
            return ref, label
    return None, None


def git_grep_files(top, patterns, ref):
    """Files in the repo matching any pattern. ref=None → working tree (tracked +
    untracked); ref set → that committed tree. Returns a set of repo-relative paths."""
    cmd = ["-C", top, "grep", "-l", "-I", "--no-color", "-F"]
    if ref is None:
        cmd.append("--untracked")
    for p in patterns:
        cmd += ["-e", p]
    if ref is not None:
        cmd.append(ref)
    cmd += ["--", "."] + _GREP_EXCLUDES
    try:
        r = _git(cmd, timeout=25)
    except subprocess.TimeoutExpired:
        _incomplete("git grep timed out (>25s) in " + abbrev(top)
                    + " — skipped (large untracked tree?)")
        return set()
    if r.returncode not in (0, 1):  # 1 = no matches; >1 = real error
        _incomplete("git grep failed in " + abbrev(top)
                    + (" @" + ref if ref else " (worktree)"))
        return set()
    files = set()
    prefix = (ref + ":") if ref else ""
    for line in r.stdout.splitlines():
        rel = line[len(prefix):] if prefix and line.startswith(prefix) else line
        files.add(rel)
    return files


def git_show(top, ref, relpath):
    try:
        r = _git(["-C", top, "show", ref + ":" + relpath], timeout=20)
    except subprocess.TimeoutExpired:
        _incomplete("git show timed out: %s:%s" % (ref, relpath))
        return None
    if r.returncode != 0:
        _incomplete("git show failed: %s:%s" % (ref, relpath))
        return None
    return r.stdout


def git_common_dir(top):
    """The shared git dir for a checkout. Every linked worktree of a repo shares
    one; the main checkout's equals its own .git. None if unresolvable."""
    r = _git(["-C", top, "rev-parse", "--git-common-dir"])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    p = r.stdout.strip()
    return os.path.realpath(p if os.path.isabs(p) else os.path.join(top, p))


def _pick_representative(tops):
    """Of several worktrees sharing a common-dir, prefer the main checkout (its
    .git is a real directory), then the shortest path."""
    return sorted(tops, key=lambda t: (0 if os.path.isdir(os.path.join(t, ".git")) else 1, len(t), t))[0]


# --------------------------------------------------------------------------- #
# discovery                                                                    #
# --------------------------------------------------------------------------- #
def discover_roots(old_abs, new_abs, explicit_roots, dev_root, config_dir, auto):
    roots = list(explicit_roots)
    if not auto:
        return _dedupe_existing(roots)
    if new_abs and os.path.isdir(new_abs):
        roots.append(new_abs)
        # The vault's symlinked-in dirs (a separate repo mounted under the vault is
        # a classic blind spot — os.walk won't follow the symlink, so add the target).
        # BUT only follow a target that is itself a git repo (a real code surface).
        # A symlink to a cloud-sync DOC mirror (Google Drive / iCloud) is not a code
        # surface, and deep-walking it BLOCKS on placeholder downloads — the very
        # demand-paging hazard this tool exists to help users escape.
        try:
            for entry in os.listdir(new_abs):
                full = os.path.join(new_abs, entry)
                if os.path.islink(full):
                    target = os.path.realpath(full)
                    if (os.path.isdir(target) and not target.startswith(new_abs + os.sep)
                            and not _under_cloud_sync(target) and git_toplevel(target)):
                        roots.append(target)
        except OSError:
            pass
    if os.path.islink(old_abs):
        roots.append(os.path.realpath(old_abs))
    if dev_root and os.path.isdir(dev_root):
        for entry in sorted(os.listdir(dev_root)):
            full = os.path.join(dev_root, entry)
            if os.path.isdir(full):
                roots.append(full)
    # Scope the Claude config dir to where DEPLOYED tooling lives — never the whole
    # tree: it holds large plugin caches, session transcripts, and snapshots that
    # would balloon the walk to 100k+ files for zero executable-config value.
    if config_dir:
        for sub in ("hooks", "scripts"):
            d = os.path.join(config_dir, sub)
            if os.path.isdir(d):
                roots.append(d)
    return _dedupe_existing(roots)


def discover_explicit_files(config_dir, auto):
    """Top-level config + shell-rc files that sit OUTSIDE any walked dir but can
    hold a load-bearing old-path ref (a hook command, an `export VAULT=`, a `cd`)."""
    if not auto:
        return []
    cands = [os.path.join(config_dir, "settings.json"),
             os.path.join(config_dir, "settings.local.json")]
    for rc in (".zshrc", ".zshenv", ".zprofile", ".zlogin",
               ".bashrc", ".bash_profile", ".profile"):
        cands.append(str(HOME / rc))
    return [c for c in cands if os.path.isfile(c)]


def _dedupe_existing(roots):
    out, seen = [], set()
    for r in roots:
        rp = os.path.realpath(r)
        if rp in seen or not os.path.exists(r):
            continue
        seen.add(rp)
        out.append(r)
    return out


def _skip(path):
    return (any(tok in path for tok in SKIP_SUB)
            or any(m in path for m in CLOUD_MARKERS)
            or os.path.basename(path) == ".claude.json")


# --------------------------------------------------------------------------- #
# scanning                                                                     #
# --------------------------------------------------------------------------- #
def _read_text_bounded(path):
    """Use the shared regular-file boundary and surface every skipped read."""
    result = safe_read_text(path, timeout=READ_TIMEOUT, max_bytes=1_000_000)
    if not result.ok:
        detail = (" (" + result.detail + ")") if result.detail else ""
        _incomplete("safe read skipped [%s]%s: %s"
                    % (result.status, detail, abbrev(str(path))))
        return None
    return result.text


def add_findings(findings, abspath, text, patterns, provenance, repo_label):
    cl, ftype = classify_text(abspath, text, patterns)
    for (ln, klass, reason) in cl:
        snippet = ""
        if ln and 0 < ln <= len(text.splitlines()):
            snippet = text.splitlines()[ln - 1].strip()[:160]
        findings.append({
            "path": abbrev(abspath),
            "line": ln,
            "snippet": abbrev(snippet),
            "file_type": ftype,
            "klass": klass,
            "provenance": provenance,
            "reason": reason,
            "repo": repo_label,
        })


def scan_git_repo(top, patterns):
    """Grep one repo at its canonical ref + working tree; classify each hit.
    Returns (root_summary, findings) so callers can run repos in parallel without
    sharing a mutable findings list across threads."""
    findings = []
    ref, ref_label = resolve_canonical_ref(top)
    canon = git_grep_files(top, patterns, ref) if ref else set()
    work = git_grep_files(top, patterns, None)
    for rel in sorted(canon | work):
        if _skip(rel):
            continue
        abspath = os.path.join(top, rel)
        in_canon, in_work = rel in canon, rel in work
        if in_canon:
            text = git_show(top, ref, rel)
            prov = "both" if in_work else ref_label
        else:
            text = _read_text_bounded(abspath)
            prov = "working-tree"
        if text is None:
            continue
        add_findings(findings, abspath, text, patterns, prov, abbrev(top))
    return {"path": abbrev(top), "kind": "git", "ref": ref}, findings


def scan_fs_root(root, patterns):
    findings = []
    if _under_cloud_sync(root):
        return {"path": abbrev(root), "kind": "fs(skipped: cloud-sync)", "ref": None}, findings
    def walk_error(error):
        _incomplete("filesystem walk failed: " + str(error))

    for dirpath, dirs, files in os.walk(root, onerror=walk_error):
        dirs[:] = [d for d in dirs if d not in WALK_PRUNE]
        for fn in files:
            full = os.path.join(dirpath, fn)
            if _skip(full) or Path(fn).suffix.lower() not in SCAN_EXTS:
                continue
            text = _read_text_bounded(full)
            if text is None or not line_hits(text, patterns):
                continue
            add_findings(findings, full, text, patterns, "filesystem", None)
    return {"path": abbrev(root), "kind": "fs", "ref": None}, findings


# --------------------------------------------------------------------------- #
# main                                                                         #
# --------------------------------------------------------------------------- #
def build_report(args):
    incomplete_start = len(INCOMPLETE_SCANS)
    old_abs = os.path.abspath(os.path.expanduser(args.old))
    new_abs = os.path.abspath(os.path.expanduser(args.new)) if args.new else None
    patterns = derive_patterns(old_abs)

    roots = discover_roots(old_abs, new_abs, args.root, args.dev_root, args.config_dir,
                           not args.no_auto_discover)

    # Group roots into git repos (deduped by toplevel) vs plain filesystem roots.
    repo_tops, fs_roots = {}, []
    for r in roots:
        top = git_toplevel(r)
        if top:
            repo_tops.setdefault(top, True)
        else:
            fs_roots.append(r)

    # Collapse sibling worktrees of ONE repo: they share a git-common-dir and the
    # same canonical ref, so scanning each reports the identical hit N times. Keep
    # the main checkout as the representative. --include-worktrees disables this.
    tops = sorted(repo_tops)
    if tops and not args.include_worktrees:
        groups = {}
        for top in tops:
            groups.setdefault(git_common_dir(top) or top, []).append(top)
        tops = sorted(_pick_representative(g) for g in groups.values())

    findings, scanned = [], []
    # Per-repo git work is I/O-bound (subprocess git grep) — run repos in parallel.
    # A power user's ~/dev can hold 100+ repos; serial would take minutes.
    if tops:
        workers = min(16, (os.cpu_count() or 4) + 4)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for sc, loc in pool.map(lambda t: scan_git_repo(t, patterns), tops):
                scanned.append(sc)
                findings.extend(loc)
    for r in fs_roots:
        sc, loc = scan_fs_root(r, patterns)
        scanned.append(sc)
        findings.extend(loc)

    # Top-level config + shell-rc files (outside any walked dir).
    for f in discover_explicit_files(args.config_dir, not args.no_auto_discover):
        if _skip(f):
            continue
        text = _read_text_bounded(f)
        if text and line_hits(text, patterns):
            add_findings(findings, f, text, patterns, "filesystem", None)

    # Dedupe identical findings reachable via two roots.
    uniq, seen = [], set()
    for f in findings:
        key = (f["path"], f["line"], f["klass"])
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    findings = uniq

    # ~/.claude.json blast radius — characterized separately, always.
    claude_summary = {"path": None, "present": False, "valid": None,
                      "dict_keys": 0, "string_values": 0}
    cj_path = None
    if args.claude_json:
        cj_path = os.path.abspath(os.path.expanduser(args.claude_json))
    elif not args.no_auto_discover:
        cj_path = str(HOME / ".claude.json")
    if cj_path:
        text_result = safe_read_text(cj_path, timeout=READ_TIMEOUT, max_bytes=1_000_000)
        if text_result.status == "missing":
            text_result = None
    else:
        text_result = None
    if text_result is not None:
        claude_summary["path"] = abbrev(cj_path)
        claude_summary["present"] = True
        text = text_result.text if text_result.ok else None
        if text is None:
            claude_summary["valid"] = False
            _incomplete("safe read skipped [%s]: %s"
                        % (text_result.status, abbrev(cj_path)))
        else:
            cj_findings, dk, sv, valid = classify_json(text, patterns)
            claude_summary["valid"] = valid
            claude_summary["dict_keys"] = dk
            claude_summary["string_values"] = sv
            if not valid:
                _incomplete("~/.claude.json did not parse — characterize it by hand before editing")
            for (ln, klass, reason) in cj_findings:
                snippet = ""
                if ln and 0 < ln <= len(text.splitlines()):
                    snippet = text.splitlines()[ln - 1].strip()[:160]
                findings.append({
                    "path": abbrev(cj_path), "line": ln, "snippet": abbrev(snippet),
                    "file_type": "json", "klass": klass, "provenance": "filesystem",
                    "reason": reason, "repo": None,
                })

    counts = {"executed": 0, "doc-pointer": 0, "keep": 0}
    for f in findings:
        counts[f["klass"]] = counts.get(f["klass"], 0) + 1
    incomplete = _dedupe(INCOMPLETE_SCANS[incomplete_start:])
    verdict = "NO-GO" if counts["executed"] > 0 or incomplete else "GO"

    return {
        "old": old_abs,
        "new": new_abs,
        "patterns": patterns,
        "roots": scanned,
        "findings": findings,
        "claude_json": claude_summary,
        "counts": {"executed": counts["executed"],
                   "doc_pointer": counts["doc-pointer"],
                   "keep": counts["keep"]},
        "warnings": list(WARNINGS),
        "incomplete": incomplete,
        "verdict": verdict,
    }


def print_human(rep):
    print("relocate-sweep")
    print("  old path: " + abbrev(rep["old"]))
    if rep["new"]:
        print("  new path: " + abbrev(rep["new"]))
    gitn = sum(1 for r in rep["roots"] if r["kind"] == "git")
    print("  scanned %d root(s) (%d git repo(s) at canonical ref where present, else HEAD; + working tree)"
          % (len(rep["roots"]), gitn))
    for w in rep["warnings"]:
        print("  WARN: " + w)

    def group(klass):
        return [f for f in rep["findings"] if f["klass"] == klass]

    ex = group("executed")
    print("\nEXECUTED — blocks symlink drop (%d):" % len(ex))
    if not ex:
        print("  (none)")
    for f in sorted(ex, key=lambda x: (x["path"], x["line"] or 0)):
        loc = f["path"] + (":" + str(f["line"]) if f["line"] else "")
        print("  - [%s] %s  %s" % (f["provenance"], loc, f["snippet"]))

    dp = group("doc-pointer")
    print("\nDOC-POINTER — cosmetic, repoint at leisure (%d):" % len(dp))
    for f in sorted(dp, key=lambda x: (x["path"], x["line"] or 0)):
        loc = f["path"] + (":" + str(f["line"]) if f["line"] else "")
        print("  - %s  (%s)" % (loc, f["reason"]))

    kp = group("keep")
    print("\nKEEP — intentional, left as-is (%d):" % len(kp))
    for f in sorted(kp, key=lambda x: (x["path"], x["line"] or 0)):
        loc = f["path"] + (":" + str(f["line"]) if f["line"] else "")
        print("  - %s  (%s)" % (loc, f["reason"]))

    cj = rep["claude_json"]
    if cj["present"]:
        valid = "valid" if cj["valid"] else "DID NOT PARSE"
        print("\n~/.claude.json blast radius: %d dead dict-key(s) (cosmetic) / %d string-value(s) (load-bearing)  [%s]"
              % (cj["dict_keys"], cj["string_values"], valid))
        print("  (this sweep does not edit it — repoint the string VALUES by hand, then re-validate the JSON)")

    print("\nVERDICT: " + rep["verdict"])
    if rep["verdict"] == "NO-GO":
        print("  %d executed reference(s); %d incomplete read(s). Fix/repoint, then re-run."
              % (rep["counts"]["executed"], len(rep.get("incomplete", []))))
        print("  The symlink at the old path is doing real work — do NOT drop it yet.")
    else:
        print("  Zero executed references. Safe to drop the symlink:")
        print("    relocate-vault.sh --drop-symlink " + abbrev(rep["old"]))


# --------------------------------------------------------------------------- #
# watch — mode 2 of the SAME engine (no second engine, no hand-kept root list)  #
# --------------------------------------------------------------------------- #
def manifest_path(config_dir):
    return os.path.join(config_dir, "relocations.json")


def watch_cache_path(config_dir):
    return os.path.join(config_dir, "relocate-watch-state.json")


def acquire_watch_lock(config_dir):
    """Single-instance lock for --watch. The SessionStart surfacer spawns --watch
    detached when its cache is stale; several sessions starting at once (3 panes in
    the morning) would otherwise launch N concurrent ~/dev+vault CORPUS WALKS — the
    MYC-570 freeze class (4 concurrent walks pegged a Mac to load 36). The cooldown
    stamp narrows the window but has a read-then-write race; this flock closes it at
    the engine, covering EVERY caller (surfacer spawn + cron) at once.

    Returns (handle, should_run): keep `handle` open for the process lifetime (the OS
    releases it on exit). should_run False = another --watch already holds it → skip
    cleanly. On a platform without fcntl (Windows) there is no lock — run anyway and
    lean on the cooldown stamp, so the watch is never silently dead there."""
    try:
        import fcntl
    except ImportError:
        return None, True
    try:
        os.makedirs(config_dir, exist_ok=True)
        fh = open(os.path.join(config_dir, ".relocate-watch.lock"), "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh, True
    except OSError:
        return None, False


def read_manifest(config_dir):
    """The install's OWN relocation record, written by relocate-vault.sh: a list of
    {old, new, symlink, at}. Missing → [] (a never-relocated install has nothing to
    watch — clean no-op). Unparseable / wrong-shape → [] + a LOUD warning, never a
    silent blind spot."""
    path = manifest_path(config_dir)
    result = safe_read_text(path, timeout=READ_TIMEOUT, max_bytes=1_000_000)
    if result.status == "missing":
        return []
    if not result.ok:
        WARNINGS.append("relocation manifest read skipped [%s]: %s"
                        % (result.status, abbrev(path)))
        return None
    try:
        data = json.loads(result.text)
    except ValueError:
        WARNINGS.append("relocation manifest did not parse: " + abbrev(path)
                        + " — the watchdog is blind until it is fixed or removed")
        return None
    if not isinstance(data, list):
        WARNINGS.append("relocation manifest is not a JSON list: " + abbrev(path))
        return None
    def valid_entry(entry):
        if not isinstance(entry, dict):
            return False
        for key in ("old", "new"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                return False
        return True

    if not all(valid_entry(entry) for entry in data):
        WARNINGS.append("relocation manifest contains an invalid entry: " + abbrev(path))
        return None
    return data


def old_path_recreated(old_abs):
    """True when a REAL directory sits at the old path. Post-relocation the old path
    is either the symlink relocate-vault.sh leaves (fine) or absent once the symlink
    is dropped (fine). A real directory there means a tool that still hardcodes the
    old path mkdir'd a phantom folder — the recreator bug, caught the instant it
    happens with one stat (no walk)."""
    return os.path.exists(old_abs) and not os.path.islink(old_abs) and os.path.isdir(old_abs)


def _watch_args_for(base, old_abs, new_abs):
    """A build_report() args object for ONE recorded move, inheriting the scan-scope
    knobs (root / dev_root / no_auto_discover / config_dir / claude_json) from the CLI
    so the watch auto-discovers in production yet is hermetically scopeable in tests."""
    return SimpleNamespace(
        old=old_abs, new=new_abs,
        root=list(getattr(base, "root", []) or []),
        dev_root=getattr(base, "dev_root", None) or str(HOME / "dev"),
        config_dir=getattr(base, "config_dir", None) or str(HOME / ".claude"),
        no_auto_discover=getattr(base, "no_auto_discover", False),
        include_worktrees=getattr(base, "include_worktrees", False),
        claude_json=getattr(base, "claude_json", None),
    )


def _dedupe(seq):
    out, seen = [], set()
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _write_watch_cache(config_dir, payload):
    """Atomically write the verdict the SessionStart surfacer + the cron read."""
    try:
        os.makedirs(config_dir, exist_ok=True)
        path = watch_cache_path(config_dir)
        tmp = path + ".tmp"
        Path(tmp).write_text(json.dumps(payload, indent=2))
        os.replace(tmp, path)
    except OSError as ex:
        WARNINGS.append("could not write watch cache: " + str(ex))


def run_watch(args):
    """Mode 2: scan the install's OWN recorded move(s) for drift back to an old path,
    write a verdict cache, return the payload. ALARM (verdict='ALARM') when ANY move
    has an executed residual reference, a recreated old directory, or a missing
    new/vault root (fail-loud). Reuses build_report() — no second engine."""
    config_dir = args.config_dir
    manifest_entries = read_manifest(config_dir)
    manifest_error = manifest_entries is None
    entries = manifest_entries or []
    executed_findings, recreated, missing_roots, incomplete = [], [], [], []

    for e in entries:
        old_abs = os.path.abspath(os.path.expanduser(e["old"]))
        new_abs = os.path.abspath(os.path.expanduser(e["new"]))
        # fail-loud: the relocated vault is supposed to live at `new`. If it does not,
        # the move is broken (vault gone / moved again) — the watch must surface it.
        if not os.path.exists(new_abs):
            missing_roots.append(abbrev(new_abs))
        if old_path_recreated(old_abs):
            recreated.append(abbrev(old_abs))
        # residual scan via the SAME engine (mode 1). new=None when absent so discovery
        # never tries to walk a path that is not there.
        rep = build_report(_watch_args_for(args, old_abs, new_abs if os.path.isdir(new_abs) else None))
        incomplete.extend(rep.get("incomplete", []))
        for f in rep["findings"]:
            if f["klass"] == "executed":
                loc = f["path"] + ((":" + str(f["line"])) if f["line"] else "")
                executed_findings.append(loc + ("  " + f["snippet"] if f["snippet"] else ""))

    executed_findings = _dedupe(executed_findings)
    incomplete = _dedupe(incomplete)
    alarm = bool(manifest_error or executed_findings or recreated or missing_roots or incomplete)
    payload = {
        "ts": time.time(),
        "verdict": "ALARM" if alarm else "OK",
        "entries": len(entries),
        "manifest_error": manifest_error,
        "executed": len(executed_findings),
        "findings": executed_findings[:50],
        "recreated": _dedupe(recreated),
        "missing_roots": _dedupe(missing_roots),
        "incomplete": incomplete,
        "warnings": _dedupe(list(WARNINGS)),
    }
    _write_watch_cache(config_dir, payload)
    return payload


def print_watch(payload):
    print("relocate-watch")
    print("  recorded moves watched: %d" % payload["entries"])
    if payload["entries"] == 0:
        print("  (no relocations recorded in the manifest — nothing to watch)")
    for w in payload.get("warnings", []):
        print("  WARN: " + w)
    if payload["recreated"]:
        print("\nRECREATED old path(s) — a real directory reappeared (a hardcoded old path mkdir'd it):")
        for p in payload["recreated"]:
            print("  - " + p)
    if payload["missing_roots"]:
        print("\nMISSING relocated vault root(s) — not where the manifest says (FAIL-LOUD):")
        for p in payload["missing_roots"]:
            print("  - " + p)
    if payload.get("manifest_error"):
        print("\nMANIFEST ERROR — relocation state exists but could not be read completely.")
    if payload.get("incomplete"):
        print("\nINCOMPLETE scan(s) — no healthy verdict may be published:")
        for item in payload["incomplete"]:
            print("  - " + item)
    print("\nEXECUTED residual reference(s) to an old path (%d):" % payload["executed"])
    if not payload["findings"]:
        print("  (none)")
    for loc in payload["findings"]:
        print("  - " + loc)
    print("\nVERDICT: " + payload["verdict"])
    if payload["verdict"] == "ALARM":
        print("  Drift detected. Repoint the executed references / remove the recreated folder, then re-run.")
    else:
        print("  No drift — every recorded move is clean.")


def run_watch_selftest():
    """Negative + positive controls for mode 2 (a guard earns trust only by failing on
    the thing it catches): a planted recreated dir, a planted executed residual, and a
    missing vault root MUST each alarm; a clean tree MUST NOT. Hermetic — temp dirs
    only, fictional paths, no network, no real ~/dev."""
    import shutil
    import tempfile
    global INCOMPLETE_SCANS, WARNINGS
    failures = []

    def _case(name, setup, expect_alarm):
        tmp = tempfile.mkdtemp(prefix="relocate-watch-selftest-")
        try:
            cfg = os.path.join(tmp, "config")
            scan = os.path.join(tmp, "scan")
            old = os.path.join(tmp, "old-vault")
            new = os.path.join(tmp, "new-vault")
            os.makedirs(cfg)
            os.makedirs(scan)
            os.makedirs(new)
            setup(old, new, scan)
            manifest = [{"old": old, "new": new, "symlink": os.path.islink(old), "at": "selftest"}]
            Path(os.path.join(cfg, "relocations.json")).write_text(json.dumps(manifest))
            WARNINGS = []
            INCOMPLETE_SCANS = []
            args = SimpleNamespace(config_dir=cfg, root=[scan], dev_root=os.path.join(tmp, "nodev"),
                                   no_auto_discover=True, include_worktrees=False, claude_json=None)
            payload = run_watch(args)
            got = payload["verdict"] == "ALARM"
            ok = got == expect_alarm
            print("  [%s] %s: alarm=%s expected=%s" % ("ok" if ok else "FAIL", name, got, expect_alarm))
            if not ok:
                failures.append(name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _recreate(old, new, scan):
        os.makedirs(old)  # a real directory at the old path = a recreator ran

    def _residual(old, new, scan):
        Path(os.path.join(scan, "recreator.sh")).write_text('#!/bin/bash\nmkdir -p "%s/sub"\n' % old)

    def _missing(old, new, scan):
        shutil.rmtree(new)  # the relocated vault root is gone

    _case("clean tree (must NOT alarm)", lambda old, new, scan: None, False)
    _case("recreated old dir (MUST alarm)", _recreate, True)
    _case("executed residual ref (MUST alarm)", _residual, True)
    _case("missing vault root (MUST alarm, fail-loud)", _missing, True)

    if failures:
        print("WATCH SELF-TEST FAILED (%d): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("WATCH SELF-TEST OK: alarms fire on recreator/residual/missing-root; clean tree stays green.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True, description="code-repo-aware vault-relocation residual sweep + watchdog")
    ap.add_argument("--old", default=None)
    ap.add_argument("--new", default=None)
    ap.add_argument("--watch", action="store_true",
                    help="mode 2: watch the manifest's recorded move(s) for drift; alarm + write a verdict cache")
    ap.add_argument("--watch-selftest", action="store_true",
                    help="run the mode-2 negative/positive controls (planted recreator/residual/missing-root), then exit")
    ap.add_argument("--root", action="append", default=[])
    ap.add_argument("--dev-root", default=str(HOME / "dev"))
    ap.add_argument("--config-dir", default=os.environ.get("CLAUDE_CONFIG_DIR", str(HOME / ".claude")))
    ap.add_argument("--claude-json", default=None)
    ap.add_argument("--no-auto-discover", action="store_true")
    ap.add_argument("--include-worktrees", action="store_true",
                    help="scan every sibling git worktree separately (default: one per repo)")
    ap.add_argument("--read-timeout", type=float, default=5.0,
                    help="per-file read timeout in seconds (default 5; a slower file is skipped, never blocks)")
    ap.add_argument("--json", action="store_true")
    # argparse exits 2 on a missing required arg / bad usage, which is our usage code.
    args = ap.parse_args(argv)
    if (
        not math.isfinite(args.read_timeout)
        or args.read_timeout <= 0
        or args.read_timeout > MAX_TIMEOUT_S
    ):
        ap.error(f"--read-timeout must be finite and within (0, {MAX_TIMEOUT_S:g}]")
    global READ_TIMEOUT
    READ_TIMEOUT = args.read_timeout

    # mode 2: the ongoing watchdog (manifest-driven, no --old).
    if args.watch_selftest:
        return run_watch_selftest()
    if args.watch:
        _lock, should_run = acquire_watch_lock(args.config_dir)
        if not should_run:
            # Another --watch is already scanning — skip rather than stack a second
            # concurrent corpus walk (the MYC-570 freeze class). Not an alarm; exit 0.
            print("relocate-watch: another scan is already running — skipping this one.")
            return 0
        payload = run_watch(args)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print_watch(payload)
        return 1 if payload["verdict"] == "ALARM" else 0

    # mode 1: the one-shot sweep.
    if not args.old:
        ap.error("--old is required for the sweep (or use --watch / --watch-selftest)")
    rep = build_report(args)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print_human(rep)
    return 1 if rep["verdict"] == "NO-GO" else 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
