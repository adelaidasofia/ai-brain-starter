#!/usr/bin/env bash
# Test the user-space Node + Python fallback in bootstrap.sh (MYC-3895).
#
# Bug (2026-08-12 cohort install session): on a corporate laptop with no admin
# rights there is no brew and no sudo. bootstrap.sh's only path was
# `brew install python@3.12 || err "..."` / `brew install node || err "..."` —
# both HARD-failed, and the install never recovered. Mirrors bootstrap.ps1's
# Node ZIP fallback: fetch an official, relocatable tarball into a user-owned
# prefix (~/.local/node, ~/.local/python), no elevation anywhere.
#
# Covered here:
#   1. bootstrap.sh is syntactically valid (bash -n).
#   2. have_sudo() answers "can I elevate WITHOUT a password prompt right now"
#      — false when sudo -n fails (the non-interactive case this whole bug is
#      about), true when it succeeds. Both directions, so a stub that always
#      returns true could not pass this silently.
#   3. add_user_path_entry() is idempotent: exporting PATH for this session AND
#      persisting to the user's shell rc file, without duplicating the line on
#      a second call.
#   4. MAIN SCENARIO: brew ABSENT, no sudo -> the real Python + Node sections of
#      bootstrap.sh (extracted verbatim, not reimplemented) land a WORKING
#      node and python3 in user space, PATH is wired, and err() is NEVER
#      called (asserted via a FAILED-array count, not string absence alone).
#   5. NEGATIVE CONTROL for 4: run the identical harness against the pre-fix
#      bootstrap.sh (git show origin/main:bootstrap.sh) and assert it DOES
#      call err() twice and DOES NOT land a working node (proves the harness
#      would have caught the bug it exists to catch, not just exercised dead
#      code). Skipped gracefully when origin/main is unavailable (no network).
#   6. Linux wiring: both sections gate their admin path on `is_linux &&
#      have_sudo`, and fall back to the same install_*_userspace() functions
#      structurally (not behaviorally re-simulated on this runner — 4 already
#      proves the functions themselves work; this proves Linux reaches them).
#
# Functions are extracted from the real bootstrap.sh with awk (never
# reimplemented), so this can't silently drift from the shipped source — same
# technique as test_bootstrap_archive_entry.sh's have_working_git/
# adopt_archive_install extraction. uname is stubbed to force darwin/arm64
# regardless of the actual test-runner OS (CI runs this suite on Linux — see
# test_bootstrap_brew_terminal_step.sh — so the Mac branch must be reachable
# deterministically, not conditionally skipped there).
#
# Self-contained; no network (curl is stubbed to serve local fixture
# tarballs); never writes outside its own sandboxed HOME.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
BOOTSTRAP="$REPO_ROOT/bootstrap.sh"
[ -f "$BOOTSTRAP" ] || { echo "ERROR: $BOOTSTRAP not found" >&2; exit 1; }

fail() { echo "FAIL: $1" >&2; exit 1; }

TMP="$(mktemp -d)"
sandbox_home "$TMP/realhome-guard"   # belt-and-braces; nothing here should ever touch it
trap 'rm -rf "$TMP"' EXIT

# ── 1. syntax ──
bash -n "$BOOTSTRAP" || fail "1: bootstrap.sh has a syntax error"

# ── extraction helper: one-liner functions have no standalone '}' line, so a
# plain awk range would swallow every function up to the NEXT one that does
# (reproduced while building this test: extracting have_sudo() alone pulled in
# quiet_retry() too). Try a single-line match first; fall back to the awk
# range for real multi-line functions. ──
extract_fn() {
  local name="$1" src="$2" oneliner
  oneliner="$(grep -E "^${name}\(\)[ ]*\{.*\}[ ]*\$" "$src" 2>/dev/null | head -1 || true)"
  if [ -n "$oneliner" ]; then printf '%s\n' "$oneliner"
  else awk "/^${name}\\(\\)[ ]*\\{/,/^}\$/" "$src"
  fi
}

for fn in have have_sudo is_mac is_linux hdr dry ok warn err log note_gap \
          user_space_platform add_user_path_entry fetch_tarball \
          install_node_userspace install_python_userspace; do
  [ -n "$(extract_fn "$fn" "$BOOTSTRAP")" ] || fail "setup: $fn() not found in bootstrap.sh"
done

# ── shared harness builder: stubs uname (force darwin/arm64 so the Mac branch
# is reachable regardless of the real CI runner OS), sudo (-n always fails —
# no cached/passwordless sudo), and curl (serves local fixture tarballs,
# keyed off the URL host, never touches the network). Fixture tarball layout
# matches the real releases exactly: nodejs.org unpacks to
# node-<ver>-<os>-<arch>/bin/node; python-build-standalone's *-install_only
# tarball unpacks to a bare python/bin/python3. ──
build_stubs() {
  local stubdir="$1"
  cat > "$stubdir/uname" <<'STUB'
#!/usr/bin/env bash
case "$1" in
  -s) echo "Darwin" ;;
  -m) echo "arm64" ;;
  *) command uname "$@" ;;
esac
STUB
  cat > "$stubdir/sudo" <<'STUB'
#!/usr/bin/env bash
exit 1
STUB
  # `have node` is naturally false everywhere (neither /usr/bin nor /bin ships
  # a node), so the Node branch triggers deterministically on any runner. The
  # Python guard is NOT naturally deterministic the same way: it checks the
  # ambient python3's VERSION, and that ambient version differs by platform
  # (macOS's /usr/bin/python3 stub is 3.9.x — always triggers the fallback;
  # Ubuntu, incl. GitHub Actions runners, ships 3.12+ by default — the guard
  # is satisfied WITHOUT installing anything, so the fallback branch this test
  # exists to prove never runs). Reproduced: this exact gap shipped green
  # locally and FAILED on the GitHub Actions ci-gate job for precisely this
  # reason. Stub python3 to fail the version assertion unconditionally, so
  # the fallback branch fires the same way regardless of the runner's real
  # ambient interpreter.
  cat > "$stubdir/python3" <<'STUB'
#!/usr/bin/env bash
[ "$1" = "-c" ] && exit 1
echo "Python 3.9.0"
STUB
  chmod +x "$stubdir/uname" "$stubdir/sudo" "$stubdir/python3"
}

build_fixtures() { # build_fixtures DEST_DIR -> writes node.tar.gz + python.tar.gz there
  local dest="$1" f
  f="$dest/build"
  mkdir -p "$f/node-v20.18.0-darwin-arm64/bin"
  printf '#!/usr/bin/env bash\necho "v20.18.0"\n' > "$f/node-v20.18.0-darwin-arm64/bin/node"
  chmod +x "$f/node-v20.18.0-darwin-arm64/bin/node"
  ( cd "$f" && tar -czf "$dest/node.tar.gz" "node-v20.18.0-darwin-arm64" )
  mkdir -p "$f/py/python/bin"
  printf '#!/usr/bin/env bash\necho "Python 3.12.7"\n' > "$f/py/python/bin/python3"
  chmod +x "$f/py/python/bin/python3"
  ( cd "$f/py" && tar -czf "$dest/python.tar.gz" "python" )
  rm -rf "$f"
}

build_curl_stub() { # build_curl_stub STUBDIR FIXTURE_DIR
  cat > "$1/curl" <<STUB
#!/usr/bin/env bash
out=""
while [ \$# -gt 0 ]; do
  case "\$1" in
    -o) out="\$2"; shift 2 ;;
    -*) shift ;;
    *) url="\$1"; shift ;;
  esac
done
case "\$url" in
  *nodejs.org*) cp "$2/node.tar.gz" "\$out" ;;
  *python-build-standalone*) cp "$2/python.tar.gz" "\$out" ;;
  *) exit 1 ;;
esac
STUB
  chmod +x "$1/curl"
}

# build_harness SRC_BOOTSTRAP OUT_FILE FAKE_HOME — assembles a runnable script
# from the REAL functions in SRC_BOOTSTRAP plus the actual Python/Node
# section bodies (extracted by their outer if/fi, identical anchor text in
# both the pre-fix and post-fix source, so this same builder proves both).
build_harness() {
  local src="$1" out="$2" fakehome="$3"
  {
    echo 'set -uo pipefail'
    echo "HOME=\"$fakehome\""
    echo "USERPROFILE=\"$fakehome\""   # Windows Python resolves ~ via USERPROFILE, not HOME (MYC-3536) — the
                                       # generated harness is bash-only today, but pairing it here matches every
                                       # other sandboxed HOME site in this suite and survives a future edit that
                                       # adds a python3 call.
    echo 'SHELL=/bin/bash'
    echo 'FAILED=()'
    echo 'LANG_CODE=en'
    echo 't() { [ "$LANG_CODE" = es ] && echo "$2" || echo "$1"; }'
    for fn in log ok warn err hdr dry is_mac is_linux have have_sudo; do
      extract_fn "$fn" "$src"
    done
    grep '^GAPS_FILE=' "$src" | sed "s|\$HOME|$fakehome|" || true
    extract_fn note_gap "$src"
    grep -E '^(NODE_FALLBACK_VERSION|PY_FALLBACK_VERSION|PY_FALLBACK_TAG)=' "$src" || true
    for fn in user_space_platform add_user_path_entry fetch_tarball install_node_userspace install_python_userspace; do
      extract_fn "$fn" "$src"
    done
    echo 'DRY_RUN=0'
    echo 'CORPORATE_PROFILE=0'   # inert on the post-fix code; required under set -u on the pre-fix code
    awk '/^if ! python3 -c "import sys; assert sys.version_info >= \(3,10\)" 2>\/dev\/null; then$/,/^fi$/' "$src"
    echo 'have python3 && ok "python3 $(python3 --version | awk "{print \$2}")"'
    awk '/^if ! have node; then$/,/^fi$/' "$src"
    echo 'have node && ok "node $(node --version)"'
    echo 'echo "RESULT_FAILED_COUNT=${#FAILED[@]}"'
    echo 'echo "RESULT_NODE=$(command -v node || echo NONE)"'
    echo 'echo "RESULT_PY=$(command -v python3 || echo NONE)"'
  } > "$out"
}

# ── 2. have_sudo(): both directions ──
HS_HARNESS="$TMP/have_sudo.sh"
{ extract_fn have_sudo "$BOOTSTRAP"; echo 'have_sudo && echo YES || echo NO'; } > "$HS_HARNESS"
NOSUDO_DIR="$TMP/nosudo"; mkdir -p "$NOSUDO_DIR"
printf '#!/usr/bin/env bash\nexit 1\n' > "$NOSUDO_DIR/sudo"; chmod +x "$NOSUDO_DIR/sudo"
[ "$(PATH="$NOSUDO_DIR:$PATH" bash "$HS_HARNESS")" = "NO" ] \
  || fail "2: have_sudo must be NO when sudo -n cannot elevate without a prompt"
YESSUDO_DIR="$TMP/yessudo"; mkdir -p "$YESSUDO_DIR"
printf '#!/usr/bin/env bash\n[ "$1" = "-n" ] && exit 0\nexit 1\n' > "$YESSUDO_DIR/sudo"; chmod +x "$YESSUDO_DIR/sudo"
[ "$(PATH="$YESSUDO_DIR:$PATH" bash "$HS_HARNESS")" = "YES" ] \
  || fail "2 (negative control): have_sudo must be YES when sudo -n CAN elevate — else check 2 passed for the wrong reason"

# ── 3. add_user_path_entry(): session PATH + idempotent rc persistence ──
AUPE_HOME="$TMP/aupe-home"; mkdir -p "$AUPE_HOME"
AUPE_HARNESS="$TMP/aupe.sh"
{
  echo "HOME=\"$AUPE_HOME\""
  echo "USERPROFILE=\"$AUPE_HOME\""   # paired per MYC-3536 (see build_harness above)
  echo 'SHELL=/bin/bash'
  extract_fn add_user_path_entry "$BOOTSTRAP"
  echo 'add_user_path_entry "/fake/tool/bin"'
  echo 'add_user_path_entry "/fake/tool/bin"'   # call twice: must not duplicate
  echo 'case ":$PATH:" in *":/fake/tool/bin:"*) echo "ON_PATH";; *) echo "NOT_ON_PATH";; esac'
} > "$AUPE_HARNESS"
AUPE_OUT="$(bash "$AUPE_HARNESS")"
[ "$AUPE_OUT" = "ON_PATH" ] || fail "3: add_user_path_entry did not export the dir onto PATH this session"
[ "$(grep -c '/fake/tool/bin' "$AUPE_HOME/.bashrc")" = "1" ] \
  || fail "3: add_user_path_entry must persist to the rc file exactly once even when called twice (idempotent)"

# ── 4. MAIN SCENARIO: brew absent, no sudo -> real node + python3, err() never called ──
POST_STUB="$TMP/post-stub"; mkdir -p "$POST_STUB"
POST_FIX="$TMP/post-fixtures"; mkdir -p "$POST_FIX"
build_stubs "$POST_STUB"
build_fixtures "$POST_FIX"
build_curl_stub "$POST_STUB" "$POST_FIX"
POST_HOME="$TMP/post-home"; mkdir -p "$POST_HOME"
POST_HARNESS="$TMP/post-harness.sh"
build_harness "$BOOTSTRAP" "$POST_HARNESS" "$POST_HOME"
POST_OUT="$(PATH="$POST_STUB:/usr/bin:/bin" bash "$POST_HARNESS" 2>&1)" || true
echo "$POST_OUT" | grep -q 'RESULT_FAILED_COUNT=0' \
  || fail "4: err() was called at least once with brew absent + no sudo — the install still dies here. Output:
$POST_OUT"
echo "$POST_OUT" | grep -q "RESULT_NODE=$POST_HOME/.local/node/bin/node" \
  || fail "4: node did not land at ~/.local/node in user space. Output:
$POST_OUT"
echo "$POST_OUT" | grep -q "RESULT_PY=$POST_HOME/.local/python/bin/python3" \
  || fail "4: python3 did not land at ~/.local/python in user space. Output:
$POST_OUT"
[ "$(PATH="$POST_STUB:/usr/bin:/bin" "$POST_HOME/.local/node/bin/node" --version)" = "v20.18.0" ] \
  || fail "4: the installed node is not actually runnable"
[ "$("$POST_HOME/.local/python/bin/python3" --version)" = "Python 3.12.7" ] \
  || fail "4: the installed python3 is not actually runnable"
grep -qF '.local/node/bin' "$POST_HOME/.bashrc" || fail "4: node's user-space bin was never persisted to PATH"
grep -qF '.local/python/bin' "$POST_HOME/.bashrc" || fail "4: python's user-space bin was never persisted to PATH"

# ── 5. NEGATIVE CONTROL: the identical harness against the PRE-FIX source
# must fail the way the bug actually failed (err() called, node absent) —
# proving check 4 exercises real logic, not a tautology.
#
# THE "BEFORE" SOURCE IS A FROZEN FIXTURE, NEVER A MOVING REF.
#
# This check used to read it with `git show origin/main:bootstrap.sh`. That was
# true exactly once: while the fix sat on a feature branch and origin/main still
# held the pre-fix code. The moment #502 merged, origin/main:bootstrap.sh BECAME
# the fixed code, so the control ran the fix against itself, observed success
# where it demands failure, and went red — on main, on every commit after it, for
# four commits, blocking every push and every merge in the repo.
#
# The shape is worth naming, because it is invisible in review: a control whose
# "before" is a moving ref is GUARANTEED to pass in the PR that introduces it and
# GUARANTEED to fail forever once that PR lands. It cannot be caught by running
# it; it can only be caught by reading it, or by the identity assertion below.
#
# A pinned SHA would fix the mutability but not the reachability: the `ci` job's
# checkout takes no fetch-depth, so it is shallow, and an old SHA is simply not
# in the object store there. Hence a vendored fixture — a snapshot of history,
# correct forever, immune to ref movement, history rewrites and clone depth.
#
# That same shallowness is why this survived four commits, and it is the
# strongest argument for the fixture. `origin/main` does not resolve in the
# shallow `ci` job either, so on every PR the old code took its `else` branch,
# printed "SKIP: 5 (negative control) — origin/main not resolvable here", and
# reported PASS. The control had therefore NEVER run in a gating context since
# it was written: green on every PR, red only on push-to-main, where origin/main
# finally resolves and it compares the fix against itself. A required check that
# can only fail AFTER the merge blocks nothing. That SKIP branch is gone for
# exactly this reason — an unresolvable "before" source now fails loud, because
# a control that cannot run must not report success.
#
# The fixture also makes PR and main SYMMETRIC: a vendored file resolves the
# same way in a shallow checkout and a full one, so this control now runs where
# it can still stop something.
#
# The fixture is named .sh.txt, not .sh, deliberately: it is frozen DATA, never
# executed, and scripts/shellcheck.sh lints every tracked *.sh. A historical
# snapshot must not be edited to satisfy a linter.
PREFIX_SRC="$REPO_ROOT/tests/fixtures/bootstrap-prefix-27d5243-parent.sh.txt"
if [ ! -s "$PREFIX_SRC" ]; then
  fail "5 (negative control): the frozen pre-fix fixture is missing or empty at $PREFIX_SRC. Without a 'before' source this control cannot run, and a control that cannot run must not report success."
fi
# The assertion that would have caught the origin/main bug on day one: if the
# "before" and "after" sources are the same bytes, this control is comparing the
# fix to itself and proves nothing, whatever its other assertions then say.
if [ "$(cksum < "$PREFIX_SRC")" = "$(cksum < "$BOOTSTRAP")" ]; then
  fail "5 (negative control): the pre-fix fixture is byte-identical to $BOOTSTRAP, so this control is comparing the fix against itself and cannot fail. Restore a genuine pre-fix snapshot."
fi
# The byte check alone is nearly inert, because the fixture is a ~70-line
# EXTRACT and bootstrap.sh is the whole 2000+ line script: those two can never
# be byte-equal in the intended design, so that check only catches someone
# literally copying bootstrap.sh over the fixture.
#
# The likelier regression is subtler and the byte check is blind to it: a future
# maintainer sees a fixture that looks stale next to bootstrap.sh and
# "refreshes" it by re-running the extraction against CURRENT source. That
# produces a ~70-line extract of POST-fix code, which is not byte-equal to
# anything, so the check above stays silent — and check 5 then fails on
# RESULT_FAILED_COUNT with a message pointing at bootstrap.sh, sending the next
# person to debug the wrong file. This repo has already paid for a
# wrong-repair-pointer once (6ac8364).
#
# So assert PROVENANCE, not merely difference. What makes this fixture pre-fix
# is precisely that the user-space installers did not exist yet; the setup loop
# above already proves all four DO exist in $BOOTSTRAP, so this binds the pair.
for _fn in install_node_userspace install_python_userspace user_space_platform fetch_tarball; do
  if grep -q "$_fn" "$PREFIX_SRC"; then
    fail "5 (negative control): the pre-fix fixture references ${_fn}, which did not exist until the fix (27d5243). It was probably regenerated from post-fix source. It must stay a frozen snapshot of 27d5243^, never a re-extraction from HEAD."
  fi
done
PRE_HOME="$TMP/pre-home"; mkdir -p "$PRE_HOME"
PRE_HARNESS="$TMP/pre-harness.sh"
build_harness "$PREFIX_SRC" "$PRE_HARNESS" "$PRE_HOME"
PRE_OUT="$(PATH="$POST_STUB:/usr/bin:/bin" bash "$PRE_HARNESS" 2>&1)" || true
echo "$PRE_OUT" | grep -q 'RESULT_FAILED_COUNT=2' \
  || fail "5 (negative control): the pre-fix source was expected to call err() exactly twice (python + node) with brew absent + no sudo, so this harness would have caught the original bug. Output:
$PRE_OUT"
echo "$PRE_OUT" | grep -q 'RESULT_NODE=NONE' \
  || fail "5 (negative control): the pre-fix source was expected to leave node completely absent. Output:
$PRE_OUT"
echo "PASS: negative control confirmed against the frozen pre-fix fixture (27d5243^) — RESULT_FAILED_COUNT=2, RESULT_NODE=NONE"

# ── 6. Linux wiring: both sections gate on is_linux && have_sudo, and fall
# back to the same install_*_userspace() functions on that branch ──
grep -q 'is_linux && have_sudo' "$BOOTSTRAP" \
  || fail "6: no section gates its Linux admin path on 'is_linux && have_sudo'"
PY_BLOCK="$(awk '/^if ! python3 -c "import sys; assert sys.version_info >= \(3,10\)" 2>\/dev\/null; then$/,/^fi$/' "$BOOTSTRAP")"
NODE_BLOCK="$(awk '/^if ! have node; then$/,/^fi$/' "$BOOTSTRAP")"
printf '%s' "$PY_BLOCK"   | grep -q 'is_linux && have_sudo' || fail "6: the Python section does not gate on is_linux && have_sudo"
printf '%s' "$NODE_BLOCK" | grep -q 'is_linux && have_sudo' || fail "6: the Node section does not gate on is_linux && have_sudo"
printf '%s' "$PY_BLOCK"   | grep -q 'install_python_userspace' || fail "6: the Python section never calls install_python_userspace on the no-admin branch"
printf '%s' "$NODE_BLOCK" | grep -q 'install_node_userspace'   || fail "6: the Node section never calls install_node_userspace on the no-admin branch"

echo "PASS: test_bootstrap_userspace_fallback (6 checks, negative control on the main scenario)"
