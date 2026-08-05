#!/usr/bin/env bash
# Self-test for vault-backup.sh's DAILY SCHEDULE install — the "registered but
# dead" class, on the macOS/Linux side.
#
# What was broken (MYC-3528): install_schedule's Darwin branch ended with
# `[ -f "$plist" ]`, so the function's return value said only "a file exists on
# disk". Both `launchctl bootstrap` and `launchctl load` were swallowed by
# `>/dev/null 2>&1 || true`, so ANY launchd failure was invisible, and cmd_setup
# printed "Daily backup scheduled (03:00 local)." over a job launchd had never
# accepted. The Linux branch had its own instance: an existing crontab line for
# the vault short-circuited to `return 0` on the mere PRESENCE of the vault path,
# so a relocated repo left a line invoking a script that no longer exists — cron
# ran it nightly, it failed nightly, and setup called that live.
#
# `test-vault-backup-roundtrip.sh` passes `--schedule none` in every case, so
# before this file install_schedule had ZERO test coverage on the bash side.
#
# Structure:
#   A. validators, portable  — sourced directly, so the Darwin logic is covered on
#                              the ubuntu CI runner and the Linux logic on a Mac.
#   B. end-to-end, per host  — the real `setup --schedule daily` CLI with launchctl
#                              / crontab STUBBED on PATH, so the honesty of the
#                              reporting is provable without mutating the real
#                              user's launchd or crontab.
#
# The discriminating pair in B is: identical filesystem state (the job file IS
# written), opposite verdicts, decided only by what the scheduler says. That is
# the exact assertion the old `[ -f "$plist" ]` could never make.
#
# Run: bash scripts/test-vault-backup-schedule.sh
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
BACKUP="$HERE/vault-backup.sh"
ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
fails=0
checks=0
pass() { checks=$((checks+1)); echo "PASS  $1"; }
fail() { checks=$((checks+1)); fails=$((fails+1)); echo "FAIL  $1"; }

# Source the script for its helpers. The main guard at the bottom of
# vault-backup.sh means this does NOT run a subcommand.
# shellcheck source=/dev/null
. "$BACKUP"

# ===========================================================================
# A. Validators — run on every platform.
# ===========================================================================
echo "== A. schedule validators (portable) =="

# ---- A1. xml_escape: the vault path is user data bound for XML -------------
got="$(xml_escape 'R & D Vault')"
[ "$got" = 'R &amp; D Vault' ] && pass "xml_escape escapes &" || fail "xml_escape &: got '$got'"
got="$(xml_escape '<a>&b')"
# & must be escaped FIRST or the < and > substitutions double-escape its entity.
[ "$got" = '&lt;a&gt;&amp;b' ] && pass "xml_escape handles < > & with no double-escaping" \
  || fail "xml_escape < > &: got '$got'"
got="$(xml_escape '/Users/x/Brain')"
[ "$got" = '/Users/x/Brain' ] && pass "xml_escape leaves an ordinary path alone" \
  || fail "xml_escape plain: got '$got'"

# ---- A2. plist_problems fixtures ------------------------------------------
FIX="$ROOT/fixtures"; mkdir -p "$FIX"
SELF="$FIX/vault-backup.sh"; : > "$SELF"          # a script that exists
LABEL="com.ai-brain-starter.vault-backup.test-1234abcd"
VAULT="$FIX/Brain"; mkdir -p "$VAULT"

write_plist() { # <out> <label> <arg-self> <arg-vault>   (values inserted RAW)
  cat > "$1" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$2</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string><string>$3</string>
    <string>run</string><string>--vault</string><string>$4</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
</dict></plist>
EOF
}

# NEGATIVE CONTROL — a healthy plist must produce NO problems. Without this, a
# validator that always reports a problem would pass every positive case below.
write_plist "$FIX/healthy.plist" "$LABEL" "$SELF" "$VAULT"
out="$(plist_problems "$FIX/healthy.plist" "$LABEL" "$SELF" "$VAULT")"
[ -z "$out" ] && pass "healthy plist -> no problems (negative control)" \
  || fail "healthy plist reported problems: $out"

# The reproducible defect: a vault path with a RAW ampersand, exactly what the
# pre-fix heredoc emitted. plutil -lint rejects it and launchd will not load it.
write_plist "$FIX/raw-amp.plist" "$LABEL" "$SELF" "$FIX/R & D Vault"
out="$(plist_problems "$FIX/raw-amp.plist" "$LABEL" "$SELF" "$FIX/R & D Vault")"
case "$out" in
  *"not a readable plist"*) pass "raw & in the vault path -> 'not a readable plist'" ;;
  *) fail "raw-ampersand plist was NOT caught: '$out'" ;;
esac
# Cross-check against the OS's own linter where it exists, so this test cannot
# drift from what launchd actually accepts.
if command -v plutil >/dev/null 2>&1; then
  if plutil -lint "$FIX/raw-amp.plist" >/dev/null 2>&1; then
    fail "plutil ACCEPTS the raw-ampersand plist — this fixture no longer reproduces"
  else
    pass "plutil -lint agrees the raw-ampersand plist is invalid"
  fi
  plutil -lint "$FIX/healthy.plist" >/dev/null 2>&1 \
    && pass "plutil -lint agrees the healthy fixture is valid" \
    || fail "plutil rejects the HEALTHY fixture — the fixture itself is wrong"
fi

# A relocated repo: the job points at a script that is gone. launchd keeps the
# job and fails it nightly, which is the same silent death one layer down.
write_plist "$FIX/gone.plist" "$LABEL" "$FIX/moved-away/vault-backup.sh" "$VAULT"
out="$(plist_problems "$FIX/gone.plist" "$LABEL" "$FIX/moved-away/vault-backup.sh" "$VAULT")"
case "$out" in
  *"script it would run is missing"*) pass "job pointing at a deleted script -> caught" ;;
  *) fail "missing-script plist was NOT caught: '$out'" ;;
esac

write_plist "$FIX/mislabel.plist" "com.someone.else" "$SELF" "$VAULT"
out="$(plist_problems "$FIX/mislabel.plist" "$LABEL" "$SELF" "$VAULT")"
case "$out" in *"Label is"*) pass "label mismatch -> caught" ;; *) fail "label mismatch NOT caught: '$out'" ;; esac

write_plist "$FIX/othervault.plist" "$LABEL" "$SELF" "$FIX/SomeOtherBrain"
out="$(plist_problems "$FIX/othervault.plist" "$LABEL" "$SELF" "$VAULT")"
case "$out" in *"different vault"*) pass "wrong vault -> caught" ;; *) fail "wrong vault NOT caught: '$out'" ;; esac

out="$(plist_problems "$FIX/does-not-exist.plist" "$LABEL" "$SELF" "$VAULT")"
case "$out" in *"no launchd job file"*) pass "absent plist -> caught" ;; *) fail "absent plist NOT caught: '$out'" ;; esac

# ---- A3. cron_plan --------------------------------------------------------
WANT="0 3 * * * /bin/bash $SELF run --vault '$VAULT' >> \$HOME/.claude/.vault-backup.log 2>&1"

# Already exactly right -> exit 3, no rewrite (idempotent re-run of setup).
cron_plan "$WANT" "$WANT" "$VAULT" >/dev/null 2>&1; rc=$?
[ "$rc" = 3 ] && pass "cron_plan: identical entry already present -> no-op (rc=3)" \
  || fail "cron_plan should have no-opped on an identical entry (rc=$rc)"

# THE Linux instance of the bug: a stale line for this same vault, naming a
# script path that no longer exists. The old code returned success on it.
STALE="0 3 * * * /bin/bash /old/gone/vault-backup.sh run --vault '$VAULT' >> \$HOME/.claude/.vault-backup.log 2>&1"
newtab="$(cron_plan "$STALE" "$WANT" "$VAULT")"; rc=$?
if [ "$rc" != 0 ]; then
  fail "cron_plan did not rewrite a stale entry (rc=$rc) — this is the bug"
else
  printf '%s\n' "$newtab" | grep -Fqx -- "$WANT" \
    && pass "cron_plan: stale entry replaced by the correct one" \
    || fail "cron_plan rewrote but the wanted line is absent: $newtab"
  printf '%s\n' "$newtab" | grep -Fq '/old/gone/vault-backup.sh' \
    && fail "cron_plan KEPT the stale dead-path entry" \
    || pass "cron_plan dropped the stale dead-path entry"
fi

# A stale entry for this vault whose LOG REDIRECT differs must still be replaced.
# Keying on the whole tail of the wanted line missed this and appended a second
# nightly job for the same vault instead of replacing the one already there.
ODDLOG="0 3 * * * /bin/bash /old/gone/vault-backup.sh run --vault '$VAULT' >> /var/log/mine.log 2>&1"
newtab="$(cron_plan "$ODDLOG" "$WANT" "$VAULT")"; rc=$?
if [ "$rc" != 0 ]; then
  fail "cron_plan kept a stale entry that used a different log path (rc=$rc)"
else
  n_for_vault="$(printf '%s\n' "$newtab" | grep -Fc -- "--vault '$VAULT'")"
  [ "$n_for_vault" = "1" ] \
    && pass "cron_plan: exactly ONE entry per vault after replacing an odd-redirect line" \
    || fail "cron_plan left $n_for_vault entries for one vault: $newtab"
fi

# An unrelated vault's schedule, and unrelated cron jobs, must survive untouched.
OTHER="0 4 * * * /bin/bash $SELF run --vault '$FIX/OtherBrain' >> \$HOME/.claude/.vault-backup.log 2>&1"
newtab="$(cron_plan "$(printf '%s\n%s\n' '@reboot /usr/bin/true' "$OTHER")" "$WANT" "$VAULT")"
if printf '%s\n' "$newtab" | grep -Fqx -- "$OTHER" \
   && printf '%s\n' "$newtab" | grep -Fq '@reboot /usr/bin/true' \
   && printf '%s\n' "$newtab" | grep -Fqx -- "$WANT"; then
  pass "cron_plan preserves other vaults' and unrelated cron lines"
else
  fail "cron_plan disturbed unrelated crontab lines: $newtab"
fi

# ===========================================================================
# B. End-to-end: the real CLI, with the OS scheduler stubbed on PATH.
# ===========================================================================
STUB="$ROOT/stub"; mkdir -p "$STUB"
HOMEDIR="$ROOT/home"; mkdir -p "$HOMEDIR/.claude"
# A vault path containing '&' — the case that produced an unloadable plist.
V="$ROOT/R & D Brain"; mkdir -p "$V"
printf '# CLAUDE.md\n' > "$V/CLAUDE.md"
D="$ROOT/dest"
# setup resolves the vault before storing it, and on macOS mktemp hands back a
# path under /var, a symlink to /private/var. Assertions about what the scheduler
# STORED must compare against the resolved form, not the one we passed in.
VR="$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$V")"

run_setup() { # -> combined output; env decides how the stubs behave
  HOME="$HOMEDIR" PATH="$STUB:$PATH" \
  VAULT_BACKUP_CONF="$ROOT/conf.json" VAULT_BACKUP_MARKER="$ROOT/marker" \
    bash "$BACKUP" setup --vault "$V" --dest "$D" --schedule daily 2>&1
}

case "$(uname)" in
  Darwin)
    echo "== B. end-to-end on Darwin: launchctl stubbed =="
    # The stub records what it was asked and answers per $STUB_MODE. It NEVER
    # touches the real launchd. `print`/`list` are the readback the fix depends on.
    cat > "$STUB/launchctl" <<'STUBEOF'
#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
case "$1" in
  print|list)
    [ "${STUB_MODE:-ok}" = "ok" ] && exit 0
    echo "Could not find service \"$2\" in domain for user gui" >&2; exit 113 ;;
  bootstrap|load)
    [ "${STUB_MODE:-ok}" = "ok" ] && exit 0
    echo "Load failed: 5: Input/output error" >&2; exit 5 ;;
  bootout) exit 0 ;;
esac
exit 0
STUBEOF
    chmod +x "$STUB/launchctl"
    PLIST_DIR="$HOMEDIR/Library/LaunchAgents"

    # --- B1. launchd REFUSES the job -> setup must NOT claim success ---------
    rm -rf "$PLIST_DIR" "$D"
    out="$(STUB_LOG="$ROOT/lc-fail.log" STUB_MODE=fail run_setup)"
    plist="$(ls -1 "$PLIST_DIR"/com.ai-brain-starter.vault-backup.*.plist 2>/dev/null | head -1)"
    # The discriminator: the FILE exists, so the old `[ -f "$plist" ]` check
    # would have returned 0 and printed success right here.
    if [ -n "$plist" ] && [ -f "$plist" ]; then
      pass "B1: the job file IS written (so a file-existence check would pass)"
    else
      fail "B1: no plist written at all — this leg no longer tests the right thing"
    fi
    if printf '%s\n' "$out" | grep -q "Daily backup scheduled"; then
      fail "B1: setup claimed 'Daily backup scheduled' while launchd refused the job"
    else
      pass "B1: setup does NOT claim the backup is scheduled when launchd refuses"
    fi
    printf '%s\n' "$out" | grep -q "launchd did not load the daily-backup job" \
      && pass "B1: the failure is reported, naming launchd" \
      || fail "B1: launchd refusal was not surfaced: $out"
    printf '%s\n' "$out" | grep -q "Input/output error" \
      && pass "B1: launchctl's real error text reaches the user" \
      || fail "B1: launchctl's stderr was swallowed: $out"

    # --- B2. launchd ACCEPTS the job -> success, and the plist is well-formed -
    rm -rf "$PLIST_DIR" "$D"
    out="$(STUB_LOG="$ROOT/lc-ok.log" STUB_MODE=ok run_setup)"
    printf '%s\n' "$out" | grep -q "Daily backup scheduled" \
      && pass "B2: setup reports success when launchd holds the job" \
      || fail "B2: setup did not report success on a good install: $out"
    plist="$(ls -1 "$PLIST_DIR"/com.ai-brain-starter.vault-backup.*.plist 2>/dev/null | head -1)"
    if [ -z "$plist" ]; then
      fail "B2: no plist written"
    else
      # The '&' in the vault path must have survived as a real ampersand, in a
      # file that is valid XML. This is the escaping fix, proven end-to-end.
      chk="$(python3 - "$plist" "$VR" 2>&1 <<'PY'
import plistlib, sys
p, vault = sys.argv[1], sys.argv[2]
try:
    with open(p, "rb") as fh:
        d = plistlib.load(fh)
except Exception as e:
    print("UNPARSEABLE %s: %s" % (type(e).__name__, e)); raise SystemExit(0)
args = d.get("ProgramArguments") or [""]
if args[-1] != vault:
    print("MISMATCH %r != %r" % (args[-1], vault))
elif "&amp;" in args[-1]:
    print("ENTITY-LEAK %r" % (args[-1],))
else:
    print("OK")
PY
)"
      [ "$chk" = "OK" ] \
        && pass "B2: a vault path containing '&' produces a VALID plist that round-trips" \
        || fail "B2: the '&' vault path did not round-trip through the plist: $chk"
      if command -v plutil >/dev/null 2>&1; then
        plutil -lint "$plist" >/dev/null 2>&1 \
          && pass "B2: plutil -lint accepts the generated plist" \
          || fail "B2: plutil -lint REJECTS the generated plist"
      fi
    fi
    grep -q "bootstrap gui/" "$ROOT/lc-ok.log" 2>/dev/null \
      && pass "B2: it actually asked launchd to load the job" \
      || fail "B2: launchctl bootstrap was never called"
    grep -qE '^(print|list) ' "$ROOT/lc-ok.log" 2>/dev/null \
      && pass "B2: it read the job back from launchd before claiming success" \
      || fail "B2: no launchd readback happened — success was assumed"

    # --- B3. self-heal: a plist on disk that launchd is not running ----------
    # The install that motivated the Windows half of this fix: setup was run
    # before the fix existed, the file is there, launchd never had it. A re-run
    # must repair it rather than see a file and declare victory.
    rm -rf "$D"
    if [ -z "$plist" ]; then
      fail "B3: cannot run the self-heal leg — B2 produced no plist to break"
    else
      printf 'not even xml\n' > "$plist"
      out="$(STUB_LOG="$ROOT/lc-heal.log" STUB_MODE=ok run_setup)"
      printf '%s\n' "$out" | grep -q "Existing daily-backup job needs repair" \
        && pass "B3: a broken existing job is detected and announced" \
        || fail "B3: the broken existing job was not detected: $out"
      printf '%s\n' "$out" | grep -q "Daily backup scheduled" \
        && pass "B3: and it is repaired, not merely warned about" \
        || fail "B3: repair did not complete: $out"
      python3 -c "import plistlib,sys; plistlib.load(open(sys.argv[1],'rb'))" "$plist" 2>/dev/null \
        && pass "B3: the repaired plist parses" \
        || fail "B3: the plist was not actually rewritten"
      # A healthy, loaded job on the NEXT re-run must be left completely alone.
      out="$(STUB_LOG="$ROOT/lc-noop.log" STUB_MODE=ok run_setup)"
      printf '%s\n' "$out" | grep -q "needs repair" \
        && fail "B4: a healthy loaded job was reported as needing repair" \
        || pass "B4: a healthy loaded job is left alone on re-run (idempotent)"
      grep -q "bootstrap gui/" "$ROOT/lc-noop.log" 2>/dev/null \
        && fail "B4: re-bootstrapped a job launchd already holds" \
        || pass "B4: no needless re-registration of a healthy job"
    fi
    ;;

  Linux)
    echo "== B. end-to-end on Linux: crontab stubbed =="
    # A file-backed crontab. `-l` prints it, `-` replaces it from stdin, and
    # STUB_MODE=fail makes the write refuse.
    cat > "$STUB/crontab" <<'STUBEOF'
#!/usr/bin/env bash
: "${STUB_CRONTAB:?}"
case "${1:-}" in
  -l) [ -f "$STUB_CRONTAB" ] && cat "$STUB_CRONTAB"; exit 0 ;;
  -)  if [ "${STUB_MODE:-ok}" != "ok" ]; then
        echo "crontab: installing new crontab failed" >&2; exit 1
      fi
      cat > "$STUB_CRONTAB"; exit 0 ;;
esac
exit 0
STUBEOF
    chmod +x "$STUB/crontab"

    # --- B1. crontab REFUSES the write -> no success claim -------------------
    rm -rf "$D"; : > "$ROOT/crontab.txt"
    out="$(STUB_CRONTAB="$ROOT/crontab.txt" STUB_MODE=fail run_setup)"
    if printf '%s\n' "$out" | grep -q "Daily backup scheduled"; then
      fail "B1: setup claimed success while crontab refused the write"
    else
      pass "B1: setup does NOT claim success when crontab refuses"
    fi
    printf '%s\n' "$out" | grep -q "crontab refused the daily-backup entry" \
      && pass "B1: the crontab refusal is reported" \
      || fail "B1: crontab refusal was not surfaced: $out"

    # --- B2. crontab ACCEPTS -> success, and the entry is really there -------
    rm -rf "$D"; : > "$ROOT/crontab.txt"
    out="$(STUB_CRONTAB="$ROOT/crontab.txt" STUB_MODE=ok run_setup)"
    printf '%s\n' "$out" | grep -q "Daily backup scheduled" \
      && pass "B2: setup reports success when the entry lands" \
      || fail "B2: setup did not report success on a good install: $out"
    grep -Fq "run --vault '$VR'" "$ROOT/crontab.txt" \
      && pass "B2: the crontab really holds an entry for this vault" \
      || fail "B2: no crontab entry for the vault: $(cat "$ROOT/crontab.txt")"

    # --- B3. a stale entry naming a script that is gone must be REPLACED -----
    rm -rf "$D"
    printf '%s\n' "@reboot /usr/bin/true" \
      "0 3 * * * /bin/bash /old/gone/vault-backup.sh run --vault '$VR' >> \$HOME/.claude/.vault-backup.log 2>&1" \
      > "$ROOT/crontab.txt"
    out="$(STUB_CRONTAB="$ROOT/crontab.txt" STUB_MODE=ok run_setup)"
    printf '%s\n' "$out" | grep -q "Daily backup scheduled" \
      && pass "B3: setup re-installs over a stale entry" \
      || fail "B3: setup did not re-install over a stale entry: $out"
    grep -Fq '/old/gone/vault-backup.sh' "$ROOT/crontab.txt" \
      && fail "B3: the stale dead-path entry SURVIVED (the bug)" \
      || pass "B3: the stale dead-path entry is gone"
    grep -Fq "$BACKUP run --vault '$VR'" "$ROOT/crontab.txt" \
      && pass "B3: replaced by an entry naming the script that exists" \
      || fail "B3: no correct entry after repair: $(cat "$ROOT/crontab.txt")"
    grep -Fq '@reboot /usr/bin/true' "$ROOT/crontab.txt" \
      && pass "B3: the unrelated cron line was left alone" \
      || fail "B3: an unrelated cron line was destroyed"
    ;;

  *)
    echo "SKIP  end-to-end leg: no scheduler branch for $(uname)"
    ;;
esac

# A test that runs to the end having asserted almost nothing is a false green -
# the class MYC-2922 closes. A `legs` counter cannot catch that (every branch of
# the platform switch, including the unsupported one, increments it), so assert a
# floor on the assertions that actually executed.
#
# Measured: section A = 16 on both platforms; section B = 14 on Darwin, 8 on
# Linux, so the totals are 30 and 24. The floor has to sit ABOVE the largest
# single section (16) to catch the other one vanishing, and far enough BELOW the
# smaller total (24) that trimming an assertion or two cannot false-trip it - an
# over-strict gate just teaches people to bypass it. 18 satisfies both: A alone
# (16) and B alone (8 or 14) each trip it, with 6 to spare on Linux.
if [ "$checks" -lt 18 ]; then
  echo "FAIL  only $checks assertion(s) ran — a section was skipped, this is a false green"
  fails=$((fails+1))
fi

echo
if [ "$fails" -gt 0 ]; then echo "FAILED: $fails"; exit 1; fi
echo "ALL TESTS PASSED"
