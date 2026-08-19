#!/usr/bin/env bash
#
# scripts/check-ps1-bom.sh - every *.ps1 must begin with a UTF-8 BOM (EF BB BF).
#
# Why this is correctness, not style. Windows PowerShell 5.1 - the version that
# ships with every Windows 10/11 box, and the one bootstrap.ps1 actually runs
# under - does not assume UTF-8. Without a BOM it reads a .ps1 as the console's
# ANSI code page (cp1252 on a US box, cp1251 on a Russian one), so the first
# non-ASCII byte decodes wrong and the parser dies on a file that is valid UTF-8
# everywhere else. The BOM is how 5.1 is told the encoding.
#
# ONE implementation, TWO callers, so the rule cannot drift between them:
#
#   1. .github/workflows/lint.yml - the ENFORCING gate. A red here blocks merge.
#   2. scripts/ci.sh              - the locally-runnable pre-push gate, for fast
#      feedback. Before this script existed the rule lived ONLY as an inline
#      loop in lint.yml, so a BOM-less .ps1 passed a full green local
#      `bash scripts/ci.sh` and failed only after a push - one full CI
#      round-trip per occurrence. Measured 2026-08-19 on
#      tests/integration/test_bootstrap_ps1_slash_commands.ps1.
#
# The drift invariant is pinned by scripts/test_ps1_bom_gate.py: it fails if
# either caller stops delegating here, or if anyone re-inlines a private copy of
# the byte comparison.
#
# Usage:
#   bash scripts/check-ps1-bom.sh              # scan this repo
#   bash scripts/check-ps1-bom.sh --self-test  # negative control: prove it bites
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"

# The message a failure prints. Kept as one constant because it is the only
# thing a contributor sees, and because lint.yml used to own the wording.
BOM_HINT='missing UTF-8 BOM (Windows PowerShell 5.1 will crash on non-ASCII bytes). Fix: prepend the bytes EF BB BF to the file.'

# Set by scan(); read by its caller. scan() must therefore never be invoked in a
# command substitution (that forks a subshell and the count is lost) - redirect
# its output to a file instead, which keeps it in the current shell.
SCANNED=0

# Enumerate the *.ps1 files of a checkout.
#
# git, not `find`, and specifically --cached PLUS --others --exclude-standard:
#   * --cached picks up every committed file (what CI sees).
#   * --others --exclude-standard adds files that exist but are not committed
#     yet, which is the whole point of a local pre-push gate: the file that cost
#     a CI round-trip was brand new.
#   * .gitignore, .git/ and node_modules/ are excluded for free, and git does
#     not descend into a nested checkout, so a `.claude/worktrees/<slug>/` copy
#     of this repo is not scanned from its parent (verified: 0 leaks).
#
# In a clean CI checkout this returns exactly what lint.yml's old
# `find . -name '*.ps1' -not -path './.git/*'` returned (both: 14 files at the
# commit this script was written against), so CI semantics are unchanged.
_ps1_files() {
  git -C "$1" ls-files -z --cached --others --exclude-standard -- '*.ps1'
}

# Scan one checkout. Returns 0 when every *.ps1 carries a BOM, 1 otherwise.
# Prints one `::error file=...` line per offender (GitHub renders these as
# inline annotations; on a terminal they are still readable).
scan() {
  local repo="$1"
  local rel abs head3 fail=0
  SCANNED=0
  while IFS= read -r -d '' rel; do
    abs="$repo/$rel"
    # A path can be in the index while absent from the working tree (a staged
    # delete). Nothing to read, nothing to assert.
    [ -f "$abs" ] || continue
    SCANNED=$((SCANNED + 1))
    head3="$(head -c 3 "$abs" | od -An -tx1 | tr -d ' \n')"
    if [ "$head3" != "efbbbf" ]; then
      echo "::error file=$rel::$BOM_HINT"
      fail=1
    fi
  done < <(_ps1_files "$repo")
  return "$fail"
}

# Scan a repo AND assert the enumeration actually matched something. Split from
# scan() so the self-test can exercise both halves independently.
_scan_repo() {
  local repo="$1" rc=0
  scan "$repo" || rc=1
  if [ "$SCANNED" -eq 0 ]; then
    echo "::error::no *.ps1 matched in $repo - this gate scanned nothing and would pass on anything." >&2
    echo "    Either the enumeration broke (a pathspec typo reads exactly like a clean repo), or this" >&2
    echo "    fork genuinely ships no PowerShell - in which case delete this gate rather than leaving" >&2
    echo "    it green over an empty set." >&2
    return 1
  fi
  return "$rc"
}

# --- negative control --------------------------------------------------------
#
# A guard that has never failed on the thing it catches is unproven. This builds
# a real git repo in a temp dir and copies the committed fixtures into it AS
# .ps1 files, so the state under test is the state a real checkout is in - not a
# synthetic one production can never reach. It then drives a full red -> green
# transition on that same tree, which is what separates a working check from one
# that is merely always-red.
_self_test() {
  local fixtures="$REPO_ROOT/tests/fixtures/ps1-bom"
  local no_bom="$fixtures/no-bom.ps1.txt"
  local with_bom="$fixtures/with-bom.ps1.txt"
  local failures=0 tmp empty out f expect quiet

  for f in "$no_bom" "$with_bom"; do
    if [ ! -f "$f" ]; then
      echo "self-test FAILED: missing fixture $f" >&2
      return 1
    fi
  done

  # Fixture integrity. If someone "fixes" the BOM-less fixture by adding a BOM,
  # the negative control below silently stops being a negative control, so
  # assert the bytes directly and say exactly what went wrong.
  if [ "$(head -c 3 "$no_bom" | od -An -tx1 | tr -d ' \n')" = "efbbbf" ]; then
    echo "self-test FAILED: $no_bom starts with a BOM. It is the NEGATIVE control and must not." >&2
    return 1
  fi
  if [ "$(head -c 3 "$with_bom" | od -An -tx1 | tr -d ' \n')" != "efbbbf" ]; then
    echo "self-test FAILED: $with_bom does not start with a BOM. It is the POSITIVE control and must." >&2
    return 1
  fi

  tmp="$(mktemp -d)"
  # shellcheck disable=SC2064  # expand $tmp now: the trap must survive the local
  trap "rm -rf '$tmp'" RETURN

  git init -q "$tmp"
  printf 'ignored/\n' > "$tmp/.gitignore"
  cp "$with_bom" "$tmp/good.ps1"           # committed, correct  -> must pass
  cp "$no_bom" "$tmp/tracked-bad.ps1"      # committed, no BOM   -> must fail
  git -C "$tmp" add .gitignore good.ps1 tracked-bad.ps1
  cp "$no_bom" "$tmp/untracked-bad.ps1"    # not committed yet   -> must fail
  mkdir -p "$tmp/ignored"
  cp "$no_bom" "$tmp/ignored/vendored.ps1" # gitignored          -> must be skipped

  out="$tmp/.scan-out"

  # --- case 1: RED. The two BOM-less files must both be named, the good one
  #     and the ignored one must not, and the count must prove the ignored file
  #     was excluded rather than silently unreadable.
  if scan "$tmp" > "$out" 2>&1; then
    echo "self-test FAILED: scan returned 0 on a tree with two BOM-less .ps1 files." >&2
    failures=$((failures + 1))
  fi
  for expect in tracked-bad.ps1 untracked-bad.ps1; do
    if ! grep -q "file=$expect::" "$out"; then
      echo "self-test FAILED: $expect is BOM-less and was not flagged." >&2
      failures=$((failures + 1))
    fi
  done
  for quiet in good.ps1 vendored.ps1; do
    if grep -q "file=.*$quiet::" "$out"; then
      echo "self-test FAILED: $quiet was flagged and should not have been." >&2
      failures=$((failures + 1))
    fi
  done
  if [ "$SCANNED" -ne 3 ]; then
    echo "self-test FAILED: scanned $SCANNED file(s), expected 3 (good + tracked-bad + untracked-bad; the gitignored one is excluded)." >&2
    failures=$((failures + 1))
  fi

  # --- case 2: GREEN on the SAME tree once the BOM is added. Without this a
  #     check that fails unconditionally would pass case 1.
  for f in "$tmp/tracked-bad.ps1" "$tmp/untracked-bad.ps1"; do
    printf '\357\273\277' | cat - "$f" > "$f.fixed" && mv "$f.fixed" "$f"
  done
  if scan "$tmp" > "$out" 2>&1; then
    :
  else
    echo "self-test FAILED: scan still red after prepending a BOM to every offender:" >&2
    sed 's|^|  |' "$out" >&2
    failures=$((failures + 1))
  fi

  # --- case 3: the enumeration's own blind spot. A pathspec that matches
  #     NOTHING is the failure mode this repo keeps hitting (a guard that reads
  #     as clean because it looked at zero files), so an empty scan must be loud
  #     rather than a green.
  empty="$tmp/empty-repo"
  mkdir -p "$empty"
  git init -q "$empty"
  if _scan_repo "$empty" > "$out" 2>&1; then
    echo "self-test FAILED: a repo with zero .ps1 files scanned green instead of reporting an empty enumeration." >&2
    failures=$((failures + 1))
  fi

  if [ "$failures" -gt 0 ]; then
    echo "self-test FAILED ($failures)" >&2
    return 1
  fi
  echo "    self-test OK - flags BOM-less .ps1 (tracked and not-yet-committed), stays quiet on BOM'd and gitignored files, goes green once fixed, and refuses to pass on an empty enumeration"
  return 0
}

main() {
  if [ "${1:-}" = "--self-test" ]; then
    _self_test
    return $?
  fi
  echo "==> UTF-8 BOM on *.ps1: $REPO_ROOT"
  if _scan_repo "$REPO_ROOT"; then
    echo "    OK - $SCANNED .ps1 file(s), all starting with EF BB BF"
    return 0
  fi
  return 1
}

main "$@"
