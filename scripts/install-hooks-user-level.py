#!/usr/bin/env python3
"""
install-hooks-user-level.py — install ai-brain-starter hooks at USER level.

Closes mycelium-hq/ai-brain-starter#6 — UserPromptSubmit hooks silently fail
in worktrees when installed at project level. User-level hooks
(~/.claude/settings.json) fire universally regardless of worktree.

What it does:
  1. Reads the canonical hooks.json from the skill repo
  2. Reads existing ~/.claude/settings.json (preserves all user content)
  3. Merges ai-brain-starter hooks into the user-level config
  4. De-duplicates by command-string fingerprint (idempotent re-runs)
  5. Backs up the existing settings.json before edit
  6. Verifies post-write JSON validity, rolls back on parse error

Safety:
  - Backup at ~/.claude/settings.json.bak-{timestamp} before any edit
  - JSON validity verified after write; rollback on parse error
  - Custom user hooks NEVER removed (we only add ai-brain-starter entries)
  - Idempotent: a second run detects already-installed hooks via fingerprint
  - --dry-run shows the planned merge without writing
  - --uninstall removes ONLY the ai-brain-starter entries (matched by
    fingerprint substring); leaves everything else intact

Usage:
  python3 install-hooks-user-level.py                          # install
  python3 install-hooks-user-level.py --dry-run                # preview
  python3 install-hooks-user-level.py --uninstall              # remove
  python3 install-hooks-user-level.py --hooks-source PATH      # custom source
  python3 install-hooks-user-level.py --quiet                  # only summary
  python3 install-hooks-user-level.py --verify                 # install, then verify
  python3 install-hooks-user-level.py --verify-only            # verify only, writes nothing

Why user-level: project-level hooks at <project>/.claude/settings.json
silently don't fire when cwd is inside <project>/.claude/worktrees/<name>/.
The Claude Code hook resolver appears to treat .claude/ as a boundary it
won't cross when looking up project hooks. User-level config is universal —
fires on every session regardless of cwd.

We ship hooks.json as the canonical source. This script is the install
mechanism; the source-of-truth content lives in hooks.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path


# Decode child-process output as UTF-8, never as the console code page.
#
# `text=True` alone decodes with locale.getpreferredencoding(False) — cp1252 on
# a Spanish/Portuguese/French Windows, cp437 under cmd.exe, ASCII in a C-locale
# pipe. Every child this installer runs prints the VAULT PATH, and that path
# contains "⚙️ Meta" — U+FE0F decodes to byte 0x8F under cp1252, which has no
# mapping, so subprocess.run() raises UnicodeDecodeError before we ever see the
# output. link_agent_memory_into_vault() then reported "could NOT link Claude
# Code memory into the vault" on a machine where the linker itself was fine,
# and the user's memory stayed in ~/.claude — the exact outcome that script
# exists to prevent.
#
# This is the READ side of the cp1252 bug that #313 fixed on the WRITE side
# (the sys.stdout.reconfigure guard at the bottom of this file, enforced fleet-
# wide by scripts/check-utf8-stdout.py). Our children are our own scripts and
# they all emit UTF-8. errors="replace" keeps a genuinely undecodable byte from
# turning a warning into a crash: a mojibake character in a diagnostic is
# always better than losing the diagnostic.
_TEXT_UTF8 = {"text": True, "encoding": "utf-8", "errors": "replace"}


# Fingerprint substrings — any hook command containing one of these is
# considered "owned by ai-brain-starter" and may be replaced or removed.
# The name must be unique enough that no third-party hook would accidentally
# include it.
#
# !! THIS LIST DOES NOT INSTALL ANYTHING. !!
#
# Membership here confers OWNERSHIP only: dedup, replace, retire, uninstall
# recognition. `merge_hooks()` wires exactly what `hooks.json` declares and
# nothing else, so **hooks.json is the ONLY activation predicate**. A hook
# added here but not to hooks.json is an "owned dormant hook": the installer
# claims it, inspection makes it look registered, and it never fires.
#
# Adding a hook therefore takes BOTH:
#   1. an entry in hooks.json  <- this is what actually installs it
#   2. an entry here (+ ABS_OWNED_BASENAMES) so re-installs dedup it properly
#
# If it signals by EXIT CODE 2 (a blocking gate), wire it in hooks.json as
# `if [ -f <path> ]; then <path>; else <allow-json>; fi` — the common
# `<path> 2>/dev/null || echo <allow-json>` idiom discards the stderr message
# AND rewrites the block into an allow.
#
# This comment replaced "Extend this list when adding new hooks", which read as
# the complete instruction and is how the most recent dormant hook happened:
# the author registered in both lists below, believed it was active, and said
# so in the commit message. Tracked as MYC-1031 (structural CI gate).
ABS_FINGERPRINTS = [
    "ai-brain-starter/hooks/detect-closing-signal.py",
    "ai-brain-starter/hooks/verify-session-close-cascade.py",
    "ai-brain-starter/hooks/lint-vault-frontmatter.py",
    "ai-brain-starter/hooks/log-skill-usage.py",
    "ai-brain-starter/hooks/first-week-checkin.py",
    "ai-brain-starter/hooks/migrate-to-user-level.py",
    "ai-brain-starter/hooks/inject-love-language-context.py",
    "ai-brain-starter/hooks/inject-meeting-workflow-on-trigger.py",
    "ai-brain-starter/scripts/session-end-hook.sh",
    "ai-brain-starter/scripts/email-gate-hook.py",
    "ai-brain-starter/scripts/post-update-email-ask.py",
    "ai-brain-starter/⚙️ Meta/scripts/graph-context-hook.sh",
    # Session-start context loaders (MYC-2359: moved UserPromptSubmit -> SessionStart;
    # must be OWNED so the installer relocates the stale old-event copy, else a moved
    # hook fires on BOTH events on every existing install):
    "ai-brain-starter/hooks/session-start-context.py",
    "ai-brain-starter/hooks/inject-instinct-context.py",
    # Legacy inline session-start loader (pre-script echo form):
    "SESSION START: CLAUDE.md is already auto-loaded",
    # Auto-update (MYC-720): the hook now runs an EXTRACTED script; identify it by
    # that path. The OLD inline auto-update blob (unique substring
    # ".ai-brain-starter-last-update") is RETIRED below, so a re-install removes it
    # and installs this fresh — merge alone would leave BOTH wired (double-fire).
    # The bash form (.sh) is itself RETIRED below in favor of the cross-platform
    # .py; it stays in this list so uninstall still recognizes it.
    "ai-brain-starter/scripts/ai-brain-auto-update.sh",
    "ai-brain-starter/scripts/ai-brain-auto-update.py",
    # Worktree-lifecycle hooks (cleanup + footprint observability):
    "ai-brain-starter/hooks/snapshot-pending-work-on-stop.py",
    "ai-brain-starter/hooks/surface-orphan-worktree-snapshots.py",
    "ai-brain-starter/hooks/remove-ended-worktree.py",
    "ai-brain-starter/hooks/enforce-worktree-cap.py",
    "ai-brain-starter/hooks/worktree-footprint-signal.py",
    # Worktree HEAD-isolation gate. The other worktree hooks CLEAN UP after a
    # worktree; this one PREVENTS the drift (a `cd` from a worktree session into
    # the shared main checkout). Shipped + tested since MYC-782 but unregistered
    # here until now: present on disk, dormant in behavior on every fresh
    # install (ARTIFACT-WITHOUT-ACTIVATION).
    "ai-brain-starter/hooks/check-cd-outside-worktree.py",
    # In-flight git-operation gate (incident 2026-07-28). Sibling to the gate
    # above, same bug family one layer deeper: that one keeps a session's HEAD
    # isolated, this one refuses to mutate a repo whose .git is ALREADY
    # mid-operation (paused rebase, unresolved merge, stopped cherry-pick).
    # MODEL-GENERAL — any agent, any repo, anything that shells out to git can
    # commit into a stalled rebase — so it belongs in the substrate and is
    # ACTIVATED here, not left in one machine's ~/.claude (MYC-1017).
    "ai-brain-starter/hooks/block-git-mutation-mid-operation.py",
    # MCP secret-leak guards (MYC-3560). Written after three real GitHub PAT
    # leaks, shipped as working files, and never once registered -- the
    # protection everyone believed was in place did not exist. Same
    # if/then/else reasoning as the two gates above: both block by exiting 2
    # with remediation prose on STDERR, so the `2>/dev/null || echo <allow>`
    # idiom would silently rewrite the block into an allow.
    "ai-brain-starter/hooks/block-claude-mcp-inline-secret.py",
    "ai-brain-starter/hooks/block-mcp-config-inline-secret.py",
    # Auto-remediation (the FIX side of the surfacing hooks):
    "ai-brain-starter/hooks/remediate-runaway-procs.py",
    # Write-time secret guard:
    "ai-brain-starter/hooks/block-secret-in-note.py",
    # Write-time privacy guard: a `__SKIP` line is content the user told the
    # assistant NOT to persist. MODEL-GENERAL -- any agent drafting a life
    # record from conversation can carry the line through to the file, and a
    # persisted line cannot be un-persisted (file + git history + any index
    # over the vault). Blocks, because a warning that is ignored still writes.
    "ai-brain-starter/hooks/block-skip-prefix-in-vault-write.py",
    # Handoff lifecycle guard (issue #375). Shipped since the handoff-files rule
    # existed but was never registered here, so templates/rules/handoff-files.md
    # documented an enforcement that did nothing on every install
    # (ARTIFACT-WITHOUT-ACTIVATION).
    "ai-brain-starter/hooks/validate-handoff-frontmatter.py",
    # Write-time template-purity guard (MYC-1765, structural isolation plane):
    "ai-brain-starter/hooks/block-populated-public-skill.py",
    # Write-time reusable-workflow permission guard. A callee asking for a scope
    # its caller never grants makes GitHub refuse to START the run:
    # startup_failure, zero jobs, no annotation, no check-run. Single-file
    # linters cannot see it — the defect lives BETWEEN two files.
    "ai-brain-starter/hooks/warn-workflow-call-permission-elevation.py",
    # Context-budget measurer (always-loaded text layer; MYC-619):
    "ai-brain-starter/hooks/context-budget-measure.py",
    # Vault-in-worktree melt tripwire (3-channel detect; SessionStart + tool-time + dedup):
    "ai-brain-starter/hooks/warn-vault-session-in-worktree.py",
    # Memory-routing nudge (team learning written to tool-private memory → shared brain):
    "ai-brain-starter/hooks/warn-learning-to-tool-private-memory.py",
    # Bare ~/dev hub-rot guard (read-time detection) + surfacer (MYC-1893):
    "ai-brain-starter/hooks/warn-stale-dev-checkout.py",
    "ai-brain-starter/hooks/dev-hub-refresh-on-session-start.py",
    # Client-side deployed==committed drift detector (MYC-2507): surfaces when this
    # deploy step itself failed silently and settings.json fell behind hooks.json.
    "ai-brain-starter/hooks/surface-deployed-hooks-behind.py",
    # Its sibling, for the other direction (MYC-1031 item 1 / MYC-3880): the one
    # above catches settings.json falling BEHIND hooks.json; this one catches a
    # SessionStart hook being pruned OUT of settings.json by a linter, a manual
    # edit, or a parallel session -- drift hooks.json cannot see. Shipped dormant
    # since MYC-1031 and wired here now that its identity function actually works
    # on Windows; before that it was a guard about drift that was itself drifting.
    "ai-brain-starter/hooks/sessionstart-hook-snapshot-guard.py",
    # Journal Step-0 context guard (2026-07-07) + its SessionStart self-heal. OWNED so
    # the installer dedups the guard (skill-path vs a ~/.claude/hooks/ copy) PER MATCHER,
    # verifies both scripts on disk, and can retire/relocate them. Registered under two
    # matchers each; the matcher-aware merge above keeps both copies.
    "ai-brain-starter/hooks/warn-journal-saved-without-context.py",
    "ai-brain-starter/scripts/heal-journal-guard.py",
    # Anti-fabrication guard family (MYC-1017). These target a MODEL-GENERAL bug
    # class — an agent asserting a verification result or a hook attribution it
    # never sourced from evidence — so they belong in the substrate, ACTIVATED
    # here. Shipping the files without registering them is ARTIFACT-WITHOUT-
    # ACTIVATION: guards present on disk, dormant in behavior.
    "ai-brain-starter/hooks/check-fabricated-verification.py",
    "ai-brain-starter/hooks/check-fabricated-hook-attribution.py",
    "ai-brain-starter/hooks/warn-chained-state-command-truncated.py",
    # Instinct-engine observer. Shipped under TWO PreToolUse matchers with a
    # byte-identical command; OWNED so its dedup is by basename (matcher-scoped),
    # not the literal command text. Without this, an interpreter-path drift (bare
    # `python3` -> absolute shim-safe path) makes is_same_command's literal fallback
    # miss and the matcher-aware merge APPENDS a second copy per matcher.
    "ai-brain-starter/hooks/observe-tool-calls.py",
]

# Path-divergence-robust matching: an ai-brain-starter hook may be wired at the
# skill path (~/.claude/skills/ai-brain-starter/hooks/) OR copied into the user
# hooks dir (~/.claude/hooks/). Dedup must recognize both as the same hook by
# SCRIPT BASENAME, else a re-run duplicates every hook a hand-maintained config
# wired at the user-hooks path. Only OUR script basenames are matched this way.
ABS_OWNED_BASENAMES = {
    "detect-closing-signal.py", "verify-session-close-cascade.py",
    "lint-vault-frontmatter.py", "log-skill-usage.py",
    "first-week-checkin.py", "migrate-to-user-level.py",
    "inject-love-language-context.py", "inject-meeting-workflow-on-trigger.py",
    "session-end-hook.sh", "email-gate-hook.py", "graph-context-hook.sh",
    "post-update-email-ask.py", "ai-brain-auto-update.sh", "ai-brain-auto-update.py",
    "snapshot-pending-work-on-stop.py", "surface-orphan-worktree-snapshots.py",
    "remove-ended-worktree.py", "enforce-worktree-cap.py",
    "worktree-footprint-signal.py", "remediate-runaway-procs.py",
    "sessionstart-hook-snapshot-guard.py",
    # Worktree HEAD-isolation gate (MYC-782). Basename listed so a copy wired at
    # ~/.claude/hooks/ — the hand-wired form on pre-registration machines —
    # dedups against the skill-path copy instead of double-firing.
    "check-cd-outside-worktree.py",
    # In-flight git-operation gate (2026-07-28). Same reason as its sibling
    # above: a hand-wired ~/.claude/hooks/ copy must dedup against the
    # skill-path copy, or the block fires twice on every git command.
    "block-git-mutation-mid-operation.py",
    # MCP secret-leak guards (MYC-3560): same basename-dedup reasoning as the
    # two gates above.
    "block-claude-mcp-inline-secret.py", "block-mcp-config-inline-secret.py",
    "block-secret-in-note.py", "block-skip-prefix-in-vault-write.py",
    "context-budget-measure.py",
    "validate-handoff-frontmatter.py",
    "block-populated-public-skill.py",
    "warn-workflow-call-permission-elevation.py",
    "warn-vault-session-in-worktree.py", "warn-learning-to-tool-private-memory.py",
    "warn-stale-dev-checkout.py", "dev-hub-refresh-on-session-start.py",
    # Session-start context loaders (MYC-2359 UPS -> SessionStart relocation):
    "session-start-context.py", "inject-instinct-context.py",
    # Client-side deployed==committed drift detector (MYC-2507):
    "surface-deployed-hooks-behind.py",
    # Journal Step-0 context guard + its SessionStart self-heal (2026-07-07):
    "warn-journal-saved-without-context.py", "heal-journal-guard.py",
    # Anti-fabrication guard family (MYC-1017). Basenames listed so a copy wired
    # at ~/.claude/hooks/ dedups against the skill-path copy instead of doubling.
    "check-fabricated-verification.py", "check-fabricated-hook-attribution.py",
    "warn-chained-state-command-truncated.py",
    # Instinct-engine observer, shipped under two matchers (dedup by basename so an
    # interpreter-path change can't double it):
    "observe-tool-calls.py",
    # Settings-integrity guards (see HOME_HOOKS_INSTALLER_DEPLOYS). Owned so
    # verify_paths_on_disk() can SEE them: an unowned command is skipped by
    # verification entirely, which is why these read as a clean install for
    # months while never once firing.
    #
    # check-claude-code-version.sh is deliberately NOT owned even though this
    # installer deploys it: it is bash-only, so platformize_template_for_windows
    # skips wiring it on Windows. An owned-but-never-wired hook is permanent
    # false drift for hooks/surface-deployed-hooks-behind.py, which diffs owned
    # committed basenames against owned deployed ones — every Windows user would
    # get a "1 background helper is not active" nag that no action can clear.
    "pre-write-settings-lint.py", "lint-claude-settings.py",
    # Phase-05 hooks, moved to the installer route 2026-08-13 (see
    # HOME_HOOKS_INSTALLER_DEPLOYS). Owned for the same reason as the two
    # above: unowned means verify_paths_on_disk() never looks at them, and a
    # never-deployed hook then reports as a clean install forever.
    "retry-budget.py", "validate-mcp-json.py", "vault-context.py",
}

# Hooks that hooks.json invokes from ~/.claude/hooks/ and that THIS INSTALLER is
# responsible for putting there.
#
# hooks.json guards each of these with `[ -f <path> ] &&`, so a missing file is
# not an error — it is a silent no-op. That is the right runtime behaviour and
# the wrong install behaviour: nothing in the repo ever copied these three, no
# phase doc mentions them, and they were absent from ABS_OWNED_BASENAMES, so
# verification skipped them too. Net effect: wired in 11 places in a real
# settings.json, present on disk 0 times, reported OK. Shipped-but-never-once-
# executed, on every install, since they were merged (#313's follow-on).
#
# retry-budget.py, validate-mcp-json.py and vault-context.py joined this set
# 2026-08-13, from a field report on a Windows install where all three were
# absent from ~/.claude/hooks/. They used to take the PHASE-DOC route: three
# literal `cp` lines in phases/phase-05-context-layer.md, executed by the MODEL
# during /setup-brain. Three things were wrong with that:
#
#   1. POSIX-ONLY. `cp` and `mkdir -p` are not commands on native Windows, so
#      the step could not run there at all — and its failure is invisible,
#      because the `[ -f ]` guard turns every missing hook into silence.
#   2. MODEL-EXECUTED. A copy step that depends on an agent reading a markdown
#      table and choosing to run it is not a deploy route; it is a suggestion.
#      Nothing verified it afterwards on any platform.
#   3. THE DEPENDENCY WAS NEVER SHIPPED. vault-context.py does
#      `from _lib.vault_root import vault_root_for` and falls back to a stub
#      returning None when that import fails. Nothing ever copied hooks/_lib/
#      to ~/.claude/hooks/, so even a SUCCESSFUL `cp` of the .py landed a hook
#      that resolved no vault and injected nothing, on every platform, forever
#      — fail-open, exit 0, no output. See HOME_HOOKS_LIB_DEPS.
#
# The phase doc no longer copies them (its `cp` lines are gone). Do not add one
# back: scripts/check-home-hook-deploy.py fails on BOTH routes as loudly as on
# neither, because a phase doc offering a choice the installer already made is
# a documented lie.
HOME_HOOKS_INSTALLER_DEPLOYS = {
    "pre-write-settings-lint.py",   # PreToolUse(Write|Edit) settings-integrity blocker
    "lint-claude-settings.py",      # SessionStart settings drift lint (+ --test self-check)
    "check-claude-code-version.sh",  # SessionStart version check (POSIX only; bash)
    "retry-budget.py",              # PreToolUse(Bash) 4th-identical-command blocker
    "validate-mcp-json.py",         # PreToolUse(Write|Edit) .mcp.json parse gate
    "vault-context.py",             # UserPromptSubmit vault-context injector
}

# Package files under hooks/_lib/ that a HOME_HOOKS_INSTALLER_DEPLOYS hook
# imports, and that therefore have to land in ~/.claude/hooks/_lib/ in the SAME
# install. "Ship the dep with the consumer, same commit."
#
# A deployed hook does `sys.path.insert(0, <its own dir>)` and then
# `from _lib.X import ...`. Deploy the hook alone and that import raises
# ImportError inside a try/except whose fallback is a silent no-op — the hook
# runs, exits 0, and does nothing, which is indistinguishable from a hook with
# nothing to say. scripts/check-home-hook-deploy.py statically asserts that
# every `_lib` module imported by a deployed hook appears here.
HOME_HOOKS_LIB_DEPS = {
    "__init__.py",     # makes _lib a package; without it the import fails
    "vault_root.py",   # vault-context.py -> vault_root_for()
}

# Hooks ai-brain-starter USED TO ship and has deliberately RETIRED. The
# installer actively REMOVES any of these still wired in a user's
# settings.json — merge_hooks() only adds/replaces template hooks, it never
# deletes one that's gone from the template, so without this step a retired
# hook stays wired (and keeps firing) forever on every existing install.
# This is what un-nags users who installed before a hook was removed.
# When retiring a hook: add its fingerprint AND basename here, keep it in the
# ABS_* lists above (so uninstall still recognizes it), and never reuse a
# retired basename for a new hook.
ABS_RETIRED_FINGERPRINTS = [
    # Retired 2026-06-03: fired on EVERY prompt of EVERY session and nagged
    # for an email forever until a marker existed — a stealth reversal of
    # docs/adr/0002-no-email-gate.md. Replaced by post-update-email-ask.py
    # (asks at most once, only after a git pull, when no email is on file).
    "ai-brain-starter/scripts/email-gate-hook.py",
    # Retired 2026-07-01 (MYC-720): the inline auto-update blob pulled but
    # DELEGATED the install step to the model, so a merged PR silently did not
    # deploy (the 40->131-behind recurrence). Replaced by scripts/ai-brain-auto-
    # update.sh, which deploys itself. EVERY prior inline variant contains
    # ".ai-brain-starter-last-update" and the new script-call command does NOT, so
    # retiring this substring removes the old blob and leaves the new hook — the
    # merge-then-retire order (main) then yields exactly one auto-update entry.
    ".ai-brain-starter-last-update",
    # Retired 2026-07-02: the BASH auto-update could not run on native Windows
    # (no bash / timeout / nice / find -mtime), leaving Windows installs
    # permanently stale. Replaced by the cross-platform ai-brain-auto-update.py;
    # the .sh file on disk survives as a thin delegator so a not-yet-migrated
    # settings.json entry keeps working until this retirement removes it.
    "ai-brain-starter/scripts/ai-brain-auto-update.sh",
]
ABS_RETIRED_BASENAMES = {
    "email-gate-hook.py",
    "ai-brain-auto-update.sh",
}

_SCRIPT_RE = re.compile(r"([\w.-]+\.(?:py|sh))")


def _owned_basenames(cmd: str) -> set[str]:
    """Owned ai-brain-starter script basenames referenced in a command."""
    return {os.path.basename(m) for m in _SCRIPT_RE.findall(cmd)} & ABS_OWNED_BASENAMES


# Extracts .py script paths from a hook command. Paths are unquoted or quoted;
# unquoted paths never contain spaces in our template. Used both to identify a
# command's target script and to rewrite that command for Windows.
_WIN_PY_PATH_RE = re.compile(r"(~?[^\s'\"|&;]+\.py)\b")

# A token holding any of these is shell syntax, not a script argument. Reaching
# one ENDS the argument list: everything past it belongs to the POSIX masking
# clause (`|| true`, `2>/dev/null || echo '{...}'`, `; else ...; fi`), not to the
# hook. Forwarding `2>/dev/null` or `true` to a hook is worse than dropping args.
_SHELL_SYNTAX_CHARS = set("|&;<>()`$\n")


def _hook_script_args(cmd: str) -> list[str]:
    """The arguments a hook command passes to the LAST .py script it names.

    Both command forms this installer handles put the hook's own script last —
    POSIX `[ -f X ] && python3 X --test || true`, and the Windows runner form
    `py -3 "hook_runner.py" --fallback silent "X" --test` — so one scan reads
    both and a hook's identity stays the same across platforms.

    Everything after that path is tokenized left to right and kept until the
    first shell-syntax token (see _SHELL_SYNTAX_CHARS). Backslashes are literal,
    not escapes: on Windows these tails sit next to real `C:\\...` paths.

    Returns [] when there is no .py path, no arguments, or the tail does not
    tokenize — the conservative answer in every case."""
    matches = list(_WIN_PY_PATH_RE.finditer(cmd))
    if not matches:
        return []
    m = matches[-1]
    tail = cmd[m.end():]
    # The path regex excludes quote characters, so a QUOTED path leaves its
    # closing quote at the head of the tail. Drop it, or that quote opens a new
    # string and the whole masking clause tokenizes as one bogus argument.
    quote = cmd[m.start() - 1] if m.start() else ""
    if quote in ("'", '"') and tail[:1] == quote:
        tail = tail[1:]
    lex = shlex.shlex(tail, posix=True)
    lex.whitespace_split = True
    lex.commenters = ""  # '#' is a legal character inside an argument
    lex.escape = ""      # a backslash is a path separator here, not an escape
    args: list[str] = []
    try:
        for token in lex:
            if not token or _SHELL_SYNTAX_CHARS & set(token):
                break
            args.append(token)
    except ValueError:  # unbalanced quote — claim no arguments rather than guess
        return []
    return args


def _owned_hook_key(cmd: str) -> tuple[frozenset, tuple] | None:
    """Dedup identity of an OWNED hook: which script, run with which arguments.

    None for a command running no owned script (a user hook — never deduped).

    Arguments belong in the key because one script under two argument lists is
    two registrations. hooks.json wires lint-claude-settings.py twice on
    SessionStart — plain, and `--test` for its self-test — and keying on the
    basename alone collapses the pair, silently retiring the self-test."""
    owned = frozenset(_owned_basenames(cmd))
    if not owned:
        return None
    return owned, tuple(_hook_script_args(cmd))


def find_repo_root() -> Path:
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent,
        Path.home() / ".claude" / "skills" / "ai-brain-starter",
        Path.home() / "Desktop" / "ai-brain-starter",
    ]
    for c in candidates:
        if (c / "hooks.json").is_file():
            return c
    raise FileNotFoundError("Could not find hooks.json source")


def load_hooks_template(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def is_abs_owned(command: str) -> bool:
    if any(fp in command for fp in ABS_FINGERPRINTS):
        return True
    if any(fp in command for fp in ABS_RETIRED_FINGERPRINTS):
        return True
    return bool(_owned_basenames(command))


def is_same_command(a: str, b: str) -> bool:
    """Two commands count as the same hook if they share an ABS fingerprint OR
    an owned script basename (so a skill-path entry and a ~/.claude/hooks/ entry
    for the same script dedup to one), else if the literal text matches.

    The same script with DIFFERENT arguments is NOT the same hook. merge_hooks()
    REPLACES on a match, so reading the pair as duplicates means whichever one
    the merge happened to keep is the only one that ever runs — for the
    lint-claude-settings.py pair that silently retired one of the two."""
    if _hook_script_args(a) != _hook_script_args(b):
        return False
    for fp in ABS_FINGERPRINTS:
        if fp in a and fp in b:
            return True
    if _owned_basenames(a) & _owned_basenames(b):
        return True
    return a.strip() == b.strip()


def _hook_depends_on_vault(command: str) -> bool:
    """True if a hook command can only run once the user's vault exists: it
    references a [VAULT_PATH]/... path AND carries no ~/.claude home fallback.

    The three vault-content hooks (graph-context-hook.sh, session-end-hook.sh,
    write-hook.sh) live inside the vault at '[VAULT_PATH]/⚙️ Meta/scripts/' as a
    single clause with no fallback. detect-closing-signal, by contrast, chains
    '... || python3 ~/.claude/skills/...', so it runs fine with no vault and its
    [VAULT_PATH]/.claude/skills/... clause resolves correctly under $HOME."""
    return "[VAULT_PATH]" in command and "~/.claude" not in command


def normalize_path_substitutions(template: dict, vault_path: str | None) -> dict:
    """Resolve [VAULT_PATH] in template hook commands.

    WITH a vault path: substitute the real, resolved vault path everywhere.

    WITHOUT one (bootstrap time, before /setup-brain creates the vault): OMIT
    every hook that depends on the vault (see _hook_depends_on_vault), then
    substitute $HOME for any surviving [VAULT_PATH] (the fallback hooks, whose
    [VAULT_PATH]/.claude/skills/... clause resolves correctly under $HOME).

    Why omit rather than substitute $HOME for the vault-content hooks: pointing
    them at $HOME produces dead '$HOME/⚙️ Meta/scripts/...' commands that error on
    every prompt / write / session-end and force a "how do you want to remove
    these?" decision on a non-technical user mid-install (MYC-739, surfaced by
    the 2026-06-09 install workshop). /setup-brain (phase-05) wires these three
    with the REAL vault path once it exists. Deferring them here is exactly what
    phase-00-install.md documents ("Bootstrap does NOT touch Hooks")."""
    if not vault_path:
        pruned = json.loads(json.dumps(template, ensure_ascii=False))  # deep copy
        for event in list((pruned.get("hooks") or {}).keys()):
            surviving_groups = []
            for group in pruned["hooks"][event]:
                group["hooks"] = [
                    h for h in group.get("hooks", [])
                    if not _hook_depends_on_vault(h.get("command", ""))
                ]
                if group["hooks"]:
                    surviving_groups.append(group)
            if surviving_groups:
                pruned["hooks"][event] = surviving_groups
            else:
                # Every hook in this event depended on the vault (e.g. the
                # PostToolUse(Write) group is only the vault write-hook). Drop
                # the now-empty event rather than leave a bare "Event": [].
                del pruned["hooks"][event]
        s = json.dumps(pruned, ensure_ascii=False)
        # JSON-escape the substituted path: on Windows it contains backslashes
        # (C:\Users\...) which are invalid JSON escapes when the string is
        # re-parsed by json.loads below, raising JSONDecodeError mid-install.
        s = s.replace("[VAULT_PATH]", json.dumps(str(Path.home()), ensure_ascii=False)[1:-1])
        return json.loads(s)
    s = json.dumps(template, ensure_ascii=False)
    s = s.replace("[VAULT_PATH]", json.dumps(str(Path(vault_path).resolve()), ensure_ascii=False)[1:-1])
    return json.loads(s)


def _is_windows() -> bool:
    """ABS_FORCE_WINDOWS=1 lets POSIX CI exercise the Windows path hermetically."""
    return os.environ.get("ABS_FORCE_WINDOWS") == "1" or os.name == "nt"


def _win_short_path(path: str) -> str | None:
    """8.3 short form of an EXISTING Windows path, or None if unavailable.

    GetShortPathNameW only resolves components that exist on disk, so the caller
    is responsible for passing a real path (see _ascii_safe_win_path, which
    shortens the longest existing ancestor). Returns None on any failure —
    non-Windows, 8.3 name creation disabled on the volume (`fsutil 8dot3name
    query`, the default on many modern SSD installs), or a path that has no
    short name — so the caller can degrade honestly instead of emitting a
    silently-wrong command.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        get_short = ctypes.windll.kernel32.GetShortPathNameW
        get_short.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_short.restype = wintypes.DWORD
        needed = get_short(path, None, 0)
        if needed == 0:
            return None
        buf = ctypes.create_unicode_buffer(needed)
        if get_short(path, buf, needed) == 0:
            return None
        return buf.value or None
    except Exception:  # noqa: BLE001 — ctypes/kernel32 unavailable: degrade
        return None


def _ascii_safe_win_path(path: str) -> str:
    """Rewrite a Windows path so the command line carrying it is pure ASCII.

    WHY: every hook command this installer writes embeds absolute paths, and on
    a Windows account like "JuanArturoGómez" (or any name with ñ, á, ü) those
    paths carry non-ASCII. settings.json stores them correctly as UTF-8, but
    Claude Code hands the command to a shell whose code page is cp1252/cp437,
    the accented byte is mangled in transit, the path no longer resolves, and
    EVERY hook fails — 53 of them on the reporting install, which blocks the
    session outright. The account name is not something a user can change.

    The fix is the 8.3 short name (C:\\Users\\JUANAR~1), which is ASCII by
    construction and which every Windows shell resolves to the same directory.
    This is exactly the workaround the affected users apply by hand.

    An ASCII path is returned UNCHANGED — the overwhelmingly common case stays
    byte-identical, so this cannot churn existing installs. A path that cannot
    be made ASCII (8.3 disabled, or the non-ASCII sits in a component that does
    not exist yet) is returned unchanged too; the caller detects that and warns.
    """
    if path.isascii():
        return path
    short = _win_short_path(path)
    if short and short.isascii():
        return short
    # The tail may not exist yet (a hook we are about to deploy). Shorten the
    # longest EXISTING ancestor — the non-ASCII is in the profile directory in
    # every reported case — and re-attach the (ASCII) remainder.
    p = Path(path)
    tail: list[str] = []
    for parent in [p, *p.parents]:
        if parent == parent.parent:  # reached the drive root
            break
        if parent.exists():
            short_parent = _win_short_path(str(parent))
            if short_parent and short_parent.isascii():
                return str(Path(short_parent, *reversed(tail)))
            break
        tail.append(parent.name)
    return path


def _windows_launcher() -> list[str] | None:
    """Resolve a Python launcher that parses as a bare command in EVERY shell
    Claude Code may use for hooks on Windows (PowerShell 5.1 / 7, cmd.exe, Git
    Bash). Only an UNQUOTED first token parses in all of them, so the launcher
    must be a bare PATH name — each candidate is validated by actually running
    it, which also filters out the Microsoft Store's fake `python` alias stub.
    Overridable for tests via ABS_WIN_LAUNCHER (space-separated tokens)."""
    import shutil
    import subprocess

    env_override = os.environ.get("ABS_WIN_LAUNCHER")
    if env_override:
        return env_override.split()
    for candidate, argv in (("py", ["py", "-3"]),
                            ("python", ["python"]),
                            ("python3", ["python3"])):
        if not shutil.which(candidate):
            continue
        try:
            probe = subprocess.run([*argv, "-c", "import sys"],
                                   capture_output=True, timeout=15)
            if probe.returncode == 0:
                return argv
        except Exception:  # noqa: BLE001 — a broken candidate is just skipped
            continue
    # ASCII-safed for the same reason the hook paths are: this token is written
    # UNQUOTED, so an accented profile directory in the interpreter path breaks
    # every hook command the moment the shell's code page mangles it.
    exe = _ascii_safe_win_path(sys.executable or "")
    if exe and " " not in exe:
        return [exe]  # unquoted absolute path parses everywhere iff space-free
    return None


def _posix_python() -> str:
    """Resolve an ABSOLUTE, real python3 that BYPASSES refuse-shims.

    The trailofbits `modern-python` plugin prepends a PATH shim for
    `python3`/`python` (SessionStart, via CLAUDE_ENV_FILE) that prints
    "ERROR: use uv run python3" and exit-1s on every bare invocation. Baking the
    resolved absolute path into hook commands as [PYTHON] makes them skip PATH
    resolution entirely, so neither that shim nor any pyenv/asdf/conda wrapper
    can turn a hook into a silent no-op. Degrades to bare `python3`.

    Only space-free paths qualify: the template invokes the interpreter
    unquoted, so a path with a space would break the command. Overridable for
    tests via ABS_POSIX_PYTHON.
    """
    override = os.environ.get("ABS_POSIX_PYTHON")
    if override:
        return override
    import subprocess

    for name in ("python3", "python"):
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if not d:
                continue
            cand = os.path.join(d, name)
            if " " in cand:
                continue  # unusable unquoted in the template
            if not (os.path.isfile(cand) and os.access(cand, os.X_OK)):
                continue
            # Cheap pre-filter: known refuse-shim locations.
            rp = os.path.realpath(cand)
            if "/hooks/shims/" in rp or "modern-python" in rp:
                continue
            # Robust: a real interpreter runs `-c` with rc 0; a refuse-shim
            # exit-1s. Bounded so a hung candidate can't stall the install.
            try:
                if subprocess.run([cand, "-c", "import sys"],
                                  capture_output=True, timeout=15).returncode == 0:
                    return cand
            except Exception:  # noqa: BLE001 — a broken candidate is just skipped
                continue
    # This installer is itself running under a real python (a refuse-shim would
    # have blocked this very process), so sys.executable is a safe absolute
    # fallback when PATH resolution came up empty.
    exe = sys.executable or ""
    if exe and " " not in exe and os.path.isfile(exe):
        return exe
    return "python3"


def substitute_python_interpreter(template: dict) -> dict:
    """Replace the [PYTHON] token in hook commands with a shim-safe interpreter.

    POSIX: an absolute real python3 (see _posix_python) so the modern-python
    PATH shim can't turn every hook into a silent no-op. Windows: a bare
    `python3` — platformize_template_for_windows() rewrites every .py command
    through the hook_runner launcher regardless, so the token value there is
    overwritten and never reaches settings.json.
    """
    py = "python3" if _is_windows() else _posix_python()
    s = json.dumps(template, ensure_ascii=False)
    s = s.replace("[PYTHON]", py)
    return json.loads(s)


def _win_quote_arg(arg: str) -> str:
    """Quote a forwarded hook argument only when it needs it.

    A bare token is the one shape PowerShell 5.1/7, cmd.exe and Git Bash all
    parse identically, so flags like `--test` stay bare. Arguments come from our
    own template, so the only case needing quotes is an embedded space; an
    argument whose CONTENT holds a quote character has no form all four shells
    parse alike, and none is attempted here."""
    return f'"{arg}"' if (not arg or any(c.isspace() for c in arg)) else arg


def _runner_path() -> str:
    """Absolute path to hook_runner.py to bake into every Windows hook command.

    Prefers the INSTALLED copy under ~/.claude/skills over the checkout this
    process happens to be running from.

    Why this is not `Path(__file__).parent / "hook_runner.py"` (MYC-3536): that
    path is written into settings.json and outlives this process by months. Run
    the installer — or any test that invokes it — from a throwaway git worktree
    under $TMP and every hook is wired to <worktree>/scripts/hook_runner.py.
    When the worktree is deleted the launcher can't open the runner and CPython
    exits 2 — which is Claude Code's intentional-BLOCK signal, not "hook
    unavailable". So every tool call in every later session is denied, with
    nothing tying the failure back to the worktree that caused it. Seen live
    2026-07-30: 95 of 111 entries pointed into four deleted temp worktrees.
    Same fail-closed class as #375 and #409.

    The hook TARGETS in these same commands already resolve to the ~/.claude
    install, so anchoring the runner there keeps one command internally
    consistent instead of straddling two checkouts.

    Falls back to the running checkout only when no installed copy exists (a
    first install from a dev tree, before the skill is deployed). ABS_HOOK_RUNNER
    overrides both, for hermetic tests.
    """
    override = os.environ.get("ABS_HOOK_RUNNER")
    if override:
        return str(Path(override).expanduser())
    local = Path(__file__).resolve().parent / "hook_runner.py"
    installed = (Path.home() / ".claude" / "skills" / "ai-brain-starter"
                 / "scripts" / "hook_runner.py")
    try:
        if installed.is_file():
            return str(installed.resolve())
    except OSError:
        pass
    return str(local)


def platformize_template_for_windows(template: dict) -> tuple[dict, list[str]]:
    """Rewrite the (already vault-substituted) template's POSIX shell commands
    into a form native Windows can execute.

    Why: hooks.json commands use `python3 X 2>/dev/null || echo JSON` and
    `[ -f X ] && ... || true`. Depending on Claude Code version + settings,
    Windows runs hook commands under PowerShell 5.1 (no `||`), PowerShell 7,
    cmd.exe (no `[ -f ]`, no /dev/null), or Git Bash — no one-liner survives
    all four, so before this rewrite every hook errored visibly on every
    prompt for Windows users. The one shape they all parse identically is a
    bare PATH command with quoted arguments:

        py -3 "<abs>/scripts/hook_runner.py" --fallback silent "<abs>/<hook>.py"

    hook_runner.py reproduces the masking semantics of the shell forms (see
    its docstring): missing script -> fallback JSON; exit 2 -> real block
    propagates; any other failure -> fallback JSON.

    Rules per command:
      - references a .sh script  -> OMIT (bash-only; reported to the caller)
      - references .py script(s) -> rewrite to the runner form. In fallback
        chains (`python3 vault-copy || python3 home-copy`) the LAST path wins:
        it is the ~/.claude home copy, the one guaranteed space-free and
        present on every install.
      - the target's own ARGUMENTS follow it, forwarded through the runner
        (whose argv contract is `[--fallback MODE] <target> [extra...]`). Only
        real arguments survive, never the POSIX masking noise that trails every
        template command — see _hook_script_args. Dropping them made
        `lint-claude-settings.py --test` platformize to a byte-identical copy of
        the plain entry, so dedup collapsed the pair and the linter's self-test
        never ran on Windows.
      - fallback flavor: `allow` when the original masked to a PreToolUse
        permissionDecision, else `silent`.

    Returns (rewritten_template, skipped_labels). If no launcher can be
    resolved, returns the template unchanged with a loud warning — failing
    toward the status quo rather than silently unwiring everything."""
    launcher = _windows_launcher()
    skipped: list[str] = []
    if launcher is None:
        print("WARNING: no Python launcher found on PATH (tried py, python, "
              "python3). Hook commands were left in POSIX form and will not "
              "run until Python is installed and this installer is re-run.",
              file=sys.stderr)
        return template, skipped

    runner = _ascii_safe_win_path(_runner_path())
    out = json.loads(json.dumps(template, ensure_ascii=False))  # deep copy
    home = str(Path.home())
    non_ascii: set[str] = set()
    if not runner.isascii():
        non_ascii.add(runner)

    for event in list((out.get("hooks") or {}).keys()):
        surviving_groups = []
        for group in out["hooks"][event]:
            new_hooks = []
            for h in group.get("hooks", []):
                cmd = h.get("command", "")
                if not cmd:
                    continue
                if ".sh" in cmd and not _WIN_PY_PATH_RE.search(cmd):
                    skipped.append(f"{event}: {cmd[:70]}")
                    continue
                paths = _WIN_PY_PATH_RE.findall(cmd)
                if not paths:
                    skipped.append(f"{event}: {cmd[:70]}")
                    continue
                target = paths[-1]
                if target.startswith("~"):
                    target = home + target[1:]
                target = _ascii_safe_win_path(str(Path(target)))
                if not target.isascii():
                    non_ascii.add(target)
                fb = "allow" if "permissionDecision" in cmd else "silent"
                h = dict(h)
                h["command"] = " ".join(
                    [*launcher, f'"{runner}"', "--fallback", fb, f'"{target}"',
                     *(_win_quote_arg(a) for a in _hook_script_args(cmd))])
                new_hooks.append(h)
            if new_hooks:
                g = dict(group)
                g["hooks"] = new_hooks
                surviving_groups.append(g)
        if surviving_groups:
            out["hooks"][event] = surviving_groups
        else:
            del out["hooks"][event]
    if non_ascii:
        # Reached only when 8.3 short names are unavailable. Never silent: the
        # symptom otherwise is "every hook errors on every prompt" with no
        # legible cause, and the user cannot rename their Windows account.
        print("WARNING: these hook paths contain non-ASCII characters and could "
              "NOT be shortened to an 8.3 name:", file=sys.stderr)
        for p in sorted(non_ascii):
            print(f"           {p}", file=sys.stderr)
        print("  Windows hands hook commands to a shell running a legacy code "
              "page (cp1252/cp437), which mangles those characters, so the "
              "paths will not resolve and the hooks will fail on every prompt.\n"
              "  Cause: 8.3 name creation is disabled on this volume. Check with:\n"
              "    fsutil 8dot3name query %SystemDrive%\n"
              "  Fix: enable it (`fsutil 8dot3name set 0`, then re-create the "
              "affected directory so it gets a short name), or move the vault / "
              "clone to an all-ASCII path and re-run this installer.",
              file=sys.stderr)
    return out, skipped


def merge_hooks(existing: dict, new_template: dict) -> tuple[dict, dict]:
    """Merge ai-brain-starter hooks into existing user settings.

    Returns (merged_settings, change_summary).

    Strategy per event:
      1. Find every group in the new template's hooks.<event> array
      2. For each new group, find ai-brain-starter command(s) inside its hooks list
      3. In existing, locate any group whose hooks list contains an ABS-owned command
      4. Replace the matching ABS commands inline; preserve non-ABS commands;
         add new ABS commands that weren't there before
    """
    summary = {"added": [], "updated": [], "kept": [], "events_touched": []}
    merged = json.loads(json.dumps(existing))  # deep copy
    if "hooks" not in merged:
        merged["hooks"] = {}

    for event, new_groups in (new_template.get("hooks") or {}).items():
        summary["events_touched"].append(event)
        if event not in merged["hooks"]:
            merged["hooks"][event] = []

        existing_groups = merged["hooks"][event]
        # Collect all ABS-owned commands from new template (flattened)
        for new_group in new_groups:
            # A hook is identified by (matcher, command): the SAME command under two
            # DIFFERENT matchers is two DISTINCT registrations, not a duplicate. The
            # journal-context guard and observe-tool-calls both ship one byte-identical
            # command under two matchers; a matcher-BLIND dedup collapsed the second copy
            # into the first, so a fresh install landed the guard under Bash only and
            # left Write|Edit|MultiEdit journal saves unguarded. Scope every replace /
            # dedup decision below to groups carrying THIS matcher.
            matcher = new_group.get("matcher")
            new_hooks = new_group.get("hooks", [])
            for new_hook in new_hooks:
                cmd = new_hook.get("command", "")
                if not cmd:
                    continue
                # Look for this command in an existing group WITH THE SAME MATCHER.
                replaced = False
                for eg in existing_groups:
                    if eg.get("matcher") != matcher:
                        continue
                    eg_hooks = eg.get("hooks", [])
                    for i, eh in enumerate(eg_hooks):
                        eh_cmd = eh.get("command", "")
                        if is_same_command(cmd, eh_cmd):
                            # Replace
                            eg_hooks[i] = new_hook
                            summary["updated"].append(f"{event}: {cmd[:80]}")
                            replaced = True
                            break
                    if replaced:
                        break

                if not replaced:
                    # Find or create a group with the same matcher (if any)
                    target_group = None
                    for eg in existing_groups:
                        if eg.get("matcher") == matcher:
                            target_group = eg
                            break
                    if not target_group:
                        target_group = {}
                        if matcher:
                            target_group["matcher"] = matcher
                        target_group["hooks"] = []
                        existing_groups.append(target_group)
                    target_group.setdefault("hooks", []).append(new_hook)
                    summary["added"].append(f"{event}: {cmd[:80]}")

        # Track non-touched non-ABS hooks as "kept"
        for eg in existing_groups:
            for eh in eg.get("hooks", []):
                cmd = eh.get("command", "")
                if cmd and not is_abs_owned(cmd):
                    summary["kept"].append(f"{event}: {cmd[:80]}")

    return merged, summary


def remove_abs_hooks(existing: dict) -> tuple[dict, int]:
    """Remove all ABS-owned hook entries. Returns (cleaned, count_removed)."""
    cleaned = json.loads(json.dumps(existing))
    removed = 0
    if "hooks" not in cleaned:
        return cleaned, 0
    for event, groups in list(cleaned["hooks"].items()):
        new_groups = []
        for g in groups:
            kept_hooks = []
            for h in g.get("hooks", []):
                if is_abs_owned(h.get("command", "")):
                    removed += 1
                    continue
                kept_hooks.append(h)
            if kept_hooks:
                new_g = dict(g)
                new_g["hooks"] = kept_hooks
                new_groups.append(new_g)
            elif "matcher" in g and not kept_hooks:
                # Matcher group emptied — drop
                pass
        if new_groups:
            cleaned["hooks"][event] = new_groups
        else:
            del cleaned["hooks"][event]
    return cleaned, removed


def _is_retired(command: str) -> bool:
    """True if a command runs a RETIRED ai-brain-starter hook."""
    if not command:
        return False
    if any(fp in command for fp in ABS_RETIRED_FINGERPRINTS):
        return True
    found = {os.path.basename(m) for m in _SCRIPT_RE.findall(command)}
    return bool(found & ABS_RETIRED_BASENAMES)


def retire_stale_hooks(existing: dict) -> tuple[dict, int]:
    """Remove every hook entry whose command runs a RETIRED hook.

    merge_hooks() only adds/replaces hooks present in the template — it never
    deletes one that's gone from the template. So a retired hook would stay
    wired in an existing user's settings.json and keep firing forever. This is
    the propagation step that actually un-wires a removed hook on the next
    install / auto-update. Returns (cleaned, count_removed).

    Groups emptied by retirement are dropped. Non-retired hooks (ours and the
    user's own) are preserved untouched."""
    cleaned = json.loads(json.dumps(existing))
    removed = 0
    if "hooks" not in cleaned:
        return cleaned, 0
    for event, groups in list(cleaned["hooks"].items()):
        new_groups = []
        for g in groups:
            hooks = g.get("hooks", [])
            kept = [h for h in hooks if not _is_retired(h.get("command", ""))]
            removed += len(hooks) - len(kept)
            if kept:
                ng = dict(g)
                ng["hooks"] = kept
                new_groups.append(ng)
            # else: group emptied by retirement -> drop it
        if new_groups:
            cleaned["hooks"][event] = new_groups
        else:
            del cleaned["hooks"][event]
    return cleaned, removed


def _template_owned_events(template: dict) -> dict:
    """basename -> set(events) for every ABS-owned command in the template — the
    authority on WHERE an owned hook belongs."""
    out: dict[str, set[str]] = {}
    for event, groups in (template.get("hooks") or {}).items():
        for g in (groups or []):
            for h in (g.get("hooks") or []):
                for bn in _owned_basenames(h.get("command", "")):
                    out.setdefault(bn, set()).add(event)
    return out


def relocate_moved_hooks(existing: dict, template: dict) -> tuple[dict, int]:
    """Remove an owned hook from an event when the template still ships that hook
    but on DIFFERENT event(s) — i.e. it MOVED. merge_hooks() adds the hook to its
    new event, but it scopes its search per-event, so it never sees (or removes)
    the stale copy on the OLD event. Without this step a hook that moved events
    (e.g. UserPromptSubmit -> SessionStart, MYC-2359) stays wired on BOTH and keeps
    firing on the old one — neutering a move on every EXISTING install.

    Distinct from retire_stale_hooks: that un-wires hooks GONE from the template
    (un-nag); this relocates hooks STILL in the template. A hook absent from the
    template (`tev` empty) is left untouched here. Groups emptied are dropped.
    Returns (cleaned, count_removed)."""
    owned_events = _template_owned_events(template)
    cleaned = json.loads(json.dumps(existing))
    removed = 0
    if "hooks" not in cleaned:
        return cleaned, 0
    for event, groups in list(cleaned["hooks"].items()):
        new_groups = []
        for g in groups:
            hooks = g.get("hooks", [])
            kept = []
            for h in hooks:
                bns = _owned_basenames(h.get("command", ""))
                tev: set[str] = set().union(*(owned_events.get(b, set()) for b in bns)) if bns else set()
                # Stale moved copy iff the hook is STILL shipped somewhere in the
                # template (tev non-empty) AND this event is not one of its
                # template events. Removed-from-template hooks (tev empty) are left
                # to retire_stale_hooks; the user's own hooks (bns empty) are never
                # touched.
                if tev and event not in tev:
                    removed += 1
                    continue
                kept.append(h)
            if kept:
                ng = dict(g)
                ng["hooks"] = kept
                new_groups.append(ng)
        if new_groups:
            cleaned["hooks"][event] = new_groups
        else:
            del cleaned["hooks"][event]
    return cleaned, removed


def dedupe_owned_hooks(existing: dict) -> tuple[dict, int]:
    """Collapse duplicate OWNED hooks that share the same (event, group, owned-script)
    down to one, keeping the LAST occurrence (the freshest merge result).

    merge_hooks() REPLACES a template hook in place only when is_same_command matches; for
    an owned hook that match is by basename, but if a machine's stored command text drifts
    in a way the owned-basename set still recognizes yet a SECOND stale copy already exists
    in the group (e.g. an interpreter-path change from bare `python3` to an absolute
    shim-safe path left two copies before this hook was owned), the replace touches only
    the first and a duplicate persists. This pass is the idempotent cleanup that self-heals
    such duplicates on the next install. Non-owned (user) hooks and DISTINCT owned hooks
    are never touched — and one script wired twice with different ARGUMENTS is two
    distinct hooks, not a duplicate (see _owned_hook_key).
    Groups emptied are dropped. Returns (cleaned, count_removed)."""
    cleaned = json.loads(json.dumps(existing))
    removed = 0
    if "hooks" not in cleaned:
        return cleaned, 0
    for event, groups in list(cleaned["hooks"].items()):
        new_groups = []
        for g in groups:
            hooks = g.get("hooks", [])
            # Last index per owned (script, args) key within THIS group. Only owned
            # hooks (non-None key) are eligible; a user hook (None) is always kept.
            last_idx: dict = {}
            for i, h in enumerate(hooks):
                key = _owned_hook_key(h.get("command", ""))
                if key:
                    last_idx[key] = i
            kept = []
            for i, h in enumerate(hooks):
                key = _owned_hook_key(h.get("command", ""))
                if key and last_idx.get(key) != i:
                    removed += 1
                    continue  # an earlier duplicate of an owned hook kept later in-group
                kept.append(h)
            if kept:
                ng = dict(g)
                ng["hooks"] = kept
                new_groups.append(ng)
        if new_groups:
            cleaned["hooks"][event] = new_groups
        else:
            del cleaned["hooks"][event]
    return cleaned, removed


def dedupe_identical_hooks(existing: dict) -> tuple[dict, int]:
    """Collapse BYTE-IDENTICAL hook commands sharing an (event, matcher). Keeps the
    first occurrence.

    WHY THIS EXISTS SEPARATELY FROM dedupe_owned_hooks(). That pass keys on
    `_owned_basenames()`, so it only ever collapses hooks the template still
    declares. A hook wired by an OLDER hooks.json but since dropped from both the
    template and ABS_OWNED_BASENAMES resolves to an empty key, is read as a user
    hook, and is therefore immortal -- every subsequent install appends another
    copy that nothing will ever reap. Measured on a real long-lived account
    (MYC-3876): 111 entries where a fresh install writes 56, with NINE hooks
    present SEVEN times each -- one POSIX form plus six byte-identical Windows
    forms -- while `Deduped:` reported 0 on every run.

    Identity, not ownership, is the safe warrant here. Two byte-identical command
    strings under the same matcher fire the same process on the same event; there
    is no configuration in which running it twice is what the user meant. So this
    pass needs no allowlist and cannot drift out of date with one.

    Deliberately narrow, because the entries it touches are UNOWNED and may be the
    user's own:
      * only EXACT string matches collapse. A POSIX `python3 ~/...` variant and a
        Windows `py -3 "C:\\..."` variant of the same script are left alone -- they
        differ, and settings.json may be shared with a machine where the other one
        is the live form.
      * scoped by matcher, so the same command under two different matchers (two
        genuinely different trigger conditions) is preserved.

    Returns (cleaned, count_removed). Locked by tests/test_install_idempotent.py."""
    cleaned = json.loads(json.dumps(existing))
    removed = 0
    if "hooks" not in cleaned:
        return cleaned, 0
    for event, groups in list(cleaned["hooks"].items()):
        # seen per matcher, so identical commands are collapsed both WITHIN a group
        # and ACROSS groups that share a matcher (a later install can append a new
        # group rather than growing the existing one).
        seen: dict[str, set] = {}
        new_groups = []
        for g in groups:
            matcher = g.get("matcher", "")
            bucket = seen.setdefault(matcher, set())
            kept = []
            for h in g.get("hooks", []):
                cmd = h.get("command", "")
                if cmd and cmd in bucket:
                    removed += 1
                    continue
                if cmd:
                    bucket.add(cmd)
                kept.append(h)
            if kept:
                ng = dict(g)
                ng["hooks"] = kept
                new_groups.append(ng)
        if new_groups:
            cleaned["hooks"][event] = new_groups
        else:
            del cleaned["hooks"][event]
    return cleaned, removed


def backup_settings(settings_path: Path) -> Path | None:
    if not settings_path.is_file():
        return None
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    backup = settings_path.with_name(f"{settings_path.name}.bak-{stamp}-abs")
    try:
        backup.write_text(settings_path.read_text(encoding="utf-8"), encoding="utf-8")
        return backup
    except OSError:
        return None


def write_settings_with_verify(settings_path: Path, settings: dict, backup: Path | None) -> bool:
    """Write settings.json, verify it parses, rollback on failure.

    ensure_ascii=True (JSON's default) is load-bearing, not style (#397). This is
    the ONE place settings become BYTES, and those bytes are read back by a
    consumer whose encoding we do not control. On Windows, Claude Code hands hook
    commands to a shell running a legacy code page: a settings.json carrying raw
    UTF-8 gets decoded there as cp1252/cp437, an account directory named "Ñoño"
    comes back as "Ã‘oÃ±o", the absolute hook path stops resolving, and EVERY hook
    fails — which blocks Bash and Grep for the whole session (47, then 132, then
    104 dead paths across one reporter's three reinstalls). Escaping non-ASCII to
    \\uXXXX makes the file pure ASCII, which decodes identically under UTF-8,
    cp1252, cp437 and latin-1, so no consumer's code page can corrupt a path.

    JSON semantics are untouched — "\\u00f3" and "ó" parse to the same string — so
    merge/dedup/uninstall, which all match on these command strings, see exactly
    what they saw before. Same escaping already relied on as cp1252 protection
    elsewhere in this repo (SEV-4-json-encoded, scripts/utf8-stdout-baseline.txt).

    This is the byte-level half of the fix whose path-level half is
    _ascii_safe_win_path() (#430). That one is necessary but not sufficient: 8.3
    short-name creation is disabled by default on many modern volumes, and there
    it degrades to returning the path unchanged; it also covers only the Windows
    hook-command rewrite, never the substituted vault path or the user's own
    pre-existing entries. Locked by scripts/test_settings_json_codepage_safe.py.
    """
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    new_text = json.dumps(settings, indent=2, ensure_ascii=True) + "\n"
    try:
        settings_path.write_text(new_text, encoding="utf-8")
    except OSError as e:
        print(f"ERROR: write failed: {e}", file=sys.stderr)
        return False
    # Verify
    try:
        json.loads(settings_path.read_text(encoding="utf-8"))
        return True
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: post-write JSON parse failed: {e}", file=sys.stderr)
        if backup and backup.is_file():
            try:
                settings_path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
                print(f"  Rolled back to {backup}", file=sys.stderr)
            except OSError as e2:
                print(f"  ERROR: rollback also failed: {e2}", file=sys.stderr)
        return False


def link_agent_memory_into_vault(vault_path: str, quiet: bool) -> None:
    """Symlink Claude Code's per-project memory dir into the vault so the
    user's "brain" actually accumulates in their vault, not in a hidden
    ~/.claude/ tool dir. Delegates to scripts/link-agent-memory.py (idempotent,
    loss-free). A failure here is the brain-durability bug recurring, so it is
    surfaced LOUDLY — but it never aborts the hook install.
    """
    import subprocess

    linker = Path(__file__).resolve().parent / "link-agent-memory.py"
    if not linker.is_file():
        print(f"WARNING: {linker} missing — Claude Code memory will NOT be linked "
              f"into the vault. Memory would strand in ~/.claude/.", file=sys.stderr)
        return
    cmd = [sys.executable, str(linker), "--vault", vault_path]
    if quiet:
        cmd.append("--quiet")
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60, **_TEXT_UTF8)
    except Exception as e:  # noqa: BLE001 — never let this brick the install
        print(f"WARNING: linking memory into the vault failed to run: {e}", file=sys.stderr)
        return
    if result.stdout and not quiet:
        print(result.stdout, end="")
    if result.returncode != 0:
        print("WARNING: could NOT link Claude Code memory into the vault — memory "
              "would strand in ~/.claude/ instead of your brain. Details below; "
              f"re-run manually:\n  python3 {linker} --vault '{vault_path}'", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)


def install_auto_gc(vault_path: str, quiet: bool) -> None:
    """Schedule the daily maintenance + auto-GC pass (MYC-2363).

    Every reclaim tool the substrate ships — worktree prune, graph-cache prune,
    the dev-repo reaper, the sibling-worktree pruner, hub freshness — was OPT-IN,
    so on a default install none of them ever ran and the machine only ever got
    fuller. An install is supposed to leave the machine BETTER over time, so
    scheduling is part of installing, not a documented extra step. Registered
    here rather than in bootstrap because THIS is the path the fresh-install
    smoke proves; a step only bootstrap runs is a step re-installs skip.

    Idempotent, gated (load + battery + user-idle) at RUN time, and never fatal:
    a machine with no scheduler still gets every hook. ABS_NO_AUTO_GC=1 opts out.
    """
    import subprocess

    if os.name == "nt":
        return  # no launchd/cron; the Windows scheduler path is not shipped yet

    # NEVER schedule for a vault that lives under the system temp dir. Every
    # integration test that exercises this installer builds its vault there, and
    # scheduling is a HOST-level side effect: a test run would write a real
    # launchd agent or crontab entry on the developer's machine (and on every CI
    # runner) pointing at a fixture that is deleted seconds later. A real brain
    # never lives in $TMPDIR, so this costs nothing and makes the test suite
    # incapable of mutating the host's scheduler.
    try:
        vault_resolved = Path(vault_path).expanduser().resolve()
        tmp_root = Path(tempfile.gettempdir()).resolve()
        if vault_resolved == tmp_root or tmp_root in vault_resolved.parents:
            if not quiet:
                print(f"NOTE: vault is under {tmp_root} — skipping auto-GC scheduling "
                      f"(fixture/test vault, not a real brain).")
            return
    except (OSError, ValueError):
        pass  # unresolvable path -> fall through; the scheduler validates it too

    installer = Path(__file__).resolve().parent / "install-vault-daily-maintenance.sh"
    if not installer.is_file():
        if not quiet:
            print(f"NOTE: {installer.name} missing — daily auto-GC not scheduled.")
        return
    cmd = ["/bin/bash", str(installer), vault_path]
    if quiet:
        cmd.append("--quiet")
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120, **_TEXT_UTF8)
    except Exception as e:  # noqa: BLE001 — scheduling must never brick the install
        print(f"WARNING: could not schedule daily auto-GC: {e}", file=sys.stderr)
        return
    if result.stdout and not quiet:
        print(result.stdout, end="")
    if result.returncode != 0:
        # Surfaced, not swallowed: silently unscheduled looks identical to
        # scheduled-and-working until the disk fills months later.
        print("WARNING: daily auto-GC was NOT scheduled — nothing will reclaim "
              "worktrees, caches, or merged branches on this machine. Re-run:\n"
              f"  bash {installer} '{vault_path}'", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)


def deploy_home_hooks(
    repo_hooks_dir: Path,
    config_dir: Path,
    *,
    dry_run: bool,
    quiet: bool,
) -> list[str]:
    """Copy the HOME_HOOKS_INSTALLER_DEPLOYS scripts into <config_dir>/hooks/.

    Closes the DEPLOYED-vs-COMMITTED gap for the hooks that hooks.json invokes
    by their ~/.claude/hooks/ path. Wiring a command that names a file nobody
    ever writes is a hook that cannot fire; the `[ -f ]` guard then makes the
    absence look like health.

    Also deploys HOME_HOOKS_LIB_DEPS into <config_dir>/hooks/_lib/. A hook that
    imports `_lib.<mod>` and finds it missing does not crash — it falls into a
    try/except stub and silently does nothing, which reads exactly like a hook
    with nothing to report. The dependency ships with its consumer.

    Contract:
      - COPY-IF-DIFFERENT: an identical destination is a no-op, so re-runs and
        the daily update pass are quiet and idempotent.
      - A destination that DIFFERS is backed up to <file>.bak-YYYY-MM-DD-HHMM
        before being overwritten, so a hand-patched copy is always recoverable
        (same contract as sync-vault-scripts).
      - Executable bit set on POSIX; the SessionStart version check is invoked
        via `bash <path>`, but a hand-run copy should work too.
      - NEVER fatal. A hook that cannot be deployed is reported and the install
        continues — the same fail-open posture as the rest of this installer.

    Returns human-readable status lines for the caller to print.
    """
    notes: list[str] = []
    dest_dir = config_dir / "hooks"

    targets: list[tuple[str, Path, Path]] = [
        (name, repo_hooks_dir / name, dest_dir / name)
        for name in sorted(HOME_HOOKS_INSTALLER_DEPLOYS)
    ]
    # _lib is a package, not a hook: same copy contract, no executable bit, and
    # its own subdirectory. Deployed BEFORE nothing in particular — order does
    # not matter, since the import only runs when a hook fires.
    targets += [
        (f"_lib/{name}", repo_hooks_dir / "_lib" / name, dest_dir / "_lib" / name)
        for name in sorted(HOME_HOOKS_LIB_DEPS)
    ]

    for name, src, dest in targets:
        if not src.is_file():
            notes.append(f"  ! {name}: not in {repo_hooks_dir} — hook will not fire")
            continue
        try:
            payload = src.read_bytes()
            if dest.is_file() and dest.read_bytes() == payload:
                continue
            if dry_run:
                notes.append(f"  + {name} (would {'update' if dest.is_file() else 'deploy'})")
                continue
            # dest.parent, not dest_dir: an entry under _lib/ needs its own
            # subdirectory created, and a FileNotFoundError here is swallowed by
            # the OSError handler below — the hook would deploy and its library
            # would not, which is the exact silent half-install being fixed.
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.is_file():
                # dest.name, not name: `name` may carry a subdirectory
                # ("_lib/vault_root.py") and Path.with_name() raises ValueError
                # on a separator — an exception this except-clause does not
                # catch, which would abort the whole install.
                backup = dest.with_name(
                    f"{dest.name}.bak-{datetime.now().strftime('%Y-%m-%d-%H%M')}")
                shutil.copyfile(dest, backup)
                notes.append(f"  ~ {name} (updated; previous copy at {backup.name})")
            else:
                notes.append(f"  + {name} (deployed)")
            shutil.copyfile(src, dest)
            if os.name != "nt" and "/" not in name:
                try:
                    dest.chmod(dest.stat().st_mode | 0o111)
                except OSError:
                    pass
        except OSError as e:
            # One unwritable hook must not abort the install.
            notes.append(f"  ! {name}: {e}")
    return notes


def _hookify_templates_dir(hooks_template_path: Path) -> Path:
    """The shipped hookify-rule templates live beside hooks.json at the repo root."""
    return hooks_template_path.resolve().parent / "templates" / "hookify-rules"


def activate_default_hookify_rules(
    templates_dir: Path,
    config_dir: Path,
    *,
    dry_run: bool,
    quiet: bool,
    fail_on_missing: bool,
) -> int:
    """Copy the activate-by-default hookify rule templates into config_dir (~/.claude).

    Reproducible activation for substrate rules that should fire on every install
    (warn-delegated-task-needs-source). Without this a rule template merged into
    the repo only fires on a machine where someone hand-copied it — the
    DEPLOYED-not-COMMITTED-not-WORKING gap for hookify rules.

    Contract:
      - templates/hookify-rules/activation.json's 'default' list names the rules
        that auto-activate; everything else is opt-in (manual cp).
      - COPY-IF-ABSENT: an existing destination is never overwritten, so a user's
        customized rule (the README tells them to customize) survives re-install
        and the daily update run.
      - Fail-loud under fail_on_missing ONLY when the manifest PROMISES a
        default template that is not on disk (governed-set drift: the manifest
        claims a rule ships, but it does not). A missing or unreadable manifest
        is treated as "feature unavailable" and skipped, never fatal, so a
        partial or older checkout still installs its hooks. Outside
        fail_on_missing a missing default template only warns; rule activation
        never blocks the core hooks install unless asked to fail closed on a
        broken promise.

    Returns 0 normally, 1 only under fail_on_missing when the manifest names a
    default template that is missing on disk.
    """
    manifest_path = templates_dir / "activation.json"
    if not manifest_path.is_file():
        # No manifest: this checkout predates the activation feature (or is a
        # minimal fixture). Activation is additive, so skip and never block the
        # hooks install -- even under --fail-on-missing, which is about missing
        # hook SCRIPTS, not this optional feature. Fail-loud is reserved for a
        # manifest that PROMISES a default template it does not ship (below).
        if not quiet:
            print(
                f"Hookify rules: no activation manifest at {manifest_path}; "
                "skipping activation.",
                file=sys.stderr,
            )
        return 0

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        if not quiet:
            print(
                f"Hookify rules: could not read activation manifest {manifest_path}: "
                f"{e}; skipping activation.",
                file=sys.stderr,
            )
        return 0

    default_rules = list(manifest.get("default", []))
    copied: list[str] = []
    already: list[str] = []
    missing: list[str] = []
    for name in default_rules:
        src = templates_dir / name
        if not src.is_file():
            missing.append(name)
            continue
        dest = config_dir / name
        if dest.exists():
            already.append(name)
            continue
        if not dry_run:
            try:
                config_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
            except OSError as e:
                # A filesystem error copying one rule must not abort the core
                # hooks install (activation is best-effort). Warn and move on.
                if not quiet:
                    print(f"  WARNING: could not activate {name}: {e}", file=sys.stderr)
                continue
        copied.append(name)

    if missing and fail_on_missing:
        print(
            "ERROR: hookify activation manifest names default template(s) missing "
            f"on disk: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    if not quiet:
        verb = "Would activate" if dry_run else "Activated"
        print(
            f"Hookify rules: {verb} {len(copied)} default rule(s), "
            f"{len(already)} already present in {config_dir}"
        )
        marker = "~" if dry_run else "+"
        for name in copied:
            print(f"  {marker} {name}")
    if missing and not quiet:
        for name in missing:
            print(f"  WARNING: default template missing, skipped: {name}", file=sys.stderr)

    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--hooks-source", help="path to hooks.json (default: auto-detect)")
    # vault-root-ok: the installer runs BEFORE the vault it targets is resolvable
    # from anything else -- it does not live in a vault (no script-location signal)
    # and has no target file (no per-file signal). --vault-path is the explicit
    # override, and the default is None, not ~/vault: unset simply skips the
    # [VAULT_PATH] substitution rather than pointing it at a wrong vault.
    ap.add_argument("--vault-path", default=os.environ.get("VAULT_ROOT"),
                    help="vault path for [VAULT_PATH] substitution (optional)")
    ap.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"),
                    help="target settings.json (default: ~/.claude/settings.json)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="INSTALL, then verify each referenced script exists on disk "
                         "and report. This WRITES settings.json — use --verify-only "
                         "to inspect an install without changing it.")
    ap.add_argument("--verify-only", action="store_true",
                    help="READ-ONLY: verify the hook scripts referenced by the CURRENT "
                         "settings.json exist on disk. Writes nothing, installs nothing.")
    ap.add_argument("--fail-on-missing", action="store_true",
                    help="exit nonzero if any required hook script is missing on disk "
                         "(implies --verify; used by bootstrap.sh to escalate divergent-fork strands)")
    args = ap.parse_args()

    settings_path = Path(args.settings).expanduser()

    # === --verify-only: inspect without touching anything ===
    # --verify has always meant "install, THEN verify" (docs/HOOKS_INSTALL.md,
    # "Verify after install"), which is a legitimate mode but leaves no way to
    # answer "is my install healthy?" without rewriting settings.json first.
    # Anyone reaching for a diagnostic gets a mutation, and any diagnostic that
    # mutates cannot be used to investigate a suspected bad write.
    if args.verify_only:
        current = {}
        if settings_path.is_file():
            try:
                current = json.loads(settings_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                print(f"ERROR: {settings_path} is not valid JSON: {e}", file=sys.stderr)
                return 2
        elif not args.quiet:
            print(f"NOTE: {settings_path} does not exist — nothing is wired yet.")
        return run_verification(current, fail_on_missing=args.fail_on_missing)

    # === make the vault the home of Claude Code's memory ===
    # Independent of the hooks install and idempotent, so do it first whenever a
    # vault path is known. This is the step that makes the "your brain lives in
    # your vault" promise true: without it, Claude Code's memory strands in
    # ~/.claude/projects/<key>/memory/, invisible in Obsidian.
    if args.vault_path and not args.dry_run and not args.uninstall:
        link_agent_memory_into_vault(args.vault_path, args.quiet)
        # === schedule the daily maintenance + auto-GC pass (MYC-2363) ===
        # Same shape and same reason as the memory link above: idempotent,
        # independent of the hook merge, and the difference between an install
        # that leaves the machine better over time and one that only fills it.
        install_auto_gc(args.vault_path, args.quiet)

    # === locate hooks template ===
    if args.hooks_source:
        hooks_template_path = Path(args.hooks_source)
    else:
        try:
            hooks_template_path = find_repo_root() / "hooks.json"
        except FileNotFoundError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

    if not hooks_template_path.is_file():
        print(f"ERROR: hooks template not found: {hooks_template_path}", file=sys.stderr)
        return 2

    template = load_hooks_template(hooks_template_path)

    # === deploy the hooks that hooks.json invokes from ~/.claude/hooks/ ===
    # BEFORE platformizing and merging: the commands about to be written name
    # these paths, so the files have to be there for the hook to fire and for
    # verification to see a truthful picture. Never fatal (see deploy_home_hooks).
    if not args.uninstall:
        deploy_notes = deploy_home_hooks(
            hooks_template_path.resolve().parent / "hooks",
            settings_path.parent,
            dry_run=args.dry_run,
            quiet=args.quiet,
        )
        if deploy_notes and not args.quiet:
            verb = "Would deploy" if args.dry_run else "Deployed"
            print(f"Home hooks: {verb} {len(deploy_notes)} change(s) in "
                  f"{settings_path.parent / 'hooks'}")
            for note in deploy_notes:
                print(note)

    template = normalize_path_substitutions(template, args.vault_path)
    # Bake a shim-safe interpreter into [PYTHON] so the modern-python PATH shim
    # (or any pyenv/conda wrapper) can't turn every hook into a silent no-op.
    template = substitute_python_interpreter(template)

    # On native Windows, POSIX one-liners (`||`, `[ -f ]`, 2>/dev/null) fail in
    # every shell Claude Code might use for hooks. Rewrite to the portable
    # hook_runner form BEFORE merging, so what lands in settings.json runs.
    win_skipped: list[str] = []
    if _is_windows():
        template, win_skipped = platformize_template_for_windows(template)

    # === load existing ===
    existing = {}
    if settings_path.is_file():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"ERROR: existing {settings_path} is not valid JSON: {e}", file=sys.stderr)
            print("Refusing to proceed. Fix the JSON or use --settings to point elsewhere.")
            return 2

    # === uninstall path ===
    if args.uninstall:
        cleaned, removed = remove_abs_hooks(existing)
        if removed == 0:
            if not args.quiet:
                print(f"No ai-brain-starter hooks found in {settings_path}. Nothing to remove.")
            return 0
        if args.dry_run:
            if not args.quiet:
                print(f"DRY RUN: would remove {removed} ai-brain-starter hook(s) from {settings_path}")
            return 0
        backup = backup_settings(settings_path)
        if write_settings_with_verify(settings_path, cleaned, backup):
            if not args.quiet:
                print(f"Removed {removed} ai-brain-starter hook(s) from {settings_path}")
                if backup:
                    print(f"Backup at {backup}")
            return 0
        return 2

    # === activate default hookify rules (reproducible activation) ===
    # Independent of the settings-merge below: copies the activate-by-default rule
    # templates into ~/.claude so a rule merged into the repo actually fires on a
    # fresh machine (not only where someone hand-copied it). Runs on every install
    # invocation — even when the hooks are already in sync — so the two concerns
    # (settings.json wiring vs rule-file presence) can never drift. Copy-if-absent,
    # so it never clobbers a user's customized rule.
    rc = activate_default_hookify_rules(
        _hookify_templates_dir(hooks_template_path),
        settings_path.parent,
        dry_run=args.dry_run,
        quiet=args.quiet,
        fail_on_missing=args.fail_on_missing,
    )
    if rc != 0:
        return rc

    # === install / update path ===
    merged, summary = merge_hooks(existing, template)
    # Retire hooks deleted from the template but still wired in this user's
    # settings.json (merge never deletes). This un-wires removed hooks.
    merged, retired_count = retire_stale_hooks(merged)
    # Relocate hooks that MOVED events in the template (merge added the new-event
    # copy but never removed the stale old-event one). Without this a moved hook
    # fires on BOTH events on every existing install (MYC-2359).
    merged, moved_count = relocate_moved_hooks(merged, template)
    # Collapse duplicate owned-hook copies that an interpreter-path drift left behind
    # (a byte-changed command the owned-basename dedup recognizes only AFTER the hook is
    # owned, so merge replaced the first copy but a second stale one persisted).
    merged, deduped_count = dedupe_owned_hooks(merged)
    # Collapse byte-identical copies regardless of ownership. dedupe_owned_hooks
    # above cannot see a hook the template no longer declares, which is exactly
    # the copy that accumulates forever (MYC-3876).
    merged, identical_count = dedupe_identical_hooks(merged)

    if not args.quiet:
        print(f"Merging into: {settings_path}")
        print(f"Source:       {hooks_template_path}")
        print(f"Events:       {', '.join(summary['events_touched'])}")
        print(f"Added:        {len(summary['added'])} hook(s)")
        print(f"Updated:      {len(summary['updated'])} hook(s)")
        print(f"Retired:      {retired_count} stale hook(s) removed")
        print(f"Relocated:    {moved_count} moved-event stale copy(ies) removed")
        print(f"Deduped:      {deduped_count} duplicate owned hook(s) removed")
        print(f"Collapsed:    {identical_count} byte-identical hook(s) removed")
        print(f"Preserved:    {len(set(summary['kept']))} non-ABS hook(s) untouched")
        if win_skipped:
            print(f"Skipped:      {len(win_skipped)} POSIX-only (bash) hook(s) not "
                  "wired on Windows:")
            for entry in win_skipped:
                print(f"  s {entry}")

    if args.dry_run:
        if not args.quiet:
            print("\nDRY RUN — no changes written.")
            for entry in summary["added"]:
                print(f"  + {entry}")
            for entry in summary["updated"]:
                print(f"  ~ {entry}")
            if retired_count:
                print(f"  - retire {retired_count} stale hook(s)")
            if moved_count:
                print(f"  - relocate {moved_count} moved-event stale copy(ies)")
            if deduped_count:
                print(f"  - dedupe {deduped_count} duplicate owned hook(s)")
        return 0

    if not summary["added"] and not summary["updated"] and not retired_count \
            and not moved_count and not deduped_count:
        if not args.quiet:
            print("\nAlready in sync. Nothing to write.")
        # Verify anyway. "Nothing to write" says the WIRING matches the template;
        # it says nothing about whether the scripts those commands name are on
        # disk. Returning early here meant bootstrap's --fail-on-missing check
        # was skipped on exactly the installs most likely to have drifted — the
        # ones already in sync — so a missing hook script passed silently.
        if args.verify or args.fail_on_missing:
            return run_verification(merged, fail_on_missing=args.fail_on_missing)
        return 0

    backup = backup_settings(settings_path)
    if write_settings_with_verify(settings_path, merged, backup):
        if not args.quiet:
            print(f"\nWrote {settings_path}")
            if backup:
                print(f"Backup: {backup}")
        if args.verify or args.fail_on_missing:
            rc = run_verification(merged, fail_on_missing=args.fail_on_missing)
            if rc != 0:
                return rc
        return 0
    return 2


def _is_gated_command(cmd: str, script_path: str) -> bool:
    """True if the command is wrapped in `[ -f <script> ] && ...` — meaning
    the script is intentionally optional and missing on disk is not a bug.
    The auto-update flow uses this for cross-vault portability."""
    import re
    # Match `[ -f <path> ]` where <path> is the same as the script being run.
    # Allow `~` prefix and arbitrary whitespace; match either bare or quoted.
    norm = script_path.replace("'", "").replace('"', "")
    pattern = re.compile(
        r"\[\s*-f\s+['\"]?" + re.escape(norm) + r"['\"]?\s*\]\s*&&"
    )
    return bool(pattern.search(cmd))


def verify_paths_on_disk(settings: dict) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """Inspect every ABS-owned hook command in settings; verify the referenced
    script exists on disk. Distinguishes:

      - REQUIRED: command runs the script directly (`python3 <path> ...`).
        Script missing = silent hook failure at runtime.
      - OPTIONAL: command wraps in `[ -f <path> ] && ...` (the guard suppresses
        the call), OR the missing path has a present same-basename sibling in
        the SAME command — a `||` fallback chain (`python3 <vault-copy> ||
        python3 <home-copy>`) is satisfied as long as one copy exists (MYC-2558).

    Returns (missing_required, missing_optional), each a list of
    (event, script_path, full_command_short).

    The full_command_short is the first 80 chars of the command for grep-friendly
    error output without dumping the entire 1500-char auto-update one-liner.
    """
    import re
    missing_required: list[tuple[str, str, str]] = []
    missing_optional: list[tuple[str, str, str]] = []
    # Two shapes to cover:
    #   POSIX: `python3 <path>` / `bash <path>`, path optionally quoted.
    #   Windows runner form: every path is a QUOTED argument. The quoted
    #   pattern also fixes a false "missing" on POSIX vault paths containing
    #   spaces, which the unquoted pattern used to truncate at the space.
    # Both patterns REQUIRE a .py/.sh extension (matching _SCRIPT_RE). Without
    # it, a fallback chain whose first path is quoted — `python3 '<vault>' ...`
    # — leaves `python3  2>/dev/null` after the quoted path is stripped for the
    # bare pass, and the bare pattern would capture `2>/dev/null` as a bogus
    # "missing required path" (MYC-2558). A shell redirection is not a script.
    quoted_re = re.compile(r"['\"]((?:~|/|[A-Za-z]:)[^'\"]+\.(?:py|sh))['\"]")
    bare_re = re.compile(r"(?:python3|bash)\s+(~?[^\s'\"|&;]+\.(?:py|sh))\b")

    for event, groups in (settings.get("hooks") or {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                cmd = h.get("command", "")
                if not cmd or not is_abs_owned(cmd):
                    continue
                # Find every script path the command references — a single
                # hook may chain `python3 a.py && bash b.sh`.
                candidates = quoted_re.findall(cmd)
                unquoted = re.sub(r"['\"][^'\"]*['\"]", " ", cmd)
                candidates += bare_re.findall(unquoted)
                paths = list(dict.fromkeys(candidates))
                exists = {r: Path(os.path.expanduser(r)).is_file() for r in paths}
                # A `||` fallback chain names the same script by more than one
                # path (vault copy || ~/.claude home copy || echo). On a vault
                # install only the home copy exists, which still satisfies the
                # chain at runtime. So a missing path whose basename ALSO has a
                # present same-basename sibling IN THIS COMMAND is optional, not
                # required. Scoped to one command — never across commands — so a
                # genuinely-missing single required script (no present sibling)
                # stays required (MYC-2558).
                satisfied_basenames = {
                    os.path.basename(r) for r in paths if exists[r]
                }
                for raw in paths:
                    if exists[raw]:
                        continue
                    p = Path(os.path.expanduser(raw))
                    short = cmd[:80] + ("…" if len(cmd) > 80 else "")
                    entry = (event, str(p), short)
                    if _is_gated_command(cmd, raw) or \
                            os.path.basename(raw) in satisfied_basenames:
                        missing_optional.append(entry)
                    else:
                        missing_required.append(entry)
    return missing_required, missing_optional


def run_verification(settings: dict, fail_on_missing: bool = False) -> int:
    """Print verification report. Returns 0 if all required paths exist, 1 otherwise.

    With fail_on_missing=True, the caller should propagate the nonzero exit
    (used by bootstrap.sh to escalate a divergent-fork strand to `err`).
    """
    print("\n--- Verification ---")
    missing_required, missing_optional = verify_paths_on_disk(settings)
    ok_count = 0
    for event, groups in (settings.get("hooks") or {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                cmd = h.get("command", "")
                if cmd and is_abs_owned(cmd):
                    ok_count += 1
    # Print OK count rather than every entry to keep output scannable
    ok_count -= len(missing_required) + len(missing_optional)
    print(f"  OK     {ok_count} hook(s) — referenced scripts exist on disk")
    if missing_optional:
        print(f"  SKIP   {len(missing_optional)} hook(s) — optional ([ -f ] guard, or a same-basename fallback sibling exists):")
        for event, p, _short in missing_optional:
            print(f"           {event}: {p}")
    if missing_required:
        print(f"  FAIL   {len(missing_required)} hook(s) — script not on disk:")
        for event, p, short in missing_required:
            print(f"           {event}: {p}")
            print(f"             command: {short}")
        print()
        print("  Likely cause: ai-brain-starter clone is on a DIVERGENT FORK and")
        print("  bootstrap skipped the pull, but the installer wrote new")
        print("  hook entries that reference files only present on origin/main.")
        print("  Recover:")
        print("    cd ~/.claude/skills/ai-brain-starter && git pull --rebase origin main")
        py = "py -3" if os.name == "nt" else "python3"
        print(f"    {py} ~/.claude/skills/ai-brain-starter/scripts/install-hooks-user-level.py")
        if fail_on_missing:
            return 1
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
