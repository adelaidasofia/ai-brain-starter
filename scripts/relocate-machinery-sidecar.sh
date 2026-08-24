#!/usr/bin/env bash
# relocate-machinery-sidecar.sh — make a vault SAFE to keep inside iCloud / a
# cloud-sync folder by moving every churning machinery dir OUT of the synced
# tree into a local sidecar, leaving only tiny static pointers/symlinks behind.
#
# WHY
# ---
# A git-backed Obsidian vault inside iCloud Drive / Desktop & Documents melts the
# OS sync daemon — NOT because of the notes (markdown is tiny + low-churn) but
# because of the MACHINERY: a `.git` rewritten wholesale on `git gc`, per-session
# worktree checkouts (~thousands of files each), and search/index caches. The
# fix is not "turn iCloud off". The fix is: the vault MAY be synced; the
# machinery NEVER is. This script relocates the machinery and leaves the docs.
#
# CRITICAL: `.gitignore` does NOT stop iCloud. iCloud syncs the folder, not
# git's view of it. The bytes must physically leave the synced tree (relocate +
# symlink) or be flagged `.nosync` (iCloud ignores any name ending in .nosync).
#
# THE ONE RULE: IT NEVER MOVES ANYTHING GIT TRACKS
# ------------------------------------------------
# Every candidate below is tested against the REAL git index before it is
# touched. A path holding tracked content is your NOTES, whatever it is named —
# moving it turns the directory into a symlink, `git status` reports every file
# as deleted, and the next auto-commit/push propagates that deletion to every
# machine. So: tracked content -> never moved, reported, left exactly as it is.
# Genuinely ignored/untracked caches still move. If the index cannot be read,
# the path is left alone (a path I cannot prove is a cache is not a cache).
#
# WHAT IT MOVES (only when the path holds NO tracked content)
#   .git              -> <sidecar>/git    via `git init --separate-git-dir`
#                        (leaves a one-line `.git` POINTER FILE — static, safe)
#   .claude/worktrees -> <sidecar>/worktrees  (relocate + symlink; MYC-543 layer)
#   caches            -> <sidecar>/cache/<name> (relocate + symlink, or .nosync)
#                        .smart-env .codegraph "⚙️ Meta/graphify-out"
#                        "⚙️ Meta/Sessions" "⚙️ Meta/Worktree Snapshots"
#                        "⚙️ Meta/logs"  (only those that exist as REAL dirs)
#                        NOTE several of these names hold TRACKED notes in a real
#                        vault. They are candidates only — the index test
#                        decides, never the name.
#
# SEQUENCING GOTCHA (verified): `git init --separate-git-dir` ORPHANS existing
# linked worktrees. So this REFUSES to run while any linked worktree or live
# Claude session exists, unless --force. After relocation it runs
# `git worktree repair`. The probes FAIL CLOSED: if git or the session lock
# cannot be read, that counts as LIVE and the run is refused. "I could not look"
# and "I looked and it was empty" are different answers.
#
# SAFETY: idempotent (re-run = no-op report), interrupt-safe (every move is
# written to an append-only journal in the sidecar BEFORE it happens, so
# --rollback works from any kill point), reversible (--rollback reads that
# record and puts everything back, exits non-zero and KEEPS the record if any
# leg fails), --dry-run changes nothing, and it never deletes content — only
# moves it and leaves a symlink/pointer.
#
# The SIDECAR DESTINATION is checked with the same cloud-sync detector as the
# vault: relocating machinery from one synced tree into another (a redirected
# $HOME on a roaming/OneDrive profile) does not fix the melt, so it is refused.
#
# Pure bash + git + python3 (JSON only). No third-party deps.
#
# Usage:
#   relocate-machinery-sidecar.sh <vault-path> [options]
#   relocate-machinery-sidecar.sh <vault-path> --rollback
#
# Options:
#   --sidecar <dir>   sidecar root (default: $BRAIN_SIDECAR or ~/.brain-sidecar)
#   --nosync          keep caches in-tree as <name>.nosync (iCloud-ignored) +
#                     symlink, instead of relocating them to the sidecar
#   --dry-run         print intended actions, change nothing
#   --rollback        reverse a previous relocation using the recorded moves
#   --force           proceed even if linked worktrees / live sessions exist, a
#                     probe could not run, or the sidecar looks cloud-synced
#                     (DANGEROUS: separate-git-dir orphans live worktrees)
#   --quiet           only print warnings/errors + the final summary line
#   -h, --help        this help
#
# Exit codes: 0 ok / no-op · 1 refused (live worktree/session, unsafe sidecar,
#             or a probe that could not run) · 2 usage / nothing to roll back ·
#             3 not a git vault and could not init · 4 partial failure
set -euo pipefail

# ---- machinery the script relocates (relative to the vault root) -------------
# CANDIDATES, not a denylist: each moves ONLY if git tracks nothing there.
CACHE_DIRS=(
  ".smart-env"
  ".codegraph"
  "⚙️ Meta/graphify-out"
  "graphify-out"
  "⚙️ Meta/Sessions"
  "⚙️ Meta/Worktree Snapshots"
  "⚙️ Meta/logs"
  ".obsidian/.smart-env"
)
WORKTREES_REL=".claude/worktrees"

# ---- arg parse --------------------------------------------------------------
VAULT="" ; SIDECAR="${BRAIN_SIDECAR:-${MYCELIUM_SIDECAR:-$HOME/.brain-sidecar}}"
NOSYNC=0 ; DRYRUN=0 ; ROLLBACK=0 ; FORCE=0 ; QUIET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --sidecar) SIDECAR="${2:?--sidecar needs a path}"; shift 2;;
    --nosync) NOSYNC=1; shift;;
    --dry-run|--dryrun) DRYRUN=1; shift;;
    --rollback) ROLLBACK=1; shift;;
    --force) FORCE=1; shift;;
    --quiet) QUIET=1; shift;;
    # print the leading comment block, however long it grows (a hardcoded line
    # range silently truncates the help the moment the header changes)
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; exit 0;;
    -*) echo "unknown option: $1" >&2; exit 2;;
    *) if [ -z "$VAULT" ]; then VAULT="$1"; else echo "unexpected arg: $1" >&2; exit 2; fi; shift;;
  esac
done
[ -n "$VAULT" ] || { echo "usage: $(basename "$0") <vault-path> [options]" >&2; exit 2; }
[ -d "$VAULT" ] || { echo "not a directory: $VAULT" >&2; exit 2; }

say()  { [ "$QUIET" = 1 ] || printf '%s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*" >&2; }
err()  { printf 'ERROR %s\n' "$*" >&2; }

# run <argv...> — NO eval, NO shell. The vault path is data, never code: a
# folder named `Notes $HOME Backup` used to re-expand, and one containing a
# backtick or $( ) would have EXECUTED under the old eval-a-string form.
run()        { if [ "$DRYRUN" = 1 ]; then printf 'DRY   %s\n' "$*"; return 0; fi; "$@"; }
run_quiet()  { if [ "$DRYRUN" = 1 ]; then printf 'DRY   %s\n' "$*"; return 0; fi; "$@" >/dev/null; }
run_silent() { if [ "$DRYRUN" = 1 ]; then printf 'DRY   %s\n' "$*"; return 0; fi; "$@" >/dev/null 2>&1; }

# Test-only fault injection for the interrupt matrix in test-machinery-sidecar.sh.
# Unset in production. Set to a step label to SIGKILL this process right after
# that step (no traps, no cleanup) — the only honest way to prove --rollback
# works from every kill point.
_maybe_die() {
  if [ "${BRAIN_SIDECAR_TEST_KILL_AT:-}" = "$1" ]; then kill -9 $$; fi
  return 0
}

PYBIN="$(command -v python3 || command -v python || true)"
[ -n "$PYBIN" ] || { err "python3 required for the move record"; exit 4; }

# absolutize (portable realpath: cd + pwd -P)
VAULT="$(cd "$VAULT" && pwd -P)"
case "$SIDECAR" in /*) :;; ~*) SIDECAR="${SIDECAR/#\~/$HOME}";; *) SIDECAR="$PWD/$SIDECAR";; esac
# ...and PHYSICALLY resolve it. The sidecar usually does not exist yet, so
# `cd + pwd -P` cannot be used; realpath() resolves the existing ancestors and
# keeps the tail. Without this, /var vs /private/var (macOS) makes the
# "is the sidecar inside the vault" check silently miss.
SIDECAR="$("$PYBIN" -c 'import os,sys; print(os.path.realpath(os.path.expanduser(sys.argv[1])))' "$SIDECAR")"

# per-vault slug = sanitized basename + short abspath hash (collision-safe)
_base="$(basename "$VAULT")"
_slug="$(printf '%s' "$_base" | tr -c 'A-Za-z0-9._-' '-' | sed 's/-\{2,\}/-/g; s/^-//; s/-$//')"
_hash="$(printf '%s' "$VAULT" | (shasum 2>/dev/null || sha1sum) | cut -c1-8)"
SLUG="${_slug:-vault}-${_hash}"

SIDE_GIT="$SIDECAR/git/$SLUG"
SIDE_CACHE="$SIDECAR/cache/$SLUG"
SIDE_WT="$SIDECAR/worktrees/$SLUG"
MANIFEST="$SIDECAR/manifests/$SLUG.json"
# Append-only write-ahead record. Written BEFORE each move, fsynced, and kept
# across runs, so a SIGKILL at ANY point still leaves --rollback a full map.
JOURNAL="$SIDECAR/manifests/$SLUG.journal.jsonl"


_tsv="$(mktemp)"
trap 'rm -f "$_tsv"' EXIT

# ---- write-ahead journal ----------------------------------------------------
# Appends one JSON object and fsyncs it (file AND directory) before returning.
# A move this fails to record is a move that does NOT happen.
journal_append() {  # type  vault_rel  target  mode
  [ "$DRYRUN" = 1 ] && return 0
  mkdir -p "$(dirname "$JOURNAL")" || return 1
  J="$JOURNAL" "$PYBIN" - "$1" "$2" "$3" "$4" <<'PY'
import json, os, sys
rec = {"type": sys.argv[1], "vault_rel": sys.argv[2],
       "target": sys.argv[3], "mode": sys.argv[4]}
p = os.environ["J"]
with open(p, "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    f.flush()
    os.fsync(f.fileno())
try:  # directory fsync: POSIX only, best-effort (unsupported on Windows)
    d = os.open(os.path.dirname(p), os.O_RDONLY)
    try:
        os.fsync(d)
    finally:
        os.close(d)
except OSError:
    pass
PY
}

# Read the recorded moves from the journal (.jsonl) or a manifest (.json) and
# print them as TSV. Later records win per (type, vault_rel) so a relocate ->
# rollback -> relocate cycle never replays a stale row. REV=1 reverses.
records_tsv() {  # $1 = source file
  SRC="$1" REV="${REV:-0}" "$PYBIN" - <<'PY'
import json, os
src = os.environ["SRC"]
recs = []
if src.endswith(".jsonl"):
    with open(src, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except ValueError:
                continue  # a torn final line from a SIGKILL is not a reason to stop
else:
    with open(src, encoding="utf-8") as f:
        recs = json.load(f).get("moves", [])
seen, order = {}, []
for r in recs:
    if not isinstance(r, dict):
        continue
    k = (r.get("type", ""), r.get("vault_rel", ""))
    if k not in seen:
        order.append(k)
    seen[k] = r
rows = [seen[k] for k in order]
if os.environ.get("REV") == "1":
    rows.reverse()
for r in rows:
    print("\t".join([r.get("type", ""), r.get("vault_rel", ""),
                     r.get("target", ""), r.get("mode", "")]))
PY
}

write_manifest() {
  [ "$DRYRUN" = 1 ] && return 0
  if [ ! -s "$JOURNAL" ]; then
    [ -f "$MANIFEST" ] && { say "  · no recorded moves; keeping existing manifest $MANIFEST"; return 0; }
    return 0
  fi
  records_tsv "$JOURNAL" > "$_tsv"
  mkdir -p "$(dirname "$MANIFEST")"
  VAULT="$VAULT" SIDECAR="$SIDECAR" SLUG="$SLUG" NOSYNC="$NOSYNC" \
  REC="$_tsv" MAN="$MANIFEST" "$PYBIN" - <<'PY'
import json, os
recs = []
with open(os.environ["REC"], encoding="utf-8") as f:
    for line in f:
        line = line.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        while len(parts) < 4:
            parts.append("")
        recs.append({"type": parts[0], "vault_rel": parts[1],
                     "target": parts[2], "mode": parts[3]})
doc = {
    "schema": "machinery-sidecar/1",
    "vault": os.environ["VAULT"],
    "sidecar": os.environ["SIDECAR"],
    "slug": os.environ["SLUG"],
    "nosync": os.environ["NOSYNC"] == "1",
    "moves": recs,
}
os.makedirs(os.path.dirname(os.environ["MAN"]), exist_ok=True)
with open(os.environ["MAN"], "w", encoding="utf-8") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
}

# ---- git probes (fail CLOSED: "could not look" != "nothing there") -----------
# LC_ALL=C so git's messages stay in English and the classification below holds
# on a translated locale.
GIT_STATE="unknown"
probe_git_state() {  # prints repo | norepo | error
  local out rc=0
  out="$(LC_ALL=C git -C "$VAULT" rev-parse --git-dir 2>&1)" || rc=$?
  if [ "$rc" -eq 0 ]; then printf 'repo\n'; return 0; fi
  case "$out" in
    *"not a git repository"*) printf 'norepo\n';;
    *) printf 'error\n'; printf 'WARN  git could not read %s: %s\n' "$VAULT" "$out" >&2;;
  esac
  return 0
}

# Prints the number of TRACKED files at a vault-relative path.
# Returns 1 when the index could not be read at all — the caller must then treat
# the path as tracked and leave it alone.
tracked_count() {  # $1 = vault-relative path
  local rel="$1" out rc=0
  case "$GIT_STATE" in
    norepo) printf '0\n'; return 0;;
    repo) :;;
    *) return 1;;
  esac
  out="$(LC_ALL=C git -C "$VAULT" ls-files -- "$rel" 2>/dev/null)" || rc=$?
  [ "$rc" -eq 0 ] || return 1
  [ -n "$out" ] || { printf '0\n'; return 0; }
  printf '%s\n' "$out" | wc -l | tr -d ' '
}

linked_worktrees() {  # prints one path per LINKED worktree; returns 2 if unprobeable
  local out rc=0
  case "$GIT_STATE" in
    norepo) return 0;;
    repo) :;;
    *) return 2;;
  esac
  # Registered LINKED worktrees = every "worktree " porcelain block EXCEPT the
  # first (the first is always the MAIN worktree). Filtering by "!= $VAULT" is
  # WRONG after --separate-git-dir: git then reports the main worktree's path as
  # the separated gitdir, not the vault, so the main would masquerade as linked.
  out="$(LC_ALL=C git -C "$VAULT" worktree list --porcelain 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    printf 'WARN  could not list git worktrees in %s: %s\n' "$VAULT" "$out" >&2
    return 2
  fi
  printf '%s\n' "$out" | awk '/^worktree /{n++; if (n>1) print substr($0,10)}'
}

live_sessions() {  # prints an integer; returns 2 if the lock could not be read
  local lock="$VAULT/.claude/.session-lock.json" out rc=0
  [ -f "$lock" ] || { printf '0\n'; return 0; }
  out="$(SELF_PID="$$" LOCK="$lock" "$PYBIN" - <<'PY'
import json, os, sys, time
lock = os.environ["LOCK"]
try:
    with open(lock, encoding="utf-8") as f:
        data = json.load(f)
except Exception as e:  # an unreadable/corrupt lock is NOT "zero sessions"
    print("cannot read %s: %s" % (lock, e), file=sys.stderr)
    raise SystemExit(3)
sessions = data.get("sessions", {}) if isinstance(data, dict) else {}
cut = time.time() - 35 * 60
self_pid = int(os.environ.get("SELF_PID", "0") or 0)
n = 0
for s in sessions.values():
    if not isinstance(s, dict):
        continue
    pid = s.get("pid")
    la = s.get("last_activity_at")
    alive = False
    if isinstance(pid, int) and pid > 0 and pid != self_pid:
        try:
            os.kill(pid, 0); alive = True
        except ProcessLookupError:
            alive = False
        except Exception:
            alive = True
    recent = isinstance(la, (int, float)) and la >= cut
    if alive or recent:
        n += 1
print(n)
PY
)" || rc=$?
  if [ "$rc" -ne 0 ]; then return 2; fi
  case "$out" in
    ''|*[!0-9]*) printf 'WARN  unreadable session count from %s: %s\n' "$lock" "$out" >&2; return 2;;
  esac
  printf '%s\n' "$out"
}

guard_no_live() {
  local wts="" sess="0" wt_rc=0 sess_rc=0 probe_failed=0
  wts="$(linked_worktrees)" || wt_rc=$?
  sess="$(live_sessions)" || sess_rc=$?
  if [ "$wt_rc" -ne 0 ] || [ "$sess_rc" -ne 0 ]; then probe_failed=1; fi
  case "$sess" in ''|*[!0-9]*) sess=0;; esac

  if [ -n "$wts" ] || [ "$sess" -gt 0 ] || [ "$probe_failed" = 1 ]; then
    if [ "$FORCE" = 1 ]; then
      warn "live worktrees/sessions present or unprobeable — proceeding under --force (separate-git-dir will orphan live worktrees)"
      return 0
    fi
    err "refusing: relocation must run in a proven no-live-worktree window."
    if [ "$probe_failed" = 1 ]; then
      err "  a safety probe could not run (see the WARN above), so I cannot prove nothing is live."
      err "  a probe that errors and a vault that is idle look identical — this refuses rather than guess."
      err "  common cause: git 'dubious ownership' on a restored/migrated/external/network vault. Fix with:"
      err "    git config --global --add safe.directory \"$VAULT\""
    fi
    if [ -n "$wts" ]; then err "  linked worktrees still registered:"; printf '    %s\n' "$wts" >&2; fi
    if [ "$sess" -gt 0 ]; then err "  $sess live Claude session(s) in $VAULT/.claude/.session-lock.json"; fi
    err "  close all sessions + 'git worktree remove' scratch trees, then re-run. Override: --force"
    return 1
  fi
  return 0
}

# ---- sidecar destination guard ----------------------------------------------
# The destination must be OUTSIDE every synced tree. Moving `.git` from one
# synced folder into another (redirected $HOME on a roaming / OneDrive-managed
# profile, a network home) passes every success check and changes nothing about
# the melt — except that the notes are now split from their repo.
guard_sidecar_location() {
  local ccs="$_sa/check-cloud-sync.py" out rc=0
  case "$SIDECAR/" in
    "$VAULT"/*)
      err "refusing: the sidecar ($SIDECAR) is INSIDE the vault ($VAULT)."
      err "  the machinery would never leave the synced tree. Pass --sidecar <a path outside the vault>."
      return 1;;
  esac
  case "$VAULT/" in
    "$SIDECAR"/*)
      err "refusing: the vault ($VAULT) is INSIDE the sidecar ($SIDECAR)."
      err "  pass --sidecar <a path outside the vault>."
      return 1;;
  esac
  if [ ! -f "$ccs" ]; then
    warn "could not verify the sidecar destination: $ccs is missing (cloud-sync detector unavailable)"
    return 0
  fi
  out="$("$PYBIN" "$ccs" --porcelain "$SIDECAR" 2>/dev/null)" || rc=$?
  case "$out" in
    CLOUD_SYNC_RISK*)
      if [ "$FORCE" = 1 ]; then
        warn "sidecar destination is inside ${out#CLOUD_SYNC_RISK:} — proceeding under --force (the sync melt is NOT fixed by this run)"
        return 0
      fi
      err "refusing: the sidecar destination is inside ${out#CLOUD_SYNC_RISK:}, a cloud-sync folder."
      err "  sidecar: $SIDECAR"
      err "  moving the machinery from one synced folder into another does not stop the melt;"
      err "  it only splits your notes from their repo. This usually means \$HOME itself is synced"
      err "  (a roaming / OneDrive-managed / network profile), so the default ~/.brain-sidecar is too."
      err "  Re-run with a local path, e.g.:  --sidecar /var/tmp/brain-sidecar   (Windows: C:\\brain-sidecar)"
      err "  Override (NOT recommended): --force"
      return 1;;
    OK_LOCAL) return 0;;
    *)
      warn "could not verify the sidecar destination is outside a cloud-sync folder"
      warn "  ($(basename "$ccs") exited $rc and said: ${out:-<nothing>}) — proceeding"
      return 0;;
  esac
}

# The path the user named is where a REPOSITORY gets created or relocated. The
# only validation used to be "non-empty" + "is a directory", so pointing this at
# $HOME fresh-init'd a repo over the whole home directory, with ~/.ssh, ~/.aws
# and ~/.config/gh sitting untracked inside the working tree and one `git add -A`
# from being written into git objects permanently (MYC-4028, observed on a real
# machine). --rollback deliberately does NOT run this: a machine already in that
# state must still be able to undo it.
#
# FAIL-CLOSED. Unlike the cloud-sync probe, a missing or unreadable answer here
# is not a cosmetic gap — it is the only thing standing between a mistyped path
# and a permanent credential leak.
guard_vault_target() {
  local cvt="$_sa/check-vault-target.py" out rc=0 initflag=""
  # --for-init tightens the rule when there is no repo yet: creating one is the
  # irreversible half. An existing .git means the user already chose this.
  [ -e "$VAULT/.git" ] || initflag="--for-init"
  if [ ! -f "$cvt" ]; then
    err "refusing: cannot validate the vault target — $cvt is missing."
    err "  that check is what stops this tool from building a git repo over your home directory."
    err "  Re-run the installer to restore it, then try again."
    return 1
  fi
  out="$("$PYBIN" "$cvt" --porcelain $initflag "$VAULT" 2>/dev/null)" || rc=$?
  case "$out" in
    OK_VAULT) return 0;;
    REFUSE_HOME:*)
      err "refusing: $VAULT is ${out#REFUSE_HOME:}."
      err "  This tool creates or relocates a GIT REPOSITORY at the path you give it."
      err "  Doing that here puts ~/.ssh, ~/.aws, ~/.netrc and ~/.config/gh inside a git"
      err "  working tree — one \`git add -A\` from being written into history permanently."
      err "  Point it at your vault instead, e.g.  ~/Brain  or  ~/vaults/<name>."
      err "  There is NO --force for this one, by design."
      return 1;;
    REFUSE_CREDENTIALS:*)
      if [ "$FORCE" = 1 ]; then
        warn "vault target holds credential material (${out#REFUSE_CREDENTIALS:}) — proceeding under --force"
        return 0
      fi
      err "refusing: $VAULT holds credential material at its top level (${out#REFUSE_CREDENTIALS:})."
      err "  That looks like a home directory, not a vault. Turning it into a git repository"
      err "  would place those files inside a working tree."
      err "  Point the tool at your vault, or pass --force if you are certain."
      return 1;;
    REFUSE_NOT_A_VAULT:*)
      if [ "$FORCE" = 1 ]; then
        warn "vault target shows no vault evidence (${out#REFUSE_NOT_A_VAULT:}) — proceeding under --force"
        return 0
      fi
      err "refusing: $VAULT has no git repo AND nothing that says it is a vault."
      err "  (${out#REFUSE_NOT_A_VAULT:})"
      err "  This tool would CREATE a repository here. If that is really what you want,"
      err "  run \`git init\` there yourself first, then re-run this tool."
      err "  Override only if you are certain: --force"
      return 1;;
    *)
      err "refusing: could not validate the vault target."
      err "  $(basename "$cvt") exited $rc and said: ${out:-<nothing>}"
      err "  A check that cannot answer is not a pass. Fix the install, then re-run."
      return 1;;
  esac
}

# =============================================================================
# ROLLBACK
# =============================================================================
rollback_git() {  # $1 = sidecar gitdir; 0 restored / 3 nothing to do / 1 failed
  local target="$1" mode="${2:-separated}" ptr="$VAULT/.git"
  # A fresh-init CREATED a repo that never existed. Reversing that means REMOVE,
  # not restore: the `mv "$target" "$ptr"` below would materialise .git as a real
  # directory and leave the vault a git repo — which, when the target was $HOME,
  # is the exact damage being repaired (MYC-4028). The recorded mode is the only
  # thing that distinguishes the two, and this function used to ignore it.
  if [ "$mode" = "fresh-init" ]; then
    if [ -d "$ptr" ] && [ ! -L "$ptr" ]; then
      err "  · .git is a real directory, not the pointer file this tool created."
      err "    that is not the state this rollback recorded — leaving it untouched."
      err "    inspect it yourself: $ptr"
      return 1
    fi
    if [ -e "$ptr" ] || [ -L "$ptr" ]; then
      run rm -f "$ptr" || { err "  · could not remove the pointer $ptr"; return 1; }
      say "  · removed the .git pointer this tool created — $VAULT is no longer a git repo"
    elif [ ! -d "$target" ]; then
      say "  · no .git pointer and no sidecar gitdir — nothing to undo"
      return 3
    else
      say "  · no .git pointer to remove"
    fi
    # NOTHING is deleted. Commits may have been made in the repo since it was
    # created, so the gitdir stays put and the user is told exactly where. A
    # silent delete here would be the same class of bug as the one being fixed.
    if [ -d "$target" ]; then
      say "  · the repository it created is KEPT at: $target"
      say "    nothing was deleted. Remove that directory yourself once you are sure."
    fi
    return 0
  fi
  if [ -d "$ptr" ] && [ ! -L "$ptr" ]; then
    if [ -d "$target" ]; then
      err "  · .git NOT restored: BOTH $ptr (a real dir) and $target exist."
      err "    refusing to overwrite either — inspect them and keep the one you want."
      return 1
    fi
    say "  · .git already a real directory — nothing to restore"
    return 3
  fi
  if [ ! -d "$target" ]; then
    err "  · .git NOT restored: the sidecar gitdir is missing ($target)"
    return 1
  fi
  if [ -e "$ptr" ] || [ -L "$ptr" ]; then
    run rm -f "$ptr" || { err "  · .git NOT restored: could not remove the pointer $ptr"; return 1; }
  fi
  run mv "$target" "$ptr" || { err "  · .git NOT restored: move failed ($target -> $ptr)"; return 1; }
  run_silent git -C "$VAULT" config --unset core.worktree || true
  run_silent git -C "$VAULT" worktree repair || true
  say "  · restored .git (directory) from $target"
  return 0
}

rollback_dir() {  # $1 rel, $2 target, $3 mode; 0 restored / 3 nothing to do / 1 failed
  local rel="$1" target="$2" mode="$3" back parent
  local link="$VAULT/$rel"
  if [ "$mode" = "nosync" ]; then back="$VAULT/${rel}.nosync"; else back="$target"; fi
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    if [ -e "$back" ]; then
      err "  · $rel NOT restored: BOTH the vault path and $back exist — refusing to overwrite either"
      return 1
    fi
    say "  · $rel already in place — nothing to restore"
    return 3
  fi
  if [ ! -e "$back" ]; then
    if [ -L "$link" ]; then
      err "  · $rel NOT restored: $back is missing and the vault path is a dangling link"
      return 1
    fi
    say "  · $rel: nothing to restore (the move never happened)"
    return 3
  fi
  if [ -L "$link" ]; then
    run rm -f "$link" || { err "  · $rel NOT restored: could not remove the symlink $link"; return 1; }
  fi
  parent="$(dirname "$link")"
  run mkdir -p "$parent" || { err "  · $rel NOT restored: could not create $parent"; return 1; }
  run mv "$back" "$link" || { err "  · $rel NOT restored: move failed ($back -> $link)"; return 1; }
  say "  · restored $rel"
  return 0
}

do_rollback() {
  local src="" typ rel target mode r rc=0 restored=0 already=0 failed=0
  if [ -s "$JOURNAL" ]; then src="$JOURNAL"
  elif [ -f "$MANIFEST" ]; then src="$MANIFEST"
  else
    err "no relocation record at $JOURNAL or $MANIFEST — nothing to roll back"
    return 2
  fi
  say "Rolling back machinery sidecar for: $VAULT"
  say "  record: $src"
  REV=1 records_tsv "$src" > "$_tsv" || { err "could not read the relocation record $src"; return 4; }

  while IFS=$'\t' read -r typ rel target mode; do
    [ -n "$typ" ] || continue
    r=0
    case "$typ" in
      git)             rollback_git "$target" "$mode" || r=$?;;
      cache|worktrees) rollback_dir "$rel" "$target" "$mode" || r=$?;;
      *)               warn "  · unknown record type '$typ' for '$rel' — skipped"; r=1;;
    esac
    case "$r" in
      0) restored=$((restored + 1));;
      3) already=$((already + 1));;
      *) failed=$((failed + 1)); rc=1;;
    esac
  done < "$_tsv"

  if [ "$rc" = 0 ]; then
    if [ "$DRYRUN" != 1 ]; then
      run rm -f "$JOURNAL" || true
      run rm -f "$MANIFEST" || true
    fi
    say "Rollback complete: $restored restored, $already already in place, 0 failed."
    return 0
  fi
  # The record is the only map back. It is NEVER deleted on a partial rollback.
  err "Rollback INCOMPLETE: $restored restored, $already already in place, $failed FAILED."
  err "  the relocation record was kept so you can fix the cause and re-run:"
  err "    $(basename "$0") \"$VAULT\" --sidecar \"$SIDECAR\" --rollback"
  return 1
}

# =============================================================================
# RELOCATE one cache/worktree dir (relocate+symlink, or nosync+symlink)
# =============================================================================
relocate_dir() {  # $1 = vault-relative path, $2 = explicit sidecar destination, $3 = record-type
  local rel="$1" dst="$2" rtype="$3" n ns
  local src="$VAULT/$rel"
  if [ -L "$src" ]; then say "  · $rel already a symlink — skip"; return 0; fi
  if [ ! -e "$src" ]; then return 0; fi          # absent → nothing to do

  # THE ONE RULE: never move anything git tracks, whatever the path is named.
  if ! n="$(tracked_count "$rel")"; then
    warn "  · $rel — KEPT: could not read the git index, so I cannot prove this is a cache"
    return 0
  fi
  if [ "$n" != 0 ]; then
    say "  · $rel — KEPT: git tracks $n file(s) here. Those are notes, not a cache;"
    say "        relocating them would show every one as DELETED and sync that deletion."
    return 0
  fi

  if [ "$NOSYNC" = 1 ]; then
    ns="$VAULT/${rel}.nosync"
    if [ -e "$ns" ]; then warn "  · $rel.nosync already exists — skip"; return 0; fi
    journal_append "$rtype" "$rel" "$ns" "nosync" || {
      err "  · $rel NOT moved: could not record the move in $JOURNAL (an unrecordable move cannot be reversed)"
      return 1
    }
    _maybe_die "post-journal:$rel"
    run mv "$src" "$ns" || { err "  · $rel: move failed"; return 1; }
    _maybe_die "post-mv:$rel"
    run ln -s "$(basename "$rel").nosync" "$src" || { err "  · $rel: symlink failed"; return 1; }
    _maybe_die "post-link:$rel"
    say "  · $rel -> ${rel}.nosync (iCloud-ignored) + symlink"
  else
    if [ -e "$dst" ]; then
      run mv "$dst" "$dst.bak-$(date +%s)" || { err "  · $rel: could not back up the prior sidecar copy"; return 1; }
      warn "  · prior sidecar copy at $dst backed up"
    fi
    journal_append "$rtype" "$rel" "$dst" "relocate" || {
      err "  · $rel NOT moved: could not record the move in $JOURNAL (an unrecordable move cannot be reversed)"
      return 1
    }
    _maybe_die "post-journal:$rel"
    run mkdir -p "$(dirname "$dst")" || { err "  · $rel: could not create the sidecar parent"; return 1; }
    run mv "$src" "$dst" || { err "  · $rel: move failed"; return 1; }
    _maybe_die "post-mv:$rel"
    run ln -s "$dst" "$src" || { err "  · $rel: symlink failed"; return 1; }
    _maybe_die "post-link:$rel"
    say "  · $rel -> $dst + symlink"
  fi
}

# =============================================================================
# RELOCATE .git via --separate-git-dir
# =============================================================================
relocate_git() {
  local gitpath="$VAULT/.git" cur
  if [ -f "$gitpath" ]; then
    cur="$(sed -n 's/^gitdir: //p' "$gitpath" | head -1)"
    say "  · .git already a pointer (-> ${cur:-?}) — skip"
    return 0
  fi
  if [ -d "$gitpath" ]; then
    journal_append "git" ".git" "$SIDE_GIT" "separated" || {
      err "  .git NOT moved: could not record the move in $JOURNAL"; return 4
    }
    _maybe_die "post-journal:.git"
    run mkdir -p "$(dirname "$SIDE_GIT")" || return 4
    run_quiet git -C "$VAULT" init --separate-git-dir "$SIDE_GIT" || return 4
    _maybe_die "post-git"
    if [ "$DRYRUN" != 1 ] && [ ! -f "$gitpath" ]; then
      err "  separate-git-dir did not produce a .git pointer file"; return 4
    fi
    run_silent git -C "$VAULT" worktree repair || true
    say "  · .git -> $SIDE_GIT (pointer file left in vault)"
    return 0
  fi
  # no .git at all → fresh repo with a separated gitdir
  journal_append "git" ".git" "$SIDE_GIT" "fresh-init" || {
    err "  could not record the fresh init in $JOURNAL"; return 4
  }
  run mkdir -p "$(dirname "$SIDE_GIT")" || return 4
  run_quiet git -C "$VAULT" init --separate-git-dir "$SIDE_GIT" || return 4
  say "  · initialized fresh repo with gitdir at $SIDE_GIT"
}

# =============================================================================
# MAIN
# =============================================================================
_sa="$(cd "$(dirname "$0")" && pwd)"

if [ "$ROLLBACK" = 1 ]; then
  _rc=0
  do_rollback || _rc=$?
  exit "$_rc"
fi

# WHAT the user pointed at, before anything is created or moved. Runs ahead of
# every other gate and every mutation, and after the --rollback branch above so
# a machine already damaged this way can still undo it.
guard_vault_target || exit 1

# detection is informational for the VAULT — the whole point is iCloud is ALLOWED
CLOUD=""
if [ -f "$_sa/check-cloud-sync.py" ]; then
  CLOUD="$("$PYBIN" "$_sa/check-cloud-sync.py" --porcelain "$VAULT" 2>/dev/null || true)"
fi

say "Machinery-sidecar relocation"
say "  vault   : $VAULT"
say "  sidecar : $SIDECAR  (slug: $SLUG)"
case "$CLOUD" in
  CLOUD_SYNC_RISK*) say "  cloud   : ${CLOUD#CLOUD_SYNC_RISK:} — relocating machinery so the docs can sync safely";;
  OK_LOCAL)         say "  cloud   : local disk (machinery relocation still valid; reduces in-tree churn)";;
esac
[ "$NOSYNC" = 1 ] && say "  mode    : --nosync (caches stay in-tree as .nosync)"
[ "$DRYRUN" = 1 ] && say "  mode    : --dry-run (no changes)"

# the destination is NOT informational: a synced sidecar fixes nothing
guard_sidecar_location || exit 1

GIT_STATE="$(probe_git_state)"
guard_no_live || exit 1

FAILED=0
say "Relocating .git ..."
relocate_git || exit 4
GIT_STATE="$(probe_git_state)"   # a fresh-init just created the repo

say "Relocating worktrees ..."
relocate_dir "$WORKTREES_REL" "$SIDE_WT" "worktrees" || FAILED=$((FAILED + 1))

say "Relocating caches ..."
for rel in "${CACHE_DIRS[@]}"; do
  relocate_dir "$rel" "$SIDE_CACHE/$rel" "cache" || FAILED=$((FAILED + 1))
done

write_manifest
if [ "$DRYRUN" = 1 ]; then
  say "DRY-RUN complete — no changes made. Manifest would be: $MANIFEST"
elif [ "$FAILED" -gt 0 ]; then
  err "$FAILED path(s) could not be relocated (see the errors above). Record: $JOURNAL"
  err "Reverse what DID move: $(basename "$0") \"$VAULT\" --sidecar \"$SIDECAR\" --rollback"
  exit 4
else
  say "Done. Manifest: $MANIFEST"
  say "Reverse anytime: $(basename "$0") \"$VAULT\" --rollback"
fi
