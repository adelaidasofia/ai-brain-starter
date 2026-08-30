#!/usr/bin/env bash
# exit-contract: ENFORCING

#
# scripts/shellcheck.sh - the canonical, locally-runnable shellcheck gate for
# ai-brain-starter. ONE command, shared by two callers so they can never drift:
#
#   1. .github/workflows/lint.yml  - the `shellcheck` job runs `bash scripts/shellcheck.sh`.
#   2. scripts/ci.sh               - section (c) runs `bash scripts/shellcheck.sh`, so the
#                                    local pre-push gate (~/.local/bin/ci-test) runs the
#                                    SAME shellcheck. CI and the laptop cannot drift.
#
# Why this exists: ai-brain-starter ships ~80 tracked *.sh that must run on macOS
# AND Linux (users install on both; CI is ubuntu). `bash -n` (the `shell` lint job)
# is syntax-only - it cannot catch the portability / quoting / correctness class:
# BSD-only flags, unquoted expansions (SC2086), unset vars, exit codes masked by a
# pipe. A real `stat -c %Y` (GNU) vs `stat -f %m` (BSD) mtime bug once passed
# macOS-local + `bash -n` and only failed on the ubuntu CI runner. shellcheck
# catches that class before it ships.
#
# Severity gate: -S warning (error + warning). info/style are NOT failed here, so
# the gate matches the real ship/hold boundary, not the strictest possible signal
# (an over-strict gate just teaches people to bypass it). Raise the floor later, on
# purpose, if the baseline supports it:  SHELLCHECK_SEVERITY=style bash scripts/shellcheck.sh
#
# A genuine false-positive is silenced at the source with an inline shellcheck
# disable directive carrying a one-line reason - never by lowering the gate for
# every file.
#
# Idioms shellcheck does NOT catch (cross-platform stat / date / sed) are
# documented in scripts/PORTABILITY.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# --- negative controls (MYC-4259) -------------------------------------------
# A signal death and a lint finding used to be indistinguishable here: every
# non-zero shellcheck status printed "found issues" and exited 1. These shim a
# fake `shellcheck` onto PATH so each documented status can be driven directly,
# and assert this script's OWN exit for each. Without the kill case the
# classification below is untested on the one input that motivated it.
if [ "${1:-}" = "--self-test" ]; then
  st_fail=0
  st_tmp="$(mktemp -d)"
  trap 'rm -rf "$st_tmp"' EXIT
  mkdir -p "$st_tmp/bin"

  st_case() { # st_case <label> <fake-rc> <expected-this-script-rc>
    local label="$1" fake="$2" want="$3" got
    # `shellcheck --version` is called before the scan, so the shim must answer
    # it with a plausible version and only then honour the injected status.
    cat > "$st_tmp/bin/shellcheck" <<SHIM
#!/bin/sh
case "\$1" in --version) echo "ShellCheck - shell script analysis tool"; echo "version: 0.0.0-fake"; exit 0;; esac
exit $fake
SHIM
    chmod +x "$st_tmp/bin/shellcheck"
    # `set -e` is active: without disabling it, the FIRST probe that returns
    # non-zero would abort the self-test and the remaining cases -- including
    # the kill case this exists for -- would never run, while the suite looked
    # like it had simply stopped early.
    set +e
    PATH="$st_tmp/bin:$PATH" bash "$0" >/dev/null 2>&1
    got=$?
    set -e
    if [ "$got" != "$want" ]; then
      echo "  FAIL [$label] fake shellcheck rc=$fake -> this script exited $got, expected $want"
      st_fail=1
    else
      echo "  ok   [$label] fake shellcheck rc=$fake -> exit $got"
    fi
  }

  st_case "clean"                0   0
  st_case "real findings"        1   1
  st_case "could-not-process"    2   2
  st_case "SIGKILL (OOM shape)"  137 2
  st_case "SIGTERM"              143 2

  if [ "$st_fail" -ne 0 ]; then
    echo "shellcheck.sh self-test FAILED" >&2
    exit 1
  fi
  echo "OK - self-test: findings (1) and a killed/failed linter (2) are distinct exits."
  exit 0
fi


if ! command -v shellcheck >/dev/null 2>&1; then
  echo "::error::shellcheck not installed." >&2
  echo "  macOS:         brew install shellcheck" >&2
  echo "  Debian/Ubuntu: sudo apt-get install -y shellcheck" >&2
  exit 1
fi

SEVERITY="${SHELLCHECK_SEVERITY:-warning}"

# Collect every tracked *.sh. git ls-files is the source of truth (same as ci.sh's
# py_compile gate): it excludes .git/, node_modules/, and untracked cruft for free,
# and -z is NUL-delimited so paths with spaces / emoji survive. Built bash-3.2-safe
# (macOS ships bash 3.2): no `mapfile -d`, just a read + append loop.
files=()
while IFS= read -r -d '' f; do
  files+=("$f")
done < <(git ls-files -z -- '*.sh')

# Empty-array expansion under `set -u` errors on bash 3.2 / 4.3; guard it.
if [ "${#files[@]}" -eq 0 ]; then
  echo "no tracked *.sh found - nothing to check"
  exit 0
fi

echo "==> shellcheck -S $SEVERITY over ${#files[@]} tracked *.sh  [$(shellcheck --version | awk '/^version:/{print $2}')]"

# GitHub Actions renders one annotation per finding from the gcc format; an
# interactive run gets shellcheck's readable default.
fmt="tty"
[ -n "${GITHUB_ACTIONS:-}" ] && fmt="gcc"

# Classify shellcheck's status instead of treating every non-zero as "found
# issues" (MYC-4259). shellcheck documents 0 = clean and 1 = findings; anything
# else means the TOOL did not deliver a verdict -- 2 for a file it could not
# process, and 128+N when the kernel killed it (137 = SIGKILL, the OOM shape on
# a loaded runner). Reporting a signal death as a lint finding sends the reader
# hunting for a defect that does not exist, and the adjacent shape is worse: a
# "no findings" line over a run that never actually linted anything.
#
# Exit 2 for could-not-evaluate, deliberately distinct from 1 = real findings,
# so a caller can tell "your code is bad" from "the linter died".
set +e
shellcheck -S "$SEVERITY" -f "$fmt" "${files[@]}"
sc_rc=$?
set -e

if [ "$sc_rc" -eq 0 ]; then
  echo "    OK - no shellcheck findings at -S $SEVERITY"
elif [ "$sc_rc" -eq 1 ]; then
  echo "FAILED: shellcheck found issues at -S $SEVERITY." >&2
  echo "Fix them, or - for a genuine false-positive - add an inline shellcheck" >&2
  echo "disable directive with a one-line reason at the source line." >&2
  echo "Do NOT lower the severity gate to hide a finding (see this script's header)." >&2
  exit 1
elif [ "$sc_rc" -gt 128 ]; then
  echo "UNEVALUATED: shellcheck was KILLED by signal $((sc_rc - 128)) (exit $sc_rc)." >&2
  echo "It did not finish, so NOTHING was linted -- this is not a finding and it" >&2
  echo "is not a pass. On a loaded runner this is usually the OOM killer." >&2
  exit 2
else
  echo "UNEVALUATED: shellcheck exited $sc_rc, which is neither clean (0) nor" >&2
  echo "findings (1). It could not process one or more files, so the scan is" >&2
  echo "incomplete and its silence means nothing." >&2
  exit 2
fi
