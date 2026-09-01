#!/usr/bin/env bash
#
# Integration test: warn-journal-saved-without-context.py resolves the vault root
# for the `cd <vault> && cat > "<relative journal path>"` save idiom.
#
# THE BUG (found 2026-08-30, /journal session)
#   daily-journal saves with the cwd-relative idiom:
#     cd "/Users/x/Brain" && cat > "📓 Journals/August 2026/entry.md" << 'EOF'
#   _vault_root()'s optional emoji-folder segment was `(?:[^/\n]*\s)?`, which
#   excludes only `/` and newline — NOT quotes or shell metacharacters. So it
#   happily swallowed `Brain" && cat > "📓 ` and the root collapsed one level to
#   the vault's PARENT (`/Users/x`).
#
#   That parent is a real directory, so the hook did NOT fail open. It blocked,
#   naming a marker path (`/Users/x/⚙️ Meta/.journal-context/<date>.json`) that
#   no preflight can ever create. The guard became UNSATISFIABLE: every save
#   needed JOURNAL_CONTEXT_BYPASS=1, which is exactly the "bypass becomes
#   routine" failure that voids the guard.
#
# WHY A TIGHTENED REGEX ALONE IS NOT THE FIX
#   Excluding quotes stops the WRONG root but leaves NO root (the journal path
#   is relative — there is no absolute root in it to find), so the hook fails
#   open and silently stops guarding the primary save path. Both halves are
#   asserted here: the bogus root must be gone AND the correct root must be
#   recovered from the `cd` target / payload cwd.
#
# Asserted against the shipped hook (module import + real stdin payloads), never
# a reimplementation. Every claim carries a control.
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
HOOK="$REPO_ROOT/hooks/warn-journal-saved-without-context.py"
PY="${PYTHON:-python3}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

VAULT="$TMP/Brain"
mkdir -p "$VAULT/📓 Journals/May 2026"
mkdir -p "$VAULT/⚙️ Meta/.journal-context"

failures=0
check() { # label got want
  if [ "$2" = "$3" ]; then printf 'PASS  %s\n' "$1"
  else printf 'FAIL  %s (got %s, want %s)\n' "$1" "$2" "$3"; failures=$((failures + 1)); fi
}

REL_CMD="cd \"$VAULT\" && cat > \"📓 Journals/May 2026/entry.md\" << 'EOF'
---
creationDate: 2026-05-04
---
body
EOF"

# --- layer 1: unit — the parser must not invent a root above the vault -------
"$PY" - "$REPO_ROOT" "$VAULT" <<'PYEOF'
import importlib.util, pathlib, sys

repo, vault = pathlib.Path(sys.argv[1]), sys.argv[2]
spec = importlib.util.spec_from_file_location(
    "journal_guard", repo / "hooks" / "warn-journal-saved-without-context.py")
m = importlib.util.module_from_spec(spec)
sys.modules["journal_guard"] = m
try:
    spec.loader.exec_module(m)
except SystemExit:
    pass

parent = str(pathlib.Path(vault).parent)
cmd = m._norm('cd "%s" && cat > "\U0001F4D3 Journals/May 2026/entry.md" << \'EOF\'' % vault)
fails = 0

def check(label, got, want):
    global fails
    if got == want:
        print("PASS  %s" % label)
    else:
        print("FAIL  %s (got %r, want %r)" % (label, got, want))
        fails += 1

check("gate opens on the relative journal path", bool(m.JOURNAL_PATH_RE.search(cmd)), True)
# The regression itself: the parent must never be returned as the vault root.
check("cd-relative save does NOT resolve to the vault's parent",
      m._vault_root(cmd) != parent, True)
# And the correct root must actually be recovered, not merely fail open.
check("cd-relative save resolves to the cd target",
      m._resolve_vault(cmd, None), vault)

# --- controls: absolute-path behaviour must be unchanged --------------------
check("CONTROL: absolute POSIX path still resolves",
      m._vault_root(m._norm("/Users/me/v/\U0001F4D3 Journals/May 2026/e.md")), "/Users/me/v")
check("CONTROL: absolute path wins over an unrelated cd target",
      m._resolve_vault(m._norm("cd /tmp && cat > '/Users/me/v/Journals/May 2026/e.md'"), "/nope"),
      "/Users/me/v")
check("CONTROL: no cd and no absolute path falls back to payload cwd",
      m._resolve_vault(m._norm('cat > "Journals/May 2026/e.md"'), "/some/cwd"), "/some/cwd")
# A wrapped/subshell cd must resolve too: falling back to cwd in a worktree
# session picks a root with no meta dir and blocks unsatisfiably again.
check("wrapped `bash -c 'cd ... && cat > ...'` resolves to the cd target",
      m._resolve_vault(m._norm(
          'bash -c \'cd "%s" && cat > "\U0001F4D3 Journals/May 2026/e.md"\'' % vault),
          "/worktree/cwd"), vault)
check("CONTROL: non-journal path does not open the gate",
      bool(m.JOURNAL_PATH_RE.search(m._norm("/Users/me/v/Notes/x.md"))), False)

sys.exit(1 if fails else 0)
PYEOF
check "unit layer" "$?" "0"

# --- layer 2: end-to-end — deny names a SATISFIABLE marker path -------------
payload() { "$PY" -c '
import json,sys
print(json.dumps({"tool_name":"Bash","hook_event_name":"PreToolUse",
                  "cwd":sys.argv[2],"tool_input":{"command":sys.argv[1]}}))' "$1" "$2"; }

# Decode the hook JSON rather than grepping raw stdout: json.dumps escapes the
# non-ASCII meta dir, so a literal grep for the marker path never matches even
# when the hook is perfectly correct.
reason() { "$PY" -c '
import json,sys
raw = sys.stdin.read().strip()
if not raw:
    print("__ALLOW__"); raise SystemExit
print(json.loads(raw)["hookSpecificOutput"]["permissionDecisionReason"])'; }

OUT="$(payload "$REL_CMD" "$VAULT" | JOURNAL_CONTEXT_BYPASS= "$PY" "$HOOK" | reason || true)"
check "marker absent -> denies" \
      "$(printf '%s' "$OUT" | grep -c '^BLOCKED by warn-journal-saved-without-context' || true)" "1"
check "deny names the real vault, not its parent" \
      "$(printf '%s' "$OUT" | grep -cF -- "$VAULT/⚙️ Meta/.journal-context/2026-05-04.json" || true)" "1"
check "deny does NOT name the vault's parent" \
      "$(printf '%s' "$OUT" | grep -cF -- "$TMP/⚙️ Meta" || true)" "0"
check "remediation names an absolute preflight path" \
      "$(printf '%s' "$OUT" | grep -cF -- "$VAULT/⚙️ Meta/scripts/journal-preflight.py" || true)" "1"
check "remediation uses a working interpreter (not bare python3)" \
      "$(printf '%s' "$OUT" | grep -c 'uv run python3' || true)" "1"

# The marker the message names must be the one that unblocks the save.
touch "$VAULT/⚙️ Meta/.journal-context/2026-05-04.json"
OUT2="$(payload "$REL_CMD" "$VAULT" | JOURNAL_CONTEXT_BYPASS= "$PY" "$HOOK" | reason || true)"
check "marker present -> allows (guard is satisfiable)" "$OUT2" "__ALLOW__"

# CONTROL: the guard still blocks a genuinely contextless save elsewhere.
OTHER="$TMP/Other"; mkdir -p "$OTHER/📓 Journals/May 2026" "$OTHER/⚙️ Meta"
OUT3="$(payload "cd \"$OTHER\" && cat > \"📓 Journals/May 2026/e.md\"
creationDate: 2026-05-04" "$OTHER" | JOURNAL_CONTEXT_BYPASS= "$PY" "$HOOK" | reason || true)"
check "CONTROL: unmarked vault still blocked" \
      "$(printf '%s' "$OUT3" | grep -c '^BLOCKED by warn-journal-saved-without-context' || true)" "1"

echo
if [ "$failures" -ne 0 ]; then echo "FAILED: $failures check(s)"; exit 1; fi
echo "OK: journal guard resolves cd-relative vault roots"
