#!/usr/bin/env bash
# exit-contract: ADVISORY -- an interactive operator sync whose status action
#   reports divergence for a human to resolve

# vault-multi-machine-sync.sh — keep two installs of the same vault coherent.
#
# Use case: you work from machine A and machine B. Same vault, two clones.
# Without sync, your CLAUDE.md, journal entries, and Sessions/ files diverge
# silently. The memory-durability rule requires "always also write to vault" —
# but vault sync between machines isn't shipped.
#
# This script ships the missing piece. It uses git as the transport (vault must
# already be git-tracked) and offers two modes:
#
#   1. push — commit local vault state (targeted paths) and push to remote
#   2. pull — fetch remote and merge with safe conflict handling
#   3. sync — pull then push (the common case)
#
# Designed for vaults that have a git remote (private GitHub or self-hosted).
# Vaults without a remote should NOT use this script — they're local-only by
# design.
#
# Usage:
#   bash scripts/vault-multi-machine-sync.sh sync [--dry-run] [--vault PATH]
#   bash scripts/vault-multi-machine-sync.sh pull
#   bash scripts/vault-multi-machine-sync.sh push
#   bash scripts/vault-multi-machine-sync.sh status
#
# Safety:
#   - Vault MUST have a remote. Refuses to run if `git remote -v` is empty.
#   - NEVER `git add -A`. Stages explicit paths only.
#   - Conflicts surface to the user with a manual-merge prompt.
#   - Aborts if a concurrent session is mid-commit (.git/index.lock present).
#   - --dry-run shows the plan without making changes.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Shared git-dir / index-lock resolver. FAIL CLOSED when the guard is missing:
# without it we cannot resolve the real lock path, so we cannot prove no other
# session is mid-commit, and this script's whole concurrency stance is "abort if
# someone else is writing".
CLOSE_GUARD="$SCRIPT_DIR/_session_close_guard.sh"
if [ -f "$CLOSE_GUARD" ]; then
  # shellcheck source=scripts/_session_close_guard.sh
  . "$CLOSE_GUARD"
else
  vault_git_locked() { return 0; }
  vault_git_index_lock() { echo ""; return 1; }
fi

ACTION="${1:-status}"
DRY_RUN=0
VAULT=""

shift 2>/dev/null || true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run|-n) DRY_RUN=1; shift ;;
    --vault) VAULT="$2"; shift 2 ;;
    --help|-h) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -z "$VAULT" ]] && VAULT="${VAULT_ROOT:-$(pwd)}"
VAULT="$(cd "$VAULT" 2>/dev/null && pwd)"
if [[ -z "$VAULT" ]] || [[ ! -d "$VAULT" ]]; then
  echo "ERROR: vault directory not found: $VAULT" >&2
  exit 2
fi

log()  { printf "  \033[36m·\033[0m %s\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
err()  { printf "  \033[31m✗\033[0m %s\n" "$*"; }
hdr()  { printf "\n\033[1m%s\033[0m\n" "$*"; }
dry()  { printf "  \033[35m[dry-run]\033[0m %s\n" "$*"; }

cd "$VAULT" || exit 2

# === safety checks ===

if [[ ! -d ".git" ]] && ! git rev-parse --git-dir >/dev/null 2>&1; then
  err "Vault is not a git repo: $VAULT"
  echo ""
  echo "  Multi-machine sync requires git as the transport. Either:"
  echo "    1. Initialize this vault as a git repo (git init), wire to a private remote, and re-run"
  echo "    2. Use a different sync mechanism (rsync, syncthing, iCloud) and skip this script"
  exit 2
fi

if [[ -z "$(git remote -v 2>/dev/null)" ]]; then
  err "Vault has no git remote: $VAULT"
  echo ""
  echo "  This script is for vaults with a remote (private GitHub or self-hosted)."
  echo "  Local-only vaults should NOT use this script."
  echo ""
  echo "  To wire a remote:"
  echo "    cd '$VAULT'"
  echo "    git remote add origin <your-private-repo-url>"
  echo "    git push -u origin main"
  exit 2
fi

# === lock check ===

# Lock path RESOLVED from git, never assembled as ".git/index.lock": on a
# sidecar-relocated vault .git is a POINTER FILE, so the assembled path can
# never exist and this abort would never fire. Fails closed - an unresolvable
# git dir counts as locked.
if vault_git_locked "$VAULT"; then
  warn "Concurrent git operation detected (git index.lock present, or the git dir could not be resolved)"
  echo "  Lock path: $(vault_git_index_lock "$VAULT" 2>/dev/null || echo '<unresolvable>')"
  echo "  Wait for that to finish, or remove the lock if it's stale."
  exit 2
fi

# === paths to sync (NEVER use git add -A) ===

# Conservative whitelist: vault config + memory + canonical aggregator inputs
SYNC_PATHS=(
  "CLAUDE.md"
  "⚙️ Meta/Sessions/"
  "⚙️ Meta/Decisions/"
  "⚙️ Meta/Session Captures.md"
  "⚙️ Meta/Last Session.md"
  "⚙️ Meta/Decision Log.md"
  "⚙️ Meta/Time Tracking.md"
  "⚙️ Meta/rules/"
  "Meta/Sessions/"
  "Meta/Decisions/"
  "Meta/Session Captures.md"
  "Meta/Last Session.md"
  "Meta/Decision Log.md"
  "Meta/rules/"
  "📓 Journals/"
  "Journals/"
  "👤 CRM/"
  "CRM/"
  "🏠 Home/"
  "Home/"
  "✅ To-dos/"
  "To-dos/"
)

# Filter to paths that actually exist
EXISTING_PATHS=()
for p in "${SYNC_PATHS[@]}"; do
  [[ -e "$p" ]] && EXISTING_PATHS+=("$p")
done

# === actions ===

do_status() {
  hdr "Vault sync status: $VAULT"
  log "Remote: $(git remote get-url origin 2>/dev/null || echo 'none')"
  log "Branch: $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
  if git fetch origin --quiet 2>/dev/null; then
    local local_head remote_head
    local_head=$(git rev-parse HEAD 2>/dev/null || echo "")
    remote_head=$(git rev-parse "origin/$(git rev-parse --abbrev-ref HEAD)" 2>/dev/null || echo "")
    if [[ "$local_head" == "$remote_head" ]]; then
      ok "In sync with remote"
    else
      local ahead behind
      ahead=$(git rev-list --count "$remote_head..$local_head" 2>/dev/null || echo "?")
      behind=$(git rev-list --count "$local_head..$remote_head" 2>/dev/null || echo "?")
      warn "Diverged: $ahead ahead, $behind behind"
    fi
  fi
  log "Tracked paths with local changes:"
  local touched=0
  for p in "${EXISTING_PATHS[@]}"; do
    if git diff --name-only HEAD -- "$p" 2>/dev/null | grep -q .; then
      log "  $p"
      touched=$((touched + 1))
    fi
  done
  [[ "$touched" -eq 0 ]] && log "  (none)"
}

do_push() {
  hdr "Pushing local vault state"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "Would stage: ${EXISTING_PATHS[*]}"
    dry "Would commit + push to origin"
    return 0
  fi
  if [[ ${#EXISTING_PATHS[@]} -eq 0 ]]; then
    warn "No tracked paths to sync"
    return 1
  fi
  if ! git add "${EXISTING_PATHS[@]}" 2>/dev/null; then
    err "git add failed"
    return 1
  fi
  if git diff --cached --quiet; then
    log "No changes to push"
    return 0
  fi
  local msg
  msg="vault-sync: $(hostname -s) $(date +%Y-%m-%dT%H:%M)"
  if git commit -m "$msg" >/dev/null 2>&1; then
    ok "Committed: $msg"
  else
    warn "Commit failed (no changes or pre-commit hook blocked)"
    return 1
  fi
  if git push origin "$(git rev-parse --abbrev-ref HEAD)" 2>&1 | tail -3; then
    ok "Pushed to remote"
  else
    err "Push failed"
    return 1
  fi
}

do_pull() {
  hdr "Pulling remote vault state"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    dry "Would fetch + merge origin"
    return 0
  fi
  if ! git fetch origin --quiet 2>/dev/null; then
    err "Fetch failed"
    return 1
  fi
  # Use --no-edit to avoid editor pop-up; conflicts will fail-loud
  if git merge --no-edit "origin/$(git rev-parse --abbrev-ref HEAD)" 2>&1 | tail -5; then
    ok "Merged from remote"
  else
    err "Merge had conflicts. Resolve manually:"
    echo ""
    echo "  cd '$VAULT'"
    echo "  git status"
    echo "  # edit conflicting files, then:"
    echo "  git add <resolved files>"
    echo "  git commit"
    echo "  git push"
    return 1
  fi
}

case "$ACTION" in
  status) do_status ;;
  push) do_push ;;
  pull) do_pull ;;
  sync)
    do_pull && do_push ;;
  *)
    echo "unknown action: $ACTION (use status / push / pull / sync)"
    exit 2 ;;
esac
