#!/bin/bash
# exit-contract: ENFORCING

# vault-repo-drift-check.sh — Detect when the vault has things the repo doesn't
#
# Runs monthly. Compares:
# 1. Vault rules/ vs repo templates/rules/
# 2. Vault scripts/ vs repo scripts/
# 3. Vault skills vs repo skills/
# 4. Vault hooks vs repo hooks/
# 5. Vault CLAUDE.md skill routing vs repo SKILL.md Phase 9
#
# Output: a list of files/features that exist in the vault but not the repo.
# For the maintainer: these are candidates for upstream propagation.
# For other users: these are features they're missing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- negative control -------------------------------------------------------
# The defect this covers: DRIFT_FOUND was computed on every branch and then
# thrown away, because the script simply ended. A fully drifted vault exited 0
# and the only non-zero in the file was the unset-VAULT_ROOT usage error, so a
# scheduled runner could not tell "in sync" from "five drifted files".
# Re-invokes this same script against fixture vaults, so the control cannot
# pass while the real exit stays 0.
if [ "${1:-}" = "--self-test" ]; then
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  fails=0

  # BITE: a rule file present in the vault and absent from the repo is drift.
  mkdir -p "$tmp/drifted/⚙️ Meta/rules"
  echo "# a rule this repo does not ship" \
    > "$tmp/drifted/⚙️ Meta/rules/zz-not-in-repo-$$.md"
  # HOME is sandboxed for both probes: the Skills and Plugins sections scan
  # ~/.claude/, so with the developer's real HOME the verdict depends on which
  # skills happen to be installed on this machine and the inverse case can
  # never be clean. A control whose result varies by machine is not a control.
  mkdir -p "$tmp/home/.claude/skills"
  rc=0
  HOME="$tmp/home" VAULT_ROOT="$tmp/drifted" bash "$0" >/dev/null 2>&1 || rc=$?
  if [ "$rc" -ne 1 ]; then
    echo "FAIL: a vault-only rule file exited $rc, expected 1. DRIFT_FOUND is"
    echo "      not reaching the exit."
    fails=1
  fi

  # INVERSE: an empty ⚙️ Meta tree has nothing to drift and must exit 0.
  # Without this, a script that exited 1 unconditionally would satisfy the
  # case above and the control would prove nothing.
  mkdir -p "$tmp/clean/⚙️ Meta/rules"
  rc=0
  HOME="$tmp/home" VAULT_ROOT="$tmp/clean" bash "$0" >/dev/null 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "FAIL: a vault with nothing vault-only exited $rc, expected 0. The"
    echo "      BITE case above may be passing for the wrong reason."
    fails=1
  fi

  if [ "$fails" -ne 0 ]; then exit 1; fi
  echo "OK - self-test: a vault-only rule file exits 1, an empty vault exits 0."
  exit 0
fi

# Detect vault root (look for ⚙️ Meta/ folder)
if [ -n "${VAULT_ROOT:-}" ] && [ -d "$VAULT_ROOT" ]; then
  VAULT="$VAULT_ROOT"
else
  echo "ERROR: Set VAULT_ROOT to your vault path" >&2
  exit 1
fi

META="$VAULT/⚙️ Meta"
DRIFT_FOUND=0


# Load .driftignore patterns (one substring per line, # comments allowed).
# A drift line is suppressed if any pattern is a substring of the emitted path.
IGNORE_FILE="$REPO_ROOT/.driftignore"
IGNORE_PATTERNS=()
if [ -f "$IGNORE_FILE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    # Strip comments and whitespace.
    line="${line%%#*}"
    line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [ -n "$line" ] && IGNORE_PATTERNS+=("$line")
  done < "$IGNORE_FILE"
fi

# Returns 0 (true) if the path matches any ignore pattern.
is_ignored() {
  local path="$1"
  # `${arr[@]}` on an EMPTY array is an unbound-variable error under `set -u`
  # in bash 3.2 (the only bash on a stock Mac) and in bash < 4.4. With no
  # .driftignore the array is never populated, so this function aborted the
  # whole script at the first skill it examined -- output simply stopped after
  # "--- Skills ---" and the script exited 1. That was survivable while the
  # exit code meant nothing; now that 1 means "drift found", a crash and a
  # finding would be the same signal, which is the defect this file was just
  # fixed for. The `+` expansion yields nothing at all when unset.
  for pat in ${IGNORE_PATTERNS[@]+"${IGNORE_PATTERNS[@]}"}; do
    case "$path" in
      *"$pat"*) return 0 ;;
    esac
  done
  return 1
}

echo "=== Vault-to-Repo Drift Check ==="
echo "Vault: $VAULT"
echo "Repo:  $REPO_ROOT"
if [ ${#IGNORE_PATTERNS[@]} -gt 0 ]; then
  echo "Ignoring ${#IGNORE_PATTERNS[@]} pattern(s) from .driftignore"
fi
echo ""

# 1. Rules files
echo "--- Rules ---"
for rule in "$META/rules/"*.md; do
  [ -f "$rule" ] || continue
  base=$(basename "$rule")
  rel="rules/$base"
  is_ignored "$rel" && continue
  if [ ! -f "$REPO_ROOT/templates/rules/$base" ]; then
    echo "  DRIFT: $rel exists in vault but not in repo"
    DRIFT_FOUND=1
  fi
done

# 2. Scripts
echo "--- Scripts ---"
for script in "$META/scripts/"*.{sh,py}; do
  [ -f "$script" ] || continue
  base=$(basename "$script")
  [ "$base" = "__pycache__" ] && continue
  rel="scripts/$base"
  is_ignored "$rel" && continue
  if [ ! -f "$REPO_ROOT/scripts/$base" ]; then
    echo "  DRIFT: $rel exists in vault but not in repo"
    DRIFT_FOUND=1
  fi
done

# 3. Skills
echo "--- Skills ---"
for skill_dir in ~/.claude/skills/*/; do
  [ -d "$skill_dir" ] || continue
  skill_name=$(basename "$skill_dir")
  # Skip the repo itself and external skills
  [ "$skill_name" = "ai-brain-starter" ] && continue
  [ "$skill_name" = "humanizer" ] && continue
  rel="skills/$skill_name"
  is_ignored "$rel" && continue
  if [ ! -d "$REPO_ROOT/skills/$skill_name" ]; then
    echo "  DRIFT: skill $skill_name installed but not in repo"
    DRIFT_FOUND=1
  fi
done

# 4. Hooks
echo "--- Hooks ---"
for hook in ~/.claude/hooks/*.sh; do
  [ -f "$hook" ] || continue
  base=$(basename "$hook")
  rel="hooks/$base"
  is_ignored "$rel" && continue
  if [ ! -f "$REPO_ROOT/hooks/$base" ]; then
    echo "  DRIFT: $rel exists locally but not in repo"
    DRIFT_FOUND=1
  fi
done

# 5. Obsidian plugins
echo "--- Obsidian Plugins ---"
if [ -d "$VAULT/.obsidian/plugins" ]; then
  for plugin_dir in "$VAULT/.obsidian/plugins/"*/; do
    [ -d "$plugin_dir" ] || continue
    plugin_name=$(basename "$plugin_dir")
    rel="obsidian-plugin:$plugin_name"
    is_ignored "$rel" && continue
    # Check if plugin is in the SKILL.md or any phases/*.md auto-install list.
    if ! grep -rq "\"$plugin_name\"" "$REPO_ROOT/SKILL.md" "$REPO_ROOT/phases/" 2>/dev/null; then
      echo "  DRIFT: Obsidian plugin $plugin_name installed but not in repo auto-install"
      DRIFT_FOUND=1
    fi
  done
fi

echo ""
if [ $DRIFT_FOUND -eq 0 ]; then
  echo "No drift detected. Vault and repo are in sync."
else
  echo "Drift detected. Review items above and propagate to the repo if they're universal."
fi

# DRIFT_FOUND was computed on every branch above and then thrown away: the file
# ended here, so a fully drifted vault exited 0 and the only non-zero in the
# script was the unset-VAULT_ROOT usage error. A scheduled runner could not tell
# "in sync" from "five drifted files". .driftignore remains the suppression
# channel; suppressed paths never set DRIFT_FOUND, so an intentional difference
# still exits 0.
exit "$DRIFT_FOUND"
