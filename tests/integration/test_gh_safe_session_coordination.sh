#!/usr/bin/env bash
# Negative controls for scripts/gh-safe.py (MYC-3527): external shared-state
# mutations (gh pr create / gh pr merge) are session-attributed, read-before-
# write, and serialized by a repo+branch advisory lock.
#
# What this proves, in order:
#   1. No session id -> fail LOUD (exit 2) before any gh call.
#   2. Attribution: the created PR body carries the `session: <id>` trailer
#      (both --body and --body-file paths; the caller's file is not mutated).
#   3. Read-before-write: an existing open PR for the branch -> refuse (exit 4),
#      zero create calls.
#   4. THE negative control: two concurrent creates for the same repo+branch ->
#      exactly one proceeds, the other YIELDS (exit 3) naming the holder, and
#      the stub gh logs exactly ONE create. Deterministic by construction: the
#      stub blocks inside `pr create` until the test drops a release file, so
#      the loser provably overlaps the winner (no sleep races).
#   5. TOCTOU closed: a create attempted AFTER the winner finishes is refused
#      by the read INSIDE the lock (exit 4), not raced.
#   6. A stale lock (ancient created_at + dead owner pid) is reclaimed, not
#      wedged forever.
#   7. Merge: refuses non-OPEN PRs (exit 4, no merge call); on OPEN it posts a
#      claim comment carrying the session trailer BEFORE merging.
#
# gh is PATH-shimmed with a stub - this suite NEVER talks to real GitHub.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
GH_SAFE="$REPO_ROOT/scripts/gh-safe.py"

# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$SCRIPT_DIR/lib/sandbox_home.sh"

PASS=0
FAIL=0
TMPDIRS=()
cleanup() { for d in "${TMPDIRS[@]:-}"; do [ -n "$d" ] && rm -rf "$d"; done; }
trap cleanup EXIT
ok()  { PASS=$((PASS+1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL  $1 :: $2"; }
assert_rc() { [ "$RC" = "$2" ] && ok "$1" || bad "$1" "rc=$RC want $2 (err=${ERR:0:120})"; }

TMP="$(mktemp -d)"; TMPDIRS+=("$TMP")
sandbox_home "$TMP"   # nothing here may touch the real ~/.claude (MYC-3536)

# --- PATH-shimmed fake gh (never hits the network) ---------------------------
# Env contract: GH_STUB_LOG (append one line per invocation), GH_STUB_STATE
# (marker dir; `pr create` records the PR so later `pr list` sees it),
# GH_STUB_EXISTING_PR (force `pr list` to report an existing PR),
# GH_STUB_VIEW_STATE (state `pr view` reports; default OPEN),
# GH_STUB_CREATE_MAX_WAIT (seconds `pr create` blocks waiting for the
# release file - the determinism barrier for the concurrency control).
BIN="$TMP/bin"; mkdir -p "$BIN"
cat > "$BIN/gh" <<'PYEOF'
#!/usr/bin/env python3
import json, os, sys, time

LOG = os.environ.get("GH_STUB_LOG", "")
STATE = os.environ.get("GH_STUB_STATE", "")

def arg_after(flag):
    a = sys.argv
    for i, t in enumerate(a):
        if t == flag and i + 1 < len(a):
            return a[i + 1]
        if t.startswith(flag + "="):
            return t.split("=", 1)[1]
    return None

if LOG:
    with open(LOG, "a", encoding="utf-8") as f:
        # one log line per invocation: escape newlines in multi-line arg values
        f.write(" ".join(sys.argv[1:]).replace("\n", "\\n") + "\n")

sub = " ".join(sys.argv[1:3])
if sub == "pr list":
    head = arg_after("--head") or ""
    if os.environ.get("GH_STUB_EXISTING_PR"):
        print(json.dumps([{"number": 7, "url": os.environ["GH_STUB_EXISTING_PR"]}]))
        sys.exit(0)
    marker = os.path.join(STATE, "pr-" + head + ".json") if STATE else ""
    if marker and os.path.exists(marker):
        with open(marker, "r", encoding="utf-8") as f:
            print(f.read())
        sys.exit(0)
    print("[]")
    sys.exit(0)
if sub == "pr create":
    if STATE:
        open(os.path.join(STATE, "create-started"), "w").close()
        wait = float(os.environ.get("GH_STUB_CREATE_MAX_WAIT", "0") or 0)
        deadline = time.time() + wait
        release = os.path.join(STATE, "release")
        while time.time() < deadline and not os.path.exists(release):
            time.sleep(0.05)
    body = arg_after("--body") or ""
    bf = arg_after("--body-file")
    if bf:
        with open(bf, "r", encoding="utf-8") as f:
            body = f.read()
    head = arg_after("--head") or "feature-x"
    if STATE:
        with open(os.path.join(STATE, "last-create-body.txt"), "w", encoding="utf-8") as f:
            f.write(body)
        with open(os.path.join(STATE, "pr-" + head + ".json"), "w", encoding="utf-8") as f:
            json.dump([{"number": 7, "url": "https://github.example/acme/demo/pull/7"}], f)
    print("https://github.example/acme/demo/pull/7")
    sys.exit(0)
if sub == "pr view":
    print(json.dumps({
        "state": os.environ.get("GH_STUB_VIEW_STATE", "OPEN"),
        "url": "https://github.example/acme/demo/pull/7",
        "headRefName": "feature-x",
        "number": 7,
    }))
    sys.exit(0)
if sub == "pr comment":
    if STATE:
        with open(os.path.join(STATE, "last-comment-body.txt"), "w", encoding="utf-8") as f:
            f.write(arg_after("--body") or "")
    print("https://github.example/acme/demo/pull/7#issuecomment-1")
    sys.exit(0)
if sub == "pr merge":
    sys.exit(0)
sys.exit(0)
PYEOF
chmod +x "$BIN/gh"

# --- a throwaway git repo with an origin remote and a feature branch ---------
DEMO="$TMP/demo"; mkdir -p "$DEMO"
(
  cd "$DEMO" \
    && git init -q \
    && git config user.email t@example.com \
    && git config user.name t \
    && git commit -q --allow-empty -m init \
    && git checkout -q -b feature-x \
    && git remote add origin https://github.example/acme/demo.git
) || { echo "FATAL: could not build the demo repo"; exit 1; }

LOG="$TMP/gh.log"; STATE="$TMP/state"; LOCKS="$TMP/locks"
reset_state() { rm -rf "$STATE" "$LOCKS" "$LOG"; mkdir -p "$STATE" "$LOCKS"; : > "$LOG"; }

# gs [VAR=val ...] -- <gh-safe args...>  (CLAUDE_SESSION_ID never leaks in
# from the caller's environment; a case must set it explicitly to have one)
gs() {
  ENVS=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --) shift; break ;;
      *=*) ENVS+=("$1"); shift ;;
      *) break ;;
    esac
  done
  (
    cd "$DEMO" && env -u CLAUDE_SESSION_ID \
      PATH="$BIN:$PATH" GH_STUB_LOG="$LOG" GH_STUB_STATE="$STATE" \
      GH_SAFE_LOCK_DIR="$LOCKS" ${ENVS[@]+"${ENVS[@]}"} \
      python3 "$GH_SAFE" "$@"
  )
}
run_gs() { OUT="$(gs "$@" 2>"$TMP/err")"; RC=$?; ERR="$(cat "$TMP/err" 2>/dev/null)"; }
count_log() { grep -c "^$1" "$LOG" 2>/dev/null || true; }

echo "=== attribution is required (fail loud, before any gh call) ==="
reset_state
run_gs -- pr create --title T --body "adds X"
assert_rc "create with no session id exits 2" 2
case "$ERR" in *CLAUDE_SESSION_ID*) ok "error names CLAUDE_SESSION_ID" ;; *) bad "error names CLAUDE_SESSION_ID" "err=${ERR:0:120}" ;; esac
[ ! -s "$LOG" ] && ok "no gh call happened" || bad "no gh call happened" "$(head -3 "$LOG")"

run_gs CLAUDE_SESSION_ID=sess-fill -- pr create --title T --fill
assert_rc "create with --fill and no body exits 2 (trailer cannot embed)" 2

run_gs CLAUDE_SESSION_ID=sess-x -- repo clone acme/demo
assert_rc "non-wrapped subcommand exits 2" 2

echo "=== happy-path create: read first, trailer embedded, lock released ==="
reset_state
run_gs CLAUDE_SESSION_ID=sess-alpha -- pr create --title T --body "adds X"
assert_rc "create succeeds" 0
case "$OUT" in */pull/7*) ok "gh output (PR URL) passes through" ;; *) bad "gh output (PR URL) passes through" "out=${OUT:0:120}" ;; esac
[ "$(count_log 'pr list')" = "1" ] && ok "read-before-write ran once" || bad "read-before-write ran once" "$(count_log 'pr list') list lines"
[ "$(count_log 'pr create')" = "1" ] && ok "exactly one create" || bad "exactly one create" "$(count_log 'pr create') create lines"
grep -q '^adds X' "$STATE/last-create-body.txt" 2>/dev/null \
  && ok "body keeps caller content" || bad "body keeps caller content" "$(cat "$STATE/last-create-body.txt" 2>/dev/null)"
grep -q '^session: sess-alpha$' "$STATE/last-create-body.txt" 2>/dev/null \
  && ok "body carries session trailer" || bad "body carries session trailer" "$(cat "$STATE/last-create-body.txt" 2>/dev/null)"
[ -z "$(ls -A "$LOCKS" 2>/dev/null)" ] && ok "lock released after create" || bad "lock released after create" "$(ls -A "$LOCKS")"

echo "=== read-before-write refuses when an open PR already exists ==="
reset_state
run_gs CLAUDE_SESSION_ID=sess-beta GH_STUB_EXISTING_PR=https://github.example/acme/demo/pull/41 -- pr create --title T --body B
assert_rc "create refuses on existing PR (exit 4)" 4
case "$ERR" in */pull/41*) ok "refusal prints the existing PR" ;; *) bad "refusal prints the existing PR" "err=${ERR:0:120}" ;; esac
[ "$(count_log 'pr create')" = "0" ] && ok "no create call on refusal" || bad "no create call on refusal" "$(count_log 'pr create')"

echo "=== NEGATIVE CONTROL: two concurrent creates, one yields ==="
reset_state
# Winner: blocks inside the stub's `pr create` (holding the lock) until the
# release file appears, so the loser provably races a HELD lock.
(
  cd "$DEMO" && env -u CLAUDE_SESSION_ID \
    PATH="$BIN:$PATH" GH_STUB_LOG="$LOG" GH_STUB_STATE="$STATE" \
    GH_SAFE_LOCK_DIR="$LOCKS" CLAUDE_SESSION_ID=sess-A GH_STUB_CREATE_MAX_WAIT=15 \
    python3 "$GH_SAFE" pr create --title T --body "adds X"
) >"$TMP/winner.out" 2>"$TMP/winner.err" &
WPID=$!
i=0
while [ "$i" -lt 200 ] && [ ! -f "$STATE/create-started" ]; do sleep 0.1; i=$((i+1)); done
if [ -f "$STATE/create-started" ]; then
  ok "winner reached gh pr create while holding the lock"
else
  bad "winner reached gh pr create while holding the lock" "timeout"
fi
LDIR="$(ls -d "$LOCKS"/gh-safe-*.lock 2>/dev/null | head -1)"
[ -n "$LDIR" ] && ok "lock dir exists while held" || bad "lock dir exists while held" "none under $LOCKS"
run_gs CLAUDE_SESSION_ID=sess-B -- pr create --title T --body "adds X"
assert_rc "loser yields with exit 3" 3
case "$ERR" in *YIELD*) ok "yield message says YIELD" ;; *) bad "yield message says YIELD" "err=${ERR:0:160}" ;; esac
case "$ERR" in *sess-A*) ok "yield names the holder session" ;; *) bad "yield names the holder session" "err=${ERR:0:160}" ;; esac
: > "$STATE/release"
wait "$WPID"; WRC=$?
[ "$WRC" = "0" ] && ok "winner completes with exit 0" || bad "winner completes with exit 0" "rc=$WRC err=$(head -c 160 "$TMP/winner.err")"
[ "$(count_log 'pr create')" = "1" ] && ok "exactly ONE create reached gh" || bad "exactly ONE create reached gh" "$(count_log 'pr create') create lines"
[ "$(count_log 'pr list')" = "1" ] && ok "loser yielded before even reading" || bad "loser yielded before even reading" "$(count_log 'pr list') list lines"

echo "=== TOCTOU closed: a late second create is refused, not raced ==="
# State still holds the winner's PR; the read INSIDE the lock must see it.
run_gs CLAUDE_SESSION_ID=sess-C -- pr create --title T --body "adds X"
assert_rc "late create refused via read inside the lock (exit 4)" 4
[ "$(count_log 'pr create')" = "1" ] && ok "still exactly one create" || bad "still exactly one create" "$(count_log 'pr create')"

echo "=== stale lock is reclaimed, not wedged ==="
reset_state
mkdir -p "$LDIR"
DEADPID="$(sh -c 'echo $$')"   # that shell has already exited
python3 - "$LDIR" "$DEADPID" <<'PYEOF'
import json, os, sys
lockdir, deadpid = sys.argv[1], int(sys.argv[2])
meta = {"session": "sess-ghost", "pid": deadpid, "host": os.uname().nodename
        if hasattr(os, "uname") else "otherhost",
        "op": "pr-create", "repo": "github.example/acme/demo",
        "branch": "feature-x", "created_at": 1000.0}
with open(os.path.join(lockdir, "owner.json"), "w", encoding="utf-8") as f:
    json.dump(meta, f)
os.utime(lockdir, (1000, 1000))
PYEOF
run_gs CLAUDE_SESSION_ID=sess-delta -- pr create --title T --body "adds X"
assert_rc "create reclaims the stale lock and proceeds" 0
case "$ERR" in *stale*) ok "reclaim is announced" ;; *) bad "reclaim is announced" "err=${ERR:0:120}" ;; esac

echo "=== --body-file: trailer embedded, caller file untouched ==="
reset_state
printf 'Original body text.\n' > "$TMP/body.md"
run_gs CLAUDE_SESSION_ID=sess-file -- pr create --title T --body-file "$TMP/body.md"
assert_rc "create with --body-file succeeds" 0
grep -q '^Original body text.$' "$STATE/last-create-body.txt" 2>/dev/null \
  && ok "body-file content preserved" || bad "body-file content preserved" "$(cat "$STATE/last-create-body.txt" 2>/dev/null)"
grep -q '^session: sess-file$' "$STATE/last-create-body.txt" 2>/dev/null \
  && ok "body-file gains session trailer" || bad "body-file gains session trailer" "$(cat "$STATE/last-create-body.txt" 2>/dev/null)"
grep -q 'session:' "$TMP/body.md" \
  && bad "caller's file is not mutated" "trailer leaked into caller file" \
  || ok "caller's file is not mutated"

echo "=== merge: claim comment carries the trailer; non-OPEN refused ==="
reset_state
run_gs CLAUDE_SESSION_ID=sess-m -- pr merge 7 --squash
assert_rc "merge of an OPEN PR succeeds" 0
SEQ="$(cut -d' ' -f1-2 "$LOG" | tr '\n' ',')"
[ "$SEQ" = "pr view,pr view,pr comment,pr merge," ] \
  && ok "merge order: view, re-view inside lock, claim comment, merge" \
  || bad "merge order: view, re-view inside lock, claim comment, merge" "seq=$SEQ"
grep -q '^session: sess-m$' "$STATE/last-comment-body.txt" 2>/dev/null \
  && ok "claim comment carries session trailer" || bad "claim comment carries session trailer" "$(cat "$STATE/last-comment-body.txt" 2>/dev/null)"

reset_state
run_gs CLAUDE_SESSION_ID=sess-n GH_STUB_VIEW_STATE=MERGED -- pr merge 7 --squash
assert_rc "merge of a non-OPEN PR is refused (exit 4)" 4
[ "$(count_log 'pr merge')" = "0" ] && ok "no merge call on refusal" || bad "no merge call on refusal" "$(count_log 'pr merge')"
run_gs CLAUDE_SESSION_ID=sess-n GH_STUB_VIEW_STATE=MERGED -- pr merge 7 --squash
[ "$(count_log 'pr comment')" = "0" ] && ok "no claim comment on refusal" || bad "no claim comment on refusal" "$(count_log 'pr comment')"

echo ""
echo "=== SUMMARY: $PASS passed, $FAIL failed ==="
[ "$FAIL" = 0 ]
