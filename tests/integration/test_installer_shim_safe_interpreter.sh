#!/usr/bin/env bash
# CI lock: the installer bakes a shim-safe ABSOLUTE interpreter into hook
# commands, so a refuse-shim can't turn the vault's bare-python3 hooks into
# silent no-ops.
#
# The trailofbits `modern-python` plugin prepends a PATH shim for
# `python3`/`python` (SessionStart, via CLAUDE_ENV_FILE) that prints
# "ERROR: use uv run python3" and exit-1s on every bare invocation. Every
# ai-brain-starter hook command used to call bare `python3 X 2>/dev/null ||
# echo ...`, so with the shim active the ENTIRE hook layer — session close, the
# write-time secret guard, context loaders, aggregators — silently no-opped.
# hooks.json now uses a [PYTHON] token that install-hooks-user-level.py resolves
# to an absolute real interpreter (_posix_python), bypassing PATH entirely.
#
# Asserts, by running the REAL installer with a fake refuse-shim FIRST on PATH:
#   0. NEGATIVE CONTROL: bare `python3` under that PATH genuinely refuses.
#   1. NO ABS-owned hook command invokes bare `python3`/`python`.
#   2. The baked interpreter is absolute and is NOT the shim.
#   3. END-TO-END: that interpreter executes under the hostile PATH.
#
# Stdlib python3 + bash only. No network, no git. Tmpdir removed on exit.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTALLER="$REPO_ROOT/scripts/install-hooks-user-level.py"
# HOME alone does not sandbox the installer on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$SCRIPT_DIR/lib/sandbox_home.sh"

PASS=0; FAIL=0
TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT
ok()  { PASS=$((PASS + 1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL  $1 :: $2"; }

# A real interpreter to LAUNCH the installer with (absolute, never the shim) —
# bare `python3` would hit the shim we are about to put on PATH.
LAUNCH_PY=""
for c in /opt/homebrew/bin/python3 /usr/bin/python3 /usr/local/bin/python3; do
  [ -x "$c" ] && "$c" -c 'import sys' >/dev/null 2>&1 && { LAUNCH_PY="$c"; break; }
done
[ -z "$LAUNCH_PY" ] && LAUNCH_PY="$(command -v python3 || true)"
[ -z "$LAUNCH_PY" ] && { echo "SKIP: no real python3 to launch installer"; exit 0; }

# Fake refuse-shim mimicking trailofbits modern-python, under a */hooks/shims
# dir so it looks exactly like the real one to the resolver's skip logic.
SHIM="$TMP/plugins/trailofbits/modern-python/1.5.0/hooks/shims"
mkdir -p "$SHIM"
cat > "$SHIM/python3" <<'SH'
#!/usr/bin/env bash
echo "ERROR: Use \`uv run python3\` instead" >&2
exit 1
SH
cp "$SHIM/python3" "$SHIM/python"
chmod +x "$SHIM/python3" "$SHIM/python"
HOSTILE_PATH="$SHIM:$PATH"

mkdir -p "$TMP/.claude"
echo '{}' > "$TMP/.claude/settings.json"
SETTINGS="$TMP/.claude/settings.json"

# Run the REAL installer with the shim FIRST on PATH. It must resolve [PYTHON]
# to a real interpreter that skips the shim.
run_sandboxed "$TMP" env -u CLAUDECODE PATH="$HOSTILE_PATH" \
  "$LAUNCH_PY" "$INSTALLER" --hooks-source "$REPO_ROOT/hooks.json" --quiet >/dev/null 2>&1

echo "=== 0. NEGATIVE CONTROL: fake shim genuinely refuses bare python3 ==="
if env -u CLAUDECODE PATH="$HOSTILE_PATH" bash -c 'python3 -c "print(1)"' >/dev/null 2>&1; then
  bad "shim refuses" "fake shim did NOT refuse — test setup is broken"
else
  ok "fake shim refuses bare python3"
fi

echo "=== 1. no ABS-owned hook command invokes bare python3/python ==="
bare="$("$LAUNCH_PY" - "$SETTINGS" <<'PY'
import json, sys, re
h = json.load(open(sys.argv[1])).get("hooks", {})
viol = []
for ev, blocks in h.items():
    for blk in blocks:
        for e in blk.get("hooks", []):
            cmd = e.get("command", "")
            if "ai-brain-starter" not in cmd:
                continue
            # First token of any pipeline/list segment being bare python3/python
            # means an un-substituted interpreter that the shim would intercept.
            for seg in re.split(r"\|\||&&|[|;&]", cmd):
                toks = seg.strip().split()
                if toks and toks[0] in ("python3", "python"):
                    viol.append(cmd[:70])
                    break
print("\n".join(viol))
PY
)"
if [ -z "$bare" ]; then ok "no bare python3 in ABS-owned commands"; else bad "bare python3 present" "$bare"; fi

echo "=== 2. baked interpreter is absolute + not a shim ==="
INTERP=$("$LAUNCH_PY" - "$SETTINGS" <<'PY'
import json, sys
h = json.load(open(sys.argv[1])).get("hooks", {})
found = ""
for ev, blocks in h.items():
    for blk in blocks:
        for e in blk.get("hooks", []):
            cmd = e.get("command", "")
            if "ai-brain-starter" not in cmd:
                continue
            for tok in cmd.split():
                if tok.startswith("/") and tok.rsplit("/", 1)[-1] in ("python3", "python"):
                    found = tok
                    break
            if found:
                break
        if found:
            break
    if found:
        break
print(found)
PY
)
echo "   interpreter: ${INTERP:-<none>}"
case "${INTERP:-}" in
  */hooks/shims/*) bad "interp not shim" "$INTERP is a shim" ;;
  /*/python3|/*/python) ok "absolute non-shim interpreter" ;;
  *) bad "interp absolute" "got [${INTERP:-<none>}]" ;;
esac

echo "=== 3. baked interpreter executes under the hostile PATH ==="
if [ -n "${INTERP:-}" ] && \
   [ "$(env -u CLAUDECODE PATH="$HOSTILE_PATH" "$INTERP" -c 'print(42)' 2>/dev/null)" = "42" ]; then
  ok "baked interpreter runs under shim-first PATH"
else
  bad "interp runs" "interpreter did not execute under hostile PATH"
fi

echo
echo "=== summary: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
