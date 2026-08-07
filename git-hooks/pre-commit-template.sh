#!/usr/bin/env bash
# pre-commit-template.sh
#
# A public-repo personal-data scrub gate, shipped as a template.
# Reads per-user token lists from config files so this script itself
# carries no personal data.
#
# INSTALL (per user, once per machine):
#
# 1. Copy this file to a global hooks path:
#      mkdir -p ~/.git-hooks/
#      cp pre-commit-template.sh ~/.git-hooks/pre-commit
#      chmod +x ~/.git-hooks/pre-commit
#      git config --global core.hooksPath ~/.git-hooks
#    ...and ship the strand guard alongside it (chained by this hook):
#      cp guard-session-artifacts-on-default-branch.sh ~/.git-hooks/
#      chmod +x ~/.git-hooks/guard-session-artifacts-on-default-branch.sh
#
# 2. Create your personal token list (the strings the hook should refuse to publish):
#      cp scrub-personal-tokens.txt.example ~/.scrub-personal-tokens.txt
#      Edit ~/.scrub-personal-tokens.txt with your real tokens (one regex per line)
#
# 3. Create your public-repo allowlist (only these repos get the scrub):
#      cp scrub-public-repos.txt.example ~/.scrub-public-repos.txt
#      Edit ~/.scrub-public-repos.txt (one substring per line)
#
# 4. Test in a public repo:
#      echo "your name" >> README.md && git add README.md
#      git commit -m "should be blocked"   # expect: hook refuses
#      git checkout README.md && git reset HEAD README.md
#
# BYPASS:
#   SCRUB_BYPASS=1 git commit -m "..."     # operator-verified false positive
#   SCRUB_PRIVATE=1 git commit -m "..."    # force private (skip even if URL matches)
#   SCRUB_PUBLIC=1 git commit -m "..."     # force public (run even if URL doesn't match)

set -euo pipefail

# --- Off-default-branch session-artifact strand guard (runs for every repo) ---
# Refuses committing session-close artifacts while the checkout is parked off the
# default branch, so they can't silently strand on a topic branch. Independent of
# the scrub gate below; shipped alongside this hook (copied into ~/.git-hooks/).
_HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -x "${_HOOK_DIR}/guard-session-artifacts-on-default-branch.sh" ]]; then
  "${_HOOK_DIR}/guard-session-artifacts-on-default-branch.sh" || exit 1
fi

PERSONAL_TOKENS_FILE="${HOME}/.scrub-personal-tokens.txt"
PUBLIC_REPOS_FILE="${HOME}/.scrub-public-repos.txt"

if [[ -n "${SCRUB_BYPASS:-}" ]]; then
  echo "[scrub-or-die] SCRUB_BYPASS=1 -- skipping personal-data check" >&2
  exit 0
fi

if [[ -n "${SCRUB_PRIVATE:-}" ]]; then
  exit 0
fi

# Public-repo detection
if [[ -z "${SCRUB_PUBLIC:-}" ]]; then
  if [[ ! -f "$PUBLIC_REPOS_FILE" ]]; then
    exit 0
  fi
  REMOTE_URL=$(git config --get remote.origin.url 2>/dev/null || echo "")
  if [[ -z "$REMOTE_URL" ]]; then
    exit 0
  fi
  IS_PUBLIC=0
  while IFS= read -r line; do
    [[ -z "$line" || "$line" =~ ^# ]] && continue
    if [[ "$REMOTE_URL" == *"$line"* ]]; then
      IS_PUBLIC=1
      break
    fi
  done < "$PUBLIC_REPOS_FILE"
  if [[ "$IS_PUBLIC" -eq 0 ]]; then
    exit 0
  fi
fi

if [[ ! -f "$PERSONAL_TOKENS_FILE" ]]; then
  echo "[scrub-or-die] no personal tokens at $PERSONAL_TOKENS_FILE - cannot scrub. Refusing to commit to a public repo without a token list. Either create the file or set SCRUB_BYPASS=1." >&2
  exit 2
fi

echo "[scrub-or-die] public repo detected -- running personal-data scrub on staged diff" >&2

if git diff --cached --quiet 2>/dev/null; then
  DIFF_SOURCE="git diff"
  DIFF=$(git diff 2>/dev/null || true)
else
  DIFF_SOURCE="git diff --cached"
  DIFF=$(git diff --cached 2>/dev/null || true)
fi

if [[ -z "$DIFF" ]]; then
  echo "[scrub-or-die] no diff to scrub. OK." >&2
  exit 0
fi

ADDED_LINES=$(echo "$DIFF" | grep -E '^\+[^+]' || true)

if [[ -z "$ADDED_LINES" ]]; then
  echo "[scrub-or-die] no added lines in diff. OK." >&2
  exit 0
fi

MATCHES=""
while IFS= read -r pattern; do
  [[ -z "$pattern" || "$pattern" =~ ^# ]] && continue
  M=$(echo "$ADDED_LINES" | grep -E "$pattern" || true)
  if [[ -n "$M" ]]; then
    MATCHES+="--- pattern: $pattern ---"$'\n'"$M"$'\n\n'
  fi
done < "$PERSONAL_TOKENS_FILE"

if [[ -n "$MATCHES" ]]; then
  echo "[scrub-or-die] PERSONAL DATA DETECTED in $DIFF_SOURCE:" >&2
  echo "" >&2
  echo "$MATCHES" >&2
  echo "" >&2
  echo "Refusing to proceed. Either:" >&2
  echo "  1. Edit the file(s) to genericize the leaked references, then re-stage" >&2
  echo "  2. If genuine false positive: SCRUB_BYPASS=1 git commit ..." >&2
  exit 2
fi

echo "[scrub-or-die] no personal-data matches in $DIFF_SOURCE. OK to commit." >&2
exit 0
