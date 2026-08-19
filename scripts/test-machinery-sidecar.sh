#!/usr/bin/env bash
# Negative-control + churn-burst test for relocate-machinery-sidecar.sh.
#
# The Done= predicate for the "vault may live in iCloud" mode is: after a full
# churn burst (worktree create + commits + `git gc`) on a vault, ZERO machinery
# CONTENT bytes live inside the (synced) vault tree — only static symlinks + the
# one-line `.git` pointer remain. We can't drive iCloud in CI, so we model
# "synced scope" as the vault tree itself and assert the heavy machinery content
# is OUT of it. `find -type f` does not follow symlinks, so relocated content
# (symlinked to the sidecar) is correctly counted as "out of tree".
#
# A guard earns trust only by failing on the thing it catches: the NEGATIVE
# CONTROL repeats the identical churn on a NON-relocated vault and asserts the
# machinery IS in-tree there — proving the metric discriminates.
#
# Run: bash scripts/test-machinery-sidecar.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
HELPER="$HERE/relocate-machinery-sidecar.sh"
ROOT="$(mktemp -d)"
SIDE="$ROOT/sidecar"
trap 'rm -rf "$ROOT"' EXIT
fails=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; fails=$((fails+1)); }

gitq() { git -C "$1" "${@:2}" >/dev/null 2>&1; }

make_vault() {  # $1 = vault dir
  local v="$1"
  mkdir -p "$v"
  gitq "$v" init
  gitq "$v" config user.email "t@t.test"
  gitq "$v" config user.name  "Test"
  gitq "$v" config commit.gpgsign false
  mkdir -p "$v/.smart-env" "$v/.codegraph" "$v/.claude/worktrees" "$v/⚙️ Meta/Sessions"
  printf 'note\n'  > "$v/note.md"
  printf 'idx\n'   > "$v/.smart-env/index.bin"
  printf 'cg\n'    > "$v/.codegraph/graph.bin"
  printf 'sess\n'  > "$v/⚙️ Meta/Sessions/s1.md"
  gitq "$v" add note.md
  gitq "$v" commit -m init
}

# A vault whose "⚙️ Meta/Sessions" notes are COMMITTED — i.e. a real one. The
# other machinery dirs stay untracked, so one fixture exercises both sides of
# the rule: tracked content is never moved, untracked caches still are.
make_vault_tracked() {  # $1 = vault dir
  local v="$1" i
  mkdir -p "$v/⚙️ Meta/Sessions" "$v/⚙️ Meta/logs" "$v/.smart-env" "$v/.codegraph" "$v/.claude/worktrees"
  gitq "$v" init
  gitq "$v" config user.email "t@t.test"
  gitq "$v" config user.name  "Test"
  gitq "$v" config commit.gpgsign false
  printf 'note\n' > "$v/note.md"
  for i in 1 2 3; do printf 'session %s\n' "$i" > "$v/⚙️ Meta/Sessions/2026-08-0$i-work.md"; done
  printf 'idx\n' > "$v/.smart-env/index.bin"
  printf 'cg\n'  > "$v/.codegraph/graph.bin"
  printf 'log\n' > "$v/⚙️ Meta/logs/run.log"
  gitq "$v" add note.md "⚙️ Meta/Sessions"
  gitq "$v" commit -m init
}

# How many paths git reports as DELETED. This is the number the user sees, the
# number auto-snapshot commits, and the number multi-machine sync pushes.
deleted_in_status() {  # $1 = vault dir
  git -C "$1" status --porcelain 2>/dev/null | awk '/^.D|^D/{n++} END{print n+0}'
}

# Count REGULAR machinery files physically inside the vault tree (symlinks not
# followed). Excludes the `.git` POINTER FILE (a tiny static pointer is allowed).
machinery_files_in_tree() {  # $1 = vault dir
  local v="$1" n
  n="$(find "$v" -type f \
        \( -path '*/.git/*' -o -path '*/.smart-env/*' -o -path '*/.codegraph/*' \
           -o -path '*/.claude/worktrees/*' -o -path '*/Sessions/*' \) \
        2>/dev/null | wc -l | tr -d ' ')"
  echo "${n:-0}"
}

churn() {  # $1 = vault dir — simulate a real session's git churn
  local v="$1"
  # a worktree (lands wherever .claude/worktrees resolves to)
  gitq "$v" worktree add "$v/.claude/worktrees/wt1" -b churnbranch HEAD || \
    gitq "$v" worktree add --detach "$v/.claude/worktrees/wt1" HEAD || true
  # several commits to generate loose objects
  local i
  for i in 1 2 3 4 5; do
    printf 'rev %s\n' "$i" > "$v/note.md"
    gitq "$v" add note.md
    gitq "$v" commit -m "rev$i"
  done
  gitq "$v" gc --aggressive --prune=now   # the wholesale-rewrite hazard
}

echo "=== machinery-sidecar: relocate + churn-burst + negative control ==="

# ---------------------------------------------------------------------------
# A. RELOCATED vault: machinery must be out of the synced tree, even after churn
# ---------------------------------------------------------------------------
VA="$ROOT/A"
make_vault "$VA"
before="$(machinery_files_in_tree "$VA")"
[ "$before" -gt 0 ] && pass "A pre-relocate: machinery present in tree ($before files)" \
                    || fail "A pre-relocate: expected machinery in tree, got $before"

bash "$HELPER" "$VA" --sidecar "$SIDE" --quiet
rc=$?
[ "$rc" = 0 ] && pass "A relocate exit 0" || fail "A relocate exit=$rc"

[ -f "$VA/.git" ] && [ ! -d "$VA/.git" ] && pass "A .git is a pointer file" \
                                         || fail "A .git is not a pointer file"
grep -q '^gitdir: ' "$VA/.git" 2>/dev/null && pass "A .git pointer well-formed" \
                                            || fail "A .git pointer malformed"
for d in ".smart-env" ".codegraph" ".claude/worktrees" "⚙️ Meta/Sessions"; do
  [ -L "$VA/$d" ] && pass "A $d is a symlink (out of tree)" || fail "A $d not a symlink"
done

after="$(machinery_files_in_tree "$VA")"
[ "$after" = 0 ] && pass "A post-relocate: ZERO machinery files in tree" \
                 || fail "A post-relocate: expected 0, got $after"

gitq "$VA" status && pass "A git still functional after relocation" \
                  || fail "A git broke after relocation"

churn "$VA"
churned="$(machinery_files_in_tree "$VA")"
[ "$churned" = 0 ] && pass "A post-CHURN (gc+worktree+5 commits): STILL zero machinery in tree" \
                   || fail "A post-churn: machinery leaked into tree ($churned files)"

# worktree content must physically live in the sidecar, not the vault
find "$SIDE/worktrees" -type f 2>/dev/null | grep -q . \
  && pass "A churned worktree content lives in sidecar" \
  || fail "A worktree content not found in sidecar"

# ---------------------------------------------------------------------------
# B. NEGATIVE CONTROL: identical churn on a NON-relocated vault → machinery IS
#    in-tree (proves the metric is not trivially always-zero)
# ---------------------------------------------------------------------------
VB="$ROOT/B"
make_vault "$VB"
churn "$VB"
ctrl="$(machinery_files_in_tree "$VB")"
[ "$ctrl" -gt 0 ] && pass "B negative control: machinery IS in tree without relocation ($ctrl files)" \
                   || fail "B negative control: expected machinery in tree, got $ctrl"

# ---------------------------------------------------------------------------
# C. IDEMPOTENCY: re-running relocate on a fresh (no-churn) vault is a clean no-op
#    (A is excluded here: its churn registered a worktree, which the no-live-
#     worktree guard correctly refuses — that path is covered by block E.)
# ---------------------------------------------------------------------------
VF="$ROOT/F"
make_vault "$VF"
bash "$HELPER" "$VF" --sidecar "$SIDE" --quiet
out="$(bash "$HELPER" "$VF" --sidecar "$SIDE" 2>&1)"; rc=$?   # non-quiet: surface "already" skip lines
[ "$rc" = 0 ] && pass "C re-run exit 0 (idempotent)" || fail "C re-run exit=$rc"
echo "$out" | grep -q 'already' && pass "C re-run reports already-relocated" \
                                 || fail "C re-run did not detect existing state"
still="$(machinery_files_in_tree "$VF")"
[ "$still" = 0 ] && pass "C re-run kept tree clean" || fail "C re-run dirtied tree ($still)"

# ---------------------------------------------------------------------------
# D. ROLLBACK: --rollback restores a normal local repo
# ---------------------------------------------------------------------------
VC="$ROOT/C"
make_vault "$VC"
bash "$HELPER" "$VC" --sidecar "$SIDE" --quiet
[ -f "$VC/.git" ] && pass "D pre-rollback .git is a pointer" || fail "D pre-rollback .git not a pointer"
bash "$HELPER" "$VC" --sidecar "$SIDE" --rollback --quiet
rc=$?
[ "$rc" = 0 ] && pass "D rollback exit 0" || fail "D rollback exit=$rc"
[ -d "$VC/.git" ] && pass "D .git is a normal dir again" || fail "D .git not restored to dir"
[ -d "$VC/.smart-env" ] && [ ! -L "$VC/.smart-env" ] && pass "D .smart-env restored as real dir" \
                                                       || fail "D .smart-env not restored"
gitq "$VC" status && pass "D git functional after rollback" || fail "D git broke after rollback"

# ---------------------------------------------------------------------------
# D2. ROLLBACK SURVIVES AN IDEMPOTENT RE-RUN (manifest-clobber regression):
#     relocate -> re-run (records zero new moves) -> rollback must STILL restore
#     the repo. A re-run that overwrites the manifest with moves:[] silently
#     breaks --rollback. This is the negative control for that fix (MYC-2383).
# ---------------------------------------------------------------------------
VG="$ROOT/G"
make_vault "$VG"
bash "$HELPER" "$VG" --sidecar "$SIDE" --quiet
bash "$HELPER" "$VG" --sidecar "$SIDE" --quiet            # idempotent re-run
bash "$HELPER" "$VG" --sidecar "$SIDE" --rollback --quiet
rc=$?
[ "$rc" = 0 ] && pass "D2 rollback after re-run exit 0" || fail "D2 rollback after re-run exit=$rc"
[ -d "$VG/.git" ] && pass "D2 .git restored to a dir after re-run+rollback" \
                  || fail "D2 .git NOT restored (manifest clobbered by re-run)"
[ -d "$VG/.smart-env" ] && [ ! -L "$VG/.smart-env" ] && pass "D2 .smart-env restored after re-run+rollback" \
                                                      || fail "D2 .smart-env NOT restored after re-run"

# ---------------------------------------------------------------------------
# E. LIVE-WORKTREE GUARD: refuse (exit 1) when a linked worktree exists
# ---------------------------------------------------------------------------
VD="$ROOT/D"
make_vault "$VD"
gitq "$VD" worktree add "$VD/.claude/worktrees/live" -b livebranch HEAD
bash "$HELPER" "$VD" --sidecar "$SIDE" --quiet >/dev/null 2>&1
rc=$?
[ "$rc" = 1 ] && pass "E refuses with live linked worktree (exit 1)" \
              || fail "E expected refuse exit 1, got $rc"
# and --force overrides
bash "$HELPER" "$VD" --sidecar "$SIDE" --force --quiet >/dev/null 2>&1
rc=$?
[ "$rc" = 0 ] && pass "E --force overrides the guard (exit 0)" || fail "E --force exit=$rc"

# ---------------------------------------------------------------------------
# F. DRY-RUN: changes nothing
# ---------------------------------------------------------------------------
VE="$ROOT/E"
make_vault "$VE"
bash "$HELPER" "$VE" --sidecar "$SIDE" --dry-run --quiet >/dev/null 2>&1
[ -d "$VE/.git" ] && [ ! -L "$VE/.smart-env" ] && pass "F dry-run changed nothing" \
                                               || fail "F dry-run mutated the vault"

# ---------------------------------------------------------------------------
# G. TRACKED NOTES ARE NEVER RELOCATED  (the data-loss regression)
#    "⚙️ Meta/Sessions" is BOTH a CACHE_DIRS name AND, in a real vault, over a
#    thousand tracked markdown notes. Relocating it turns the directory into an
#    absolute symlink, `git status` reports every note as DELETED, the hourly
#    auto-snapshot (`git add -A`) COMMITS that deletion and the multi-machine
#    sync PUSHES it — while the script prints "your notes did not move".
#    The rule is the git INDEX, never the name. Negative control: a genuinely
#    untracked cache still moves, so the rule is not "move nothing".
# ---------------------------------------------------------------------------
VH="$ROOT/H"
make_vault_tracked "$VH"
gout="$(bash "$HELPER" "$VH" --sidecar "$SIDE" 2>&1)"; rc=$?
[ "$rc" = 0 ] && pass "G relocate exit 0 on a vault with tracked notes" || fail "G relocate exit=$rc"
gdel="$(deleted_in_status "$VH")"
[ "$gdel" = 0 ] && pass "G ZERO deletions in git status after relocation" \
                || fail "G git status reports $gdel deleted file(s) — tracked notes were moved"
{ [ -d "$VH/⚙️ Meta/Sessions" ] && [ ! -L "$VH/⚙️ Meta/Sessions" ]; } \
  && pass "G tracked ⚙️ Meta/Sessions left as a real directory" \
  || fail "G tracked ⚙️ Meta/Sessions was replaced by a symlink"
[ -f "$VH/⚙️ Meta/Sessions/2026-08-01-work.md" ] && pass "G the tracked notes are still on disk" \
                                                  || fail "G tracked notes are gone"
echo "$gout" | grep -q 'KEPT: git tracks' && pass "G reports WHY the path was kept" \
                                           || fail "G silent about the skip"
[ -L "$VH/.smart-env" ] && pass "G neg-control: untracked .smart-env still relocated" \
                        || fail "G neg-control: untracked cache was NOT relocated (rule too broad)"
[ -L "$VH/⚙️ Meta/logs" ] && pass "G neg-control: untracked ⚙️ Meta/logs still relocated" \
                          || fail "G neg-control: untracked emoji-path cache was NOT relocated"

# ---------------------------------------------------------------------------
# H. INTERRUPT MATRIX: --rollback works from EVERY kill point
#    The manifest used to be written on the LAST line, so any interrupt left no
#    record at all and `--rollback` hard-refused (exit 2). A SIGKILL between the
#    `mv` and the `ln -s` left the directory ABSENT — no dir, no symlink, all the
#    content stranded in the sidecar, no way back. Now every move is journalled
#    (append + fsync) BEFORE it happens. BRAIN_SIDECAR_TEST_KILL_AT is the
#    script's test-only fault-injection seam: SIGKILL right after a named step.
# ---------------------------------------------------------------------------
interrupt_case() {  # $1 = kill label, $2 = short id
  local label="$1" id="$2" v side krc rrc d
  v="$ROOT/I-$id"; side="$ROOT/I-$id-side"
  make_vault_tracked "$v"
  BRAIN_SIDECAR_TEST_KILL_AT="$label" bash "$HELPER" "$v" --sidecar "$side" --quiet >/dev/null 2>&1
  krc=$?
  [ "$krc" = 137 ] || fail "H[$id] expected SIGKILL (137) at '$label', got $krc"
  bash "$HELPER" "$v" --sidecar "$side" --rollback --quiet >/dev/null 2>&1
  rrc=$?
  if [ "$rrc" != 0 ]; then
    fail "H[$id] rollback after kill at '$label' exit=$rrc"
    return
  fi
  d="$(deleted_in_status "$v")"
  if [ -d "$v/.git" ] && [ ! -L "$v/.git" ] \
     && [ -d "$v/.smart-env" ] && [ ! -L "$v/.smart-env" ] && [ -f "$v/.smart-env/index.bin" ] \
     && [ -d "$v/⚙️ Meta/logs" ] && [ ! -L "$v/⚙️ Meta/logs" ] && [ -f "$v/⚙️ Meta/logs/run.log" ] \
     && [ -f "$v/⚙️ Meta/Sessions/2026-08-01-work.md" ] && [ "$d" = 0 ]; then
    pass "H[$id] kill at '$label' -> rollback restored everything (0 deletions)"
  else
    fail "H[$id] kill at '$label' -> vault NOT fully restored (git-status deletions=$d)"
  fi
}
interrupt_case "post-journal:.git"        gitpre
interrupt_case "post-git"                 gitpost
interrupt_case "post-journal:.smart-env"  cachepre
interrupt_case "post-mv:.smart-env"       cachemid
interrupt_case "post-link:.smart-env"     cachepost
interrupt_case "post-mv:⚙️ Meta/logs"      nested

# H2. the kill lands BEFORE any manifest could be written — the write-ahead
#     journal is the only reason rollback has anything to read.
VJ="$ROOT/J"; SJ="$ROOT/J-side"
make_vault_tracked "$VJ"
BRAIN_SIDECAR_TEST_KILL_AT="post-link:.smart-env" bash "$HELPER" "$VJ" --sidecar "$SJ" --quiet >/dev/null 2>&1
ls "$SJ/manifests/"*.json >/dev/null 2>&1 \
  && fail "H2 a manifest exists — the premise (killed before the manifest) is wrong" \
  || pass "H2 killed before any manifest was written"
ls "$SJ/manifests/"*.journal.jsonl >/dev/null 2>&1 \
  && pass "H2 the write-ahead journal survived the kill" \
  || fail "H2 no journal — rollback would have nothing to read"

# ---------------------------------------------------------------------------
# I. A FAILED ROLLBACK SAYS SO (and keeps the evidence)
#    `local rc=0` was never reassigned, a failed restore was only a `warn`, and
#    `rm -f "$MANIFEST"` ran unconditionally — so a rollback that restored
#    NOTHING printed "Rollback complete.", exited 0, and deleted the only map
#    back. Here the sidecar gitdir goes missing (unplugged drive / sync deleted
#    it): the cache legs restore, the git leg cannot, and that must be loud.
# ---------------------------------------------------------------------------
VK="$ROOT/K"; SK="$ROOT/K-side"
make_vault "$VK"
bash "$HELPER" "$VK" --sidecar "$SK" --quiet
rm -rf "$SK/git"
kout="$(bash "$HELPER" "$VK" --sidecar "$SK" --rollback 2>&1)"; rc=$?
[ "$rc" != 0 ] && pass "I rollback exits non-zero when a leg fails (rc=$rc)" \
               || fail "I rollback reported success while restoring nothing"
echo "$kout" | grep -q 'INCOMPLETE' && pass "I rollback names the failure" \
                                     || fail "I rollback did not report the failed leg"
ls "$SK/manifests/"*.jsonl >/dev/null 2>&1 \
  && pass "I the relocation record was KEPT after a failed rollback" \
  || fail "I the record was deleted — the only map back is gone"
[ -f "$VK/.git" ] && pass "I the dangling .git pointer was left for inspection, not silently removed" \
                  || fail "I the .git pointer was removed without restoring the gitdir"

# ---------------------------------------------------------------------------
# J. A PROBE THAT CANNOT RUN COUNTS AS LIVE (fail closed)
#    `git worktree list ... 2>/dev/null | awk` gave the same empty answer for
#    "no worktrees" and "git refused to look" — so a `dubious ownership` error
#    (real on restored / migrated / external-drive / network / Windows vaults)
#    defeated the refusal and separate-git-dir orphaned live worktrees anyway.
#    The shim below fails ONLY `git worktree list`; everything else is real git.
# ---------------------------------------------------------------------------
VL="$ROOT/L"; SL="$ROOT/L-side"
make_vault "$VL"
mkdir -p "$ROOT/gitshim"
REALGIT="$(command -v git)"
printf '#!/bin/bash\ncase " $* " in *" worktree list "*) echo "fatal: detected dubious ownership in repository at %s" >&2; exit 128;; esac\nexec %s "$@"\n' \
  "$VL" "$REALGIT" > "$ROOT/gitshim/git"
chmod +x "$ROOT/gitshim/git"
lout="$(PATH="$ROOT/gitshim:$PATH" bash "$HELPER" "$VL" --sidecar "$SL" 2>&1)"; rc=$?
[ "$rc" = 1 ] && pass "J refuses when the live-worktree probe cannot run (exit 1)" \
              || fail "J expected exit 1, got $rc — an errored probe read as 'nothing live'"
echo "$lout" | grep -q 'could not run' && pass "J explains that a probe failed" \
                                        || fail "J did not explain the refusal"
[ -d "$VL/.git" ] && pass "J the vault was not touched by the refused run" \
                  || fail "J the vault was modified despite the refusal"
bash "$HELPER" "$VL" --sidecar "$SL" --quiet >/dev/null 2>&1; rc=$?
[ "$rc" = 0 ] && pass "J neg-control: same vault with a working git relocates (exit 0)" \
              || fail "J neg-control exit=$rc"

# ---------------------------------------------------------------------------
# K. THE SIDECAR DESTINATION IS CHECKED TOO
#    $HOME can itself be synced (Windows roaming profile, OneDrive-managed or
#    network home), so the default ~/.brain-sidecar lands INSIDE a sync root:
#    .git moves from one synced tree to another, every success check passes,
#    and the melt is unchanged — now with the notes split off as well.
# ---------------------------------------------------------------------------
VM="$ROOT/M"
make_vault "$VM"
mout="$(bash "$HELPER" "$VM" --sidecar "$ROOT/OneDrive/brain-sidecar" 2>&1)"; rc=$?
[ "$rc" = 1 ] && pass "K refuses a sidecar inside a cloud-sync root (exit 1)" \
              || fail "K expected exit 1, got $rc — machinery would move synced-tree to synced-tree"
echo "$mout" | grep -q 'refusing: the sidecar destination is inside OneDrive' \
  && pass "K names the sync service in the refusal" \
  || fail "K did not name the sync service in a refusal line"
[ -d "$VM/.git" ] && pass "K vault untouched by the refusal" || fail "K vault modified despite the refusal"
mout="$(bash "$HELPER" "$VM" --sidecar "$VM/.sidecar" 2>&1)"; rc=$?
[ "$rc" = 1 ] && pass "K refuses a sidecar INSIDE the vault (exit 1)" || fail "K sidecar-inside-vault exit=$rc"
bash "$HELPER" "$VM" --sidecar "$ROOT/M-side" --quiet >/dev/null 2>&1; rc=$?
[ "$rc" = 0 ] && pass "K neg-control: a local sidecar is accepted (exit 0)" || fail "K neg-control exit=$rc"

# ---------------------------------------------------------------------------
# L. THE VAULT PATH IS DATA, NEVER CODE
#    run() used `eval "$@"`, so every path was expanded a SECOND time: a folder
#    named `Notes $HOME Backup` mis-resolved, and the same eval drove mv, rm -f
#    and ln -s — so a folder name containing a backtick or $( ) would have
#    EXECUTED. Now run() takes an argv array and no shell ever sees the path.
# ---------------------------------------------------------------------------
VN="$ROOT/Notes \$HOME Backup"
make_vault_tracked "$VN"
bash "$HELPER" "$VN" --sidecar "$ROOT/N-side" --quiet >/dev/null 2>&1; rc=$?
[ "$rc" = 0 ] && pass "L a vault path containing a literal \$HOME relocates (exit 0)" \
              || fail "L exit=$rc — the path was expanded twice"
{ [ -L "$VN/.smart-env" ] && [ -f "$VN/.smart-env/index.bin" ]; } \
  && pass "L cache relocated and still readable through the symlink" \
  || fail "L relocation mis-resolved the doubly-expanded path"
ndel="$(deleted_in_status "$VN")"
[ "$ndel" = 0 ] && pass "L no deletions in the \$HOME-named vault" || fail "L $ndel deletion(s)"

VP="$ROOT/Notes \$(touch pwned-proof) X"
make_vault_tracked "$VP"
( cd "$ROOT" && bash "$HELPER" "$VP" --sidecar "$ROOT/P-side" --quiet >/dev/null 2>&1 )
[ -e "$ROOT/pwned-proof" ] \
  && fail "L a folder name EXECUTED — command substitution in the path ran" \
  || pass "L a folder name containing \$( ) is data, not code"
[ -L "$VP/.smart-env" ] && pass "L the \$( )-named vault still relocated correctly" \
                        || fail "L the \$( )-named vault did not relocate"


echo
echo "--- MANUAL (operator-gated, cannot automate in CI) ---"
echo "iPhone round-trip: with the vault in iCloud Drive + this relocation applied,"
echo "  edit a note on iPhone (Obsidian/Files) → confirm it appears on the Mac, and"
echo "  vice-versa, while Activity Monitor shows fileproviderd/bird idle during a"
echo "  Mac-side git gc. (Done= part b.)"
echo

if [ "$fails" -gt 0 ]; then echo "FAILED: $fails"; exit 1; fi
echo "ALL TESTS PASSED"
