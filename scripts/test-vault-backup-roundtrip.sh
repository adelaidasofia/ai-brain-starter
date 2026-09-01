#!/usr/bin/env bash
# Round-trip self-test for vault-backup.sh.
# A backup you have never restored is a hope, not a backup — so this proves the
# whole loop on a fixture vault: setup -> ONE archive -> exhaust excluded ->
# detector flips to BACKED_UP -> a real restore extracts the notes back ->
# rotation honors --keep -> (if gpg/openssl) the encrypted path round-trips too.
# Run: bash scripts/test-vault-backup-roundtrip.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
BACKUP="$HERE/vault-backup.sh"
CHECK="$HERE/check-vault-backup.py"
ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
export VAULT_BACKUP_CONF="$ROOT/conf.json"
export VAULT_BACKUP_MARKER="$ROOT/marker"
fails=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; fails=$((fails+1)); }

# ---- fixture vault: real notes + machine-exhaust that MUST be excluded ----
V="$ROOT/Brain"
mkdir -p "$V/⚙️ Meta" "$V/.claude/worktrees/scratch" "$V/.smart-env" "$V/.codegraph"
printf '# CLAUDE.md\nbrain memory\n' > "$V/CLAUDE.md"
printf 'a private journal entry\n'   > "$V/journal.md"
printf 'EXHAUST-should-not-appear\n' > "$V/.claude/worktrees/scratch/junk.md"
printf 'EXHAUST-cache\n'             > "$V/.smart-env/cache.bin"
DEST="$ROOT/dest"

# ---- 1. setup (non-interactive: --dest given, no encrypt, no schedule) ----
bash "$BACKUP" setup --vault "$V" --dest "$DEST" --schedule none >/dev/null 2>&1 \
  && pass "setup ran" || fail "setup failed"

# ---- 2. exactly ONE archive, and it is a single .tar.gz file ----
n=$(ls -1 "$DEST"/vault-backup-* 2>/dev/null | wc -l | tr -d ' ')
[ "$n" = "1" ] && pass "exactly one archive written ($n)" || fail "expected 1 archive, got $n"
arc=$(ls -1 "$DEST"/vault-backup-* 2>/dev/null | head -1)
case "$arc" in *.tar.gz) pass "archive is a single .tar.gz" ;; *) fail "archive not .tar.gz: $arc" ;; esac

# ---- 3. notes present, machine-exhaust EXCLUDED ----
listing="$(tar -tzf "$arc" 2>/dev/null)"
printf '%s\n' "$listing" | grep -q './CLAUDE.md'  && pass "CLAUDE.md is in the archive" || fail "CLAUDE.md missing from archive"
printf '%s\n' "$listing" | grep -q './journal.md' && pass "journal.md is in the archive" || fail "journal.md missing"
if printf '%s\n' "$listing" | grep -q 'worktrees\|smart-env\|codegraph'; then
  fail "machine-exhaust leaked into the archive"
else
  pass "machine-exhaust (.claude/worktrees, .smart-env, .codegraph) excluded"
fi

# ---- 4. detector now says BACKED_UP (exit 0) ----
verdict="$(python3 "$CHECK" --porcelain "$V" 2>/dev/null)"; rc=$?
case "$verdict" in BACKED_UP:vault-backup*) pass "detector -> $verdict (rc=$rc)";; *) fail "detector wrong: $verdict (rc=$rc)";; esac

# ---- 4b. `status` PROPAGATES the detector's verdict ----
# Negative control for the `|| true` that used to discard it. `status` ran the
# canonical detector and threw its exit code away, so a cron line branching on
# `vault-backup.sh status` saw green over an unreachable destination or a vault
# with no backup at all -- while cmd_schedule in the same file documents the
# opposite policy ("Exits non-zero ... so a caller can branch on it instead of
# parsing prose"). Both directions are asserted: a configured vault must exit
# 0, an unconfigured one must not, or the pair proves nothing.
rc=0; bash "$BACKUP" status --vault "$V" >/dev/null 2>&1 || rc=$?
[ "$rc" -eq 0 ] && pass "status exits 0 for a backed-up vault" \
  || fail "status exited $rc for a vault that IS backed up"

UNBACKED="$ROOT/unbacked"; mkdir -p "$UNBACKED"
printf '# no backup here\n' > "$UNBACKED/CLAUDE.md"
rc=0; bash "$BACKUP" status --vault "$UNBACKED" >/dev/null 2>&1 || rc=$?
if [ "$rc" -ne 0 ]; then
  pass "status propagates the detector's non-zero verdict (rc=$rc)"
else
  fail "status exited 0 for a vault with NO backup -- the detector's verdict is being discarded again"
fi

# ---- 5. a REAL restore extracts the notes back ----
out="$(bash "$BACKUP" verify --vault "$V" 2>&1)"
echo "$out" | grep -q "Restore verified" && pass "verify reports a successful restore" || fail "verify did not confirm: $out"
# last_verify recorded
lv="$(python3 -c "import json;print((json.load(open('$VAULT_BACKUP_CONF')).get('vaults',{}).get('$(python3 -c "import os;print(os.path.realpath('$V'))")',{}) or {}).get('last_verify',''))" 2>/dev/null)"
[ -n "$lv" ] && pass "last_verify recorded ($lv)" || fail "last_verify not recorded"

# ---- 6. rotation honors --keep ----
# Pre-seed 5 older dummy archives, set keep=2, run once -> at most 2 remain.
for i in 1 2 3 4 5; do : > "$DEST/vault-backup-2000010$i-000000.tar.gz"; done
python3 - "$VAULT_BACKUP_CONF" "$(python3 -c "import os;print(os.path.realpath('$V'))")" <<'PY'
import json,sys
c=json.load(open(sys.argv[1])); c['vaults'][sys.argv[2]]['keep']=2; json.dump(c,open(sys.argv[1],'w'))
PY
sleep 1
bash "$BACKUP" run --vault "$V" >/dev/null 2>&1
left=$(ls -1 "$DEST"/vault-backup-* 2>/dev/null | wc -l | tr -d ' ')
[ "$left" -le 2 ] && pass "rotation kept <= keep (=2): $left remain" || fail "rotation kept too many: $left"

# ---- 7. encrypted round-trip (only if gpg or openssl is available) ----
if command -v gpg >/dev/null 2>&1 || command -v openssl >/dev/null 2>&1; then
  V2="$ROOT/Brain2"; DEST2="$ROOT/dest2"
  mkdir -p "$V2"; printf '# CLAUDE.md\n' > "$V2/CLAUDE.md"; printf 'secret journal\n' > "$V2/secret.md"
  # Hermetic passphrase store: bypass the real OS keychain and keep the 0600 fallback
  # file inside $ROOT, so the encrypted leg does not depend on a keychain service or
  # on ~/.claude existing — the env-correlated flake fixed in MYC-1804.
  export VAULT_BACKUP_PASS_DIR="$ROOT/passdir"; mkdir -p "$VAULT_BACKUP_PASS_DIR"
  # Capture setup output: on failure, surface the REAL encryption error instead of an
  # empty archive path (it used to be swallowed by >/dev/null 2>&1, undiagnosable in CI).
  setup_log="$ROOT/encrypt-setup.log"
  # Feed the passphrase twice on stdin (setup reads it with read -rs).
  printf 'pw-correct-horse\npw-correct-horse\n' | bash "$BACKUP" setup --vault "$V2" --dest "$DEST2" --encrypt --schedule none >"$setup_log" 2>&1
  enc="$(ls -1 "$DEST2"/vault-backup-* 2>/dev/null | head -1)"
  case "$enc" in
    *.tar.gz.gpg|*.tar.gz.enc) pass "encrypted archive written ($(basename "$enc"))" ;;
    *) fail "encrypted archive not produced: $enc"
       echo "      --- setup --encrypt output (the real error) ---"
       sed 's/^/      /' "$setup_log" ;;
  esac
  # The encrypted blob must NOT contain the plaintext.
  if grep -qa "secret journal" "$enc" 2>/dev/null; then fail "plaintext leaked into encrypted archive"; else pass "ciphertext does not contain plaintext"; fi
  vout="$(bash "$BACKUP" verify --vault "$V2" 2>&1)"
  echo "$vout" | grep -q "Restore verified" && pass "encrypted backup restores" || fail "encrypted restore failed: $vout"

  # ---- 7b. NEGATIVE CONTROL: setup --encrypt must FAIL LOUD when the passphrase
  #          store cannot be written (the silent-success bug fixed in MYC-1804).
  #          Point VAULT_BACKUP_PASS_DIR at a path whose parent is a regular file so
  #          mkdir -p fails; setup must abort non-zero, produce NO archive, and name
  #          the real cause — never echo a false "Passphrase stored".
  V3="$ROOT/Brain3"; DEST3="$ROOT/dest3"
  mkdir -p "$V3"; printf '# CLAUDE.md\n' > "$V3/CLAUDE.md"
  : > "$ROOT/not-a-dir"
  neg_out="$(printf 'pw\npw\n' | VAULT_BACKUP_PASS_DIR="$ROOT/not-a-dir/sub" \
    bash "$BACKUP" setup --vault "$V3" --dest "$DEST3" --encrypt --schedule none 2>&1)"; neg_rc=$?
  if [ "$neg_rc" -ne 0 ] && [ -z "$(ls -1 "$DEST3"/vault-backup-* 2>/dev/null)" ]; then
    pass "setup --encrypt fails loud on an unwritable passphrase store (rc=$neg_rc, no archive)"
  else
    fail "setup --encrypt did NOT fail loud on an unwritable store (rc=$neg_rc): $neg_out"
  fi
  printf '%s\n' "$neg_out" | grep -q "could not store backup passphrase\|could not create passphrase store dir\|could not write passphrase file" \
    && pass "the failure names the real cause (passphrase store)" \
    || fail "fail-loud message did not name the passphrase store: $neg_out"
else
  echo "SKIP  encrypted round-trip (no gpg/openssl)"
fi

echo
if [ "$fails" -gt 0 ]; then echo "FAILED: $fails"; exit 1; fi
echo "ALL TESTS PASSED"
