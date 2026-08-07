#!/usr/bin/env bash
# relocate-vault.sh — move a vault OUT of a consumer cloud-sync folder onto a
# local disk, leave a symlink so existing references keep resolving, AND migrate
# Claude Code's path-keyed state (session transcripts + the agent-memory symlink)
# so prior session history survives the move.
#
# WHY
# ---
# A git-backed Obsidian/AI-brain vault inside iCloud Drive / OneDrive / Dropbox /
# Google Drive / Box melts the OS sync daemon — the high-churn .git/ + per-session
# worktree checkouts generate millions of file events. The supported fix (Shape A
# in docs/CLOUD_SYNC.md) is to move the vault onto a local disk and leave a
# symlink. A RAW `mv` does the move but SILENTLY ORPHANS Claude Code history:
# Claude stores per-project state under <config>/projects/<key>, where <key> is
# the absolute cwd with every non-alphanumeric character replaced by '-'. Moving
# the vault changes the key, so every prior transcript reads "Session history
# unavailable ... no longer on disk" and the agent-memory symlink dangles. This
# helper does the move AND re-homes that state (copy, never move — old keys stay
# as a backup), so reopening Claude Code in the relocated vault shows the history.
#
# Pure bash + python3 (only for the key transform, to match Claude Code's
# char-wise semantics exactly). No third-party deps.
#
# Usage:
#   relocate-vault.sh <old-vault-path> <new-vault-path> [options]
#   relocate-vault.sh --migrate-claude-state <old-abs-path> <new-abs-path>
#     (state-only: the vault is already at the new path; just fix Claude history)
#   relocate-vault.sh --sweep <old-path> [<new-path>]
#     (report residual references to the old path — classified executed /
#      doc-pointer / keep — with a go/no-go on retiring the symlink)
#   relocate-vault.sh --drop-symlink <old-path> [<new-path>]
#     (retire the old-path symlink, but ONLY when the sweep finds zero executed
#      references; otherwise it refuses and lists what still points at the old path)
#
# Options:
#   --dry-run            print intended actions, change nothing
#   --no-symlink         do NOT leave a symlink at the old path (default leaves one)
#   --force              skip the soft gates (Obsidian-running, active-session,
#                        backup-first). target-exists and source-already-a-symlink
#                        stay FATAL.
#   --ensure-backup      if there is no surviving off-machine backup, STAND ONE UP
#                        (vault-backup.sh setup + verify) BEFORE moving, then proceed
#                        — so a non-technical user reaches a backed-up, relocated
#                        vault in ONE step, without knowing --force or running
#                        vault-backup.sh by hand. Fail-closed: if the stand-up does
#                        not verify, the move is REFUSED (vault untouched). This is
#                        what the SessionStart cloud-sync offer runs.
#   --backup-dest <dir>  where --ensure-backup writes the backup archive (default:
#                        <parent-of-vault>/ai-brain-backups — one file that survives
#                        the move; for a cloud vault that sibling is off-machine).
#   --backup-schedule <daily|none>
#                        schedule the stood-up backup installs (default: daily).
#   --config-dir <dir>   Claude Code config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)
#   -h, --help           this help
#
# --sweep / --drop-symlink shell out to scripts/relocate-sweep.py (same dir).
#
# Exit codes: 0 ok / no-op / GO · 1 refused (gate) / NO-GO · 2 usage · 4 partial failure
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OLD="" ; NEW="" ; DRYRUN=0 ; FORCE=0 ; NOSYMLINK=0 ; MIGRATE_ONLY=0 ; SWEEP=0 ; DROP=0
ENSURE_BACKUP=0 ; BACKUP_DEST="" ; BACKUP_SCHEDULE="daily"
SWEEP_EXTRA=()
CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|--dryrun) DRYRUN=1; shift;;
    --no-symlink) NOSYMLINK=1; shift;;
    --force) FORCE=1; shift;;
    --ensure-backup) ENSURE_BACKUP=1; shift;;
    --backup-dest) BACKUP_DEST="${2:?--backup-dest needs a path}"; shift 2;;
    --backup-schedule) BACKUP_SCHEDULE="${2:?--backup-schedule needs daily|none}"; shift 2;;
    --config-dir) CONFIG_DIR="${2:?--config-dir needs a path}"; shift 2;;
    --migrate-claude-state) MIGRATE_ONLY=1; shift;;
    --sweep) SWEEP=1; shift;;
    --drop-symlink) DROP=1; shift;;
    --) shift; SWEEP_EXTRA=("$@"); break;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '/^set -euo/d; s/^# \{0,1\}//'; exit 0;;
    -*) echo "unknown option: $1" >&2; exit 2;;
    *) if [ -z "$OLD" ]; then OLD="$1"; elif [ -z "$NEW" ]; then NEW="$1"; else echo "unexpected arg: $1" >&2; exit 2; fi; shift;;
  esac
done

say()  { printf '%s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*" >&2; }
die()  { printf 'relocate-vault: REFUSE — %s\n' "$1" >&2; exit "${2:-1}"; }

# Obsidian-running probe, with a test seam so the negative-control suite can drive
# BOTH outcomes deterministically on any host. RELOCATE_VAULT_OBSIDIAN:
#   unset / auto -> real `pgrep -x Obsidian` probe — the production default;
#                   behavior is IDENTICAL to before this seam existed.
#   running      -> report Obsidian as running    (drives the refusal assertion).
#   absent       -> report Obsidian as not running (lets the tests exercise the
#                   OTHER soft gates on a dev box that has Obsidian open — the
#                   normal state when developing an Obsidian-vault tool).
# Mirrors the VAULT_BACKUP_SKIP_TIMEMACHINE / VAULT_BACKUP_CMD test seams already
# used below: the default path runs the real check, so real safety is unchanged.
obsidian_running() {
  case "${RELOCATE_VAULT_OBSIDIAN:-auto}" in
    running) return 0 ;;
    absent)  return 1 ;;
    *) command -v pgrep >/dev/null 2>&1 && pgrep -x Obsidian >/dev/null 2>&1 ;;
  esac
}

# abspath/pathkey via python3 so the key transform matches Claude Code's JS
# `cwd.replace(/[^a-zA-Z0-9]/g, "-")` for spaces, dots, slashes, and the ⚙️
# variation-selector exactly. A byte-wise `sed` would mis-key multibyte paths.
abspath() { python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$1"; }
# projkey: Claude Code's per-project dir name = the absolute cwd run through
# `replace(/[^A-Za-z0-9]/g, "-")`. cwd is the PHYSICAL path (getcwd resolves
# symlinks), so resolve the ANCESTRY — but NEVER follow a leaf symlink: post-move
# the old leaf IS the symlink we leave behind, and following it would key the new
# path. Resolve dirname, keep basename literal. Matches both before and after the
# move, and the two modes (full relocate / state-only) compute the same key.
projkey() { python3 -c 'import os,re,sys
p = os.path.abspath(os.path.expanduser(sys.argv[1]))
resolved = os.path.join(os.path.realpath(os.path.dirname(p)), os.path.basename(p))
print(re.sub(r"[^a-zA-Z0-9]", "-", resolved))' "$1"; }

# ---- migrate Claude Code path-keyed state (sessions + agent memory) -----------
# Copies <config>/projects/<oldkey>* -> <newkey>* (prefix swap preserves every
# sub-key suffix verbatim — worktree subdirs etc.) and re-homes the agent-memory
# symlink under the new base key. Copy, never move: old keys remain a backup.
# Fail LOUD when zero keys match (don't silently report "0 migrated").
migrate_claude_state() {  # $1=old-abs  $2=new-abs
  local old="$1" new="$2"
  local projdir="$CONFIG_DIR/projects"
  if [ ! -d "$projdir" ]; then
    say "  · no Claude Code projects dir ($projdir) — nothing to migrate"
    return 0
  fi
  local oldkey newkey
  oldkey="$(projkey "$old")"
  newkey="$(projkey "$new")"
  if [ "$oldkey" = "$newkey" ]; then
    say "  · old and new path keys are identical — nothing to migrate"
    return 0
  fi

  local matched=0 keys=0 files=0
  local d nb nd before after
  while IFS= read -r d; do
    [ -d "$d" ] || continue
    matched=$((matched + 1))
    nb="$(basename "$d" | sed "s#^${oldkey}#${newkey}#")"
    nd="$projdir/$nb"
    if [ "$DRYRUN" = 1 ]; then
      say "  · would migrate $(basename "$d") -> $nb"
      keys=$((keys + 1))
      continue
    fi
    mkdir -p "$nd"
    before="$(find "$nd" -maxdepth 1 -name '*.jsonl' 2>/dev/null | wc -l | tr -d ' ')"
    find "$d" -maxdepth 1 -name '*.jsonl' -exec cp -np {} "$nd"/ \; 2>/dev/null || true
    after="$(find "$nd" -maxdepth 1 -name '*.jsonl' 2>/dev/null | wc -l | tr -d ' ')"
    keys=$((keys + 1))
    files=$((files + after - before))
  done < <(find "$projdir" -maxdepth 1 -type d -name "${oldkey}*" 2>/dev/null)

  if [ "$matched" = 0 ]; then
    warn "  · no Claude Code session history found under old key '${oldkey}*' in $projdir."
    warn "    Nothing to migrate, OR the path-key encoding differs from what was expected —"
    warn "    inspect $projdir if you expected prior sessions here."
    return 0
  fi
  if [ "$DRYRUN" = 1 ]; then
    say "  · would migrate transcripts across $keys project key(s) -> new path key"
  else
    say "  · migrated $files transcript(s) across $keys project key(s) -> new path key"
  fi

  # Re-home the agent-memory symlink under the new base key. Prefer repointing
  # an existing old-key symlink (handles any memory layout); fall back to the
  # ai-brain-starter convention "⚙️ Meta/Agent Memory".
  local oldmem newmem target
  oldmem="$projdir/$oldkey/memory"
  newmem="$projdir/$newkey/memory"
  if [ ! -e "$newmem" ]; then
    if [ -L "$oldmem" ]; then
      target="$(readlink "$oldmem")"
      case "$target" in
        "$old"/*) target="$new/${target#"$old"/}";;
        "$old")   target="$new";;
      esac
    else
      target="$new/⚙️ Meta/Agent Memory"
    fi
    if [ -d "$target" ]; then
      if [ "$DRYRUN" = 1 ]; then
        say "  · would re-link agent-memory -> $target"
      else
        mkdir -p "$projdir/$newkey"
        ln -s "$target" "$newmem"
        say "  · agent-memory re-linked -> $target"
      fi
    fi
  fi
}

# ---- record the move in the relocation manifest (the watchdog's source of truth) ---
# scripts/relocate-sweep.py --watch reads $CONFIG_DIR/relocations.json to learn which
# (old,new) move(s) to watch for drift back to the old path — NEVER a hardcoded literal,
# so a paying install passes its own move and this one passes ours. Upsert keyed on
# `old`. Non-fatal by construction: a manifest hiccup must never fail a real relocation.
record_relocation() {  # $1=old-abs  $2=new-abs  $3=symlink(0|1)
  local old="$1" new="$2" symlink="$3"
  local manifest="$CONFIG_DIR/relocations.json"
  if [ "$DRYRUN" = 1 ]; then
    say "  · would record the move in $manifest (the watchdog reads this)"
    return 0
  fi
  mkdir -p "$CONFIG_DIR" 2>/dev/null || true
  if python3 - "$manifest" "$old" "$new" "$symlink" <<'PY'
import json, os, sys, tempfile, time
manifest, old, new, symlink = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
try:
    data = json.load(open(manifest)) if os.path.isfile(manifest) else []
    if not isinstance(data, list):
        data = []
except (OSError, ValueError):
    data = []
data = [e for e in data if not (isinstance(e, dict) and e.get("old") == old)]  # upsert by old
data.append({"old": old, "new": new, "symlink": symlink,
             "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
d = os.path.dirname(manifest) or "."
fd, tmp = tempfile.mkstemp(dir=d, prefix=".relocations.")
with os.fdopen(fd, "w") as f:
    json.dump(data, f, indent=2)
os.replace(tmp, manifest)
PY
  then
    say "  · recorded the move in $manifest (scripts/relocate-sweep.py --watch reads this)"
  else
    warn "could not record the move in $manifest (the watchdog will not see it until recorded)"
  fi
}

# ---- stand up a verified off-machine backup, THEN let the move proceed (MYC-2404) ---
# The MYC-2382 gate REFUSES a backup-less move but only PRINTS the fix — a non-
# technical user on a melting cloud-sync setup doesn't know --force and won't run
# vault-backup.sh by hand, so they stay stuck on the very setup the offer meant to
# rescue them from. --ensure-backup FULFILLS the SessionStart offer's "I'll stand
# up a verified backup first" promise IN the mechanism: run vault-backup.sh setup
# (snapshots immediately) then verify (proves it restores). FAIL-CLOSED — if either
# step fails, die BEFORE the move so the vault is never touched. The caller RE-CHECKS
# with check-vault-backup.py afterward, so a tool that lies about success can't slip
# an unbacked move through. VAULT_BACKUP_CMD overrides the tool (test injection).
ensure_backup_standup() {  # uses $OLD_ABS $BACKUP_DEST $BACKUP_SCHEDULE $DRYRUN $SCRIPT_DIR
  local backup_cmd dest
  backup_cmd="${VAULT_BACKUP_CMD:-$SCRIPT_DIR/vault-backup.sh}"
  dest="${BACKUP_DEST:-$(dirname "$OLD_ABS")/ai-brain-backups}"
  if [ ! -f "$backup_cmd" ]; then
    die "--ensure-backup cannot stand up a backup — the vault-backup tool is missing ($backup_cmd). Restore it, or re-run with --force to move without a backup."
  fi
  if [ "$DRYRUN" = 1 ]; then
    say "DRY  would: stand up + verify an off-machine backup BEFORE the move —"
    say "DRY    bash \"$backup_cmd\" setup --vault \"$OLD_ABS\" --dest \"$dest\" --schedule \"$BACKUP_SCHEDULE\""
    say "DRY    bash \"$backup_cmd\" verify --vault \"$OLD_ABS\""
    return 0
  fi
  say "relocate-vault: --ensure-backup — standing up a verified off-machine backup BEFORE the move"
  say "  · backup destination: $dest"
  if ! bash "$backup_cmd" setup --vault "$OLD_ABS" --dest "$dest" --schedule "$BACKUP_SCHEDULE"; then
    die "backup stand-up FAILED at 'setup' — NOT moving the vault (fail-closed; your vault is untouched).
  Check the backup destination (disk reachable? space? permissions?), then re-run; or re-run with --force to move without a backup." 1
  fi
  if ! bash "$backup_cmd" verify --vault "$OLD_ABS"; then
    die "backup stand-up FAILED at 'verify' — the snapshot did not restore, so it is not a trustworthy backup. NOT moving the vault (fail-closed; your vault is untouched).
  Re-run once the backup verifies, or re-run with --force to move without a verified backup." 1
  fi
  # The auto stand-up produces an UNENCRYPTED archive (setup ran without --encrypt,
  # which needs an interactive passphrase and would break the one-step flow). Say so
  # LOUDLY: this path only runs when there is no Time Machine / pushed git-remote, so
  # the archive is the only backup, and its default home is often a synced cloud
  # folder — and a vault may hold private notes. Do not silently drop a plaintext
  # copy of someone's brain into their cloud.
  warn "relocate-vault: the stand-up backup is NOT encrypted — it may contain private notes,"
  warn "  and now lives in: $dest (often a synced cloud folder)."
  warn "  To encrypt it, re-run the backup with --encrypt and RECORD the passphrase OFF this"
  warn "  machine (a password manager) — a keychain-only passphrase dies with the disk:"
  warn "    bash \"$backup_cmd\" setup --vault \"$OLD_ABS\" --dest \"$dest\" --encrypt"
  say "relocate-vault: backup stood up + verified — proceeding with the move."
}

# =============================================================================
# state-only mode: the vault is already at the new path; just fix Claude history
# =============================================================================
if [ "$MIGRATE_ONLY" = 1 ]; then
  if [ -z "$OLD" ] || [ -z "$NEW" ]; then
    echo "usage: $(basename "$0") --migrate-claude-state <old-abs-path> <new-abs-path>" >&2
    exit 2
  fi
  OLD_ABS="$(abspath "$OLD")"
  NEW_ABS="$(abspath "$NEW")"
  say "relocate-vault: migrating Claude Code state only ($OLD_ABS -> $NEW_ABS)"
  migrate_claude_state "$OLD_ABS" "$NEW_ABS"
  SYM=0; if [ -L "$OLD_ABS" ]; then SYM=1; fi
  record_relocation "$OLD_ABS" "$NEW_ABS" "$SYM"
  exit 0
fi

# Run the code-repo-aware residual sweep. Returns the sweep's exit code:
# 0 = GO (zero executed references) · 1 = NO-GO (executed references remain).
sweep_old_refs() {  # $1=old  [$2=new];  forwards any args given after `--`
  local sweep_py="$SCRIPT_DIR/relocate-sweep.py"
  [ -f "$sweep_py" ] || die "relocate-sweep.py not found next to this script ($sweep_py)"
  local args=( --old "$1" )
  [ -n "${2:-}" ] && args+=( --new "$2" )
  if [ "${#SWEEP_EXTRA[@]}" -gt 0 ]; then args+=( "${SWEEP_EXTRA[@]}" ); fi
  python3 "$sweep_py" "${args[@]}"
}

# =============================================================================
# sweep mode: report residual references to the old path, classified, + go/no-go
# =============================================================================
if [ "$SWEEP" = 1 ]; then
  [ -n "$OLD" ] || { echo "usage: $(basename "$0") --sweep <old-path> [<new-path>]" >&2; exit 2; }
  set +e; sweep_old_refs "$OLD" "$NEW"; rc=$?; set -e
  exit "$rc"
fi

# =============================================================================
# drop-symlink mode: retire the old-path symlink ONLY when the sweep says GO
# =============================================================================
if [ "$DROP" = 1 ]; then
  [ -n "$OLD" ] || { echo "usage: $(basename "$0") --drop-symlink <old-path> [<new-path>]" >&2; exit 2; }
  [ -L "$OLD" ] || die "old path '$OLD' is not a symlink — nothing to drop (relocate first, or it is already gone)"
  # If the caller did not pass the new path, read it from the symlink target so the
  # sweep can name the repoint destination in its report.
  [ -n "$NEW" ] || NEW="$(readlink "$OLD")"
  say "relocate-vault: sweeping for residual references before retiring the symlink at '$OLD' ..."
  set +e; sweep_old_refs "$OLD" "$NEW"; rc=$?; set -e
  if [ "$rc" != 0 ]; then
    die "NO-GO — executed references still resolve the old path (see report above). Repoint them, then re-run --drop-symlink." 1
  fi
  if [ "$DRYRUN" = 1 ]; then
    say "DRY  would: rm '$OLD' (symlink) — sweep returned GO (zero executed references)."
    exit 0
  fi
  rm "$OLD"
  record_relocation "$OLD" "$NEW" 0
  say "relocate-vault: retired the symlink '$OLD' — sweep returned GO (zero executed references)."
  exit 0
fi

# =============================================================================
# full relocate
# =============================================================================
if [ -z "$OLD" ] || [ -z "$NEW" ]; then
  echo "usage: $(basename "$0") <old-vault-path> <new-vault-path> [--dry-run] [--force] [--no-symlink]" >&2
  exit 2
fi
[ -e "$OLD" ] || die "source '$OLD' not found (already moved?)"
if [ -L "$OLD" ]; then die "source '$OLD' is already a symlink (already relocated?)"; fi
[ -d "$OLD" ] || die "source '$OLD' is not a directory"

OLD_ABS="$(cd "$OLD" && pwd -P)"   # physical path, BEFORE the move
NEW_ABS="$(abspath "$NEW")"
[ "$OLD_ABS" != "$NEW_ABS" ] || die "source and target are the same path"
if [ -e "$NEW_ABS" ]; then die "target '$NEW_ABS' already exists (will not overwrite)"; fi

# Soft gates — skippable with --force.
BACKUP_TOKEN=""
if [ "$FORCE" != 1 ]; then
  if obsidian_running; then
    die "Obsidian is running — quit it first (moving an open vault is unsafe), or pass --force"
  fi
  if [ -d "$OLD_ABS/.claude/worktrees" ]; then
    active="$(find "$OLD_ABS/.claude/worktrees" -type f -mmin -10 2>/dev/null | head -1 || true)"
    if [ -n "$active" ]; then
      die "an active Claude session is writing under .claude/worktrees (touched <10 min) — close it, or pass --force"
    fi
  fi
  # Backup-first ENFORCEMENT (MYC-2382). A vault is often the one irreplaceable
  # asset; moving it with no off-machine backup is the one move you cannot undo
  # if the disk dies mid-flight. Route through the SINGLE source of truth
  # (check-vault-backup.py) and REFUSE on NO_BACKUP. The whole block is skipped
  # under --force, so --force IS the documented escape hatch — by construction.
  #
  # --ignore-cloud (MYC-2401): this move REMOVES the source's cloud-sync copy —
  # we mv the vault OUT and leave a symlink, so the sync daemon then follows a
  # few-byte symlink, not the tree. A cloud copy therefore does NOT survive the
  # move and must NOT satisfy the gate (else we'd green-light the move citing the
  # very backup the move destroys, leaving the user with nothing). Only backups
  # that survive — a vault-backup archive, Time Machine, a pushed git remote —
  # count here.
  backup_guard="$SCRIPT_DIR/check-vault-backup.py"
  if [ ! -f "$backup_guard" ]; then
    die "cannot verify a backup — the guard is missing ($backup_guard). Restore it, or re-run with --force to move without the check."
  fi
  set +e
  BACKUP_TOKEN="$(python3 "$backup_guard" --porcelain --ignore-cloud "$OLD_ABS" 2>/dev/null)"
  brc=$?
  set -e
  if [ "$brc" != 0 ]; then
    if [ "$ENSURE_BACKUP" = 1 ]; then
      # No surviving backup, but the caller asked us to STAND ONE UP. Do it, then
      # RE-CHECK through the same single source of truth — never trust the stand-up's
      # own exit code alone. A stand-up that "succeeds" but lands no archive must
      # still refuse the move (fail-closed; no silent no-op). Dry-run only previews.
      ensure_backup_standup
      if [ "$DRYRUN" != 1 ]; then
        set +e
        BACKUP_TOKEN="$(python3 "$backup_guard" --porcelain --ignore-cloud "$OLD_ABS" 2>/dev/null)"
        brc=$?
        set -e
        if [ "$brc" != 0 ]; then
          die "backup stand-up reported success but no surviving backup is detectable (${BACKUP_TOKEN:-none}) — NOT moving the vault (fail-closed; your vault is untouched).
  Check the backup destination, then re-run; or re-run with --force to move without the check." 1
        fi
      fi
    else
      die "no verified off-machine backup of '$OLD_ABS' that survives this move (${BACKUP_TOKEN:-backup check failed}).
  Moving a vault with no backup is the one move you cannot undo if the disk dies mid-flight.
  A cloud-sync copy does NOT count here — this move takes the vault OUT of the sync folder, so that copy goes away too.
  Hands-off fix — stand up a verified backup AND move, in ONE step:
    bash \"$0\" --ensure-backup \"$OLD_ABS\" \"$NEW_ABS\"
  Or stand it up yourself first, then re-run this move:
    bash \"$SCRIPT_DIR/vault-backup.sh\" setup && bash \"$SCRIPT_DIR/vault-backup.sh\" verify
  Or move anyway, accepting the risk: re-run with --force."
    fi
  fi
fi

say ""
if [ "$FORCE" = 1 ]; then
  warn "relocate-vault: --force — skipping the backup check. A vault is often your one"
  warn "  irreplaceable asset; stand up + verify an off-machine backup as soon as the move lands:"
  warn "    bash \"$SCRIPT_DIR/vault-backup.sh\" setup && bash \"$SCRIPT_DIR/vault-backup.sh\" verify"
  warn "  (or drop --force and add --ensure-backup to stand one up automatically BEFORE the move.)"
elif [ "$brc" = 0 ]; then
  say "relocate-vault: verified off-machine backup present (${BACKUP_TOKEN}) — proceeding."
else
  # Reachable only under --dry-run --ensure-backup: no real backup yet, just previewing.
  say "relocate-vault: (dry-run) a verified backup would be stood up before the move."
fi
say ""

if [ "$DRYRUN" = 1 ]; then
  say "DRY  would: mv '$OLD_ABS' -> '$NEW_ABS'"
  [ "$NOSYMLINK" = 1 ] || say "DRY  would: ln -s '$NEW_ABS' '$OLD_ABS'"
  migrate_claude_state "$OLD_ABS" "$NEW_ABS"
  SYM=1; if [ "$NOSYMLINK" = 1 ]; then SYM=0; fi
  record_relocation "$OLD_ABS" "$NEW_ABS" "$SYM"
  say "DRY-RUN complete — no changes made."
  exit 0
fi

say "relocate-vault: moving '$OLD_ABS' -> '$NEW_ABS'"
mkdir -p "$(dirname "$NEW_ABS")"
mv "$OLD_ABS" "$NEW_ABS"
[ -d "$NEW_ABS" ] || die "POST: '$NEW_ABS' missing after move" 4

if [ "$NOSYMLINK" != 1 ]; then
  ln -s "$NEW_ABS" "$OLD_ABS"
  if [ -L "$OLD_ABS" ] && [ "$(readlink "$OLD_ABS")" = "$NEW_ABS" ]; then
    say "relocate-vault: left symlink '$OLD_ABS' -> '$NEW_ABS' (old references keep resolving; the sync daemon follows the tiny symlink, not the churn)"
  else
    warn "POST: expected symlink not in place at $OLD_ABS"
  fi
fi

say "relocate-vault: migrating Claude Code session history + agent memory ..."
migrate_claude_state "$OLD_ABS" "$NEW_ABS"

SYM=1; if [ "$NOSYMLINK" = 1 ]; then SYM=0; fi
record_relocation "$OLD_ABS" "$NEW_ABS" "$SYM"

say ""
say "relocate-vault: DONE."
say "  - Vault now lives at:  $NEW_ABS   (outside the sync folder)"
[ "$NOSYMLINK" = 1 ] || say "  - Old path is a symlink: $OLD_ABS -> $NEW_ABS   (scripts/hooks/CLAUDE.md keep working)"
say "  - Reopen Obsidian; if it lost the vault, open $NEW_ABS"
say "  - Reopen Claude Code in the vault — prior sessions appear in the picker"
say "    (history was re-homed to the new path key; the old keys are kept as a backup)"
say "  - Re-run a backup against the new path:  VAULT_ROOT=\"$NEW_ABS\" bash scripts/vault-backup.sh"
