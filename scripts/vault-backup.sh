#!/usr/bin/env bash
# vault-backup.sh — one-command, provider-agnostic, off-machine backup for a brain vault.
#
# The gap this closes: a brain in active daily use whose only copy is the local
# disk it runs on. The hourly git auto-snapshot is LOCAL-only (it refuses to run
# with a remote) — so without this, "I have snapshots" still means one disk
# failure = everything gone. This writes ONE compressed archive per run to a
# destination you already have (an external disk, or a Google Drive / Dropbox /
# OneDrive folder — your choice), so it is genuinely off-machine. One file, not
# the churning git object tree, so a cloud folder syncs it without a storm.
#
#   setup   pick a destination (+ optional encryption + a daily schedule), then
#           take the first snapshot immediately.
#   run     take one snapshot now (what the schedule calls; non-interactive).
#   verify  restore the newest snapshot to a temp dir and confirm it actually
#           extracts — a backup you have never restored is a hope, not a backup.
#   status  show where backups go, how fresh they are, and the canonical verdict.
#   schedule  check the daily schedule is REALLY installed with the OS scheduler
#           and repair it if not (non-interactive, no snapshot). This is the
#           reachable repair path: the self-heal must not live only inside
#           `setup`, because an install carrying a dead schedule is exactly the
#           one that never re-runs setup. (MYC-3528)
#
# Provider-agnostic: the destination is any folder path you give it. Encryption
# (--encrypt) is optional and uses gpg (or openssl), with the passphrase stored
# in your OS keychain when one is available. With NO keychain it falls back to a
# 0600 file (loudly, and `status` keeps reporting which of the two you have).
# Pure POSIX-ish bash + python3 for the small JSON bits (python3 is already a
# hard dependency of this repo).
#
# Usage:
#   bash vault-backup.sh setup  [--vault PATH] [--dest DIR] [--encrypt] [--keep N] [--schedule daily|none]
#   bash vault-backup.sh run    [--vault PATH]
#   bash vault-backup.sh verify [--vault PATH]
#   bash vault-backup.sh status [--vault PATH]
#   bash vault-backup.sh schedule [--vault PATH]
#
# Config:  ~/.claude/.vault-backup.conf  (JSON, keyed by resolved vault path)
# Marker:  ~/.claude/.vault-backup-last  (ISO8601 of the last successful run)
# Pass dir: VAULT_BACKUP_PASS_DIR overrides where the 0600 passphrase-fallback file
#           lives AND bypasses the OS keychain. Used by the round-trip self-test to
#           stay hermetic and not depend on ~/.claude existing. Defaults to ~/.claude.
set -uo pipefail

# Resolve this script's own directory so sibling helpers (vault-nested-repos.py)
# are found no matter where the script is invoked from. No hardcoded install
# path: the repo may live anywhere on a client's machine.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONF="${VAULT_BACKUP_CONF:-$HOME/.claude/.vault-backup.conf}"
MARKER="${VAULT_BACKUP_MARKER:-$HOME/.claude/.vault-backup-last}"
KEYCHAIN_SERVICE="ai-brain-starter-vault-backup"
ARCHIVE_STEM="vault-backup"

# Dirs that are regenerable machine-exhaust — never worth backing up, and the
# exact churn that would bloat the archive. Notes + .git history are kept.
EXCLUDES=(
  "./.claude/worktrees" "./.smart-env" "./.codegraph" "./node_modules"
  "./.venv" "./__pycache__" "./.pytest_cache" "./.mypy_cache" "./.ruff_cache"
  "./.trash" "./.DS_Store"
)

# ---------- tiny ui helpers (no-op color if not a tty) ----------
if [ -t 1 ]; then G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; B=$'\033[1m'; N=$'\033[0m'
else G=''; Y=''; R=''; B=''; N=''; fi
say()  { echo "$*"; }
ok()   { echo "${G}OK${N}   $*"; }
warn() { echo "${Y}WARN${N} $*"; }
die()  { echo "${R}ERROR${N} $*" >&2; exit 1; }

iso_now() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

# ---------- json conf helpers (python3 is a repo dependency) ----------
conf_get() { # <vault-key> <field> -> value or empty
  python3 - "$CONF" "$1" "$2" <<'PY'
import json, sys
conf_path, key, field = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    d = json.load(open(conf_path))
except Exception:
    print(""); sys.exit(0)
e = (d.get("vaults") or {}).get(key) or {}
v = e.get(field, "")
print(v if v is not None else "")
PY
}

conf_set() { # <vault-key> <field=value>...  (values are strings unless int/bool-looking)
  local key="$1"; shift
  python3 - "$CONF" "$key" "$@" <<'PY'
import json, os, sys
conf_path, key = sys.argv[1], sys.argv[2]
pairs = sys.argv[3:]
try:
    d = json.load(open(conf_path))
except Exception:
    d = {}
d.setdefault("vaults", {})
e = d["vaults"].get(key, {})
for p in pairs:
    f, _, v = p.partition("=")
    if v in ("true", "false"):
        e[f] = (v == "true")
    elif v.isdigit():
        e[f] = int(v)
    else:
        e[f] = v
d["vaults"][key] = e
os.makedirs(os.path.dirname(conf_path), exist_ok=True)
tmp = conf_path + ".tmp"
json.dump(d, open(tmp, "w"), indent=2)
os.replace(tmp, conf_path)
PY
}

realpath_py() { python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"; }

slug_for() { # <vault-path> -> stable short slug for keychain acct / plist name
  local base hash
  base="$(basename "$1" | tr ' /' '__')"
  hash="$(printf '%s' "$1" | (md5 2>/dev/null || md5sum 2>/dev/null) | tr -d ' -' | cut -c1-8)"
  printf '%s-%s' "$base" "$hash"
}

# ---------- vault resolution ----------
resolve_vault() { # echoes resolved vault path; dies if not a dir
  local v="${1:-}"
  if [ -z "$v" ]; then v="${VAULT_PATH:-$PWD}"; fi
  [ -d "$v" ] || die "vault path is not a directory: $v"
  realpath_py "$v"
}

# ---------- passphrase (keychain-backed) ----------
store_passphrase() { # <slug> <passphrase>  -> store-kind on stdout; non-zero on failure
  local acct="$1" pass="$2"
  # VAULT_BACKUP_PASS_DIR set => use a 0600 file in that dir and bypass the OS
  # keychain entirely. Keeps the round-trip self-test hermetic (no real keychain
  # write, no dependence on ~/.claude pre-existing) and exercises the same
  # file-store path CI uses on Linux. (MYC-1804)
  if [ -z "${VAULT_BACKUP_PASS_DIR:-}" ]; then
    if command -v security >/dev/null 2>&1; then        # macOS Keychain
      security add-generic-password -a "$acct" -s "$KEYCHAIN_SERVICE" -w "$pass" -U >/dev/null 2>&1 \
        && { echo "keychain"; return 0; }
    fi
    if command -v secret-tool >/dev/null 2>&1; then      # Linux libsecret
      printf '%s' "$pass" | secret-tool store --label="$KEYCHAIN_SERVICE" \
        service "$KEYCHAIN_SERVICE" account "$acct" >/dev/null 2>&1 \
        && { echo "secret-tool"; return 0; }
    fi
  fi
  # Fallback: 0600 file. Loud about it — this is weaker than a keychain. Ensure the
  # parent dir exists and FAIL LOUD if the write does not land. Silently reporting
  # success here (the old bug) made `setup --encrypt` produce no archive on a fresh
  # box with no ~/.claude, and the real error got swallowed downstream. (MYC-1804)
  local store_dir="${VAULT_BACKUP_PASS_DIR:-$HOME/.claude}"
  local pf="$store_dir/.vault-backup-pass-$acct"
  mkdir -p "$store_dir" 2>/dev/null \
    || { warn "could not create passphrase store dir: $store_dir" >&2; return 1; }
  ( umask 077; printf '%s' "$pass" > "$pf" ) \
    || { warn "could not write passphrase file: $pf" >&2; return 1; }
  warn "No OS keychain found; passphrase stored in $pf (chmod 600). A keychain is safer." >&2
  echo "file:$pf"
}

get_passphrase() { # <slug> <store-kind> -> passphrase on stdout (non-interactive)
  local acct="$1" kind="$2"
  case "$kind" in
    keychain)     security find-generic-password -a "$acct" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null ;;
    secret-tool)  secret-tool lookup service "$KEYCHAIN_SERVICE" account "$acct" 2>/dev/null ;;
    file:*)       cat "${kind#file:}" 2>/dev/null ;;
    *)            return 1 ;;
  esac
}

# ---------- the archive ----------
make_archive() { # <vault> <out-base-no-ext> <encrypt 0|1> <slug> <store-kind> -> echoes final path
  local vault="$1" outbase="$2" enc="$3" acct="$4" kind="$5"
  local excl=() e
  for e in "${EXCLUDES[@]}"; do excl+=("--exclude=$e"); done
  # Always exclude any prior archives that happen to live under the vault.
  excl+=("--exclude=./*.tar.gz" "--exclude=./*.tar.gz.gpg" "--exclude=./*.tar.gz.enc")

  # FOREIGN CHECKOUTS NESTED IN THE VAULT — excluded by PROPERTY, not by name.
  #
  # `EXCLUDES` above is a denylist against an open set: it names
  # `./.claude/worktrees`, so it covers the Desktop app's worktrees and nothing
  # else. Measured on a real install: a second agent CLI used a directory the
  # list did not name, parked eleven checkouts of three unrelated repos in the
  # vault, and added 37 GB to the backup source — which pushed the offsite
  # provider past its storage cap and left the offsite copy stale for 50+ days.
  # Adding one more name fixes one tool and loses to the next.
  #
  # The scanner excludes only checkouts recoverable from a git remote; a nested
  # repo with NO remote is kept and reported, because the vault may be its only
  # copy. FAIL-OPEN: if the scan cannot run we back up MORE, never less.
  local nested; nested="$(mktemp)"
  if command -v python3 >/dev/null 2>&1 \
     && python3 "$SCRIPT_DIR/vault-nested-repos.py" --vault-root "$vault" \
          --relative --exclude-file "$nested" >/dev/null; then
    while IFS= read -r line; do
      case "$line" in ''|'#'*) continue;; esac
      excl+=("--exclude=$line")
    done < "$nested"
  else
    warn "nested-checkout scan unavailable — a foreign git checkout under a new"
    warn "directory name would be included in this archive."
  fi
  rm -f "$nested"

  # Capture the encryption tool's stderr to a temp file so a failure surfaces the
  # REAL error (gpg/openssl/tar) instead of dying with a bare "encryption failed".
  # This is a BACKUP tool: a silent failure here is the difference between "I have
  # backups" and data loss, so the failure path must be diagnosable for a real user,
  # not just in the self-test. The passphrase is never written to stderr. (MYC-1804)
  local errf; errf="$(mktemp)"
  err_tail() { tr '\n' ' ' < "$errf" 2>/dev/null | sed 's/  */ /g; s/ *$//'; rm -f "$errf"; }

  if [ "$enc" = "1" ]; then
    local pass; pass="$(get_passphrase "$acct" "$kind")"
    [ -n "$pass" ] || { rm -f "$errf"; die "could not read backup passphrase from $kind"; }
    if command -v gpg >/dev/null 2>&1; then
      local out="$outbase.tar.gz.gpg"
      tar -cz "${excl[@]}" -C "$vault" . 2>/dev/null \
        | gpg --batch --yes --pinentry-mode loopback --passphrase "$pass" \
              -c --cipher-algo AES256 -o "$out" 2>"$errf" \
        && { rm -f "$errf"; echo "$out"; return 0; }
      die "gpg encryption failed: $(err_tail)"
    elif command -v openssl >/dev/null 2>&1; then
      local out="$outbase.tar.gz.enc"
      tar -cz "${excl[@]}" -C "$vault" . 2>/dev/null \
        | openssl enc -aes-256-cbc -pbkdf2 -salt -pass "pass:$pass" -out "$out" 2>"$errf" \
        && { rm -f "$errf"; echo "$out"; return 0; }
      die "openssl encryption failed: $(err_tail)"
    else
      rm -f "$errf"; die "--encrypt needs gpg or openssl; neither found"
    fi
  else
    local out="$outbase.tar.gz"
    tar -czf "$out" "${excl[@]}" -C "$vault" . 2>"$errf" && { rm -f "$errf"; echo "$out"; return 0; }
    die "tar failed: $(err_tail)"
  fi
}

rotate() { # <dest> <keep>
  local dest="$1" keep="$2" f
  # newest-first; delete everything past $keep
  ls -1t "$dest"/$ARCHIVE_STEM-* 2>/dev/null | tail -n +"$((keep+1))" | while IFS= read -r f; do
    [ -n "$f" ] && rm -f "$f"
  done
}

human_size() { # <file>
  if [ "$(uname)" = "Darwin" ]; then stat -f %z "$1" 2>/dev/null; else stat -c %s "$1" 2>/dev/null; fi \
    | awk '{ s=$1; u="B"; if(s>1073741824){s/=1073741824;u="GB"} else if(s>1048576){s/=1048576;u="MB"} else if(s>1024){s/=1024;u="KB"} printf "%.1f%s", s, u }'
}

# ============================ subcommands ============================

cmd_setup() {
  local vault="" dest="" encrypt=0 keep=7 schedule="daily"
  while [ $# -gt 0 ]; do
    case "$1" in
      --vault) vault="$2"; shift 2;;
      --dest) dest="$2"; shift 2;;
      --encrypt) encrypt=1; shift;;
      --keep) keep="$2"; shift 2;;
      --schedule) schedule="$2"; shift 2;;
      *) die "unknown setup arg: $1";;
    esac
  done
  vault="$(resolve_vault "$vault")"
  say "${B}Backing up:${N} $vault"

  # Destination — prompt if not given.
  if [ -z "$dest" ]; then
    say ""
    say "Where should the off-machine backup go? Give a folder you already have"
    say "somewhere OTHER than this machine's single disk — an external drive, or a"
    say "Google Drive / Dropbox / OneDrive folder (one daily file syncs fine; it is"
    say "the churning vault that must not go in cloud sync, not a single archive)."
    printf "Destination folder: "
    read -r dest
    [ -n "$dest" ] || die "no destination given"
  fi
  # Expand ~ and resolve.
  dest="${dest/#\~/$HOME}"
  mkdir -p "$dest" 2>/dev/null || die "could not create destination: $dest"
  dest="$(realpath_py "$dest")"

  # Refuse a destination INSIDE the vault — that is not off-machine at all.
  case "$dest/" in
    "$vault/"*) die "destination is inside the vault ($dest). Pick a folder off this machine.";;
  esac
  # Nudge (don't block) if the destination is not obviously off-machine.
  if printf '%s' "$dest" | grep -Eq '/(OneDrive|Dropbox|Google ?Drive|CloudStorage|Box|Mobile Documents)/|/Volumes/'; then
    ok "Destination looks off-machine (cloud folder or external volume)."
  else
    warn "Destination $dest may be on this same disk. That protects against accidental"
    warn "deletion but NOT against disk failure. An external drive or cloud folder is safer."
  fi

  local slug store_kind=""; slug="$(slug_for "$vault")"

  # Encryption passphrase (stored in keychain), if requested.
  if [ "$encrypt" = "1" ]; then
    command -v gpg >/dev/null 2>&1 || command -v openssl >/dev/null 2>&1 \
      || die "--encrypt needs gpg or openssl installed"
    local p1 p2
    # Do NOT promise the keychain here: whether one exists is not known until
    # store_passphrase() runs, a few lines down. Promising it at the moment the
    # user types the secret is the worst place to be wrong, so state the
    # conditional truth and let the WARN + `status` report what actually happened.
    printf "Set a backup passphrase (stored in your OS keychain when the machine has one): "
    read -rs p1; echo
    printf "Confirm passphrase: "
    read -rs p2; echo
    [ -n "$p1" ] || die "empty passphrase"
    [ "$p1" = "$p2" ] || die "passphrases do not match"
    store_kind="$(store_passphrase "$slug" "$p1")" \
      || die "could not store backup passphrase (see error above)"
    [ -n "$store_kind" ] || die "could not store backup passphrase (empty store kind)"
    ok "Passphrase stored ($store_kind). Daily runs read it from there, no prompt."
  fi

  # Persist config.
  conf_set "$vault" \
    "dest=$dest" "archive_stem=$ARCHIVE_STEM" "encrypt=$([ "$encrypt" = 1 ] && echo true || echo false)" \
    "keep=$keep" "keychain_account=$slug" "store_kind=$store_kind"
  ok "Saved config -> $CONF"

  # First snapshot immediately, so there is no configured-but-empty gap.
  say ""
  say "${B}Taking the first snapshot...${N}"
  cmd_run --vault "$vault" || die "first snapshot failed"

  # Schedule the daily run. install_schedule returns 0 ONLY when the OS scheduler
  # actually holds the job, so this success line is now a claim about launchd/cron
  # reality rather than about a file having been written. It also prints WHY it
  # could not schedule (never a bare "could not"), so a real user can act on it.
  if [ "$schedule" = "daily" ]; then
    if install_schedule "$vault" "$slug"; then
      ok "Daily backup scheduled (03:00 local), verified with the OS scheduler."
    else
      warn "No automatic schedule installed (reason above). Until that is fixed, run"
      warn "this yourself:  bash $0 run --vault \"$vault\""
    fi
  else
    say "No schedule installed (--schedule none). Run \`vault-backup.sh run\` when you want a snapshot."
  fi

  say ""
  ok "${B}Backup is live.${N} Now prove it restores (do this once):"
  say "    bash $0 verify --vault \"$vault\""
}

cmd_run() {
  local vault=""
  while [ $# -gt 0 ]; do case "$1" in --vault) vault="$2"; shift 2;; *) shift;; esac; done
  vault="$(resolve_vault "$vault")"
  local dest; dest="$(conf_get "$vault" dest)"
  [ -n "$dest" ] || die "vault not configured for backup. Run: bash $0 setup --vault \"$vault\""
  [ -d "$dest" ] || die "backup destination is unreachable: $dest (external disk unplugged?)"
  local enc encbool keep slug kind
  encbool="$(conf_get "$vault" encrypt)"; enc=$([ "$encbool" = "True" ] || [ "$encbool" = "true" ] && echo 1 || echo 0)
  keep="$(conf_get "$vault" keep)"; [ -n "$keep" ] || keep=7
  slug="$(conf_get "$vault" keychain_account)"; [ -n "$slug" ] || slug="$(slug_for "$vault")"
  kind="$(conf_get "$vault" store_kind)"

  local stamp outbase out
  stamp="$(date +%Y%m%d-%H%M%S)"
  outbase="$dest/$ARCHIVE_STEM-$stamp"
  out="$(make_archive "$vault" "$outbase" "$enc" "$slug" "$kind")" || exit 1

  # Integrity sanity-check before declaring success.
  local sz; sz="$(human_size "$out")"
  if [ "$enc" = "0" ]; then
    tar -tzf "$out" >/dev/null 2>&1 || { rm -f "$out"; die "archive failed integrity check (corrupt tar.gz)"; }
  else
    # Can't list without decrypting; assert it is non-trivially sized.
    local bytes; bytes="$(if [ "$(uname)" = Darwin ]; then stat -f %z "$out"; else stat -c %s "$out"; fi)"
    [ "${bytes:-0}" -gt 256 ] || { rm -f "$out"; die "encrypted archive is suspiciously small"; }
  fi

  rotate "$dest" "$keep"
  conf_set "$vault" "last=$(iso_now)"
  ( umask 077; iso_now > "$MARKER" ) 2>/dev/null || true
  ok "Snapshot: $out ($sz)"
}

cmd_verify() {
  local vault=""
  while [ $# -gt 0 ]; do case "$1" in --vault) vault="$2"; shift 2;; *) shift;; esac; done
  vault="$(resolve_vault "$vault")"
  local dest; dest="$(conf_get "$vault" dest)"
  [ -n "$dest" ] || die "vault not configured. Run setup first."
  local newest
  newest="$(ls -1t "$dest"/$ARCHIVE_STEM-* 2>/dev/null | head -1)"
  [ -n "$newest" ] || die "no snapshot found in $dest. Run: bash $0 run --vault \"$vault\""

  local tmp; tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  say "Restoring ${B}$newest${N} to a temp dir to prove it actually works..."

  case "$newest" in
    *.tar.gz)
      tar -xzf "$newest" -C "$tmp" 2>/dev/null || die "restore FAILED: archive did not extract" ;;
    *.tar.gz.gpg)
      local slug kind pass; slug="$(conf_get "$vault" keychain_account)"; kind="$(conf_get "$vault" store_kind)"
      pass="$(get_passphrase "$slug" "$kind")"; [ -n "$pass" ] || die "no passphrase to decrypt"
      gpg --batch --yes --pinentry-mode loopback --passphrase "$pass" -d "$newest" 2>/dev/null \
        | tar -xz -C "$tmp" 2>/dev/null || die "restore FAILED: could not decrypt + extract" ;;
    *.tar.gz.enc)
      local slug kind pass; slug="$(conf_get "$vault" keychain_account)"; kind="$(conf_get "$vault" store_kind)"
      pass="$(get_passphrase "$slug" "$kind")"; [ -n "$pass" ] || die "no passphrase to decrypt"
      openssl enc -d -aes-256-cbc -pbkdf2 -pass "pass:$pass" -in "$newest" 2>/dev/null \
        | tar -xz -C "$tmp" 2>/dev/null || die "restore FAILED: could not decrypt + extract" ;;
    *) die "unrecognized archive type: $newest" ;;
  esac

  local count; count="$(find "$tmp" -type f 2>/dev/null | wc -l | tr -d ' ')"
  [ "$count" -gt 0 ] || die "restore produced ZERO files — the backup is empty/broken"
  # Prefer a meaningful sentinel if the vault has one.
  local sentinel=""
  [ -f "$tmp/CLAUDE.md" ] && sentinel="CLAUDE.md"
  conf_set "$vault" "last_verify=$(iso_now)"
  ok "Restore verified: extracted $count file(s)${sentinel:+, $sentinel present}. Your backup actually restores."
}

# The REACHABLE repair path for the daily schedule.
#
# `install_schedule` self-heals, but for most of this fix's life it had exactly
# ONE caller: `cmd_setup`. That means the repair only ever fired for someone who
# chose to re-run setup — while the population carrying a dead schedule is
# precisely the population that never does. A capability with no production
# caller that the affected user actually reaches is not shipped. (MYC-3528 work
# item 2.)
#
# Non-interactive and cheap on purpose: no prompts, no snapshot, no config
# rewrite — so a hook, a cron line, or a human can run it safely and often.
# Exits non-zero when the schedule is NOT installed and could not be repaired,
# so a caller can branch on it instead of parsing prose.
cmd_schedule() {
  local vault=""
  while [ $# -gt 0 ]; do case "$1" in --vault) vault="$2"; shift 2;; *) shift;; esac; done
  vault="$(resolve_vault "$vault")"
  local dest; dest="$(conf_get "$vault" dest)"
  [ -n "$dest" ] || die "vault not configured for backup. Run: bash $0 setup --vault \"$vault\""
  local slug; slug="$(conf_get "$vault" keychain_account)"; [ -n "$slug" ] || slug="$(slug_for "$vault")"

  if install_schedule "$vault" "$slug"; then
    ok "Daily backup schedule is installed and held by the OS scheduler (03:00 local)."
  else
    warn "The daily backup schedule is NOT installed (reason above)."
    warn "Snapshots will only happen when you run:  bash $0 run --vault \"$vault\""
    return 1
  fi
}

cmd_status() {
  local vault="" enc_on=0
  while [ $# -gt 0 ]; do case "$1" in --vault) vault="$2"; shift 2;; *) shift;; esac; done
  vault="$(resolve_vault "$vault")"
  say "${B}Vault:${N} $vault"
  local dest; dest="$(conf_get "$vault" dest)"
  if [ -z "$dest" ]; then
    warn "No backup configured for this vault."
    say  "Set one up: bash $0 setup --vault \"$vault\""
  else
    say "${B}Destination:${N} $dest $([ -d "$dest" ] && echo "(reachable)" || echo "${R}(UNREACHABLE)${N}")"
    say "${B}Encrypted:${N}   $(conf_get "$vault" encrypt)    ${B}Keep:${N} $(conf_get "$vault" keep)"
    # Where the passphrase lives is part of the security posture, not a setup
    # detail. "Encrypted: true" on its own reads as safe even when the key fell
    # back to a file on the same disk as the backup. The setup-time warning is a
    # one-shot the user scrolls past; status is the surface they come back to, so
    # report the store kind every time and mark the fallback as weaker.
    # conf_get renders the JSON bool through python, so this reads "True", not
    # "true". Match every plausible spelling: a strict compare here fails open
    # (the line silently never prints) and looks exactly like a healthy status.
    case "$(conf_get "$vault" encrypt)" in true|True|TRUE|1|yes) enc_on=1;; *) enc_on=0;; esac
    if [ "$enc_on" = 1 ]; then
      local skind; skind="$(conf_get "$vault" store_kind)"
      case "$skind" in
        keychain|secret-tool|dpapi)
          say "${B}Passphrase:${N}  OS keychain ($skind)" ;;
        file:*)
          warn "Passphrase:  ${skind#file:}"
          warn "             chmod-600 file, NOT an OS keychain. Weaker: anyone who can"
          warn "             read your home dir can decrypt the backups. Install a keychain"
          warn "             (macOS: built in; Linux: libsecret/secret-tool) and re-run setup." ;;
        "")
          warn "Passphrase:  unknown (no store_kind recorded) - re-run setup to repair" ;;
        *)
          say "${B}Passphrase:${N}  $skind" ;;
      esac
    fi
    local last lastv n
    last="$(conf_get "$vault" last)"; lastv="$(conf_get "$vault" last_verify)"
    n="$(ls -1 "$dest"/$ARCHIVE_STEM-* 2>/dev/null | wc -l | tr -d ' ')"
    say "${B}Snapshots:${N}   ${n:-0} in destination"
    say "${B}Last run:${N}    ${last:-never}"
    say "${B}Last verify:${N} ${lastv:-never  (run: bash $0 verify)}"
  fi
  # Canonical verdict from the single source of truth.
  local checker; checker="$(cd "$(dirname "$0")" && pwd)/check-vault-backup.py"
  [ -f "$checker" ] && { say ""; python3 "$checker" "$vault" || true; }
}

# ---------- scheduling ----------
#
# THE CLASS THIS GUARDS AGAINST: "registered but dead" — every layer reports
# success over a schedule that can never fire. It shipped once on the Windows
# side (a task registered with `-File ""`; 25 days, zero snapshots, while setup
# printed "Backup is live"), was fixed there in #410/#453, and the SAME class had
# a live instance on both branches below. A schedule is only installed when the
# OS's scheduler actually holds it — writing the file is not installing it.
# (MYC-3528)

# WRITE the launchd job file. Built by python3's plistlib, NOT by a heredoc.
#
# The vault path is USER DATA: an `&`, `<` or `>` anywhere in it (a vault at
# "R & D Vault") made the old heredoc emit a plist that is not well-formed XML,
# so launchd refused to load it — while the FILE still existed, which was all
# the old success check looked at. Measured: `plutil -lint` exits 1 ("unknown
# ampersand-escape sequence") and plistlib raises ExpatError on that plist.
#
# The first fix here escaped the three characters in shell with `${s//&/&amp;}`.
# That was WRONG on a modern bash and nobody local could see it: since bash 5.2,
# a bare `&` in the REPLACEMENT half of `${var//pat/repl}` expands to the text
# that matched (ksh93 behaviour), so `${s//</&lt;}` yields `<lt;`. macOS ships
# bash 3.2, which has no such rule, so it passed on every local run and failed
# only on CI's ubuntu bash 5.2. The `&`→`&amp;` line survived by pure accident,
# because there the matched text IS `&`.
#
# So: do not hand-write XML from the shell at all. plistlib serialises a dict and
# cannot get escaping wrong, in any bash, on any platform. The read-back
# validator below already uses plistlib, so writer and reader now share one
# implementation of the format. (MYC-3528)
write_backup_plist() { # <plist> <label> <self> <vault> <logfile> -> 0 on success
  python3 - "$1" "$2" "$3" "$4" "$5" <<'PY'
import plistlib, sys
path, label, self_path, vault, logfile = sys.argv[1:6]
job = {
    "Label": label,
    "ProgramArguments": ["/bin/bash", self_path, "run", "--vault", vault],
    "StartCalendarInterval": {"Hour": 3, "Minute": 0},
    "StandardErrorPath": logfile,
    "StandardOutPath": logfile,
}
with open(path, "wb") as fh:
    plistlib.dump(job, fh)
PY
}

# Read a written plist BACK and report what is wrong with it, one problem per
# line; empty output means healthy. Parses the file only — no launchctl — so it
# is portable and unit-testable on any platform (CI is ubuntu, where `plutil`
# does not exist; plistlib ships with the python3 this repo already requires).
plist_problems() { # <plist> <label> <self> <vault> -> problem lines on stdout
  python3 - "$1" "$2" "$3" "$4" <<'PY'
import os, plistlib, sys
path, label, self_path, vault = sys.argv[1:5]
if not os.path.isfile(path):
    print("no launchd job file at %s" % path); raise SystemExit(0)
try:
    with open(path, "rb") as fh:
        d = plistlib.load(fh)
except Exception as e:
    # The reproducible case: an unescaped & / < / > from the vault path.
    print("not a readable plist (%s: %s)" % (type(e).__name__, e)); raise SystemExit(0)
if d.get("Label") != label:
    print("Label is %r, expected %r" % (d.get("Label"), label))
args = d.get("ProgramArguments") or []
if len(args) < 5:
    print("ProgramArguments is not a backup run: %r" % (args,))
    raise SystemExit(0)
if not args[1] or not os.path.isfile(args[1]):
    # A relocated repo leaves a job pointing at a script that is gone. launchd
    # keeps the job loaded and fails it nightly.
    print("the script it would run is missing: %r" % (args[1],))
elif os.path.realpath(args[1]) != os.path.realpath(self_path):
    print("points at a different script: %r (this one is %r)" % (args[1], self_path))
if args[-1] != vault:
    print("points at a different vault: %r (expected %r)" % (args[-1], vault))
PY
}

# Does launchd ACTUALLY hold this job? The check the old code never made.
# Measured on macOS (Darwin 25.5): `launchctl print gui/<uid>/<label>` exits 0
# when the service is loaded and 113 ("Could not find service ... in domain")
# when it is not. `launchctl list <label>` behaves identically and covers the
# older `launchctl load` path, so try both before concluding it is absent.
launchd_job_loaded() { # <label> -> 0 if launchd holds it
  local label="$1" uid; uid="$(id -u)"
  launchctl print "gui/$uid/$label" >/dev/null 2>&1 && return 0
  launchctl list "$label" >/dev/null 2>&1
}

# Decide the crontab this vault should have, given the one it currently has.
# stdout + exit 0 = install this crontab. Exit 3 = already exactly right, no-op.
# Any line for THIS vault that is not byte-identical to the wanted line is
# REPLACED, not kept: the old code returned success on the mere PRESENCE of the
# vault path, so a relocated repo left a line invoking a script that no longer
# exists — cron ran it nightly, it failed nightly, and setup called that "live".
cron_plan() { # <current-crontab> <wanted-line> <vault> -> new crontab on stdout
  python3 - "$1" "$2" "$3" <<'PY'
import sys
cur, want, vault = sys.argv[1], sys.argv[2], sys.argv[3]
# Identify lines belonging to THIS vault by its exact --vault argument, so a
# second vault's schedule is never disturbed. Deliberately NOT the whole tail of
# the wanted line: a hand-edited entry with a different log redirect would then
# fail to match, and we would APPEND a second nightly job for the same vault
# instead of replacing the one that is already there.
marker = "--vault '%s'" % vault
kept, already = [], False
for ln in cur.splitlines():
    if marker in ln:
        if ln.strip() == want.strip():
            already = True
            kept.append(ln)
        continue          # stale form / dead path: drop, the fresh line replaces it
    kept.append(ln)
if already:
    raise SystemExit(3)
kept = [l for l in kept if l.strip()]
kept.append(want)
sys.stdout.write("\n".join(kept) + "\n")
PY
}

install_schedule() { # <vault> <slug>  -> 0 ONLY when the OS scheduler holds the job
  local vault="$1" slug="$2" self
  self="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  case "$(uname)" in
    Darwin)
      local label="com.ai-brain-starter.vault-backup.$slug"
      local plist="$HOME/Library/LaunchAgents/$label.plist"
      local uid; uid="$(id -u)"
      local problems

      # SELF-HEAL, parallel to the Windows task fix (#453). An install that ran
      # setup before this fix may carry a plist launchd never loaded, and nothing
      # in the product ever looked at it again. Healthy AND loaded -> leave it
      # alone. Otherwise rewrite and re-bootstrap in place: idempotent, no
      # prompt, because the user already consented to a daily backup.
      problems="$(plist_problems "$plist" "$label" "$self" "$vault")"
      if [ -z "$problems" ] && launchd_job_loaded "$label"; then
        return 0
      fi
      if [ -f "$plist" ]; then
        warn "Existing daily-backup job needs repair:"
        if [ -n "$problems" ]; then
          printf '%s\n' "$problems" | while IFS= read -r p; do warn "  $p"; done
        else
          warn "  the job file is fine but launchd is not running it"
        fi
      fi

      mkdir -p "$HOME/Library/LaunchAgents" 2>/dev/null \
        || { warn "could not create $HOME/Library/LaunchAgents"; return 1; }
      write_backup_plist "$plist" "$label" "$self" "$vault" \
        "$HOME/.claude/.vault-backup.log" \
        || { warn "could not write the daily-backup job file at $plist"; return 1; }

      # Prove what we just wrote is loadable BEFORE asking launchd for it, so the
      # failure names the real cause instead of launchctl's generic refusal.
      problems="$(plist_problems "$plist" "$label" "$self" "$vault")"
      if [ -n "$problems" ]; then
        warn "the daily-backup job file this wrote is not usable:"
        printf '%s\n' "$problems" | while IFS= read -r p; do warn "  $p"; done
        return 1
      fi

      # Unload any prior registration first: bootstrap refuses a label it
      # already holds, and that refusal is how a BROKEN job survived a repair.
      launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
      local errf; errf="$(mktemp)"
      launchctl bootstrap "gui/$uid" "$plist" 2>"$errf" \
        || launchctl load "$plist" 2>>"$errf" || true

      # THE check: launchd's own answer, not the filesystem's.
      if ! launchd_job_loaded "$label"; then
        warn "launchd did not load the daily-backup job ($label):"
        warn "  $(tr '\n' ' ' < "$errf" 2>/dev/null | sed 's/  */ /g; s/^ *//; s/ *$//')"
        warn "  the job file is at $plist"
        rm -f "$errf"
        return 1
      fi
      rm -f "$errf"
      ;;
    Linux)
      command -v crontab >/dev/null 2>&1 \
        || { warn "no crontab command found; cannot install a daily schedule"; return 1; }
      local line="0 3 * * * /bin/bash $self run --vault '$vault' >> \$HOME/.claude/.vault-backup.log 2>&1"
      local cur newtab rc
      cur="$(crontab -l 2>/dev/null)"
      newtab="$(cron_plan "$cur" "$line" "$vault")"; rc=$?
      [ "$rc" = 3 ] && return 0
      [ "$rc" = 0 ] || { warn "could not work out the crontab update for this vault"; return 1; }

      local errf; errf="$(mktemp)"
      if ! printf '%s' "$newtab" | crontab - 2>"$errf"; then
        warn "crontab refused the daily-backup entry:"
        warn "  $(tr '\n' ' ' < "$errf" 2>/dev/null | sed 's/  */ /g; s/^ *//; s/ *$//')"
        rm -f "$errf"
        return 1
      fi
      rm -f "$errf"
      # Read it back. A write that returned 0 without landing is this same class.
      # Matched on the invocation rather than the whole line: some cron
      # implementations prepend a header or normalise whitespace, and an exact
      # whole-line match would then report failure over a schedule that is fine.
      # (Whether the cron DAEMON is running is a separate, inherent-to-cron
      # caveat — true of every cron consumer — and deliberately not asserted.)
      if ! crontab -l 2>/dev/null | grep -Fq -- "$self run --vault '$vault'"; then
        warn "the crontab entry did not land; cron did not keep it"
        return 1
      fi
      ;;
    *) warn "no supported scheduler on $(uname); install a daily job yourself"; return 1;;
  esac
}

# ============================ dispatch ============================
# Only dispatch when RUN, not when SOURCED. Sourcing exposes the schedule
# validators (plist_problems, cron_plan, write_backup_plist) to the self-test on ANY
# platform — which is the only way the Darwin branch's logic gets covered on the
# ubuntu CI runner, and the Linux branch's on a Mac. When executed normally,
# $0 and ${BASH_SOURCE[0]} are the same string, so behaviour is unchanged.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  CMD="${1:-status}"; shift 2>/dev/null || true
  case "$CMD" in
    setup)    cmd_setup "$@";;
    run)      cmd_run "$@";;
    verify)   cmd_verify "$@";;
    status)   cmd_status "$@";;
    schedule) cmd_schedule "$@";;
    -h|--help|help)
      sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) die "unknown command: $CMD (use setup|run|verify|status|schedule)";;
  esac
fi
