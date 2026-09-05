#!/usr/bin/env bash
# test_real_python_shim.sh
#
# lib/real_python.sh exists because a `python3` shim on PATH turns the whole
# suite red for a reason no test asserts. Every check below is a NEGATIVE
# control: the shim is planted and the helper must defeat it, or no shim exists
# and the helper must leave PATH alone. A helper that has never faced the thing
# it defeats is unproven, and one that meddles when nothing is wrong is a new
# hazard of its own.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$SCRIPT_DIR/lib/real_python.sh"

FAILED=0
pass() { printf '  PASS: %s\n' "$1"; }
fail() { printf '  FAIL: %s\n' "$1"; FAILED=1; }

echo "test_real_python_shim"

[ -f "$LIB" ] || { echo "  FAIL: $LIB not found"; exit 1; }

# A stand-in for the real plugin shim: named python3, on PATH, refuses to run.
plant_shim() {
    local d; d="$(mktemp -d)"
    cat > "$d/python3" <<'SH'
#!/bin/sh
echo "ERROR: Use \`uv run python3\` instead of \`python3\`" >&2
exit 1
SH
    chmod +x "$d/python3"
    printf '%s' "$d"
}

# A python3 that works, for the leg where the helper must do nothing.
plant_working() {
    local d r c; d="$(mktemp -d)"
    for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 \
             /usr/bin/python3 /opt/homebrew/bin/python3; do
        r="$(command -v "$c" 2>/dev/null)" || continue
        [ -n "$r" ] || continue
        if "$r" -c 'pass' >/dev/null 2>&1; then ln -s "$r" "$d/python3"; printf '%s' "$d"; return 0; fi
    done
    rm -rf "$d"; return 1
}

# --- 1. defeats a refusing shim ---------------------------------------------
SHIMDIR="$(plant_shim)"
out=$(
    PATH="$SHIMDIR:$PATH"
    if python3 -c 'pass' >/dev/null 2>&1; then echo "FIXTURE_INERT"; exit 0; fi
    # shellcheck source=tests/integration/lib/real_python.sh
    . "$LIB"
    ensure_real_python >/dev/null 2>&1 || { echo "HELPER_FAILED"; exit 0; }
    python3 -c 'print("ran")' 2>&1
)
case "$out" in
  ran)           pass "a refusing python3 shim on PATH is defeated" ;;
  FIXTURE_INERT) fail "fixture never shadowed python3 — this control proves nothing" ;;
  HELPER_FAILED) fail "helper found no working interpreter to fall back to" ;;
  *)             fail "helper did not yield a runnable python3: $out" ;;
esac
rm -rf "$SHIMDIR"

# --- 2. shadows python3 ONLY, not the interpreter's whole bin dir ------------
# Prepending /usr/bin would reorder git, sed, and everything else for the rest of
# the run — a far larger change than the defect warrants.
SHIMDIR="$(plant_shim)"
out=$(
    PATH="$SHIMDIR:$PATH"
    # shellcheck source=tests/integration/lib/real_python.sh
    . "$LIB"
    ensure_real_python >/dev/null 2>&1 || { echo "HELPER_FAILED"; exit 0; }
    ls "$REAL_PYTHON_SHIM_DIR" | tr '\n' ' '
)
if [ "$(printf '%s' "$out" | tr -d ' ')" = "python3" ]; then
    pass "the prepended dir holds python3 and nothing else"
else
    fail "prepended dir was not surgical, it holds: $out"
fi
rm -rf "$SHIMDIR"

# --- 3. no-op when python3 already runs -------------------------------------
if WORKDIR="$(plant_working)"; then
    out=$(
        PATH="$WORKDIR:$PATH"
        # shellcheck source=tests/integration/lib/real_python.sh
    . "$LIB"
        before="$PATH"
        ensure_real_python >/dev/null 2>&1
        [ "$PATH" = "$before" ] && echo UNCHANGED || echo CHANGED
    )
    [ "$out" = "UNCHANGED" ] && pass "leaves PATH untouched when python3 already runs" \
                             || fail "meddled with PATH on a healthy machine ($out)"
    rm -rf "$WORKDIR"
else
    fail "could not find any working interpreter to build the no-op control"
fi

[ $FAILED -eq 0 ] && echo "OK" || echo "FAILURES"
exit $FAILED
