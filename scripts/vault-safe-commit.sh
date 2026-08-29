#!/bin/bash
# exit-contract: NOT-A-CHECKER -- performs a lock-safe targeted git commit

# vault-safe-commit.sh — safe targeted commit for a large Obsidian vault.
#
# Solves lock-conflict and index-corruption problems from leaked git processes
# and concurrent Claude sessions all competing for .git/index.lock.
#
# Usage:
#   VAULT_ROOT="/path/to/vault" vault-safe-commit.sh [--kill-leaked] "commit message" path1 path2 ...
#
# Configuration:
#   VAULT_ROOT   Absolute path to the vault. Optional — when unset, falls back
#                to `git rev-parse --show-toplevel` from the current directory.
#                Set it explicitly when running from outside the vault.
#
# Flags:
#   --kill-leaked   Kill leaked git-status/diff children of Claude.app
#                   before attempting the commit. Use when you know
#                   Claude.app is actively polling this vault.
#
# Refuses: -A, --all, ., * as paths; empty path list; missing message.
#
# Lock safety:
#   1. If lock is 0 bytes AND no real write process running: stale, remove.
#   2. If lock is non-empty: read PID inside, check kill -0 $pid. If dead:
#      stale, remove. If alive: real write, wait.
#   3. Wait up to MAX_WAIT_SECONDS, then fail loudly.
#
# Vault-wide mutex: uses /tmp/vault-commit-<hash>.lock to serialize
# concurrent vault-safe-commit.sh invocations. Prevents two sessions from
# racing each other even if the index.lock check passes.
#
# The index-lock path is RESOLVED from git (vault_git_index_lock in
# _session_close_guard.sh), never assembled as "$VAULT_ROOT/.git/index.lock". A
# vault relocated by relocate-machinery-sidecar.sh has a .git POINTER FILE, so
# the assembled path can never exist and this mutex would be silently disarmed.
# An unresolvable git dir => refuse (fail closed).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Shared git-dir / index-lock resolver. FAIL CLOSED when the guard is missing:
# with no way to resolve the real lock path we cannot prove the vault is free,
# and an unprovable mutex must refuse rather than assume free.
CLOSE_GUARD="$SCRIPT_DIR/_session_close_guard.sh"
if [ -f "$CLOSE_GUARD" ]; then
    # shellcheck source=scripts/_session_close_guard.sh
    . "$CLOSE_GUARD"
else
    vault_git_index_lock() { echo ""; return 1; }
fi

# VAULT_ROOT is optional: when unset, derive it from the repo the caller is
# standing in. The session-close cascade (Phase 2b) and the block-raw-vault-git
# hook both prescribe this script WITHOUT the env var, and a hard exit there
# meant the close committed nothing AND the verify-session-close-cascade Stop
# hook then blocked the close over those same uncommitted artifacts.
if [ -z "${VAULT_ROOT:-}" ]; then
    VAULT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || true)
    if [ -z "${VAULT_ROOT}" ]; then
        echo "vault-safe-commit: VAULT_ROOT not set and cwd ($(pwd)) is not a git repo" >&2
        exit 1
    fi
fi

LOCK_FILE="$(vault_git_index_lock "${VAULT_ROOT}" || true)"
LOG_FILE="${VAULT_ROOT}/.vault-snapshot.log"
MAX_WAIT_SECONDS="${VAULT_GIT_LOCK_MAX_WAIT:-60}"
VAULT_MUTEX="/tmp/vault-commit-$(echo "${VAULT_ROOT}" | md5 2>/dev/null | cut -c1-8 || echo "${VAULT_ROOT}" | md5sum | cut -c1-8).lock"
GIT_BIN=$(command -v git)

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" >> "${LOG_FILE}"
}

die() {
    log "FAIL: $1"
    echo "vault-safe-commit: $1" >&2
    exit 1
}

# Fail closed: an unresolvable git dir means we cannot see the index lock at
# all, so we cannot prove no other writer holds it. Refuse instead of
# committing blind. (Checked here, not at assignment, so it can use die/log.)
if [ -z "${LOCK_FILE}" ]; then
    die "cannot resolve the git directory for ${VAULT_ROOT} — refusing to commit without a working index-lock mutex"
fi

# --- parse --kill-leaked flag ---
KILL_LEAKED=0
if [ "${1:-}" = "--kill-leaked" ]; then
    KILL_LEAKED=1
    shift
fi

# --- argument validation ---
if [ $# -lt 2 ]; then
    die "usage: vault-safe-commit.sh [--kill-leaked] \"message\" path1 [path2 ...]"
fi

MESSAGE="$1"
shift
PATHS=("$@")

if [ -z "${MESSAGE}" ]; then
    die "commit message is empty"
fi

for p in "${PATHS[@]}"; do
    case "$p" in
        -A|--all|.|\*)
            die "refusing path '${p}' — vault rule forbids unscoped staging"
            ;;
    esac
done

cd "${VAULT_ROOT}"

# --- kill leaked Claude.app git children (optional, macOS) ---
if [ "${KILL_LEAKED}" = "1" ]; then
    CLAUDE_PID=$(pgrep -f "Claude.app/Contents/MacOS/Claude" 2>/dev/null | head -1 || true)
    if [ -n "${CLAUDE_PID}" ]; then
        pkill -P "${CLAUDE_PID}" -f "git " 2>/dev/null && \
            log "killed leaked git children of Claude.app PID ${CLAUDE_PID}" || \
            log "--kill-leaked: no children matched (ok)"
    fi
fi

# --- vault-wide mutex: serialize concurrent invocations ---
_MUTEX_ACQUIRED=0
cleanup_mutex() {
    if [ "${_MUTEX_ACQUIRED}" = "1" ]; then
        rm -f "${VAULT_MUTEX}"
    fi
}
trap cleanup_mutex EXIT

mutex_wait=0
while ! (set -C; echo "$$" > "${VAULT_MUTEX}") 2>/dev/null; do
    if [ "${mutex_wait}" = "0" ]; then
        log "waiting for vault mutex (another vault-safe-commit is running)"
        echo "vault-safe-commit: waiting for vault mutex..." >&2
    fi
    sleep 2
    mutex_wait=$((mutex_wait + 2))
    if [ "${mutex_wait}" -ge "${MAX_WAIT_SECONDS}" ]; then
        die "vault mutex held for ${MAX_WAIT_SECONDS}s — investigate ${VAULT_MUTEX}"
    fi
done
_MUTEX_ACQUIRED=1
log "acquired vault mutex ($$)"

# --- is_real_write_process: check if any ACTUAL git binary write is running ---
# Echoes a count, never a failure. The `|| n=0` is load-bearing under
# `set -euo pipefail`: no match makes grep exit 1, pipefail propagates it, and
# an unguarded `n=$(...)` would abort the whole script — silently, with the
# vault mutex released and nothing committed. That was unreachable while the
# lock path could never exist (the wait loop never ran); resolving the real git
# dir makes this path live, so it has to be correct.
is_real_write_process() {
    local n
    n=$(ps -ax -o pid,command 2>/dev/null \
        | grep -E "^\s*[0-9]+ .*${GIT_BIN}.*(add|commit|checkout|reset|merge|rebase)" \
        | grep -v grep \
        | wc -l | tr -d ' ') || n=0
    case "$n" in ''|*[!0-9]*) n=0 ;; esac
    echo "$n"
}

# --- lock safety check loop ---
waited=0
while [ -e "${LOCK_FILE}" ]; do
    lock_size=$(stat -f%z "${LOCK_FILE}" 2>/dev/null || stat -c%s "${LOCK_FILE}" 2>/dev/null || echo "999")
    write_procs=$(is_real_write_process)

    stale=0

    if [ "${lock_size}" = "0" ] && [ "${write_procs}" = "0" ]; then
        stale=1
    elif [ "${lock_size}" != "0" ] && [ "${write_procs}" = "0" ]; then
        lock_pid=$(cat "${LOCK_FILE}" 2>/dev/null | tr -d '[:space:]' || echo "")
        if [ -n "${lock_pid}" ] && [[ "${lock_pid}" =~ ^[0-9]+$ ]]; then
            if ! kill -0 "${lock_pid}" 2>/dev/null; then
                log "stale non-empty lock: PID ${lock_pid} is dead — removing"
                stale=1
            fi
        else
            lock_age=$(( $(date +%s) - $(stat -f%m "${LOCK_FILE}" 2>/dev/null || stat -c%Y "${LOCK_FILE}" 2>/dev/null || date +%s) ))
            if [ "${lock_age}" -gt 60 ]; then
                log "stale non-empty lock (non-PID content, ${lock_age}s old) — removing"
                stale=1
            fi
        fi
    fi

    if [ "${stale}" = "1" ]; then
        rm -f "${LOCK_FILE}"
        break
    fi

    if [ "${waited}" -ge "${MAX_WAIT_SECONDS}" ]; then
        die "lock held for ${MAX_WAIT_SECONDS}s (size=${lock_size}, write_procs=${write_procs}). Investigate before retry."
    fi

    if [ "${waited}" = "0" ]; then
        echo "vault-safe-commit: lock held (size=${lock_size}, write_procs=${write_procs}), waiting up to ${MAX_WAIT_SECONDS}s..." >&2
    fi
    sleep 3
    waited=$((waited + 3))
done

# --- stage paths ---
# NOTE: `git add ... | while read` runs the add inside a pipeline, so a FAILING add is
# silently swallowed (the `while` exits 0) and the script commits anyway. Capture the
# output, check the real status, then log.
log "staging: ${PATHS[*]}"
if ! add_output=$(git add -- "${PATHS[@]}" 2>&1); then
    [ -n "${add_output}" ] && log "git add: ${add_output}"
    die "git add failed for: ${PATHS[*]}"
fi
[ -n "${add_output}" ] && log "git add: ${add_output}"

# --- check OUR paths actually have something to commit ---
# Scoped with `-- "${PATHS[@]}"`. Unscoped, this asks "is ANYTHING staged?" — so with a
# sibling session's change sitting in the shared index and our own paths unchanged, it
# answers yes and we commit THEIR work under OUR message.
if git diff --cached --quiet -- "${PATHS[@]}"; then
    log "no changes staged for the named paths — skipping commit"
    echo "vault-safe-commit: nothing to commit (named paths match HEAD)" >&2
    exit 0
fi

# --- commit ONLY the named paths ---
# `--only` (-o) commits the working-tree contents of the named paths and DISREGARDS the
# rest of the index, so a sibling session's staged work cannot ride along. Without it,
# `git commit` takes the ENTIRE index: measured on a live vault, two consecutive calls
# that each named ONE path produced commits of 1,191 files / 598,702 insertions, sweeping
# other live sessions' data, hook edits and logs into an unrelated message.
# This wrapper is the ONLY sanctioned route past the raw-git block guard, so without
# `--only` that guard provides zero real scoping while looking fully enforced.
# `--only` needs paths git already knows; the `git add` above guarantees that, including
# for new files.
git commit --quiet --only -m "${MESSAGE}" -- "${PATHS[@]}" || die "commit failed"
COMMIT_HASH=$(git log --oneline -1 | awk '{print $1}')
log "committed ${COMMIT_HASH}: ${MESSAGE}"
echo "vault-safe-commit: ${COMMIT_HASH} — ${MESSAGE}"
