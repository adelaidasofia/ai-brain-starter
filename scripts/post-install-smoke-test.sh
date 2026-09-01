#!/usr/bin/env bash
# exit-contract: ENFORCING

# post-install-smoke-test.sh — end-to-end verification after bootstrap.
#
# Verifies that every component the bootstrap claimed to install is actually
# functional. Same class of bug as the Windows BOM crash: install succeeds,
# downstream invocation fails silently. Catch it at the install boundary.
#
# Tests:
#   1. Every Python script in ai-brain-starter passes py_compile
#   2. Every bash script passes `bash -n`
#   3. Every JSON config parses
#   4. Every hook script returns valid JSON when piped a sample input
#   5. Every aggregator script runs --help / dry-run without crashing
#   6. Every bundled skill folder has a SKILL.md
#   7. Optional: every skill responds to a no-op invocation
#
# Usage:
#   bash scripts/post-install-smoke-test.sh             # full run
#   bash scripts/post-install-smoke-test.sh --quick     # syntax + JSON only
#   bash scripts/post-install-smoke-test.sh --quiet     # only print summary
#
# Exit codes: 0 = all pass, 1 = warnings only, 2 = critical failure.

set -uo pipefail

QUICK=0
QUIET=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --quick) QUICK=1; shift ;;
    --quiet) QUIET=1; shift ;;
    --help|-h) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Honor an explicit SKILL_DIR (lets a maintainer smoke a checkout/worktree
# pre-merge); otherwise auto-detect the installed clone, then the maintainer dir.
SKILL_DIR="${SKILL_DIR:-}"
if [[ -z "$SKILL_DIR" ]]; then
  SKILL_DIR="$HOME/.claude/skills/ai-brain-starter"
  [[ -d "$SKILL_DIR" ]] || SKILL_DIR="$HOME/Desktop/ai-brain-starter"
fi

PASS=0
WARN=0
FAIL=0

ok()   { [[ "$QUIET" -eq 0 ]] && printf "  \033[32m✓\033[0m %s\n" "$*"; PASS=$((PASS+1)); }
warn() { [[ "$QUIET" -eq 0 ]] && printf "  \033[33m!\033[0m %s\n" "$*"; WARN=$((WARN+1)); }
fail() { [[ "$QUIET" -eq 0 ]] && printf "  \033[31m✗\033[0m %s\n" "$*"; FAIL=$((FAIL+1)); }
hdr()  { [[ "$QUIET" -eq 0 ]] && printf "\n\033[1m%s\033[0m\n" "$*"; }

if [[ ! -d "$SKILL_DIR" ]]; then
  echo "FAIL: ai-brain-starter skill not found at any known location" >&2
  exit 2
fi

# === 1. Python syntax ===
hdr "Python syntax (py_compile)"
while IFS= read -r f; do
  if python3 -m py_compile "$f" 2>/dev/null; then
    ok "$f"
  else
    fail "$f"
  fi
done < <(find "$SKILL_DIR" -name "*.py" -type f -not -path "*/__pycache__/*" -not -path "*/.git/*" 2>/dev/null)

# === 2. Bash syntax ===
hdr "Bash syntax (bash -n)"
while IFS= read -r f; do
  if bash -n "$f" 2>/dev/null; then
    ok "$f"
  else
    fail "$f"
  fi
done < <(find "$SKILL_DIR" -name "*.sh" -type f -not -path "*/.git/*" 2>/dev/null)

# === 3. JSON config syntax ===
hdr "JSON config syntax"
for f in "$SKILL_DIR/hooks.json" "$SKILL_DIR"/templates/closing-signals/*.json "$SKILL_DIR"/templates/schemas/*.json; do
  [[ -f "$f" ]] || continue
  if python3 -c "import json; json.load(open('$f'))" 2>/dev/null; then
    ok "$f"
  else
    fail "$f"
  fi
done

[[ "$QUICK" -eq 1 ]] && {
  hdr "Summary"
  ok "$PASS pass, $WARN warn, $FAIL fail (quick mode)"
  [[ "$FAIL" -gt 0 ]] && exit 2 || ([[ "$WARN" -gt 0 ]] && exit 1) || exit 0
}

# === 4. Hook smoke test (sample stdin) ===
# Hermetic fixture: a throwaway vault with a Meta/ dir and NO CLAUDE.md, with
# VAULT_ROOT pinned to it. This isolates the detector from the operator's own
# closingSignals config and any ambient VAULT_ROOT. Without it, a machine whose
# real vault sets `closingSignals.customOnly: true` (which makes the detector
# fire ONLY on the user's custom phrases and skip the shared pack) sees the
# shared-pack "bye" correctly suppressed — and this check false-FAILs even though
# the detector is behaving as configured (MYC-1988). CLOSING_SIGNAL_DETECTION is
# unset so the check never reaches the optional Haiku/API path.
hdr "Hook smoke tests"
DETECTOR="$SKILL_DIR/hooks/detect-closing-signal.py"
if [[ -f "$DETECTOR" ]]; then
  FIXTURE_VAULT=$(mktemp -d)
  mkdir -p "$FIXTURE_VAULT/Meta/Sessions"
  resp=$(echo '{"prompt":"hello world","session_id":"smoke","cwd":"'"$FIXTURE_VAULT"'"}' | env -u CLOSING_SIGNAL_DETECTION -u CLOSING_SIGNAL_LANGS VAULT_ROOT="$FIXTURE_VAULT" python3 "$DETECTOR" 2>/dev/null)
  if echo "$resp" | python3 -c "import json,sys; json.loads(sys.stdin.read())" 2>/dev/null; then
    ok "detect-closing-signal.py returns valid JSON for non-close input"
  else
    fail "detect-closing-signal.py returned invalid JSON"
  fi
  resp=$(echo '{"prompt":"bye","session_id":"smoke","cwd":"'"$FIXTURE_VAULT"'"}' | env -u CLOSING_SIGNAL_DETECTION -u CLOSING_SIGNAL_LANGS VAULT_ROOT="$FIXTURE_VAULT" python3 "$DETECTOR" 2>/dev/null)
  if echo "$resp" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); assert 'hookSpecificOutput' in d" 2>/dev/null; then
    ok "detect-closing-signal.py injects context on 'bye' (hermetic shared-pack fixture)"
  else
    fail "detect-closing-signal.py did not inject context on 'bye'"
  fi
  rm -rf "$FIXTURE_VAULT"
fi

LINTER="$SKILL_DIR/hooks/lint-vault-frontmatter.py"
if [[ -f "$LINTER" ]]; then
  resp=$(echo '{"tool_name":"Read","tool_input":{}}' | python3 "$LINTER" 2>/dev/null)
  if echo "$resp" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); assert d.get('hookSpecificOutput',{}).get('permissionDecision')=='allow'" 2>/dev/null; then
    ok "lint-vault-frontmatter.py allows non-Write tools"
  else
    fail "lint-vault-frontmatter.py wrong response for Read tool"
  fi
fi

# === 4b. dev-hub-refresh guard + surfacer (MYC-1893) ===
hdr "dev-hub-refresh (bare ~/dev hub-rot guard)"
WARN_STALE="$SKILL_DIR/hooks/warn-stale-dev-checkout.py"
if [[ -f "$WARN_STALE" ]]; then
  # Pin the scanned root. Unpinned it resolves the operator's real ~/dev, so
  # this check's verdict depends on a tree no fixture controls -- today the
  # probe path falls outside it and the hook short-circuits, which is exactly
  # the kind of accident that stops being true after any later edit.
  FAKE_DEV=$(mktemp -d); FIRES_F=$(mktemp)
  [[ -n "$FAKE_DEV" && -n "$FIRES_F" ]] || fail "mktemp failed; the pins below would silently fall back to the real HOME"
  resp=$(echo '{"tool_name":"Read","session_id":"smoke","tool_input":{"file_path":"/tmp/not-a-dev-repo"}}' | STALE_CHECKOUT_DEV_ROOT="$FAKE_DEV" GUARD_FIRES_LOG="$FIRES_F" python3 "$WARN_STALE" 2>/dev/null)
  if echo "$resp" | python3 -c "import json,sys; s=sys.stdin.read(); json.loads(s) if s.strip() else None" 2>/dev/null; then
    ok "warn-stale-dev-checkout.py runs + emits valid JSON (silent on a non-~/dev path)"
  else
    fail "warn-stale-dev-checkout.py produced invalid output"
  fi
  rm -rf "$FAKE_DEV"; rm -f "$FIRES_F"
else
  warn "warn-stale-dev-checkout.py not present"
fi

SURFACER="$SKILL_DIR/hooks/dev-hub-refresh-on-session-start.py"
if [[ -f "$SURFACER" ]]; then
  STATE_F=$(mktemp)
  printf '%s' '{"summary":{"ff":0,"surfaced":1,"skipped":0,"max_behind":360,"offenders":[["studio","surface:off-default",360]]}}' > "$STATE_F"
  # PIN EVERY state path this surfacer reads, not only the one the fixture feeds.
  # It reads four, and three of them defaulted to the operator's real ~/.claude:
  #   DEV_HUB_REFRESH_STATE      the fixture above (already pinned)
  #   DEV_DRIFT_STATE            prepends the un-backed-up-drift section
  #   STANDING_REPORT_STATE_DIR  the anti-wallpaper condenser (_lib/standing_report)
  #   DEV_DRIFT_FETCH_STATE      _lib/dev_repo_scan binds it at MODULE level, so
  #                              importing the module is enough; its own comment
  #                              names "a test" as the caller that poisons the
  #                              operator's real 4-hour fetch cap
  #
  # Unpinned, this check flipped its own verdict on the NEXT run with zero code
  # change. Run 1 found no prior hash, so condense() returned the FULL render and
  # the per-offender line naming `studio` was present -- and it WROTE that hash.
  # Run 2 matched it, the digest fired, and the enumeration this assertion looks
  # for was gone. Measured 2026-09-01: PASS, FAIL, FAIL over three identical runs.
  # It also clobbered the operator's real
  # ~/.claude/.standing-reports/dev-hub-refresh.json with a synthetic
  # single-offender hash, resetting the age counter on their live surfacer.
  # Same shape as #539; the standing_report docstring already prescribes this
  # remedy. Sandboxing HOME is NOT a substitute -- on Windows expanduser reads
  # USERPROFILE and walks straight back out (MYC-3536).
  #
  # DRIFT_F is deliberately an EMPTY file: unparseable JSON and an absent file
  # both yield an empty drift section, so the render is hub-only on a developer
  # box and on a clean CI runner alike.
  SR_A=$(mktemp -d); SR_B=$(mktemp -d)
  DRIFT_F=$(mktemp); FETCH_F=$(mktemp); SWEEP_F=$(mktemp)
  [[ -n "$SR_A" && -n "$SR_B" && -n "$DRIFT_F" && -n "$FETCH_F" && -n "$SWEEP_F" ]] \
    || fail "mktemp failed; an empty pin is falsy and silently resolves the real HOME"
  # env -u STANDING_REPORT_BYPASS: with it set, standing_report.report() returns
  # BEFORE writing, so the bind probe below reported "pin did not bind" on a pin
  # that bound perfectly -- a spurious failure naming the wrong cause, and the
  # check's exit code depending on the operator's ambient environment again.
  run_surfacer() {   # $1 = condenser state dir
    echo '{}' | env -u STANDING_REPORT_BYPASS \
      DEV_HUB_REFRESH_STATE="$STATE_F" DEV_DRIFT_STATE="$DRIFT_F" \
      DEV_DRIFT_FETCH_STATE="$FETCH_F" STANDING_REPORT_STATE_DIR="$1" \
      LS_SWEEP_TOOL="$SWEEP_F" python3 "$SURFACER" 2>/dev/null
  }
  names_studio() {
    python3 -c "import json,sys; d=json.loads(sys.stdin.read()); assert 'studio' in d.get('hookSpecificOutput',{}).get('additionalContext','')" 2>/dev/null
  }
  # TWICE, each from its OWN fresh condenser dir. One invocation cannot tell a
  # hermetic check from a self-poisoning one -- the incident shape IS "passes
  # once, fails after". Reusing ONE dir would instead assert against the
  # condenser's own contract (an unchanged finding-set is SUPPOSED to condense on
  # the second render; that is test_standing_report.py's subject, not this
  # check's).
  #
  # SCOPE OF THIS CONTROL, measured rather than assumed: dropping each pin in turn
  # and running the section twice, only STANDING_REPORT_STATE_DIR makes the two
  # runs disagree. DEV_DRIFT_STATE and DEV_DRIFT_FETCH_STATE are read-only on this
  # path, so the two-run control catches 1 of the 3 pins, not all of them. An
  # earlier draft of this comment claimed "if ANY state path is still unpinned ...
  # invocation 2 diverges", which is false by a factor of three -- exactly the kind
  # of confident sentence about correct code that reads as evidence and is not.
  # hooks/test_smoke_hook_hermeticity.py is what actually covers the other two.
  if run_surfacer "$SR_A" | names_studio && run_surfacer "$SR_B" | names_studio; then
    ok "dev-hub-refresh surfacer emits a surface line from its state file (agrees across independent runs)"
  else
    fail "dev-hub-refresh surfacer did not surface the off-default hub"
  fi
  # The pin BINDS: every assertion above is vacuous if the surfacer still reached
  # the real state dir. Mirrors the bind-probe control in test_standing_report.py.
  if [[ -n "$(ls -A "$SR_A" 2>/dev/null)" ]]; then
    ok "surfacer condenser state landed in the pinned dir, not the operator's ~/.claude"
  else
    fail "STANDING_REPORT_STATE_DIR pin did not bind (surfacer wrote its state elsewhere)"
  fi
  rm -f "$STATE_F" "$DRIFT_F" "$FETCH_F" "$SWEEP_F"; rm -rf "$SR_A" "$SR_B"
  unset -f run_surfacer names_studio
else
  warn "dev-hub-refresh-on-session-start.py not present"
fi

# === 4c. Journal Step-0 context-guard self-heal (2026-07-07) ===
# The SessionStart self-heal that re-derives the /journal context guard from the
# vault-synced substrate on every session, so a stale account cannot silently skip
# Step 0. --self-test carries the pos/neg + repair controls; the direct negative
# control here proves the check BITES on an unprotected settings.json.
hdr "Journal-guard self-heal"
HEALER="$SKILL_DIR/scripts/heal-journal-guard.py"
if [[ -f "$HEALER" ]]; then
  if python3 "$HEALER" --self-test >/dev/null 2>&1; then
    ok "heal-journal-guard.py --self-test (registration + preflight controls)"
  else
    fail "heal-journal-guard.py --self-test failed"
  fi
  NEG_SET=$(mktemp)
  printf '%s' '{"hooks": {}}' > "$NEG_SET"
  if python3 "$HEALER" --check-only --settings "$NEG_SET" >/dev/null 2>&1; then
    fail "self-heal --check-only PASSED an unprotected settings.json (guard asleep)"
  else
    ok "self-heal --check-only trips on an unprotected account (exit 1)"
  fi
  rm -f "$NEG_SET"
else
  warn "heal-journal-guard.py not present"
fi

# === 5. Aggregator smoke ===
hdr "Aggregator scripts"
for s in aggregate-sessions.py aggregate-decisions.py rotate-meta-archives.py; do
  f="$SKILL_DIR/scripts/$s"
  if [[ -f "$f" ]]; then
    if python3 "$f" --help >/dev/null 2>&1 || python3 -c "import importlib.util; spec=importlib.util.spec_from_file_location('m','$f'); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)" 2>/dev/null; then
      ok "$s loads without error"
    else
      warn "$s could not be loaded standalone (may need vault context)"
    fi
  fi
done

# === 6. Bundled skills have SKILL.md ===
hdr "Bundled skills"
for s in daily-journal deconstruct diagnose for-my-team graphify insights meeting-todos nano-banana patterns repurpose-talk second-brain-mapping setup-vault-types; do
  if [[ -f "$SKILL_DIR/skills/$s/SKILL.md" ]]; then
    ok "skills/$s/SKILL.md"
  else
    warn "skills/$s/SKILL.md missing (may not be bundled)"
  fi
done

# === 7. Schema validator self-test ===
hdr "Schema validator"
if [[ -f "$SKILL_DIR/scripts/vault-schema-validator.py" ]]; then
  if python3 "$SKILL_DIR/scripts/vault-schema-validator.py" --self-test >/dev/null 2>&1; then
    ok "vault-schema-validator.py self-test"
  else
    fail "vault-schema-validator.py self-test failed"
  fi
fi

# === 7b. Context-budget measurer self-test (MYC-619) ===
hdr "Context-budget measurer"
if [[ -f "$SKILL_DIR/hooks/context-budget-measure.py" ]]; then
  CBM_FIRES=$(mktemp)
  # The self-test sizes its fixture against the DEFAULT ceiling, so an operator
  # who has tuned either knob fails a check about code that is behaving.
  if env -u CONTEXT_BUDGET_GLOBAL_CEILING -u CONTEXT_BUDGET_TOL_FRAC \
       GUARD_FIRES_LOG="$CBM_FIRES" python3 "$SKILL_DIR/hooks/context-budget-measure.py" --self-test >/dev/null 2>&1; then
    ok "context-budget-measure.py self-test"
  else
    fail "context-budget-measure.py self-test failed"
  fi
fi

# === 8. Closing-signal fixture harness ===
hdr "Closing-signal fixtures"
if [[ -f "$SKILL_DIR/scripts/test-closing-signals.py" ]]; then
  # The harness spawns the detector with {**os.environ}, overriding only
  # CLOSING_SIGNAL_DETECTION / VAULT_ROOT / ANTHROPIC_API_KEY -- so an ambient
  # CLOSING_SIGNAL_LANGS reaches the child and fails a check about working code.
  if env -u CLOSING_SIGNAL_LANGS python3 "$SKILL_DIR/scripts/test-closing-signals.py" >/dev/null 2>&1; then
    ok "test-closing-signals.py 74/74"
  else
    fail "test-closing-signals.py had failures"
  fi
fi

hdr "Summary"
[[ "$QUIET" -eq 1 ]] && printf "smoke: %d pass, %d warn, %d fail\n" "$PASS" "$WARN" "$FAIL"
[[ "$QUIET" -eq 0 ]] && printf "  \033[1m%d pass · %d warn · %d fail\033[0m\n" "$PASS" "$WARN" "$FAIL"
[[ "$FAIL" -gt 0 ]] && exit 2
[[ "$WARN" -gt 0 ]] && exit 1
exit 0
