#!/bin/bash
# session-close-runner.sh — deterministic session-close aggregation + proof report.
#
# Runs the index aggregators (Last Session.md, Decision Log) so a session close does
# NOT depend on the model running them by hand, and writes a proof report the optional
# verify-session-close-cascade Stop hook can check.
#
# Defensive by design: no `set -e`. Every step is guarded and non-fatal — a missing or
# failing sub-script must NEVER abort a close. A vault that lacks a given aggregator
# simply skips it.
#
# Invoked from the close cascade (hooks/detect-closing-signal.py, Phase 0a) as:
#   bash "<vault>/<Meta>/scripts/session-close-runner.sh"
# where <Meta> is the vault's meta folder ("⚙️ Meta" or plain "Meta").
#
# Report contract (consumed by hooks/verify-session-close-cascade.py):
#   /tmp/abs-session-close-runner.report exists, is fresh (<30 min), and its last
#   line is "RUNNER COMPLETE @ <timestamp>".
#
# Env:
#   VAULT_ROOT  Optional. Defaults to two levels up from this script (the vault root
#               when the script lives at <vault>/<Meta>/scripts/).

set -uo pipefail

# --- ai-brain-starter: shim-safe PATH (strip refuse-shims) ----------------
# Some machines carry a python3/python PATH shim (e.g. trailofbits
# modern-python) that exit-1s on bare invocation. It sits FIRST on PATH, so the
# `command -v python3` below would otherwise resolve the shim and every close
# aggregator would silently no-op. Drop any */hooks/shims dir from PATH so bare
# python calls here (and, via export, in children) hit a real python.
if [ "${PATH#*/hooks/shims}" != "$PATH" ]; then
  _abs_new=""; _abs_oifs=$IFS; IFS=:
  for _abs_d in $PATH; do
    case $_abs_d in */hooks/shims|*/hooks/shims/) ;; *) _abs_new=${_abs_new:+$_abs_new:}$_abs_d ;; esac
  done
  IFS=$_abs_oifs; PATH=$_abs_new; export PATH
  unset _abs_new _abs_d _abs_oifs
fi
# --------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT="${VAULT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
export VAULT_ROOT="$VAULT"
REPORT="/tmp/abs-session-close-runner.report"
TS="$(date '+%Y-%m-%dT%H:%M:%S%z')"
# Pick an interpreter that RUNS, not merely one that resolves. `command -v`
# answers "is this name on PATH", which is a weaker claim than "this executes
# python". On Windows the gap is routine: an app-execution alias for python3
# ships enabled by default in %LOCALAPPDATA%\Microsoft\WindowsApps, sits on
# PATH, satisfies `command -v`, and then exits non-zero printing an ad for the
# Microsoft Store — while a real python is on the same PATH one candidate
# later. Probing each candidate generalises the shim-strip above: it covers
# any non-working stub, wherever it lives and whatever it is called.
PYTHON=""
PYTHON_REJECTED=""
pick_python() {  # sets PYTHON / PYTHON_REJECTED in place — no subshell, or the
  local c p     # rejected list would be lost with the command substitution.
  for c in python3 python py; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    [ -n "$p" ] || continue
    if "$p" -c 'import sys; sys.exit(0)' >/dev/null 2>&1; then
      PYTHON="$p"
      return 0
    fi
    PYTHON_REJECTED="${PYTHON_REJECTED:+$PYTHON_REJECTED, }$c"
  done
  return 1
}
pick_python || true

log() { printf '%s\n' "$1" | tee -a "$REPORT"; }

: > "$REPORT"
log "session-close-runner @ $TS"
log "vault: $VAULT"
log "--- deterministic aggregation ---"

run_step() {  # human-name  script-filename
  local name="$1" script="$2" path="$SCRIPT_DIR/$2"
  if [ ! -f "$path" ]; then
    log "  [absent] $name ($script not installed — skipped)"
    return 0
  fi
  if [ -z "$PYTHON" ]; then
    if [ -n "$PYTHON_REJECTED" ]; then
      log "  [absent] $name (found $PYTHON_REJECTED on PATH, none of them ran — skipped)"
    else
      log "  [absent] $name (no python interpreter on PATH — skipped)"
    fi
    return 0
  fi
  local out rc line
  out="$("$PYTHON" "$path" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ]; then
    log "  [ok]     $name"
  else
    # Say WHY. The close is non-fatal by design, but a warning that drops the
    # reason is how a broken aggregator survives for weeks: the report reads
    # the same whether the vault is fine or the interpreter never ran.
    log "  [warn]   $name (exit $rc — non-fatal, continuing close)"
    if [ -n "$out" ]; then
      printf '%s\n' "$out" | tail -n 5 | while IFS= read -r line; do
        log "           | $line"
      done
    fi
  fi
}

run_step "aggregate-sessions"  "aggregate-sessions.py"
run_step "aggregate-decisions" "aggregate-decisions.py"

log ""
log "Still the model's manual job: Phase 0b (incomplete-work gate), Phase 1"
log "(conversation scan: seeds, to-dos, decisions), Phase 2 (batch writes),"
log "Phase 2b (vault-safe-commit the artifacts), Phase 3 (public-repo audit"
log "if this session shipped to one)."
log "RUNNER COMPLETE @ $TS"
