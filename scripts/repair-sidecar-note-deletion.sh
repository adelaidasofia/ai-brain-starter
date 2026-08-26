#!/usr/bin/env bash
# repair-sidecar-note-deletion.sh — find notes an OLD machinery-relocation
# deleted from a vault's history, and put them back.
#
# WHO THIS IS FOR
# ---------------
# An earlier version of scripts/relocate-machinery-sidecar.sh treated
# "⚙️ Meta/Sessions" as a cache because of its NAME. In a real vault that folder
# holds notes, and git tracks them. Relocating it turned the folder into a
# symlink, `git status` reported every note as deleted, the hourly auto-snapshot
# (`git add -A`) COMMITTED that deletion, and the multi-machine sync PUSHED it.
# The whole time, the message on screen said "nothing was deleted".
#
# The current script can no longer do this — it tests the git index before it
# moves anything, and `--rollback` puts the folder back. But rollback only fixes
# the FOLDER. It does not fix the HISTORY, and it cannot reach the other
# computers that already pulled the deletion. That is what this script is for.
#
# WHAT IT LOOKS FOR (the exact fingerprint, not a guess)
# ------------------------------------------------------
# A folder is reported ONLY when BOTH of these are true:
#   1. git history contains a commit that DELETED tracked files under that path
#      (an ordinary note deletion by hand looks the same on this half alone), AND
#   2. that same path is a SYMLINK pointing outside the vault, or that very
#      commit recorded the path ITSELF as a symlink.
# Nothing a person does by hand produces (2). A folder replaced by a link to
# somewhere else, in the same commit that removed everything inside it, is a
# machine doing it — this one.
#
# Condition (2) is also what makes this work on a SECOND computer, which never
# ran the relocation and has no sidecar at all: it pulled the commit, so it has
# the dangling link and the same deletion.
#
# WHAT IT DOES NOT CATCH: a vault where the deletion was committed but the link
# was never committed AND has since been removed. Nothing durable is left to
# distinguish that from a person deleting a folder on purpose, so this refuses
# to guess. `git log -- "<folder>"` shows it by hand.
#
# WHAT --repair DOES
#   · takes the link out of the way (never overwrites anything real)
#   · if the notes are still on this machine in the sidecar, moves them back —
#     these are the LIVE copies, so they win over the older ones in history
#   · anything still missing is restored from the commit BEFORE the deletion
#   · saves that as one ordinary commit, touching only the damaged folder
#   · never pushes. The next sync carries the repair to the other computers.
#
# It never deletes note content, never force-anythings, and refuses rather than
# overwrite whenever both a real folder and a saved copy exist.
#
# Pure bash + git + python3 (path handling only). No third-party deps.
#
# Usage:
#   repair-sidecar-note-deletion.sh <vault-path>              # look only, change nothing
#   repair-sidecar-note-deletion.sh <vault-path> --repair     # put the notes back
#
# Options:
#   --sidecar <dir>   sidecar root to consult for recorded moves
#                     (default: $BRAIN_SIDECAR or ~/.brain-sidecar)
#   --repair          perform the repair (default is look-only)
#   --quiet           only print the verdict line
#   -h, --help        this help
#
# Exit codes: 0 nothing wrong / repaired · 1 could not check, or a repair leg
#             failed · 2 usage · 10 damage found (look-only mode)
set -euo pipefail

# ---- folders an old version listed as "cache" and could therefore have taken --
# Used when the sidecar record is gone (or the vault was damaged on another
# machine). Recorded moves are read too and merged in.
CANDIDATE_DIRS=(
  "⚙️ Meta/Sessions"
  "⚙️ Meta/Worktree Snapshots"
  "⚙️ Meta/logs"
  "⚙️ Meta/graphify-out"
  "graphify-out"
  ".smart-env"
  ".codegraph"
  ".obsidian/.smart-env"
  ".claude/worktrees"
)

VAULT="" ; SIDECAR="${BRAIN_SIDECAR:-${MYCELIUM_SIDECAR:-$HOME/.brain-sidecar}}"
REPAIR=0 ; QUIET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --sidecar) SIDECAR="${2:?--sidecar needs a path}"; shift 2;;
    --repair)  REPAIR=1; shift;;
    --quiet)   QUIET=1; shift;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; exit 0;;
    -*) echo "unknown option: $1" >&2; exit 2;;
    *) if [ -z "$VAULT" ]; then VAULT="$1"; else echo "unexpected arg: $1" >&2; exit 2; fi; shift;;
  esac
done
[ -n "$VAULT" ] || { echo "usage: $(basename "$0") <vault-path> [--repair]" >&2; exit 2; }
[ -d "$VAULT" ] || { echo "not a directory: $VAULT" >&2; exit 2; }

say()  { [ "$QUIET" = 1 ] || printf '%s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*" >&2; }
err()  { printf 'ERROR %s\n' "$*" >&2; }

# `command -v` answers "is this name on PATH", which is a weaker claim than
# "this executes python". On Windows the gap is routine: the app-execution
# alias for python3 ships enabled by default in
# %LOCALAPPDATA%\Microsoft\WindowsApps, sits on PATH, satisfies `command -v`,
# and then exits non-zero printing an ad for the Microsoft Store -- while a real
# python is on the same PATH one candidate later. So the guard below passes and
# the script dies at the first real call with a third-party message instead of
# the one written here. Probe each candidate, same rule session-close-runner.sh
# uses.
PYBIN=""
for _abs_c in python3 python py; do
  _abs_p="$(command -v "$_abs_c" 2>/dev/null)" || continue
  [ -n "$_abs_p" ] || continue
  if "$_abs_p" -c 'import sys; sys.exit(0)' >/dev/null 2>&1; then
    PYBIN="$_abs_p"
    break
  fi
done
unset _abs_c _abs_p
[ -n "$PYBIN" ] || { err "python3 is required (used only to read paths safely)"; exit 1; }

VAULT="$(cd "$VAULT" && pwd -P)"

# Same sidecar path resolution and same per-vault slug as
# relocate-machinery-sidecar.sh, so this reads THIS vault's record and no other.
# A shared sidecar holds one record per vault; matching on the folder name alone
# would let a different vault's record answer for this one.
case "$SIDECAR" in /*) :;; ~*) SIDECAR="${SIDECAR/#\~/$HOME}";; *) SIDECAR="$PWD/$SIDECAR";; esac
SIDECAR="$("$PYBIN" -c 'import os,sys; print(os.path.realpath(os.path.expanduser(sys.argv[1])))' "$SIDECAR")"
_base="$(basename "$VAULT")"
_slug="$(printf '%s' "$_base" | tr -c 'A-Za-z0-9._-' '-' | sed 's/-\{2,\}/-/g; s/^-//; s/-$//')"
_hash="$(printf '%s' "$VAULT" | (shasum 2>/dev/null || sha1sum) | cut -c1-8)"
SLUG="${_slug:-vault}-${_hash}"

_tmp="$(mktemp -d)"
trap 'rm -rf "$_tmp"' EXIT

# ---- git access -------------------------------------------------------------
# A read that FAILS is not a read that found nothing. If git cannot be asked,
# this says so and stops rather than reporting a clean vault.
if ! command -v git >/dev/null 2>&1; then
  err "git is not installed, so this cannot check the vault's history."
  exit 1
fi
_gitdir_out=""; _rc=0
_gitdir_out="$(LC_ALL=C git -C "$VAULT" rev-parse --git-dir 2>&1)" || _rc=$?
if [ "$_rc" -ne 0 ]; then
  case "$_gitdir_out" in
    *"not a git repository"*)
      say "This folder is not tracked by git, so nothing could have been deleted from its history."
      say "OK: nothing to repair."
      exit 0;;
    *)
      err "git could not read $VAULT: $_gitdir_out"
      err "Nothing was checked. Fix that first — an unreadable vault is not a clean one."
      exit 1;;
  esac
fi

git_v() { LC_ALL=C git -C "$VAULT" "$@"; }

# ---- helpers ----------------------------------------------------------------

# Absolute destination of a symlink, WITHOUT requiring it to exist (the sidecar
# is gone on a second computer, and that is exactly the case that matters).
link_target_abs() {  # $1 = absolute path of the symlink
  "$PYBIN" - "$1" <<'PY'
import os, sys
p = sys.argv[1]
try:
    t = os.readlink(p)
except OSError:
    sys.exit(1)
if not os.path.isabs(t):
    t = os.path.join(os.path.dirname(p), t)
print(os.path.realpath(t))
PY
}

inside_vault() {  # $1 = absolute path; 0 = inside the vault
  case "$1/" in "$VAULT"/*) return 0;; *) return 1;; esac
}

# Vault-relative paths this vault has a RECORDED move for. Silent when there is
# no record — the built-in candidate list still covers those vaults.
recorded_rels() {
  local rec
  for rec in "$SIDECAR/manifests/$SLUG.json" "$SIDECAR/manifests/$SLUG.journal.jsonl"; do
    [ -f "$rec" ] || continue
    V="$VAULT" R="$rec" "$PYBIN" - <<'PY' || true
import json, os
src, vault = os.environ["R"], os.environ["V"]
recs = []
try:
    if src.endswith(".jsonl"):
        with open(src, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        recs.append(json.loads(line))
                    except ValueError:
                        continue
    else:
        with open(src, encoding="utf-8", errors="replace") as f:
            doc = json.load(f)
        if doc.get("vault") and os.path.realpath(doc["vault"]) != os.path.realpath(vault):
            recs = []
        else:
            recs = doc.get("moves", [])
except (OSError, ValueError):
    recs = []
for r in recs:
    if isinstance(r, dict) and r.get("type") in ("cache", "worktrees") and r.get("vault_rel"):
        print(r["vault_rel"] + "\t" + (r.get("target") or ""))
PY
  done
}

RECORDED="$_tmp/recorded.tsv"
recorded_rels > "$RECORDED" 2>/dev/null || : > "$RECORDED"

# Every path worth testing: the built-in candidates plus anything recorded.
candidate_rels() {
  { printf '%s\n' "${CANDIDATE_DIRS[@]}"; cut -f1 "$RECORDED"; } | awk 'NF && !seen[$0]++'
}

# Where a recorded move put this folder. Needed when the symlink is already gone
# (someone deleted it, or a half-finished rollback) — otherwise a second copy of
# the user's notes sits in the sidecar and nothing ever mentions it.
recorded_target_for() {  # $1 = rel
  awk -F'\t' -v k="$1" '$1==k && $2!="" {print $2; exit}' "$RECORDED" 2>/dev/null || true
}

# The most recent commit that DELETED tracked files under $1. Empty = none.
deletion_commit() {  # $1 = vault-relative path
  git_v log --diff-filter=D --format=%H -n 1 -- "$1" 2>/dev/null || true
}

# Did commit $1 record path $2 as a symlink? (mode 120000 at exactly that path.)
commit_recorded_symlink() {  # $1 = commit, $2 = rel
  local out
  out="$(git_v -c core.quotepath=false ls-tree "$1" -- "$2" 2>/dev/null || true)"
  case "$out" in 120000\ *) return 0;; *) return 1;; esac
}

# Files deleted by $1 under $2, NUL-separated, into $3.
deleted_paths_into() {  # $1 = commit, $2 = rel, $3 = out file
  git_v -c core.quotepath=false diff --diff-filter=D --name-only -z \
        "$1^" "$1" -- "$2" > "$3" 2>/dev/null || return 1
}

count_nul() { tr -cd '\0' < "$1" | wc -c | tr -d ' '; }

# Is the commit already on a remote branch — i.e. did the other computers get it?
pushed_already() {  # $1 = commit
  local out
  out="$(git_v branch -r --contains "$1" 2>/dev/null || true)"
  [ -n "$out" ]
}

# ---- detection --------------------------------------------------------------
DAMAGED_RELS=() ; DAMAGED_COMMITS=() ; DAMAGED_COUNTS=() ; DAMAGED_TARGETS=() ; DAMAGED_PUSHED=()
DAMAGED_LISTS=()

detect() {
  local rel link target commit n listf
  while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    link="$VAULT/$rel"

    commit="$(deletion_commit "$rel")"
    [ -n "$commit" ] || continue                      # nothing was ever deleted here

    # A root commit has no "before" to restore from; it also cannot be this bug.
    git_v rev-parse --verify --quiet "$commit^" >/dev/null 2>&1 || continue

    # Half two of the fingerprint — without it this is just someone deleting notes.
    target=""
    if [ -L "$link" ]; then
      target="$(link_target_abs "$link" || true)"
      if [ -z "$target" ] || inside_vault "$target"; then
        commit_recorded_symlink "$commit" "$rel" || continue
      fi
    else
      commit_recorded_symlink "$commit" "$rel" || continue
    fi
    # no live link to read: consult the record, so a stranded second copy of the
    # notes is still seen rather than silently left behind
    [ -n "$target" ] || target="$(recorded_target_for "$rel")"

    listf="$_tmp/deleted.${#DAMAGED_RELS[@]}"
    deleted_paths_into "$commit" "$rel" "$listf" || {
      warn "could not list what commit ${commit:0:8} removed under $rel — skipped"
      continue
    }
    n="$(count_nul "$listf")"
    [ "${n:-0}" -gt 0 ] || continue

    DAMAGED_RELS+=("$rel")
    DAMAGED_COMMITS+=("$commit")
    DAMAGED_COUNTS+=("$n")
    DAMAGED_TARGETS+=("$target")
    DAMAGED_LISTS+=("$listf")
    if pushed_already "$commit"; then DAMAGED_PUSHED+=("yes"); else DAMAGED_PUSHED+=("no"); fi
  done < <(candidate_rels)
}

report() {
  local i rel n commit target pushed when total=0
  say "Checking for notes an old relocation removed from this vault's history"
  say "  vault: $VAULT"
  say ""
  if [ "${#DAMAGED_RELS[@]}" -eq 0 ]; then
    say "Good news: nothing was deleted. Your notes and your history are intact."
    return 0
  fi
  for i in "${!DAMAGED_RELS[@]}"; do
    rel="${DAMAGED_RELS[$i]}"; n="${DAMAGED_COUNTS[$i]}"; commit="${DAMAGED_COMMITS[$i]}"
    target="${DAMAGED_TARGETS[$i]}"; pushed="${DAMAGED_PUSHED[$i]}"
    total=$((total + n))
    when="$(git_v log -1 --format=%cd --date=format:'%B %-d, %Y' "$commit" 2>/dev/null || echo 'an earlier date')"
    say "  \"$rel\""
    say "    $n note(s) were removed from your saved history on $when."
    if [ -n "$target" ] && [ -d "$target" ] && [ -d "$VAULT/$rel" ] && [ ! -L "$VAULT/$rel" ]; then
      say "    There are TWO copies of this folder: the one here, and one at:"
      say "      $target"
      say "    Nothing can be put back automatically until you decide which to keep."
    elif [ -n "$target" ] && [ -d "$target" ]; then
      say "    The notes themselves are still on this computer, at:"
      say "      $target"
    elif [ -n "$target" ]; then
      say "    The folder here is now a shortcut pointing at $target, which does not exist."
      say "    The notes come back out of your saved history."
    else
      say "    The notes come back out of your saved history."
    fi
    if [ "$pushed" = "yes" ]; then
      say "    Your other computers already received this, so they are missing the notes too."
    fi
    say ""
  done
  say "In total, $total note(s) need to be put back."
  return 0
}

# ---- repair -----------------------------------------------------------------

# Commit exactly one folder and nothing else — even if the hourly auto-snapshot
# has something unrelated staged in this vault right this second.
#
# `git commit --only -- <folder>` is the obvious way and it does NOT work here.
# HEAD holds a SYMLINK at that path while the worktree now holds a directory, and
# --only rebuilds a temporary index that dies on exactly that type change
# ("does not have a commit checked out"). So the commit is assembled directly:
# HEAD's tree, with this one folder replaced by what is in the index for it.
# Anything else staged stays staged and stays out of the commit.
commit_path_only() {  # $1 = rel, $2 = message
  local rel="$1" msg="$2" priv="$_tmp/commit-index" tree new
  rm -f "$priv"
  GIT_INDEX_FILE="$priv" git_v read-tree HEAD || return 1
  # clear whatever HEAD had at this path (the symlink), leaving the rest of HEAD
  GIT_INDEX_FILE="$priv" git_v ls-files -z -- "$rel" \
    | GIT_INDEX_FILE="$priv" git_v update-index -z --force-remove --stdin || return 1
  # and put the repaired folder in its place
  git_v ls-files -s -z -- "$rel" \
    | GIT_INDEX_FILE="$priv" git_v update-index -z --index-info || return 1
  tree="$(GIT_INDEX_FILE="$priv" git_v write-tree)" || return 1
  new="$(git_v commit-tree "$tree" -p HEAD -m "$msg")" || return 1
  git_v update-ref -m "$msg" HEAD "$new" || return 1
  # bring the real index in step for this folder only; leave everything else alone
  git_v reset -q "$new" -- "$rel" || return 1
  return 0
}

repair_one() {  # $1 rel, $2 commit, $3 target, $4 deleted-list; 0 ok / 1 failed
  local rel="$1" commit="$2" target="$3" listf="$4"
  local link="$VAULT/$rel" from_sidecar=0 d missing=0

  # 1. the shortcut goes away — but never on top of anything real.
  if [ -L "$link" ]; then
    if [ -n "$target" ] && [ -d "$target" ]; then
      rm -f "$link" || { err "  · $rel: could not remove the shortcut"; return 1; }
      mkdir -p "$(dirname "$link")" || { err "  · $rel: could not create the parent folder"; return 1; }
      mv "$target" "$link" || { err "  · $rel: could not move the notes back from $target"; return 1; }
      from_sidecar=1
      say "  · moved your notes back into $rel"
    else
      rm -f "$link" || { err "  · $rel: could not remove the shortcut"; return 1; }
      say "  · removed the broken shortcut at $rel"
    fi
  elif [ -e "$link" ] && [ -n "$target" ] && [ -d "$target" ]; then
    err "  · $rel NOT repaired: there is a real folder here AND a copy at $target."
    err "    Refusing to overwrite either. Look at both and keep the one you want."
    return 1
  fi

  # 2. put the FILE LIST back the way it was just before the deletion — index
  #    only, worktree untouched. This is what actually undoes the damage, and it
  #    is done through git rather than `git add` on purpose: `git add` obeys
  #    .gitignore, so a stray ignore rule would silently leave the notes out of
  #    history again and the repair would look like it worked.
  git_v reset -q "$commit^" -- "$rel" \
    || { err "  · $rel: could not restore the file list from your history"; return 1; }

  # 3. make the folder on disk match, without clobbering anything newer.
  if [ "$from_sidecar" = 1 ]; then
    # the live copies are already here; only fill genuine gaps
    while IFS= read -r -d '' d; do
      [ -n "$d" ] || continue
      if [ ! -e "$VAULT/$d" ]; then
        git_v checkout -- "$d" || { err "  · $rel: could not restore $d"; return 1; }
        missing=$((missing + 1))
      fi
    done < "$listf"
    [ "$missing" -eq 0 ] || say "  · restored $missing further note(s) from your history"
  else
    # nothing is on disk — one call brings the whole folder back
    git_v checkout -- "$rel" || { err "  · $rel: could not restore $rel from your history"; return 1; }
    say "  · restored $rel from your history"
  fi

  # 4. pick up anything newer than the copies in history, then save. Scoped to
  #    this folder so nothing else in flight is swept in.
  git_v add --all -- "$rel" || { err "  · $rel: could not stage the restored notes"; return 1; }
  if git_v diff --cached --quiet -- "$rel"; then
    say "  · $rel was already correct in your history — nothing to save"
    return 0
  fi
  # --only keeps the commit to exactly this folder even if something else is staged.
  commit_path_only "$rel" "restore notes an old machinery relocation deleted from $rel" \
    || { err "  · $rel: could not save the restored notes"; return 1; }
  say "  · saved the restored notes to your history"
  return 0
}

verify_one() {  # $1 rel — proves the repair at the artifact, not from its own prose
  local rel="$1" n
  [ -d "$VAULT/$rel" ] && [ ! -L "$VAULT/$rel" ] || { err "  · $rel is still not a real folder"; return 1; }
  n="$(git_v -c core.quotepath=false ls-files -- "$rel" 2>/dev/null | wc -l | tr -d ' ')"
  [ "${n:-0}" -gt 0 ] || { err "  · $rel holds no tracked notes after the repair"; return 1; }
  say "  · $rel: $n note(s) are back in your history"
  return 0
}

# ---- main -------------------------------------------------------------------
detect
report

if [ "${#DAMAGED_RELS[@]}" -eq 0 ]; then
  exit 0
fi

if [ "$REPAIR" != 1 ]; then
  say ""
  say "Nothing has been changed. To put the notes back, run the same command with --repair:"
  say "  bash $(basename "$0") \"$VAULT\" --repair"
  exit 10
fi

say ""
say "Putting your notes back ..."
FAILED=0
for i in "${!DAMAGED_RELS[@]}"; do
  repair_one "${DAMAGED_RELS[$i]}" "${DAMAGED_COMMITS[$i]}" "${DAMAGED_TARGETS[$i]}" \
           "${DAMAGED_LISTS[$i]}" \
    || { FAILED=$((FAILED + 1)); continue; }
  verify_one "${DAMAGED_RELS[$i]}" || FAILED=$((FAILED + 1))
done

say ""
if [ "$FAILED" -gt 0 ]; then
  err "$FAILED folder(s) could not be repaired — see the messages above. Nothing was deleted."
  exit 1
fi
say "Done. Your notes are back, and they are saved in your history again."
say "Your other computers get them back the next time this vault syncs."
exit 0
