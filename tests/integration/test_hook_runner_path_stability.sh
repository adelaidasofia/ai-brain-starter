#!/usr/bin/env bash
# The hook_runner.py path baked into settings.json must be STABLE (MYC-3536).
#
# THE BUG
#
# platformize_template_for_windows() wires every Windows hook as
#     py -3 "<abs>/scripts/hook_runner.py" --fallback silent "<abs>/hooks/<x>.py"
# and used to derive <abs> from Path(__file__).parent — i.e. whatever checkout
# happened to run the installer. Run it (or any test that invokes it) from a
# throwaway git worktree under $TMP and all ~95 hook entries point into that
# worktree. The path is then persisted in settings.json and outlives the process.
#
# When the worktree is deleted the launcher cannot open the runner. CPython exits
# 2 for "can't open file", and exit 2 is Claude Code's intentional-BLOCK signal —
# not "hook unavailable". So every tool call in every later session is DENIED,
# with nothing tying the failure back to the worktree that caused it. Observed
# live 2026-07-30: 95 of 111 entries pointed at four deleted temp worktrees.
# Same fail-closed class as #375 and #409.
#
# hook_runner.py already fails open when its TARGET is missing. It cannot defend
# against its own absence — it is not running. So the fix is upstream: never bake
# a disposable path in the first place.
#
# Asserts (unit-level, so it runs on Linux CI as well as Windows — the Windows
# rewrite itself is platform-gated, this resolution is not):
#   1. An installed ~/.claude/skills/.../scripts/hook_runner.py WINS over the
#      running checkout — the actual fix.
#   2. NEGATIVE CONTROL: with no installed copy, it falls back to the checkout,
#      so a first install from a dev tree still wires a runner that exists.
#   3. ABS_HOOK_RUNNER overrides both (hermetic-test escape hatch).
#   4. The resolved path never lands inside a scratchpad worktree when an
#      installed copy exists — the exact shape of the live incident.
#
# Stdlib python3 + bash only. Exit 0 = pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$SCRIPT_DIR/lib/sandbox_home.sh"
INSTALLER="$REPO_ROOT/scripts/install-hooks-user-level.py"

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL  $1 :: $2"; }

[ -f "$INSTALLER" ] || { echo "ERROR: installer missing at $INSTALLER" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# A fake "temp worktree" checkout, mirroring the layout that caused the incident.
WT="$TMP/scratchpad/wt-throwaway"
mkdir -p "$WT/scripts"
cp "$INSTALLER" "$WT/scripts/install-hooks-user-level.py"
cp "$REPO_ROOT/scripts/hook_runner.py" "$WT/scripts/hook_runner.py"

# resolve <home> — prints _runner_path() with the installer loaded FROM $WT,
# so __file__ is the throwaway worktree, exactly as in the incident.
resolve() {
  run_sandboxed "$1" python3 - "$WT/scripts/install-hooks-user-level.py" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("ih", sys.argv[1])
ih = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ih)
print(ih._runner_path())
PY
}

echo "=== 1. an INSTALLED runner wins over the running checkout ==="
H1="$TMP/home_installed"
INSTALLED_DIR="$H1/.claude/skills/ai-brain-starter/scripts"
mkdir -p "$INSTALLED_DIR"
cp "$REPO_ROOT/scripts/hook_runner.py" "$INSTALLED_DIR/hook_runner.py"
GOT1="$(resolve "$H1")"
case "$GOT1" in
  *scratchpad*|*wt-throwaway*)
    bad "installed copy wins" "resolved into the throwaway worktree: $GOT1" ;;
  *ai-brain-starter*scripts*hook_runner.py)
    ok "resolved to the installed copy ($GOT1)" ;;
  *)
    bad "installed copy wins" "unexpected path: $GOT1" ;;
esac

echo "=== 2. NEGATIVE CONTROL: no installed copy -> falls back to the checkout ==="
H2="$TMP/home_bare"
mkdir -p "$H2/.claude"
GOT2="$(resolve "$H2")"
case "$GOT2" in
  *wt-throwaway*hook_runner.py)
    ok "fell back to the running checkout ($GOT2)" ;;
  *)
    bad "first-install fallback" "expected the checkout copy, got: $GOT2" ;;
esac

echo "=== 3. ABS_HOOK_RUNNER overrides both ==="
OVERRIDE="$TMP/custom/hook_runner.py"
mkdir -p "$TMP/custom"; : > "$OVERRIDE"
GOT3="$(ABS_HOOK_RUNNER="$OVERRIDE" resolve "$H1")"
# Compare separator-agnostically: on Git Bash, MSYS rewrites a POSIX path in an
# env var to its Windows spelling before a native python sees it, so the strings
# differ by separator and prefix while naming the same file.
if [ "${GOT3//\\//}" = "${OVERRIDE//\\//}" ] \
   || [ "$(cygpath -m "$OVERRIDE" 2>/dev/null)" = "${GOT3//\\//}" ]; then
  ok "ABS_HOOK_RUNNER honoured ($GOT3)"
else
  bad "ABS_HOOK_RUNNER" "expected [$OVERRIDE], got [$GOT3]"
fi

echo "=== 4. the resolved runner exists on disk ==="
# The whole failure mode is a wired path that is not there. Whatever we resolve
# to must be openable, or we have simply moved the fail-closed trigger.
if [ -f "$GOT1" ]; then
  ok "resolved runner exists on disk"
else
  bad "runner exists" "resolved to a path that does not exist: $GOT1"
fi

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
echo "PASS: hook_runner.py resolves to a stable installed path, never a disposable worktree (MYC-3536)"
