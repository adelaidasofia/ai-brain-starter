#!/usr/bin/env bash
# Test the zero-prerequisite entry path: bootstrap.sh must survive being run
# from a directory that holds this repo's files but has no .git.
#
# Bug (MYC-3895, 2026-08-12 cohort install session): the install's FIRST
# command was `git clone`, and git was the one prerequisite neither bootstrap
# installed. Corporate laptops block the CLI installers that provide it — the
# session transcript records it verbatim: "Git and Node.js CLI installs require
# administrator rights." Five participants ended the session still blocked. Every
# no-admin recovery path further down bootstrap.sh was unreachable, because the
# fetch that lands bootstrap.sh itself ran first and needed git.
#
# Fix: the entry command falls back to a tarball/zip (curl+tar, Invoke-WebRequest
# +Expand-Archive) which need no git, no brew and no admin. That means bootstrap
# can now start in a directory with no .git, where a plain `git clone` FAILS
# (non-empty target) and used to land in the red actionable-failures list —
# trading a missing-git block for a scary-looking one. adopt_archive_install()
# converts that directory into a real clone so auto-update keeps working.
#
# Covered here:
#   1. bootstrap.sh is syntactically valid (bash -n).
#   2. have_working_git probes by EXECUTION, not presence — macOS ships a
#      /usr/bin/git stub with the Command Line Tools absent, so `command -v git`
#      succeeds on a machine where git cannot clone anything.
#   3. adopt_archive_install turns a clean archive dir into a real clone tracking
#      origin/main, and leaves NO backup behind.
#   4. Negative control for 3: an archive dir with a local edit is NOT silently
#      reset — a .bak-<stamp> copy is made and its path recorded.
#   5. adopt_archive_install RETURNS NON-ZERO when it cannot reach the remote, so
#      the caller's non-fatal warn path is real rather than assumed.
#   6. Ordering: the archive-adopt branch comes BEFORE the bare `git clone` else
#      branch, so a non-empty directory can never reach the failing clone.
#
# Self-contained; no network; never writes outside its tmpdir.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOOTSTRAP="$REPO_ROOT/bootstrap.sh"
[ -f "$BOOTSTRAP" ] || { echo "ERROR: $BOOTSTRAP not found" >&2; exit 1; }

fail() { echo "FAIL: $1" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ── 1. syntax ──
bash -n "$BOOTSTRAP" || fail "1: bootstrap.sh has a syntax error"

# ── 2. capability probe, not presence probe ──
# Presence-only would be `have git` alone. The whole point is that macOS's git
# stub passes that check and then fails to clone, so the probe MUST execute git.
PROBE_BODY="$(awk '/^have_working_git\(\) \{/,/^\}/' "$BOOTSTRAP")"
[ -n "$PROBE_BODY" ] || fail "2: have_working_git() not found in bootstrap.sh"
printf '%s' "$PROBE_BODY" | grep -q 'git --version' \
  || fail "2: have_working_git must EXECUTE git (git --version), not just check presence"
printf '%s' "$PROBE_BODY" | grep -q 'run_with_timeout' \
  || fail "2: have_working_git must bound the probe — a CLT stub can open a GUI and hang"

# Behavioral: a git that exists on PATH but exits non-zero (the stub) must read
# as ABSENT. This is the exact macOS failure, reproduced portably.
STUBDIR="$TMP/stubbin"; mkdir -p "$STUBDIR"
cat > "$STUBDIR/git" <<'STUB'
#!/usr/bin/env bash
echo "xcode-select: note: No developer tools were found." >&2
exit 1
STUB
chmod +x "$STUBDIR/git"
PROBE_HARNESS="$TMP/probe.sh"
{
  echo 'have() { command -v "$1" >/dev/null 2>&1; }'
  awk '/^run_with_timeout\(\) \{/,/^\}/' "$BOOTSTRAP"
  echo "$PROBE_BODY"
  echo 'if have_working_git; then echo WORKING; else echo ABSENT; fi'
} > "$PROBE_HARNESS"

STUB_RESULT="$(PATH="$STUBDIR:$PATH" bash "$PROBE_HARNESS")"
[ "$STUB_RESULT" = "ABSENT" ] \
  || fail "2: a git stub that exits non-zero must read as ABSENT, got '$STUB_RESULT'"
# Negative control: the same probe must say WORKING with a real git, or the
# assertion above would pass for the wrong reason (e.g. a broken harness).
REAL_RESULT="$(bash "$PROBE_HARNESS")"
[ "$REAL_RESULT" = "WORKING" ] \
  || fail "2 (negative control): a real git must read as WORKING, got '$REAL_RESULT'"

# ── shared fixture: a local "origin" so no test below touches the network ──
export GIT_CONFIG_NOSYSTEM=1
export HOME="$TMP/fakehome"; mkdir -p "$HOME"
git config --global user.email "ci@example.invalid"
git config --global user.name  "CI"
git config --global init.defaultBranch main

ORIGIN="$TMP/origin.git"
SEED="$TMP/seed"
git init -q --bare -b main "$ORIGIN"
git init -q -b main "$SEED"
mkdir -p "$SEED"
printf '#!/usr/bin/env bash\necho hi\n' > "$SEED/bootstrap.sh"
printf 'upstream content\n' > "$SEED/README.md"
git -C "$SEED" add -A
git -C "$SEED" commit -qm "seed"
git -C "$SEED" remote add origin "$ORIGIN"
git -C "$SEED" push -q origin main

ADOPT_FN="$(awk '/^adopt_archive_install\(\) \{/,/^\}/' "$BOOTSTRAP")"
[ -n "$ADOPT_FN" ] || fail "3: adopt_archive_install() not found in bootstrap.sh"

run_adopt() { # run_adopt DIR REPO_URL -> prints exit code
  local d="$1" u="$2" h="$TMP/adopt.sh"
  { echo "$ADOPT_FN"; echo 'adopt_archive_install "$1" "$2"; echo "RC=$?"'; } > "$h"
  ( bash "$h" "$d" "$u" 2>/dev/null || true )
}

# ── 3. clean archive dir adopts cleanly ──
CLEAN="$TMP/clean"
mkdir -p "$CLEAN"
cp "$SEED/bootstrap.sh" "$SEED/README.md" "$CLEAN/"      # archive extract: files, no .git
[ -d "$CLEAN/.git" ] && fail "3: fixture invalid — archive dir must start with no .git"

OUT="$(run_adopt "$CLEAN" "$ORIGIN")"
echo "$OUT" | grep -q 'RC=0' || fail "3: adopt of a clean archive dir failed ($OUT)"
[ -d "$CLEAN/.git" ] || fail "3: adopt did not create a real clone (.git missing)"
[ "$(git -C "$CLEAN" rev-parse HEAD)" = "$(git -C "$ORIGIN" rev-parse main)" ] \
  || fail "3: adopted clone is not at origin/main"
[ "$(git -C "$CLEAN" rev-parse --abbrev-ref HEAD)" = "main" ] \
  || fail "3: adopted clone is not on branch main (auto-update pulls main)"
git -C "$CLEAN" rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1 \
  || fail "3: adopted clone has no upstream — git pull would fail on the next run"
compgen -G "$CLEAN.bak-*" >/dev/null \
  && fail "3: a clean archive dir must NOT produce a backup copy"
[ -f "$CLEAN/.adopt-backup-path" ] \
  && fail "3: a clean archive dir must not record a backup path"

# ── 4. NEGATIVE CONTROL: local edits are preserved, never silently reset ──
DIRTY="$TMP/dirty"
mkdir -p "$DIRTY"
cp "$SEED/bootstrap.sh" "$DIRTY/"
printf 'MY LOCAL PATCH\n' > "$DIRTY/README.md"        # differs from upstream
printf 'my own file\n'   > "$DIRTY/my-notes.txt"      # untracked-upstream addition

OUT="$(run_adopt "$DIRTY" "$ORIGIN")"
echo "$OUT" | grep -q 'RC=0' || fail "4: adopt of a modified archive dir failed ($OUT)"
[ -f "$DIRTY/.adopt-backup-path" ] \
  || fail "4: modified archive dir must record a backup path"
SHELVED="$(cat "$DIRTY/.adopt-backup-path")"
[ -d "$SHELVED" ] || fail "4: recorded backup path '$SHELVED' does not exist"
grep -q 'MY LOCAL PATCH' "$SHELVED/README.md" \
  || fail "4: the user's local edit was not preserved in the backup"
grep -q 'my own file' "$SHELVED/my-notes.txt" \
  || fail "4: the user's added file was not preserved in the backup"
grep -q 'upstream content' "$DIRTY/README.md" \
  || fail "4: the adopted tree should now match origin/main"

# ── 5. unreachable remote returns non-zero (the caller's warn path is real) ──
BROKEN="$TMP/broken"
mkdir -p "$BROKEN"
cp "$SEED/bootstrap.sh" "$BROKEN/"
OUT="$(run_adopt "$BROKEN" "$TMP/does-not-exist.git")"
echo "$OUT" | grep -q 'RC=0' \
  && fail "5: adopt must return non-zero when the remote is unreachable"

# ── 6. ordering: archive-adopt branch precedes the bare `git clone` ──
# `|| true` is load-bearing: under `set -e`, a grep that matches nothing makes
# the assignment itself non-zero and kills the script BEFORE the check below can
# report why. That turns "the adopt branch is gone" — the exact regression this
# check exists to catch — into a silent non-zero exit with no message.
ADOPT_LINE="$( { grep -n 'elif \[\[ -f "\$SKILL_DIR/bootstrap.sh" \]\]' "$BOOTSTRAP" || true; } | head -1 | cut -d: -f1)"
CLONE_LINE="$( { grep -n 'clone ai-brain-starter to \$SKILL_DIR' "$BOOTSTRAP" || true; } | head -1 | cut -d: -f1)"
[ -n "$ADOPT_LINE" ] || fail "6: the archive-adopt branch is missing"
[ -n "$CLONE_LINE" ] || fail "6: the bare clone branch is missing"
[ "$ADOPT_LINE" -lt "$CLONE_LINE" ] \
  || fail "6: a non-empty dir reaches 'git clone' (line $CLONE_LINE) before adopt (line $ADOPT_LINE) — the clone fails on a non-empty target and lands in the red FAILED list"

echo "PASS: test_bootstrap_archive_entry (6 checks, 2 negative controls)"
