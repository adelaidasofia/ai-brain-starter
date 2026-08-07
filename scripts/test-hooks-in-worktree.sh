#!/usr/bin/env bash
# test-hooks-in-worktree.sh — verify hooks fire inside a git worktree.
#
# Closes mycelium-hq/ai-brain-starter#6 verification path. Creates a
# temporary git repo, adds ai-brain-starter hooks at project level, creates
# a worktree, then simulates UserPromptSubmit input from inside the worktree
# and checks the hook responds.
#
# This script is the regression test that proves the user-level install fix
# actually solves the worktree-firing problem.
#
# Usage:
#   bash scripts/test-hooks-in-worktree.sh
#   bash scripts/test-hooks-in-worktree.sh --verbose
#
# Exits 0 on pass, 1 on fail. CI-runnable.

set -uo pipefail

VERBOSE=0
[[ "${1:-}" == "--verbose" ]] && VERBOSE=1

ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
fail() { printf "  \033[31m✗\033[0m %s\n" "$*"; }
log()  { [[ "$VERBOSE" -eq 1 ]] && printf "  · %s\n" "$*"; }
hdr()  { printf "\n\033[1m%s\033[0m\n" "$*"; }

PASS=0
FAIL=0
SKIP=0

# === setup ===

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

REPO="$TMP/test-vault"
mkdir -p "$REPO"
cd "$REPO" || exit 1
git init -b main --quiet 2>/dev/null
git config user.email "test@example.com"
git config user.name "test"
echo "# Test vault" > README.md
git add README.md
git commit --quiet -m "init" 2>/dev/null

# Locate the detector hook (test target)
DETECTOR=""
for candidate in \
  "$HOME/.claude/skills/ai-brain-starter/hooks/detect-closing-signal.py" \
  "$HOME/Desktop/ai-brain-starter/hooks/detect-closing-signal.py" \
  "$(dirname "$0")/../hooks/detect-closing-signal.py"; do
  if [[ -f "$candidate" ]]; then
    DETECTOR="$candidate"
    break
  fi
done

if [[ -z "$DETECTOR" ]]; then
  fail "detect-closing-signal.py not found in any expected location"
  exit 1
fi
log "Using detector: $DETECTOR"

# === Test 1: hook fires from MAIN worktree (sanity) ===

hdr "Test 1: hook fires from main worktree"

cd "$REPO" || exit 1
RESPONSE=$(echo '{"prompt":"bye","session_id":"test-1","cwd":"'"$REPO"'"}' \
  | python3 "$DETECTOR" 2>/dev/null)
if echo "$RESPONSE" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); assert 'hookSpecificOutput' in d; assert 'CLOSE detected' in d.get('hookSpecificOutput',{}).get('additionalContext','')" 2>/dev/null; then
  ok "main worktree: detector responded with cascade injection"
  PASS=$((PASS+1))
else
  fail "main worktree: detector did not respond as expected"
  log "Response: $RESPONSE"
  FAIL=$((FAIL+1))
fi

# === Test 2: hook fires from a CHILD worktree ===

hdr "Test 2: hook fires from inside .claude/worktrees/<name>/"

mkdir -p .claude/worktrees
git worktree add .claude/worktrees/test-wt --quiet 2>/dev/null || {
  log "git worktree add failed — skipping test 2"
  SKIP=$((SKIP+1))
}

if [[ -d ".claude/worktrees/test-wt" ]]; then
  cd .claude/worktrees/test-wt || exit 1
  WORKTREE_PWD="$(pwd)"
  log "Worktree cwd: $WORKTREE_PWD"

  RESPONSE=$(echo '{"prompt":"bye","session_id":"test-2","cwd":"'"$WORKTREE_PWD"'"}' \
    | python3 "$DETECTOR" 2>/dev/null)
  if echo "$RESPONSE" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); assert 'hookSpecificOutput' in d; assert 'CLOSE detected' in d.get('hookSpecificOutput',{}).get('additionalContext','')" 2>/dev/null; then
    ok "worktree: detector responded with cascade injection"
    PASS=$((PASS+1))
  else
    fail "worktree: detector did not respond from inside a worktree"
    log "Response: $RESPONSE"
    FAIL=$((FAIL+1))
  fi

  # Verify worktree name was correctly derived (regex-tolerant: any whitespace)
  RESPONSE=$(echo '{"prompt":"bye","session_id":"test-3","cwd":"'"$WORKTREE_PWD"'"}' \
    | python3 "$DETECTOR" 2>/dev/null)
  if echo "$RESPONSE" | python3 -c "
import json, sys, re
d = json.loads(sys.stdin.read())
ctx = d.get('hookSpecificOutput', {}).get('additionalContext', '')
m = re.search(r'Worktree:\s+(\S+)', ctx)
assert m, f'Worktree line not found in ctx[:300]: {ctx[:300]}'
assert m.group(1) == 'test-wt', f'Expected worktree=test-wt, got {m.group(1)!r}'
" 2>/dev/null; then
    ok "worktree name correctly derived: 'test-wt'"
    PASS=$((PASS+1))
  else
    fail "worktree name not derived correctly"
    FAIL=$((FAIL+1))
  fi
fi

# === Test 3: installer is idempotent + non-destructive ===

hdr "Test 3: installer preserves non-ABS hooks"

cd "$TMP" || exit 1
cat > test-settings.json <<'EOF'
{
  "model": "opus",
  "hooks": {
    "UserPromptSubmit": [
      {"hooks": [{"type":"command","command":"echo 'user custom hook' && true"}]}
    ]
  }
}
EOF

INSTALLER=""
for candidate in \
  "$HOME/.claude/skills/ai-brain-starter/scripts/install-hooks-user-level.py" \
  "$HOME/Desktop/ai-brain-starter/scripts/install-hooks-user-level.py" \
  "$(dirname "$0")/install-hooks-user-level.py"; do
  if [[ -f "$candidate" ]]; then
    INSTALLER="$candidate"
    break
  fi
done

if [[ -z "$INSTALLER" ]]; then
  fail "install-hooks-user-level.py not found"
  FAIL=$((FAIL+1))
else
  HOOKS_SOURCE=""
  for candidate in \
    "$HOME/.claude/skills/ai-brain-starter/hooks.json" \
    "$HOME/Desktop/ai-brain-starter/hooks.json"; do
    [[ -f "$candidate" ]] && HOOKS_SOURCE="$candidate" && break
  done

  if [[ -z "$HOOKS_SOURCE" ]]; then
    fail "hooks.json source not found"
    FAIL=$((FAIL+1))
  else
    python3 "$INSTALLER" --settings test-settings.json --hooks-source "$HOOKS_SOURCE" --quiet >/dev/null 2>&1

    if python3 -c "import json,sys; d=json.load(open('test-settings.json')); cmds=[h['command'] for g in d['hooks']['UserPromptSubmit'] for h in g.get('hooks',[])]; assert any('user custom hook' in c for c in cmds), 'user hook lost'" 2>/dev/null; then
      ok "installer preserved user's custom hook"
      PASS=$((PASS+1))
    else
      fail "installer destroyed user's custom hook"
      FAIL=$((FAIL+1))
    fi

    if python3 -c "import json; d=json.load(open('test-settings.json')); cmds=[h['command'] for g in d['hooks'].get('UserPromptSubmit',[]) for h in g.get('hooks',[])]; assert any('detect-closing-signal' in c for c in cmds), 'ABS hook missing'" 2>/dev/null; then
      ok "installer added ai-brain-starter hooks"
      PASS=$((PASS+1))
    else
      fail "installer did not add ai-brain-starter hooks"
      FAIL=$((FAIL+1))
    fi

    # Re-run for idempotency
    BEFORE_HASH=$(python3 -c "import hashlib; print(hashlib.sha256(open('test-settings.json','rb').read()).hexdigest())")
    python3 "$INSTALLER" --settings test-settings.json --hooks-source "$HOOKS_SOURCE" --quiet >/dev/null 2>&1
    AFTER_HASH=$(python3 -c "import hashlib; print(hashlib.sha256(open('test-settings.json','rb').read()).hexdigest())")
    if [[ "$BEFORE_HASH" == "$AFTER_HASH" ]]; then
      ok "installer is idempotent (re-run produced identical file)"
      PASS=$((PASS+1))
    else
      fail "installer is not idempotent (file changed on second run)"
      FAIL=$((FAIL+1))
    fi
  fi
fi

# === summary ===

hdr "Summary"
printf "  \033[1m%d pass · %d fail · %d skip\033[0m\n" "$PASS" "$FAIL" "$SKIP"

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
