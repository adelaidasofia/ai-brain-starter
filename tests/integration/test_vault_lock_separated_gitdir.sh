#!/usr/bin/env bash
# Test that the vault's git index-lock mutex SURVIVES a separated git directory.
#
# WHY THIS EXISTS
#
# scripts/relocate-machinery-sidecar.sh (docs/CLOUD_SYNC.md "Shape B") moves the
# vault's git machinery out of the synced tree with `git init --separate-git-dir`.
# After that, `$VAULT/.git` is a one-line POINTER FILE, not a directory, and the
# real index lives in the sidecar.
#
# Every vault writer here serializes on the git index lock before it commits.
# Five sites used to build that lock path by joining `.git/index.lock` onto the
# vault root. On a relocated vault that path is under a FILE, so it can never
# exist — the check reports "free" forever and the mutex is silently disarmed.
# Two sessions closing at once both see "no lock" and both commit. This is the
# bottom-of-stack lost-update defense, off with no signal.
#
# The fix: resolve the REAL git dir (`git rev-parse --absolute-git-dir`), which
# answers correctly whether `.git` is a directory, a pointer file, or a worktree.
# One shared helper in scripts/_session_close_guard.sh, five call sites.
#
# NEGATIVE CONTROL: Part B drives all five sites against a fixture vault with a
# separated gitdir while a live concurrent writer holds the real index.lock, and
# asserts each one takes its LOCKED branch. Every Part B assertion is RED against
# the pre-fix code. Part D then proves the fix is not "always locked": with no
# lock held the same close DOES commit.
#
# Part B asserts on the GATE'S DECISION, not merely on "did it commit". That
# distinction is the whole point: git has its own internal index lock, so a
# script that sails past a disarmed mutex still fails to commit — it just fails
# the WRONG way. Measured on the pre-fix code (separated gitdir, lock held), the
# close never logs "index.lock held; skipped snapshot"; instead `git add` and
# `git commit` both die with "fatal: Unable to create ... index.lock: File
# exists", the close logs "git snapshot commit failed", and the session file is
# left uncommitted. The graceful defer path — the one whose contract is "daily
# maintenance will catch up" — never runs, and daily maintenance carries the
# same bug. So each assertion below requires the skip/refuse signal AND the
# absence of the blundered-into-git's-own-lock signal.
#
# Fail-closed contract: when the git dir cannot be resolved at all, the helper
# reports LOCKED (refuse). A mutex that fails open is the defect being fixed.
#
# Self-contained: temp vaults + temp sidecars + temp HOME, cleaned on exit.
# NEVER touches a real vault. Exit 0 = pass.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GUARD="$REPO_ROOT/scripts/_session_close_guard.sh"
HOOK="$REPO_ROOT/scripts/session-end-hook.sh"
MAINT="$REPO_ROOT/scripts/vault-daily-maintenance.sh"
SYNC="$REPO_ROOT/scripts/vault-multi-machine-sync.sh"
SAFE="$REPO_ROOT/scripts/vault-safe-commit.sh"

# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/sandbox_home.sh"

fail() { echo "FAIL: $1" >&2; exit 1; }

for f in "$GUARD" "$HOOK" "$MAINT" "$SYNC" "$SAFE"; do
  [ -f "$f" ] || fail "missing required file: $f"
  bash -n "$f" 2>/dev/null || fail "bash -n failed: $f"
done

command -v git >/dev/null 2>&1 || fail "git is required for this test"

TMP="$(mktemp -d)"
HOLDER_PID=""
cleanup() {
  [ -n "$HOLDER_PID" ] && kill "$HOLDER_PID" 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT

# Bound every lock wait so the suite stays fast. The scripts default to 60s.
export VAULT_GIT_LOCK_MAX_WAIT=2

# ─── fixtures ───────────────────────────────────────────────────────────────

# make_sidecar_vault <vault> <sidecar> — a vault whose .git is a POINTER FILE.
make_sidecar_vault() {
  local v="$1" side="$2"
  mkdir -p "$v/⚙️ Meta/Sessions"
  git init --quiet --initial-branch=master --separate-git-dir "$side" "$v"
  git -C "$v" config user.email "t@example.com"
  git -C "$v" config user.name "t"
  echo "# vault" > "$v/README.md"
  git -C "$v" add README.md
  git -C "$v" commit --quiet -m "init"
  printf -- '---\ntype: session\n---\n# session\nbody\n' \
    > "$v/⚙️ Meta/Sessions/$(date +%Y-%m-%d)T00-00-main.md"
}

# make_plain_vault <vault> — a normal vault whose .git is a real DIRECTORY.
make_plain_vault() {
  local v="$1"
  mkdir -p "$v/⚙️ Meta/Sessions"
  git init --quiet --initial-branch=master "$v"
  git -C "$v" config user.email "t@example.com"
  git -C "$v" config user.name "t"
  echo "# vault" > "$v/README.md"
  git -C "$v" add README.md
  git -C "$v" commit --quiet -m "init"
}

# hold_lock <gitdir> — a LIVE concurrent writer holding the real index.lock.
# A live holder PID is required: every site here reclaims a lock whose holder
# is dead, so a fake PID would be swept and prove nothing.
hold_lock() {
  sleep 300 &
  HOLDER_PID=$!
  disown "$HOLDER_PID" 2>/dev/null   # keep "Terminated" job noise out of the log
  echo "$HOLDER_PID" > "$1/index.lock"
}
release_lock() {
  [ -n "$HOLDER_PID" ] && kill "$HOLDER_PID" 2>/dev/null
  HOLDER_PID=""
  rm -f "$1/index.lock"
}

commit_count() { git -C "$1" log --oneline 2>/dev/null | wc -l | tr -d ' '; }

# run_close <vault> <home> — drive one session-end-hook close. Echoes nothing;
# the assertion is on the commit count.
run_close() {
  local v="$1" home="$2" sid="sid$$_${RANDOM}" sf
  sf="$(ls "$v/⚙️ Meta/Sessions/"*.md 2>/dev/null | head -1)"
  mkdir -p "$home/.claude"
  printf '{"session_file":"%s","is_trivial":false}\n' "$sf" \
    > "$home/.claude/.closing-signal-$sid.json"
  printf '{"session_id":"%s","transcript_path":""}' "$sid" | \
    run_sandboxed "$home" env VAULT_ROOT="$v" CLOSE_MAX_LOAD_PER_CORE=99999 \
      CLOSE_MUTEX="$home/close.lock" VAULT_GIT_LOCK_MAX_WAIT=2 \
      bash "$HOOK" >/dev/null 2>&1
}

# ─── Part A: the shape — prove the fixture reproduces the defect ────────────

VS="$TMP/vault-sidecar"; SIDE="$TMP/sidecar.git"
make_sidecar_vault "$VS" "$SIDE"

[ -f "$VS/.git" ] || fail "fixture wrong: .git should be a pointer FILE after --separate-git-dir"
[ -d "$VS/.git" ] && fail "fixture wrong: .git is still a directory"
grep -q '^gitdir:' "$VS/.git" || fail "fixture wrong: .git file is not a gitdir pointer"
echo "PASS: A1 --separate-git-dir leaves .git as a one-line pointer FILE"

REAL_GITDIR="$(git -C "$VS" rev-parse --absolute-git-dir)"
[ -d "$REAL_GITDIR" ] || fail "rev-parse --absolute-git-dir did not resolve to a directory"
echo "PASS: A2 rev-parse --absolute-git-dir resolves the real git dir outside the vault"

hold_lock "$REAL_GITDIR"

# The naive predicate the five sites used. It cannot ever be true here.
[ -f "$VS/.git/index.lock" ] && fail "naive path unexpectedly exists — fixture is not reproducing the bug"
# ...while git itself is genuinely locked.
if ( cd "$VS" && echo x > probe.txt && git add probe.txt ) >/dev/null 2>&1; then
  fail "git accepted a write while index.lock was held — the lock holder is not real"
fi
echo "PASS: A3 lock held: git REFUSES the write while the naive \$VAULT/.git/index.lock check reports FREE"

# ─── Part B: NEGATIVE CONTROL — all five sites must refuse ─────────────────
# Every assertion below is RED against the pre-fix code.

# --- B1: session-end-hook.sh (sites :258, :263) ---
BEFORE="$(commit_count "$VS")"
run_close "$VS" "$TMP/home-b1"
AFTER="$(commit_count "$VS")"
B1_LOG="$(cat "$TMP/home-b1/.claude/logs/session-close-errors.log" 2>/dev/null)"
echo "$B1_LOG" | grep -q "index.lock held" \
  || fail "B1 session-end-hook.sh never took its LOCKED branch on a separated gitdir — no 'index.lock held' decision was logged. Got: $(echo "$B1_LOG" | tr '\n' ' ' | head -c 300)"
echo "$B1_LOG" | grep -q "Unable to create" \
  && fail "B1 session-end-hook.sh blundered into git's own lock (fatal: Unable to create index.lock) instead of gating on it"
echo "$B1_LOG" | grep -q "git snapshot commit failed" \
  && fail "B1 session-end-hook.sh failed its snapshot instead of deferring it"
[ "$BEFORE" = "$AFTER" ] \
  || fail "B1 session-end-hook.sh COMMITTED through a held index.lock (before=$BEFORE after=$AFTER)"
echo "PASS: B1 session-end-hook.sh takes its LOCKED branch and defers, rather than failing into git's own lock"

# --- B2: vault-daily-maintenance.sh (sites :173, :174) ---
BEFORE="$(commit_count "$VS")"
B2_LOG="$(run_sandboxed "$TMP/home-b2" env VAULT_GIT_LOCK_MAX_WAIT=2 \
  bash "$MAINT" --vault-root "$VS" --reconcile-only --force 2>&1)"
AFTER="$(commit_count "$VS")"
echo "$B2_LOG" | grep -q "reconcile-commit] SKIPPED" \
  || fail "B2 vault-daily-maintenance.sh never took its LOCKED branch on a separated gitdir — no '[reconcile-commit] SKIPPED' decision. Got: $(echo "$B2_LOG" | tr '\n' ' ' | head -c 300)"
echo "$B2_LOG" | grep -q "Unable to create" \
  && fail "B2 vault-daily-maintenance.sh blundered into git's own lock instead of gating on it"
[ "$BEFORE" = "$AFTER" ] \
  || fail "B2 vault-daily-maintenance.sh COMMITTED through a held index.lock (before=$BEFORE after=$AFTER)"
echo "PASS: B2 vault-daily-maintenance.sh takes its LOCKED branch and skips the reconcile commit"

# --- B3: vault-multi-machine-sync.sh (site :91) ---
# Needs a remote to reach the lock gate; a bare repo in tmp is enough.
git init --quiet --bare "$TMP/remote.git"
git -C "$VS" remote add origin "$TMP/remote.git"
SYNC_OUT="$(run_sandboxed "$TMP/home-b3" env VAULT_GIT_LOCK_MAX_WAIT=2 \
  bash "$SYNC" status --vault "$VS" 2>&1)"
SYNC_RC=$?
echo "$SYNC_OUT" | grep -qi "Concurrent git operation detected" \
  || fail "B3 vault-multi-machine-sync.sh did NOT detect the held lock on a separated gitdir (rc=$SYNC_RC, out: $(echo "$SYNC_OUT" | tr '\n' ' ' | head -c 300))"
echo "PASS: B3 vault-multi-machine-sync.sh aborts on a concurrent git operation"

# --- B4: vault-safe-commit.sh (site :37) ---
BEFORE="$(commit_count "$VS")"
printf 'edit\n' >> "$VS/README.md"
B4_LOG="$(run_sandboxed "$TMP/home-b4" env VAULT_ROOT="$VS" VAULT_GIT_LOCK_MAX_WAIT=2 \
  bash "$SAFE" "test commit" "README.md" 2>&1)"
B4_RC=$?
AFTER="$(commit_count "$VS")"
echo "$B4_LOG" | grep -q "lock held for" \
  || fail "B4 vault-safe-commit.sh never took its LOCKED branch on a separated gitdir — no 'lock held for' refusal (rc=$B4_RC). Got: $(echo "$B4_LOG" | tr '\n' ' ' | head -c 300)"
[ "$B4_RC" -ne 0 ] \
  || fail "B4 vault-safe-commit.sh exited 0 while the real index.lock was held"
[ "$BEFORE" = "$AFTER" ] \
  || fail "B4 vault-safe-commit.sh COMMITTED through a held index.lock (before=$BEFORE after=$AFTER)"
echo "PASS: B4 vault-safe-commit.sh refuses loudly (non-zero) while the real lock is held"

release_lock "$REAL_GITDIR"

# ─── Part C: the shared helper's contract ──────────────────────────────────

# shellcheck source=/dev/null
( . "$GUARD"
  for fn in vault_git_dir vault_git_index_lock vault_git_locked vault_git_wait_unlocked; do
    command -v "$fn" >/dev/null 2>&1 || { echo "missing fn $fn" >&2; exit 1; }
  done ) || fail "C1 guard does not define the git-lock helpers"
echo "PASS: C1 guard defines vault_git_{dir,index_lock,locked,wait_unlocked}"

# shellcheck source=/dev/null
( . "$GUARD"
  got="$(vault_git_dir "$VS")"
  [ "$got" = "$REAL_GITDIR" ] || { echo "vault_git_dir=$got want=$REAL_GITDIR" >&2; exit 1; }
  got="$(vault_git_index_lock "$VS")"
  [ "$got" = "$REAL_GITDIR/index.lock" ] || { echo "lock path=$got" >&2; exit 1; }
  exit 0 ) || fail "C2 helper does not resolve the separated gitdir"
echo "PASS: C2 helper resolves the separated gitdir and its real index.lock path"

# shellcheck source=/dev/null
( . "$GUARD"
  vault_git_locked "$VS" && { echo "reported LOCKED with no lock present" >&2; exit 1; }
  exit 0 ) || fail "C3 helper reported locked on a free separated-gitdir vault"
echo "PASS: C3 helper reports FREE when no lock is held (no false positive)"

hold_lock "$REAL_GITDIR"
# shellcheck source=/dev/null
( . "$GUARD"
  vault_git_locked "$VS" || { echo "reported FREE while the real lock is held" >&2; exit 1; }
  vault_git_wait_unlocked "$VS" 2 && { echo "wait_unlocked returned success under a held lock" >&2; exit 1; }
  exit 0 ) || fail "C4 helper missed a lock held in the separated gitdir"
echo "PASS: C4 helper reports LOCKED for a lock held in the sidecar gitdir"
release_lock "$REAL_GITDIR"

# FAIL CLOSED: an unresolvable git dir must read as LOCKED, never as free.
mkdir -p "$TMP/not-a-repo"
# shellcheck source=/dev/null
( . "$GUARD"
  got="$(vault_git_dir "$TMP/not-a-repo")"
  [ -z "$got" ] || { echo "vault_git_dir on a non-repo returned '$got'" >&2; exit 1; }
  vault_git_locked "$TMP/not-a-repo" || { echo "non-repo reported FREE — fails OPEN" >&2; exit 1; }
  vault_git_wait_unlocked "$TMP/not-a-repo" 2 && { echo "non-repo wait returned success — fails OPEN" >&2; exit 1; }
  vault_git_locked "$TMP/does-not-exist-at-all" || { echo "missing dir reported FREE — fails OPEN" >&2; exit 1; }
  exit 0 ) || fail "C5 helper FAILS OPEN when the git dir cannot be resolved"
echo "PASS: C5 helper FAILS CLOSED (reports LOCKED) when the git dir cannot be resolved"

# Regression: a normal, non-separated vault must still work in both directions.
VP="$TMP/vault-plain"; make_plain_vault "$VP"
# shellcheck source=/dev/null
( . "$GUARD"
  got="$(vault_git_dir "$VP")"
  [ "$got" = "$(git -C "$VP" rev-parse --absolute-git-dir)" ] || { echo "plain gitdir=$got" >&2; exit 1; }
  vault_git_locked "$VP" && { echo "plain repo reported LOCKED with no lock" >&2; exit 1; }
  : > "$VP/.git/index.lock"
  vault_git_locked "$VP" || { echo "plain repo missed a real lock" >&2; exit 1; }
  rm -f "$VP/.git/index.lock"
  exit 0 ) || fail "C6 helper regressed on a normal (non-separated) repo"
echo "PASS: C6 helper still correct on a normal repo whose .git is a directory"

# ─── Part D: liveness — the fix must not be "always locked" ────────────────
# Without this, a helper hardwired to report LOCKED would pass all of Part B.

BEFORE="$(commit_count "$VS")"
git -C "$VS" remote remove origin 2>/dev/null   # keep the close path local-only
run_close "$VS" "$TMP/home-d1"
AFTER="$(commit_count "$VS")"
[ "$AFTER" -gt "$BEFORE" ] \
  || fail "D1 close did NOT commit on an UNLOCKED separated-gitdir vault (before=$BEFORE after=$AFTER) — the lock check is stuck closed"
echo "PASS: D1 with no lock held, the close DOES commit on a separated-gitdir vault"

echo
echo "All assertions passed. The vault index-lock mutex survives a separated git directory."
