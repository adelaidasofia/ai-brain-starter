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
}

# --- A: pre-existing debt (ratchet, may only shrink) -------------------------
# Shipped before this gate existed; each still needs a wire-or-retire decision
# under MYC-1031 item 1. Listing them is the opposite of silence: they are
# counted, printed on every run, and the count cannot grow. Resolve one by
# either wiring it in hooks.json or deleting it, then remove it from this list
# -- a stale entry is itself a failure.
UNCLASSIFIED_BASELINE: Set[str] = {
    "agent-briefing-check.py",
    "auto-capture-public-ships.py",
    "block-branch-switch-with-untracked-build.py",
    "block-worktree-shared-edit.py",
    "build-runbook-check.py",
    "check-cron-paths.sh",
    "check-py-import-precommit.py",
    "check-rule-conflicts-on-write.py",
    "check-sync-folder-machinery.py",
    "check_fab_shim.py",
    "claude-scheduled-runner.sh",
    "create-dev-repo-checkpoint.py",
    "cwd-changed.sh",
    "detect-secrets-in-bash-output.py",
    "file-changed-settings.sh",
    "health-auto-sync.py",
    "hookify-auto-commit.py",
    "imessage-mcp-auto-export.py",
    "inject-best-of-best-on-consulting.py",
    "list-wip-stashes-on-session-start.py",
    "nudge-checkpoint-after-pytest-pass.py",
    "pre-compact.sh",
    "pty-pressure-check.sh",
    "reconcile-worktree-shared.py",
    "rotate-logs.sh",
    "route-suggest.py",
    "scan-prior-sessions-for-secrets.py",
    "scrub-session-jsonl-secrets.py",
    "sdd-cache-post.sh",
    "sdd-cache-pre.sh",
    "session-lock.py",
    "session-turn-counter.py",
    "sunday-review-nudge.py",
    "surface-dependabot-backlog.py",
    "surface-stale-automation-failures.py",
    "validate-calendar-timezone.py",
    "validate-subagent-return.py",
    "vault-command-nudges.py",
    "verify-discoverability-on-close.py",
    "warn-recreate-deleted-file.py",
    "warn-uncommitted-builds-on-stop.py",
    "whatsapp-mcp-auto-export.py",
    "worktree-archive-autoprep.py",
}

# --- B: pre-existing debt (ratchet, may only shrink) -------------------------
# Wired at the skill path but absent from the installer's owned set, so the
# installer cannot dedup / replace / retire them. Fix by adding the fingerprint
# + basename to install-hooks-user-level.py, then removing the name here.
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
                     baseline: Set[str]) -> List[str]:
    """A: every shipped hook is wired, decided, or on the shrinking baseline."""
    problems: List[str] = []
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
                    baseline: Set[str]) -> List[str]:
    """B: every hook we wire is also owned by the installer."""
    problems: List[str] = []
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
                                 UNCLASSIFIED_BASELINE)
    problems += check_ownership(iter_commands(template), installer.is_abs_owned,
                                UNOWNED_BASELINE)

    examined = len([b for b in hook_files if not is_test_file(b)])
    activated = len([b for b in hook_files
                     if not is_test_file(b) and b in wired])
    print(f"hook activation: {activated}/{examined} shipped hooks wired; "
          f"{len(UNCLASSIFIED_BASELINE)} on the shrinking unclassified "
          f"baseline, {len(UNOWNED_BASELINE)} on the unowned baseline.")

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
