#!/usr/bin/env bash
#
# scripts/ci.sh - the canonical, locally-runnable unit/type gate for
# ai-brain-starter. ONE command, shared by two callers so they can never drift:
#
#   1. .github/workflows/lint.yml  - the `ci` job runs `bash scripts/ci.sh`.
#   2. ~/.local/bin/ci-test        - resolves this repo to mode=canon and runs
#                                    the same `bash scripts/ci.sh` pre-push.
#
# It runs EXACTLY the unit/type gate that lint.yml used to run as separate jobs:
#   (a) Python syntax     - py_compile every tracked *.py. Catches the PEP 604
#                           `X | None` annotation class that crashes Python 3.9
#                           at module load (many users invoke hooks via macOS's
#                           system /usr/bin/python3, which is 3.9).
#   (b) Shell integration - the named tests under tests/integration/, in
#                           order, stopping at the first failure.
#   (c) Shell static analysis - scripts/shellcheck.sh, the SAME script lint.yml's
#                           dedicated `shellcheck` job runs, so the local pre-push
#                           gate and CI cannot drift on shell quality. Skipped here
#                           when running inside GitHub Actions (the dedicated job
#                           already covers it); locally it is warn-and-skipped
#                           when the shellcheck binary is absent (CI enforces it).
#   (d) Phase-doc Python  - scripts/check-phase-python.py extracts every Python
#                           block from phases/*.md and runs the undefined-name
#                           check (ruff F821). Catches a bare-identifier typo in
#                           an install heredoc - a runtime NameError that (a)'s
#                           py_compile cannot see (it lints tracked *.py only, and
#                           py_compile does not catch undefined names). A dedicated
#                           lint.yml job enforces this in CI (as the shellcheck job
#                           does); here it is best-effort, skipped without a linter.
#   (e) UTF-8 console guard - scripts/check-utf8-stdout.py fails a runnable
#                           scripts/*.py or hooks/*.py CLI that print()s non-ASCII
#                           (the "gear Meta" emoji, an em dash, an accented name)
#                           without reconfiguring stdout/stderr to UTF-8. On a
#                           Windows cp1252 console that print() raises
#                           UnicodeEncodeError and the caller reads the empty
#                           output as failure (ai-brain-starter#313). hooks/ came
#                           into scope in MYC-3530; its 78 pre-existing violations
#                           are content-pinned in scripts/utf8-stdout-baseline.txt,
#                           so an edit to any of them reds this gate.
#                           A dedicated lint.yml 'utf8-console-guard' job is
#                           authoritative in CI; here it runs locally (pure stdlib).
#   (e2) Hook block-protocol - scripts/check-hook-block-protocol.py fails a hook
#                           registered with the allow-fallback wrapper
#                           (`... || echo '{...permissionDecision:allow}'`) that
#                           blocks by exiting non-zero: the `||` fires on ANY
#                           non-zero exit, so the block is rewritten into an
#                           ALLOW and the guard is structurally unable to say no
#                           (#405). Pure stdlib.
#   (e3) VAULT_ROOT reads - scripts/check-vault-root-reads.py fails code that
#                           reads the VAULT_ROOT env var outside a sanctioned
#                           resolver. A globally-exported VAULT_ROOT names ONE
#                           vault: unset, the usual ~/vault default makes a guard
#                           inert on every vault not named "vault"; set, it
#                           overrides the vault the caller meant. Same
#                           SILENT-NO-OP family as (e)/(e2); pure stdlib.
#   (e4) home-hook deploy - scripts/check-home-hook-deploy.py fails when hooks.json
#                           wires a hook from ~/.claude/hooks/ that no route ever
#                           copies there. The `[ -f ]` guard on those commands makes
#                           a never-deployed hook look identical to one the user
#                           switched off, so it never fires and nothing says so.
#                           Same SILENT-NO-OP family as (e)/(e2)/(e3); pure stdlib.
#   (e5) subprocess decode - scripts/check-utf8-subprocess.py is the READ half of (e).
#                           (e) fails a CLI that PRINTS non-ASCII without a UTF-8
#                           stdout guard; this fails one that READS a child's output
#                           with text=True and no encoding=, so the decode uses the
#                           console code page. Every child here prints a vault path
#                           and every vault path carries the gear emoji, whose 0x8F
#                           byte is unmapped in cp1252 -> UnicodeDecodeError inside
#                           subprocess.run(). Shipped twice: #313 (write side) and
#                           #430 (read side, memory stranded outside the vault).
#                           Content-pinned in scripts/utf8-subprocess-baseline.txt.
#   (f) Python unit tests - the scripts/test_*.py stdlib suites (the claude-router
#                           structured-envelope gate, the graph-liveness
#                           STAMP-GREEN-WHILE-GONE guard). Gate (a) py_compiles them,
#                           which proves they PARSE but never that their asserts RUN;
#                           a suite that compiles but never executes is a false green
#                           (revert the code it guards and the gate stays green -
#                           MYC-2922). scripts/test_*.py runs as a GLOB (a new one can
#                           never go dormant); hooks/test_*.py + tests/test_*.py are
#                           guarded by a coverage invariant so a new one must be wired
#                           to run (wrapper or direct) or the gate fails (MYC-2959).
#                           Unlike (c)/(d)/(e) it has no dedicated CI job, so it runs
#                           in BOTH CI and the local pre-push gate (like (a) and (b)).
#
# It does NOT run the OTHER pure-lint jobs (bash -n, pwsh ParseFile, BOM, em-dash,
# JSON, privacy, references, no-remote-pipe-install). Those stay as their own
# lint.yml jobs - they are lint, not the unit/type gate. Two are exceptions,
# enforced pre-push because they are cross-platform CORRECTNESS gates, not style:
# the shell static-analysis gate (the GNU-vs-BSD `stat` mtime class) and the UTF-8
# console guard (the Windows cp1252 print-crash class).
#
# Environment the integration tests need (lint.yml provides these in the `ci`
# job; this script adds a non-invasive fallback so a fresh `bash scripts/ci.sh`
# works for a contributor too):
#   - A git identity. The tests create temp repos and commit; they set their own
#     LOCAL identity, but a fresh box may have no ambient identity at all, so we
#     export GIT_* defaults only when nothing is configured.
#   - ruff on PATH is OPTIONAL: test_session_coordination_guards exercises the
#     F821 block path when ruff is present and fails OPEN (still green) when not.
#   - Python 3.9 for the py_compile check is IDEAL (it is the version whose crash
#     class this gate exists to catch). We prefer `python3.9` when present and
#     fall back to `python3` otherwise. lint.yml pins 3.9 via setup-python, so
#     CI is always authoritative for the 3.9 check; the local fallback to a newer
#     interpreter is a weaker superset check (it will not catch 3.9-only crashes).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# Keep generated .pyc out of the working tree. This script runs IN the checkout
# (ci-test runs it inside the live worktree), so route every child interpreter's
# bytecode cache to a temp dir instead of littering __pycache__/ across the repo.
PYCACHE_TMP="$(mktemp -d)"
export PYTHONPYCACHEPREFIX="$PYCACHE_TMP"
cleanup() { rm -rf "$PYCACHE_TMP"; }
trap cleanup EXIT

# Non-invasive git identity fallback - only when nothing is configured. Uses env
# vars (not `git config --global`) so we never mutate the caller's global config.
if [ -z "$(git config user.email 2>/dev/null || true)" ]; then
  export GIT_AUTHOR_NAME="ci" GIT_AUTHOR_EMAIL="ci@example.com"
  export GIT_COMMITTER_NAME="ci" GIT_COMMITTER_EMAIL="ci@example.com"
fi

# PyYAML, in CI only. The metadata extractors and the insight engine `import
# yaml`, and test_extractors_localized_vault runs them end to end; setup-python
# ships without PyYAML, so on the runner that suite would SKIP forever. On a
# contributor's machine nothing is installed: the test finds an interpreter that
# has yaml or says SKIP, and running the extractors already required it anyway.
if [ -n "${GITHUB_ACTIONS:-}" ] && ! python3 -c "import yaml" >/dev/null 2>&1; then
  python3 -m pip install --user --quiet pyyaml >/dev/null 2>&1 \
    || python3 -m pip install --quiet pyyaml >/dev/null 2>&1 \
    || echo "    (PyYAML install failed; test_extractors_localized_vault will SKIP)"
fi

# ---- (a) Python syntax gate ------------------------------------------------
if command -v python3.9 >/dev/null 2>&1; then
  PY=python3.9
else
  PY=python3
fi
echo "==> (a) Python syntax: py_compile every tracked *.py  [$("$PY" --version 2>&1)]"
err="$(mktemp)"
fail=0
count=0
while IFS= read -r -d '' f; do
  count=$((count + 1))
  if ! "$PY" -m py_compile "$f" 2>"$err"; then
    echo "::error file=$f::python3 -m py_compile failed"
    sed 's|^|  |' "$err"
    fail=1
  fi
done < <(git ls-files -z -- '*.py')
rm -f "$err"
if [ "$fail" != 0 ]; then
  echo "FAILED: Python syntax gate (see errors above)"
  exit 1
fi
echo "    OK - $count file(s) compiled clean"

# ---- (b) Shell integration gate --------------------------------------------
# The named tests that constitute lint.yml's integration gate, in order.
# `set -e` aborts on the first non-zero test, which is the required
# stop-on-first-failure behavior. This list is the gate's source of truth:
# tests/integration/ also holds .sh/.py files that are NOT part of this gate, so
# it must be an explicit allow-list, never a glob over the directory.
INTEGRATION_TESTS=(
  test_session_start_announces_memory_index
  test_worktree_session_close
  test_bootstrap_dry_run
  test_dry_run_purity
  test_phase_doc_slash_commands_installed
  test_reconcile_ff_invariant
  test_ai_brain_auto_update
  test_installer_replaces_auto_update
  test_verify_fallback_chain_optional
  test_verify_real_hooksjson_healthy_install
  test_detect_closing_signal_worktree
  test_detect_closing_signal_repo_aware_vault
  test_detect_closing_signal_goal_clear
  test_close_phase_numbering_aligned
  test_phase_chain_contract
  test_closing_claim_shared
  test_meta_resolver
  test_meta_resolution_guard
  test_split_meta
  test_context_load_selftest
  test_verify_cascade_failsafe
  test_verify_cascade_repo_aware_vault
  test_aggregator_vault_root_guard
  test_discoverability_partition
  test_stranded_session_artifacts_watchdog
  test_offmain_strand_guard
  test_session_coordination_guards
  test_cd_worktree_guard_wiring
  test_trust_prompt_preframing
  test_onboarding_wrong_surface_and_nudge
  test_post_update_email_ask
  test_installer_retires_email_gate
  test_installer_relocates_moved_hooks
  test_installer_shim_safe_interpreter
  test_deployed_hooks_behind
  test_sync_guard_surface
  test_windows_platformize
  test_memory_routing_guard
  test_bootstrap_omits_vault_hooks
  test_bootstrap_archive_entry
  test_bootstrap_brew_terminal_step
  test_bootstrap_optional_packs_soft_fail
  test_bootstrap_corporate_profile
  test_bootstrap_userspace_fallback
  test_preflight_git_and_it_request
  test_remediate_runaway_procs
  test_scan_prior_single_instance
  test_scan_prior_failclosed_scrub
  test_sessionstart_freeze_class_excluded
  test_sessionstart_boundedness
  test_orphan_branch_bounded_git
  test_footprint_sla
  test_vault_safety_guards
  test_vault_backup_conf_bom
  test_backup_staleness_surfaces
  test_home_fingerprint_noise
  test_scheduled_task_registration
  test_vault_backup_task_healing
  test_resource_aware_session_close
  test_cloud_sync_guard
  test_cloud_safe_file_walkers
  test_delegated_task_needs_source
  test_cloud_sync_offer
  test_worktree_on_vault_guard
  test_machinery_sidecar
  test_relocate_vault
  test_relocate_sweep
  test_relocate_watch
  test_relocate_ps1
  test_renderer_crash_guard
  test_install_api_canonical_base
  test_cd_worktree_inline_bypass
  test_sessionstart_hook_snapshot_guard
  test_context_budget_measure
  test_connector_liveness_watchdog
  test_connector_liveness_surface
  test_granola_sync_offline
  test_granola_core_shared
  test_agent_memory_link
  test_open_core_boundary
  test_template_purity
  test_audited_content_injection_scan
  test_post_tool_use_learnings
  # Wired 2026-07-02 — found dormant by the gate-coverage invariant below.
  # These existed on disk, passed locally, and never ran in CI.
  test_detect_closing_signal_strict_guards
  test_detect_closing_signal_es_guards
  test_journal_index_localized_dir
  test_inject_meeting_workflow_truncation_flag
  test_install_path_verification
  test_meeting_todos_step0_create_if_absent
  test_meeting_workflow_trigger_hook
  test_personal_brain_not_optional
  test_phase11_writes_to_vault_rule_file
  test_post_commit_ff_worktrees
  test_write_hook_meeting_folder_i18n
  test_vault_script_sync
  # UTF-8 console guard (ai-brain-starter#313 follow-up): negative controls prove the
  # lint FAILS an unguarded non-ASCII-printing CLI and that the guard is load-bearing
  # under a real cp1252 console; also asserts the real scripts/ tree is clean.
  test_utf8_console_guard
  # Journal Step-0 context-guard self-heal (2026-07-07): end-to-end proof the
  # SessionStart repair restores an unprotected account (registration under both
  # matchers + vault preflight) and no-ops on a healthy one, with pos/neg controls.
  test_heal_journal_guard
  # Anti-fabrication guard family (MYC-1017): proves a fresh install REGISTERS the
  # Stop + PreToolUse guards, and that the SHIPPED wiring blocks the incident it
  # exists for while passing an honest close. File presence is not the assertion —
  # activation is (the family sat dormant in the repo precisely because nothing
  # asserted registration).
  test_installer_registers_fabrication_guards
  # Hookify template capability gate (2026-07-18): the OFFICIAL engine returns
  # False for an operator it does not implement and None for a field it cannot
  # resolve, so a shipped template using either loads fine and SILENTLY NEVER
  # FIRES — protection that is not actually there. Negative controls prove the
  # gate reddens on a bad operator, a bad field, and a stale allowlist entry.
  test_hookify_template_capabilities
  # Owned-hook dedup (follow-up to the matcher-aware merge): a hook shipped under two
  # matchers whose stored interpreter path drifted duplicated per matcher; proves owning
  # observe-tool-calls + the dedupe pass collapse the drift copy and never touch user hooks.
  test_installer_dedupes_owned_hooks
  # Handoff consumes_when guard (#375): proves the guard detects a vault by its
  # Meta folder (not a hardcoded ~/vault), denies via the JSON protocol the
  # hooks.json wrapper preserves, covers MultiEdit, and fails OPEN off-vault.
  test_handoff_frontmatter_guard
  # Naive VAULT_ROOT read ban (MYC-2505): negative controls for the (e3) gate.
  # Proves the guard trips on all four read forms (including one indirected
  # through a module constant), that an exemption with no reason is itself a
  # violation, that the hash ratchet bites on an EDITED pinned file and on a row
  # left behind after its file went clean -- and that the guard has not gone
  # blind (fleet scan still matches, sanctioned resolver names still exist,
  # pinned rows are still real violations, guard still wired into both gates).
  test_vault_root_read_guard
  # SEV-A remediation (MYC-3529): the guard above froze 12 naive VAULT_ROOT reads
  # in hooks/; this proves they are FIXED. Every assertion points $VAULT_ROOT at a
  # decoy vault and acts on a different one, because that is the assertion the
  # whole class was missing -- #404's bug survived a full suite precisely because
  # every test ran against the vault the env var already named, which makes
  # "resolve from the env" and "resolve from the target" indistinguishable.
  test_hook_vault_root_per_target
  # The rm -rf rule in that same hook, which MYC-3529 left alone: its regex
  # spelled the vault root `$HOME/vault` -- a SHELL string in a PYTHON regex,
  # where `$` is an end-of-line anchor, so the branch was dead and the vault
  # root was never blocked. Asserts on RESOLVED targets (abs, relative, quoted,
  # escaped) against a vault named neither "vault" nor by any hardcoded folder
  # name, so a rule that pattern-matches names cannot pass it.
  test_rm_rf_vault_target
  # The High-Rise vendor pin is a content hash like the two ratchets above, and
  # had the same defect: hashing raw bytes made a CRLF checkout report all three
  # vendored files as hand-edited. Proves the pin is line-ending independent AND
  # -- the control that matters -- that normalizing did not make the drift guard
  # blind to a genuine hand-edit on either line ending.
  test_high_rise_pin
  # Windows HOME-sandbox hermeticity (MYC-3536): a test that redirects HOME must
  # redirect USERPROFILE with it, because Python on Windows resolves "~" from
  # USERPROFILE and ignores HOME. Without this the suite rewrote the developer's
  # REAL ~/.claude/settings.json, pointing 95 of 111 hook entries at a temp
  # worktree; once deleted every hook exited 2 (the BLOCK signal) and denied
  # every tool call. This static half runs on Linux CI, where the runtime
  # tripwire below cannot see the bug because HOME works there.
  test_home_sandbox_hermeticity
  # bash-3.2 portability (2026-08-15): the guard above built its offender list
  # with `mapfile`, a bash-4 builtin macOS /bin/bash does not have. Under
  # `set -u` without `-e` that is non-fatal, so its primary check evaluated
  # NOTHING on every Mac while the file still printed PASS and exited 0; the
  # same defect left hooks/rotate-logs.sh rotating no logs at all. Neither
  # existing gate can see the class: the bash32-syntax job is `-n` only and all
  # of these PARSE on 3.2 (they fail at run time), and shellcheck has no
  # bash-version model. Static, so it gives the same verdict on any runner.
  test_bash32_portability
  # Root cause of the same incident: the runner path baked into settings.json
  # came from Path(__file__), so installing from a throwaway worktree wired ~95
  # hooks to a path that vanished with it. Pins the resolution to the INSTALLED
  # copy, with a negative control that a first install from a dev tree still
  # wires a runner that exists.
  test_hook_runner_path_stability
  # In-flight git-operation gate (incident 2026-07-28): proves a fresh install
  # REGISTERS the guard, wires it in the block-preserving `if [ -f ]` form, and
  # that the SHIPPED command refuses a commit into a genuinely stalled rebase
  # while allowing one in a clean repo. Registration is the assertion — a guard
  # present on disk and absent from settings.json protects nobody.
  test_installer_registers_inflight_guard
  # MCP secret-leak guards (MYC-3560): block-claude-mcp-inline-secret.py and
  # block-mcp-config-inline-secret.py were written after three real GitHub PAT
  # leaks and shipped as working files, referenced nowhere — never wired, so
  # never once fired on any install. Same registration-is-the-assertion proof
  # as the guard above, for both hooks: wired in the block-preserving form,
  # and the shipped command actually BLOCKS a seeded secret while passing a
  # clean payload.
  test_installer_registers_mcp_secret_guards
  # Skip-prefix privacy guard: a `__SKIP` line is content the user told the
  # assistant NOT to persist, and a persisted line cannot be un-persisted (file
  # + git history + any index over the vault). Every assertion carries a
  # negative control, because a guard that blocks everything and one that blocks
  # the right thing produce identical PASSes on a block-only suite.
  test_skip_prefix_guard
  # Journal-context guard vs Windows paths. Its patterns are forward-slash only,
  # so a `C:\vault\Journals\...` write matched nothing and the gate never opened
  # — silently, on a whole platform. Two layers must hold (the path gate AND the
  # vault-root resolve); fixing only the first looks right and still fails open.
  test_journal_guard_windows_paths
  # Floor name -> number map (2026-08-16): the journal extractor hand-kept the
  # pre-expansion 17-level English map while the framework has 34 floors with
  # Spanish names, so Spanish floors and 16 English ones scored nothing and the
  # rest scored on the wrong scale. _floors.py is now the one map, a COPY of
  # vendor/high-rise/floors.md (the extractors are symlinked into vaults where
  # vendor/ is absent); this asserts the copy matches the table, with a planted
  # drift as negative control. Pure stdlib.
  test_floor_name_map_canonical
  # The same fix end to end on a Spanish vault: journals found in 📓 Diarios,
  # floor names (es + en) scored, es/rise types routed to real extractors, a
  # user's own extractor beating an alias, and the engine's floor baseline.
  # 11 assertions fail on the pre-fix tree. Needs PyYAML (the extractors import
  # it); says SKIP and exits 0 when no interpreter has it, and the CI-only
  # bootstrap near the top of this script installs it so CI never takes that path.
  test_extractors_localized_vault
)
# ---- Gate-coverage invariant -------------------------------------------------
# The list above is an explicit allow-list, and allow-lists rot: a new
# tests/integration/test_*.sh that never gets listed passes locally forever and
# never runs in CI (caught live 2026-07-02: nine suites were dormant, one of
# them hiding a hermeticity bug). Every test_*.sh file must be IN the list, or
# named here with a one-line reason. A test that shouldn't gate merges still
# gets a row here — silence is the only banned state.
GATE_EXEMPT=(
)
missing_from_gate=()
for f in tests/integration/test_*.sh; do
  name="$(basename "$f" .sh)"
  found=0
  for t in "${INTEGRATION_TESTS[@]}" ${GATE_EXEMPT[@]+"${GATE_EXEMPT[@]}"}; do
    if [ "$t" = "$name" ]; then found=1; break; fi
  done
  if [ "$found" -eq 0 ]; then missing_from_gate+=("$name"); fi
done
if [ "${#missing_from_gate[@]}" -gt 0 ]; then
  echo "::error::integration test file(s) not wired into the gate — add each to INTEGRATION_TESTS (or GATE_EXEMPT with a reason): ${missing_from_gate[*]}"
  exit 1
fi

# ---- Real-home tripwire (MYC-3536) ------------------------------------------
# The suite must never touch the developer's real ~/.claude/settings.json. It is
# not enough to trust each test's own sandboxing: on Windows `HOME=...` does NOT
# redirect "~" for Python (ntpath.expanduser reads USERPROFILE and ignores HOME),
# so tests that look sandboxed ran against the real file for months. Live damage
# on 2026-07-30: the suite rewrote the real settings.json to point 95 of 111 hook
# entries at hook_runner.py inside the throwaway worktree the tests ran from;
# deleting that worktree made every hook exit 2 (Claude Code's BLOCK signal) and
# denied every tool call in later, unrelated sessions.
#
# This is the assertion that would have caught it: content + mtime, before and
# after EVERY test, so the failure names the exact culprit instead of surfacing
# weeks later as an unexplained fail-closed harness.
#
# SCOPE. settings.json alone is not enough — measured, not assumed. The sweep
# that found the original 10 escaping suites showed only 3 rewrote settings.json;
# the other 7 wrote elsewhere under ~/.claude, including SIX hook scripts copied
# straight into the live install at ~/.claude/skills/ai-brain-starter/hooks/.
# A settings.json-only tripwire would have missed 70% of them. So it watches the
# paths a test can plausibly corrupt, and deliberately NOT the ones Claude Code
# itself churns during a session (projects/, logs/, todos/, shell-snapshots/,
# statsig/) — watching those would make the gate flaky and get it disabled,
# which is worse than not having it.
#
# The paths resolve through Python's Path.home() — the same resolution the
# installer uses — deliberately NOT through $HOME, because on Windows those two
# disagree and $HOME would watch the wrong home.
REAL_SETTINGS="$(python3 -c 'from pathlib import Path; print(Path.home() / ".claude" / "settings.json")')"
real_home_fingerprint() {
  python3 <<'PY'
import glob, hashlib, os
from pathlib import Path

claude = Path.home() / ".claude"
settings = claude / "settings.json"
parts = []

# The catastrophic one: full content + mtime.
try:
    parts.append(hashlib.sha256(settings.read_bytes()).hexdigest())
    parts.append("mtime=%r" % (settings.stat().st_mtime,))
except FileNotFoundError:
    parts.append("ABSENT")
# Installer backups: a run that wrote here and rolled back leaves content
# identical but the side effect on disk.
parts.append("baks=%d" % len(glob.glob(str(settings) + ".bak-*")))

# The live install's own code, deployed hooks, activated rules, and the
# SessionStart snapshot. size+mtime, not content: enough to catch a rewrite,
# fast enough to run around all ~100 tests.
WATCH_TREES = [
    claude / "skills" / "ai-brain-starter" / "hooks",
    claude / "skills" / "ai-brain-starter" / "scripts",
    claude / "hooks",
]
WATCH_GLOBS = [
    str(claude / "hookify.*.md"),
    str(claude / "settings.local.json"),
    # state/ as a TREE is gone (see below); this is the one durable artifact in
    # it worth protecting -- the SessionStart snapshot the paragraph above meant.
    str(claude / "state" / "sessionstart-hooks-snapshot.json"),
]

# WHAT THIS WATCHES, AND WHAT IT DELIBERATELY NO LONGER DOES (2026-08-05)
#
# The paragraph above names projects/, logs/, todos/, shell-snapshots/ and
# statsig/ as the churn to stay out of -- but that is a list of DIRECTORIES,
# and the churn was never confined to them. Two places leaked:
#
#   ~/.claude/hooks/  holds append-only logs (cwd-changed.log,
#       sync-my-skills.log, secret-detection-log.jsonl) and runtime lock dirs
#       (sync.*.lock/pid) sitting right beside the deployed hook CODE this
#       tripwire exists to protect.
#   ~/.claude/state/  is not "the SessionStart snapshot". Measured: 93 files,
#       78 of them per-session scratch keyed by session UUID
#       (branch-ticket-warn-<uuid>, linear-ids-seen-<uuid>), the rest last-run
#       stamps and append-only integrity streams. It is a scratch directory,
#       the same category as the five already excluded above.
#
# Measured at rest with no test running: hooks/cwd-changed.log,
# hooks/sync-my-skills.log, two sync.*.lock/pid files and
# state/settings-hook-integrity.jsonl all moved inside 30 seconds; a full
# instrumented run additionally caught state/linear-ids-seen-<uuid>.txt. The
# gate therefore failed on a DIFFERENT test every run, naming whichever test
# happened to be executing when a background job appended a line -- a pristine
# origin/main checkout failed identically. That is exactly the outcome the
# paragraph above warns against: "watching those would make the gate flaky and
# get it disabled, which is worse than not having it."
#
# state/ is excluded as a TREE rather than by picking off file kinds. Its
# churn set is open-ended -- every new hook that drops a dedup marker there
# would redden this gate again -- and a denylist against an open set always
# loses. The one durable artifact in it is allow-listed in WATCH_GLOBS above.
#
# COVERAGE IS UNCHANGED for every corruption class this gate was built for,
# each verified against a fake home so the real one is never touched:
#   trips    settings.json rewritten (the catastrophic 2026-07-30 class,
#            still compared by FULL CONTENT hash)
#   trips    hook script copied into the live install (.py)
#   trips    deployed .sh hook modified
#   trips    installer backup left behind (.bak-*)
#   trips    the SessionStart snapshot rewritten
#   ignores  append-only .log / .jsonl grows, lock dir churns, per-session
#            scratch appears, a hook runtime-state DOTFILE is rewritten
#
# DOTFILES, added 2026-08-13. ~/.claude/hooks/ is shared ground: this repo's
# deployed hooks sit there alongside whatever private tooling the operator has
# installed, and that tooling drops last-run stamps right beside them. Measured
# on one machine: 8 dotfiles, 7 of them pure runtime state --
# .mcp-config-secret-scan-last, .last-secret-scan, .last-secret-scan-full,
# .secret-scan-findings.json, .pre-push-doubt-heeding.stamp,
# .deployed-manifest.sha256, .secret-scan.lock. Only .gitignore is durable, and
# nothing this gate protects is a dotfile.
#
# CHURN_SUFFIXES cannot catch them: .stamp, .sha256, .json and a bare
# extensionless marker are four spellings of one behaviour, and the next hook
# adds a fifth. That is the open set this file's own comment warns about ("a
# denylist against an open set always loses"), so exclude by the PROPERTY that
# separates them: deployed hook code is never a dotfile. Every entry in
# install-hooks-user-level.py's HOME_HOOKS_INSTALLER_DEPLOYS and every
# ABS-owned basename is a named *.py / *.sh.
#
# Caught live: a background secret-scan hook rewrote
# hooks/.mcp-config-secret-scan-last 91 seconds into a gate run and reddened
# test_bootstrap_dry_run -- a test that touches neither file. Re-running that
# test alone, on that branch AND on a pristine origin/main, tripped nothing.
# That is precisely the "reddens an unrelated test and trains a bypass" outcome
# the quiet control below exists to prevent, arriving through a gap in the walk
# the quiet control does not inspect.
CHURN_SUFFIXES = (".log", ".jsonl")

seen = []
for tree in WATCH_TREES:
    if not tree.is_dir():
        seen.append(f"{tree.name}:ABSENT")
        continue
    for root, dirs, files in os.walk(tree):
        # Prune runtime lock dirs from the walk (sync.*.lock/pid is rewritten
        # per run), and keep the traversal order deterministic.
        dirs[:] = sorted(d for d in dirs if not d.endswith(".lock"))
        for name in sorted(files):
            fp = Path(root) / name
            if fp.suffix in CHURN_SUFFIXES:
                continue
            if name.startswith("."):
                continue  # hook runtime state, never deployed hook code
            try:
                st = fp.stat()
            except OSError:
                continue
            seen.append(f"{fp.relative_to(claude)}:{st.st_size}:{st.st_mtime!r}")
for pattern in WATCH_GLOBS:
    for match in sorted(glob.glob(pattern)):
        try:
            st = os.stat(match)
        except OSError:
            continue
        seen.append(f"{os.path.basename(match)}:{st.st_size}:{st.st_mtime!r}")

# MANIFEST mode names the paths the digest only summarises (MYC-3721 work item
# 1: "print WHAT changed, not just that something did"). Same walk, same trees,
# same exclusions -- deliberately one function, because a second implementation
# would drift from the digest it exists to explain, and would then name paths
# the gate never actually compared.
#
# Emitted as one sortable line per watched entry so callers can diff two
# manifests and print only what moved. The three settings.json components are
# spelled out rather than hashed together, so a content rewrite, a pure mtime
# touch and a stray .bak-* are told apart on sight.
if os.environ.get("FINGERPRINT_MANIFEST"):
    print("settings.json content=%s" % parts[0])
    for extra in parts[1:]:
        print("settings.json %s" % extra)
    for line in seen:
        print(line)
else:
    parts.append("tree=" + hashlib.sha256("\n".join(seen).encode()).hexdigest()[:16])
    parts.append("files=%d" % len(seen))
    print(" ".join(parts))
PY
}

# ---- QUIET control for the tripwire ----------------------------------------
# A guard needs TWO controls, and this repo had only ever written the first:
#
#   BITE   does it FIRE on the real thing?   (six such steps in lint.yml)
#   QUIET  does it stay SILENT at rest?      (this)
#
# Both of the 2026-08-05 defects lived in the missing one. The tripwire watched
# append-only logs and per-session scratch, so it reddened the gate on a
# DIFFERENT test every run while its bite control passed the whole time; a
# pristine origin/main checkout failed identically, which is what proved the
# noise was ambient rather than the diff. A guard that cries wolf gets bypassed,
# and the bypass becomes the habit -- so a noisy guard is a security problem,
# not a nuisance.
#
# Asserted STRUCTURALLY, not by timing. "Sample twice and compare" would be a
# sleep-dependent test that is itself flaky, and flaky is the disease. Instead:
# the watched set must contain nothing whose whole purpose is to be rewritten
# while the machine runs. That is deterministic, costs milliseconds, and goes
# red the moment someone re-adds a churning tree.
real_home_quiet_control() {
  # Resolved from THIS script's own location, never cwd: a control that
  # inspects the wrong file, or no file, must not be able to pass.
  CI_SH_PATH="${CI_SH_PATH:-$SCRIPT_DIR/ci.sh}" python3 <<'PY'
import os, re, sys
from pathlib import Path

# STRUCTURAL, not behavioural. The churn files legitimately EXIST in
# ~/.claude/hooks/ — that is normal and is not the bug. The bug is the
# fingerprint COLLECTING them. So assert the three exclusions that actually
# regressed, against this script's own source, which is where a regression
# lands. A behavioural walk here would either restate the exclusion (a
# tautology) or flag reality (a false alarm).
ci_sh = Path(os.environ.get("CI_SH_PATH", "scripts/ci.sh"))
if not ci_sh.is_file():
    print("::error::tripwire quiet-control cannot read %s — it would pass "
          "vacuously. A control that inspects nothing is worse than no "
          "control." % ci_sh)
    sys.exit(1)

body = ci_sh.read_text(encoding="utf-8", errors="replace")
fn = re.search(r"real_home_fingerprint\(\) \{(.*?)\n\}", body, re.S)
bad = []

if not fn:
    bad.append("real_home_fingerprint() not found — this control is looking at "
               "the wrong file and would pass vacuously")
else:
    src = fn.group(1)
    trees = re.search(r"WATCH_TREES = \[(.*?)\]", src, re.S)
    if trees and re.search(r'claude\s*/\s*"state"\s*,', trees.group(1)):
        bad.append('state/ is back in WATCH_TREES — ~78 of its ~93 files are '
                   'per-session scratch keyed by session UUID, so no suffix '
                   'rule can tame it; allow-list the one durable artifact')
    if "CHURN_SUFFIXES" not in src or "fp.suffix in CHURN_SUFFIXES" not in src:
        bad.append("the append-only (.log/.jsonl) exclusion is gone from the "
                   "fingerprint walk")
    if 'endswith(".lock")' not in src:
        bad.append("the runtime lock-dir pruning is gone from the walk")
    if 'name.startswith(".")' not in src:
        bad.append('the dotfile exclusion is gone from the walk — ~/.claude/'
                   'hooks/ is shared with the operator\'s private tooling, '
                   'which drops last-run stamps (.stamp/.sha256/.json/no '
                   'suffix) beside the deployed hooks; without this the gate '
                   'reddens on whichever test happens to be running')

if bad:
    print("::error::real-home tripwire quiet-control FAILED — the watched set "
          "is picking up churn again. It will redden this gate on an unrelated "
          "test and train a bypass. Exclude the CLASS, never pin the file:")
    for b in bad:
        print("::error::  - " + b)
    sys.exit(1)
print("    tripwire quiet-control: fingerprint still excludes append-only "
      "logs, lock dirs, runtime-state dotfiles and per-session scratch")
PY
}
# ---- The tripwire watches a DECOY home, not the shared one ------------------
#
# WHY THE PER-TEST CHECK MOVED OFF THE REAL ~/.claude (2026-08-15)
#
# Everything above is about WHAT to watch. This is about WHOSE home, and it is
# the half that kept failing. Fingerprinting the real ~/.claude before and after
# each test and blaming the test that was running is attribution by WALL CLOCK,
# not by causation. The real ~/.claude is shared ground: on a developer machine
# the operator's own tooling writes to it continuously and asynchronously, so
# whichever test happens to be executing when that lands is named as the
# culprit. Three such failures were captured inside 30 minutes, each naming a
# DIFFERENT innocent test:
#
#   blamed test_offmain_strand_guard   a launchd agent (StartInterval 900)
#                                      re-derived settings.json
#   blamed test_vault_safety_guards    a skill auto-sync ran the installer,
#                                      which rewrote settings.json + a .bak-*
#   blamed test_vault_root_read_guard  __pycache__/*.pyc rewritten by hooks
#                                      firing in a concurrent session
#
# The third is the tell: settings.json content hash, mtime, baks count AND file
# count were all IDENTICAL before and after — only tree= moved. No test did
# anything. The comment block above already documents this exact shape twice
# ("reddened test_bootstrap_dry_run — a test that touches neither file"), and
# each time the answer was to widen the exclusions. That approach cannot finish:
# the loudest remaining writers rewrite settings.json ITSELF, which is the one
# file this gate exists to watch and can never exclude.
#
# So stop watching a resource other processes write. Point HOME and USERPROFILE
# at a throwaway decoy for the duration of each test, and fingerprint THAT. The
# decoy is private to one test, so nothing else on the machine can move it and a
# mismatch is CAUSAL — no timing, no sleeps, no re-runs, no denylist.
#
# This also upgrades the gate from detection to PREVENTION. Previously a test
# that escaped its sandbox really did corrupt the developer's live config and
# the gate told you afterwards. Now that write lands in a tmpdir that is deleted
# seconds later, and the gate still names the test.
#
# Coverage is unchanged: the same real_home_fingerprint() runs, with the same
# watched trees, globs and exclusions, so real_home_quiet_control and
# test_home_fingerprint_noise.sh keep asserting exactly what they always did.
# Only Path.home() resolves somewhere safe.
#
# Verified before switching, against the whole suite: every test still passes
# under a decoy home; ZERO change their assertion count (so nothing starts
# passing vacuously because the deployed install is absent); and no test writes
# any WATCHED path of the decoy — the only ~/.claude writes at all are three
# tests creating empty dirs under projects/, which is already excluded.
# (Measured at 106 tests, then re-swept at 111 once the suite grew; the sole
# failure in the re-sweep was the pre-existing red the parent commit fixes.)
#
# The real home is still measured once around the whole suite, but ADVISORY:
# with tests unable to reach it through "~", a change there is ambient by
# construction, and failing on it is the bug this section fixes.
_SANDBOX_LIB="$SCRIPT_DIR/../tests/integration/lib/sandbox_home.sh"
if [ ! -f "$_SANDBOX_LIB" ]; then
  # Fail loud. Without it there is no decoy, and every check below would compare
  # an untouched empty dir against itself and pass vacuously.
  echo "::error::missing $_SANDBOX_LIB — the integration tripwire cannot sandbox HOME without it, and would pass vacuously"
  exit 1
fi
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$_SANDBOX_LIB"

# Fingerprint a decoy home with the real function. The subshell keeps the
# sandbox exports from leaking into the gate itself.
decoy_fingerprint() { ( sandbox_home "$1" >/dev/null; real_home_fingerprint ); }

# The same two walks in MANIFEST mode, naming paths instead of digesting them.
# Both run only on a mismatch, so they cost nothing at rest.
decoy_manifest()     { ( sandbox_home "$1" >/dev/null; FINGERPRINT_MANIFEST=1 real_home_fingerprint ); }
real_home_manifest() { FINGERPRINT_MANIFEST=1 real_home_fingerprint; }

# Print only the manifest lines that differ, capped so a large drift cannot bury
# the failure it is explaining.
_manifest_delta() {
  local before="$1" after="$2" delta n
  # diff exits 1 whenever the two differ, which is the only case this is called
  # in; set -e must not read that as an error.
  delta="$(diff <(printf '%s\n' "$before") <(printf '%s\n' "$after") || true)"
  delta="$(printf '%s\n' "$delta" | sed -n -e 's/^> /      + /p' -e 's/^< /      - /p')"
  [ -n "$delta" ] || { echo "      (no path-level delta — the change was in a component the manifest folds, e.g. settings.json mtime)"; return 0; }
  n="$(printf '%s\n' "$delta" | wc -l | tr -d ' ')"
  # `sed -n 1,20p`, never `head -20`: head closes the pipe at line 20, printf
  # takes SIGPIPE, and under this script's `set -o pipefail` that aborts the
  # whole gate. On the ADVISORY path that would kill a run over ambient churn —
  # precisely the misattribution this section exists to prevent. sed reads to
  # EOF, so it cannot SIGPIPE.
  printf '%s\n' "$delta" | sed -n '1,20p'
  [ "$n" -gt 20 ] && echo "      ... and $((n - 20)) more"
  return 0
}

# Name the exact paths a test wrote into its decoy.
#
# The decoy's "before" state is RECONSTRUCTED rather than guessed: sandbox_home
# only creates an empty <dir>/.claude, so a freshly-made decoy is byte-identical
# to how this one started. Manifest lines are relative to ~/.claude (never the
# tmpdir path) and an absent tree emits a bare "name:ABSENT" carrying no mtime,
# so the two are directly comparable. That makes this a real diff, not a guess.
decoy_written_paths() {
  local fresh
  fresh="$(mktemp -d)"
  _manifest_delta "$(decoy_manifest "$fresh")" "$(decoy_manifest "$1")"
  rm -rf "$fresh"
}

# BITE control for the decoy tripwire. The quiet control above proves the
# fingerprint stays silent at rest; this proves it still SPEAKS. It runs a
# synthetic test through the SAME run_sandboxed wrapper the real tests use, so
# it exercises the wrapper too — a wrapper that failed to redirect HOME would
# make every check below compare an empty decoy against itself and pass.
decoy_tripwire_bite_control() {
  local d before after
  d="$(mktemp -d)"
  before="$(decoy_fingerprint "$d")"
  run_sandboxed "$d" bash -c 'mkdir -p "$HOME/.claude/hooks" && printf "print(1)\n" > "$HOME/.claude/hooks/escaped.py"'
  after="$(decoy_fingerprint "$d")"
  rm -rf "$d"
  if [ "$before" = "$after" ]; then
    echo "::error::decoy tripwire BITE control FAILED — a synthetic test wrote \$HOME/.claude/hooks/escaped.py and the tripwire did not see it. Every per-test check below is inert; a green run proves nothing."
    return 1
  fi
  echo "    tripwire bite control: a test writing \$HOME/.claude/hooks/ is still caught"
  return 0
}

_home_before_suite="$(real_home_fingerprint)"
# One extra walk, once, so the advisory below can name paths instead of printing
# two opaque digests. An advisory nobody can act on becomes background noise,
# and this one is the ONLY remaining detector for a test that writes by absolute
# path — the single escape a decoy home cannot intercept.
_home_before_suite_manifest="$(real_home_manifest)"

echo "==> (b) Shell integration: ${#INTEGRATION_TESTS[@]} tests"
echo "    tripwire: each test runs against a throwaway decoy home (settings.json, installed skill, deployed hooks; state/ only its SessionStart snapshot)"
real_home_quiet_control || exit 1
decoy_tripwire_bite_control || exit 1
for t in "${INTEGRATION_TESTS[@]}"; do
  script="tests/integration/$t.sh"
  if [ ! -f "$script" ]; then
    echo "::error::missing integration test $script"
    exit 1
  fi
  echo "--- $t"
  _decoy="$(mktemp -d)"
  _home_before="$(decoy_fingerprint "$_decoy")"
  # NOT inside an `if`: set -e must still abort the gate when a test fails.
  # Wrapping this in a condition would silently disable that.
  run_sandboxed "$_decoy" bash "$script"
  _home_after="$(decoy_fingerprint "$_decoy")"
  if [ "$_home_before" != "$_home_after" ]; then
    echo "::error::$t wrote into ~/.claude — the suite must sandbox HOME *and* USERPROFILE (source tests/integration/lib/sandbox_home.sh and use sandbox_home/run_sandboxed). It was already running under a decoy home, so the developer's real config is intact and this is the test's own write."
    echo "::error::  it wrote these paths (relative to ~/.claude):"
    decoy_written_paths "$_decoy"
    echo "      digests: before=[$_home_before] after=[$_home_after]"
    rm -rf "$_decoy"
    exit 1
  fi
  rm -rf "$_decoy"
done
# The real home, measured once around the whole suite. ADVISORY on purpose: the
# tests just ran against a decoy, so they could not reach this through "~", and
# anything that moved here came from outside the suite. Reported rather than
# swallowed, because silence would hide a test that writes by ABSOLUTE path —
# the one escape a decoy cannot intercept.
_home_after_suite="$(real_home_fingerprint)"
if [ "$_home_before_suite" != "$_home_after_suite" ]; then
  echo "    note: the real $(dirname "$REAL_SETTINGS") changed while the suite ran. The tests ran against a decoy home, so this is an out-of-band writer on this machine (a settings assembler on a timer, a skill auto-sync running the installer, __pycache__ churn from a concurrent session), not the suite. Not failing the gate on it — that misattribution is what this section exists to prevent."
  echo "    what moved (read it: an entry under skills/ai-brain-starter/ or a settings.json content= change is NOT ordinary churn, and would mean a test wrote by ABSOLUTE path, which a decoy home cannot intercept):"
  _manifest_delta "$_home_before_suite_manifest" "$(real_home_manifest)"
fi

# ---- (c) Shell static analysis gate ----------------------------------------
# Runs the SAME canonical gate as lint.yml's `shellcheck` job - scripts/shellcheck.sh
# - so the local pre-push gate (~/.local/bin/ci-test) and CI cannot drift on shell
# quality. In CI we skip it here because the dedicated `shellcheck` job already runs
# scripts/shellcheck.sh; running it again in this job would only duplicate work and
# muddy failure attribution. Locally there is no such job, so this is where the
# pre-push gate gets shellcheck coverage. If shellcheck is not installed locally we
# warn and skip (CI still enforces it) rather than blocking the python + integration
# gates a contributor can still run.
if [ -n "${GITHUB_ACTIONS:-}" ]; then
  shellcheck_note="skipped in CI (dedicated lint.yml 'shellcheck' job runs scripts/shellcheck.sh)"
  echo "==> (c) shellcheck: $shellcheck_note"
elif command -v shellcheck >/dev/null 2>&1; then
  echo "==> (c) shellcheck: bash scripts/shellcheck.sh"
  bash scripts/shellcheck.sh
  shellcheck_note="passed"
else
  shellcheck_note="skipped (shellcheck not installed locally; CI enforces it)"
  echo "==> (c) shellcheck: $shellcheck_note"
  echo "    install: brew install shellcheck  (macOS)  /  sudo apt-get install -y shellcheck  (Debian/Ubuntu)"
fi

# ---- (d) Phase-doc Python undefined-name gate ------------------------------
# The Phase 2 plugin installer and Phase 10a graph-config block are python3
# heredocs / ```python fences inside Markdown, so gate (a) py_compile never sees
# them - and py_compile cannot catch an undefined name (a runtime NameError)
# anyway. A bare `VAULT_DIR` typo shipped and crashed the installer on every
# platform (2026-07-07). Mirrors the shellcheck arrangement: a dedicated lint.yml
# 'phase-python' job is authoritative in CI (installs ruff, fails closed); here it
# is local best-effort, warn-skipped when no linter is installed (CI enforces it).
if [ -n "${GITHUB_ACTIONS:-}" ]; then
  phasepy_note="skipped in CI (dedicated lint.yml 'phase-python' job runs scripts/check-phase-python.py)"
  echo "==> (d) phase-doc python: $phasepy_note"
elif command -v ruff >/dev/null 2>&1 || "$PY" -c 'import pyflakes' >/dev/null 2>&1; then
  echo "==> (d) phase-doc python: $PY scripts/check-phase-python.py"
  "$PY" scripts/check-phase-python.py
  phasepy_note="passed"
else
  phasepy_note="skipped (no ruff/pyflakes locally; CI enforces it)"
  echo "==> (d) phase-doc python: $phasepy_note"
  echo "    install: pip install ruff"
fi

# ---- (e) UTF-8 console guard -----------------------------------------------
# scripts/check-utf8-stdout.py fails a runnable scripts/*.py or hooks/*.py CLI
# that print()s non-ASCII without the UTF-8 stdout/stderr reconfigure guard - the
# Windows cp1252 crash class (ai-brain-starter#313: a non-ASCII print raised
# UnicodeEncodeError, the caller captured an empty string, and read it as "no
# Meta folder"). In a HOOK the same crash is worse than in a script: a hook gates
# the tool call, so it either fails silently open or denies every Write with no
# legible cause (#375, #409). hooks/ was outside this gate's scan until MYC-3530.
# Mirrors how
# the shell static-analysis gate is wired: a dedicated lint.yml 'utf8-console-guard'
# job is authoritative in CI; here it runs locally so the pre-push gate catches the crash
# class before a Windows console does. Pure stdlib - no external linter to skip on,
# so unlike (c)/(d) it always runs locally (and is skipped in CI where the
# dedicated job owns it, to keep failure attribution clean).
if [ -n "${GITHUB_ACTIONS:-}" ]; then
  utf8_note="skipped in CI (dedicated lint.yml 'utf8-console-guard' job runs scripts/check-utf8-stdout.py)"
  echo "==> (e) utf8 console guard: $utf8_note"
else
  echo "==> (e) utf8 console guard: $PY scripts/check-utf8-stdout.py"
  "$PY" scripts/check-utf8-stdout.py
  utf8_note="passed"
fi

# ---- (e2) Hook block-protocol ----------------------------------------------
# scripts/check-hook-block-protocol.py fails a hook that is registered with the
# allow-fallback wrapper (`... || echo '{...permissionDecision:allow}'`) but
# blocks by exiting non-zero. That `||` fires on ANY non-zero exit, so such a
# hook is rewritten into an ALLOW: present, registered, auditable, and unable to
# block anything. Caught live on validate-handoff-frontmatter.py (#375) -- the
# WIRED-BUT-NEUTERED sibling of ARTIFACT-WITHOUT-ACTIVATION. Pure stdlib, so it
# always runs here.
echo "==> (e2) hook block-protocol: $PY scripts/check-hook-block-protocol.py"
"$PY" scripts/check-hook-block-protocol.py

# ---- (e2b) Hook activation --------------------------------------------------
# scripts/check-hook-activation.py fails a hook that ships in hooks/ but is wired
# in NO activation channel (dormant on every install), and a hook we DO wire that
# the installer does not own (a re-install duplicates a hand-wired copy instead of
# replacing it, and verify_paths_on_disk() skips it). Three guards shipped dormant
# before this existed: MYC-1017, MYC-1031, and MYC-782 / #371.
# The ARTIFACT-WITHOUT-ACTIVATION half of the family whose WIRED-BUT-NEUTERED half
# is (e2) and whose wired-but-never-deployed half is (e4). Pure stdlib.
echo "==> (e2b) hook activation: $PY scripts/check-hook-activation.py"
"$PY" scripts/check-hook-activation.py --selftest >/dev/null
"$PY" scripts/check-hook-activation.py

# ---- (e3) Naive VAULT_ROOT reads -------------------------------------------
# scripts/check-vault-root-reads.py fails code that reads the VAULT_ROOT env var
# outside a sanctioned resolver. A globally-exported VAULT_ROOT names ONE vault,
# so a naive read fails two ways and both are silent: UNSET it defaults to
# ~/vault (inert on every vault not literally named "vault"), SET it overrides
# the vault the caller actually meant. hooks/validate-handoff-frontmatter.py
# shipped that way and was inert on every install (#375/#404) -- the same
# SILENT-NO-OP family as (e) and (e2). Twin of the naive *Meta glob ban
# (scripts/check-meta-resolution.sh, a lint.yml step). The pre-existing
# population is byte-pinned in scripts/vault-root-read-baseline.txt, so a NEW or
# EDITED file fails while the backlog burns down. Pure stdlib, always runs here.
echo "==> (e3) vault-root reads: $PY scripts/check-vault-root-reads.py"
"$PY" scripts/check-vault-root-reads.py

# ---- (e4) Home-hook deploy routes ------------------------------------------
# scripts/check-home-hook-deploy.py fails when hooks.json wires a hook from
# ~/.claude/hooks/ that nothing ever copies there. hooks.json guards those
# commands with `[ -f <path> ] &&`, which is right at runtime and fatal at
# install time: a never-deployed hook is indistinguishable from one the user
# turned off. pre-write-settings-lint.py, lint-claude-settings.py and
# check-claude-code-version.sh shipped that way -- wired in 11 places on a real
# settings.json, present on disk 0 times, and reported OK by the installer's own
# verification. Same SILENT-NO-OP family as (e), (e2) and (e3). Pure stdlib.
echo "==> (e4) home-hook deploy: $PY scripts/check-home-hook-deploy.py"
"$PY" scripts/check-home-hook-deploy.py

# ---- (e5) Locale-decoded subprocess output ---------------------------------
# The READ half of (e). scripts/check-utf8-subprocess.py fails a subprocess call
# that decodes a child's output with the LOCALE encoding instead of an explicit
# one. On a Spanish/French Windows that is cp1252, and any vault path carries the
# gear emoji whose 0x8F byte is unmapped there -- subprocess.run() raises before
# the caller sees a byte. Self-test first (proves the gate still bites), then the
# fleet against the content-pinned baseline. Pure stdlib, always runs here.
echo "==> (e5) subprocess decode: $PY scripts/check-utf8-subprocess.py"
"$PY" scripts/check-utf8-subprocess.py --self-test >/dev/null
"$PY" scripts/check-utf8-subprocess.py

# ---- (f) Python unit tests (scripts/ + hooks/ + tests/) --------------------
# Every Python unit suite in the repo, run under the SAME interpreter as the rest
# of the gate. Gate (a) py_compiles them (proves they parse); this proves their
# asserts RUN. scripts/test_*.py is a pure GLOB (a new one runs automatically, can
# never sit dormant - the false-green class MYC-2922 closes); hooks/+tests/ suites
# split between integration-wrapper-driven and direct-run, guarded by a coverage
# invariant below (MYC-2959). No dedicated lint.yml job, so (unlike c/d/e) it runs
# in CI too. `set -e` aborts on the first failing suite (stop-on-first-failure).
echo "==> (f) Python unit tests: scripts/test_*.py  [$("$PY" --version 2>&1)]"
unit_count=0
while IFS= read -r -d '' t; do
  unit_count=$((unit_count + 1))
  echo "--- $t"
  "$PY" "$t"
done < <(git ls-files -z -- 'scripts/test_*.py')
if [ "$unit_count" -eq 0 ]; then
  echo "::error::no scripts/test_*.py matched - the unit-test glob is empty (did the suites move or get renamed?)"
  exit 1
fi
echo "    OK - $unit_count scripts/ unit suite(s) passed"

# hooks/ + tests/ Python suites: unlike scripts/ (a pure glob), these split into two
# run paths - some are driven by a tests/integration/*.sh wrapper (already in the
# section (b) allow-list), the rest must run directly here. So a bare glob would
# DOUBLE-run the wrapped ones. Instead a coverage invariant (mirrors the section (b)
# allow-list invariant, L203-224) asserts every hooks/test_*.py + tests/test_*.py is
# EITHER wrapper-referenced OR in PY_DIRECT below; a suite in neither is dormant and
# fails the gate LOUD (the false-green class MYC-2922 closed for scripts/, MYC-2959
# for hooks/+tests/). PY_DIRECT then runs the non-wrapped suites exactly once.
PY_DIRECT=(
  hooks/test_memory_index.py
  tests/test_instinct.py
  hooks/test_live_session_reap.py
  hooks/test_relocation_orphan_reclaim.py
  hooks/test_secret_patterns_fp_filter.py
  hooks/test_check_fabricated_verification.py
  hooks/test_warn_chained_state_command.py
  hooks/test_footprint_aggregate_bloat.py
  hooks/test_footprint_disk_floor.py
  hooks/test_unpushed_drift_surface.py
  hooks/test_claim_surface_honesty.py
  hooks/test_narrow_refspec_falsealarm.py
  # auto-capture-public-ships shipped `ZoneInfo("America/user-local-tz")`, an
  # unsubstituted placeholder that raised at IMPORT, so the SessionEnd hook
  # exited 1 on every machine and captured nothing for its entire life. Nothing
  # ran the hook, so nothing noticed. Also carries the CLASS guard: no hook may
  # resolve a zone at module level.
  hooks/test_auto_capture_ships_tz.py
  hooks/test_git_inflight_op_guard.py
  # The generalisation of the line above (MYC-3537). Two hooks were dead on
  # arrival for their whole lives because NOTHING executed them: the tz
  # placeholder, and `import fcntl` at module scope in the SessionStart secret
  # scanner -- POSIX-only, so it crashed at import on every Windows install.
  # Runs every hook on a minimal payload AND statically bans an unguarded
  # platform-only import, because a Linux runner structurally cannot observe a
  # Windows-only import crash.
  hooks/test_hook_smoke.py
)
dormant_py=()
while IFS= read -r -d '' f; do
  base="$(basename "$f")"
  # covered by a tests/integration/*.sh wrapper (proxy: the wrapper names the file)?
  if grep -qF -- "$base" tests/integration/*.sh 2>/dev/null; then continue; fi
  in_direct=0
  for d in "${PY_DIRECT[@]}"; do [ "$d" = "$f" ] && { in_direct=1; break; }; done
  [ "$in_direct" -eq 1 ] && continue
  dormant_py+=("$f")
done < <(git ls-files -z -- 'hooks/test_*.py' 'tests/test_*.py')
if [ "${#dormant_py[@]}" -gt 0 ]; then
  echo "::error::dormant Python test suite(s) — none runs in any CI job. Run each via a tests/integration/*.sh wrapper or add it to PY_DIRECT in scripts/ci.sh: ${dormant_py[*]}"
  exit 1
fi
echo "==> (f cont.) hooks/ + tests/ direct-run suites: ${#PY_DIRECT[@]}"
for t in "${PY_DIRECT[@]}"; do
  if [ ! -f "$t" ]; then
    echo "::error::PY_DIRECT names a missing suite (moved/renamed?): $t"
    exit 1
  fi
  echo "--- $t"
  "$PY" "$t"
done
echo "    OK - ${#PY_DIRECT[@]} hooks/+tests/ direct suite(s) passed; dormancy invariant clean"

echo
echo "All gates passed: py_compile ($count file(s)) + ${#INTEGRATION_TESTS[@]} integration tests + $unit_count scripts/ + ${#PY_DIRECT[@]} hooks/tests unit suite(s) + shellcheck [$shellcheck_note] + phase-doc python [$phasepy_note] + utf8 console guard [$utf8_note] + hook block-protocol [passed] + vault-root reads [passed] + home-hook deploy [passed] + subprocess decode [passed]."
