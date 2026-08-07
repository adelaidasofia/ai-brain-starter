#!/bin/bash
# Weekly git worktree maintenance for the vault. Self-locates so it survives a move.
#
# Worktree pileup is prevented at the SOURCE by the lifecycle hooks
# (remove-ended-worktree at SessionEnd, enforce-worktree-cap at SessionStart,
# worktree-footprint-signal at SessionStart). This weekly job is the BACKSTOP +
# the orphan/branch/snapshot sweep:
#   1. Safe reclaim of scratch worktrees over cap + ORPHAN DIRS via
#      worktree-reclaim.py — snapshot-then-remove. NEVER the unsafe `rm -rf` an
#      earlier version did: genuinely-unsaved files are copied out first, and a
#      dir we can't reason about (dangling git metadata) is KEPT and reported.
#   2. `git worktree prune` for stale refs.
#   3. Delete orphan claude/ BRANCHES whose worktree is gone AND merged to master
#      (REFUSE + recover-script pointer for any with unmerged commits — the
#      worktree session-loss class from issue #65).
#   4. Prune orphan SNAPSHOT dirs past the recovery-retention window.
#
# Only `.claude/worktrees/` scratch + their orphan dirs are auto-removed.
# Deliberate ~/dev/<repo>-<slug> sibling worktrees and committed branches are
# never removed. Each block silently no-ops when its tool/dir is absent, so this
# is safe to ship before the matching hooks land.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT_ROOT="${VAULT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
LOG_DIR="${LOG_DIR:-$SCRIPT_DIR/../logs}"
LOG="$LOG_DIR/worktree-prune.log"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-$VAULT_ROOT/⚙️ Meta/Worktree Snapshots}"
WT_DIR="${WT_DIR:-$VAULT_ROOT/.claude/worktrees}"
SNAPSHOT_RETENTION_DAYS="${SNAPSHOT_RETENTION_DAYS:-30}"
# Safe reclaim engine — first existing of: alongside, skill install, legacy.
# ORDER IS LOAD-BEARING. $SCRIPT_DIR first: worktree-reclaim.py is a sibling of
# THIS script in scripts/, so that copy is always version-matched with the prune
# logic invoking it. ~/.claude/hooks/ is LAST and legacy-only: no installer ever
# writes worktree-reclaim.py there (it is a script, not a hook), so the only way
# that path is populated is a hand-placed or stale copy. Ranking it FIRST meant
# a stale copy silently shadowed canonical forever — observed live 2026-07-23.
# Keep it in the chain so a machine that only has that copy still sweeps, but
# never let it outrank canonical.
RECLAIM=""
for _c in "$SCRIPT_DIR/worktree-reclaim.py" \
          "$HOME/.claude/skills/ai-brain-starter/scripts/worktree-reclaim.py" \
          "$HOME/.claude/hooks/worktree-reclaim.py"; do
  [ -f "$_c" ] && RECLAIM="$_c" && break
done
mkdir -p "$(dirname "$LOG")"
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') prune $VAULT_ROOT ==="
  cd "$VAULT_ROOT" || { echo "ERR: cannot cd to vault"; exit 1; }

  # 1. Safe reclaim: scratch worktrees over cap + orphan dirs (snapshot-first).
  if [ -n "$RECLAIM" ]; then
    echo "--- safe reclaim via $(basename "$RECLAIM") ---"
    /usr/bin/python3 "$RECLAIM" --repo "$VAULT_ROOT" --idle-min 60 2>&1
  else
    echo "WARN: worktree-reclaim.py not found; orphan dirs not swept this run"
  fi

  # 2. Remove stale worktree refs (directories already deleted)
  git worktree prune -v 2>&1

  # 3. Delete orphan claude/ branches whose worktree is gone AND merged to master.
  #    REFUSE (loudly) any with unmerged commits so a human can recover via reflog
  #    or recover-orphan-claude-branches.py.
  DELETED=0
  REFUSED=0
  while IFS= read -r branch; do
    slug="${branch#claude/}"
    [ -d "$VAULT_ROOT/.claude/worktrees/$slug" ] && continue
    UNMERGED=$(git rev-list --count "master..$branch" 2>/dev/null || echo "?")
    if [ "$UNMERGED" = "0" ]; then
      git branch -D "$branch" 2>&1 && DELETED=$((DELETED + 1))
    else
      echo "REFUSE: $branch has $UNMERGED commit(s) not on master — not deleting."
      echo "  Inspect:  git log master..$branch --oneline"
      echo "  Recover:  python3 \"$SCRIPT_DIR/recover-orphan-claude-branches.py\""
      REFUSED=$((REFUSED + 1))
    fi
  done < <(git branch | grep 'claude/' | sed 's/^[* ]*//')
  echo "Deleted $DELETED orphaned claude/ branch(es)"
  [ "$REFUSED" -gt 0 ] && echo "Refused $REFUSED branch(es) with unmerged commits — run recover-orphan-claude-branches.py"

  echo "--- remaining worktrees ---"
  git worktree list 2>&1 | wc -l | xargs echo "count:"
  echo "--- remaining claude/ branches ---"
  git branch | grep -c 'claude/' 2>/dev/null | xargs echo "count:" || echo "count: 0"

  # 4. Prune orphan snapshot dirs (worktree archived AND past recovery window).
  #    No-op when SNAPSHOT_DIR doesn't exist (snapshot hooks not installed).
  if [ -d "$SNAPSHOT_DIR" ]; then
    echo "--- snapshot prune (retention=${SNAPSHOT_RETENTION_DAYS}d) ---"
    snap_deleted=0
    snap_kept_active=0
    snap_kept_recoverable=0
    while IFS= read -r snap_path; do
      [ -z "$snap_path" ] && continue
      slug="$(basename "$snap_path")"
      if [ -d "$WT_DIR/$slug" ]; then
        echo "keeping snapshot (worktree live): $slug"
        snap_kept_active=$((snap_kept_active + 1))
        continue
      fi
      stale=$(find "$snap_path" -maxdepth 0 -mtime +"$SNAPSHOT_RETENTION_DAYS" 2>/dev/null)
      if [ -n "$stale" ]; then
        files_dropped=$(find "$snap_path" -type f 2>/dev/null | wc -l | tr -d ' ')
        echo "deleting orphan snapshot (idle >${SNAPSHOT_RETENTION_DAYS}d, ${files_dropped} file(s)): $slug"
        rm -rf "$snap_path"
        snap_deleted=$((snap_deleted + 1))
      else
        echo "keeping orphan snapshot (within ${SNAPSHOT_RETENTION_DAYS}d window): $slug"
        snap_kept_recoverable=$((snap_kept_recoverable + 1))
      fi
    done < <(find "$SNAPSHOT_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
    echo "snapshots: deleted=$snap_deleted kept_active=$snap_kept_active kept_recoverable=$snap_kept_recoverable"
  fi
  echo
} >> "$LOG" 2>&1
