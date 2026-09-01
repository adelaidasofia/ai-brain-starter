#!/usr/bin/env bash
# exit-contract: ENFORCING

#
# scripts/ruff-gate.sh - the canonical, locally-runnable undefined-name gate for
# every tracked *.py in ai-brain-starter. ONE command, shared by two callers so
# they can never drift:
#
#   1. .github/workflows/lint.yml  - a step in the `lint` job (ruff is installed
#                                    fail-closed two steps above).
#   2. scripts/ci.sh               - section (d2), so the local pre-push gate
#                                    (~/.local/bin/ci-test) runs the SAME check.
#
# Why this exists. Until this gate landed, NO CI check linted this repo's own
# *.py for undefined names. Gate (a) py_compiles every tracked *.py, but
# py_compile parses -- it cannot see a name that does not exist. The only ruff
# F821 in CI was scripts/check-phase-python.py, scoped to Python blocks embedded
# in phases/*.md. So the ~400 tracked *.py -- including hooks/ that execute on
# every install -- had a syntax check and nothing more.
#
# The gap shipped. `env=_GIT_CLEAN_ENV` landed in hooks/session-lock.py on
# 2026-08-12 (4a2bf7c) with the constant never defined, and sat on main for
# THIRTEEN DAYS until 5952dac defined it on 2026-08-25. Nothing in CI could see
# it. hooks/check-py-import-precommit.py catches exactly this class and is
# tested by tests/integration/test_session_coordination_guards.sh -- but it is a
# commit-time hook on a developer's machine, so it only fires where it happens
# to be registered, and it fails OPEN when ruff is absent. A guard the repo
# ships, tests, and never points at itself is not coverage.
#
# session-lock.py is the concurrency gate, and settings.json registers it behind
# an allow-fallback wrapper. A NameError there does not fail loudly: the hook
# crashes, the wrapper answers "allow", and the gate is silently open.
#
# Severity gate: E9,F63,F7,F82 -- ruff's standard error set (syntax errors,
# undefined names, and the always-true assert/comparison class). This is the
# real ship/hold boundary, NOT the strictest signal: the full `F` ruleset
# reports 138 findings on this tree today (mostly unused imports/locals), and a
# gate that lands red just teaches people to bypass it
# (over-strict-verification-teaches-bypass). Raise the floor later, on purpose,
# once a baseline supports it:
#     RUFF_GATE_SELECT=F bash scripts/ruff-gate.sh
#
# A genuine false-positive is silenced at the source with `# noqa: <rule>` and a
# one-line reason -- never by narrowing the select for every file.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
cd "$REPO_ROOT"

SELECT="${RUFF_GATE_SELECT:-E9,F63,F7,F82}"

# --- negative controls -------------------------------------------------------
# A linter that dies and a linter that finds nothing both print no findings. The
# classification below exists to tell them apart, and without these controls it
# would be untested on the only inputs that motivated it. Each case shims a fake
# `ruff` onto PATH, drives one documented status, and asserts THIS script's exit.
if [ "${1:-}" = "--self-test" ]; then
  st_fail=0
  st_tmp="$(mktemp -d)"
  trap 'rm -rf "$st_tmp"' EXIT
  mkdir -p "$st_tmp/bin"

  st_case() { # st_case <label> <fake-rc> <expected-this-script-rc>
    local label="$1" fake="$2" want="$3" got
    # `ruff --version` runs before the scan, so the shim must answer it first
    # and only then honour the injected status.
    cat > "$st_tmp/bin/ruff" <<SHIM
#!/bin/sh
case "\$1" in --version) echo "ruff 0.0.0-fake"; exit 0;; esac
exit $fake
SHIM
    chmod +x "$st_tmp/bin/ruff"
    # `set -e` is active: without disabling it the FIRST non-zero probe would
    # abort the self-test and the remaining cases -- including the kill case
    # this exists for -- would never run, while the suite looked like it simply
    # stopped early.
    set +e
    PATH="$st_tmp/bin:$PATH" bash "$0" >/dev/null 2>&1
    got=$?
    set -e
    if [ "$got" != "$want" ]; then
      echo "  FAIL [$label] fake ruff rc=$fake -> this script exited $got, expected $want"
      st_fail=1
    else
      echo "  ok   [$label] fake ruff rc=$fake -> exit $got"
    fi
  }

  # The empty-enumeration refusal. Driven by pointing the scan at a repo with no
  # tracked *.py, which production reaches via a broken pathspec or an empty
  # checkout -- never because the work is clean. `git init` ONLY: a commit needs
  # user.name/user.email, which a dev box has globally and a CI runner does not,
  # and `git ls-files` already returns nothing in a repo with no commits.
  st_empty="$st_tmp/emptyrepo"
  mkdir -p "$st_empty/scripts"
  if ! ( cd "$st_empty" && git init -q . ) >/dev/null 2>&1; then
    echo "  FAIL [empty enumeration] could not create the fixture repo -- the"
    echo "       control did not run, which is not a pass."
    st_fail=1
  fi
  # This script resolves REPO_ROOT from its OWN location, so it must be copied
  # into the fixture for that resolution to land on the empty tree.
  cp "$REPO_ROOT/scripts/ruff-gate.sh" "$st_empty/scripts/ruff-gate.sh"
  set +e
  ( cd "$st_empty" && PATH="$st_tmp/bin:$PATH" bash scripts/ruff-gate.sh ) >/dev/null 2>&1
  st_got=$?
  set -e
  if [ "$st_got" -ne 2 ]; then
    echo "  FAIL [empty enumeration] expected exit 2 (UNEVALUATED), got $st_got."
    echo "       A scan of zero files is not a clean tree."
    st_fail=1
  else
    echo "  ok   [empty enumeration] zero tracked *.py -> exit 2, not a pass"
  fi

  st_case "clean"                0   0
  st_case "real findings"        1   1
  st_case "could-not-process"    2   2
  st_case "SIGKILL (OOM shape)"  137 2
  st_case "SIGTERM"              143 2

  if [ "$st_fail" -ne 0 ]; then
    echo "ruff-gate.sh self-test FAILED" >&2
    exit 1
  fi
  echo "OK - self-test: findings (1), a killed/failed linter (2), and an empty enumeration (2) are all distinct from a clean pass (0)."
  exit 0
fi

if ! command -v ruff >/dev/null 2>&1; then
  echo "::error::ruff not installed." >&2
  echo "  pipx install ruff   (or)   python3 -m pip install --user ruff" >&2
  exit 1
fi

# Collect every tracked *.py. git ls-files is the source of truth -- the SAME
# corpus ci.sh gate (a) py_compiles, so the two cannot disagree about what
# "every tracked *.py" means. It excludes .git/, node_modules/ and untracked
# cruft for free, and -z is NUL-delimited so paths with spaces / emoji survive.
# Built bash-3.2-safe (macOS ships bash 3.2): no `mapfile -d`, just read+append.
files=()
while IFS= read -r -d '' f; do
  files+=("$f")
done < <(git ls-files -z -- '*.py')

# Empty-array expansion under `set -u` errors on bash 3.2 / 4.3; guard it.
if [ "${#files[@]}" -eq 0 ]; then
  # REFUSE, do not pass. This repo tracks ~400 *.py, so an empty enumeration
  # means the SELECTION broke -- a bad pathspec, a detached/empty checkout, a
  # `git ls-files` that failed -- never that the work is clean. Exiting 0 here
  # would report a clean lint over a scan that never happened, the same shape as
  # the killed-linter case below: nothing ran, and the caller was told it was
  # fine. Exit 2 = could-not-evaluate, distinct from 1 = real findings.
  echo "UNEVALUATED: zero tracked *.py found. This repo tracks many, so an" >&2
  echo "empty file list means the SELECTION failed, not that the tree is" >&2
  echo "clean. Nothing was linted -- this is not a pass." >&2
  exit 2
fi

echo "==> ruff --select $SELECT over ${#files[@]} tracked *.py  [$(ruff --version | awk '{print $2}')]"

# GitHub Actions renders one annotation per finding from the github format; an
# interactive run gets ruff's readable default.
fmt="full"
[ -n "${GITHUB_ACTIONS:-}" ] && fmt="github"

# Classify ruff's status instead of treating every non-zero as "found issues".
# ruff documents 0 = clean and 1 = findings; anything else means the TOOL did
# not deliver a verdict -- 2 for a CLI/config error, and 128+N when the kernel
# killed it (137 = SIGKILL, the OOM shape on a loaded runner). Reporting a
# signal death as a lint finding sends the reader hunting a defect that does not
# exist; the adjacent shape is worse -- a "no findings" line over a run that
# never linted anything.
set +e
ruff check --select "$SELECT" --no-cache --output-format="$fmt" -- "${files[@]}"
rf_rc=$?
set -e

if [ "$rf_rc" -eq 0 ]; then
  echo "    OK - no ruff findings at --select $SELECT"
elif [ "$rf_rc" -eq 1 ]; then
  echo "FAILED: ruff found issues at --select $SELECT." >&2
  echo "These are undefined names, syntax errors, and always-true asserts --" >&2
  echo "runtime bugs, not style. Fix them, or for a genuine false-positive add" >&2
  echo "a '# noqa: <rule>' comment with a one-line reason at the source line." >&2
  echo "Do NOT narrow the select to hide a finding (see this script's header)." >&2
  exit 1
elif [ "$rf_rc" -gt 128 ]; then
  echo "UNEVALUATED: ruff was KILLED by signal $((rf_rc - 128)) (exit $rf_rc)." >&2
  echo "It did not finish, so NOTHING was linted -- this is not a finding and it" >&2
  echo "is not a pass. On a loaded runner this is usually the OOM killer." >&2
  exit 2
else
  echo "UNEVALUATED: ruff exited $rf_rc, which is neither clean (0) nor findings" >&2
  echo "(1). It could not run -- a bad --select, a config error, a missing file --" >&2
  echo "so the scan is incomplete and its silence means nothing." >&2
  exit 2
fi
