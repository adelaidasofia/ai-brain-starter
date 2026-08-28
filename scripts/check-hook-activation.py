#!/usr/bin/env python3
"""Fail when a shipped hook is wired nowhere, or is wired but not installer-owned.

THE CLASS (MYC-1031, generalizing MYC-1017). A hook file in `hooks/` is a FILE.
Whether it ever RUNS on a user's machine is a separate fact, and nothing
structurally connected the two. "We ship guard X" kept being true at the file
level and false at the behavior level:

  * MYC-1017  the fabrication-guard family, dormant.
  * MYC-1031  sessionstart-hook-snapshot-guard.py, dormant ~its entire life. It
              stayed on UNCLASSIFIED_BASELINE below until MYC-3880 found its
              identity function fused all 19 Windows hooks into one -- a dormant
              guard whose live logic was ALSO dead, so nothing could have noticed
              from behavior. Fixed and wired together; both halves had to land.
  * MYC-782   check-cd-outside-worktree.py, dormant ~6 weeks while CLAUDE.md
              described it as an active guard. Fixed in #371.

THE SPEC ERROR THIS CHECK EXISTS TO AVOID. MYC-1031 originally specified the
gate as "in the installer's owned set OR in hooks.json". That `or` is wrong, and
a gate built to it would have PASSED the third instance. `merge_hooks()` wires
exactly what hooks.json declares; ABS_FINGERPRINTS / ABS_OWNED_BASENAMES carry
only OWNERSHIP semantics (dedup / replace / retire / uninstall). A hook in the
owned set but absent from hooks.json is an OWNED DORMANT HOOK.

  hooks.json membership is the activation predicate. Owned-set membership is not.

TWO ASSERTIONS, separate so each failure names its own cause:

  A. ACTIVATION. Every top-level hooks/*.py is wired in an activation channel,
     or carries an explicit written reason. Silence is the only banned state.
  B. OWNERSHIP. Every hook we wire is also OWNED by the installer. Not
     activation -- but an unowned wired hook cannot be deduped, replaced or
     retired, so a re-install duplicates a hand-wired copy instead of replacing
     it, and verify_paths_on_disk() skips it (the hole #406 closed by hand).

TWO ACTIVATION CHANNELS, both real -- checking only one false-positives:
  * hooks.json        the installer template (install-hooks-user-level.py)
  * hooks/hooks.json  the plugin manifest (how install-ping.py activates)

WHAT THIS CHECK DELIBERATELY DOES NOT DO. It does not judge whether a wired
hook can still deliver a verdict. That is
scripts/check-hook-block-protocol.py's job (#405: a blocking hook under the
allow-fallback wrapper is rewritten into an ALLOW), and
scripts/check-hook-emission-channel.py's (MYC-3246: a warning written to a
stderr the wiring discards). Three checks, three disjoint questions -- is it
wired, is it owned, can it speak. Overlapping them would double-report one file
and let the duplicated rule drift into conflicting advice.

BASELINE RATCHET. Both assertions carry pre-existing debt, so they run as
ratchets: listed names are tolerated, the count may never GROW, and a name that
becomes wired/owned or disappears must be REMOVED (a stale entry fails). Debt
can only shrink.

ASCII-only output on purpose -- see scripts/check-utf8-stdout.py.
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Set

REPO_ROOT = Path(__file__).resolve().parent.parent

# Any .py/.sh path mentioned in a hook command.
SCRIPT_RE = re.compile(r"[\w.-]+\.(?:py|sh)")
# A hook this repo SHIPS (vs. an optional third-party hook the template invokes
# behind a `[ -f ~/.claude/hooks/X ]` guard, which we neither own nor install --
# scripts/check-home-hook-deploy.py owns that surface).
ABS_SHIPPED_RE = re.compile(r"ai-brain-starter/(?:hooks|scripts)/([\w.-]+\.py)")

# --- A: decided exemptions ---------------------------------------------------
# A hook here is NOT wired and that is CORRECT. Every entry states why, and the
# reason must be checkable by a reader. This is not the place for "probably
# fine" -- an entry whose reason you cannot verify belongs in the baseline
# below, honestly labelled, not here.
TEMPLATE_ONLY: Dict[str, str] = {
    "surface-stalled-git-operation.py":
        "not a hook: a report BUILDER (build_report) called by "
        "surface-stranded-session-artifacts.py, plus a standalone --test "
        "carrying its own negative control. Deliberately unwired for the same "
        "reason as surface-sync-guard-findings.py below -- its own SessionStart "
        "entry put that event at 20/19 on the footprint SLA gate, and buying "
        "budget to fit one more cold start hides the cost that gate exists to "
        "surface. Checkable: grep surface-stalled-git-operation "
        "hooks/surface-stranded-session-artifacts.py",
    "check_fab_shim.py":
        "not a hook: an import shim so tests can load the hyphenated guard "
        "module (hyphens are not importable identifiers).",
    "surface-sync-guard-findings.py":
        "not a hook: a report BUILDER (build_report) called by "
        "worktree-footprint-signal.py at its single emission point, plus a "
        "standalone --self-test diagnostic. Deliberately unwired -- wiring it "
        "as its own SessionStart entry put that event at 20/19 on the "
        "footprint SLA gate, and buying budget to fit one more cold start "
        "would hide the cost that gate exists to surface. Checkable: grep "
        "surface-sync-guard-findings hooks/worktree-footprint-signal.py "
        "(MYC-1133).",
    "surface-unniced-launchagents.py":
        "opt-in detector, deliberately unwired. It names LaunchAgents that run "
        "at normal scheduling priority -- the 2026-08-19 freeze class (68 "
        "agents, none declaring itself background, load 109 on 10 cores with "
        "RAM healthy). Same call as surface-sync-guard-findings above: "
        "SessionStart is at 19/19, so wiring it would breach the footprint SLA, "
        "and raising the budget would tax every install's cold start forever "
        "for a GROWTH failure that a default install (0 daemons wired) does not "
        "have. Run it by hand, or wire it locally, on a machine whose agent "
        "fleet is growing. Checkable: bash "
        "tests/integration/test_surface_unniced_launchagents.sh (MYC-4032).",
    "sunday-review-nudge.py":
        "budget: SessionStart is 19/19 and a once-weekly cosmetic nudge "
        "cannot justify evicting a guard from a saturated cold-start event. "
        "Substrate-clean otherwise (root via $SUNDAY_NUDGE_VAULT, no-ops "
        "when unset). Checkable: python3 scripts/footprint-sla-check.py "
        "--gate",
    "surface-dependabot-backlog.py":
        "two blockers. SessionStart 19/19; and OWNER is the unsubstituted "
        "placeholder 'github-username' that no installer rewrites, so a "
        "stranger's cold start would shell out to `gh repo list` against "
        "whoever owns that handle, then up to 100 further `gh pr list` "
        "calls. Checkable: git grep -n github-username",
    "surface-stale-automation-failures.py":
        "budget: SessionStart 19/19, and this is the heaviest candidate on "
        "that event -- a launchctl subprocess per job plus several log "
        "scans. Checkable: python3 scripts/footprint-sla-check.py --gate",
    "list-wip-stashes-on-session-start.py":
        "budget: its only useful event is SessionStart (the point is to "
        "warn BEFORE fresh work starts) and that event is 19/19. Checkable: "
        "python3 scripts/footprint-sla-check.py --gate",
    "scan-prior-sessions-for-secrets.py":
        "FORBIDDEN, not merely unbudgeted: "
        "tests/integration/test_sessionstart_freeze_class_excluded.sh "
        "asserts against the real hooks.json that this basename stays off "
        "SessionStart, and ships a negative control proving the assertion "
        "bites. Its corpus walk caused a machine freeze at load 36. "
        "Checkable: bash "
        "tests/integration/test_sessionstart_freeze_class_excluded.sh",
    "health-auto-sync.py":
        "deliberate opt-in with a regression test ENFORCING the dormancy: "
        "services/health-mcp/tests/test_v05_hooks.py asserts this basename "
        "is NOT in the SessionStart blob. It also does network I/O on cold "
        "start, the freeze class. Not debt. Checkable: grep -n health-auto- "
        "sync services/health-mcp/tests/test_v05_hooks.py",
    "check-cron-paths.sh":
        "budget: SessionStart 19/19, and it is POSIX-only (crontab -l), so "
        "it would tax a Windows install's cold start forever for a check "
        "that can never fire there. Checkable: python3 scripts/footprint- "
        "sla-check.py --gate",
    "pty-pressure-check.sh":
        "budget: SessionStart 19/19, plus an lsof subprocess on the one "
        "event the budgets file names as saturated, for a macOS-only, long- "
        "session-only failure a fresh install does not have. Checkable: "
        "python3 scripts/footprint-sla-check.py --gate",
    "rotate-logs.sh":
        "budget: SessionStart 19/19, and silent housekeeping is the wrong "
        "thing to buy the last slot with -- it belongs on a periodic job. "
        "Reads no hook payload at all (env/argv driven). Checkable: grep -c "
        "stdin hooks/rotate-logs.sh",
    "session-turn-counter.py":
        "two blockers. Stop is 5/5 against a hard cap of 5; and its user- "
        "facing message renders one operator's spend figure to every "
        "stranger's console, so it is unshippable as written regardless of "
        "budget. Checkable: grep -n 'last 30d burn' hooks/session-turn- "
        "counter.py",
    "create-dev-repo-checkpoint.py":
        "budget: Stop 5/5. Note the trap -- wiring it to SubagentStop "
        "instead would APPEAR to fit, but SubagentStop is absent from "
        "NON_MATCHER_EVENTS in footprint-sla-check.py, so that wire evades "
        "the SLA rather than satisfying it. Checkable: grep -n "
        "NON_MATCHER_EVENTS scripts/footprint-sla-check.py",
    "verify-discoverability-on-close.py":
        "two blockers. Stop 5/5; and its only actuator, discoverability- "
        "verifier.py, does not ship in this repo, so the exists() guard "
        "returns an empty gap list and it can never block. Checkable: git "
        "ls-files | grep -i discoverab",
    "warn-uncommitted-builds-on-stop.py":
        "budget: Stop 5/5, and a warn-only scan cannot displace a Stop- "
        "event blocker. Checkable: python3 scripts/footprint-sla-check.py "
        "--gate",
    "worktree-archive-autoprep.py":
        "budget: Stop 5/5, and it only fires when cwd is inside "
        ".claude/worktrees/, a workflow this substrate advises against for "
        "vaults -- a per-turn no-op on the reference install. Checkable: "
        "grep -n worktrees hooks/worktree-archive-autoprep.py",
    "reconcile-worktree-shared.py":
        "its own docstring says SessionEnd alone leaves a race window and "
        "it needs a Stop leg too; Stop is 5/5, so the wiring it calls "
        "necessary cannot be installed. Independently, a default-on hook "
        "that unlinks files and can auto-commit in a stranger's repo needs "
        "a far higher bar than a warn hook. Checkable: grep -n unlink "
        "hooks/reconcile-worktree-shared.py",
    "block-branch-switch-with-untracked-build.py":
        "budget: PreToolUse:Bash 9/9. A real data-loss guard, but its block "
        "message also cites a rule doc that does not ship here. Checkable: "
        "python3 scripts/footprint-sla-check.py --gate",
    "check-py-import-precommit.py":
        "budget: PreToolUse:Bash 9/9. Wire-ready the moment a Bash slot "
        "frees -- unlike its budget-blocked siblings it already has a test "
        "surface. Checkable: grep -n check-py-import-precommit "
        "tests/integration/test_session_coordination_guards.sh",
    "vault-command-nudges.py":
        "budget: PreToolUse:Bash 9/9, and its two load-bearing rules are "
        "already delivered by block-raw-vault-git.py and block-vault-git- "
        "fullwalk.py, which activate through the phase-doc channel. "
        "Checkable: grep -n block-raw-vault-git phases/phase-05-context- "
        "layer.md",
    "session-lock.py":
        "documented opt-in, not debt: docs/HOOKS_INSTALL.md states it is "
        "not auto-installed because it only matters with concurrent "
        "sessions on one repo, and it needs a manual global-gitignore step "
        "first. Its enforcement leg needs PreToolUse:Bash (9/9) anyway; "
        "wiring only the SessionEnd heartbeat would install bookkeeping "
        "without the gate, which is worse than dormant. Checkable: grep -n "
        "session-lock docs/HOOKS_INSTALL.md",
    "block-worktree-shared-edit.py":
        "budget: all three of its matchers (Write, Edit, MultiEdit) are "
        "12/12. On merit the strongest budget-blocked candidate -- "
        "substrate-general, data-loss class, already tested, already "
        "generalized off VAULT_ROOT. First in line if a Write slot is ever "
        "freed. Checkable: python3 scripts/footprint-sla-check.py --gate",
    "warn-recreate-deleted-file.py":
        "budget: PreToolUse:Write 12/12, and it is warn-only, so it cannot "
        "justify evicting a blocking guard. Checkable: python3 "
        "scripts/footprint-sla-check.py --gate",
    "auto-capture-public-ships.py":
        "personal: PENDING_SUBPATH carries an unsubstituted template "
        "placeholder on a mkdir(parents=True) WRITE path, so a stranger's "
        "install silently creates a folder named after the placeholder; and "
        "PUBLIC_REPOS is one operator's repo list, so the loop matches "
        "nothing anywhere else. Checkable: grep -n PENDING_SUBPATH "
        "hooks/auto-capture-public-ships.py",
    "build-runbook-check.py":
        "dependency does not ship: it gates on two private vault runbooks "
        "absent from this repo, so its read-check is always False on a "
        "stranger's install and it emits an unconditional false-positive "
        "nag citing lessons from a document the user does not have. "
        "Checkable: git ls-files | grep -ci 'Build Standards'",
    "hookify-auto-commit.py":
        "personal and unsafe by default: it creates commits in a stranger's "
        "repo without consent on every hookify-file edit, and its merge "
        "target is hardcoded to master with no main fallback, so on most "
        "repos it silently no-ops or fast-forwards the wrong branch. "
        "Checkable: grep -n master hooks/hookify-auto-commit.py",
    "imessage-mcp-auto-export.py":
        "dependency does not ship: its WRAPPER path is a maintainer-machine "
        "binary this repo does not install, so the isfile/access guard "
        "fails forever and wiring it buys a cold start per MCP call and "
        "zero behavior. Checkable: git ls-files | grep -ci imessage-export- "
        "vault",
    "inject-best-of-best-on-consulting.py":
        "personal: it injects one operator's house style (banning option- "
        "menus and cost framing), not a guard over a model-general defect. "
        "Its trigger vocabulary is ambiguous in the language it scans -- "
        "scope, sprint, package, rate, tier and offer are ordinary "
        "engineering words -- so it would fire on a large share of normal "
        "prompts. Checkable: grep -n KEYWORD hooks/inject-best-of-best-on- "
        "consulting.py",
    "route-suggest.py":
        "personal: it encodes one operator's model ladder and cost "
        "preference, and names a specific third-party vendor as a route, "
        "rather than guarding a bug class. Its classifier is broad enough "
        "to fire on a large share of ordinary prompts, on the one event "
        "that still has headroom. Checkable: grep -n minimax hooks/route- "
        "suggest.py",
    "whatsapp-mcp-auto-export.py":
        "dependency does not ship: its WRAPPER path is absent from this "
        "repo, so it takes the wrapper-missing branch forever. It also "
        "writes a fixed /tmp state filename that collides across concurrent "
        "sessions. Checkable: git ls-files | grep -ci export-vault",
    "file-changed-settings.sh":
        "superseded: pre-write-settings-lint.py (wired, PreToolUse) catches "
        "a bad config BEFORE it lands, and lint-claude-settings.py (wired) "
        "catches duplicate keys at any depth, which json.load tolerates. "
        "This hook's after-the-fact validity check is strictly weaker than "
        "both. Checkable: grep -n pre-write-settings-lint hooks.json",
    "pre-compact.sh":
        "superseded: pre-compact-context.py is already wired on PreCompact "
        "and does the same job. PreCompact has free slots, so budget is NOT "
        "the reason -- wiring both would double the injected-token cost on "
        "one event for one behavior. It also emits additionalContext where "
        "the repo documents systemMessage as PreCompact's channel. "
        "Checkable: grep -n pre-compact-context hooks.json",
    "check-sync-folder-machinery.py":
        "not a hook: a standalone bounded-walk audit driven by sys.argv "
        "that reads no stdin payload. It already reaches the fleet by two "
        "other routes -- scripts/vault-daily-maintenance.sh runs it, and "
        "surface-sync-guard-findings.py reads its snapshot from inside the "
        "wired worktree-footprint-signal.py. Checkable: grep -n sync- "
        "folder-machinery scripts/vault-daily-maintenance.sh",
    "claude-scheduled-runner.sh":
        "not a hook: an argv-driven launchd/cron entry point taking a task "
        "name, reading no stdin payload, so it can never appear in "
        "hooks.json. Misfiled in hooks/; belongs in scripts/. Checkable: "
        "grep -c stdin hooks/claude-scheduled-runner.sh",
    "cwd-changed.sh":
        "emits no signal on any path: its only output statement appends to "
        "a log file, so nothing reaches stdout or stderr. Its header also "
        "promises to run vault-integrity checks on entering a wrapped vault "
        "and the body contains no such check. Wiring it would ship a per- "
        "cwd-change interpreter spawn for zero user-visible behavior. "
        "Checkable: grep -n LOG hooks/cwd-changed.sh",
}

# --- A: the drained baseline, now pinned ------------------------------------
# EMPTY, and enforced empty. Every name that sat here was triaged to exactly one
# outcome: wired in hooks.json, or a checkable reason in TEMPLATE_ONLY above.
#
# WHY A PINNED COUNT AND NOT JUST A LIST. The docstring above has always said
# this baseline "may never GROW". Nothing enforced that. Measured on the parent
# commit: appending one name to this set took it 43 -> 44 and the gate printed
# "44 on the shrinking unclassified baseline" and exited 0. `len()` appeared
# twice in this file, both times inside that print, never once in a comparison.
# So the ratchet was a sentence, and any dormant hook could be waved through by
# adding a line to it -- which made "unbaselined" a movable goalpost rather than
# a predicate. That is the appendable-suppression-list class: amnesty must
# ratchet DOWN, never up.
#
# TEMPLATE_ONLY is the escape valve, and deliberately a different shape: it
# costs a WRITTEN, CHECKABLE reason that a reader can run, and a stale entry
# there fails the gate. Amnesty with no reason attached has no home any more.
UNCLASSIFIED_BASELINE: Set[str] = set()

# The cap. Raising this is the explicit, reviewable act of re-opening amnesty --
# which is the point: it can be done, but not silently, and not by one line in a
# set literal.
BASELINE_MAX = 0

# --- B: pre-existing debt (ratchet, may only shrink) -------------------------
# Wired at the skill path but absent from the installer's owned set, so the
# installer cannot dedup / replace / retire them. Fix by adding the fingerprint
# + basename to install-hooks-user-level.py, then removing the name here.
# Pinned by UNOWNED_MAX below, same reason as BASELINE_MAX.
UNOWNED_BASELINE: Set[str] = {
    "coach-auto-prescribe-on-journal.py",
    "post-tool-use-learnings.py",
    "pre-compact-context.py",
    "relocate-watch-surface.py",
    "surface-backup-status.py",
    "surface-connector-liveness.py",
    "surface-orphan-claude-branches.py",
    "surface-stranded-session-artifacts.py",
    "validate-skill-frontmatter.py",
}

UNOWNED_MAX = 9


PHASE_HOOK_RE = re.compile(r"hooks/([\w.-]+\.(?:py|sh))")


def phase_doc_registrations(repo_root: Path) -> Set[str]:
    """Hooks a phase doc tells the installing assistant to wire.

    THE THIRD CHANNEL. hooks.json calls itself canonical, and it is not the
    whole truth: some hooks are registered by instructions inside phases/*.md
    (block-raw-vault-git.py and block-vault-git-fullwalk.py via
    phase-05-context-layer.md). A gate that knows only the JSON channels
    reports those as dormant -- debt that is not debt, which is exactly the
    kind of false alarm that teaches people to ignore a gate. Surfaced by
    MYC-3550, which owns classifying what remains.

    KNOWN LIMITATION, accepted deliberately. This matches ANY `hooks/<name>`
    mention in a phase doc, including prose and negative examples -- a line
    reading "do NOT wire hooks/evil.py" marks evil.py activated. Tightening it
    means guessing which prose is an instruction, which is exactly the fragile
    heuristic that makes a gate untrustworthy. The failure mode is a false
    NEGATIVE (the gate stays quiet about one hook); it can never produce a
    false BLOCK, so it cannot wedge a build. Given the alternative was
    reporting 3 genuinely-activated hooks as dormant, quiet-on-one beats
    crying-wolf-on-three. If phases/*.md is ever formalised as a registration
    surface (MYC-3550 item 2), key this off that structure instead.
    """
    found: Set[str] = set()
    for doc in sorted(glob.glob(str(repo_root / "phases" / "*.md"))):
        found.update(PHASE_HOOK_RE.findall(
            Path(doc).read_text(encoding="utf-8", errors="replace")))
    return found


def is_test_file(basename: str) -> bool:
    """Structural exemption: a test is not a hook. Kept as CODE, not a list, so
    adding a test never requires touching an allow-list. Covers both naming
    conventions in hooks/: the `test_*` prefix and the `*.test.{py,sh}` infix."""
    return basename.startswith("test_") or ".test." in basename


def wired_basenames(*docs: dict) -> Set[str]:
    """Every script basename referenced by any hook command in any channel."""
    out: Set[str] = set()
    for doc in docs:
        for groups in (doc.get("hooks") or {}).values():
            for group in groups:
                for hook in group.get("hooks", []):
                    out.update(SCRIPT_RE.findall(hook.get("command", "")))
    return out


def iter_commands(doc: dict) -> Iterable[str]:
    for groups in (doc.get("hooks") or {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                if cmd:
                    yield cmd


def check_activation(hook_files: Iterable[str], wired: Set[str],
                     template_only: Dict[str, str],
                     baseline: Set[str],
                     baseline_max: int | None = None) -> List[str]:
    """A: every shipped hook is wired, decided, or on the shrinking baseline."""
    problems: List[str] = []

    # THE RATCHET, enforced. Without this the baseline is an amnesty list anyone
    # can append to, and the gate reports OK while the debt grows.
    if baseline_max is not None and len(baseline) > baseline_max:
        problems.append(
            f"A: UNCLASSIFIED_BASELINE has {len(baseline)} entries but "
            f"BASELINE_MAX is {baseline_max}.\n    The baseline may only "
            f"SHRINK. Wire the hook in hooks.json, or add it to TEMPLATE_ONLY "
            f"with a checkable reason. Raising BASELINE_MAX re-opens amnesty "
            f"and must be a deliberate, reviewed change."
        )
    files = {b for b in hook_files if not is_test_file(b)}

    for basename in sorted(files):
        if basename in wired or basename in template_only or basename in baseline:
            continue
        problems.append(
            f"A: hooks/{basename} is shipped but wired in NO activation "
            f"channel.\n    It cannot fire on any install. Wire it in "
            f"hooks.json, or add it to TEMPLATE_ONLY with a reason "
            f"(scripts/check-hook-activation.py)."
        )

    # Ratchet: an entry that no longer needs excusing must be removed, else the
    # list silently re-accumulates slack and stops meaning anything.
    for basename in sorted(baseline):
        if basename not in files:
            problems.append(
                f"A: '{basename}' is on UNCLASSIFIED_BASELINE but no such hook "
                f"exists.\n    Remove the stale entry."
            )
        elif basename in wired:
            problems.append(
                f"A: '{basename}' is on UNCLASSIFIED_BASELINE but is now WIRED."
                f"\n    Remove it -- the debt is paid."
            )
    for basename in sorted(template_only):
        if basename not in files:
            problems.append(
                f"A: '{basename}' is on TEMPLATE_ONLY but no such hook exists."
                f"\n    Remove the stale entry."
            )
    return problems


def check_ownership(commands: Iterable[str], is_owned: Callable[[str], bool],
                    baseline: Set[str],
                    baseline_max: int | None = None) -> List[str]:
    """B: every hook we wire is also owned by the installer."""
    problems: List[str] = []

    if baseline_max is not None and len(baseline) > baseline_max:
        problems.append(
            f"B: UNOWNED_BASELINE has {len(baseline)} entries but UNOWNED_MAX "
            f"is {baseline_max}.\n    Add the hook's fingerprint + basename to "
            f"scripts/install-hooks-user-level.py instead of widening the list."
        )
    offenders: Set[str] = set()

    for cmd in commands:
        shipped = ABS_SHIPPED_RE.findall(cmd)
        if not shipped or is_owned(cmd):
            continue
        offenders.update(shipped)

    for basename in sorted(offenders - baseline):
        problems.append(
            f"B: hooks/{basename} is WIRED but not in the installer's owned "
            f"set.\n    A re-install cannot dedup or replace it -- a hand-wired "
            f"copy double-fires, and verify_paths_on_disk() skips it. Add its "
            f"fingerprint + basename to scripts/install-hooks-user-level.py."
        )
    for basename in sorted(baseline - offenders):
        problems.append(
            f"B: '{basename}' is on UNOWNED_BASELINE but is now owned (or no "
            f"longer wired).\n    Remove the stale entry."
        )
    return problems


def run_gate(repo_root: Path) -> int:
    template = json.loads((repo_root / "hooks.json").read_text(encoding="utf-8"))
    docs = [template]
    for extra in sorted(set(glob.glob(str(repo_root / "hooks" / "hooks.json"))
                            + glob.glob(str(repo_root / "skills" / "*" / "hooks" / "hooks.json")))):
        docs.append(json.loads(Path(extra).read_text(encoding="utf-8")))

    wired = wired_basenames(*docs) | phase_doc_registrations(repo_root)
    hook_files = [os.path.basename(p)
                  for p in glob.glob(str(repo_root / "hooks" / "*.py"))
                  + glob.glob(str(repo_root / "hooks" / "*.sh"))]

    spec = importlib.util.spec_from_file_location(
        "abs_installer", repo_root / "scripts" / "install-hooks-user-level.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)

    problems: List[str] = []
    problems += check_activation(hook_files, wired, TEMPLATE_ONLY,
                                 UNCLASSIFIED_BASELINE, BASELINE_MAX)
    problems += check_ownership(iter_commands(template), installer.is_abs_owned,
                                UNOWNED_BASELINE, UNOWNED_MAX)

    examined = len([b for b in hook_files if not is_test_file(b)])
    activated = len([b for b in hook_files
                     if not is_test_file(b) and b in wired])
    print(f"hook activation: {activated}/{examined} shipped hooks wired; "
          f"{len(UNCLASSIFIED_BASELINE)}/{BASELINE_MAX} on the unclassified "
          f"baseline, {len(UNOWNED_BASELINE)}/{UNOWNED_MAX} on the unowned "
          f"baseline (both capped; a cap is raised deliberately, never by "
          f"appending a name).")

    if problems:
        print()
        for p in problems:
            print(f"::error::{p}")
        print(f"\n{len(problems)} hook-activation problem(s).")
        return 1
    print("hook activation OK -- every shipped hook is wired or accounted for.")
    return 0


def self_test() -> int:
    """Negative controls. Each check must BITE on the exact shape that shipped."""
    results: List[tuple] = []

    def case(label, got, want):
        results.append((label, got, want))

    # --- A ---
    # The incident: a shipped hook in no channel and on no list.
    case("A: unwired + unlisted -> FAIL",
         bool(check_activation({"guard.py"}, set(), {}, set())), True)
    # THE SPEC ERROR: being owned is not being wired. This check never consults
    # the owned set for activation, so an owned-but-unwired hook still fails --
    # the `or` in the original MYC-1031 spec would have passed it.
    case("A: wired -> pass",
         bool(check_activation({"guard.py"}, {"guard.py"}, {}, set())), False)
    case("A: on TEMPLATE_ONLY -> pass",
         bool(check_activation({"guard.py"}, set(), {"guard.py": "why"}, set())),
         False)
    case("A: on baseline -> pass",
         bool(check_activation({"guard.py"}, set(), {}, {"guard.py"})), False)
    case("A: tests are not hooks -> pass",
         bool(check_activation({"test_x.py", "y.test.py"}, set(), {}, set())),
         False)
    # Both naming conventions live in hooks/; missing either re-opens the false
    # alarm that .test.sh files are dormant hooks.
    case("A: .test.sh is not a hook -> pass",
         bool(check_activation({"y.test.sh"}, set(), {}, set())), False)
    # Shell hooks are in scope. They were NOT, and 9 of them (PreCompact,
    # FileChanged, SessionStart, WebFetch pre/post) sat outside the gate
    # entirely -- the guard's own scope as its blind spot.
    case("A: unwired .sh hook -> FAIL",
         bool(check_activation({"guard.sh"}, set(), {}, set())), True)
    case("A: wired .sh hook -> pass",
         bool(check_activation({"guard.sh"}, {"guard.sh"}, {}, set())), False)
    # The third channel: a hook registered by a phase doc is ACTIVATED. Treating
    # it as dormant is a false alarm, and false alarms are how a gate gets
    # ignored. `wired` is the union of every channel, so this is the same path
    # phase_doc_registrations() feeds.
    case("A: registered via a phase doc -> pass",
         bool(check_activation({"block-raw-vault-git.py"},
                               {"block-raw-vault-git.py"}, {}, set())), False)
    # THE RATCHET CAP -- the assertion that did not exist before, and the exact
    # shape that shipped: a dormant hook waved through by appending one name to
    # the baseline. Measured on the parent commit as 43 -> 44, exit 0.
    case("A: baseline OVER cap -> FAIL",
         bool(check_activation({"g.py"}, set(), {}, {"g.py"}, 0)), True)
    case("A: baseline AT cap -> pass",
         bool(check_activation({"g.py"}, set(), {}, {"g.py"}, 1)), False)
    # Back-compat: cap is opt-in, so a caller that passes no max keeps the old
    # behaviour. If this ever flips to FAIL the default changed under someone.
    case("A: no cap given -> cap not enforced",
         bool(check_activation({"g.py"}, set(), {}, {"g.py"})), False)
    # The drained state itself: an empty baseline against a zero cap must be
    # quiet, or the shipped configuration reds its own gate.
    case("A: empty baseline at cap 0 -> pass",
         bool(check_activation({"g.py"}, {"g.py"}, {}, set(), 0)), False)

    # Ratchet.
    case("A: stale baseline entry (file gone) -> FAIL",
         bool(check_activation(set(), set(), {}, {"gone.py"})), True)
    case("A: baseline entry now wired -> FAIL",
         bool(check_activation({"g.py"}, {"g.py"}, {}, {"g.py"})), True)
    case("A: stale TEMPLATE_ONLY entry -> FAIL",
         bool(check_activation(set(), set(), {"gone.py": "why"}, set())), True)

    # --- B ---
    owned = lambda c: "OWNED" in c  # noqa: E731
    unowned_cmd = "python3 ~/.claude/skills/ai-brain-starter/hooks/g.py"
    owned_cmd = "python3 ~/.claude/skills/ai-brain-starter/hooks/g.py OWNED"
    optional = ("[ -f ~/.claude/hooks/third-party.py ] && "
                "python3 ~/.claude/hooks/third-party.py")
    case("B: wired but unowned -> FAIL",
         bool(check_ownership([unowned_cmd], owned, set())), True)
    case("B: wired and owned -> pass",
         bool(check_ownership([owned_cmd], owned, set())), False)
    case("B: optional third-party hook is out of scope -> pass",
         bool(check_ownership([optional], owned, set())), False)
    case("B: unowned but on baseline -> pass",
         bool(check_ownership([unowned_cmd], owned, {"g.py"})), False)
    case("B: stale baseline entry -> FAIL",
         bool(check_ownership([owned_cmd], owned, {"g.py"})), True)
    case("B: unowned baseline OVER cap -> FAIL",
         bool(check_ownership([unowned_cmd], owned, {"g.py"}, 0)), True)
    case("B: unowned baseline AT cap -> pass",
         bool(check_ownership([unowned_cmd], owned, {"g.py"}, 1)), False)

    failed = 0
    for label, got, want in results:
        ok = got == want
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            failed += 1
    print(f"\n=== selftest: {len(results) - failed} passed, {failed} failed ===")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true",
                    help="run positive + negative controls, then exit")
    args = ap.parse_args()
    return self_test() if args.selftest else run_gate(REPO_ROOT)


if __name__ == "__main__":
    sys.exit(main())
