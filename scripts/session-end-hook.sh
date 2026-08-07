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
# Session End Hook — Stop hook backstop + finalization.
# Called by Claude Code Stop hook (every turn end).
#
# Layered architecture:
#   Layer 1: hooks/detect-closing-signal.py (UserPromptSubmit) detects close,
#            pre-resolves paths, writes marker, pre-builds session shell,
#            injects cascade context.
#   Layer 2: model writes captures using injected paths.
#   Layer 3 (this file): finalize — Haiku fallback if model bailed,
#            aggregators, retention, git snapshot.
#
# Flow:
#   1. Append timestamp to Session Log (every turn, cheap).
#   2. If marker exists for this session_id: this is a close turn.
#      a. If session file body is empty: model bailed, fire Haiku fallback.
#      b. Run aggregators (rebuild Last Session.md + Decision Log.md).
#      c. Targeted git snapshot (if vault is git-tracked + has remote OR is local snapshot).
#      d. Retention cleanup (stubs >7d delete, substantive >7d archive).
#      e. Clean up marker file.
#   3. If no marker: normal turn end, just timestamp + retention. Skip everything else.
#
# Performance: cheap path (no marker) ~50ms. Close path 5-10s including aggregators.
# Failures fail-open: never block the user, log to ~/.claude/logs/session-close-errors.log.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resource-awareness + close-cascade serialization (load gate + mutex), shared
# with the daily-maintenance cron via _session_close_guard.sh. FAIL-OPEN: if the
# guard is absent (older install) define no-op fallbacks so close never breaks -
# never "high" (so never defer) and always "acquired" (so no serialization).
CLOSE_GUARD="$SCRIPT_DIR/_session_close_guard.sh"
if [ -f "$CLOSE_GUARD" ]; then
  # shellcheck source=/dev/null
  . "$CLOSE_GUARD"
else
  close_resource_high() { return 1; }
  close_load_per_core() { echo "0"; }
  close_mutex_acquire() { return 0; }
  close_mutex_release() { :; }
fi

# Resolve VAULT to the MAIN vault root, never a worktree.
#
# If $SCRIPT_DIR matches `.../<vault>/.claude/worktrees/<slug>/.../scripts`,
# the script is executing from a worktree's own checkout. Walking up via
# `$SCRIPT_DIR/../..` resolves to the worktree path, not the main vault,
# and every subsequent write (META_DIR, SESSIONS_DIR, git add) lands on
# the `claude/<slug>` branch instead of `master`. The worktree-prune cron
# then deletes those branches assuming the work is on master — it isn't —
# and the commits become orphaned, recoverable only via reflog until gc.
# Symptom: session files silently disappear when the worktree is archived.
# Mirrors the fix in hooks/post-tool-use-learnings.py (commit 78f4a37).
resolve_main_vault() {
  local p="$1"
  case "$p" in
    *"/.claude/worktrees/"*)
      # Strip from `.claude/worktrees/<slug>/...` onward → main vault root.
      echo "${p%%/.claude/worktrees/*}"
      ;;
    *)
      echo "$p"
      ;;
  esac
}

if [ -n "${VAULT_ROOT:-}" ]; then
  VAULT="$VAULT_ROOT"
else
  CANDIDATE="$(cd "$SCRIPT_DIR/../.." && pwd)"
  VAULT="$(resolve_main_vault "$CANDIDATE")"
fi

# Read hook input (Stop hook contract: {session_id, transcript_path, cwd, ...})
HOOK_INPUT="$(cat || true)"
SESSION_ID=$(echo "$HOOK_INPUT" | python3 -c "import json,sys; d=json.loads(sys.stdin.read() or '{}'); print(d.get('session_id',''))" 2>/dev/null || echo "")
TRANSCRIPT_PATH=$(echo "$HOOK_INPUT" | python3 -c "import json,sys; d=json.loads(sys.stdin.read() or '{}'); print(d.get('transcript_path',''))" 2>/dev/null || echo "")

# Auto-detect the Meta folder via the shared resolver, which prefers the variant
# containing a known human-memory subfolder. This stops a machine-memory "Meta/"
# (Learnings/, created by the closed-loop capture) from shadowing the human
# "⚙️ Meta/" just because plain "Meta" sorts before the emoji prefix. See
# scripts/_meta_resolver.py.
META_DIR="$(python3 "$SCRIPT_DIR/_meta_resolver.py" "$VAULT" Sessions Decisions 2>/dev/null || true)"
[ -z "$META_DIR" ] && META_DIR="$VAULT/Meta"

SESSIONS_DIR="$META_DIR/Sessions"
ARCHIVE_DIR="$SESSIONS_DIR/Archive"
SESSION_LOG="$META_DIR/Session Log.md"
AGGREGATE_SESSIONS="$META_DIR/scripts/aggregate-sessions.py"
AGGREGATE_DECISIONS="$META_DIR/scripts/aggregate-decisions.py"
ERROR_LOG="$HOME/.claude/logs/session-close-errors.log"
mkdir -p "$HOME/.claude/logs"

DATE=$(date +%Y-%m-%d)
TIME=$(date +%H:%M)
TIMESTAMP_FILE=$(date +%Y-%m-%dT%H-%M)

# Cutoff for retention (7 days back). BSD date (macOS) uses -v, GNU uses -d.
if date -v-7d +%Y-%m-%d >/dev/null 2>&1; then
  CUTOFF=$(date -v-7d +%Y-%m-%d)
else
  CUTOFF=$(date -d '7 days ago' +%Y-%m-%d)
fi

# Derive worktree name
WORKTREE_NAME=""
PWD_PATH="$(pwd)"
case "$PWD_PATH" in
  *"/.claude/worktrees/"*)
    WORKTREE_NAME=$(echo "$PWD_PATH" | sed -n 's|.*/\.claude/worktrees/\([^/]*\).*|\1|p')
    ;;
esac
if [ -z "$WORKTREE_NAME" ] && [ -f "$PWD_PATH/.git" ]; then
  GITDIR=$(grep -o 'worktrees/[^ ]*' "$PWD_PATH/.git" 2>/dev/null | head -1)
  if [ -n "$GITDIR" ]; then
    WORKTREE_NAME=$(echo "$GITDIR" | sed 's|worktrees/||' | tr -d '[:space:]')
  fi
fi
[ -z "$WORKTREE_NAME" ] && WORKTREE_NAME="main"

mkdir -p "$SESSIONS_DIR" "$ARCHIVE_DIR"

log_err() {
  echo "[$(date +'%Y-%m-%dT%H:%M:%S')] $1" >> "$ERROR_LOG" 2>/dev/null || true
}

# === Step 1: Cheap-path work — runs every turn ===

# Append turn-end timestamp to Session Log
echo "- $DATE $TIME — turn ended ($WORKTREE_NAME)" >> "$SESSION_LOG" 2>/dev/null || true

# Retention cleanup (cheap, file-system only)
for f in "$SESSIONS_DIR"/*.md; do
  [ -f "$f" ] || continue
  fname=$(basename "$f")
  fdate="${fname:0:10}"
  [[ "$fdate" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || \
  [[ "$fdate" =~ ^[0-9]{8} ]] || continue
  if [[ "$fdate" =~ ^[0-9]{8}$ ]]; then
    fdate="${fdate:0:4}-${fdate:4:2}-${fdate:6:2}"
  fi
  [[ "$fdate" > "$CUTOFF" || "$fdate" == "$CUTOFF" ]] && continue
  if grep -q 'session_label: "update pending"' "$f" 2>/dev/null; then
    rm "$f" 2>/dev/null || true
  else
    mv "$f" "$ARCHIVE_DIR/" 2>/dev/null || true
  fi
done

# === Step 2: Marker check — only fires on a close turn ===

MARKER=""
if [ -n "$SESSION_ID" ]; then
  MARKER="$HOME/.claude/.closing-signal-${SESSION_ID}.json"
fi

if [ -z "$MARKER" ] || [ ! -f "$MARKER" ]; then
  # No close signal this turn — emit minimal continue and exit.
  echo '{"continue":true,"suppressOutput":true}'
  exit 0
fi

# This is a close turn. Read marker for the session file path.
SESSION_FILE=$(python3 -c "
import json, sys
try:
    with open('$MARKER') as f:
        d = json.load(f)
    print(d.get('session_file', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

IS_TRIVIAL=$(python3 -c "
import json
try:
    with open('$MARKER') as f:
        d = json.load(f)
    print('1' if d.get('is_trivial') else '0')
except Exception:
    print('0')
" 2>/dev/null || echo "0")

# === Step 2a: Trivial close — clean up marker, no further work ===

if [ "$IS_TRIVIAL" = "1" ]; then
  rm -f "$MARKER" 2>/dev/null || true
  echo '{"continue":true,"suppressOutput":true}'
  exit 0
fi

# === Step 2b: Haiku fallback if model bailed ===

FALLBACK_SCRIPT="$SCRIPT_DIR/session-close-fallback.py"
if [ -f "$FALLBACK_SCRIPT" ] && [ -n "$TRANSCRIPT_PATH" ]; then
  python3 "$FALLBACK_SCRIPT" \
    --session-id "$SESSION_ID" \
    --transcript-path "$TRANSCRIPT_PATH" \
    >/dev/null 2>>"$ERROR_LOG" || log_err "fallback exited non-zero"
fi

# === Step 2b2: Resource gate + close-cascade mutex ===
# The aggregators (2c) + git snapshot (2d) below are the heavy part of the close
# path - on a mature vault the git index is megabytes and `git commit` rewrites
# it. DEFER them when the machine is already saturated, or while a sibling close
# holds the cascade mutex, so we never pile git IO onto an overloaded system at
# close. Nothing is lost: the captured session file is already on disk (model or
# Haiku fallback wrote it), and the daily cron vault-daily-maintenance.sh re-runs
# the aggregators and commits any session / decision / captures files this close
# left uncommitted. The cheap path (Step 1 timestamp + retention) already ran.
CLOSE_DEFER=0
if close_resource_high; then
  CLOSE_DEFER=1
  log_err "close: deferred aggregation + snapshot - load $(close_load_per_core)/core >= ${CLOSE_MAX_LOAD_PER_CORE:-3.0}; daily maintenance will catch up"
elif ! close_mutex_acquire 20; then
  CLOSE_DEFER=1
  log_err "close: deferred aggregation + snapshot - a sibling close holds the cascade mutex; daily maintenance will catch up"
fi

if [ "$CLOSE_DEFER" = "0" ]; then

# === Step 2c: Aggregators (foreground, sequential) ===

if [ -f "$AGGREGATE_SESSIONS" ]; then
  VAULT_ROOT="$VAULT" python3 "$AGGREGATE_SESSIONS" >/dev/null 2>>"$ERROR_LOG" \
    || log_err "aggregate-sessions failed"
fi
if [ -f "$AGGREGATE_DECISIONS" ]; then
  VAULT_ROOT="$VAULT" python3 "$AGGREGATE_DECISIONS" >/dev/null 2>>"$ERROR_LOG" \
    || log_err "aggregate-decisions failed"
fi

# === Step 2d: Targeted git snapshot (only if vault is git-tracked) ===
# Conservative: only commit if we're inside a git repo, and only stage the
# session file + any decisions touched. Never `git add -A`. Never push (vaults
# are typically local-only snapshot repos).

if [ -d "$VAULT/.git" ] || git -C "$VAULT" rev-parse --git-dir >/dev/null 2>&1; then
  # Wait for any concurrent index lock (up to 60s, then give up gracefully)
  WAITED=0
  while [ -f "$VAULT/.git/index.lock" ] && [ $WAITED -lt 60 ]; do
    sleep 2
    WAITED=$((WAITED + 2))
  done

  if [ ! -f "$VAULT/.git/index.lock" ]; then
    # Stage only paths we know about
    PATHS_TO_STAGE=()
    [ -f "$SESSION_FILE" ] && PATHS_TO_STAGE+=("$SESSION_FILE")
    # Recently-touched decision files (within this minute)
    while IFS= read -r decision_file; do
      [ -f "$decision_file" ] && PATHS_TO_STAGE+=("$decision_file")
    done < <(find "$META_DIR/Decisions" -maxdepth 1 -name "${TIMESTAMP_FILE:0:10}*.md" -mmin -10 2>/dev/null)

    # Captures file if modified
    if [ -f "$META_DIR/Session Captures.md" ]; then
      if [ "$(find "$META_DIR/Session Captures.md" -mmin -10 2>/dev/null)" ]; then
        PATHS_TO_STAGE+=("$META_DIR/Session Captures.md")
      fi
    fi

    # Aggregator outputs
    [ -f "$META_DIR/Last Session.md" ] && PATHS_TO_STAGE+=("$META_DIR/Last Session.md")
    [ -f "$META_DIR/Decision Log.md" ] && PATHS_TO_STAGE+=("$META_DIR/Decision Log.md")

    if [ ${#PATHS_TO_STAGE[@]} -gt 0 ]; then
      cd "$VAULT" && {
        git add "${PATHS_TO_STAGE[@]}" 2>>"$ERROR_LOG" \
          && git diff --cached --quiet \
          || git commit -m "session: ${WORKTREE_NAME} ${DATE}" >/dev/null 2>>"$ERROR_LOG" \
          || log_err "git snapshot commit failed"
      }
    fi
  else
    log_err "git index.lock held >60s; skipped snapshot"
  fi
fi

close_mutex_release
fi   # end Step 2b2 resource gate (aggregators + snapshot)

# === Step 3: Clean up marker ===

rm -f "$MARKER" 2>/dev/null || true

# === Done — emit minimal continue ===
echo '{"continue":true,"suppressOutput":true}'
exit 0
