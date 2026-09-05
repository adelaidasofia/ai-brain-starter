#!/usr/bin/env bash
# Guarantee that `python3` on PATH is an interpreter which actually runs.
#
# Some machines carry a `python3` shim that refuses a direct call and answers
# with advice instead of running: the trailofbits/modern-python plugin ships one
# that replies to every `python3 ...` with "Use `uv run python3 ...`". That is a
# defensible default for interactive work and ruinous here — 103 test files in
# this suite shell out to `python3`, so on a machine carrying the shim every one
# of them fails, and the failure text is the shim's advice rather than anything
# the test asserts. A whole red suite that says nothing about the code is worse
# than a broken test, because it trains you to stop reading the output.
#
# Sandboxing HOME does not help: the shim sits on PATH by absolute path and
# outlives the decoy home the suite installs.
#
# ensure_real_python is a no-op wherever `python3` already runs — CI, and most
# contributor machines. Where it does not, it prepends a directory holding a
# single `python3` symlink to a working interpreter. Only python3 is shadowed:
# prepending the interpreter's own directory (/usr/bin, say) would reorder every
# other tool on PATH for the rest of the run, which is a much larger promise than
# this needs to make.
#
# ci.sh calls it once for the whole suite. A single test run by hand opts in the
# same way:
#
#   . tests/integration/lib/real_python.sh && ensure_real_python
#
# On success PATH is exported and REAL_PYTHON_SHIM_DIR names the directory it
# created — empty when nothing was needed — so the caller can clean it up.

REAL_PYTHON_SHIM_DIR="${REAL_PYTHON_SHIM_DIR:-}"

ensure_real_python() {
    if python3 -c 'pass' >/dev/null 2>&1; then
        return 0
    fi

    # Versioned names and absolute paths are what escape a shim: it only ever
    # claims the bare `python3` and `python` names.
    local cand resolved real=''
    for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 \
                /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
        resolved="$(command -v "$cand" 2>/dev/null)" || continue
        [ -n "$resolved" ] || continue
        if "$resolved" -c 'pass' >/dev/null 2>&1; then
            real="$resolved"
            break
        fi
    done

    if [ -z "$real" ]; then
        echo "ensure_real_python: \`python3\` on PATH does not execute, and no working" >&2
        echo "  interpreter was found. Every test that shells out to python3 would fail" >&2
        echo "  for that reason alone, saying nothing about the code under test." >&2
        return 1
    fi

    REAL_PYTHON_SHIM_DIR="$(mktemp -d)" || return 1
    ln -s "$real" "$REAL_PYTHON_SHIM_DIR/python3" || return 1
    PATH="$REAL_PYTHON_SHIM_DIR:$PATH"
    export PATH
    echo "    note: \`python3\` on PATH does not execute (a shim); using $real for this run"
}
