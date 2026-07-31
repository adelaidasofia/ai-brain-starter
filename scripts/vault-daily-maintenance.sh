#!/bin/bash
# vault-daily-maintenance.sh - heavy vault maintenance, kept OFF the interactive
# session-close path.
#
# Two jobs:
#   1. RECONCILE (data safety): re-run the aggregators and commit any session /
#      decision / captures files a session-close left uncommitted. The close
#      hook (session-end-hook.sh) DEFERS its git snapshot when the machine is
#      saturated or a sibling close holds the cascade mutex; this is the catch-up
#      that snapshots that work within a day so nothing is lost.
#   2. HEAVY HYGIENE (best effort): the full-tree / git-log-walking scripts the
#      substrate ships but that are too heavy to run at every close -
#      drift-detection, check-rule-conflicts --scan-all, passive-capture. They
#      run here once a day, resource-gated, serialized, and at low IO/CPU
#      priority (see templates/launchd/com.abs.vault-daily-maintenance.plist).
#
# Resource-aware: shares the load gate + close-cascade mutex with the close hook
# via _session_close_guard.sh. If 1-min loadavg/core >= CLOSE_MAX_LOAD_PER_CORE
# (default 3.0) it skips the heavy work and logs "deferred - high load"; the next
# daily run (or a --force run) catches up. Serialized on the same mutex the close
# hook uses, so a heavy daily pass never overlaps a live close cascade.
#
# Usage:
#   vault-daily-maintenance.sh --vault-root /path/to/vault          # load-gated
#   VAULT_ROOT=/path/to/vault vault-daily-maintenance.sh            # env form
#   vault-daily-maintenance.sh --vault-root /path --force           # ignore load gate
#   vault-daily-maintenance.sh --vault-root /path --reconcile-only  # skip heavy hygiene
#
# Exit codes: 0 = ran (or cleanly deferred); 1 only on its own setup error
# (missing vault root). Individual maintenance steps never abort the run.

set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

FORCE=0
RECONCILE_ONLY=0
VAULT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=1 ;;
    --reconcile-only) RECONCILE_ONLY=1 ;;
    --vault-root) shift; VAULT="${1:-}" ;;
    --vault-root=*) VAULT="${1#--vault-root=}" ;;
    *) ;;
  esac
  shift
done
[ -z "$VAULT" ] && VAULT="${VAULT_ROOT:-}"

if [ -z "$VAULT" ] || [ ! -d "$VAULT" ]; then
  echo "vault-daily-maintenance: set --vault-root PATH or VAULT_ROOT env to an existing vault" >&2
  exit 1
fi

# Shared load gate + close-cascade mutex. Fail-open if the guard is absent.
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

# Auto-detect the Meta folder via the shared resolver (prefers the variant
# containing a known human-memory subfolder), like the close hook. See
# scripts/_meta_resolver.py.
META_DIR="$(python3 "$SCRIPT_DIR/_meta_resolver.py" "$VAULT" Sessions Decisions 2>/dev/null || true)"
[ -z "$META_DIR" ] && META_DIR="$VAULT/Meta"

LOG_DIR="$META_DIR/logs"
mkdir -p "$LOG_DIR" 2>/dev/null
LOG="$LOG_DIR/vault-daily-maintenance.log"

log() { printf '%s\n' "$*"; printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$LOG" 2>/dev/null; }

LOAD_PER_CORE=$(close_load_per_core)
log "=== vault-daily-maintenance @ $(date -u +%Y-%m-%dT%H:%M:%SZ) (load=${LOAD_PER_CORE}/core, force=$FORCE, reconcile_only=$RECONCILE_ONLY) ==="

# --- ensure Claude Code memory lives in the vault (idempotent self-heal) ----
# Installs created before the memory-symlink fix have their memory stranded in
# ~/.claude/projects/<key>/memory/, invisible in Obsidian. Re-link it into the
# vault so the brain accumulates where the user can see it. Near-free + safe to
# run daily; runs BEFORE the load gate because it is trivially cheap and the
# durability it restores matters more than the few stats it costs.
LINK_MEM="$SCRIPT_DIR/link-agent-memory.py"
if [ -f "$LINK_MEM" ]; then
  if python3 "$LINK_MEM" --vault "$VAULT" --quiet >> "$LOG" 2>&1; then
    log "agent-memory link verified -> vault"
  else
    log "WARNING: agent-memory link failed (memory may strand in ~/.claude/); see log above"
  fi
fi

# --- load gate -------------------------------------------------------------
if [ $FORCE -eq 0 ] && close_resource_high; then
  log "DEFERRED - high load (${LOAD_PER_CORE}/core >= ${CLOSE_MAX_LOAD_PER_CORE:-3.0}). Next daily run will catch up."
  exit 0
fi

# --- idle gate (MYC-2363) ---------------------------------------------------
# The load average says the machine is not busy; it does not say the machine is
# a good place to spend a few minutes of disk churn right now. Two cases where
# it is not, both invisible to loadavg:
#   * ON BATTERY - a GC pass is never worth someone's remaining charge. It is a
#     daily job; tomorrow (or the next time they plug in) is soon enough.
#   * SOMEONE IS TYPING - felt footprint is the point. Work that lands while the
#     user is mid-task is exactly the "the install made my machine slower"
#     experience this whole track exists to prevent.
# Both fail OPEN: a machine we cannot interrogate runs the pass as before.
# --force overrides (the manual "run it now" path).
on_battery() {
  if command -v pmset >/dev/null 2>&1; then
    pmset -g batt 2>/dev/null | grep -q "Now drawing from 'Battery Power'" && return 0
    return 1
  fi
  for s in /sys/class/power_supply/BAT*/status; do
    [ -r "$s" ] && grep -qi discharging "$s" && return 0
  done
  return 1
}

# Seconds since the last keyboard/mouse event, or empty when unknowable.
user_idle_seconds() {
  if command -v ioreg >/dev/null 2>&1; then
    ioreg -c IOHIDSystem 2>/dev/null \
      | awk '/HIDIdleTime/ {print int($NF/1000000000); exit}'
  fi
}

if [ $FORCE -eq 0 ]; then
  if on_battery; then
    log "DEFERRED - on battery. A daily GC pass is not worth someone's charge; next run catches up."
    exit 0
  fi
  IDLE_S="$(user_idle_seconds)"
  IDLE_FLOOR="${MAINT_MIN_USER_IDLE_SEC:-300}"
  if [ -n "$IDLE_S" ] && [ "$IDLE_S" -lt "$IDLE_FLOOR" ] 2>/dev/null; then
    log "DEFERRED - someone is at the keyboard (idle ${IDLE_S}s < ${IDLE_FLOOR}s). Next daily run will catch up."
    exit 0
  fi
fi

# --- serialize against a live close cascade --------------------------------
if ! close_mutex_acquire 60; then
  log "DEFERRED - a close cascade (or another maintenance pass) holds the mutex. Next daily run will catch up."
  exit 0
fi
log "acquired close-cascade mutex ($$)"

run() {
  # run <label> <command...> - runs at low priority, logs a tail, never aborts.
  local label="$1"; shift
  local out rc
  out=$(nice -n 10 "$@" 2>&1); rc=$?
  printf '%s\n' "$out" | tail -3
  log "[$label] rc=$rc :: $(printf '%s\n' "$out" | tail -1)"
}

# === RECONCILE (data safety): aggregators + commit deferred close artifacts ===
AGG_SESSIONS="$SCRIPT_DIR/aggregate-sessions.py"
AGG_DECISIONS="$SCRIPT_DIR/aggregate-decisions.py"
[ -f "$AGG_SESSIONS" ]  && VAULT_ROOT="$VAULT" run "aggregate-sessions"  /usr/bin/env python3 "$AGG_SESSIONS"
[ -f "$AGG_DECISIONS" ] && VAULT_ROOT="$VAULT" run "aggregate-decisions" /usr/bin/env python3 "$AGG_DECISIONS"

# Commit any close artifacts left uncommitted by a load-deferred close. Targeted
# paths only - NEVER `git add -A` (vaults are commonly 10k-60k+ files).
if [ -d "$VAULT/.git" ] || git -C "$VAULT" rev-parse --git-dir >/dev/null 2>&1; then
  WAITED=0
  while [ -f "$VAULT/.git/index.lock" ] && [ $WAITED -lt 60 ]; do sleep 2; WAITED=$((WAITED + 2)); done
  if [ ! -f "$VAULT/.git/index.lock" ]; then
    PATHS=()
    for p in "$META_DIR/Sessions" "$META_DIR/Decisions" \
             "$META_DIR/Last Session.md" "$META_DIR/Decision Log.md" \
             "$META_DIR/Session Captures.md"; do
      [ -e "$p" ] && PATHS+=("$p")
    done
    if [ ${#PATHS[@]} -gt 0 ]; then
      ( cd "$VAULT" && git add -- "${PATHS[@]}" 2>>"$LOG" \
          && { git diff --cached --quiet \
               || git commit -m "maint: reconcile deferred session-close $(date +%Y-%m-%d)" >/dev/null 2>>"$LOG"; } )
      log "[reconcile-commit] staged ${#PATHS[@]} close-artifact path(s)"
    fi
  else
    log "[reconcile-commit] SKIPPED - git index.lock held >60s"
  fi
fi

if [ $RECONCILE_ONLY -eq 1 ]; then
  close_mutex_release
  log "=== reconcile-only COMPLETE @ $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  exit 0
fi

# === HEAVY HYGIENE (best effort, low priority, never abort) ==================
# The full-tree / git-log-walking scripts that are too heavy for the close path.
# Each runs only if present and routes VAULT_ROOT to the user's vault.

# Drift detection (30-day git log walk; needs a git-tracked vault).
DRIFT="$SCRIPT_DIR/drift-detection.py"
if [ -f "$DRIFT" ] && { [ -d "$VAULT/.git" ] || git -C "$VAULT" rev-parse --git-dir >/dev/null 2>&1; }; then
  VAULT_ROOT="$VAULT" run "drift-detection" /usr/bin/env python3 "$DRIFT"
fi

# Rule-conflicts keyword scan (full-tree --scan-all).
RULECONF="$SCRIPT_DIR/check-rule-conflicts.py"
[ -f "$RULECONF" ] && VAULT_ROOT="$VAULT" run "check-rule-conflicts" /usr/bin/env python3 "$RULECONF" --scan-all

# Session-close phase contract: this install's session-close rule vs the numbered
# cascade detect-closing-signal.py injects at close. Both reach the model, so a
# number that means different things in the two is worse than no number — and the
# drift is silent, because each file reads fine on its own. STRUCTURAL tier only
# (duplicate numbers, orphan sub-phases): an installed rule is customised prose,
# and pinning its wording would fail on legitimate edits, which is how a check
# earns itself an --ignore. Skips cleanly when either file is absent.
PHASECHECK="$SCRIPT_DIR/check-close-phase-contract.py"
CLOSE_RULE="$META_DIR/rules/session-close.md"
CLOSE_CASCADE="$SCRIPT_DIR/../hooks/detect-closing-signal.py"
[ -f "$PHASECHECK" ] && [ -f "$CLOSE_RULE" ] && [ -f "$CLOSE_CASCADE" ] && \
  run "close-phase-contract" /usr/bin/env python3 "$PHASECHECK" \
    --rule "$CLOSE_RULE" --cascade "$CLOSE_CASCADE"

# Passive capture of the day's content (full-tree scan of today's transcripts).
PASSIVE="$SCRIPT_DIR/passive-capture.py"
[ -f "$PASSIVE" ] && VAULT_ROOT="$VAULT" run "passive-capture" /usr/bin/env python3 "$PASSIVE" --scan-today

# Vault-relocation watchdog (mode 2): scan the install's recorded move(s) for drift
# back to an old path (executed residuals / recreated old dir / missing vault root).
# Heavy (auto-discovers ~/dev + the vault at canonical ref) so it lives here, not on
# SessionStart. Writes the verdict cache the SessionStart surfacer reads; a no-op when
# no relocation was ever recorded. rc=1 (ALARM) is logged, never fatal (set +e + run).
RELOCWATCH="$SCRIPT_DIR/relocate-sweep.py"
[ -f "$RELOCWATCH" ] && run "relocate-watch" /usr/bin/env python3 "$RELOCWATCH" --watch

# === AUTO-GC (MYC-2363) =====================================================
# The reclaim tier. Every piece below already existed and every one was OPT-IN,
# so on a default install NOTHING ever ran them and the machine only ever got
# fuller. An install should leave the machine BETTER over time, so the GC runs
# by default — behind the same load + battery + idle gates as everything above.
#
# There is no opt-in toggle: a toggle that gates the VALUE means the default
# install stays dirty, which is the bug. ABS_NO_AUTO_GC=1 turns it OFF for a
# power user who wants to drive reclaim by hand.
if [ "${ABS_NO_AUTO_GC:-0}" = "1" ]; then
  log "[auto-gc] SKIPPED - ABS_NO_AUTO_GC=1"
else
  # LOOK-BEFORE-DELETE (client trust). Reclaiming a regenerable CACHE needs no
  # ceremony — nobody mourns a cache, and a fast-forward destroys nothing. But
  # the two legs that DELETE a person's git branches and directories must not do
  # that on day one, before they have ever seen what this pass does. Those two
  # REPORT on the first run and APPLY from the second onward.
  #
  # This does NOT gate the VALUE behind an opt-in — that would leave the default
  # install dirty forever, the bug MYC-2363 fixed. Nothing is asked of the user,
  # no flag is needed, and the reclaim still arrives automatically: one cycle
  # later for the destructive legs, with a report banked first.
  # ABS_GC_APPLY_NOW=1 skips the dry first pass.
  GC_SEEN_MARKER="$HOME/.claude/.auto-gc-first-report"
  if [ -f "$GC_SEEN_MARKER" ] || [ "${ABS_GC_APPLY_NOW:-0}" = "1" ]; then
    DESTRUCTIVE_MODE="--apply"
  else
    DESTRUCTIVE_MODE=""
    log "[auto-gc] FIRST RUN — branch/worktree reclaim REPORTS ONLY this pass."
    log "[auto-gc] Review below; the next run applies. Force now: ABS_GC_APPLY_NOW=1"
  fi

  # 1. Vault scratch worktrees + orphan dirs + orphan branches + old snapshots.
  WTPRUNE="$SCRIPT_DIR/worktree-prune.sh"
  [ -f "$WTPRUNE" ] && VAULT_ROOT="$VAULT" run "worktree-prune" /bin/bash "$WTPRUNE"

  # 2. Stale graph-cache entries (regenerable by construction). The corpus is
  #    the WHOLE vault ('.') on purpose: the tool drops cache keys that match no
  #    current file in the corpus, so the widest corpus is the SAFEST one — it
  #    can only under-prune, never delete a key some other corpus still uses.
  #    No-ops (rc!=0, logged) when the vault has no graph cache at all.
  GCACHE="$SCRIPT_DIR/graphify_prune_stale_cache.py"
  [ -f "$GCACHE" ] && run "graphify-cache-prune" \
      /usr/bin/env python3 "$GCACHE" . --vault-root "$VAULT"

  # 3. Code-repo branch/worktree/stash drift. --apply reaps ONLY what is
  #    provably preserved on a remote; un-backed-up work is surfaced, never
  #    touched, and every deletion writes a recovery manifest.
  REAPER="$SCRIPT_DIR/dev-repo-reaper.py"
  [ -f "$REAPER" ] && run "dev-repo-reaper" /usr/bin/env python3 "$REAPER" $DESTRUCTIVE_MODE

  # 4. Orphaned <dev-root>/<repo>-<slug> session worktrees (MYC-587/677).
  #    Nothing swept these before: the vault pruner deliberately excludes them.
  #    Dirty ones are snapshotted before removal; un-backed-up ones are kept.
  WTREAP="$SCRIPT_DIR/dev-worktree-prune.py"
  [ -f "$WTREAP" ] && run "dev-worktree-prune" /usr/bin/env python3 "$WTREAP" $DESTRUCTIVE_MODE

  # 5. Bare ~/dev hub freshness — fast-forward the clean ones so a recon read is
  #    never weeks stale (MYC-677 STALE-BARE-CHECKOUT-READ). Dirty / diverged
  #    hubs are surfaced by the SessionStart hook, never force-moved here.
  # 6. Un-backed-up drift report -> state file. The EXPENSIVE leg (fleet-wide
  #    git I/O) lives here so the SessionStart surface can render it for free
  #    instead of paying ~15s and a process on every interactive start.
  DRIFT="$SCRIPT_DIR/dev-drift-report.py"
  [ -f "$DRIFT" ] && run "dev-drift-report" /usr/bin/env python3 "$DRIFT" --write-state

  HUBREFRESH="$SCRIPT_DIR/dev-hub-refresh.py"
  [ -f "$HUBREFRESH" ] && run "dev-hub-refresh" /usr/bin/env python3 "$HUBREFRESH" --apply
fi

# Bank the first report so the NEXT pass applies. Written after the legs ran, so
# a crashed first pass reports again rather than silently escalating to delete on
# a run nobody ever saw.
if [ "${ABS_NO_AUTO_GC:-0}" != "1" ] && [ -n "${GC_SEEN_MARKER:-}" ] && [ ! -f "$GC_SEEN_MARKER" ]; then
  mkdir -p "$(dirname "$GC_SEEN_MARKER")" 2>/dev/null
  date -u +%Y-%m-%dT%H:%M:%SZ > "$GC_SEEN_MARKER" 2>/dev/null
fi

close_mutex_release
log "=== vault-daily-maintenance COMPLETE @ $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
exit 0
