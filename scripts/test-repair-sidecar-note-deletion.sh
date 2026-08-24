#!/usr/bin/env bash
# Fixture test for scripts/repair-sidecar-note-deletion.sh.
#
# It reproduces the WHOLE incident chain end to end, because every earlier
# safeguard passed on this bug and only the finished chain shows it:
#
#   1. a real vault whose "⚙️ Meta/Sessions" notes are TRACKED
#   2. the OLD relocation behaviour: move the folder to a sidecar, leave a symlink
#      (modelled directly — the current script correctly refuses to do this, and a
#      test that could only run through the fixed script could never build the
#      damaged state it has to repair)
#   3. the hourly auto-snapshot: `git add -A` + commit  → the deletion is now history
#   4. the multi-machine sync: `git push`               → the other computers have it
#   5. the repair
#
# NEGATIVE CONTROLS — the fingerprint has to DISCRIMINATE, or "found nothing" is
# worth nothing. Four vaults that must stay silent:
#   · a healthy vault
#   · a correctly relocated CACHE (untracked, no deletion in history) — the thing
#     the current script does on purpose, every day
#   · a person deleting a notes folder by hand and committing it
#   · a look-only run: it must change nothing at all
#
# Plus the case nobody thinks of until it eats data: the copy in the sidecar is
# NEWER than the copy in history. The repair must keep the newer one.
#
# Run: bash scripts/test-repair-sidecar-note-deletion.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SUT="$HERE/repair-sidecar-note-deletion.sh"
ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
fails=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; fails=$((fails+1)); }

gitq() { git -C "$1" "${@:2}" >/dev/null 2>&1; }

SESS="⚙️ Meta/Sessions"

# A vault the way a real one looks: notes under "⚙️ Meta/Sessions" are TRACKED,
# machinery caches are not.
make_vault() {  # $1 = vault dir, $2 = number of session notes
  local v="$1" n="${2:-4}" i
  mkdir -p "$v/$SESS" "$v/.smart-env"
  gitq "$v" init -b main
  gitq "$v" config user.email "t@t.test"
  gitq "$v" config user.name  "Test"
  gitq "$v" config commit.gpgsign false
  printf 'a note\n' > "$v/note.md"
  for i in $(seq 1 "$n"); do printf 'session %s body\n' "$i" > "$v/$SESS/2026-08-0$i-work.md"; done
  printf 'cache bytes\n' > "$v/.smart-env/index.bin"
  gitq "$v" add note.md "$SESS"
  gitq "$v" commit -m init
}

# Exactly what the OLD script did: move the folder out, leave a symlink behind,
# and write the move to the sidecar journal. No index test — that omission IS the
# bug being repaired. The journal is written for real (same schema, same per-vault
# slug) so the repair's record-reading path is exercised rather than assumed.
sidecar_slug() {  # $1 = vault (already absolute)
  local base slug hash
  base="$(basename "$1")"
  slug="$(printf '%s' "$base" | tr -c 'A-Za-z0-9._-' '-' | sed 's/-\{2,\}/-/g; s/^-//; s/-$//')"
  hash="$(printf '%s' "$1" | (shasum 2>/dev/null || sha1sum) | cut -c1-8)"
  printf '%s-%s\n' "${slug:-vault}" "$hash"
}

journal_move() {  # $1 = journal file, $2 = rel, $3 = absolute target
  J="$1" REL="$2" TGT="$3" python3 -c 'import json, os
rec = {"type": "cache", "vault_rel": os.environ["REL"],
       "target": os.path.realpath(os.environ["TGT"]), "mode": "sidecar"}
with open(os.environ["J"], "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")'
}

old_style_relocate() {  # $1 = vault, $2 = rel, $3 = sidecar root
  local v="$1" rel="$2" side="$3" vabs slug
  vabs="$(cd "$v" && pwd -P)"
  slug="$(sidecar_slug "$vabs")"
  mkdir -p "$side/$(dirname "$rel")" "$side/manifests"
  mv "$v/$rel" "$side/$rel"
  ln -s "$side/$rel" "$v/$rel"
  journal_move "$side/manifests/$slug.journal.jsonl" "$rel" "$side/$rel"
}

# The hourly auto-snapshot. `git add -A` is the real thing, verbatim.
auto_snapshot() { gitq "$1" add -A; gitq "$1" commit -m "auto-snapshot"; }

tracked_under() { git -C "$1" ls-files -- "$2" 2>/dev/null | wc -l | tr -d ' '; }
files_on_disk()  { find "$1/$2" -type f 2>/dev/null | wc -l | tr -d ' '; }

# =============================================================================
echo "--- 1. full chain: relocate → auto-commit → push → repair -----------------"
V1="$ROOT/v1"; S1="$ROOT/side1"; R1="$ROOT/remote1.git"
make_vault "$V1" 4
git init --bare -q -b main "$R1"
gitq "$V1" remote add origin "$R1"
gitq "$V1" push -u origin main

BEFORE_TRACKED="$(tracked_under "$V1" "$SESS")"
old_style_relocate "$V1" "$SESS" "$S1"
auto_snapshot "$V1"
gitq "$V1" push origin main                       # the loss reaches the other machines
AFTER_TRACKED="$(tracked_under "$V1" "$SESS")"

[ "$BEFORE_TRACKED" = "4" ] && [ "$AFTER_TRACKED" = "1" ] \
  && pass "fixture really destroyed the notes (4 tracked → 1, the symlink)" \
  || fail "fixture did not reproduce the damage (before=$BEFORE_TRACKED after=$AFTER_TRACKED)"

out="$(bash "$SUT" "$V1" --sidecar "$S1" 2>&1)"; rc=$?
[ "$rc" = "10" ] && pass "look-only exits 10 when damage is found" \
                 || fail "look-only exit was $rc, expected 10"
case "$out" in *"4 note(s) were removed"*) pass "names the right number of notes";;
               *) fail "did not name 4 notes: $out";; esac
case "$out" in *"other computers already received this"*) pass "says the loss already spread";;
               *) fail "did not report that the deletion was pushed";; esac
case "$out" in *"$S1"*) pass "points at where the notes still are";;
               *) fail "did not name the sidecar location";; esac

# look-only must change NOTHING
HEAD_BEFORE="$(git -C "$V1" rev-parse HEAD)"
STAT_BEFORE="$(git -C "$V1" status --porcelain)"
bash "$SUT" "$V1" --sidecar "$S1" >/dev/null 2>&1
[ "$HEAD_BEFORE" = "$(git -C "$V1" rev-parse HEAD)" ] && [ "$STAT_BEFORE" = "$(git -C "$V1" status --porcelain)" ] \
  && pass "look-only changed nothing" || fail "look-only modified the vault"

out="$(bash "$SUT" "$V1" --sidecar "$S1" --repair 2>&1)"; rc=$?
[ "$rc" = "0" ] && pass "--repair exits 0" || fail "--repair exit was $rc: $out"

[ -d "$V1/$SESS" ] && [ ! -L "$V1/$SESS" ] && pass "the folder is a real folder again" \
  || fail "the folder is still a symlink"
[ "$(tracked_under "$V1" "$SESS")" = "4" ] && pass "all 4 notes are tracked again" \
  || fail "tracked after repair = $(tracked_under "$V1" "$SESS"), expected 4"
[ "$(files_on_disk "$V1" "$SESS")" = "4" ] && pass "all 4 notes are back on disk" \
  || fail "on disk after repair = $(files_on_disk "$V1" "$SESS")"
grep -q 'session 3 body' "$V1/$SESS/2026-08-03-work.md" 2>/dev/null \
  && pass "note contents survived intact" || fail "note contents wrong after repair"
[ "$(git -C "$V1" ls-files -s -- "$SESS" | awk '$1=="120000"' | wc -l | tr -d ' ')" = "0" ] \
  && pass "the symlink is gone from the saved history" || fail "a symlink is still tracked"
git -C "$V1" status --porcelain -- "$SESS" | grep -q . \
  && fail "the folder is not clean after the repair" || pass "the folder is clean after the repair"

# the fix travels the same road the loss did
gitq "$V1" push origin main && pass "the repair pushes to the other computers" \
  || fail "could not push the repair"
[ "$(git -C "$R1" ls-tree -r --name-only main | grep -c "Sessions/")" = "4" ] \
  && pass "the remote has all 4 notes back" \
  || fail "remote has $(git -C "$R1" ls-tree -r --name-only main | grep -c 'Sessions/') notes"

# re-running is a no-op, not a second commit
H="$(git -C "$V1" rev-parse HEAD)"
bash "$SUT" "$V1" --sidecar "$S1" >/dev/null 2>&1; rc=$?
[ "$rc" = "0" ] && [ "$H" = "$(git -C "$V1" rev-parse HEAD)" ] \
  && pass "a repaired vault reports clean and is left alone" || fail "not idempotent (rc=$rc)"

# =============================================================================
echo "--- 2. the SECOND computer: only ever pulled the deletion ------------------"
# It never ran the relocation, has no sidecar, and the link dangles. The notes
# have to come back out of history alone.
V2="$ROOT/v2"; S2="$ROOT/side2"; R2="$ROOT/remote2.git"
VX="$ROOT/vx"
make_vault "$VX" 3
git init --bare -q -b main "$R2"
gitq "$VX" remote add origin "$R2"; gitq "$VX" push -u origin main
old_style_relocate "$VX" "$SESS" "$ROOT/sidex"
auto_snapshot "$VX"; gitq "$VX" push origin main
# the second computer never had the sidecar — that is the whole point of this case
rm -rf "${ROOT:?}/sidex"

git clone -q "$R2" "$V2"
[ -f "$V2/note.md" ] && pass "second-computer clone actually checked out a tree" \
  || fail "second-computer clone produced no checkout — everything below would test nothing"
gitq "$V2" config user.email "t@t.test"; gitq "$V2" config user.name "Test"
gitq "$V2" config commit.gpgsign false

[ -L "$V2/$SESS" ] && [ ! -e "$V2/$SESS" ] \
  && pass "second computer has a dangling link where its notes were" \
  || fail "second computer fixture is not the dangling-link state"

out="$(bash "$SUT" "$V2" --sidecar "$S2" 2>&1)"; rc=$?
[ "$rc" = "10" ] && pass "detected on a machine with no sidecar at all" \
                 || fail "second-computer detection exit was $rc: $out"
bash "$SUT" "$V2" --sidecar "$S2" --repair >/dev/null 2>&1; rc=$?
[ "$rc" = "0" ] && [ "$(tracked_under "$V2" "$SESS")" = "3" ] && [ "$(files_on_disk "$V2" "$SESS")" = "3" ] \
  && pass "second computer restored all 3 notes from history alone" \
  || fail "second-computer repair failed (rc=$rc tracked=$(tracked_under "$V2" "$SESS"))"

# =============================================================================
echo "--- 3. the live copy is NEWER than the copy in history ---------------------"
V3="$ROOT/v3"; S3="$ROOT/side3"
make_vault "$V3" 3
old_style_relocate "$V3" "$SESS" "$S3"
auto_snapshot "$V3"
# sessions kept being written after the relocation — straight into the sidecar
printf 'EDITED AFTER THE MOVE\n' > "$S3/$SESS/2026-08-02-work.md"
printf 'brand new session\n'     > "$S3/$SESS/2026-08-09-new.md"
bash "$SUT" "$V3" --sidecar "$S3" --repair >/dev/null 2>&1
grep -q 'EDITED AFTER THE MOVE' "$V3/$SESS/2026-08-02-work.md" 2>/dev/null \
  && pass "the newer edit won over the older copy in history" \
  || fail "the repair clobbered a newer note with an older one"
[ -f "$V3/$SESS/2026-08-09-new.md" ] && [ "$(tracked_under "$V3" "$SESS")" = "4" ] \
  && pass "a session written after the move was kept and saved" \
  || fail "a post-move session was lost (tracked=$(tracked_under "$V3" "$SESS"))"

# =============================================================================
echo "--- 4. it refuses rather than overwrite ------------------------------------"
V4="$ROOT/v4"; S4="$ROOT/side4"
make_vault "$V4" 2
old_style_relocate "$V4" "$SESS" "$S4"
auto_snapshot "$V4"
rm "$V4/$SESS"                      # link gone, but the sidecar copy is still there
mkdir -p "$V4/$SESS"; printf 'someone put something here\n' > "$V4/$SESS/manual.md"
out="$(bash "$SUT" "$V4" --sidecar "$S4" --repair 2>&1)"; rc=$?
[ "$rc" = "1" ] && pass "refuses when a real folder AND a saved copy both exist" \
                || fail "did not refuse the two-copies case (rc=$rc)"
[ -f "$V4/$SESS/manual.md" ] && [ -d "$S4/$SESS" ] \
  && pass "refusing destroyed neither copy" || fail "the refusal lost data"

# =============================================================================
echo "--- 5. NEGATIVE CONTROLS: it must stay silent ------------------------------"
VN="$ROOT/healthy"; make_vault "$VN" 3
out="$(bash "$SUT" "$VN" --sidecar "$ROOT/nosidecar" 2>&1)"; rc=$?
[ "$rc" = "0" ] && pass "healthy vault: silent, exit 0" || fail "healthy vault flagged (rc=$rc): $out"

# the correct, everyday relocation of a genuine untracked cache
VC="$ROOT/cacheok"; SC="$ROOT/sidec"; make_vault "$VC" 3
old_style_relocate "$VC" ".smart-env" "$SC"
auto_snapshot "$VC"
out="$(bash "$SUT" "$VC" --sidecar "$SC" 2>&1)"; rc=$?
[ "$rc" = "0" ] && pass "a correctly relocated cache is NOT flagged" \
                || fail "flagged an untracked cache relocation (rc=$rc): $out"

# a person deleting a notes folder on purpose
VH="$ROOT/byhand"; make_vault "$VH" 3
rm -rf "${VH:?}/$SESS"; auto_snapshot "$VH"
out="$(bash "$SUT" "$VH" --sidecar "$ROOT/nosidecar" 2>&1)"; rc=$?
[ "$rc" = "0" ] && pass "a deliberate deletion by hand is NOT flagged" \
                || fail "flagged a hand deletion (rc=$rc): $out"

# a folder that is not a git repo at all
VP="$ROOT/plain"; mkdir -p "$VP"
out="$(bash "$SUT" "$VP" 2>&1)"; rc=$?
[ "$rc" = "0" ] && pass "a non-git folder is reported clean, not crashed" || fail "non-git exit $rc"

# git answers with an ERROR — "could not look" must never read as "nothing there".
# A corrupt .git is NOT this case: git calls that "not a git repository", which is
# a real answer. The case that matters is git failing to answer at all.
mkdir -p "$ROOT/bin"
cat > "$ROOT/bin/git" <<'SHIM'
#!/bin/sh
echo "fatal: unable to read the index file" >&2
exit 128
SHIM
chmod +x "$ROOT/bin/git"
out="$(PATH="$ROOT/bin:$PATH" bash "$SUT" "$VN" 2>&1)"; rc=$?
[ "$rc" = "1" ] && pass "a git that cannot answer fails closed (exit 1), never 'clean'" \
                || fail "unreadable vault exited $rc, expected 1: $out"

# ...and the OTHER arm of the same shim, so the fail-closed test above is proven
# to discriminate rather than just always exiting 1: git answering "this is not a
# repository" IS an answer, and must read as clean.
cat > "$ROOT/bin/git" <<'SHIM'
#!/bin/sh
echo "fatal: not a git repository (or any of the parent directories): .git" >&2
exit 128
SHIM
chmod +x "$ROOT/bin/git"
out="$(PATH="$ROOT/bin:$PATH" bash "$SUT" "$VN" 2>&1)"; rc=$?
[ "$rc" = "0" ] && pass "'not a repository' is an ANSWER and reads as clean (exit 0)" \
                || fail "'not a repository' exited $rc, expected 0: $out"

# =============================================================================
echo "--- 6. the doorbell: the affected user is TOLD, unprompted -----------------"
# This population is non-technical by definition. A repair nobody is told about
# repairs nobody, so the SessionStart hook has to name it without being asked.
HOOK="$(cd "$HERE/.." && pwd)/hooks/worktree-footprint-signal.py"
HSCRATCH="$ROOT/hookscratch"; mkdir -p "$HSCRATCH/empty-dev"

hook_ctx() {  # $1 = cwd
  ( cd "$1" && env WORKTREE_FREE_GB=0 WORKTREE_WARN=9999 \
      ABS_DEV_ROOT="$HSCRATCH/empty-dev" \
      WORKTREE_FOOTPRINT_CACHE="$HSCRATCH/bloat-cache.json" \
      WORKTREE_FOOTPRINT_FILE_WARN=999999999 \
      OBSIDIAN_CONFIG="$ROOT/no-such-obsidian.json" \
      python3 "$HOOK" </dev/null ) | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
except (ValueError, OSError):
    print(""); sys.exit(0)
print((d.get("hookSpecificOutput") or {}).get("additionalContext", "") if isinstance(d, dict) else "")'
}

VD="$ROOT/doorbell"; SD="$ROOT/sided"
make_vault "$VD" 3; : > "$VD/CLAUDE.md"
gitq "$VD" add CLAUDE.md; gitq "$VD" commit -m claude
ctx="$(hook_ctx "$VD")"
case "$ctx" in *"notes-deleted"*) fail "fired on a healthy vault";;
               *) pass "healthy vault: the alert stays quiet";; esac

old_style_relocate "$VD" "$SESS" "$SD"
auto_snapshot "$VD"
ctx="$(hook_ctx "$VD")"
case "$ctx" in *"notes-deleted"*) pass "damaged vault: the alert fires unprompted";;
               *) fail "the alert did not fire on a damaged vault: ${ctx:0:160}";; esac
case "$ctx" in *"repair-sidecar-note-deletion.sh"*) pass "names the repair command";;
               *) fail "did not name the repair command";; esac
case "$ctx" in *"--repair"*) pass "names the step that actually puts them back";;
               *) fail "did not name --repair";; esac

# and it goes quiet once the repair has run — a permanent alarm is wallpaper
bash "$SUT" "$VD" --sidecar "$SD" --repair >/dev/null 2>&1
ctx="$(hook_ctx "$VD")"
case "$ctx" in *"notes-deleted"*) fail "still firing after the repair";;
               *) pass "goes quiet once the notes are back";; esac

# a correctly relocated CACHE must never ring it
VDC="$ROOT/doorbell-cache"; SDC="$ROOT/sidedc"
make_vault "$VDC" 3; : > "$VDC/CLAUDE.md"
gitq "$VDC" add CLAUDE.md; gitq "$VDC" commit -m claude
old_style_relocate "$VDC" ".smart-env" "$SDC"
auto_snapshot "$VDC"
ctx="$(hook_ctx "$VDC")"
case "$ctx" in *"notes-deleted"*) fail "rang on an ordinary cache relocation";;
               *) pass "an ordinary cache relocation does not ring it";; esac

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; exit 0; fi
echo "$fails FAILING"; exit 1
