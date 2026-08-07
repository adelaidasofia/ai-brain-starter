#!/bin/bash
# --- ai-brain-starter: shim-safe PATH (strip refuse-shims) ----------------
# Some machines carry a python3/python PATH shim (e.g. trailofbits
# modern-python) that exit-1s on bare invocation and would turn every bare
# python call below into a silent no-op. Drop any */hooks/shims dir from PATH
# so bare python calls here (and, via export, in children) hit a real python.
if [ "${PATH#*/hooks/shims}" != "$PATH" ]; then
  _abs_new=""; _abs_oifs=$IFS; IFS=:
  for _abs_d in $PATH; do
    case $_abs_d in */hooks/shims|*/hooks/shims/) ;; *) _abs_new=${_abs_new:+$_abs_new:}$_abs_d ;; esac
  done
  IFS=$_abs_oifs; PATH=$_abs_new; export PATH
  unset _abs_new _abs_d _abs_oifs
fi
# --------------------------------------------------------------------------
# graph-context-hook.sh — UserPromptSubmit hook that injects targeted graph
# context when the user's prompt contains routing keywords.
#
# Companion to the static MANDATORY SESSION PROTOCOL hook in hooks.json.
# That hook always fires once per session. This one fires on EVERY prompt
# but is silent unless the prompt mentions one of your routing keywords.
#
# WHY: If your vault has a knowledge graph (e.g. from /graphify) — or several
# of them, like a personal graph and a separate team/work graph — you want
# Claude to actually OPEN the right graph report before answering instead of
# re-reading 5 source files. Telling Claude this in CLAUDE.md helps; injecting
# it as additionalContext at the moment of the matching prompt helps more.
#
# CUSTOMIZE THIS SCRIPT for your vault:
#   1. Set VAULT_ROOT to your vault path
#   2. Set PRIMARY_GRAPH / SECONDARY_GRAPH to your GRAPH_REPORT.md locations
#      (delete SECONDARY_* if you only have one graph)
#   3. Edit PRIMARY_PATTERN / SECONDARY_PATTERN regexes — list the keywords
#      that should trigger the routing hint for each scope
#   4. Edit the emit_context strings below to describe your scopes
#
# DESIGN NOTES:
#   - The hook does NOT pin specific god-node names. God-node names go stale
#     every graphify run. The stable signal is the PATH and the FRESHNESS DATE.
#     Let the model open the report to get the actual current top nodes.
#   - The hook computes the graph file's mtime and warns "STALE — last updated
#     N days ago" if older than STALE_DAYS, so you know when to re-run graphify.
#   - On no keyword match, emits {"continue":true} (silent passthrough).
#
# TEST IT:
#   echo '{"hook_event_name":"UserPromptSubmit","prompt":"<your test phrase>"}' | bash this-script.sh

set -euo pipefail

# ─── CONFIG ────────────────────────────────────────────────────────────────
# Edit these for your vault:

VAULT_ROOT="${VAULT_ROOT:-$HOME/Documents/MyVault}"

# Primary graph (e.g. main vault knowledge graph)
PRIMARY_GRAPH="$VAULT_ROOT/graphify-out/GRAPH_REPORT.md"
PRIMARY_LABEL="vault root"
PRIMARY_PATTERN='\bjournal\b|\bnote\b|\bidea\b|\bproject\b|writing|reading'

# Secondary graph (optional — e.g. separate work/team graph)
# Set to empty string if you only have one graph.
SECONDARY_GRAPH="$VAULT_ROOT/Work/⚙️ Meta/graphify-out/GRAPH_REPORT.md"
SECONDARY_LABEL="Work/"
SECONDARY_PATTERN='\bwork\b|\bteam\b|\bclient\b|meeting|deadline|sprint'

# Stale threshold (days). Reports older than this trigger a re-run warning.
STALE_DAYS=14

# ─── IMPLEMENTATION ────────────────────────────────────────────────────────

INPUT="$(cat || true)"
PROMPT="$(printf '%s' "$INPUT" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get("prompt", ""))
except Exception:
    print("")
' 2>/dev/null || true)"

LPROMPT="$(printf '%s' "$PROMPT" | tr '[:upper:]' '[:lower:]')"

emit_continue() {
  printf '{"continue":true}\n'
  exit 0
}

emit_context() {
  local ctx="$1"
  python3 -c '
import json, sys
print(json.dumps({"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext": sys.argv[1]}}))
' "$ctx"
  exit 0
}

freshness_note() {
  local path="$1"
  local label="$2"
  if [ ! -f "$path" ]; then
    # Distinguish a NEVER-BUILT graph from one that was built and then LOST.
    # graphify-out/ is gitignored, so a lost graph leaves NO trace while the
    # second-brain-mapping state file still records that graphify ran. That
    # silent gap is exactly how a graph can vanish for weeks unnoticed — a bare
    # "missing" note reads identically whether it was never built or just lost.
    local _state="$VAULT_ROOT/⚙️ Meta/.second-brain-mapping-state.json"
    if [ -f "$_state" ] && python3 -c "import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get('phase_2_graphify') else 1)" "$_state" 2>/dev/null; then
      printf "⚠ LOST — this graph was built before but is GONE now (gitignored + untracked, no git trace). Your SOURCE is intact; rebuild it: /graphify --update on %s" "$label"
    else
      printf "missing (run /graphify on %s to build it)" "$label"
    fi
    return
  fi
  local mtime now days
  # Try macOS first, fall back to Linux
  mtime=$(stat -f %m "$path" 2>/dev/null || stat -c %Y "$path" 2>/dev/null || echo 0)
  now=$(date +%s)
  days=$(( (now - mtime) / 86400 ))
  if [ "$days" -gt "$STALE_DAYS" ]; then
    printf "STALE — last updated %d days ago, run /graphify --update on %s before trusting it" "$days" "$label"
  else
    printf "updated %d day(s) ago" "$days"
  fi
}

if [ -z "$LPROMPT" ]; then
  emit_continue
fi

# Match against PRIMARY scope first
if printf '%s' "$LPROMPT" | grep -Eiq "$PRIMARY_PATTERN"; then
  freshness="$(freshness_note "$PRIMARY_GRAPH" "$PRIMARY_LABEL")"
  emit_context "GRAPH ROUTING (keyword match: primary scope) — This prompt mentions primary-vault concepts. Before answering, READ the graph at \"$PRIMARY_GRAPH\" — ${freshness}. It is the structural summary (god nodes, communities, hyperedges) of the primary vault. After reading, drill into the named source files only as needed."
fi

# Match against SECONDARY scope (if configured)
if [ -n "$SECONDARY_GRAPH" ] && printf '%s' "$LPROMPT" | grep -Eiq "$SECONDARY_PATTERN"; then
  freshness="$(freshness_note "$SECONDARY_GRAPH" "$SECONDARY_LABEL")"
  emit_context "GRAPH ROUTING (keyword match: secondary scope) — This prompt mentions ${SECONDARY_LABEL} concepts. Before answering, READ the graph at \"$SECONDARY_GRAPH\" — ${freshness}. It is the authoritative source for ${SECONDARY_LABEL} content. Do NOT use the primary vault graph for this — its coverage of ${SECONDARY_LABEL} content may be partial. After reading, drill into named source files only as needed."
fi

emit_continue
