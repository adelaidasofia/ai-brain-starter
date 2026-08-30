#!/usr/bin/env bash
# exit-contract: ENFORCING

#
# scripts/psscriptanalyzer.sh - the canonical, locally-runnable PSScriptAnalyzer
# gate for every tracked *.ps1 in ai-brain-starter. ONE command, shared by two
# callers so they can never drift:
#
#   1. .github/workflows/lint.yml  - a step in the `lint` job.
#   2. scripts/ci.sh               - section (c2), so the local pre-push gate
#                                    (~/.local/bin/ci-test) runs the SAME check.
#
# Completes the .ps1 half of the syntax-check-only gap that scripts/shellcheck.sh
# closed for *.sh and scripts/ruff-gate.sh closed for *.py. Until this landed,
# .ps1 was checked for ENCODING (check-ps1-encoding.sh: UTF-8 BOM, no em dash)
# and PARSE only. Nothing looked at semantics -- and .ps1 is the Windows install
# path, the platform with the fewest local eyes on it.
#
# WHY A CURATED RULE LIST AND NOT A SEVERITY FLOOR. shellcheck.sh gates at
# `-S warning` because shellcheck's warning tier is mostly correctness. PSSA's
# is not, and the numbers are not close. Measured over the 17 tracked *.ps1
# when this gate was written:
#
#     Warning      317   (251 of them PSAvoidUsingWriteHost)
#     Information   18
#     Error          1
#
# A `-Severity Warning` gate would therefore land 318 findings RED on day one,
# and the single largest rule in it is WRONG for this repo: these are installers
# and CLIs whose whole job is talking to a human, so Write-Host is the correct
# call, not a defect. Landing red like that only teaches people to bypass the
# gate (over-strict-verification-teaches-bypass). So the contract is an explicit
# RULE LIST -- security and real-bug rules -- every one of which is green on this
# tree. That is auditable in a way "-Severity Warning" is not: you can read
# exactly what is enforced, and adding a file cannot silently change it.
#
# The one Error-severity finding was adjudicated at the source, not gate-wide:
# vault-backup.ps1's Store-Passphrase carries a SuppressMessageAttribute with a
# justification, because it converts a just-typed passphrase INTO DPAPI (the very
# next line writes the encrypted blob). Any OTHER file that genuinely leaks a
# plaintext secret still reds this gate.
#
# PARSE ERRORS ARE STILL CAUGHT. An `-IncludeRule` list does not suppress them:
# PSSA reports ParseError findings regardless of the rule filter (verified, and
# covered by a negative control below). So a syntax error in any tracked *.ps1
# fails this gate even though no syntax rule is named in the list.
#
# Read the list as what it enforces, not as a claim about the repo. Example:
# PSAvoidUsingBrokenHashAlgorithms is green here, and vault-backup.ps1 still uses
# MD5 for a filename slug -- the rule matches the Get-FileHash cmdlet form, not a
# raw .NET type. It is in the list to keep the cmdlet form out, nothing more.
#
# Widen locally without editing the contract:
#     PSSA_INCLUDE_EXTRA='PSAvoidUsingEmptyCatchBlock' bash scripts/psscriptanalyzer.sh
#
# A genuine false-positive is silenced at the SOURCE with a
# SuppressMessageAttribute carrying a Justification -- never by dropping a rule
# from the list for every file.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# The enforced contract. Every name is verified to RESOLVE against the installed
# PSScriptAnalyzer before the scan runs: a renamed or removed rule would
# otherwise sit in this list matching nothing while the gate still reported
# clean, which is the silent-no-op failure this repo treats as worse than no gate.
PSSA_RULES="PSAvoidUsingInvokeExpression,PSAvoidUsingConvertToSecureStringWithPlainText,PSAvoidUsingPlainTextForPassword,PSAvoidUsingUsernameAndPasswordParams,PSUsePSCredentialType,PSAvoidUsingBrokenHashAlgorithms,PSAvoidUsingComputerNameHardcoded,PSPossibleIncorrectComparisonWithNull,PSPossibleIncorrectUsageOfAssignmentOperator,PSPossibleIncorrectUsageOfRedirectionOperator,PSAvoidGlobalVars,PSAvoidDefaultValueSwitchParameter,PSReservedCmdletChar,PSReservedParams,PSUseCmdletCorrectly,PSUseProcessBlockForPipelineCommand,PSShouldProcess,PSMissingModuleManifestField,PSAvoidUsingDeprecatedManifestFields,PSAvoidNullOrEmptyHelpMessageAttribute,PSUseLiteralInitializerForHashtable"
[ -n "${PSSA_INCLUDE_EXTRA:-}" ] && PSSA_RULES="${PSSA_RULES},${PSSA_INCLUDE_EXTRA}"

# --- negative controls -------------------------------------------------------
# An analyzer that dies and an analyzer that finds nothing both print no
# findings. The classification below exists to tell them apart; without these
# controls it is untested on exactly the inputs that motivated it. Each case
# shims a fake `pwsh` onto PATH and asserts THIS script's exit.
if [ "${1:-}" = "--self-test" ]; then
  st_fail=0
  st_tmp="$(mktemp -d)"
  trap 'rm -rf "$st_tmp"' EXIT
  mkdir -p "$st_tmp/bin"

  st_case() { # st_case <label> <fake-rc> <expected-this-script-rc>
    local label="$1" fake="$2" want="$3" got
    cat > "$st_tmp/bin/pwsh" <<SHIM
#!/bin/sh
case "\$1" in --version) echo "PowerShell 0.0.0-fake"; exit 0;; esac
exit $fake
SHIM
    chmod +x "$st_tmp/bin/pwsh"
    # `set -e` is active: without disabling it the FIRST non-zero probe would
    # abort the self-test and the remaining cases -- including the kill case
    # this exists for -- would never run, while the suite looked like it had
    # simply stopped early.
    set +e
    PATH="$st_tmp/bin:$PATH" bash "$0" >/dev/null 2>&1
    got=$?
    set -e
    if [ "$got" != "$want" ]; then
      echo "  FAIL [$label] fake pwsh rc=$fake -> this script exited $got, expected $want"
      st_fail=1
    else
      echo "  ok   [$label] fake pwsh rc=$fake -> exit $got"
    fi
  }

  # Empty-enumeration refusal. Production reaches this via a broken pathspec or
  # an empty checkout -- never because the work is clean. `git init` ONLY: a
  # commit needs user.name/user.email, which a dev box has globally and a CI
  # runner does not, and `git ls-files` already returns nothing without commits.
  st_empty="$st_tmp/emptyrepo"
  mkdir -p "$st_empty/scripts"
  if ! ( cd "$st_empty" && git init -q . ) >/dev/null 2>&1; then
    echo "  FAIL [empty enumeration] could not create the fixture repo -- the"
    echo "       control did not run, which is not a pass."
    st_fail=1
  fi
  cp "$REPO_ROOT/scripts/psscriptanalyzer.sh" "$st_empty/scripts/psscriptanalyzer.sh"
  set +e
  ( cd "$st_empty" && PATH="$st_tmp/bin:$PATH" bash scripts/psscriptanalyzer.sh ) >/dev/null 2>&1
  st_got=$?
  set -e
  if [ "$st_got" -ne 2 ]; then
    echo "  FAIL [empty enumeration] expected exit 2 (UNEVALUATED), got $st_got."
    echo "       A scan of zero files is not a clean tree."
    st_fail=1
  else
    echo "  ok   [empty enumeration] zero tracked *.ps1 -> exit 2, not a pass"
  fi

  st_case "clean"                 0   0
  st_case "real findings"         1   1
  st_case "unresolvable rule"     3   2
  st_case "analyzer error"        4   2
  st_case "SIGKILL (OOM shape)" 137   2
  st_case "SIGTERM"             143   2

  # A syntax error must fail this gate even though the contract names no syntax
  # rule -- the property the curated list depends on. Uses the REAL pwsh; skipped
  # with a loud note (never a silent pass) when pwsh is unavailable. The fixture
  # stages via `update-index` (plumbing) so no porcelain staging runs here.
  if command -v pwsh >/dev/null 2>&1; then
    st_bad="$st_tmp/badrepo"
    mkdir -p "$st_bad/scripts"
    ( cd "$st_bad" && git init -q . ) >/dev/null 2>&1
    printf '\xef\xbb\xbffunction Broken {\n  if ($a -eq 1) {\n    Write-Output "x"\n' > "$st_bad/scripts/broken.ps1"
    cp "$REPO_ROOT/scripts/psscriptanalyzer.sh" "$st_bad/scripts/psscriptanalyzer.sh"
    ( cd "$st_bad" && git update-index --add scripts/broken.ps1 ) >/dev/null 2>&1
    set +e
    ( cd "$st_bad" && bash scripts/psscriptanalyzer.sh ) >/dev/null 2>&1
    st_syn=$?
    set -e
    if [ "$st_syn" -ne 1 ]; then
      echo "  FAIL [parse error] a *.ps1 with a syntax error exited $st_syn, expected 1."
      echo "       An -IncludeRule list must NOT suppress ParseError findings."
      st_fail=1
    else
      echo "  ok   [parse error] syntax error reds the gate despite the curated list"
    fi
  else
    echo "  SKIP [parse error] pwsh absent -- this control did NOT run"
  fi

  # A file tracked in the index but MISSING from disk makes the analyzer throw.
  # That must classify as UNEVALUATED (2), never as findings (1): an uncaught
  # throw exits 1, and reporting a dead analyzer as bad code is the exact
  # confusion this script's three-state contract exists to prevent.
  if command -v pwsh >/dev/null 2>&1; then
    st_gone="$st_tmp/gonerepo"
    mkdir -p "$st_gone/scripts"
    ( cd "$st_gone" && git init -q . ) >/dev/null 2>&1
    printf '\xef\xbb\xbfWrite-Output "ok"\n' > "$st_gone/scripts/vanishes.ps1"
    cp "$REPO_ROOT/scripts/psscriptanalyzer.sh" "$st_gone/scripts/psscriptanalyzer.sh"
    ( cd "$st_gone" && git update-index --add scripts/vanishes.ps1 ) >/dev/null 2>&1
    rm -f "$st_gone/scripts/vanishes.ps1"
    set +e
    ( cd "$st_gone" && bash scripts/psscriptanalyzer.sh ) >/dev/null 2>&1
    st_gone_rc=$?
    set -e
    if [ "$st_gone_rc" -ne 2 ]; then
      echo "  FAIL [analyzer throws] tracked-but-missing *.ps1 exited $st_gone_rc, expected 2."
      echo "       A crashed analyzer must not be reported as a lint finding."
      st_fail=1
    else
      echo "  ok   [analyzer throws] tracked-but-missing *.ps1 -> exit 2, not findings"
    fi
  else
    echo "  SKIP [analyzer throws] pwsh absent -- this control did NOT run"
  fi

  if [ "$st_fail" -ne 0 ]; then
    echo "psscriptanalyzer.sh self-test FAILED" >&2
    exit 1
  fi
  echo "OK - self-test: findings (1), a killed/unusable analyzer (2), and an empty enumeration (2) are all distinct from a clean pass (0)."
  exit 0
fi

if ! command -v pwsh >/dev/null 2>&1; then
  echo "::error::pwsh (PowerShell 7+) not installed." >&2
  echo "  macOS:         brew install --cask powershell" >&2
  echo "  Debian/Ubuntu: https://learn.microsoft.com/powershell/scripting/install/install-ubuntu" >&2
  exit 1
fi

# Collect every tracked *.ps1. git ls-files is the source of truth -- the SAME
# corpus check-ps1-encoding.sh reads, so the two cannot disagree about what
# "every tracked *.ps1" means. -z is NUL-delimited so paths with spaces survive.
# Built bash-3.2-safe (macOS ships bash 3.2): no `mapfile -d`, just read+append.
files=()
while IFS= read -r -d '' f; do
  files+=("$f")
done < <(git ls-files -z -- '*.ps1')

if [ "${#files[@]}" -eq 0 ]; then
  # REFUSE, do not pass. This repo tracks 17 *.ps1, so an empty enumeration
  # means the SELECTION broke -- a bad pathspec, an empty checkout, a failed
  # `git ls-files` -- never that the work is clean. Exiting 0 here would report
  # a clean scan over a run that never happened.
  echo "UNEVALUATED: zero tracked *.ps1 found. This repo tracks several, so an" >&2
  echo "empty file list means the SELECTION failed, not that the tree is clean." >&2
  echo "Nothing was analyzed -- this is not a pass." >&2
  exit 2
fi

# The analyzer runs from a FILE rather than -Command: the rule list and the
# GitHub annotation format both contain characters that do not survive being
# quoted through two shells intact.
helper="$(mktemp -t pssa-gate.XXXXXX)"
mv "$helper" "$helper.ps1"; helper="$helper.ps1"
listfile="$(mktemp -t pssa-files.XXXXXX)"
cleanup_all() { rm -f "$helper" "$listfile"; }
trap cleanup_all EXIT

cat > "$helper" <<'HELPER'
param([string]$RuleCsv, [string]$FileList)
$ErrorActionPreference = 'Stop'
try { Import-Module PSScriptAnalyzer -ErrorAction Stop } catch {
  [Console]::Error.WriteLine("PSScriptAnalyzer module not available: $($_.Exception.Message)")
  exit 3
}
$rules = $RuleCsv.Split(',') | Where-Object { $_ }
# Fail LOUD on a rule name the installed analyzer does not know. A stale name in
# the contract would sit there matching nothing while the gate reported clean --
# the scope silently shrinks and the green looks identical.
$known = (Get-ScriptAnalyzerRule).RuleName
$bad = @($rules | Where-Object { $_ -notin $known })
if ($bad.Count -gt 0) {
  [Console]::Error.WriteLine("UNRESOLVABLE RULE NAME(S): " + ($bad -join ', '))
  [Console]::Error.WriteLine("These would match nothing and the scan would still report clean.")
  exit 3
}
$files = [System.IO.File]::ReadAllLines($FileList) | Where-Object { $_ }
$ver = (Get-Module -ListAvailable PSScriptAnalyzer | Select-Object -First 1).Version
Write-Output "==> PSScriptAnalyzer over $($files.Count) tracked *.ps1, $($rules.Count) enforced rule(s)  [$ver]"
$found = @()
# The scan is wrapped because an UNCAUGHT terminating error exits 1, which the
# caller reads as "found issues" -- mislabelling a dead analyzer as bad code and
# sending the reader hunting a defect that does not exist. A file tracked in the
# index but missing from disk throws ItemNotFoundException here, and so does an
# unreadable or mangled path. Exit 3 = could-not-evaluate, which the caller maps
# to UNEVALUATED instead.
try {
  foreach ($f in $files) { $found += Invoke-ScriptAnalyzer -Path $f -IncludeRule $rules }
} catch {
  [Console]::Error.WriteLine("SCAN FAILED: $($_.Exception.GetType().Name): $($_.Exception.Message)")
  [Console]::Error.WriteLine("The analyzer did not finish, so the tree was NOT verified.")
  exit 3
}
if ($found.Count -eq 0) { exit 0 }
foreach ($d in $found) {
  if ($env:GITHUB_ACTIONS) {
    Write-Output ("::error file={0},line={1}::{2}: {3}" -f $d.ScriptPath, $d.Line, $d.RuleName, $d.Message)
  } else {
    Write-Output ("{0}:{1} [{2}] {3}" -f $d.ScriptName, $d.Line, $d.RuleName, $d.Message)
  }
}
exit 1
HELPER

printf '%s\n' "${files[@]}" > "$listfile"

# Classify pwsh's status instead of treating every non-zero as "found issues".
# The helper returns 0 = clean, 1 = findings, 3 = it could not evaluate (module
# missing, or a rule name that no longer resolves). Anything else means the
# process itself did not deliver a verdict -- 128+N when the kernel killed it
# (137 = SIGKILL, the OOM shape on a loaded runner). Reporting a signal death as
# a lint finding sends the reader hunting a defect that does not exist; the
# adjacent shape is worse -- a "no findings" line over a scan that never ran.
set +e
pwsh -NoProfile -NonInteractive -File "$helper" -RuleCsv "$PSSA_RULES" -FileList "$listfile"
ps_rc=$?
set -e

if [ "$ps_rc" -eq 0 ]; then
  echo "    OK - no PSScriptAnalyzer findings in the enforced rule set"
elif [ "$ps_rc" -eq 1 ]; then
  echo "FAILED: PSScriptAnalyzer found issues in the enforced rule set." >&2
  echo "These are security and real-bug rules, plus any syntax error. Fix them," >&2
  echo "or for a genuine false-positive add a SuppressMessageAttribute WITH a" >&2
  echo "Justification at the source. Do NOT drop a rule from the list in this" >&2
  echo "script to hide a finding (see its header)." >&2
  exit 1
elif [ "$ps_rc" -eq 3 ]; then
  echo "UNEVALUATED: the analyzer could not run its contract (see stderr above)." >&2
  echo "Either PSScriptAnalyzer is not installed, or a rule name in this script no" >&2
  echo "longer resolves. Nothing was verified -- this is not a pass." >&2
  exit 2
elif [ "$ps_rc" -gt 128 ]; then
  echo "UNEVALUATED: pwsh was KILLED by signal $((ps_rc - 128)) (exit $ps_rc)." >&2
  echo "It did not finish, so NOTHING was analyzed -- this is not a finding and it" >&2
  echo "is not a pass. On a loaded runner this is usually the OOM killer." >&2
  exit 2
else
  echo "UNEVALUATED: pwsh exited $ps_rc, which is none of clean (0), findings (1)," >&2
  echo "or could-not-evaluate (3). The scan is incomplete and its silence means" >&2
  echo "nothing." >&2
  exit 2
fi
