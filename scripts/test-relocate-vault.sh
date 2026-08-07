#!/usr/bin/env bash
# Negative-control test for scripts/relocate-vault.sh — the vault relocation +
# Claude Code path-keyed state migration helper (MYC-511 item 4).
#
# A guard earns trust only by failing on the thing it catches. This asserts:
#   - POSITIVE: transcripts copy from the old path key to the new key (base +
#     sub-keys), the agent-memory symlink re-homes, old keys are preserved.
#   - NO-OP DETECTOR: 0 transcripts under the new key BEFORE migration, N after
#     (a silently-broken key transform would leave it 0 -> this test goes red).
#   - FAIL-LOUD: a zero-match key prints "no Claude Code session history" and
#     exits 0 (nothing to do), never a silent success.
#   - REFUSALS: full relocate refuses when the target exists and when the source
#     is already a symlink (both fatal even under --force).
#   - HAPPY PATH: a full move relocates the dir, leaves the symlink, migrates
#     history; --no-symlink omits the symlink.
#   - BACKUP GATE (MYC-2382): a backup-less full relocate REFUSES (vault not
#     moved, remedy named); a verified off-machine backup lets it PROCEED;
#     --force overrides the gate on the same no-backup condition.
#   - CLOUD MOVE-OUT (MYC-2401): a vault whose only off-machine copy is the
#     cloud-sync it's being moved OUT of REFUSES (that copy doesn't survive the
#     move); a cloud vault with a surviving backup (archive/TM/git-remote) still
#     PROCEEDS.
#   - ENSURE-BACKUP (MYC-2404): --ensure-backup STANDS UP a verified backup as
#     part of the flow so a non-technical user reaches a backed-up, relocated
#     vault in ONE approved step (no --force, no vault-backup.sh by hand).
#     Negative controls prove fail-closed: a stand-up that FAILS at setup, and a
#     stand-up that reports success but lands NO archive, BOTH refuse the move and
#     preserve the vault. A real vault-backup.sh stand-up proceeds; a pre-existing
#     surviving backup skips the stand-up (idempotent).
# Hermetic: a temp HOME-like sandbox + an isolated --config-dir; the real
# ~/.claude is never touched.
# Run: bash scripts/test-relocate-vault.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/relocate-vault.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fails=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; fails=$((fails + 1)); }

# HERMETICITY: Obsidian-running is a SOFT gate in relocate-vault.sh, skippable
# with --force. Its real probe (pgrep -x Obsidian) reads AMBIENT host state, so a
# developer with Obsidian OPEN — the normal state when developing an Obsidian-vault
# tool — would see every full-relocate case below that omits --force die at that
# gate instead of exercising the backup/cloud/ensure-backup gate it targets (15
# such failures on a dev Mac). Pin the probe to "absent" for the whole suite so
# those OTHER gates are what get tested; Section 10 flips it to "running" per-call
# to cover the Obsidian refusal itself, deterministically, with no real Obsidian.
export RELOCATE_VAULT_OBSIDIAN=absent

# Mirror the script's projkey EXACTLY (resolve ancestry, keep leaf literal) so
# fixtures land at the same key the script computes, even where $TMPDIR resolves
# /var -> /private/var on macOS.
pathkey() { python3 -c 'import os,re,sys
p = os.path.abspath(os.path.expanduser(sys.argv[1]))
resolved = os.path.join(os.path.realpath(os.path.dirname(p)), os.path.basename(p))
print(re.sub(r"[^a-zA-Z0-9]", "-", resolved))' "$1"; }

CFG="$TMP/.claude"
PROJ="$CFG/projects"
jsonl_count() { find "$1" -maxdepth 1 -name '*.jsonl' 2>/dev/null | wc -l | tr -d ' '; }

# --- 1. POSITIVE + NO-OP DETECTOR: --migrate-claude-state copies old -> new ----
# The old path has a SPACE -> exercises the non-alphanumeric key transform that
# the reference [/ ]->- sed would also handle, but a dotted/emoji path needs the
# full [^A-Za-z0-9]->- rule this helper uses.
OLD="$TMP/Desktop/My Vault"
NEW="$TMP/brain"
mkdir -p "$OLD/⚙️ Meta/Agent Memory" "$NEW/⚙️ Meta/Agent Memory"
OLDKEY="$(pathkey "$OLD")"
NEWKEY="$(pathkey "$NEW")"
mkdir -p "$PROJ/$OLDKEY" "$PROJ/${OLDKEY}--claude-worktrees-foo"
echo '{"x":1}' > "$PROJ/$OLDKEY/sess1.jsonl"
echo '{"x":2}' > "$PROJ/${OLDKEY}--claude-worktrees-foo/sess2.jsonl"

before="$(jsonl_count "$PROJ/$NEWKEY")"
[ "$before" = 0 ] && pass "no-op detector: 0 transcripts under new key before migration" \
                  || fail "no-op detector: expected 0 before, got $before"

CLAUDE_CONFIG_DIR="$CFG" bash "$SCRIPT" --migrate-claude-state "$OLD" "$NEW" >/dev/null 2>&1

a1="$(jsonl_count "$PROJ/$NEWKEY")"
[ "$a1" = 1 ] && pass "base-key transcript migrated (sess1)" || fail "base-key migrate: want 1 got $a1"
a2="$(jsonl_count "$PROJ/${NEWKEY}--claude-worktrees-foo")"
[ "$a2" = 1 ] && pass "sub-key transcript migrated (sess2)" || fail "sub-key migrate: want 1 got $a2"
[ -L "$PROJ/$NEWKEY/memory" ] && pass "agent-memory symlink re-homed" || fail "agent-memory symlink missing"
[ -f "$PROJ/$OLDKEY/sess1.jsonl" ] && pass "old key preserved (copy, not move)" || fail "old key was destroyed (should be a backup)"

# --- 2. FAIL-LOUD: a zero-match key warns + exits 0 (never a silent success) ---
out="$(CLAUDE_CONFIG_DIR="$CFG" bash "$SCRIPT" --migrate-claude-state "$TMP/nothing-here" "$TMP/elsewhere" 2>&1)"
rc=$?
echo "$out" | grep -qi 'no Claude Code session history' \
  && pass "zero-match: fails loud (warns, not silent)" \
  || fail "zero-match: expected a loud warning, got: $out"
[ "$rc" = 0 ] && pass "zero-match: exits 0 (nothing to do, not a crash)" || fail "zero-match: want rc=0 got $rc"

# --- 3. REFUSAL: full relocate refuses when the target already exists ----------
src="$TMP/src-vault" ; dst="$TMP/dst-exists" ; mkdir -p "$src" "$dst"
CLAUDE_CONFIG_DIR="$CFG" bash "$SCRIPT" "$src" "$dst" --force >/dev/null 2>&1
rc=$?
[ "$rc" = 1 ] && pass "refuses when target exists (rc=1, fatal even under --force)" \
             || fail "target-exists: want rc=1 got $rc"

# --- 4. REFUSAL: refuses when the source is already a symlink ------------------
realdir="$TMP/realdir" ; linkdir="$TMP/linkdir" ; mkdir -p "$realdir" ; ln -s "$realdir" "$linkdir"
CLAUDE_CONFIG_DIR="$CFG" bash "$SCRIPT" "$linkdir" "$TMP/new-from-link" --force >/dev/null 2>&1
rc=$?
[ "$rc" = 1 ] && pass "refuses when source already a symlink (rc=1)" || fail "source-symlink: want rc=1 got $rc"

# --- 5. HAPPY PATH: move dir, leave symlink, migrate history -------------------
hv="$TMP/Desktop/Happy Vault" ; nv="$TMP/local-brain"
mkdir -p "$hv/⚙️ Meta/Agent Memory" ; echo "note" > "$hv/note.md"
HK="$(pathkey "$hv")" ; NK="$(pathkey "$nv")"
mkdir -p "$PROJ/$HK" ; echo '{"s":1}' > "$PROJ/$HK/h.jsonl"
CLAUDE_CONFIG_DIR="$CFG" bash "$SCRIPT" "$hv" "$nv" --force >/dev/null 2>&1
rc=$?
[ "$rc" = 0 ] && pass "happy path exits 0" || fail "happy path rc=$rc"
{ [ -d "$nv" ] && [ -f "$nv/note.md" ]; } && pass "vault moved to new local path" || fail "vault not at new path"
{ [ -L "$hv" ] && [ "$(readlink "$hv")" = "$nv" ]; } && pass "symlink left at old path" || fail "symlink not left at old path"
hh="$(jsonl_count "$PROJ/$NK")"
[ "$hh" = 1 ] && pass "happy path migrated session history" || fail "happy path history not migrated (want 1 got $hh)"

# --- 6. --no-symlink omits the symlink ----------------------------------------
nsv="$TMP/Desktop/NoSym Vault" ; nsn="$TMP/nosym-brain" ; mkdir -p "$nsv"
CLAUDE_CONFIG_DIR="$CFG" bash "$SCRIPT" "$nsv" "$nsn" --force --no-symlink >/dev/null 2>&1
{ [ -d "$nsn" ] && [ ! -e "$nsv" ]; } && pass "--no-symlink leaves nothing at the old path" || fail "--no-symlink left something at old path"

# --- 7. BACKUP-FIRST GATE (MYC-2382): refuse a backup-less full relocate -------
# Moving a vault — often the one irreplaceable asset — with no off-machine backup
# is the nightmare failure. The full-relocate path must REFUSE unless a backup is
# verified (check-vault-backup.py) OR --force is passed. The no-backup signal is
# made hermetic with an empty backup-conf + the Time Machine probe skipped, so
# "no backup" is deterministic even on a dev Mac that has Time Machine set up.
NOBK_CONF="$TMP/no-backup-conf.json" ; echo '{}' > "$NOBK_CONF"

# 7a REFUSE: no backup, no --force -> refuses (rc!=0), vault NOT moved, names remedy.
gb_src="$TMP/gate-src" ; gb_dst="$TMP/gate-dst" ; mkdir -p "$gb_src" ; echo n > "$gb_src/n.md"
out="$(CLAUDE_CONFIG_DIR="$CFG" VAULT_BACKUP_CONF="$NOBK_CONF" VAULT_BACKUP_SKIP_TIMEMACHINE=1 \
       bash "$SCRIPT" "$gb_src" "$gb_dst" 2>&1)" ; rc=$?
[ "$rc" != 0 ] && pass "backup gate: refuses a backup-less move (rc=$rc)" \
               || fail "backup gate: expected a refusal, moved with rc=$rc"
{ [ -d "$gb_src" ] && [ ! -e "$gb_dst" ]; } && pass "backup gate: vault NOT moved on refusal" \
               || fail "backup gate: vault was moved despite the refusal"
echo "$out" | grep -qi 'backup' && pass "backup gate: refusal names the backup remedy" \
               || fail "backup gate: refusal output lacked a backup remedy: $out"

# 7b PROCEED: a verified vault-backup archive present, no --force -> moves.
ok_src="$TMP/gate-ok-src" ; ok_dst="$TMP/gate-ok-dst" ; mkdir -p "$ok_src" ; echo n > "$ok_src/n.md"
OK_RES="$(cd "$ok_src" && pwd -P)"   # the physical path relocate-vault.sh hands the guard
OK_DEST="$TMP/gate-backups" ; mkdir -p "$OK_DEST" ; touch "$OK_DEST/vault-backup-now.tar.gz"
OK_CONF="$TMP/ok-backup-conf.json"
printf '{"vaults": {"%s": {"dest": "%s", "archive_stem": "vault-backup"}}}\n' "$OK_RES" "$OK_DEST" > "$OK_CONF"
CLAUDE_CONFIG_DIR="$CFG" VAULT_BACKUP_CONF="$OK_CONF" VAULT_BACKUP_SKIP_TIMEMACHINE=1 \
  bash "$SCRIPT" "$ok_src" "$ok_dst" >/dev/null 2>&1 ; rc=$?
[ "$rc" = 0 ] && pass "backup gate: proceeds with a verified backup (rc=0)" \
             || fail "backup gate: a verified backup was still refused (rc=$rc)"
{ [ -d "$ok_dst" ] && [ -L "$ok_src" ]; } && pass "backup gate: verified backup -> moved + symlink left" \
             || fail "backup gate: verified backup did not relocate the vault"

# 7c FORCE OVERRIDE: no backup but --force -> moves (same no-backup condition as 7a).
fb_src="$TMP/gate-force-src" ; fb_dst="$TMP/gate-force-dst" ; mkdir -p "$fb_src" ; echo n > "$fb_src/n.md"
CLAUDE_CONFIG_DIR="$CFG" VAULT_BACKUP_CONF="$NOBK_CONF" VAULT_BACKUP_SKIP_TIMEMACHINE=1 \
  bash "$SCRIPT" "$fb_src" "$fb_dst" --force >/dev/null 2>&1 ; rc=$?
[ "$rc" = 0 ] && pass "backup gate: --force overrides the gate (rc=0)" \
             || fail "backup gate: --force did not override the gate (rc=$rc)"
[ -d "$fb_dst" ] && pass "backup gate: --force -> vault moved despite no backup" \
             || fail "backup gate: --force did not move the vault"

# --- 8. BACKUP GATE x CLOUD (MYC-2401): a cloud copy doesn't count for a move-OUT
# relocate moves the vault OUT of the cloud root and leaves a symlink, so the cloud
# copy is gone post-move. The gate passes --ignore-cloud, so a vault whose ONLY
# off-machine copy is the cloud it's fleeing must REFUSE (else we green-light the
# move citing the very backup the move destroys). A SURVIVING backup still proceeds.
# 8a a vault inside a cloud root with NO surviving backup -> REFUSE.
cl_src="$TMP/OneDrive/Cloud Vault" ; cl_dst="$TMP/cloud-local"
mkdir -p "$cl_src" ; echo n > "$cl_src/n.md"
out="$(CLAUDE_CONFIG_DIR="$CFG" VAULT_BACKUP_CONF="$NOBK_CONF" VAULT_BACKUP_SKIP_TIMEMACHINE=1 \
       bash "$SCRIPT" "$cl_src" "$cl_dst" 2>&1)" ; rc=$?
[ "$rc" != 0 ] && pass "cloud move-out, no surviving backup -> refuses (rc=$rc)" \
               || fail "cloud move-out should refuse (cloud copy doesn't survive), got rc=$rc"
{ [ -d "$cl_src" ] && [ ! -e "$cl_dst" ]; } && pass "cloud move-out refusal: vault NOT moved" \
               || fail "cloud move-out: vault moved despite no surviving backup"
echo "$out" | grep -qi 'cloud' && pass "cloud move-out refusal: explains the cloud copy won't survive" \
               || fail "cloud move-out refusal should mention the cloud copy: $out"

# 8b same cloud vault but WITH a surviving vault-backup archive -> proceeds.
cl2_src="$TMP/OneDrive/Cloud Vault 2" ; cl2_dst="$TMP/cloud-local-2"
mkdir -p "$cl2_src" ; echo n > "$cl2_src/n.md"
CL2_RES="$(cd "$cl2_src" && pwd -P)"
CL2_DEST="$TMP/cloud2-backups" ; mkdir -p "$CL2_DEST" ; touch "$CL2_DEST/vault-backup-now.tar.gz"
CL2_CONF="$TMP/cloud2-conf.json"
printf '{"vaults": {"%s": {"dest": "%s", "archive_stem": "vault-backup"}}}\n' "$CL2_RES" "$CL2_DEST" > "$CL2_CONF"
CLAUDE_CONFIG_DIR="$CFG" VAULT_BACKUP_CONF="$CL2_CONF" VAULT_BACKUP_SKIP_TIMEMACHINE=1 \
  bash "$SCRIPT" "$cl2_src" "$cl2_dst" >/dev/null 2>&1 ; rc=$?
[ "$rc" = 0 ] && pass "cloud vault WITH a surviving archive -> proceeds (rc=0)" \
             || fail "cloud vault with a surviving archive should proceed, got rc=$rc"
{ [ -d "$cl2_dst" ] && [ -L "$cl2_src" ]; } && pass "cloud+archive move-out: moved + symlink left" \
             || fail "cloud+archive: did not relocate"

# --- 9. ENSURE-BACKUP (MYC-2404): stand up the backup, don't just refuse --------
# The MYC-2382 gate REFUSES a backup-less move but leaves a non-technical user
# stuck (they don't know --force and won't run vault-backup.sh by hand). --ensure
# -backup STANDS UP a verified off-machine backup as part of the move — one step.
# The whole point is safety, so the negative controls come FIRST: a stand-up that
# fails MUST NOT move the vault (fail-closed preserves; it never destroys).
# VAULT_BACKUP_CMD lets these tests inject a stub backup tool deterministically.

# Stub A: FAILS on setup (unreachable/denied dest, disk full, etc.).
STUB_FAIL="$TMP/vault-backup-fail.sh"
cat > "$STUB_FAIL" <<'STUB'
#!/usr/bin/env bash
echo "stub: simulated backup failure ($1)" >&2
exit 1
STUB
chmod +x "$STUB_FAIL"

# Stub B: pretends success (exit 0) but lands NO archive + writes NO conf. The
# re-check must still catch that nothing survives and refuse (no silent no-op).
STUB_LIE="$TMP/vault-backup-lie.sh"
cat > "$STUB_LIE" <<'STUB'
#!/usr/bin/env bash
echo "stub: reports success but lands nothing ($1)"
exit 0
STUB
chmod +x "$STUB_LIE"

# 9a NEGATIVE CONTROL: stand-up FAILS at setup -> refuse, vault preserved.
eb1_src="$TMP/eb-fail-src" ; eb1_dst="$TMP/eb-fail-dst"
mkdir -p "$eb1_src" ; echo n > "$eb1_src/n.md"
out="$(CLAUDE_CONFIG_DIR="$CFG" VAULT_BACKUP_CONF="$NOBK_CONF" VAULT_BACKUP_SKIP_TIMEMACHINE=1 \
       VAULT_BACKUP_CMD="$STUB_FAIL" \
       bash "$SCRIPT" --ensure-backup "$eb1_src" "$eb1_dst" 2>&1)" ; rc=$?
[ "$rc" != 0 ] && pass "ensure-backup NC1: stand-up fails at setup -> refuses the move (rc=$rc)" \
               || fail "ensure-backup NC1: expected a refusal when stand-up fails, moved with rc=$rc"
{ [ -d "$eb1_src" ] && [ ! -L "$eb1_src" ] && [ -f "$eb1_src/n.md" ] && [ ! -e "$eb1_dst" ]; } \
  && pass "ensure-backup NC1: vault NOT moved on stand-up failure (fail-closed preserves)" \
  || fail "ensure-backup NC1: vault was moved/altered despite the stand-up failure"
echo "$out" | grep -qiE 'fail-closed|not moving|untouched' \
  && pass "ensure-backup NC1: refusal explains it is fail-closed" \
  || fail "ensure-backup NC1: refusal lacked a fail-closed explanation: $out"

# 9b NEGATIVE CONTROL: stand-up returns 0 but no surviving backup -> re-check refuses.
eb2_src="$TMP/eb-lie-src" ; eb2_dst="$TMP/eb-lie-dst"
mkdir -p "$eb2_src" ; echo n > "$eb2_src/n.md"
out="$(CLAUDE_CONFIG_DIR="$CFG" VAULT_BACKUP_CONF="$NOBK_CONF" VAULT_BACKUP_SKIP_TIMEMACHINE=1 \
       VAULT_BACKUP_CMD="$STUB_LIE" \
       bash "$SCRIPT" --ensure-backup "$eb2_src" "$eb2_dst" 2>&1)" ; rc=$?
[ "$rc" != 0 ] && pass "ensure-backup NC2: stand-up 'succeeds' but no archive -> re-check refuses (rc=$rc)" \
               || fail "ensure-backup NC2: expected a refusal when no surviving backup materialized, rc=$rc"
{ [ -d "$eb2_src" ] && [ ! -L "$eb2_src" ] && [ ! -e "$eb2_dst" ]; } \
  && pass "ensure-backup NC2: vault NOT moved when stand-up produced no backup" \
  || fail "ensure-backup NC2: vault moved despite no surviving backup"

# 9c POSITIVE: the REAL vault-backup.sh stands up a verified archive -> move proceeds.
# Hermetic: --backup-schedule none (no launchd/cron), temp conf/marker, temp dest.
eb3_src="$TMP/eb-real-src" ; eb3_dst="$TMP/eb-real-dst"
mkdir -p "$eb3_src/⚙️ Meta" ; echo "brain" > "$eb3_src/CLAUDE.md" ; echo "note" > "$eb3_src/note.md"
EB3_DEST="$TMP/eb-real-backups"
EB3_CONF="$TMP/eb-real-conf.json" ; echo '{}' > "$EB3_CONF"
EB3_MARKER="$TMP/eb-real-marker"
out="$(CLAUDE_CONFIG_DIR="$CFG" VAULT_BACKUP_CONF="$EB3_CONF" VAULT_BACKUP_MARKER="$EB3_MARKER" \
       VAULT_BACKUP_SKIP_TIMEMACHINE=1 \
       bash "$SCRIPT" --ensure-backup --backup-dest "$EB3_DEST" --backup-schedule none \
            "$eb3_src" "$eb3_dst" 2>&1)" ; rc=$?
[ "$rc" = 0 ] && pass "ensure-backup P1: real stand-up + verify -> move proceeds (rc=0)" \
             || fail "ensure-backup P1: real stand-up should proceed, rc=$rc; out=$out"
{ [ -d "$eb3_dst" ] && [ -L "$eb3_src" ] && [ "$(readlink "$eb3_src")" = "$eb3_dst" ]; } \
  && pass "ensure-backup P1: vault moved + symlink left after stand-up" \
  || fail "ensure-backup P1: vault not relocated as expected"
ls "$EB3_DEST"/vault-backup-*.tar.gz >/dev/null 2>&1 \
  && pass "ensure-backup P1: a real verified archive was stood up BEFORE the move" \
  || fail "ensure-backup P1: no archive found in $EB3_DEST"
# The auto stand-up is UNENCRYPTED — the move must say so loudly (privacy: a vault may
# hold private notes and the archive often lands in a cloud folder). MYC-2512 phase 1.
echo "$out" | grep -qiE 'not encrypted' \
  && pass "ensure-backup P1: warns loudly that the stood-up backup is unencrypted" \
  || fail "ensure-backup P1: missing the unencrypted-backup warning: $out"

# 9d POSITIVE (idempotent): a pre-existing surviving backup skips the stand-up.
# VAULT_BACKUP_CMD points at the FAIL stub — if the stand-up were (wrongly) invoked
# the move would refuse; it proceeds only because a backup already exists.
eb4_src="$TMP/eb-idem-src" ; eb4_dst="$TMP/eb-idem-dst"
mkdir -p "$eb4_src" ; echo n > "$eb4_src/n.md"
EB4_RES="$(cd "$eb4_src" && pwd -P)"
EB4_DEST="$TMP/eb-idem-backups" ; mkdir -p "$EB4_DEST" ; touch "$EB4_DEST/vault-backup-now.tar.gz"
EB4_CONF="$TMP/eb-idem-conf.json"
printf '{"vaults": {"%s": {"dest": "%s", "archive_stem": "vault-backup"}}}\n' "$EB4_RES" "$EB4_DEST" > "$EB4_CONF"
CLAUDE_CONFIG_DIR="$CFG" VAULT_BACKUP_CONF="$EB4_CONF" VAULT_BACKUP_SKIP_TIMEMACHINE=1 \
  VAULT_BACKUP_CMD="$STUB_FAIL" \
  bash "$SCRIPT" --ensure-backup "$eb4_src" "$eb4_dst" >/dev/null 2>&1 ; rc=$?
[ "$rc" = 0 ] && pass "ensure-backup P2: pre-existing backup -> stand-up skipped, move proceeds (rc=0)" \
             || fail "ensure-backup P2: pre-existing backup should skip stand-up + proceed, rc=$rc"
{ [ -d "$eb4_dst" ] && [ -L "$eb4_src" ]; } \
  && pass "ensure-backup P2: idempotent -> moved + symlink (fail-stub never invoked)" \
  || fail "ensure-backup P2: did not relocate with a pre-existing backup"

# 9e DRY-RUN: --ensure-backup --dry-run previews the stand-up + move, changes nothing.
eb5_src="$TMP/eb-dry-src" ; eb5_dst="$TMP/eb-dry-dst"
mkdir -p "$eb5_src" ; echo n > "$eb5_src/n.md"
out="$(CLAUDE_CONFIG_DIR="$CFG" VAULT_BACKUP_CONF="$NOBK_CONF" VAULT_BACKUP_SKIP_TIMEMACHINE=1 \
       VAULT_BACKUP_CMD="$STUB_FAIL" \
       bash "$SCRIPT" --ensure-backup --dry-run "$eb5_src" "$eb5_dst" 2>&1)" ; rc=$?
[ "$rc" = 0 ] && pass "ensure-backup 9e: --dry-run previews without refusing (rc=0)" \
             || fail "ensure-backup 9e: --dry-run should not refuse/execute, rc=$rc; out=$out"
{ [ -d "$eb5_src" ] && [ ! -L "$eb5_src" ] && [ ! -e "$eb5_dst" ]; } \
  && pass "ensure-backup 9e: --dry-run changed nothing (no stand-up, no move)" \
  || fail "ensure-backup 9e: --dry-run mutated the filesystem"
echo "$out" | grep -qi 'would' \
  && pass "ensure-backup 9e: --dry-run names the stand-up it would run" \
  || fail "ensure-backup 9e: --dry-run did not preview the stand-up: $out"

# --- 10. OBSIDIAN SOFT GATE: the refusal fires when Obsidian IS detected --------
# The whole suite pins RELOCATE_VAULT_OBSIDIAN=absent so the other gates are
# testable on any host; here we flip it to "running" to prove the Obsidian gate
# itself STILL refuses (and stays escapable with --force), deterministically and
# with no real Obsidian process. This is the dedicated coverage the seam must keep:
# a soft gate that silently stopped firing would otherwise pass every test.
ob_src="$TMP/obsidian-src" ; ob_dst="$TMP/obsidian-dst" ; mkdir -p "$ob_src" ; echo n > "$ob_src/n.md"

# 10a REFUSE: Obsidian "running", no --force -> refuses at the Obsidian gate FIRST
# (before the backup gate), names Obsidian + the --force remedy, vault NOT moved.
out="$(CLAUDE_CONFIG_DIR="$CFG" RELOCATE_VAULT_OBSIDIAN=running \
       bash "$SCRIPT" "$ob_src" "$ob_dst" 2>&1)" ; rc=$?
[ "$rc" = 1 ] && pass "obsidian gate: refuses a move while Obsidian runs (rc=1)" \
             || fail "obsidian gate: expected rc=1, got $rc"
{ [ -d "$ob_src" ] && [ ! -L "$ob_src" ] && [ ! -e "$ob_dst" ]; } \
  && pass "obsidian gate: vault NOT moved on refusal" \
  || fail "obsidian gate: vault was moved despite the refusal"
echo "$out" | grep -qi 'obsidian' \
  && pass "obsidian gate: refusal names Obsidian (fired before the backup gate)" \
  || fail "obsidian gate: refusal did not mention Obsidian: $out"
echo "$out" | grep -qi -- '--force' \
  && pass "obsidian gate: refusal names the --force escape hatch" \
  || fail "obsidian gate: refusal did not name --force: $out"

# 10b ESCAPE HATCH: Obsidian "running" + --force -> gate skipped, move proceeds
# (the refusal's own promise; also proves the seam does not leak past --force).
CLAUDE_CONFIG_DIR="$CFG" RELOCATE_VAULT_OBSIDIAN=running \
  bash "$SCRIPT" "$ob_src" "$ob_dst" --force >/dev/null 2>&1 ; rc=$?
[ "$rc" = 0 ] && pass "obsidian gate: --force overrides it even while Obsidian runs (rc=0)" \
             || fail "obsidian gate: --force did not override the Obsidian gate (rc=$rc)"
{ [ -d "$ob_dst" ] && [ -L "$ob_src" ]; } \
  && pass "obsidian gate: --force -> vault moved + symlink left despite Obsidian running" \
  || fail "obsidian gate: --force did not relocate with Obsidian running"

echo
if [ "$fails" -gt 0 ]; then echo "FAILED: $fails"; exit 1; fi
echo "ALL TESTS PASSED"
