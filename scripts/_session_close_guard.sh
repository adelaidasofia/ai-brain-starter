#!/bin/bash
# _session_close_guard.sh - shared resource-awareness + serialization for the
# session-close path (session-end-hook.sh) and the daily-maintenance cron
# (vault-daily-maintenance.sh).
#
# SOURCE this file; do not execute it. ONE source of truth for the load gate +
# the close-cascade mutex so the two callers can never drift apart.
#
# Why this exists: on a mature Obsidian vault (10k-60k+ tracked files) the
# session-close git snapshot is the one genuinely heavy operation on the
# interactive close path - `git add` + `git commit` read and rewrite an index
# that is megabytes large. When the machine is already saturated (many parallel
# sessions, a sync client churning, a graph build running), piling that IO on at
# close can pin or crash the machine right when the user is wrapping up. The fix
# is two primitives:
#   1. a LOAD GATE so heavy work is skipped when the machine is already saturated
#      (the daily-maintenance cron catches up later), and
#   2. a close-cascade MUTEX so two concurrent closes never hammer the git index
#      at the same time.
#
# Cross-platform: the load read works on Linux (/proc/loadavg) and macOS/BSD
# (sysctl vm.loadavg). Every read FAILS OPEN - any error yields a load of 0, so
# a platform we cannot read load on (or a broken sysctl) never wedges the close;
# it simply never defers.

# --- load gate -------------------------------------------------------------
# Normalized 1-min load average per core. At or above the threshold the machine
# is saturated; skip heavy work so we do not add to the fire. Override with the
# CLOSE_MAX_LOAD_PER_CORE env var (e.g. 0 forces "always high", a huge value
# forces "never high" - both used by the test suite).
: "${CLOSE_MAX_LOAD_PER_CORE:=3.0}"

close_ncpu() {
  # Echoes the online CPU count. getconf is the most portable; fall back to
  # nproc (Linux) / sysctl (macOS); default 1 on any failure.
  local n=""
  n=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo "")
  case "$n" in ''|*[!0-9]*) n="" ;; esac
  if [ -z "$n" ]; then
    n=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 1)
  fi
  case "$n" in ''|*[!0-9]*) n=1 ;; esac
  [ "$n" -lt 1 ] && n=1
  echo "$n"
}

close_load_1min() {
  # Echoes the raw 1-min load average, cross-platform. Empty on any read error.
  local one=""
  if [ -r /proc/loadavg ]; then
    one=$(awk '{print $1}' /proc/loadavg 2>/dev/null)          # Linux
  else
    # macOS/BSD: `sysctl -n vm.loadavg` -> "{ 4.90 6.59 11.53 }" (field 2 = 1-min)
    one=$(sysctl -n vm.loadavg 2>/dev/null | awk '{print $2}')
  fi
  case "$one" in ''|*[!0-9.]*) one="" ;; esac      # keep only a plain number
  echo "$one"
}

close_load_per_core() {
  # Echoes the 1-min loadavg divided by ncpu, e.g. "0.38". 0 on any read error
  # (fail-open: a load we cannot read must never wedge the close).
  local one ncpu
  one=$(close_load_1min)
  [ -z "$one" ] && { echo "0"; return; }
  ncpu=$(close_ncpu)
  awk -v o="$one" -v n="$ncpu" 'BEGIN{ if (n < 1) n = 1; printf "%.2f", o / n }'
}

close_resource_high() {
  # Returns 0 (true) when normalized 1-min load >= CLOSE_MAX_LOAD_PER_CORE.
  local per_core
  per_core=$(close_load_per_core)
  awk -v p="$per_core" -v t="$CLOSE_MAX_LOAD_PER_CORE" 'BEGIN{ exit !((p + 0) >= (t + 0)) }'
}

# --- close-cascade mutex ---------------------------------------------------
# `set -C` (noclobber) + redirect is atomic on a local filesystem. flock(1) is
# absent on stock macOS, so we use the portable noclobber primitive. Serializes
# heavy close/maintenance passes so two never hammer the git index concurrently.
CLOSE_MUTEX="${CLOSE_MUTEX:-${TMPDIR:-/tmp}/abs-close-cascade.lock}"
_CLOSE_MUTEX_HELD=0

_close_mutex_cleanup() {
  [ "$_CLOSE_MUTEX_HELD" = "1" ] && rm -f "$CLOSE_MUTEX" 2>/dev/null
}

_close_lock_mtime() {
  # Epoch mtime of $1, cross-platform. Try GNU `stat -c %Y` first, then BSD
  # `stat -f %m`, and VALIDATE the result is numeric after each attempt. We must
  # not rely on exit code alone: GNU `stat -f` means `--file-system`, so
  # `stat -f %m FILE` on Linux exits 0 with non-numeric filesystem text instead
  # of failing - if we trusted `||` that garbage would make lock_age huge and
  # the mutex would falsely reclaim a live lock on Linux.
  local m
  m=$(stat -c %Y "$1" 2>/dev/null)              # GNU/Linux
  case "$m" in ''|*[!0-9]*) m=$(stat -f %m "$1" 2>/dev/null) ;; esac   # BSD/macOS
  case "$m" in ''|*[!0-9]*) m="" ;; esac        # neither gave a plain integer
  echo "$m"
}

close_mutex_acquire() {
  # arg1: max seconds to wait (default 20). Returns 0 if acquired, 1 if not.
  # Reclaims a stale lock (holder PID dead, OR lock file older than 600s).
  local max_wait="${1:-20}" waited=0 held_pid now mt lock_age
  while ! (set -C; echo "$$" > "$CLOSE_MUTEX") 2>/dev/null; do
    held_pid=$(cat "$CLOSE_MUTEX" 2>/dev/null | tr -d '[:space:]')
    if [ -n "$held_pid" ] && printf '%s' "$held_pid" | grep -qE '^[0-9]+$' \
       && ! kill -0 "$held_pid" 2>/dev/null; then
      rm -f "$CLOSE_MUTEX" 2>/dev/null; continue        # dead holder -> reclaim
    fi
    now=$(date +%s)
    mt=$(_close_lock_mtime "$CLOSE_MUTEX"); [ -z "$mt" ] && mt="$now"
    lock_age=$(( now - mt ))
    if [ "$lock_age" -gt 600 ]; then
      rm -f "$CLOSE_MUTEX" 2>/dev/null; continue          # proven-orphan -> reclaim
    fi
    [ "$waited" -ge "$max_wait" ] && return 1
    sleep 2; waited=$((waited + 2))
  done
  _CLOSE_MUTEX_HELD=1
  trap _close_mutex_cleanup EXIT
  return 0
}

close_mutex_release() {
  _close_mutex_cleanup
  _CLOSE_MUTEX_HELD=0
  trap - EXIT
}

# --- real git dir + index lock ---------------------------------------------
# A vault that has been through scripts/relocate-machinery-sidecar.sh
# (docs/CLOUD_SYNC.md "Shape B") keeps its git machinery OUTSIDE the synced tree
# via `git init --separate-git-dir`. Its `$VAULT/.git` is then a one-line
# POINTER FILE, not a directory, and the real index lives in the sidecar.
#
# So NEVER build a lock path by joining `.git/index.lock` onto the vault root.
# On a relocated vault that path sits under a FILE and can never exist, so the
# check reports "free" forever and the vault-wide commit mutex is SILENTLY
# disarmed: two sessions closing at once both see "no lock" and both commit.
# Ask git instead — `rev-parse --absolute-git-dir` answers correctly whether
# `.git` is a directory, a pointer file, or a linked worktree.
#
# FAIL CLOSED. When the git dir cannot be resolved (not a repo, git missing,
# unreadable) these report LOCKED / refuse. A mutex that fails OPEN is the exact
# defect this exists to prevent. Refusing costs a deferred snapshot — the work
# stays on disk and the daily-maintenance reconcile catches up — while failing
# open costs a lost update, which nothing recovers.
: "${VAULT_GIT_LOCK_MAX_WAIT:=60}"

vault_git_dir() {
  # Echoes the ABSOLUTE git directory for the repo at $1. Empty + rc 1 on any
  # failure, so callers can distinguish "resolved" from "could not resolve".
  local root="${1:-}" gd=""
  { [ -n "$root" ] && [ -d "$root" ]; } || { echo ""; return 1; }
  # --absolute-git-dir (git >= 2.13) resolves a pointer file and a linked
  # worktree. Fall back to --git-dir on older git, absolutizing a relative
  # answer (a normal repo answers a bare ".git") against the vault root.
  gd=$(git -C "$root" rev-parse --absolute-git-dir 2>/dev/null)
  if [ -z "$gd" ]; then
    gd=$(git -C "$root" rev-parse --git-dir 2>/dev/null)
    [ -n "$gd" ] || { echo ""; return 1; }
    case "$gd" in
      /*)     ;;                              # POSIX absolute (incl. Git Bash)
      ?:/*|?:\\*) ;;                          # Windows drive-absolute
      *)      gd="$root/$gd" ;;               # relative -> anchor to the vault
    esac
  fi
  [ -d "$gd" ] || { echo ""; return 1; }
  echo "$gd"
}

vault_git_index_lock() {
  # Echoes <real-git-dir>/index.lock. Empty + rc 1 when the git dir cannot be
  # resolved. Never join .git/index.lock onto the vault root yourself.
  local gd
  gd=$(vault_git_dir "${1:-}") || { echo ""; return 1; }
  [ -n "$gd" ] || { echo ""; return 1; }
  echo "$gd/index.lock"
}

vault_git_locked() {
  # Returns 0 (LOCKED) when the real index lock is present, OR when the git dir
  # cannot be resolved (fail closed). Returns 1 (provably free) only when
  # resolution SUCCEEDED and no lock file is present.
  local lock
  lock=$(vault_git_index_lock "${1:-}") || return 0
  [ -n "$lock" ] || return 0
  [ -e "$lock" ] && return 0
  return 1
}

vault_git_wait_unlocked() {
  # Spin until the real index lock clears. $1 = vault root, $2 = max seconds
  # (default $VAULT_GIT_LOCK_MAX_WAIT). Returns 0 once provably free; 1 if it is
  # still held at the deadline or the git dir never resolved (fail closed).
  local root="${1:-}" max_wait="${2:-$VAULT_GIT_LOCK_MAX_WAIT}" waited=0
  # Validate the budget. A non-numeric value (a typo'd env var) would make the
  # deadline test below error instead of returning true, so the loop would spin
  # forever on a genuinely held lock - a hang, on the interactive close path.
  case "$max_wait" in ''|*[!0-9]*) max_wait=60 ;; esac
  vault_git_dir "$root" >/dev/null || return 1
  while vault_git_locked "$root"; do
    [ "$waited" -ge "$max_wait" ] && return 1
    sleep 2
    waited=$((waited + 2))
  done
  return 0
}
