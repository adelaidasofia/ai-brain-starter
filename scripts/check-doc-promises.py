#!/usr/bin/env python3
"""Fail RED when a doc promises a command, script, or flag that does not exist.

THE CLASS THIS EXISTS FOR (MYC-4121). This repo is what a brand-new paying
client installs. Eight separate broken promises were found by hand in one
audit: a slash command in the installer's LAST sentence that was never built
(/optimize-brain), a command advertised after its own removal (/mem-search),
scripts documented with a specific ship date that were never added on any
branch, vertical packs sold after deletion, a "git pull to fix it" remedy for
a pack that no longer exists at any pulled ref, a diagnostic that red-flags
the exact thing a test asserts must be off by default, three CLI flags that
appear exactly once in their own skill (the menu line) and nowhere else, and
a cited end-to-end test file that was never written. Nothing caught any of
these before a human read every file by hand. This script is that read,
automated, so it cannot rot back to eight the next time someone edits a doc
faster than the code it describes.

WHAT IT CHECKS, per file under README.md / docs/ (except CHANGELOG.md, which
is an intentionally historical dev log -- see EXCLUDED_DOC_FILES) / phases/ /
skills/*/SKILL.md:

  1. Every backtick- or bold-wrapped slash command (`/foo`, `/foo bar`,
     **/foo**) resolves to a real skill or command -- the union of every
     `name:` in **/SKILL.md, every commands/*.md filename stem, and every
     bare /word inside a SKILL.md's OWN frontmatter block (a skill's own
     claim about its own aliases is authoritative; see
     build_command_vocabulary's docstring for why /journal, /weekly, and
     /health all resolve despite matching no name: or directory). Only the
     first hyphenated token is checked, so `/graphify query` checks
     "graphify" (subcommands are not separately validated).
  2. Every backtick-wrapped `scripts/*.py` reference resolves to a real file,
     checked repo-root-relative, sibling-relative to the referencing file,
     and (for a bare "scripts/NAME.py" with no deeper prefix) against every
     skills/*/scripts/NAME.py -- the documented convention for a skill's own
     bundled scripts.
  3. (cheap extension) Every `--flag` shown in a SKILL.md usage/menu code
     fence appears somewhere else in that skill's own directory. A flag that
     appears exactly once -- the menu line and nowhere else -- is exactly
     the shape of the three inert graphify flags this ticket found by hand.

WHAT IS DELIBERATELY EXEMPT, and why (a guard that cries wolf gets ignored):
  - A line containing "removed in v" (case-insensitive): the repo's own
    correction convention -- keep the historical mention, append a
    parenthetical, do not scrub the record. See docs/RELEASES.md.
  - A line whose table row starts with an HTTP verb (GET/POST/...): that is
    an API route table (docs/AGENTS.md's memory-api endpoints), not a slash
    command, even though `/healthz` matches the same character pattern.
  - NON_COMMAND_TOKENS: Unix top-level directories that happen to start with
    a letter after the leading slash (/tmp, /var, ...); Claude Code's own
    built-in slash commands (/compact, /plugin, ...), which this repo does
    not ship; the superpowers bundle's skill names, which POWER_TOOLS.md
    documents as installed from an EXTERNAL repo (obra/superpowers), not
    this one; docs/SESSION_CLOSE.md's own words for its exit-detector
    keywords ("They are detector keywords, not registered commands");
    "skill-name" / "command", generic placeholders used when documenting the
    naming PATTERN itself, not a real instance of it; and "panel" /
    "decision" / "goal", cross-cutting feature words that appear backticked
    in several skills' own descriptions (coaching, health-context,
    repurpose-talk, note-todos) as a shared concept, never as one skill's
    registered trigger.
  Every exemption above is a documented, bounded category, not an
  appendable list. A skill NAME can never be added to NON_COMMAND_TOKENS to
  silence a real gap -- ship the skill instead.

Run `--self-test` for the negative controls: a fixture doc line naming a
command that will never exist must turn this RED. A guard earns trust only
by failing on the thing it catches.

ASCII-only output on purpose -- see scripts/check-utf8-stdout.py.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hooks"))
from _lib.safe_read import safe_read_text  # noqa: E402

# Every file read in this module calls safe_read_text(...) DIRECTLY, inline
# at the read site -- never through a further wrapper function.
# scripts/check-cloud-safe-file-walkers.py's AST check looks for a direct
# call to one of SAFE_READ_NAMES in the same function that performs the
# recursive glob; a wrapper one level removed reads as an unguarded reader
# to that check, even though it calls safe_read_text internally. This
# module walks the whole doc tree with a recursive glob (**/SKILL.md,
# docs/**/*.md, ...), and a recursive walk over a cloud-synced checkout can
# hand back a placeholder file whose read blocks indefinitely
# (hooks/_lib/safe_read.py's own docstring). A doc-accuracy linter has no
# reason to hang the whole CI job on one placeholder file, so every call
# site uses the pattern `safe_read_text(p, timeout=5.0, max_bytes=4_000_000,
# errors="replace").text or ""` -- non-ok reads simply contribute no
# vocabulary and no findings, the same tolerance a bare errors="replace"
# was reaching for, just bounded in time and size too.

# ---------------------------------------------------------------------------
# Scan surface
# ---------------------------------------------------------------------------

DOC_GLOBS = ["README.md", "docs/**/*.md", "phases/**/*.md", "skills/*/SKILL.md"]

# CHANGELOG.md is a chronological development log ("full development history
# including internal refactors and bug fixes" -- its own framing in
# RELEASES.md). It legitimately mentions removed commands, renamed scripts,
# and fixed-then-reverted flags as a natural function of being a history.
# Scanning it at the same strictness as a live promise would flag accurate
# history as broken. RELEASES.md (the user-facing "what's new") stays in
# scope; only the raw dev log is excluded.
EXCLUDED_DOC_FILES = {"docs/CHANGELOG.md"}

# docs/adr/ is the same category as CHANGELOG.md: an ADR's Context section
# narrates a past design (often one that was built, then deliberately
# removed) to justify the current decision. docs/adr/0003 describes
# scripts/email-gate-hook.py, which the ADR itself says was deleted -- an
# accurate historical record, not a live promise about repo contents.
EXCLUDED_DOC_PREFIXES = ("docs/adr/",)

# ---------------------------------------------------------------------------
# Non-command tokens: bounded, documented categories only (see module
# docstring "WHAT IS DELIBERATELY EXEMPT" for the rationale behind each row).
# ---------------------------------------------------------------------------

NON_COMMAND_TOKENS = {
    # Unix top-level directories, backticked as filesystem paths.
    "tmp", "var", "etc", "usr", "bin", "sbin", "home", "opt", "dev",
    "proc", "sys", "mnt", "root", "lib", "private",
    # Claude Code's own built-in slash commands -- not shipped by this repo.
    "compact", "cost", "docs", "model", "plugin", "mcp", "help", "clear",
    "agents", "resume", "permissions", "init", "review", "usage", "status",
    "config", "hooks", "vim", "bug", "pr-comments", "add-dir",
    "allowed-tools", "ide", "login", "logout", "output-style", "rewind",
    "sandbox", "terminal-setup", "todos", "export", "memory",
    "install-github-app", "statusline", "schedule", "stats",
    # obra/superpowers bundle (POWER_TOOLS.md: installed from an external
    # repo via bootstrap.sh, never shipped inside this one).
    "brainstorming", "systematic-debugging", "test-driven-development",
    "verification-before-completion", "using-git-worktrees",
    "dispatching-parallel-agents", "executing-plans",
    "subagent-driven-development", "writing-plans", "writing-skills",
    "finishing-a-development-branch", "receiving-code-review",
    "requesting-code-review", "using-superpowers",
    # docs/SESSION_CLOSE.md, verbatim: "They are detector keywords, not
    # registered commands." EN + ES + PT exit synonyms.
    "close", "wrap-up", "bye", "done", "finish", "cerrar", "terminar",
    "chao", "fechar", "encerrar", "tchau",
    # Generic placeholders used when documenting the naming PATTERN itself
    # ("...become available as /skill-name commands"; "/skill invocation
    # logging" -- docs/HOOKS_INSTALL.md describing logging for ANY skill).
    "skill-name", "command", "skill",
    # Cross-cutting concept words, backticked in several skills' own
    # descriptions as a shared feature, never as one skill's own trigger.
    "panel", "decision", "goal",
    # Created BY a phase doc's install flow for the specific user going
    # through it (phases/phase-19-23-finish.md, "Create /team-weekly skill"
    # -> writes ~/.claude/skills/team-weekly/SKILL.md from a template). This
    # is disclosed construction at install time, not a claim that this repo
    # ships a team-weekly skill of its own -- it doesn't, by design.
    "team-weekly",
}

# Confirmed references to something this repo does NOT resolve, found by
# this script but not yet decided. Listed here (never silently folded into
# NON_COMMAND_TOKENS, which is reserved for tokens confirmed NOT to be
# commands) so a future pass can grep this exact name and decide: ship it,
# or remove the promise. EMPTY IS THE CORRECT STEADY STATE -- an entry here
# is a debt with a name, not an exemption, and it is never the way to
# silence a finding.
#
# Both original entries were resolved by removing the promise (2026-08-24):
#   /plan (phases/phase-19-23-finish.md) and /code-security
#   (skills/sunday-review/SKILL.md) are both paid-tier capabilities that live
#   in a private repo -- not part of the free substrate.
#   Neither was ever planned or built here (no file on any ref, zero mentions
#   across CHANGELOG.md), and neither is an externally installable tool like
#   the obra/superpowers bundle POWER_TOOLS.md documents, so there is no
#   "install from X" note to write: X is private. The prose was rewritten to
#   drop the command and keep the advice. Layer 3 of the security-cadence
#   model now states plainly that this repo does not ship that layer --
#   security-snapshot (passive, external-only) and secret-warn (edit-time
#   guardrails) both explicitly disclaim deep review, so neither could stand
#   in for it. The "removed in v" convention deliberately does NOT apply:
#   nothing was removed from this repo, and writing it would both fabricate
#   history and buy a green via REMOVED_MARKER instead of earning one.
DEFERRED_UNRESOLVED_TOKENS: set[str] = set()

HTTP_VERBS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")

# `/foo` or `/foo-bar`, optionally followed by more words we don't check.
# Requires a lowercase-start token so it never matches an absolute path like
# `/Users/...` (uppercase) and never matches a fraction like `92/100`.
_BACKTICK_CMD = re.compile(r"`(/[a-z][a-z0-9-]*)(?:\s+[^`]*)?`")
_BOLD_CMD = re.compile(r"\*\*(/[a-z][a-z0-9-]*)\*\*")

# Backtick-wrapped script paths. Captures the full path so a deeper prefix
# (services/health-mcp/scripts/x.py) is checked as literally written, while a
# bare "scripts/NAME.py" also gets the skills/*/scripts/ fallback below.
_SCRIPT_PATH = re.compile(r"`([A-Za-z0-9_./-]*scripts/[A-Za-z0-9_-]+\.py)`")

# A --flag token inside a fenced code block (the usage/menu convention every
# skill in this repo uses for its command reference).
_FLAG_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])(--[a-z][a-z0-9-]*)")

REMOVED_MARKER = "removed in v"


class Finding:
    def __init__(self, kind: str, file: str, line: int, token: str, detail: str):
        self.kind = kind
        self.file = file
        self.line = line
        self.token = token
        self.detail = detail

    def report(self) -> str:
        return f"::error file={self.file},line={self.line}::{self.kind} `{self.token}` {self.detail}"


# ---------------------------------------------------------------------------
# Vocabulary: what actually exists
# ---------------------------------------------------------------------------


_FRONTMATTER = re.compile(r"\A---\n(.*?\n)---\n", re.DOTALL)
_ALIAS_IN_PROSE = re.compile(r"(?<![`\w/])/([a-z][a-z0-9-]*)")


def build_command_vocabulary(root: Path) -> set[str]:
    """Every name a slash-command reference is allowed to resolve to.

    NOT just each skill's `name:` field. A large share of this repo's real
    commands are natural-language aliases declared only in the description
    prose -- /journal routes to the "daily-journal" skill, /weekly and
    /monthly both route to "insights", /health routes to "health-doctor" --
    and the alias never appears as the skill's own name or directory. The
    convention (confirmed across every skill that uses it) is a description
    sentence like "...or says /journal" or "user types /weekly or /monthly".
    Every bare /word inside a SKILL.md's own frontmatter block (name,
    description, trigger, argument-hint) counts as a valid alias for that
    skill -- a skill's own frontmatter is the one place a claim about its own
    triggers is authoritative by construction, not something to verify.
    """
    names: set[str] = set()
    for skill_md in root.glob("**/SKILL.md"):
        if ".git" in skill_md.parts:
            continue
        text = safe_read_text(skill_md, timeout=5.0, max_bytes=4_000_000, errors="replace").text or ""
        m = re.search(r"^name:\s*(\S+)\s*$", text, re.MULTILINE)
        if m:
            names.add(m.group(1).strip())
        # The directory a skill lives in is also a valid /trigger even when
        # name: differs cosmetically (defensive; every skill observed in
        # this repo keeps them in sync, but don't depend on that holding).
        if skill_md.parent != root:
            names.add(skill_md.parent.name)
        fm = _FRONTMATTER.match(text)
        if fm:
            for alias_m in _ALIAS_IN_PROSE.finditer(fm.group(1)):
                names.add(alias_m.group(1))
    commands_dir = root / "commands"
    if commands_dir.is_dir():
        for f in commands_dir.glob("*.md"):
            names.add(f.stem)
    return names


def build_skills_scripts_index(root: Path) -> dict[str, list[Path]]:
    """basename -> every skills/*/scripts/<basename> that exists."""
    index: dict[str, list[Path]] = {}
    for p in root.glob("skills/*/scripts/*.py"):
        index.setdefault(p.name, []).append(p)
    return index


# ---------------------------------------------------------------------------
# Extraction + resolution
# ---------------------------------------------------------------------------


def iter_doc_files(root: Path) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for pattern in DOC_GLOBS:
        for p in sorted(root.glob(pattern)):
            if p in seen or not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if rel in EXCLUDED_DOC_FILES or rel.startswith(EXCLUDED_DOC_PREFIXES):
                continue
            seen.add(p)
            out.append(p)
    return out


def is_exempt_line(line: str) -> bool:
    if REMOVED_MARKER in line.lower():
        return True
    stripped = line.strip()
    if stripped.startswith("|"):
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if cells and cells[0].upper() in HTTP_VERBS:
            return True
    return False


def tokens_with_removed_marker(text: str) -> set[str]:
    """Command/path tokens that appear on a "removed in v..." line ANYWHERE
    in this file. A changelog-style doc often introduces a command in one
    entry's heading and discloses its removal only in a later entry (the
    established convention: see docs/RELEASES.md's extract-rules-from-vault,
    where the removal note sits on a line 8 lines below the entry heading
    that first names it). Scoped to the SPECIFIC token, not the whole file --
    a file may legitimately disclose one removal while still promising
    something else that is genuinely broken."""
    found: set[str] = set()
    for line in text.splitlines():
        if REMOVED_MARKER not in line.lower():
            continue
        found.update(m.group(1)[1:] for m in _BACKTICK_CMD.finditer(line))
        found.update(m.group(1)[1:] for m in _BOLD_CMD.finditer(line))
        found.update(m.group(1) for m in _SCRIPT_PATH.finditer(line))
    return found


def check_commands(root: Path, files: list[Path], vocabulary: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    for f in files:
        rel = f.relative_to(root).as_posix()
        text = safe_read_text(f, timeout=5.0, max_bytes=4_000_000, errors="replace").text or ""
        removed = tokens_with_removed_marker(text)
        for lineno, line in enumerate(text.splitlines(), 1):
            if is_exempt_line(line):
                continue
            candidates = [m.group(1) for m in _BACKTICK_CMD.finditer(line)]
            candidates += [m.group(1) for m in _BOLD_CMD.finditer(line)]
            for raw in candidates:
                token = raw[1:]  # drop leading /
                if token in NON_COMMAND_TOKENS or token in DEFERRED_UNRESOLVED_TOKENS or token in removed:
                    continue
                if token not in vocabulary:
                    findings.append(Finding(
                        "slash command", rel, lineno, raw,
                        "does not match any commands/*.md or skills/*/SKILL.md name:",
                    ))
    return findings


# Vault-relative paths (the user's OWN Obsidian vault, e.g.
# "Meta/scripts/foo.py" or the emoji-prefixed "gear Meta/scripts/foo.py") --
# a file Claude WRITES to or reads from the vault, never something that
# exists in THIS repo. Same category the pre-existing phase-doc-reference
# check in .github/workflows/lint.yml already carves out for scripts/*.sh.
_VAULT_PATH_PREFIXES = ("Meta/", "⚙️ Meta/")


def _contains(parent: Path, child: Path) -> bool:
    """True if child resolves to a path inside parent. Both are resolved
    first so a ../ in `ref` can't walk the check outside the repo -- the
    doc-promises regex's own charset allows '.' and '/' in a script path's
    prefix (it has to, to match a real nested path like
    services/health-mcp/scripts/x.py), so nothing stops a crafted line like
    `../../../etc/scripts/passwd.py` from reaching this function. The
    consequence of NOT checking would only ever be an existence probe (this
    function calls is_file(), never reads content), but a doc-accuracy
    linter has no business resolving anywhere outside its own repo."""
    try:
        parent_r = str(parent.resolve())
        child_r = str(child.resolve())
    except OSError:
        return False
    return child_r == parent_r or child_r.startswith(parent_r + "/")


def resolve_script(root: Path, doc_file: Path, ref: str, skills_scripts: dict[str, list[Path]]) -> bool:
    if ref.startswith(_VAULT_PATH_PREFIXES):
        return True
    candidate = root / ref
    if _contains(root, candidate) and candidate.is_file():
        return True
    candidate = doc_file.parent / ref
    if _contains(root, candidate) and candidate.is_file():
        return True
    if ref.startswith("scripts/") and ref.count("/") == 1:
        basename = ref.split("/", 1)[1]
        if basename in skills_scripts:
            return True
    return False


def check_scripts(root: Path, files: list[Path], skills_scripts: dict[str, list[Path]]) -> list[Finding]:
    findings: list[Finding] = []
    for f in files:
        rel = f.relative_to(root).as_posix()
        text = safe_read_text(f, timeout=5.0, max_bytes=4_000_000, errors="replace").text or ""
        for lineno, line in enumerate(text.splitlines(), 1):
            if is_exempt_line(line):
                continue
            for m in _SCRIPT_PATH.finditer(line):
                ref = m.group(1)
                if not resolve_script(root, f, ref, skills_scripts):
                    findings.append(Finding(
                        "script path", rel, lineno, ref,
                        "does not exist at repo root, next to the referencing doc, or under any skills/*/scripts/",
                    ))
    return findings


def skill_trigger_word(text: str, skill_dir_name: str) -> Optional[str]:
    """The single bare word a skill is invoked with (no leading slash), if
    one exists. Prefers `trigger: /word`; falls back to `name:`. Returns
    None for a multi-phrase trigger field (e.g. skillify-meta-loop's list of
    natural-language patterns) -- those skills are skipped by the flag
    check rather than guessed at."""
    m = re.search(r"^trigger:\s*/([a-z][a-z0-9-]*)\s*$", text, re.MULTILINE)
    if m:
        return m.group(1)
    m = re.search(r"^name:\s*(\S+)\s*$", text, re.MULTILINE)
    if m and re.fullmatch(r"[a-z][a-z0-9-]*", m.group(1)):
        return m.group(1)
    return skill_dir_name


def check_flags(root: Path) -> list[Finding]:
    """Cheap extension: a --flag shown on the skill's OWN usage line (a
    fenced line starting with /<its-trigger>) should be wired somewhere else
    in that skill's own directory -- either the literal flag string again,
    or the matching Python kwarg (--auto-approve ~ auto_approve=), which is
    how graphify's own `/graphify add --author` wires into ingest(author=).
    A flag whose menu line says outright that it's a no-op is not a broken
    promise, it's a disclosed one -- skip it, matching the same-line
    "removed in v" convention used for commands.

    Deliberately scoped to lines starting with the skill's own trigger, not
    every fenced code block: several skills' fences invoke an UNRELATED
    standalone script with its own flag interface (insights' menu-less
    SKILL.md shells out to compress-vault-doc.py --dry-run, which has
    nothing to do with /weekly or /monthly) -- flagging those would be
    penalizing a skill for someone else's CLI."""
    findings: list[Finding] = []
    for skill_md in sorted(root.glob("skills/*/SKILL.md")):
        skill_dir = skill_md.parent
        text = safe_read_text(skill_md, timeout=5.0, max_bytes=4_000_000, errors="replace").text or ""
        trigger = skill_trigger_word(text, skill_dir.name)
        if not trigger:
            continue
        menu_prefix = f"/{trigger}"

        menu_flags: dict[str, int] = {}
        in_fence = False
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if not in_fence or not stripped.startswith(menu_prefix):
                continue
            if "no-op" in line.lower():
                continue
            for m in _FLAG_TOKEN.finditer(line):
                menu_flags.setdefault(m.group(1), lineno)
        if not menu_flags:
            continue

        corpus = text
        for ref_md in skill_dir.glob("**/*.md"):
            if ref_md == skill_md:
                continue
            corpus += "\n" + (safe_read_text(ref_md, timeout=5.0, max_bytes=4_000_000, errors="replace").text or "")

        for flag, lineno in menu_flags.items():
            name = flag[2:]  # strip leading --
            kwarg_pattern = re.escape(name.replace("-", "_")) + r"\s*="
            wired_elsewhere = corpus.count(flag) > 1 or re.search(kwarg_pattern, corpus)
            if not wired_elsewhere:
                rel = skill_md.relative_to(root).as_posix()
                findings.append(Finding(
                    "flag", rel, lineno, flag,
                    f"appears once in {skill_dir.name}'s own docs (the menu line) and nowhere else -- looks unwired",
                ))
    return findings


def scan(root: Path) -> list[Finding]:
    files = iter_doc_files(root)
    vocabulary = build_command_vocabulary(root)
    skills_scripts = build_skills_scripts_index(root)
    findings: list[Finding] = []
    findings += check_commands(root, files, vocabulary)
    findings += check_scripts(root, files, skills_scripts)
    findings += check_flags(root)
    return findings


# ---------------------------------------------------------------------------
# Self-test: negative controls
# ---------------------------------------------------------------------------


def self_test() -> int:
    """The guard must go RED on a fabricated command/script/flag it has
    never seen before, and stay quiet on real, exempt, or historically
    corrected content. A guard earns trust only by failing on the thing it
    catches."""
    failures = 0

    def check(name: str, got: bool, expect: bool) -> None:
        nonlocal failures
        if got == expect:
            print(f"  ok   {name:60s} ({'RED' if got else 'clean'})")
        else:
            print(f"  FAIL {name:60s} expected {'RED' if expect else 'clean'}, got {'RED' if got else 'clean'}")
            failures += 1

    vocabulary = {"graphify", "optimize-brain", "coaching"}
    skills_scripts: dict[str, list[Path]] = {"real_script.py": [Path("skills/x/scripts/real_script.py")]}

    # --- slash commands ---
    fabricated_line = "Just type: **/definitely-not-a-real-command**"
    findings = check_commands(Path("."), [], vocabulary)  # sanity: empty file list -> no findings
    check("empty file list produces no findings", bool(findings), False)

    # Exercise the line-level rules directly (no disk I/O needed).
    check(
        "fabricated bold command is not exempt and not in vocabulary",
        (not is_exempt_line(fabricated_line))
        and _BOLD_CMD.search(fabricated_line) is not None
        and _BOLD_CMD.search(fabricated_line).group(1)[1:] not in vocabulary,
        True,
    )
    real_line = "Run `/graphify` after setup."
    check(
        "real backtick command resolves against vocabulary",
        _BACKTICK_CMD.search(real_line).group(1)[1:] in vocabulary,
        True,
    )
    removed_line = "Trigger: `/vertical-finance init`. (Removed in v1.5.0 as a paid-tier capability.)"
    check("removed-in-v line is exempt", is_exempt_line(removed_line), True)
    table_line = "| GET | `/healthz` | Liveness check |"
    check("HTTP verb table row is exempt", is_exempt_line(table_line), True)
    for tok in ("close", "wrap-up", "bye"):
        check(f"denylisted token '{tok}' is in NON_COMMAND_TOKENS", tok in NON_COMMAND_TOKENS, True)

    # --- script paths ---
    root = Path(".").resolve()
    fake_doc = root / "README.md"  # any real file works as the "referencing file" anchor
    check(
        "fabricated repo-root script path does not resolve",
        resolve_script(root, fake_doc, "scripts/definitely_not_a_real_script_xyz.py", skills_scripts),
        False,
    )
    check(
        "bare scripts/NAME.py resolves via skills/*/scripts/ fallback",
        resolve_script(root, fake_doc, "scripts/real_script.py", skills_scripts),
        True,
    )
    check(
        "deeper-prefixed fabricated path is not silently passed by the fallback",
        resolve_script(root, fake_doc, "services/fake/scripts/real_script.py", skills_scripts),
        False,
    )

    # A ../ reference that genuinely resolves to a REAL file outside the
    # repo must still be refused. The regex's own charset allows '.' and
    # '/' in a script path's prefix (needed to match a real nested path
    # like services/health-mcp/scripts/x.py), so nothing in the extraction
    # step stops a crafted `../../outside/scripts/x.py` -- only _contains()
    # does. Proven against a file that ACTUALLY EXISTS: if this test used a
    # nonexistent traversal target, it would pass for the wrong reason (the
    # file's absence) and never exercise the containment check at all.
    traversal_root = root.parent / "_check_doc_promises_traversal_fixture"
    (traversal_root / "scripts").mkdir(parents=True, exist_ok=True)
    (traversal_root / "scripts" / "evil.py").write_text("# fixture\n", encoding="utf-8")
    try:
        traversal_ref = f"../{traversal_root.name}/scripts/evil.py"
        check(
            "../ traversal to a file that genuinely exists outside root is still refused",
            resolve_script(root, fake_doc, traversal_ref, skills_scripts),
            False,
        )
    finally:
        shutil.rmtree(traversal_root, ignore_errors=True)

    # --- flags ---
    fixture_dir = Path("/tmp/check-doc-promises-selftest")
    shutil.rmtree(fixture_dir, ignore_errors=True)
    (fixture_dir / "skills" / "fixture-skill").mkdir(parents=True)
    (fixture_dir / "skills" / "fixture-skill" / "SKILL.md").write_text(
        "---\nname: fixture-skill\n---\n"
        "## Usage\n```\n/fixture-skill --wired-elsewhere\n/fixture-skill --never-mentioned-again\n```\n"
        "Later: `--wired-elsewhere` is handled by doing the thing.\n",
        encoding="utf-8",
    )
    flag_findings = check_flags(fixture_dir)
    flagged = {f.token for f in flag_findings}
    check("inert flag (menu-only) is flagged", "--never-mentioned-again" in flagged, True)
    check("wired flag (mentioned twice) is not flagged", "--wired-elsewhere" in flagged, False)
    shutil.rmtree(fixture_dir, ignore_errors=True)

    print()
    if failures:
        print(f"check-doc-promises self-test: {failures} FAILED")
        return 1
    print("check-doc-promises self-test: all cases passed")
    return 0


def fixture_test(root: Path) -> int:
    """Full-pipeline negative control: write a real fixture file into the
    repo's own doc surface, run the real scan() entry point end to end, and
    prove it goes RED -- then always clean up. Distinct from self_test()
    above, which exercises the pure functions in isolation; this proves the
    GLOB + FILE-READ + END-TO-END path also catches it, not just the logic."""
    fixture = root / "docs" / "_check_doc_promises_selftest_fixture.md"
    fixture.write_text(
        "This fixture line references a command that will never exist: "
        "`/definitely-not-a-real-command-abc123`.\n",
        encoding="utf-8",
    )
    try:
        findings = scan(root)
        hit = any(f.token == "/definitely-not-a-real-command-abc123" for f in findings)
        if hit:
            print("ok   fixture_test: end-to-end scan() caught the planted fabricated command")
            return 0
        print("FAIL fixture_test: end-to-end scan() did NOT catch the planted fabricated command")
        return 1
    finally:
        fixture.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true", help="run the unit-level negative controls and exit")
    ap.add_argument("--fixture-test", action="store_true", help="run the end-to-end negative control and exit")
    ap.add_argument("--root", default=".", help="repo root")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    root = Path(args.root).resolve()

    if args.fixture_test:
        return fixture_test(root)

    findings = scan(root)
    if not findings:
        print("check-doc-promises: clean -- every slash command, scripts/*.py path, and menu flag checked resolves.")
        return 0

    by_kind: dict[str, int] = {}
    for f in findings:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
        print(f.report())
    print()
    summary = ", ".join(f"{n} {k}" for k, n in sorted(by_kind.items()))
    print(f"::error::check-doc-promises: {len(findings)} broken promise(s) ({summary}).")
    return 1


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
