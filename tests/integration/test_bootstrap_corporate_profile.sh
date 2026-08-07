#!/usr/bin/env bash
# Test the corporate / hardened install profile in bootstrap.sh.
#
# Surfaced by an enterprise security review of the workshop install: corporate
# rollouts need a compliance-ready profile. This test pins that contract so a
# future refactor of bootstrap.sh can't silently un-harden the corporate path.
#
# Asserts, via --dry-run (no real install, no network):
#   1. `--profile corporate --dry-run` exits 0
#   2. The hardening markers appear: banner, manifest, telemetry-off enforcement,
#      external-egress MCP skip, playwright exclusion, the PHANTOMPULSE rationale
#   3. The minimal first-party skill set still installs (graphify present)
#   4. The env-var form CORPORATE_PROFILE=1 activates the same path
#   5. NEGATIVE CONTROL: standard mode is unchanged — no corporate banner, MCPs
#      registered, playwright enabled. (A guard earns trust by failing on the
#      thing it catches: if corporate logic leaked into standard mode, this fails.)
#
# Self-contained. EACH bootstrap invocation runs in its OWN fresh $HOME (a tmpdir
# with the repo symlinked into SKILL_DIR + the email marker pre-staged), exactly
# like test_bootstrap_dry_run. Isolating per-run is deliberate: the bootstrap
# writes some state even under --dry-run (e.g. ~/.claude/commands, ~/.claude/
# .bootstrap.log), and sharing one HOME across invocations couples the runs.
# Writes nothing outside its tmpdirs.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
BOOTSTRAP="$REPO_ROOT/bootstrap.sh"

if [ ! -f "$BOOTSTRAP" ]; then
  echo "ERROR: $BOOTSTRAP not found" >&2
  exit 1
fi

HOMES=()
# return 0 so a failed `rm` (or empty-array iteration) never becomes the
# script's exit status via the EXIT trap.
cleanup() { local h; for h in "${HOMES[@]:-}"; do [ -n "$h" ] && rm -rf "$h"; done; return 0; }
trap cleanup EXIT

fail=0

# run_bootstrap <out_file> <extra_env e.g. "CORPORATE_PROFILE=1" or ""> <args...>
# Runs bootstrap in its OWN fresh $HOME (a subshell exports it). Sets $LAST_CODE.
# NOTE: the tmpdir is created + appended to HOMES in THIS (parent) shell — doing
# it inside a $(...) helper would lose the array append to the subshell.
LAST_CODE=0
run_bootstrap() {
  local out="$1"; shift
  local extra_env="$1"; shift
  local h; h="$(mktemp -d)"
  HOMES+=("$h")
  mkdir -p "$h/.claude/skills"
  ln -s "$REPO_ROOT" "$h/.claude/skills/ai-brain-starter"
  touch "$h/.claude/.ai-brain-starter-email-on-file"
  set +e
  # BOOTSTRAP_LANG pins detect_lang()'s output — the variable it actually
  # reads (BOOTSTRAP_LANG > LC_ALL > LANG > AppleLocale). LANG_HINT does NOT
  # reach it (it only feeds QM_LANG, the quick-mint API payload), so on a
  # Spanish-locale Mac the t()-translated lines came out in Spanish and the
  # English greps below failed. Green on linux CI (no AppleLocale fallback),
  # red on any es_* Mac.
  ( sandbox_home "$h"
    # shellcheck disable=SC2086
    env $extra_env EMAIL="ci@example.com" NAME="CI Test" LANG_HINT="en" BOOTSTRAP_LANG="en" \
      bash "$BOOTSTRAP" "$@" ) > "$out" 2>&1
  LAST_CODE=$?
  set -e
}

assert_grep()   { if grep -qF "$2" "$1"; then echo "PASS: $3"; else echo "FAIL: $3 (pattern not found: $2)" >&2; fail=1; fi; }
assert_absent() { if grep -qF "$2" "$1"; then echo "FAIL: $3 (pattern unexpectedly present: $2)" >&2; fail=1; else echo "PASS: $3"; fi; }

# ─── 1. Corporate via --profile corporate (own fresh HOME) ─────────────────
CORP_OUT="$(mktemp)"
run_bootstrap "$CORP_OUT" "" --profile corporate --dry-run
if [ "$LAST_CODE" -eq 0 ]; then
  echo "PASS: --profile corporate --dry-run exited 0"
else
  echo "FAIL: --profile corporate --dry-run exited $LAST_CODE" >&2
  tail -30 "$CORP_OUT" >&2
  fail=1
fi

assert_grep   "$CORP_OUT" "CORPORATE / HARDENED PROFILE ACTIVE" "corporate banner shown"
assert_grep   "$CORP_OUT" "Corporate component manifest"        "component manifest emitted"
assert_grep   "$CORP_OUT" "skipping external-egress MCPs"       "granola/chatprd MCPs skipped"
assert_grep   "$CORP_OUT" "playwright EXCLUDED"                 "playwright excluded from minimal set"
assert_grep   "$CORP_OUT" "would ENFORCE telemetry-off"        "telemetry-off env enforced"
assert_grep   "$CORP_OUT" "DISABLE_TELEMETRY"                  "manifest names telemetry env vars"
assert_grep   "$CORP_OUT" "REF6598 / PHANTOMPULSE"             "shell-exec-plugin threat cited"
assert_grep   "$CORP_OUT" ".ai-brain-starter-pinned"           "version-pin sentinel named"
assert_grep   "$CORP_OUT" "graphify"                           "minimal first-party skill set still present"
assert_absent "$CORP_OUT" "would register granola + chatprd"   "no external MCP registration in corporate mode"

# ─── 2. Corporate via CORPORATE_PROFILE=1 env var (own fresh HOME) ─────────
ENV_OUT="$(mktemp)"
run_bootstrap "$ENV_OUT" "CORPORATE_PROFILE=1" --dry-run
if [ "$LAST_CODE" -eq 0 ]; then
  echo "PASS: CORPORATE_PROFILE=1 --dry-run exited 0"
else
  echo "FAIL: CORPORATE_PROFILE=1 --dry-run exited $LAST_CODE" >&2
  tail -30 "$ENV_OUT" >&2
  fail=1
fi
assert_grep "$ENV_OUT" "CORPORATE / HARDENED PROFILE ACTIVE" "env-var form activates corporate profile"

# ─── 3. NEGATIVE CONTROL: standard mode is unchanged (own fresh HOME) ──────
STD_OUT="$(mktemp)"
run_bootstrap "$STD_OUT" "" --dry-run
if [ "$LAST_CODE" -eq 0 ]; then
  echo "PASS: standard --dry-run exited 0"
else
  echo "FAIL: standard --dry-run exited $LAST_CODE" >&2
  tail -30 "$STD_OUT" >&2
  fail=1
fi
assert_absent "$STD_OUT" "CORPORATE / HARDENED PROFILE ACTIVE" "no corporate banner in standard mode"
assert_absent "$STD_OUT" "Corporate component manifest"        "no manifest in standard mode"
assert_grep   "$STD_OUT" "would register granola + chatprd"    "standard mode registers MCPs"
assert_grep   "$STD_OUT" "obsidian, context7, playwright"      "standard mode enables playwright"

echo
if [ "$fail" -eq 0 ]; then
  echo "All assertions passed. Corporate profile is hardened and standard mode is intact."
else
  echo "One or more corporate-profile assertions failed." >&2
  exit 1
fi
