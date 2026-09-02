#!/usr/bin/env bash
# Test that a brew-less, non-interactive, NON-corporate Mac REACHES the
# user-space Python/Node installers instead of exiting early (MYC-4285).
#
# Bug (MYC-739 regression, found 2026-09-01): a normal Mac with no Homebrew,
# run the way real users run it -- the install prompt pasted into Claude
# Code, so stdin is not a TTY -- hit `exit 0` right after print_terminal_step()
# in the Homebrew section and NEVER reached the user-space Python/Node
# fallbacks a few sections down, even though those fallbacks exist and work
# (test_bootstrap_userspace_fallback.sh proves that). Only `--profile
# corporate` reached them. The corporate branch already had the right shape:
# skip brew, note_gap the components that need it, keep going. This test
# pins that same shape for the non-interactive/non-corporate case.
#
# WHY A SEPARATE TEST FROM test_bootstrap_userspace_fallback.sh: that suite
# extracts the Python/Node sections ALONE and runs them in isolation, so it
# proves the installers WORK but never that they are REACHED -- it never goes
# through the Homebrew section's TTY gate at all. This test extracts the
# REAL Homebrew decision block (with its TTY/corporate/dry-run branches)
# immediately followed by the REAL Python/Node blocks, in the same order
# bootstrap.sh runs them, so it proves the gate in front of the installers
# lets a brew-less non-interactive Mac through instead of stopping short.
#
# SCOPE: this is a narrow gate on one thing -- non-early-exit and reaching
# the two call sites. It does not re-verify installer internals (covered by
# test_bootstrap_userspace_fallback.sh), the corporate profile (covered by
# test_bootstrap_corporate_profile.sh), or the interactive/dry-run Homebrew
# branches (covered structurally by test_bootstrap_brew_terminal_step.sh,
# which this fix leaves untouched -- same anchors, same assertions, still
# green). A full non-dry-run end-to-end install on real macOS + Linux, both
# profiles, with hooks-wired and MCP-registration assertions, is MYC-2392 and
# stays open; not attempted here.
#
# HERMETICITY NOTE: this machine class may have a REAL Homebrew installed at
# /opt/homebrew/bin/brew or /usr/local/bin/brew. bootstrap.sh's own
# "already-installed brew" PATH-sourcing loop (the `for _brew in
# /opt/homebrew/bin/brew ...` block right above the decision block this test
# extracts) checks those ABSOLUTE paths directly, bypassing $PATH entirely --
# so running it here would `eval` a REAL `brew shellenv` and defeat the
# brew-absent scenario regardless of PATH stubbing. This test deliberately
# extracts and runs ONLY the decision block that follows it, never that
# sourcing loop, so `have brew` (which the decision block actually branches
# on) stays governed by the stubbed $PATH on every machine, brew installed
# or not. uname is still stubbed to force darwin/arm64, matching
# test_bootstrap_userspace_fallback.sh, so the scenario is reachable
# regardless of the real runner OS (CI runs this suite on Linux).
#
# Functions and control-flow blocks are extracted from the real bootstrap.sh
# with awk (never reimplemented), same technique as
# test_bootstrap_userspace_fallback.sh and test_bootstrap_archive_entry.sh.
#
# NEGATIVE CONTROL: the identical harness runs against a frozen, vendored
# snapshot of bootstrap.sh as it stood at commit e9f80e0 (the confirmed base
# this fix branched from) -- never `git show origin/main:bootstrap.sh`, which
# is a MOVING ref that becomes the FIXED code the moment this fix merges and
# would make the control compare the fix against itself forever after
# (documented at length in test_bootstrap_userspace_fallback.sh's own check 5,
# the same lesson applied here from the start).
#
# Self-contained; no network; never writes outside its own tmpdir.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
BOOTSTRAP="$REPO_ROOT/bootstrap.sh"
FIXTURE="$REPO_ROOT/tests/fixtures/bootstrap-prefix-e9f80e0.sh.txt"
[ -f "$BOOTSTRAP" ] || { echo "ERROR: $BOOTSTRAP not found" >&2; exit 1; }
[ -s "$FIXTURE" ]   || { echo "ERROR: $FIXTURE missing or empty -- without a 'before' source the negative control cannot run, and a control that cannot run must not report success" >&2; exit 1; }

fail() { echo "FAIL: $1" >&2; exit 1; }

TMP="$(mktemp -d)"
sandbox_home "$TMP/realhome-guard"   # belt-and-braces; nothing here should ever touch it
trap 'rm -rf "$TMP"' EXIT

# ── syntax ──
bash -n "$BOOTSTRAP" || fail "0: bootstrap.sh has a syntax error"

# ── extraction helper (identical to test_bootstrap_userspace_fallback.sh):
# one-liner functions have no standalone '}' line, so a plain awk range would
# swallow every function up to the NEXT one that does. Try a single-line
# match first; fall back to the awk range for real multi-line functions. ──
extract_fn() {
  local name="$1" src="$2" oneliner
  oneliner="$(grep -E "^${name}\(\)[ ]*\{.*\}[ ]*\$" "$src" 2>/dev/null | head -1 || true)"
  if [ -n "$oneliner" ]; then printf '%s\n' "$oneliner"
  else awk "/^${name}\\(\\)[ ]*\\{/,/^}\$/" "$src"
  fi
}

for fn in have have_sudo is_mac is_linux hdr dry ok warn err log note_gap print_terminal_step; do
  [ -n "$(extract_fn "$fn" "$BOOTSTRAP")" ] || fail "setup: $fn() not found in bootstrap.sh"
done

# ── stubs: uname forced to darwin/arm64 (see HERMETICITY NOTE above), and
# python3 stubbed to fail its version probe unconditionally so the Python
# block's fallback branch fires deterministically regardless of the runner's
# real ambient interpreter (same trap documented in
# test_bootstrap_userspace_fallback.sh's build_stubs). No brew stub: absence
# of a brew binary anywhere on $PATH IS the scenario. No node stub either --
# neither /usr/bin nor /bin ships one, so `have node` is naturally false. ──
build_stubs() {
  local stubdir="$1"
  cat > "$stubdir/uname" <<'STUB'
#!/usr/bin/env bash
case "$1" in
  -s) echo "Darwin" ;;
  -m) echo "arm64" ;;
  *) command uname "$@" ;;
esac
STUB
  cat > "$stubdir/python3" <<'STUB'
#!/usr/bin/env bash
[ "$1" = "-c" ] && exit 1
echo "Python 3.9.0"
STUB
  chmod +x "$stubdir/uname" "$stubdir/python3"
}

# build_harness SRC OUT FAKEHOME -- assembles a runnable script from the REAL
# Homebrew decision block plus the REAL Python/Node section bodies (each
# extracted by its outer if/fi, identical anchor text in both the pre-fix
# fixture and the post-fix source, so this same builder proves both). The
# two user-space installer functions are stubbed to a sentinel + success:
# their own correctness is test_bootstrap_userspace_fallback.sh's job, this
# harness only needs to prove the call sites are REACHED.
build_harness() {
  local src="$1" out="$2" fakehome="$3"
  {
    echo 'set -uo pipefail'
    echo "HOME=\"$fakehome\""
    echo "USERPROFILE=\"$fakehome\""
    echo 'SHELL=/bin/bash'
    echo 'FAILED=()'
    echo 'LANG_CODE=en'
    echo 't() { [ "$LANG_CODE" = es ] && echo "$2" || echo "$1"; }'
    for fn in log ok warn err hdr dry is_mac is_linux have have_sudo; do
      extract_fn "$fn" "$src"
    done
    grep '^GAPS_FILE=' "$src" | sed "s|\$HOME|$fakehome|" || true
    extract_fn note_gap "$src"
    echo "SKILL_DIR=\"$fakehome/.claude/skills/ai-brain-starter\""
    extract_fn print_terminal_step "$src"
    echo 'CORPORATE_PROFILE=0'
    echo 'DRY_RUN=0'
    # Stub the leaf installers BEFORE the blocks that call them. Reachability
    # only: always succeed, and print a sentinel this test greps for.
    echo 'install_python_userspace() { echo "STUB_PY_USERSPACE_CALLED"; return 0; }'
    echo 'install_node_userspace() { echo "STUB_NODE_USERSPACE_CALLED"; return 0; }'
    echo 'PY="python3"'
    # The Homebrew DECISION block only (never the absolute-path brew-sourcing
    # loop above it in the real file -- see HERMETICITY NOTE at the top).
    awk '/^if is_mac && ! have brew && \[\[ "\$CORPORATE_PROFILE" == "1" \]\]; then$/,/^fi$/' "$src"
    # Interpreter-token-agnostic anchor (matches both "$PY" and a literal
    # python3), same reasoning as test_bootstrap_userspace_fallback.sh.
    awk '/^if ! .* -c "import sys; assert sys.version_info >= \(3,10\)" 2>\/dev\/null; then$/,/^fi$/' "$src"
    awk '/^if ! have node; then$/,/^fi$/' "$src"
    # Only printed if control fell all the way through with no `exit` above.
    echo 'echo "RESULT_REACHED_END=1"'
    echo 'echo "RESULT_FAILED_COUNT=${#FAILED[@]}"'
  } > "$out"
}

# ── MAIN SCENARIO: brew absent, stdin not a TTY, no --profile corporate,
# DRY_RUN=0 -> the run must not exit early; it must reach and call both
# user-space installer sites and fall through to the end. ──
POST_STUB="$TMP/post-stub"; mkdir -p "$POST_STUB"
build_stubs "$POST_STUB"
POST_HOME="$TMP/post-home"; mkdir -p "$POST_HOME"
POST_HARNESS="$TMP/post-harness.sh"
build_harness "$BOOTSTRAP" "$POST_HARNESS" "$POST_HOME"
set +e
POST_OUT="$(PATH="$POST_STUB:/usr/bin:/bin" bash "$POST_HARNESS" < /dev/null 2>&1)"
POST_CODE=$?
set -e

echo "$POST_OUT" | grep -q '^RESULT_REACHED_END=1$' \
  || fail "1: the run exited before reaching the end of the extracted control flow (the MYC-739-regression exit-0 bug). Output:
$POST_OUT"
echo "$POST_OUT" | grep -q '^STUB_PY_USERSPACE_CALLED$' \
  || fail "2: install_python_userspace was never called -- the Python user-space call site was not reached. Output:
$POST_OUT"
echo "$POST_OUT" | grep -q '^STUB_NODE_USERSPACE_CALLED$' \
  || fail "3: install_node_userspace was never called -- the Node user-space call site was not reached. Output:
$POST_OUT"
echo "$POST_OUT" | grep -q '^RESULT_FAILED_COUNT=0$' \
  || fail "4: err() fired at least once -- brew was attempted-and-failed rather than cleanly skipped. Output:
$POST_OUT"
echo "$POST_OUT" | grep -qi "TERMINAL STEP NEEDED" \
  || fail "5: the optional Homebrew-managed-install guidance (print_terminal_step) never printed -- it should still show as advice even though the run continues past it. Output:
$POST_OUT"
[ "$POST_CODE" -eq 0 ] || fail "6: the harness itself exited non-zero ($POST_CODE) -- unexpected error, not the early-exit this test targets. Output:
$POST_OUT"
echo "PASS: main scenario -- brew-less, non-interactive, non-corporate Mac reaches both user-space installer call sites and continues (RESULT_FAILED_COUNT=0)"

# ── NEGATIVE CONTROL: the identical harness against the frozen pre-fix
# fixture must fail the way the bug actually failed -- exit early via the old
# `exit 0`, before RESULT_REACHED_END, before either installer call site. ──

# Provenance, not merely difference (same reasoning as
# test_bootstrap_userspace_fallback.sh check 5): a byte-identical fixture
# would make this control compare the fix against itself and pass for the
# wrong reason.
if [ "$(cksum < "$FIXTURE")" = "$(cksum < "$BOOTSTRAP")" ]; then
  fail "7 (negative control): the pre-fix fixture is byte-identical to $BOOTSTRAP, so this control cannot fail. Restore a genuine pre-fix snapshot."
fi
# What makes this fixture pre-fix is precisely that it still contains the
# "stop and wait" instruction this fix deleted. If a future maintainer
# "refreshes" the fixture by re-extracting from a later bootstrap.sh, this
# marker is gone and this assertion catches it -- pointing at the fixture,
# not sending the next person to debug bootstrap.sh for a bug that isn't there.
grep -q "STOP here" "$FIXTURE" \
  || fail "7 (negative control): the pre-fix fixture no longer contains bootstrap.sh's old \"STOP here\" instruction, so it was probably regenerated from post-fix source. It must stay a frozen snapshot of e9f80e0, never a re-extraction from HEAD."

PRE_HOME="$TMP/pre-home"; mkdir -p "$PRE_HOME"
PRE_HARNESS="$TMP/pre-harness.sh"
build_harness "$FIXTURE" "$PRE_HARNESS" "$PRE_HOME"
set +e
PRE_OUT="$(PATH="$POST_STUB:/usr/bin:/bin" bash "$PRE_HARNESS" < /dev/null 2>&1)"
PRE_CODE=$?
set -e

if echo "$PRE_OUT" | grep -q '^RESULT_REACHED_END=1$'; then
  fail "8 (negative control): the pre-fix source was expected to exit early (never print RESULT_REACHED_END), but it reached the end anyway -- this harness would NOT have caught the original bug. Output:
$PRE_OUT"
fi
if echo "$PRE_OUT" | grep -q 'STUB_PY_USERSPACE_CALLED\|STUB_NODE_USERSPACE_CALLED'; then
  fail "8 (negative control): the pre-fix source was expected to never reach either user-space installer, but at least one was called. Output:
$PRE_OUT"
fi
[ "$PRE_CODE" -eq 0 ] \
  || fail "8 (negative control): the pre-fix source's early exit was expected to be a CLEAN exit 0 (the deceptive part of the original bug -- it looks like success) but the harness exited $PRE_CODE instead. Output:
$PRE_OUT"
echo "PASS: negative control confirmed against the frozen pre-fix fixture (e9f80e0) -- exited early and cleanly (code 0), before RESULT_REACHED_END, before either installer call site"

echo "PASS: test_bootstrap_brewless_reaches_userspace (negative control on the main scenario)"
