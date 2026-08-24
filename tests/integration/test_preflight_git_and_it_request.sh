#!/usr/bin/env bash
# Test scripts/preflight.sh's git capability check + IT-authorization request
# block (MYC-3895).
#
# Bug: preflight.sh never checked git at all (a full-file grep for "git"
# returned only 3 incidental hits, none a presence/version probe), and there
# was no composed request a participant on a locked-down machine could hand
# to IT — only inline "ask IT" clauses with no package list, repo URL, or
# rationale to copy.
#
# Covered here:
#   1. preflight.sh is syntactically valid (bash -n).
#   2. have_working_git(): PRESENCE IS NOT CAPABILITY, same macOS CLT-stub
#      class bootstrap.sh's identically-named probe already guards against —
#      a git binary that exists on PATH and exits non-zero must read as
#      ABSENT, and a negative control proves a real git reads as WORKING (so
#      check 2 isn't passing for the wrong reason).
#   3. git ABSENT/broken -> reported as a WARNING, not a blocker (--json
#      mode: the message lands in lines.yellow, never lines.red) — the
#      archive-entry fetch already works without git (PR #488), so preflight
#      must not re-introduce it as a hard requirement. bootstrap.sh aborts
#      on any RED (see its own docstring), so a RED here would silently
#      re-break the zero-prerequisite promise.
#   4. git ABSENT/broken -> the composed IT-authorization request text
#      appears (the required negative control), naming: the repo URL, and
#      Git/Homebrew/Python/Node.js by name, in BOTH English and Spanish in
#      the same block (the cohort is bilingual).
#   5. NEGATIVE CONTROL for 4: with a WORKING git, the IT-request text does
#      NOT appear — proves it's conditionally gated on a real need, not
#      printed unconditionally.
#
# Self-contained; no network; never writes outside its tmpdir. Doesn't assert
# on preflight.sh's overall exit code or RED/YELLOW totals anywhere (a CI
# sandbox is legitimately red on unrelated checks — no Claude.app, possibly
# no network) — every assertion here is scoped to the git-specific message or
# the IT-request text specifically, so environmental noise elsewhere can't
# make this test flaky.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PREFLIGHT="$REPO_ROOT/scripts/preflight.sh"
[ -f "$PREFLIGHT" ] || { echo "ERROR: $PREFLIGHT not found" >&2; exit 1; }

fail() { echo "FAIL: $1" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ── 1. syntax ──
bash -n "$PREFLIGHT" || fail "1: preflight.sh has a syntax error"

# ── 2. capability probe, not presence probe (same class as bootstrap.sh's
# have_working_git — a macOS git stub with the CLT absent exists on PATH and
# still fails to run) ──
PROBE_BODY="$(awk '/^have_working_git\(\) \{/,/^}/' "$PREFLIGHT")"
[ -n "$PROBE_BODY" ] || fail "2: have_working_git() not found in preflight.sh"
printf '%s' "$PROBE_BODY" | grep -q 'git --version' \
  || fail "2: have_working_git must EXECUTE git (git --version), not just check presence"
printf '%s' "$PROBE_BODY" | grep -q 'run_with_timeout' \
  || fail "2: have_working_git must bound the probe — a CLT stub can open a GUI and hang"

BROKEN_GIT_DIR="$TMP/broken-git-bin"; mkdir -p "$BROKEN_GIT_DIR"
cat > "$BROKEN_GIT_DIR/git" <<'STUB'
#!/usr/bin/env bash
echo "xcode-select: note: No developer tools were found." >&2
exit 1
STUB
chmod +x "$BROKEN_GIT_DIR/git"

PROBE_HARNESS="$TMP/probe.sh"
{
  awk '/^run_with_timeout\(\) \{/,/^}/' "$PREFLIGHT"
  echo 'have() { command -v "$1" >/dev/null 2>&1; }'
  echo "$PROBE_BODY"
  echo 'if have_working_git; then echo WORKING; else echo ABSENT; fi'
} > "$PROBE_HARNESS"
STUB_RESULT="$(PATH="$BROKEN_GIT_DIR:$PATH" bash "$PROBE_HARNESS")"
[ "$STUB_RESULT" = "ABSENT" ] \
  || fail "2: a git stub that exits non-zero must read as ABSENT, got '$STUB_RESULT'"
# Negative control: a real git must read as WORKING, or the assertion above
# would pass for the wrong reason (e.g. a broken harness always prints ABSENT).
REAL_RESULT="$(bash "$PROBE_HARNESS")"
[ "$REAL_RESULT" = "WORKING" ] \
  || fail "2 (negative control): a real git must read as WORKING, got '$REAL_RESULT'"

# ── 3 + 4: git broken -> yellow (never red), and the IT-request text appears ──
# --json mode buckets messages by severity, so this is immune to whatever
# else is red/yellow in this environment (network, Claude Code, disk space).
JSON_OUT="$TMP/broken.json"
PATH="$BROKEN_GIT_DIR:$PATH" bash "$PREFLIGHT" --json > "$JSON_OUT" 2>/dev/null || true
python3 - "$JSON_OUT" <<'PY' || fail "3: git-broken JSON assertions failed (see stderr)"
import json, sys
data = json.load(open(sys.argv[1]))
yellow = "\n".join(data["lines"]["yellow"])
red = "\n".join(data["lines"]["red"])
if "git" not in yellow.lower():
    print("git message not found in lines.yellow: " + yellow, file=sys.stderr)
    sys.exit(1)
if "git" in red.lower():
    print("git message found in lines.red (must never block — the archive-entry "
          "fetch already works without git): " + red, file=sys.stderr)
    sys.exit(1)
PY

HUMAN_OUT="$TMP/broken.txt"
PATH="$BROKEN_GIT_DIR:$PATH" bash "$PREFLIGHT" > "$HUMAN_OUT" 2>&1 || true
assert_grep() { grep -qF "$2" "$1" || fail "$3 (pattern not found: $2)"; }
assert_grep "$HUMAN_OUT" "Copy-paste request for your IT team" "4: IT-request block header missing when git is broken"
assert_grep "$HUMAN_OUT" "https://github.com/mycelium-hq/ai-brain-starter" "4: IT-request block missing the repo URL"
assert_grep "$HUMAN_OUT" "Git (macOS: Xcode Command Line Tools" "4: IT-request block does not name Git specifically"
assert_grep "$HUMAN_OUT" "Homebrew" "4: IT-request block does not name Homebrew"
assert_grep "$HUMAN_OUT" "Python 3.10" "4: IT-request block does not name the Python version needed"
assert_grep "$HUMAN_OUT" "Node.js 18" "4: IT-request block does not name the Node version needed"
assert_grep "$HUMAN_OUT" "EN —" "4: IT-request block missing its English half"
assert_grep "$HUMAN_OUT" "ES —" "4: IT-request block missing its Spanish half (cohort is bilingual)"
assert_grep "$HUMAN_OUT" "instalar" "4: IT-request block's Spanish half doesn't look like Spanish"

# ── 5. NEGATIVE CONTROL: a WORKING git must NOT print the IT-request block ──
WORKING_GIT_DIR="$TMP/working-git-bin"; mkdir -p "$WORKING_GIT_DIR"
REAL_GIT="$(command -v git)"
ln -s "$REAL_GIT" "$WORKING_GIT_DIR/git"
WORKING_OUT="$TMP/working.txt"
PATH="$WORKING_GIT_DIR:$PATH" bash "$PREFLIGHT" > "$WORKING_OUT" 2>&1 || true
grep -qF "Copy-paste request for your IT team" "$WORKING_OUT" \
  && fail "5 (negative control): the IT-request block appeared even though git works — it must be conditional, not unconditional"
echo "PASS: negative control confirmed — a working git prints no IT-request block"

echo "PASS: test_preflight_git_and_it_request (5 checks, 2 negative controls)"
