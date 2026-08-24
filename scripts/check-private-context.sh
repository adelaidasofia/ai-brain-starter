#!/usr/bin/env bash
# Private-context token scan, PR-scoped.
#
# Lives here rather than inline in .github/workflows/lint.yml for the same
# reason check-ps1-encoding.sh does: an inline copy is unreachable locally, so
# it can only be exercised by pushing, and the enforcing gate and any local
# equivalent drift apart silently.
#
# Contract: fail (exit 1) if THIS BRANCH ADDS a line carrying a private-context
# token. Pre-existing committed content is intentionally ignored -- the gate is
# about net-new additions, not legacy text in a file the branch happens to touch.
#
# THE TRAP THIS SCRIPT EXISTS TO NOT FALL INTO: `git diff A..B` (two dots)
# compares the two TIPS. Every line the base changed since the branch forked
# comes back as a `+` on the branch side and gets blamed on the branch. Only
# `git diff A...B` (three dots) diffs from the MERGE BASE, which is the only
# thing that means "what this branch added".
#
# Measured 2026-08-23 on PR #519: two-dot reported 165 files and 10 token hits
# on a PR that touches 3 files and adds none. That false positive stranded ~15
# PRs for weeks -- the over-strict-verification failure mode that teaches people
# to bypass a security gate.
set -euo pipefail

P='\b[A]delaida\b|\b[O]nde\b|\b[D]iaz.Roa\b|\b[B]ogot[aá]\b|\b[S]ergio Perez\b|\b[N]atalia\b|\b[P]aola\b|\b[A]ccenture\b|\b[H]igh.Rise Series\b|\b[S]hark Tank\b'

scan() {
  local BASE="$1" fail=0 f added_lines matches merge_base

  # Fail LOUD if the merge base is unreachable. A shallow clone that cannot
  # reach the fork point would make `git diff BASE...HEAD` empty, and an empty
  # diff is indistinguishable from a clean one -- a silent fail-open on the
  # exact gate that must never fail open.
  if ! merge_base="$(git merge-base "$BASE" HEAD 2>/dev/null)" || [ -z "$merge_base" ]; then
    echo "::error::cannot compute merge base against $BASE -- the checkout is too shallow to scan. Fetch the base branch with full history (no --depth)." >&2
    return 1
  fi

  while IFS= read -r f; do
    [ -z "$f" ] && continue
    [ ! -f "$f" ] && continue
    case "$f" in
      */.venv/*|*/node_modules/*|*/__pycache__/*) continue ;;
    esac
    case "$f" in
      *.md|*.sh|*.ps1|*.py|*.json|*.yml|*.yaml|*.txt|.env.example) ;;
      *) continue ;;
    esac
    added_lines="$(git diff "$BASE"...HEAD -- "$f" | grep -E '^\+' | grep -vE '^\+\+\+' || true)"
    [ -z "$added_lines" ] && continue
    if matches="$(echo "$added_lines" | grep -nE "$P" 2>/dev/null)"; then
      echo "::error file=$f::private-context token added in this PR"
      echo "$matches" | head -3 | sed 's|^|  |'
      fail=1
    fi
  done < <(git diff --name-only "$BASE"...HEAD)

  if [ "$fail" = "1" ]; then
    echo "::error::Private-context scan failed. Use placeholders or fictional names instead."
    return 1
  fi
  echo "Private-context scan passed (added lines only, merge-base scoped)."
  return 0
}

# ---------------------------------------------------------------------------
# --self-test: the negative control. A gate that has never been observed to
# FAIL on the thing it catches is not known to work.
# ---------------------------------------------------------------------------
self_test() {
  local pass=0 fail=0 work
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' RETURN
  local SELF; SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"

  ok()  { echo "  PASS: $1"; pass=$((pass + 1)); }
  bad() { echo "  FAIL: $1"; fail=$((fail + 1)); }

  git -C "$work" init -q -b main
  git -C "$work" config user.email t@t.local
  git -C "$work" config user.name t

  mkdir -p "$work/docs" "$work/templates"
  # Legacy file that ALREADY contains a private token on both sides.
  printf 'legacy line mentioning Adelaida here\n' > "$work/docs/legacy.md"
  printf '{"a":1}\n' > "$work/templates/x.json"
  git -C "$work" add -A && git -C "$work" commit -qm base

  # Branch forks here.
  git -C "$work" branch pr
  # main advances, MODIFYING the legacy file (this is what makes two-dot lie).
  printf 'legacy line mentioning Adelaida here\nmain added this later\n' > "$work/docs/legacy.md"
  git -C "$work" add -A && git -C "$work" commit -qm main-advances

  # The PR touches ONLY an innocent file.
  git -C "$work" checkout -q pr
  printf '{"a":1,"b":2}\n' > "$work/templates/x.json"
  git -C "$work" add -A && git -C "$work" commit -qm pr-innocent

  # CASE 1: the real-world false positive. Stale branch, innocent change.
  if ( cd "$work" && bash "$SELF" main >/dev/null 2>&1 ); then
    ok "stale branch with an innocent change PASSES (no false positive)"
  else
    bad "stale branch with an innocent change was flagged -- the two-dot bug is back"
  fi

  # CASE 2 (NEGATIVE CONTROL): a genuinely added token MUST still fail.
  printf 'brand new line naming Accenture\n' >> "$work/templates/x.json"
  git -C "$work" add -A && git -C "$work" commit -qm pr-adds-token
  if ( cd "$work" && bash "$SELF" main >/dev/null 2>&1 ); then
    bad "a NET-NEW private token was NOT caught -- the gate is vacuous"
  else
    ok "a NET-NEW private token is still caught (guard bites)"
  fi

  # CASE 3: fail LOUD, never silently clean, when the base is unreachable.
  if ( cd "$work" && bash "$SELF" refs/heads/does-not-exist >/dev/null 2>&1 ); then
    bad "an unreachable base returned CLEAN -- silent fail-open"
  else
    ok "an unreachable base fails loudly rather than reporting clean"
  fi

  echo "=== private-context selftest: $pass passed, $fail failed ==="
  [ "$fail" -eq 0 ]
}

if [ "${1:-}" = "--self-test" ]; then
  self_test
else
  scan "${1:?usage: check-private-context.sh <BASE_REF> | --self-test}"
fi
