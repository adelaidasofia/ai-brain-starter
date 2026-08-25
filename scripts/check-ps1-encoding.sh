#!/usr/bin/env bash
#
# scripts/check-ps1-encoding.sh - *.ps1 byte hygiene for Windows PowerShell 5.1.
#
# TWO rules, one scanner:
#
#   BOM      every *.ps1 must BEGIN with a UTF-8 BOM (EF BB BF).
#   EM DASH  no *.ps1 may CONTAIN U+2014 (E2 80 94).
#
# They are one scanner and not two scripts because they run over the same
# enumeration, and the enumeration is where the bugs have actually been: a
# `head -20` cap in one copy silently hid a real defect (#559). A second script
# would mean a second enumeration to cap, mis-glob or point at the wrong tree -
# deduplicating the rule while duplicating the scanner is a half fix.
#
# Why this is correctness, not style. Windows PowerShell 5.1 - the version that
# ships with every Windows 10/11 box, and the one bootstrap.ps1 actually runs
# under - does not assume UTF-8. Without a BOM it reads a .ps1 as the console's
# ANSI code page (cp1252 on a US box, cp1251 on a Russian one), so the first
# non-ASCII byte decodes wrong and the parser dies on a file that is valid UTF-8
# everywhere else. The BOM is how 5.1 is told the encoding.
#
# The em-dash rule is the same failure from the other side. U+2014 was the exact
# byte sequence that crashed the 5.1 parser before the BOM fix, and it is the
# non-ASCII character most likely to arrive by accident - an editor's autocorrect
# or a paste from a doc - in a file where a plain hyphen was meant.
#
# Be precise about what this buys, because it is easy to overstate. If the BOM is
# present, non-ASCII parses fine and this rule is style. If the BOM is ever lost,
# ANY non-ASCII byte breaks the file, and U+2014 is not special among them. So
# this is NOT an ASCII-cleanliness guarantee: 4 of the 14 *.ps1 files here carry
# deliberate non-ASCII (box-drawing rules in section headers, a middle dot in the
# log prefix, the enye in "Espanol", a gear and an arrow) and are intentionally
# untouched. What the rule actually does is close the one hole that opens by
# accident rather than by choice. Widening it to all non-ASCII behind a pinned
# baseline - the pattern utf8-stdout-baseline.txt already uses here - is tracked
# separately (MYC-4040); it is a real decision about those four files, not a bug fix.
#
# ONE implementation, THREE callers, so the rule cannot drift between them:
#
#   1. .github/workflows/lint.yml - the ENFORCING gate. A red here blocks merge.
#      Both rules lived there as inline loops, each with its own `find`.
#   2. scripts/ci.sh              - the locally-runnable pre-push gate, for fast
#      feedback. Before this script existed the rule lived ONLY as an inline
#      loop in lint.yml, so a BOM-less .ps1 passed a full green local
#      `bash scripts/ci.sh` and failed only after a push - one full CI
#      round-trip per occurrence. Measured 2026-08-19 on
#      tests/integration/test_bootstrap_ps1_slash_commands.ps1.
#   3. scripts/diagnose.sh        - the USER-facing health report, via
#      --porcelain. Different target (an installed vault, which is usually not
#      a git repo), different verdict (a warning, not a merge block), same rule.
#      Its private copy was capped at `head -20`, so a vault with more than 20
#      .ps1 files reported "All .ps1 files have UTF-8 BOM" while never reading
#      the rest - and diagnose.ps1, which has no cap, reported the same vault
#      honestly. Folding it in removed the copy and the truncation together.
#
# scripts/diagnose.ps1 is DELIBERATELY not a caller. It is the Windows-native
# surface, run as `pwsh diagnose.ps1 -Vault C:\path` on a box that need not have
# bash at all, and no .ps1 in this repo shells out to bash. This repo's idiom
# for sh/ps1 parity is a shared data source plus a test that reads both (see
# sync-vault-scripts.ps1), not a cross-language call - so the .ps1 keeps its own
# byte check and scripts/test_ps1_encoding_gate.py pins the two to the same bytes.
#
# The drift invariant is pinned by scripts/test_ps1_encoding_gate.py: it fails if any
# caller stops delegating here, if anyone re-inlines a private copy of the byte
# comparison, or if either surface starts capping its enumeration again.
#
# Usage:
#   bash scripts/check-ps1-encoding.sh                    # scan this repo (gate, both rules)
#   bash scripts/check-ps1-encoding.sh --self-test        # negative control
#   bash scripts/check-ps1-encoding.sh --porcelain DIR... # report on arbitrary trees
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"

# The message a failure prints. Kept as one constant because it is the only
# thing a contributor sees, and because lint.yml used to own the wording.
BOM_HINT='missing UTF-8 BOM (Windows PowerShell 5.1 will crash on non-ASCII bytes). Fix: prepend the bytes EF BB BF to the file.'
EMDASH_HINT='contains em dash. Replace with ASCII hyphen or comma. Em dashes break Windows PowerShell 5.1 parsing if the BOM is ever lost.'

# U+2014 as bytes, built with printf rather than a literal, so this checker stays
# ASCII-clean itself. A checker that contains the character it bans would be
# flagged by its own rule if it were ever a .ps1, and is confusing regardless.
EMDASH="$(printf '\xe2\x80\x94')"

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
    # Both rules on the SAME file in the SAME pass. A file can violate both, and
    # both are reported: fixing the BOM does not fix an em dash.
    if grep -q "$EMDASH" "$abs" 2>/dev/null; then
      echo "::error file=$rel::$EMDASH_HINT"
      # Line numbers, because unlike the BOM this is a property of a POSITION in
      # the file and "somewhere in this file" is not an actionable report.
      grep -n "$EMDASH" "$abs" | head -5 | sed 's|^|  |'
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

# --- porcelain mode (arbitrary trees, e.g. an installed vault) ----------------
#
# `find`, not `git ls-files`, and deliberately so: this mode reports on a user's
# VAULT, which diagnose.sh itself warns may not be a git repo. Asking git to
# enumerate a non-repo returns nothing, and "nothing" is indistinguishable from
# "clean" - the exact silent-green this file exists to prevent. The gate above
# keeps its git enumeration because a repo checkout IS a repo.
#
# NO cap. The copy this replaced ended in `| head -20`, which is what let a
# 25-file vault report green on 20 files. A scanner states how much of its input
# it read, so the count travels with the verdict and the caller can print it.
_ps1_files_find() {
  find "$@" -name '*.ps1' -not -path '*/.git/*' -print0 2>/dev/null
}

# Emit ONE machine-readable line of key=value fields:
#
#   scanned=<n> missing_bom=<m> emdash=<k>
#
# key=value rather than the verdict token check-cloud-sync.py uses, because
# there are two independent rules now and a caller needs both counts to print
# two verdicts. `scanned` always travels with them: a scanner states how much of
# its input it read, so "0 missing" can be told apart from "read nothing".
#
# Exit is 0 whenever the scan ran: this is a REPORT, not a gate. A caller that
# cannot parse the line must warn ("could not evaluate"), never assume clean.
_report() {
  local abs head3 scanned=0 missing=0 emdash=0
  while IFS= read -r -d '' abs; do
    [ -f "$abs" ] || continue
    scanned=$((scanned + 1))
    head3="$(head -c 3 "$abs" 2>/dev/null | od -An -tx1 | tr -d ' \n')"
    [ "$head3" != "efbbbf" ] && missing=$((missing + 1))
    grep -q "$EMDASH" "$abs" 2>/dev/null && emdash=$((emdash + 1))
  done < <(_ps1_files_find "$@")

  echo "scanned=$scanned missing_bom=$missing emdash=$emdash"
  return 0
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
  local fixtures="$REPO_ROOT/tests/fixtures/ps1-encoding"
  local no_bom="$fixtures/no-bom.ps1.txt"
  local with_bom="$fixtures/with-bom.ps1.txt"
  local em_dash="$fixtures/emdash.ps1.txt"
  local failures=0 tmp empty out f expect quiet

  for f in "$no_bom" "$with_bom" "$em_dash"; do
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

  # The em-dash control must CARRY a BOM and CONTAIN exactly the banned bytes.
  # With no BOM it would be red for the wrong reason; with no em dash it would
  # be a second positive control wearing a negative control's name.
  if [ "$(head -c 3 "$em_dash" | od -An -tx1 | tr -d ' \n')" != "efbbbf" ]; then
    echo "self-test FAILED: $em_dash has no BOM. It isolates the EM DASH, so it must be BOM-clean." >&2
    return 1
  fi
  if ! grep -q "$EMDASH" "$em_dash"; then
    echo "self-test FAILED: $em_dash contains no em dash. It is the NEGATIVE control for that rule." >&2
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
  cp "$em_dash" "$tmp/emdash-bad.ps1"      # BOM ok, em dash     -> must fail
  git -C "$tmp" add emdash-bad.ps1
  mkdir -p "$tmp/ignored"
  cp "$no_bom" "$tmp/ignored/vendored.ps1" # gitignored          -> must be skipped

  out="$tmp/.scan-out"

  # --- case 1: RED. The two BOM-less files must both be named, the good one
  #     and the ignored one must not, and the count must prove the ignored file
  #     was excluded rather than silently unreadable.
  if scan "$tmp" > "$out" 2>&1; then
    echo "self-test FAILED: scan returned 0 on a tree with two BOM-less and one em-dashed .ps1." >&2
    failures=$((failures + 1))
  fi
  for expect in tracked-bad.ps1 untracked-bad.ps1; do
    if ! grep -q "file=$expect::" "$out"; then
      echo "self-test FAILED: $expect is BOM-less and was not flagged." >&2
      failures=$((failures + 1))
    fi
  done
  # The em-dash file carries a BOM, so it can only be flagged by the em-dash
  # rule. If the two rules were ever collapsed into one, this is what notices.
  if ! grep -q "file=emdash-bad.ps1::.*em dash" "$out"; then
    echo "self-test FAILED: emdash-bad.ps1 contains U+2014 and was not flagged for it." >&2
    failures=$((failures + 1))
  fi
  if grep -q "file=emdash-bad.ps1::missing UTF-8 BOM" "$out"; then
    echo "self-test FAILED: emdash-bad.ps1 was flagged for a missing BOM; it has one." >&2
    failures=$((failures + 1))
  fi
  for quiet in good.ps1 vendored.ps1; do
    if grep -q "file=.*$quiet::" "$out"; then
      echo "self-test FAILED: $quiet was flagged and should not have been." >&2
      failures=$((failures + 1))
    fi
  done
  if [ "$SCANNED" -ne 4 ]; then
    echo "self-test FAILED: scanned $SCANNED file(s), expected 4 (good + tracked-bad + untracked-bad + emdash-bad; the gitignored one is excluded)." >&2
    failures=$((failures + 1))
  fi

  # --- case 2: GREEN on the SAME tree once the BOM is added. Without this a
  #     check that fails unconditionally would pass case 1.
  for f in "$tmp/tracked-bad.ps1" "$tmp/untracked-bad.ps1"; do
    printf '\357\273\277' | cat - "$f" > "$f.fixed" && mv "$f.fixed" "$f"
  done
  # Still red, and specifically for the OTHER rule. Fixing every BOM must not
  # silence an em dash - if it does, one rule is shadowing the other.
  if scan "$tmp" > "$out" 2>&1; then
    echo "self-test FAILED: scan went green with an em-dashed .ps1 still present." >&2
    failures=$((failures + 1))
  elif ! grep -q "file=emdash-bad.ps1::.*em dash" "$out"; then
    echo "self-test FAILED: after the BOMs were fixed the em dash was no longer reported." >&2
    failures=$((failures + 1))
  fi

  # Now fix the em dash too. Only now may the tree be green.
  sed "s/$EMDASH/-/g" "$tmp/emdash-bad.ps1" > "$tmp/emdash-bad.fixed" \
    && mv "$tmp/emdash-bad.fixed" "$tmp/emdash-bad.ps1"
  if scan "$tmp" > "$out" 2>&1; then
    :
  else
    echo "self-test FAILED: scan still red after fixing every BOM and every em dash:" >&2
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

  # --- case 4: porcelain mode, on the tree diagnose.sh actually reports on -
  #     a plain directory that is NOT a git repo. If this mode ever enumerated
  #     with git it would return zero files here and print OK, which is the
  #     silent-green the gate half already guards against.
  local vault="$tmp/vault-not-a-repo"
  mkdir -p "$vault"
  cp "$with_bom" "$vault/ok.ps1"
  if [ "$(_report "$vault")" != "scanned=1 missing_bom=0 emdash=0" ]; then
    echo "self-test FAILED: porcelain on a non-repo dir with one clean file returned '$(_report "$vault")'." >&2
    failures=$((failures + 1))
  fi
  cp "$no_bom" "$vault/bad.ps1"
  if [ "$(_report "$vault")" != "scanned=2 missing_bom=1 emdash=0" ]; then
    echo "self-test FAILED: porcelain did not report 1 missing BOM of 2 (got '$(_report "$vault")')." >&2
    failures=$((failures + 1))
  fi
  # The two counts must move INDEPENDENTLY. Adding an em-dashed file must raise
  # emdash without touching missing_bom, or the caller's two verdicts are one.
  cp "$em_dash" "$vault/em.ps1"
  if [ "$(_report "$vault")" != "scanned=3 missing_bom=1 emdash=1" ]; then
    echo "self-test FAILED: porcelain did not count the em dash separately (got '$(_report "$vault")')." >&2
    failures=$((failures + 1))
  fi
  rm -rf "$vault"
  mkdir -p "$vault"
  if [ "$(_report "$vault")" != "scanned=0 missing_bom=0 emdash=0" ]; then
    echo "self-test FAILED: porcelain on an empty dir returned '$(_report "$vault")'." >&2
    failures=$((failures + 1))
  fi

  # --- case 5: the TRUNCATION class, which is why this mode exists. The copy
  #     in diagnose.sh ended in `| head -20`, so a vault with more than 20 .ps1
  #     files reported green while never reading the rest. Plant 25 good files
  #     and exactly one bad one, and require the bad one to be found regardless
  #     of where it lands in enumeration order.
  local big="$tmp/big-vault" n
  mkdir -p "$big"
  for n in $(seq 1 25); do
    cp "$with_bom" "$big/f$n.ps1"
  done
  cp "$no_bom" "$big/needle.ps1"
  cp "$em_dash" "$big/needle2.ps1"
  if [ "$(_report "$big")" != "scanned=27 missing_bom=1 emdash=1" ]; then
    echo "self-test FAILED: porcelain missed a needle among 27 files (got '$(_report "$big")', expected scanned=27 missing_bom=1 emdash=1). A cap on the enumeration is back." >&2
    failures=$((failures + 1))
  fi

  if [ "$failures" -gt 0 ]; then
    echo "self-test FAILED ($failures)" >&2
    return 1
  fi
  echo "    self-test OK - both rules: flags BOM-less .ps1 (tracked and not-yet-committed) and em-dashed .ps1 independently, stays quiet on clean and gitignored files, goes green only once BOTH are fixed, refuses to pass on an empty enumeration, and counts honestly on a non-repo vault of 27 files with no cap"
  return 0
}

main() {
  if [ "${1:-}" = "--self-test" ]; then
    _self_test
    return $?
  fi
  if [ "${1:-}" = "--porcelain" ]; then
    shift
    if [ "$#" -eq 0 ]; then
      echo "usage: check-ps1-encoding.sh --porcelain DIR [DIR...]" >&2
      return 2
    fi
    # Drop roots that do not exist so `find` cannot fail the whole report over
    # one absent path (an install without the skill dir is normal, not an error).
    local roots=() d
    for d in "$@"; do
      [ -d "$d" ] && roots+=("$d")
    done
    if [ "${#roots[@]}" -eq 0 ]; then
      echo "NONE:0"
      return 0
    fi
    _report "${roots[@]}"
    return $?
  fi
  echo "==> .ps1 encoding (UTF-8 BOM + no em dash): $REPO_ROOT"
  if _scan_repo "$REPO_ROOT"; then
    echo "    OK - $SCANNED .ps1 file(s): all start with EF BB BF, none contain an em dash"
    return 0
  fi
  return 1
}

main "$@"
