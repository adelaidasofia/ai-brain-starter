#!/usr/bin/env bash
# diagnose.sh - Self-check for an installed AI Brain Starter vault.
# Runs ~10 checks and prints a green/yellow/red report.
#
# Usage:
#   bash diagnose.sh              # auto-detect vault from $VAULT_PATH or cwd
#   bash diagnose.sh /path/vault  # check a specific vault
#
# Exit codes:
#   0 = all green
#   1 = at least one red (something is broken)
#   2 = only yellows (warnings, not broken)
#
# Designed to be safe to run any time. No writes, no network.

set -u

# --- ai-brain-starter: shim-safe PATH (strip refuse-shims) ----------------
# Some machines carry a python3/python PATH shim (e.g. trailofbits
# modern-python) that exit-1s on bare invocation and would turn every bare
# python call below into a silent no-op. Drop any */hooks/shims dir from PATH
# so bare python calls here (and, via export, in children) hit a real python.
if [ "${PATH#*/hooks/shims}" != "$PATH" ]; then
  _abs_new=""; _abs_oifs=$IFS; IFS=:
  for _abs_d in $PATH; do
    case $_abs_d in */hooks/shims|*/hooks/shims/) ;; *) _abs_new=${_abs_new:+$_abs_new:}$_abs_d ;; esac
  done
  IFS=$_abs_oifs; PATH=$_abs_new; export PATH
  unset _abs_new _abs_d _abs_oifs
fi
# --------------------------------------------------------------------------

# ----- color helpers (no-op if not a tty) -----
if [ -t 1 ]; then
  G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; B=$'\033[1m'; N=$'\033[0m'
else
  G=''; Y=''; R=''; B=''; N=''
fi

GREEN=0; YELLOW=0; RED=0

ok()   { echo "  ${G}OK${N}    $1"; GREEN=$((GREEN+1)); }
warn() { echo "  ${Y}WARN${N}  $1"; [ -n "${2:-}" ] && echo "        -> $2"; YELLOW=$((YELLOW+1)); }
bad()  { echo "  ${R}FAIL${N}  $1"; [ -n "${2:-}" ] && echo "        -> $2"; RED=$((RED+1)); }

section() { echo; echo "${B}$1${N}"; }

# ----- locate vault -----
VAULT="${1:-${VAULT_PATH:-$PWD}}"
if [ ! -d "$VAULT" ]; then
  echo "${R}Vault path not a directory:${N} $VAULT"
  echo "Pass the vault path: bash diagnose.sh /path/to/vault"
  exit 1
fi
VAULT="$(cd "$VAULT" && pwd)"

echo "${B}AI Brain Starter diagnostics${N}"
echo "Vault: $VAULT"

# ----- 1. CLAUDE.md exists -----
section "1. Vault memory (CLAUDE.md)"
if [ -f "$VAULT/CLAUDE.md" ]; then
  size=$(wc -c < "$VAULT/CLAUDE.md" | tr -d ' ')
  if [ "$size" -lt 200 ]; then
    warn "CLAUDE.md exists but is tiny ($size bytes)" "Re-run /setup-brain Phase 4 to rebuild it."
  else
    ok "CLAUDE.md present ($size bytes)"
  fi
  if grep -q "## Vault Map" "$VAULT/CLAUDE.md"; then
    ok "Vault Map section present"
  else
    warn "No '## Vault Map' section in CLAUDE.md" "Claude will create duplicate folders without it."
  fi
else
  bad "CLAUDE.md missing" "Run /setup-brain Phase 4."
fi

# ----- 2. Meta folder structure -----
section "2. Meta folder"
# Resolve the Meta folder via the shared resolver so a plain "Meta/" and an
# emoji "⚙️ Meta/" are both handled (and a machine "Meta/" can't shadow the
# human one). Falls back to the decorated default when the resolver is absent.
META_RESOLVER="$(cd "$(dirname "$0")" && pwd)/_meta_resolver.py"
[ -f "$META_RESOLVER" ] || META_RESOLVER="$HOME/.claude/skills/ai-brain-starter/scripts/_meta_resolver.py"
META=""
if [ -f "$META_RESOLVER" ]; then
  META="$(python3 "$META_RESOLVER" "$VAULT" scripts rules Decisions 2>/dev/null || true)"
fi
[ -z "$META" ] && META="$VAULT/⚙️ Meta"
if [ -d "$META" ]; then
  ok "Meta folder present ($(basename "$META")/)"
  for sub in scripts rules; do
    if [ -d "$META/$sub" ]; then
      ok "$(basename "$META")/$sub/ present"
    else
      warn "$(basename "$META")/$sub/ missing"
    fi
  done
else
  bad "Meta folder missing (looked for ⚙️ Meta/ and Meta/)" "Run /setup-brain Phase 3."
fi

# ----- 3. Skills installed -----
section "3. Claude Code skills"
SKILLS_DIR="$HOME/.claude/skills"
if [ -d "$SKILLS_DIR" ]; then
  # shellcheck disable=SC2088  # literal ~ in user-facing message, not a path
  ok "~/.claude/skills/ exists"
  # ai-brain-starter itself
  if [ -d "$SKILLS_DIR/ai-brain-starter" ]; then
    ok "ai-brain-starter skill installed"
  else
    warn "ai-brain-starter skill not in ~/.claude/skills/" "Re-run bootstrap to symlink it."
  fi
  # daily-journal is the most-used downstream skill
  if [ -d "$SKILLS_DIR/daily-journal" ] || [ -f "$SKILLS_DIR/daily-journal/SKILL.md" ]; then
    ok "daily-journal skill installed"
  else
    warn "daily-journal skill not installed" "/setup-brain Phase 10a creates it."
  fi
else
  # shellcheck disable=SC2088  # literal ~ in user-facing message, not a path
  bad "~/.claude/skills/ missing" "Claude Code may not be installed, or the skills dir was deleted."
fi

# ----- 4. Hooks registered -----
section "4. Claude Code hooks"
SETTINGS="$HOME/.claude/settings.json"
LOCAL_SETTINGS="$VAULT/.claude/settings.local.json"
hook_found=0
for f in "$SETTINGS" "$LOCAL_SETTINGS"; do
  if [ -f "$f" ] && grep -q '"hooks"' "$f" 2>/dev/null; then
    hook_found=1
    ok "hooks registered in $f"
  fi
done
if [ "$hook_found" -eq 0 ]; then
  warn "No hooks registered in settings.json or .claude/settings.local.json" \
    "/setup-brain Phase 5 wires them. Without them, no auto context-loading."
fi

# Wired-but-absent: a hook command naming ~/.claude/hooks/<file> that is not on
# disk. Every such command carries a `[ -f ]` guard (or hook_runner's
# --fallback silent), so the miss is a SILENT no-op -- the hook never fires and
# nothing says so. The `grep -q '"hooks"'` check above passes even when every
# referenced file is gone.
#
# Observed on a real install: vault-context.py wired 7 times, present 0 times,
# alongside retry-budget.py and validate-mcp-json.py -- all three are phase-05
# "install by default" copies that never ran. scripts/check-home-hook-deploy.py
# does NOT cover this: it is a static REPO lint proving each hook has a
# documented deploy route, and cannot see whether that route ever executed here.
#
# WARNING, not error: phase-05 documents `rm ~/.claude/hooks/<name>.py` as the
# supported uninstall, so an absent file is genuinely ambiguous between
# never-installed and deliberately-removed. Naming it lets the reader tell which.
ABS_HOOKS_DIR="$HOME/.claude/hooks"
ABS_HOOKS_SRC="$HOME/.claude/skills/ai-brain-starter/hooks"
wired_names=""
for f in "$SETTINGS" "$LOCAL_SETTINGS"; do
  [ -f "$f" ] || continue
  # Anchor on .claude/hooks/ so the starter's own hooks dir
  # (.claude/skills/ai-brain-starter/hooks/) is not swept in. The separator
  # class covers POSIX "/" and JSON-escaped Windows "\\".
  names=$(grep -oE '\.claude[\\/]+hooks[\\/]+[A-Za-z0-9_.-]+\.(py|sh)' "$f" 2>/dev/null \
          | sed 's#.*[\\/]##')
  [ -n "$names" ] && wired_names="$wired_names$names
"
done
wired_names=$(printf '%s' "$wired_names" | grep -v '^$' | sort -u)
if [ -n "$wired_names" ]; then
  wired_count=$(printf '%s\n' "$wired_names" | grep -c .)
  missing=""
  for n in $wired_names; do
    [ -f "$ABS_HOOKS_DIR/$n" ] || missing="$missing $n"
  done
  missing=$(printf '%s' "$missing" | sed 's/^ //')
  if [ -z "$missing" ]; then
    ok "all $wired_count wired ~/.claude/hooks/ file(s) present on disk"
  else
    missing_count=$(printf '%s\n' "$missing" | tr ' ' '\n' | grep -c .)
    hint="These never fire and report no error."
    for n in $missing; do
      if [ -f "$ABS_HOOKS_SRC/$n" ]; then
        hint="$hint Reinstall: cp $ABS_HOOKS_SRC/{$(printf '%s' "$missing" | tr ' ' ',')} $ABS_HOOKS_DIR/ -- or, if you removed them on purpose, this is expected."
        break
      fi
    done
    warn "$missing_count of $wired_count wired ~/.claude/hooks/ file(s) MISSING: $(printf '%s' "$missing" | tr ' ' ' ')" "$hint"
  fi
fi

# graph-context-hook script (the one that bit Windows users)
GCH="$META/scripts/graph-context-hook.sh"
if [ -f "$GCH" ]; then
  ok "graph-context-hook.sh present"
  if bash -n "$GCH" 2>/dev/null; then
    ok "graph-context-hook.sh parses cleanly"
  else
    bad "graph-context-hook.sh has bash syntax errors" "Run: bash -n '$GCH'"
  fi
else
  warn "graph-context-hook.sh not in ⚙️ Meta/scripts/" "Phase 5 installs it."
fi

# Sync-clobber check: a synced script whose sibling dependency did not come along
# (it is silently running a fallback stub), or a local patch the sync overwrote.
# Measured incident: the commit wrapper fell to a fail-closed stub and EVERY vault
# commit refused for ~19h, while the session-end hook fell to "defer" and silently
# skipped every snapshot. Neither surfaced anywhere until a detector existed.
# Resolved from this script's own location: diagnose.sh defines no repo variable,
# and a `[ -f "$UNSET/..." ]` guard would make this check a silent no-op — the
# exact failure class it exists to catch.
_ABS_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
CLOBBER="$_ABS_REPO/scripts/check-clobbered-vault-scripts.py"
if [ -f "$CLOBBER" ] && command -v python3 >/dev/null 2>&1; then
  CLOBBER_OUT=$(python3 "$CLOBBER" --vault "$VAULT" --repo "$_ABS_REPO" 2>&1)
  if [ $? -eq 0 ]; then
    ok "vault scripts: no broken sibling deps, none reverted by a sync"
  else
    # Report each finding on its own line so the remedy is actionable, not a blob.
    printf '%s\n' "$CLOBBER_OUT" | grep -E '^\s+(BROKEN-DEP|REVERTED)' | while IFS= read -r l; do
      warn "$(printf '%s' "$l" | sed 's/^[[:space:]]*//')"
    done
    warn "vault scripts need attention" "Full detail: python3 '$CLOBBER' --vault '$VAULT'"
  fi
else
  warn "sync-clobber check did not run" "Missing $CLOBBER or python3 — this check is INACTIVE, not passing."
fi

# ----- 5. Journal index -----
section "5. Insights pipeline"
INDEX="$META/journal-index.json"
if [ -f "$INDEX" ]; then
  if [ "$(uname)" = "Darwin" ]; then
    age_days=$(( ( $(date +%s) - $(stat -f %m "$INDEX") ) / 86400 ))
  else
    age_days=$(( ( $(date +%s) - $(stat -c %Y "$INDEX") ) / 86400 ))
  fi
  if [ "$age_days" -gt 14 ]; then
    warn "journal-index.json is $age_days days old" "Re-run build-journal-index.py or /weekly to refresh."
  else
    ok "journal-index.json is fresh ($age_days days old)"
  fi
  # quick JSON validity check
  if python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$INDEX" 2>/dev/null; then
    ok "journal-index.json is valid JSON"
  else
    bad "journal-index.json is malformed" "Delete it and re-run build-journal-index.py."
  fi
else
  warn "No journal-index.json yet" "/weekly or /monthly will build it on first run."
fi

# ----- 6. Required CLI tools -----
section "6. Required CLI tools"
for tool in git python3 jq; do
  if command -v "$tool" >/dev/null 2>&1; then
    ok "$tool installed"
  else
    if [ "$tool" = "jq" ]; then
      warn "$tool missing" "Optional, but some scripts prefer it. brew install jq"
    else
      bad "$tool missing" "Required. brew install $tool"
    fi
  fi
done

# ----- 7. Git in vault -----
section "7. Vault git status"
if [ -d "$VAULT/.git" ]; then
  ok "Vault is a git repo (snapshot history available)"
  remote=$(cd "$VAULT" && git remote -v 2>/dev/null | head -1)
  if [ -n "$remote" ]; then
    warn "Vault has a git remote: $remote" \
      "Vaults are usually local-only. Make sure you actually want this."
  else
    ok "No git remote (correct for a private vault)"
  fi
else
  warn "Vault is not a git repo" "You won't have rollback history. Optional but recommended."
fi

# ----- 8. .ps1 sanity (if any exist) -----
section "8. PowerShell files (Windows compat)"
# NEITHER .ps1 byte rule is reimplemented here. Both have one owner,
# scripts/check-ps1-encoding.sh, which the CI gate and the pre-push gate also
# call - so a fix lands everywhere at once. This block used to carry its own
# copy of both, sharing one enumeration capped at `head -20`: a vault with more
# than 20 .ps1 files printed "All .ps1 files have UTF-8 BOM" without ever
# reading the rest, while diagnose.ps1 (no cap) reported the same vault
# honestly. Same vault, two different answers.
CHECK_ENC=""
for c in "$(cd "$(dirname "$0")" && pwd)/check-ps1-encoding.sh" \
         "$HOME/.claude/skills/ai-brain-starter/scripts/check-ps1-encoding.sh"; do
  [ -f "$c" ] && CHECK_ENC="$c" && break
done
if [ -n "$CHECK_ENC" ]; then
  enc_verdict="$(bash "$CHECK_ENC" --porcelain "$VAULT" "$HOME/.claude/skills/ai-brain-starter" 2>/dev/null)"
  # Contract: `scanned=<n> missing_bom=<m> emdash=<k>`. Parsed field by field so
  # a malformed line leaves the counts empty and falls through to the warn below
  # rather than being read as zeros, which would print a clean bill of health.
  enc_scanned=""; enc_bom=""; enc_em=""
  for kv in $enc_verdict; do
    case "$kv" in
      scanned=*)     enc_scanned="${kv#scanned=}" ;;
      missing_bom=*) enc_bom="${kv#missing_bom=}" ;;
      emdash=*)      enc_em="${kv#emdash=}" ;;
    esac
  done
  if [ -z "$enc_scanned" ] || [ -z "$enc_bom" ] || [ -z "$enc_em" ]; then
    warn "Could not evaluate .ps1 encoding" "check-ps1-encoding.sh returned: ${enc_verdict:-<empty>}"
  elif [ "$enc_scanned" -eq 0 ]; then
    ok "No .ps1 files to check"
  else
    if [ "$enc_bom" -eq 0 ]; then
      ok "All .ps1 files have UTF-8 BOM ($enc_scanned scanned)"
    else
      warn "$enc_bom of $enc_scanned .ps1 file(s) missing UTF-8 BOM" \
        "Windows PowerShell 5.1 will crash on non-ASCII bytes."
    fi
    if [ "$enc_em" -eq 0 ]; then
      ok "No em dashes in .ps1 files ($enc_scanned scanned)"
    else
      warn "$enc_em of $enc_scanned .ps1 file(s) contain em dashes" \
        "Replace with ASCII hyphens (defense-in-depth)."
    fi
  fi
else
  warn "check-ps1-encoding.sh not found" "Cannot verify .ps1 BOM or em-dash status."
fi

# ----- 9. MCP config -----
section "9. MCP servers"
MCP_LOCAL="$VAULT/.mcp.json"
MCP_GLOBAL="$HOME/.claude.json"
mcp_found=0
for f in "$MCP_LOCAL" "$MCP_GLOBAL"; do
  if [ -f "$f" ] && grep -q '"mcpServers"' "$f" 2>/dev/null; then
    mcp_found=1
    if python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$f" 2>/dev/null; then
      ok "MCP config valid: $f"
    else
      bad "MCP config malformed: $f" "Invalid JSON. Fix the file."
    fi
  fi
done
[ "$mcp_found" -eq 0 ] && warn "No MCP config found" "MCPs are optional. Skip if you don't use them."

# ----- 10b. Scheduled-task naming hygiene -----
section "10b. Scheduled-task naming hygiene"
SCHED_DIR="$HOME/.claude/scheduled-tasks"
if [ -d "$SCHED_DIR" ]; then
  collide_count=0
  noprefix_count=0
  collide_names=""
  noprefix_names=""
  for entry in "$SCHED_DIR"/*; do
    [ -d "$entry" ] || continue
    [ -f "$entry/SKILL.md" ] || continue
    name=$(basename "$entry")
    # Collision: scheduled-task name matches an installed skill name.
    if [ -d "$HOME/.claude/skills/$name" ] || [ -f "$HOME/.claude/skills/$name/SKILL.md" ]; then
      collide_count=$((collide_count+1))
      collide_names="$collide_names $name"
    fi
    # Convention: cron-only tasks should prefix with _ so autocomplete
    # surfaces them at the bottom and reads them as cron-only.
    case "$name" in
      _*) ;;
      *) noprefix_count=$((noprefix_count+1)); noprefix_names="$noprefix_names $name";;
    esac
  done
  if [ "$collide_count" -eq 0 ] && [ "$noprefix_count" -eq 0 ]; then
    ok "Scheduled-task names are clean (no skill collisions, all underscore-prefixed)"
  else
    if [ "$collide_count" -gt 0 ]; then
      warn "$collide_count scheduled-task name(s) collide with installed skills:$collide_names" \
        "Rename: e.g. 'daily-journal' -> '_daily-journal-cron'. See docs/MAINTENANCE.md."
    fi
    if [ "$noprefix_count" -gt 0 ]; then
      warn "$noprefix_count scheduled-task name(s) lack '_' prefix:$noprefix_names" \
        "Cron-only tasks should start with _ to read as cron-only in autocomplete. See docs/MAINTENANCE.md."
    fi
  fi
else
  ok "No ~/.claude/scheduled-tasks/ directory (skipped)"
fi

# ----- 10. SessionStart freshness check -----
section "10. ai-brain-starter freshness"
ABS_DIR="$HOME/.claude/skills/ai-brain-starter"
if [ -d "$ABS_DIR/.git" ]; then
  cd "$ABS_DIR" || exit 1
  local_sha=$(git rev-parse HEAD 2>/dev/null | cut -c1-7)
  if git fetch origin main --quiet 2>/dev/null; then
    remote_sha=$(git rev-parse origin/main 2>/dev/null | cut -c1-7)
    if [ "$local_sha" = "$remote_sha" ]; then
      ok "ai-brain-starter is up to date ($local_sha)"
    else
      behind=$(git rev-list --count "$local_sha..$remote_sha" 2>/dev/null)
      warn "ai-brain-starter is $behind commit(s) behind origin/main" \
        "cd ~/.claude/skills/ai-brain-starter && git pull"
    fi
  else
    warn "Could not fetch from origin (offline?)"
  fi
  cd - >/dev/null || exit 1
else
  # shellcheck disable=SC2088  # literal ~ in user-facing message, not a path
  warn "~/.claude/skills/ai-brain-starter is not a git repo" "Re-run bootstrap.sh to clone it."
fi

# ----- 11. cloud-sync location (the freeze class) -----
section "11. Cloud-sync location"
CHECK_CLOUD=""
for c in "$(cd "$(dirname "$0")" && pwd)/check-cloud-sync.py" \
         "$HOME/.claude/skills/ai-brain-starter/scripts/check-cloud-sync.py"; do
  [ -f "$c" ] && CHECK_CLOUD="$c" && break
done
if [ -n "$CHECK_CLOUD" ]; then
  verdict="$(python3 "$CHECK_CLOUD" --porcelain "$VAULT" 2>/dev/null)"
  case "$verdict" in
    OK_LOCAL)
      ok "Vault is on a local disk (not a consumer cloud-sync root)" ;;
    CLOUD_SYNC_RISK:*)
      bad "Vault is inside ${verdict#CLOUD_SYNC_RISK:} — a consumer cloud-sync folder" \
        "A git-backed vault here melts the sync daemon (pegged CPU / frozen machine). Move it local (e.g. ~/Brain). See docs/CLOUD_SYNC.md." ;;
    *)
      warn "Could not evaluate cloud-sync location" "check-cloud-sync.py returned: ${verdict:-<empty>}" ;;
  esac
else
  warn "check-cloud-sync.py not found" "Cannot verify the vault is outside a cloud-sync root."
fi

# ----- 12. off-machine backup (the one-disk-failure class) -----
section "12. Off-machine backup"
CHECK_BACKUP=""
for c in "$(cd "$(dirname "$0")" && pwd)/check-vault-backup.py" \
         "$HOME/.claude/skills/ai-brain-starter/scripts/check-vault-backup.py"; do
  [ -f "$c" ] && CHECK_BACKUP="$c" && break
done
if [ -n "$CHECK_BACKUP" ]; then
  bverdict="$(python3 "$CHECK_BACKUP" --porcelain "$VAULT" 2>/dev/null)"
  case "$bverdict" in
    BACKED_UP:vault-backup:*)
      # An archive that EXISTS is not the same as a backup that RUNS. Mirror the
      # staleness contract of hooks/surface-backup-status.py so the two surfaces
      # cannot disagree: past the threshold, a snapshot is evidence the schedule
      # stopped firing, not evidence of a healthy backup.
      _bage="${bverdict##*:}"
      _bstale="${VAULT_BACKUP_STALE_DAYS:-3}"
      if awk "BEGIN{exit !($_bage > $_bstale)}" 2>/dev/null; then
        warn "Last vault snapshot is ~${_bage} days old (> ${_bstale}d) — the backup is not running on schedule" \
          "Run it now: bash scripts/vault-backup.sh run --vault '$VAULT'. Then find out why the schedule stopped firing."
      else
        ok "Off-machine backup present (vault-backup, ~${_bage} days old)"
      fi ;;
    BACKED_UP:timemachine)   ok "Off-machine backup present (Time Machine destination configured)" ;;
    BACKED_UP:cloud:*)       ok "Off-machine copy present (${bverdict#BACKED_UP:cloud:} — a cloud copy; single-file snapshots are safer, see docs/BACKUP.md)" ;;
    BACKED_UP:git-remote)    ok "Off-machine backup present (git HEAD pushed to a remote)" ;;
    NO_BACKUP:configured-not-run)
      warn "Backup configured but no snapshot exists yet (or destination unreachable)" \
        "Run: bash scripts/vault-backup.sh run --vault '$VAULT'" ;;
    NO_BACKUP)
      bad "Vault has NO off-machine backup — one disk failure loses everything" \
        "Set one up (one command): bash scripts/vault-backup.sh setup --vault '$VAULT'. See docs/BACKUP.md." ;;
    *)
      warn "Could not evaluate backup status" "check-vault-backup.py returned: ${bverdict:-<empty>}" ;;
  esac
else
  warn "check-vault-backup.py not found" "Cannot verify the vault has an off-machine backup."
fi

# ----- 13. Obsidian renderer crashes (the large-vault OOM class) -----
section "13. Obsidian renderer crashes"
CHECK_RENDERER=""
for c in "$(cd "$(dirname "$0")" && pwd)/check-renderer-crashes.py" \
         "$HOME/.claude/skills/ai-brain-starter/scripts/check-renderer-crashes.py"; do
  [ -f "$c" ] && CHECK_RENDERER="$c" && break
done
if [ -n "$CHECK_RENDERER" ]; then
  rverdict="$(python3 "$CHECK_RENDERER" --porcelain 2>/dev/null)"
  case "$rverdict" in
    OK_NO_CRASHES)
      ok "No repeated Obsidian renderer crashes" ;;
    SKIP_NOT_MACOS)
      ok "Renderer-crash check skipped (macOS-only crash reports)" ;;
    RENDERER_CRASHES:*)
      warn "Repeated Obsidian renderer crashes (${rverdict#RENDERER_CRASHES:} in ~14 days, EXC_BREAKPOINT / renderer OOM)" \
        "A heavy indexer plugin is likely exhausting the renderer on a large vault. Quit Obsidian, set .obsidian/community-plugins.json to [] (restricted mode), reopen, enable Dataview only, then add others one at a time. Scope or drop Smart Connections / Tasks. See templates/rules/obsidian-plugins.md 'Large-vault plugin posture'." ;;
    *)
      warn "Could not evaluate renderer-crash history" "check-renderer-crashes.py returned: ${rverdict:-<empty>}" ;;
  esac
else
  warn "check-renderer-crashes.py not found" "Cannot check for repeated Obsidian renderer crashes."
fi

# ----- 14. Split Meta folders (the leaked-session class) -----
section "14. Split Meta folders"
CHECK_SPLIT=""
for c in "$(cd "$(dirname "$0")" && pwd)/check-split-meta.py" \
         "$HOME/.claude/skills/ai-brain-starter/scripts/check-split-meta.py"; do
  [ -f "$c" ] && CHECK_SPLIT="$c" && break
done
if [ -n "$CHECK_SPLIT" ]; then
  sverdict="$(python3 "$CHECK_SPLIT" --porcelain "$VAULT" 2>/dev/null)"
  case "$sverdict" in
    OK_NO_META|OK_SINGLE_META|OK_PARTITIONED)
      ok "Meta folders are not split (session + traffic data is where it belongs)" ;;
    SPLIT_META:*)
      bad "Session/traffic data leaked into a plain 'Meta/' (${sverdict#SPLIT_META:} item(s)), not '⚙️ Meta/'" \
        "Update ai-brain-starter (the resolver fix stops new leaks), then move the leaked items from 'Meta/' into '⚙️ Meta/' and merge 'Session Log.md'. See docs/CHANGELOG.md (2026-06-07 Meta entries)." ;;
    *)
      warn "Could not evaluate Meta-folder split" "check-split-meta.py returned: ${sverdict:-<empty>}" ;;
  esac
else
  warn "check-split-meta.py not found" "Cannot check for split Meta folders."
fi

# ----- 14b. Connector liveness (the silent-empty 0-vs-0 gap) -----
section "14b. Connector liveness"
CHECK_CONN=""
for c in "$(cd "$(dirname "$0")" && pwd)/check-connector-liveness.py" \
         "$HOME/.claude/skills/ai-brain-starter/scripts/check-connector-liveness.py"; do
  [ -f "$c" ] && CHECK_CONN="$c" && break
done
if [ -n "$CHECK_CONN" ]; then
  cverdict="$(python3 "$CHECK_CONN" --porcelain "$VAULT" 2>/dev/null)"
  case "${cverdict%%$'\n'*}" in
    OK_ALL_FRESH)
      ok "All ingest connectors are producing data within their cadence" ;;
    SKIP_NO_CONNECTORS)
      ok "No ingest connectors have landed data yet (nothing to watch)" ;;
    CONNECTOR_GAP:*)
      connlist="$(printf '%s\n' "$cverdict" | grep '^CONNECTOR_GAP:' | cut -d: -f2,3 | paste -sd ',' -)"
      warn "Connector(s) silently went empty (the 0-vs-0 gap): $connlist" \
        "A connector can exit 0 while returning 0 items after a vendor changes a surface. Check each source's auth/permissions, re-run its ingest skill, and confirm it pulls >0 items. Detail: python3 scripts/check-connector-liveness.py \"$VAULT\"." ;;
    *)
      warn "Could not evaluate connector liveness" "check-connector-liveness.py returned: ${cverdict:-<empty>}" ;;
  esac
else
  warn "check-connector-liveness.py not found" "Cannot check connectors for the silent-empty gap."
fi

# ----- 15. First-run context load (the wrong-cwd / generic-answer class) -----
# The install's "ask me what you know about you" test only works if the vault's
# personalized CLAUDE.md actually loads — which depends on launching from the
# right folder and on CLAUDE.md being filled in, not the bare template. This
# simulates Claude Code's CLAUDE.md ancestor-walk and proves the load instead of
# only checking that files exist (sections 1-2 check existence; this checks LOAD).
section "15. First-run context load"
CHECK_CTX=""
for c in "$(cd "$(dirname "$0")" && pwd)/check-context-load.py" \
         "$HOME/.claude/skills/ai-brain-starter/scripts/check-context-load.py"; do
  [ -f "$c" ] && CHECK_CTX="$c" && break
done
if [ -n "$CHECK_CTX" ]; then
  cverdict="$(python3 "$CHECK_CTX" "$VAULT" --porcelain 2>/dev/null)"
  case "$cverdict" in
    OK_WILL_LOAD)
      ok "Personalized context will load — launch with: cd \"$VAULT\" && claude" ;;
    FAIL_NO_CLAUDE_MD)
      bad "No CLAUDE.md at the vault root — first run answers generically" \
        "Re-run /setup-brain Phase 4 to build it." ;;
    FAIL_TEMPLATE_UNFILLED:*)
      bad "CLAUDE.md is the unfilled template or a stub (${cverdict#FAIL_TEMPLATE_UNFILLED:})" \
        "The first-run 'what do you know about me' answer will be generic. Re-run /setup-brain Phase 4 to fill it in." ;;
    WARN_MISSING_CONTEXT:*)
      warn "CLAUDE.md references session-start files the vault is missing (${cverdict#WARN_MISSING_CONTEXT:})" \
        "Run: python3 \"$CHECK_CTX\" \"$VAULT\" to see which. Create them or fix the references." ;;
    *)
      warn "Could not evaluate first-run context load" "check-context-load.py returned: ${cverdict:-<empty>}" ;;
  esac
else
  warn "check-context-load.py not found" "Cannot verify the vault's context will load on first run."
fi

# ----- 16. SessionStart hook boundedness (the effective wired set) -----
# Section 11 + the CI gate prove the canonical hooks.json TEMPLATE is bounded.
# This checks the EFFECTIVE wired set in THIS machine's settings.json — the
# surface where the 2026-06-05 freeze actually happened: a per-session hook that
# corpus-walks becomes N concurrent walks under N sessions. MYC-1113.
section "16. SessionStart hook boundedness"
AUDIT_SS=""
for c in "$(cd "$(dirname "$0")" && pwd)/audit-sessionstart-boundedness.py" \
         "$HOME/.claude/skills/ai-brain-starter/scripts/audit-sessionstart-boundedness.py"; do
  [ -f "$c" ] && AUDIT_SS="$c" && break
done
SS_SETTINGS="$HOME/.claude/settings.json"
if [ -z "$AUDIT_SS" ]; then
  warn "audit-sessionstart-boundedness.py not found" "Cannot verify the effective SessionStart fleet is bounded."
elif [ ! -f "$SS_SETTINGS" ]; then
  ok "No ~/.claude/settings.json yet (no SessionStart fleet wired)"
else
  ssverdict="$(python3 "$AUDIT_SS" --settings "$SS_SETTINGS" --porcelain 2>/dev/null)"
  case "$ssverdict" in
    OK:*)
      ok "Effective SessionStart fleet is bounded (${ssverdict#OK:} clean/declared/unevaluated)" ;;
    PARTIAL:*)
      # MYC-3879. The auditor resolves each wired command to a file and reads it;
      # anything it cannot resolve is SKIPPED. This branch used to be folded into
      # OK:, so a real machine printed "6 resolved - 6 clean, 0 unguarded; 19
      # unresolved" and diagnose showed a green line. Nineteen SessionStart
      # commands unread is not a bounded fleet - it is an unmeasured one, and a
      # freeze-class hook sitting in any of them was structurally invisible.
      _rest="${ssverdict#PARTIAL:}"; _skipped="${_rest##*:}"
      warn "$_skipped SessionStart command(s) could not be evaluated for boundedness" \
        "The auditor never opened them, so nothing is known about their work shape - this is NOT a clean bill of health. Run: python3 \"$AUDIT_SS\" --settings \"$SS_SETTINGS\" to see each skipped command and why (unresolvable command string, script not on disk, or unreadable source). Common cause on Windows: hook commands are wired in the 'py -3 \"<runner>\" --fallback silent \"<hook>\"' form, whose backslash paths the resolver does not match." ;;
    UNGUARDED:*)
      _rest="${ssverdict#UNGUARDED:}"; _n="${_rest%%:*}"; _hooks="${_rest#*:}"
      bad "$_n SessionStart hook(s) do an unguarded corpus walk: $_hooks" \
        "Under N concurrent sessions each becomes N corpus walks (the 2026-06-05 freeze class). Run: python3 \"$AUDIT_SS\" --settings \"$SS_SETTINGS\" . Add a single-instance flock + cooldown stamped-at-START + wall-clock deadline, or a '# sessionstart-walk-bounded: <reason>' exemption. See docs/HOOK_FLEET_RESOURCE_GOVERNANCE.md." ;;
    ERROR:*)
      warn "Could not audit SessionStart boundedness" "settings.json missing or unparseable (${ssverdict#ERROR:})" ;;
    *)
      warn "Could not evaluate SessionStart boundedness" "audit returned: ${ssverdict:-<empty>}" ;;
  esac
fi

# ----- 17. Worktree-on-vault melt risk (the Desktop per-session checkout class) -----
# A git worktree under .claude/worktrees/ is cheap+correct on a CODE repo, but on
# an OBSIDIAN VAULT (repo-root == vault-root) it drops a full second checkout
# INSIDE Obsidian's watched tree -> renderer OOM/crash (the 2026-06-06 melt), and
# the worktree can be silently deleted mid-session. Relocation is dead (a symlink
# out is followed back IN by Obsidian's watcher; a WorktreeCreate redirect is not
# honored), and the flag does NOT gate Desktop worktree creation. The runtime hook
# warn-vault-session-in-worktree.py is the live tripwire; this is the at-rest scan.
section "17. Worktree-on-vault melt risk"
CHECK_WT=""
for c in "$(cd "$(dirname "$0")" && pwd)/check-worktree-on-vault.py" \
         "$HOME/.claude/skills/ai-brain-starter/scripts/check-worktree-on-vault.py"; do
  [ -f "$c" ] && CHECK_WT="$c" && break
done
if [ -n "$CHECK_WT" ]; then
  wverdict="$(python3 "$CHECK_WT" --porcelain "$VAULT" 2>/dev/null)"
  case "$wverdict" in
    OK_NOT_VAULT|OK_NOT_GIT|OK_NO_WORKTREES)
      ok "No git worktree inside the Obsidian-watched vault tree" ;;
    WORKTREE_ON_VAULT:*)
      warn "${wverdict#WORKTREE_ON_VAULT:} git worktree checkout(s) live INSIDE the vault (.claude/worktrees/)" \
        "The Desktop per-session worktree checkbox dropped a full-vault checkout inside Obsidian's watched tree -> renderer OOM/crash, and it can be silently deleted mid-session. Relocation is DEAD (a symlink out is followed back in; a WorktreeCreate redirect is not honored) and the flag does NOT gate it. FIX: launch the vault PLAIN with the worktree box UNCHECKED: cd \"$VAULT\" && claude. See docs/VAULT_WORKTREE_MELT.md." ;;
    *)
      warn "Could not evaluate worktree-on-vault melt risk" "check-worktree-on-vault.py returned: ${wverdict:-<empty>}" ;;
  esac
else
  warn "check-worktree-on-vault.py not found" "Cannot check whether a git worktree is melting the vault tree."
fi

# ----- summary -----
echo
echo "${B}Summary${N}"
echo "  ${G}OK:   $GREEN${N}"
echo "  ${Y}WARN: $YELLOW${N}"
echo "  ${R}FAIL: $RED${N}"
echo

if [ "$RED" -gt 0 ]; then
  echo "${R}Something is broken.${N} Fix the FAILs above, then re-run."
  exit 1
elif [ "$YELLOW" -gt 0 ]; then
  echo "${Y}Working, with caveats.${N} Address WARNs when convenient."
  exit 2
else
  echo "${G}All green. Your second brain is healthy.${N}"
  exit 0
fi
