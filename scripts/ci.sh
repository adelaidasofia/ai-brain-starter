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
#   (e) UTF-8 console guard - scripts/check-utf8-stdout.py fails a runnable vault
#                           CLI that print()s non-ASCII (the "gear Meta" emoji, an
#                           em dash, an accented name) without reconfiguring
#                           stdout/stderr to UTF-8. On a Windows cp1252 console
#                           that print() raises UnicodeEncodeError and the caller
#                           reads the empty output as failure (ai-brain-starter#313).
#                           A dedicated lint.yml 'utf8-console-guard' job is
#                           authoritative in CI; here it runs locally (pure stdlib).
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
  test_windows_platformize
  test_memory_routing_guard
  test_bootstrap_omits_vault_hooks
  test_bootstrap_brew_terminal_step
  test_bootstrap_optional_packs_soft_fail
  test_bootstrap_corporate_profile
  test_remediate_runaway_procs
  test_scan_prior_single_instance
  test_scan_prior_failclosed_scrub
  test_sessionstart_freeze_class_excluded
  test_sessionstart_boundedness
  test_orphan_branch_bounded_git
  test_footprint_sla
  test_vault_safety_guards
  test_vault_backup_conf_bom
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

echo "==> (b) Shell integration: ${#INTEGRATION_TESTS[@]} tests"
for t in "${INTEGRATION_TESTS[@]}"; do
  script="tests/integration/$t.sh"
  if [ ! -f "$script" ]; then
    echo "::error::missing integration test $script"
    exit 1
  fi
  echo "--- $t"
  bash "$script"
done

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
# scripts/check-utf8-stdout.py fails a runnable vault CLI that print()s non-ASCII
# without the UTF-8 stdout/stderr reconfigure guard - the Windows cp1252 crash
# class (ai-brain-starter#313: a non-ASCII print raised UnicodeEncodeError, the
# caller captured an empty string, and read it as "no Meta folder"). Mirrors how
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
  tests/test_instinct.py
  hooks/test_live_session_reap.py
  hooks/test_relocation_orphan_reclaim.py
  hooks/test_secret_patterns_fp_filter.py
  hooks/test_check_fabricated_verification.py
  hooks/test_warn_chained_state_command.py
  hooks/test_footprint_aggregate_bloat.py
  hooks/test_unpushed_drift_surface.py
  hooks/test_claim_surface_honesty.py
  hooks/test_narrow_refspec_falsealarm.py
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
echo "All gates passed: py_compile ($count file(s)) + ${#INTEGRATION_TESTS[@]} integration tests + $unit_count scripts/ + ${#PY_DIRECT[@]} hooks/tests unit suite(s) + shellcheck [$shellcheck_note] + phase-doc python [$phasepy_note] + utf8 console guard [$utf8_note] + hook block-protocol [passed]."
