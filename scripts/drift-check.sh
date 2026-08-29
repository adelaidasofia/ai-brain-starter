#!/usr/bin/env bash
# exit-contract: ADVISORY -- its consumer contract is the parsed STATUS /
#   DRIFT_COUNT stdout block, not the exit code; a session-start walkthrough
#   resolves each row

#
# drift-check.sh — detect file drift between the ai-brain-starter repo and
# the user's installed copies. READ-ONLY: never modifies anything.
#
# Why this exists:
#   update-check.sh only knows whether the user is BEHIND on commits. It does
#   NOT know whether files that were already installed in a prior release have
#   since drifted from the repo's version (because someone hand-edited a script
#   in the vault, or a previous sync only partially landed, or the user pulled
#   a sub-set of changes manually). Without drift-check, the only way to find
#   stale files is for a human to ask Claude "compare everything" — which is
#   exactly what we're automating away.
#
# What it checks:
#
#   Scope A — installed Claude skills:
#     For every skill bundled under $STARTER/skills/<skill>/, walk every file
#     and compare to ~/.claude/skills/<skill>/<rel-path>. Report any installed
#     file that differs from the repo version.
#
#   Scope B — vault-installed scripts (only if --vault PATH given):
#     For a curated list of scripts the starter copies into vaults during
#     /setup-brain (aggregate-sessions.py, aggregate-decisions.py,
#     graph-context-hook.sh, etc.), compare $VAULT/⚙️ Meta/scripts/<basename>
#     against $STARTER/scripts/<basename>. Annotate scripts that are known to
#     have hand-edited config blocks so the caller doesn't blindly overwrite.
#
#   Scope C — vault CLAUDE.md rule blocks (only if --vault given):
#     For every templates/rules/*.md, find its top-level heading inside
#     $VAULT/CLAUDE.md. If the heading is present but the block underneath
#     differs from the repo template, report drift. The block boundary is the
#     next top-level (#) heading or end-of-file.
#
#   Scope D — standalone vault rule files (only if --vault given):
#     For every templates/rules/*.md, check if a matching file exists at
#     $VAULT/⚙️ Meta/rules/<basename>. If it does and differs from the repo
#     version, report drift. This scope covers modular rule files that were
#     extracted from CLAUDE.md into standalone files (2026-04-12 split).
#
# What it does NOT do:
#   - Modify files. Ever. drift-check is a detector, not a fixer.
#   - Decide what to update. The Claude session-start rule reads this output
#     and walks the user through each drift one by one — show diff, ask, back
#     up, then replace (or skip).
#
# Output format (stable, parseable):
#   STATUS: <OK | DRIFT | SKIPPED_TODAY | ERROR>
#   DRIFT_COUNT: <integer>
#   ---DRIFT_FILES---
#   <scope>|<installed_path>|<repo_source_path>|<note>
#   ...
#   ---END---
#
#   Scopes: skill | vault-script | vault-rule | vault-rule-file
#   Note: free-text annotation, may be empty. Used to flag hand-edited files
#         (e.g. graph-context-hook.sh) that need cherry-pick instead of
#         wholesale overwrite.
#

# Cooldown:
#   drift-check honors a once-per-day cooldown (mirrors update-check.sh) so
#   that session start isn't noisy. Pass --force to bypass.
#
# Usage:
#   bash drift-check.sh                        # skills scope only
#   bash drift-check.sh --vault "/path/to/vault"   # all three scopes
#   bash drift-check.sh --vault "..." --force      # bypass cooldown

set -uo pipefail

# Derive STARTER_DIR from the script's own location (scripts/drift-check.sh →
# parent dir is the repo root). This works whether the repo is installed at
# ~/.claude/skills/ai-brain-starter (end users) or somewhere else like
# ~/Desktop/ai-brain-starter (maintainer / fork developer).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STARTER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_DIR="$HOME/.claude/skills"
COOLDOWN_FILE="$HOME/.claude/.ai-brain-starter-drift-check-last-run"
IGNORE_FILE="$HOME/.claude/.ai-brain-starter-drift-check-ignore"
TODAY="$(date +%Y-%m-%d)"
FORCE=0
SELF_TEST=0
VAULT=""

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vault)
      VAULT="${2:-}"
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --self-test)
      SELF_TEST=1
      shift
      ;;
    *)
      shift
      ;;
  esac
done

# --- negative control -------------------------------------------------------
# The defect this covers: STATUS was the literal string OK on every run, so a
# fully drifted install and a clean one emitted the identical token. The exit
# code is 0 either way by design (a session-start consumer parses this block
# and must not be blocked), which made STATUS the ONLY channel that could carry
# the finding -- and it carried nothing.
#
# Asserts on the emitted TOKEN, not on the exit code, because the exit code is
# deliberately constant here. HOME and the vault are sandboxed so the verdict
# cannot depend on what happens to be installed on this machine.
if [ "$SELF_TEST" = "1" ]; then
  st_tmp="$(mktemp -d)"
  trap 'rm -rf "$st_tmp"' EXIT
  st_fail=0

  mkdir -p "$st_tmp/home/.claude/skills"
  mkdir -p "$st_tmp/vault/⚙️ Meta/rules"

  # BITE: a vault rule file that differs from the repo template is drift.
  st_rule="$(ls "$STARTER_DIR/templates/rules/"*.md 2>/dev/null | head -1)"
  if [ -z "$st_rule" ]; then
    echo "UNEVALUATED: no templates/rules/*.md to build a drift fixture from."
    echo "  The control could not run, which is not a pass."
    exit 2
  fi
  cp "$st_rule" "$st_tmp/vault/⚙️ Meta/rules/$(basename "$st_rule")"
  printf '\n<!-- local edit that makes this differ from the repo -->\n' \
    >> "$st_tmp/vault/⚙️ Meta/rules/$(basename "$st_rule")"

  st_out="$(HOME="$st_tmp/home" bash "$0" --vault "$st_tmp/vault" --force 2>&1)"
  st_status="$(printf '%s\n' "$st_out" | grep '^STATUS:' | head -1)"
  st_count="$(printf '%s\n' "$st_out" | grep '^DRIFT_COUNT:' | head -1 | tr -cd '0-9')"
  if [ "${st_count:-0}" -gt 0 ] && [ "$st_status" != "STATUS: DRIFT" ]; then
    echo "FAIL: DRIFT_COUNT is ${st_count:-0} but STATUS is '$st_status'."
    echo "      A standing alarm must not emit the same token as an all-clear."
    st_fail=1
  elif [ "${st_count:-0}" -eq 0 ]; then
    echo "UNEVALUATED: the drift fixture produced DRIFT_COUNT 0, so the BITE"
    echo "  case never exercised the branch it names."
    exit 2
  fi

  # INVERSE: an empty vault has no drift and must still say OK. Without this a
  # script that printed DRIFT unconditionally would satisfy the case above.
  mkdir -p "$st_tmp/clean/⚙️ Meta/rules"
  st_out2="$(HOME="$st_tmp/home" bash "$0" --vault "$st_tmp/clean" --force 2>&1)"
  st_status2="$(printf '%s\n' "$st_out2" | grep '^STATUS:' | head -1)"
  if [ "$st_status2" != "STATUS: OK" ]; then
    echo "FAIL: a vault with no drift emitted '$st_status2', expected STATUS: OK."
    echo "      The BITE case above may be passing for the wrong reason."
    st_fail=1
  fi

  if [ "$st_fail" -ne 0 ]; then exit 1; fi
  echo "OK - self-test: a drifted vault emits STATUS: DRIFT, a clean one emits"
  echo "     STATUS: OK (so the control is not vacuous)."
  exit 0
fi

# Cooldown
if [[ $FORCE -eq 0 && -f "$COOLDOWN_FILE" ]]; then
  LAST="$(cat "$COOLDOWN_FILE" 2>/dev/null || echo '')"
  if [[ "$LAST" == "$TODAY" ]]; then
    echo "STATUS: SKIPPED_TODAY"
    exit 0
  fi
fi

# Repo guard
if [[ ! -d "$STARTER_DIR" ]]; then
  echo "STATUS: ERROR"
  echo "REASON: ai-brain-starter not installed at $STARTER_DIR"
  exit 0
fi

# Bash 3.2 — initialize as empty string and append; avoid array unbound issues
DRIFT_LINES=""
DRIFT_COUNT=0


# ── Ignore registry ──────────────────────────────────────────────────────
# Per-user file at ~/.claude/.ai-brain-starter-drift-check-ignore. Each line
# is either a literal installed path OR a shell-glob pattern matching one.
# `#` starts a comment, blank lines are ignored. Trailing whitespace stripped.
#
# Use case: a file like graph-context-hook.sh ships as a generic template in
# the repo and gets hand-customized in the vault during /setup-brain. Drift
# is permanent-by-design — listing it in the ignore file tells drift-check
# "I customized this on purpose, don't tell me about it again."
#
# Editing: open the file in a text editor and add a line. Or, during the
# drift walkthrough in session-start-update-check.md, the user can pick
# "skip permanently" and Claude appends the path here for them.
IGNORE_PATTERNS=""
if [[ -f "$IGNORE_FILE" ]]; then
  IGNORE_PATTERNS="$(grep -v '^[[:space:]]*\(#\|$\)' "$IGNORE_FILE" 2>/dev/null || true)"
fi

is_ignored() {
  local path="$1"
  [[ -z "$IGNORE_PATTERNS" ]] && return 1
  while IFS= read -r pat; do
    pat="${pat#"${pat%%[![:space:]]*}"}"  # strip leading whitespace
    pat="${pat%"${pat##*[![:space:]]}"}"  # strip trailing whitespace
    [[ -z "$pat" ]] && continue
    # shellcheck disable=SC2254  # intentional unquoted glob match
    case "$path" in
      $pat) return 0 ;;
    esac
  done <<< "$IGNORE_PATTERNS"
  return 1
}

add_drift() {
  # $1=scope $2=installed_path $3=repo_source_path $4=note
  if is_ignored "$2"; then
    return 0
  fi
  DRIFT_LINES="${DRIFT_LINES}${1}|${2}|${3}|${4}"$'\n'
  DRIFT_COUNT=$((DRIFT_COUNT + 1))
}

# ── Scope A — installed skills ────────────────────────────────────────────
if [[ -d "$STARTER_DIR/skills" ]]; then
  for skill_dir in "$STARTER_DIR/skills"/*/; do
    [[ -d "$skill_dir" ]] || continue
    skill_name=$(basename "$skill_dir")
    installed_skill_dir="$INSTALL_DIR/$skill_name"

    # If the skill isn't installed at all, that's not "drift" — it's a missing
    # install, which bootstrap.sh handles. Skip.
    [[ -d "$installed_skill_dir" ]] || continue

    while IFS= read -r -d '' src_file; do
      rel_path="${src_file#"$skill_dir"}"
      dest_file="$installed_skill_dir/$rel_path"
      if [[ -f "$dest_file" ]] && ! cmp -s "$src_file" "$dest_file"; then
        add_drift "skill" "$dest_file" "$src_file" ""
      fi
    done < <(find "$skill_dir" -type f -print0 2>/dev/null)
  done
fi

# ── Scope B — vault-installed scripts ─────────────────────────────────────
if [[ -n "$VAULT" && -d "$VAULT" ]]; then
  VAULT_SCRIPTS_DIR="$VAULT/⚙️ Meta/scripts"
  if [[ -d "$VAULT_SCRIPTS_DIR" ]]; then
    # Curated list of scripts that the starter installs into vaults via
    # /setup-brain. Anything outside this list is user-installed and should
    # not be touched by drift-check.
    VAULT_SCRIPT_NAMES="aggregate-sessions.py aggregate-decisions.py auto-wikilink.py build-journal-index.py graphify_dedupe_by_adjacency.py graph-context-hook.sh"

    for name in $VAULT_SCRIPT_NAMES; do
      src="$STARTER_DIR/scripts/$name"
      dest="$VAULT_SCRIPTS_DIR/$name"
      [[ -f "$src" && -f "$dest" ]] || continue
      if ! cmp -s "$src" "$dest"; then
        note=""
        # Files known to be hand-edited after install (CONFIG blocks, paths,
        # vault-specific values). For these, the caller MUST cherry-pick
        # repo changes into the user's file rather than overwriting.
        case "$name" in
          graph-context-hook.sh)
            note="hand-edited CONFIG block at top of file — cherry-pick changes, do NOT overwrite wholesale"
            ;;
        esac
        add_drift "vault-script" "$dest" "$src" "$note"
      fi
    done
  fi
fi

# ── Scope C — vault CLAUDE.md rule blocks ─────────────────────────────────
if [[ -n "$VAULT" && -f "$VAULT/CLAUDE.md" && -d "$STARTER_DIR/templates/rules" ]]; then
  for rule_file in "$STARTER_DIR/templates/rules"/*.md; do
    [[ -f "$rule_file" ]] || continue

    # Get the first H1 heading line from the template (e.g. "# Session start...")
    heading=$(grep -m1 '^# ' "$rule_file" 2>/dev/null || echo '')
    [[ -n "$heading" ]] || continue

    # Check whether that heading appears anywhere in the user's CLAUDE.md
    if ! grep -qxF "$heading" "$VAULT/CLAUDE.md" 2>/dev/null; then
      # Heading not present. The rule isn't installed (or has been removed by
      # the user). Don't report drift — bootstrap/setup-brain owns install.
      continue
    fi

    # Heading IS present. Extract the block from CLAUDE.md and compare to the
    # template content. Use python for clean block extraction + comparison.
    drift_result=$(python3 - "$VAULT/CLAUDE.md" "$rule_file" "$heading" <<'PY'
import sys
from pathlib import Path

vault_claude_md = Path(sys.argv[1])
template_path = Path(sys.argv[2])
heading = sys.argv[3]

claude_lines = vault_claude_md.read_text(encoding="utf-8").splitlines()
template_text = template_path.read_text(encoding="utf-8")

# Extract the installed block: from `heading` line up to (but not including)
# the next H1 heading, or end of file.
installed_block_lines = []
in_block = False
for line in claude_lines:
    if line == heading:
        in_block = True
        installed_block_lines.append(line)
        continue
    if in_block and line.startswith("# ") and line != heading:
        break
    if in_block:
        installed_block_lines.append(line)

# Defensive normalization to avoid false-positive drift on benign formatting
# differences. The diff that matters is content drift, not whitespace or
# editor conventions. Normalize both sides through the same pipeline:
#   1. Strip UTF-8 BOM (Windows editors love adding these)
#   2. Convert CRLF to LF (Windows editors save \r\n; the repo uses \n)
#   3. Strip trailing blank lines
#   4. Strip trailing markdown thematic breaks (---, ***, ___) — these are
#      CLAUDE.md visual separators between concatenated rule blocks, not
#      part of any individual rule template
#   5. Re-add a single trailing newline for consistency

def normalize_for_compare(text):
    # 1. BOM
    if text.startswith("\ufeff"):
        text = text[1:]
    # 2. Line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 3 & 4. Trailing blank lines and thematic breaks
    lines = text.split("\n")
    THEMATIC_BREAKS = {"---", "***", "___"}
    while lines and (lines[-1].strip() == "" or lines[-1].strip() in THEMATIC_BREAKS):
        lines.pop()
    # 5. Single trailing newline
    return "\n".join(lines) + "\n"

installed = normalize_for_compare("\n".join(installed_block_lines))
template = normalize_for_compare(template_text)

if installed != template:
    print("DRIFT")
PY
)

    if [[ "$drift_result" == "DRIFT" ]]; then
      add_drift "vault-rule" "$VAULT/CLAUDE.md" "$rule_file" "block heading: $heading"
    fi
  done
fi

# ── Scope D — standalone vault rule files ─────────────────────────────────
# Modular rule files live at $VAULT/⚙️ Meta/rules/*.md. They are standalone
# copies of templates/rules/*.md (not inlined into CLAUDE.md). Compare each
# installed file against its repo counterpart. This scope was added when
# CLAUDE.md was split into modular files to stay under the 10K-token read
# limit (2026-04-12).
if [[ -n "$VAULT" && -d "$VAULT/⚙️ Meta/rules" && -d "$STARTER_DIR/templates/rules" ]]; then
  for rule_file in "$STARTER_DIR/templates/rules"/*.md; do
    [[ -f "$rule_file" ]] || continue
    basename_rule=$(basename "$rule_file")
    dest="$VAULT/⚙️ Meta/rules/$basename_rule"

    # Only check files that are actually installed as standalone files.
    # If the file doesn't exist in the vault's rules/ dir, it might be
    # an old inline-only template (Scope C handles those). Skip.
    [[ -f "$dest" ]] || continue

    if ! cmp -s "$rule_file" "$dest"; then
      add_drift "vault-rule-file" "$dest" "$rule_file" ""
    fi
  done
fi

# ── Output ─────────────────────────────────────────────────────────────────
# STATUS used to be the literal string OK on every run, so a fully drifted
# install and a clean one emitted the identical token -- the documented
# vocabulary at the top of this file had no value meaning "drift", and any
# consumer keying on STATUS alone read a standing alarm as an all-clear. The
# exit code stays 0 on purpose (see the exit-contract marker: the consumer
# parses this block, and a session-start hook must not block a session), so
# the STATUS token is the ONLY channel that can carry the finding.
if [ "$DRIFT_COUNT" -gt 0 ]; then
  echo "STATUS: DRIFT"
else
  echo "STATUS: OK"
fi
echo "DRIFT_COUNT: $DRIFT_COUNT"
echo "---DRIFT_FILES---"
if [[ -n "$DRIFT_LINES" ]]; then
  printf "%s" "$DRIFT_LINES"
fi
echo "---END---"

# Record cooldown
echo "$TODAY" > "$COOLDOWN_FILE"

exit 0
