#!/usr/bin/env bash
# Regression test for team_broadcast_install_gap() in
# hooks/surface-stale-automation-failures.py.
#
# WHY THIS EXISTS: team_broadcast_findings() infers health from a log file
# (~/.claude/logs/team-broadcast-daily.log). A machine where team-broadcast was
# NEVER installed has no log file for the same reason a HEALTHY, quiet install
# has no log file yet: nothing has run. Both read as team_broadcast_findings()
# returning [] -- so "never installed" produced zero signal, on every session,
# indefinitely. That is a stricter silence than an outright failure would have
# been. team_broadcast_install_gap() checks installation directly (script
# presence, then the launchd job) instead of inferring it from a log.
#
# THE LABEL IS MATCHED BY SUFFIX, NEVER BY A LITERAL NAMESPACE. The job is
# `com.<operator>.team-broadcast-daily`, and the `<operator>` half is chosen by
# whoever installed it. A hardcoded reverse-DNS prefix would query a label that
# exists on exactly one machine and report "not registered" forever to every
# other operator who HAS installed it. Cases 3 and 4 are the control for that:
# two DIFFERENT fictional namespaces must both read as installed.
#
# Asserts, in order:
#   0. DEFAULT INSTALL: never set up here -> SILENT. This substrate ships no
#      team-broadcast skill, so a missing auto-send.py is the NORMAL state and
#      nagging about it would train every operator to ignore this watchdog.
#   1. OPTED-IN BUT BROKEN: set up here, script gone -> FIRES, names the path.
#   2. CRON-GAP: script present, no *.team-broadcast-daily job -> FIRES, names
#      the suffix it looked for, and does NOT claim session-close broadcasts (a
#      separate, live-invoked path) are affected.
#   3. NEG-CONTROL: script present, job registered under one namespace -> SILENT.
#   4. ANY-OPERATOR: same, under a completely different namespace -> SILENT.
#      Case 3 alone would still pass if the suffix match were secretly a literal.
#   4b. HOLLOW: registered but runs=0 -> FIRES. `launchctl list` shows status 0
#      for a job that never ran, identical to a healthy one, and the generic
#      launchd pass skips this label by suffix — so only this finder can see it.
#   5. END-TO-END: the finding reaches the real SessionStart entrypoint
#      (main()'s systemMessage), not just the helper function in isolation.
#
# launchctl is macOS-only; CI runs ubuntu (CONTRIBUTING.md). A fake launchctl on
# PATH makes cases 2-5 deterministic on any OS instead of skipping Linux CI.
# Stdlib python3 + bash only. No network, no real launchd/git. Tmpdir on exit.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/surface-stale-automation-failures.py"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$SCRIPT_DIR/lib/sandbox_home.sh"

PASS=0
FAIL=0
TMPDIRS=()
cleanup() { for d in "${TMPDIRS[@]:-}"; do [ -n "$d" ] && rm -rf "$d"; done; }
trap cleanup EXIT

ok()  { PASS=$((PASS+1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL  $1 :: $2"; }
assert_fires()  { case "$OUT" in *additionalContext*|*systemMessage*) ok "$1" ;; *) bad "$1" "no finding (out=${OUT:0:120})" ;; esac; }
assert_silent() { case "$OUT" in "") ok "$1" ;; *) bad "$1" "unexpected output (out=${OUT:0:120})" ;; esac; }
assert_has()    { case "$OUT" in *"$2"*) ok "$1" ;; *) bad "$1" "missing '$2' (out=${OUT:0:150})" ;; esac; }
assert_lacks()  { case "$OUT" in *"$2"*) bad "$1" "unexpectedly present: '$2'" ;; *) ok "$1" ;; esac; }

newhome() { local d; d="$(mktemp -d)"; TMPDIRS+=("$d"); mkdir -p "$d/.claude"; echo "$d"; }

install_broadcast_script() {  # <home> -- make auto-send.py exist
  mkdir -p "$1/.claude/skills/team-broadcast/scripts"
  touch "$1/.claude/skills/team-broadcast/scripts/auto-send.py"
}

opt_in_but_broken() {  # <home> -- skill dir present, auto-send.py MISSING
  mkdir -p "$1/.claude/skills/team-broadcast"
}

# fake_launchctl HOME REGISTERED_LABEL_OR_EMPTY [RUNS] -- puts a fake `launchctl`
# ahead of the real one on PATH. Answers the two calls this hook makes:
#   `launchctl list`                  -> the tab-separated PID/Status/Label table
#   `launchctl print gui/<uid>/<lbl>` -> the `runs = N` liveness field
# RUNS defaults to 7 (a job that has actually executed). Pass 0 to model a
# HOLLOW job: registered, status 0, never once run — indistinguishable from
# healthy in the `list` table alone, which is the whole reason for the probe.
fake_launchctl() {
  local home="$1" registered="$2" runs="${3:-7}" bin="$1/fakebin" row=""
  mkdir -p "$bin"
  [ -n "$registered" ] && row="printf -- '-\t0\t%s\n' '$registered'"
  cat > "$bin/launchctl" <<EOF
#!/usr/bin/env bash
if [ "\$1" = "list" ] && [ -z "\${2:-}" ]; then
  printf 'PID\tStatus\tLabel\n'
  $row
  exit 0
fi
if [ "\$1" = "print" ]; then
  if [ -n "$registered" ]; then
    case "\${2:-}" in
      *"$registered")
        printf '\tstate = waiting\n\truns = $runs\n\tlast exit code = 0\n'
        exit 0
        ;;
    esac
  fi
  exit 1
fi
exit 0
EOF
  chmod +x "$bin/launchctl"
}

# run_hook HOME -- invokes the real SessionStart entrypoint, sandboxed fakebin
# first on PATH so it beats any real launchctl on the runner.
run_hook() {
  local home="$1"
  OUT="$(PATH="$home/fakebin:$PATH" run_sandboxed "$home" python3 "$HOOK" <<<'{}' 2>/dev/null)"
}

echo "=== precondition ==="
[ -f "$HOOK" ] && ok "hook exists" || bad "hook exists" "missing $HOOK"

echo "=== 0. DEFAULT INSTALL: never set up here -> SILENT ==="
# THE control for this whole hook. This substrate never installs team-broadcast,
# so a missing auto-send.py is the normal state for nearly every install. If
# this case ever fires, every stranger who installs the repo is nagged on every
# session about a component they never asked for.
H0="$(newhome)"
fake_launchctl "$H0" ""
run_hook "$H0"
assert_silent "a clean install that never had team-broadcast is not nagged about it"

echo "=== 1. OPTED-IN BUT BROKEN: set up here, auto-send.py gone -> FIRES ==="
H1="$(newhome)"
opt_in_but_broken "$H1"
fake_launchctl "$H1" ""
run_hook "$H1"
assert_fires "fires when the operator set it up and the script went missing"
assert_has   "names the missing script path" "team-broadcast/scripts/auto-send.py"
assert_has   "says how to restore it" "Reinstall the team-broadcast skill"

echo "=== 2. CRON-GAP: script present, no *.team-broadcast-daily job -> FIRES ==="
H2="$(newhome)"
install_broadcast_script "$H2"
fake_launchctl "$H2" ""   # nothing registered
run_hook "$H2"
assert_fires "fires when the daily-broadcast launchd job is unregistered"
assert_has   "names the label suffix it looked for" ".team-broadcast-daily"
assert_lacks "reports the suffix, not any literal reverse-DNS namespace" "com."
assert_has   "clarifies session-close broadcasts are unaffected" "unaffected"
assert_lacks "does not claim broadcasts are unreachable (that's case 1's wording)" "unreachable"

echo "=== 3. NEG-CONTROL: registered under one namespace -> SILENT ==="
H3="$(newhome)"
install_broadcast_script "$H3"
fake_launchctl "$H3" "com.example.team-broadcast-daily"
run_hook "$H3"
assert_silent "no finding when installed and the cron is registered (no cry-wolf)"

echo "=== 4. ANY-OPERATOR: a totally different namespace -> ALSO SILENT ==="
H4="$(newhome)"
install_broadcast_script "$H4"
fake_launchctl "$H4" "io.someone-else.team-broadcast-daily"
run_hook "$H4"
assert_silent "the suffix match is not secretly a literal: another operator's label also counts as installed"

echo "=== 4b. HOLLOW: registered but runs=0 -> FIRES ==="
# The case launchd_failures() cannot report: it skips this label by suffix
# because THIS finder owns it. If the finder reads "registered" as healthy,
# a daily broadcast that has never once fired is invisible everywhere.
H4B="$(newhome)"
install_broadcast_script "$H4B"
fake_launchctl "$H4B" "com.example.team-broadcast-daily" 0
run_hook "$H4B"
assert_fires "fires when the job is registered but has never run"
assert_has   "names the hollow state" "NEVER RUN"
assert_has   "gives the reload remedy" "launchctl bootout"

echo "=== 5. END-TO-END: reaches main()'s systemMessage, not just the helper ==="
H5="$(newhome)"
opt_in_but_broken "$H5"
fake_launchctl "$H5" ""
run_hook "$H5"
assert_has "the missing-install finding rides the real systemMessage envelope" "systemMessage"
assert_has "carries the shared incident framing" "191-file strand"

echo ""
echo "=== SUMMARY: $PASS passed, $FAIL failed ==="
[ "$FAIL" = 0 ]
