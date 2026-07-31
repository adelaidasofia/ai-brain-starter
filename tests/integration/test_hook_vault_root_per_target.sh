#!/usr/bin/env bash
# Test: every hook remediated by MYC-3529 resolves its vault PER TARGET, and
# therefore fires on a vault that is NOT the one $VAULT_ROOT names.
#
# THE ASSERTION THIS CLASS WAS MISSING
#   MYC-2505 (#407) surveyed the fleet and found 52 naive `VAULT_ROOT` reads.
#   Twelve of them were tagged SEV-A: all in hooks/, all shaped
#       VAULT = os.environ.get("VAULT_ROOT", str(Path.home() / "vault"))
#   which fails silently in both directions. UNSET it resolves to ~/vault, so
#   the hook is inert on every vault not literally named "vault". SET it names
#   exactly ONE vault, so a target in any OTHER vault resolves against the wrong
#   root, the containment check returns False, and the hook SKIPS the thing it
#   exists to check -- failing OPEN, with no output at all.
#
#   #404 fixed this once, in hooks/validate-handoff-frontmatter.py. The bug had
#   survived that long precisely because the suite was not hermetic: every test
#   ran against the vault $VAULT_ROOT already named, so "resolve from the env
#   var" and "resolve from the target" were indistinguishable. A test written
#   that way re-creates the blind spot no matter how many cases it has.
#
#   So EVERY assertion below points $VAULT_ROOT at a DECOY vault and then acts
#   on a DIFFERENT one. A hook that still reads the env var naively cannot pass:
#   it will either do nothing (silent open) or answer for the decoy.
#
# STRUCTURE
#   $TMP/decoy-vault    a real, populated vault. $VAULT_ROOT points here in
#                       every assertion, and no assertion wants its content.
#   $TMP/other-vault    the vault each hook must actually resolve to.
#   $TMP/coderepo       NOT a vault (no Meta-suffixed folder) -- the negative
#                       control that keeps the fixes from over-firing.
#   $TMP/home           HOME/USERPROFILE override, so the ~/dev-scoped and
#                       ~/.claude-scoped hooks are hermetic too.
#
# Bash + python3 only, no network. exit 0 = every remediated hook is per-target.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOKS="$REPO_ROOT/hooks"
GUARD="$REPO_ROOT/scripts/check-vault-root-reads.py"
BASELINE="$REPO_ROOT/scripts/vault-root-read-baseline.txt"

# The MYC-3529 population, verbatim from the SEV-A tier of PR #407's survey.
# Named explicitly so this guard cannot silently narrow: if a file is renamed or
# deleted the existence check below fails LOUD rather than skipping it.
SEV_A=(
  auto-capture-public-ships.py
  block-raw-vault-git.py
  block-vault-git-fullwalk.py
  block-worktree-shared-edit.py
  build-runbook-check.py
  create-dev-repo-checkpoint.py
  surface-stale-automation-failures.py
  vault-command-nudges.py
  vault-context.py
  verify-discoverability-on-close.py
  verify-session-close-cascade.py
  worktree-archive-autoprep.py
)

FAILURES=0
pass() { echo "PASS: $1"; }
fail() {
  echo "FAIL: $1" >&2
  FAILURES=$((FAILURES + 1))
}
show_streams() {
  echo "  --- stdout ---" >&2; sed 's/^/  /' "$OUT" >&2
  echo "  --- stderr ---" >&2; sed 's/^/  /' "$ERR" >&2
}

# The tmpdir comes from python's tempfile, NOT `mktemp -d`, and is normalized to
# forward slashes. Under Git Bash on Windows `mktemp -d` hands back an MSYS path
# ("/tmp/tmp.XXXX") that native python cannot resolve -- it silently reinterprets
# it relative to the current drive. Every assertion here compares a path the
# SHELL produced against a path PYTHON resolved, so the two must agree on what a
# path is or the suite tests nothing. python's tempfile answers in the same
# namespace the hooks run in, on both platforms.
TMP="$(python3 -c 'import tempfile; print(tempfile.mkdtemp(prefix="abs-vault-root-").replace(chr(92), "/"))')"
if [ -z "$TMP" ] || [ ! -d "$TMP" ]; then
  echo "FAIL: could not create a tmpdir python and the shell both agree on" >&2
  exit 1
fi
trap 'rm -rf "$TMP"' EXIT
OUT="$TMP/out.txt"
ERR="$TMP/err.txt"
DECOY="$TMP/decoy-vault"
OTHER="$TMP/other-vault"
CODEREPO="$TMP/coderepo"
FAKEHOME="$TMP/home"

# Every hook's own bypass switch is cleared, and CLAUDE_CWD with it: several of
# these hooks prefer $CLAUDE_CWD over the payload's cwd, and the ambient value
# (set by the harness that may be running this suite) would silently retarget
# every assertion at the real machine.
CLEAN_ENV=(
  -u CLAUDE_CWD
  -u AUTO_CAPTURE_SHIPS_BYPASS
  -u BUILD_RUNBOOK_BYPASS
  -u DEV_CHECKPOINT_BYPASS
  -u DISCOVERABILITY_VERIFIER_BYPASS
  -u DISCOVERABILITY_VERIFIER_PATH
  -u SURFACE_STALE_AUTOMATION_BYPASS
  -u VERIFY_CASCADE_BYPASS
  -u VERIFY_CASCADE_SOFT
  -u WORKTREE_AUTOPREP_BYPASS
  -u WORKTREE_VAULT_EDIT_BYPASS
)

run_hook() {  # <hook-file> <stdin-json> [extra env args...] -> sets RC
  local hook="$1" payload="$2"; shift 2
  printf '%s' "$payload" | env "${CLEAN_ENV[@]}" "$@" python3 "$hook" >"$OUT" 2>"$ERR"
  RC=$?
}

payload_bash() {  # <command> <cwd>
  python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[1]},"cwd":sys.argv[2]}))' "$1" "$2"
}
payload_write() {  # <file_path> <cwd>
  python3 -c 'import json,sys; print(json.dumps({"tool_name":"Write","tool_input":{"file_path":sys.argv[1],"content":"x"},"cwd":sys.argv[2]}))' "$1" "$2"
}

git_repo() {  # <dir> -- init + identity + one commit so rev-parse/stash work
  git -C "$1" init -q
  git -C "$1" config user.email t@t
  git -C "$1" config user.name t
  echo seed > "$1/seed.txt"
  git -C "$1" add seed.txt >/dev/null 2>&1
  git -C "$1" -c commit.gpgsign=false commit -qm init >/dev/null 2>&1
}

# --------------------------------------------------------------------------
# fixture
# --------------------------------------------------------------------------
# Built in python, not bash: the vault subpaths this repo actually uses carry
# emoji ("⚙️ Meta", "🍄 .../📋 Pending Signals") and the shell's locale handling
# of those is not something a portability gate should depend on.
python3 - "$TMP" <<'PY'
import sys
from datetime import datetime, timedelta
from pathlib import Path

tmp = Path(sys.argv[1])
decoy, other, coderepo, home = (
    tmp / "decoy-vault", tmp / "other-vault", tmp / "coderepo", tmp / "home")

# PRECONDITION. Detection walks UP from the target for a Meta-suffixed folder,
# so a stray "*Meta" directory in an ancestor of the tmpdir would make the
# not-a-vault negative controls resolve to it and quietly stop testing anything.
# Assert the fixture's ancestry is clean instead of assuming it.
for anc in [tmp, *tmp.parents]:
    try:
        hit = next((c.name for c in anc.iterdir()
                    if c.is_dir() and c.name.endswith("Meta")), None)
    except OSError:
        continue
    if hit:
        print(f"FIXTURE-UNUSABLE {anc}/{hit}", flush=True)
        sys.exit(3)

for v in (decoy, other):
    (v / "⚙️ Meta").mkdir(parents=True, exist_ok=True)
    (v / "⚙️ Meta" / "scripts").mkdir(exist_ok=True)
coderepo.mkdir(parents=True, exist_ok=True)
(coderepo / ".claude" / "worktrees" / "slug1").mkdir(parents=True, exist_ok=True)
(coderepo / "CLAUDE.md").write_text("# a plain code repo\n", encoding="utf-8")
(home / ".claude").mkdir(parents=True, exist_ok=True)
(home / "dev").mkdir(exist_ok=True)

# vault-context.py CORE_FILES, distinguishable per vault.
for v, tag in ((decoy, "DECOY"), (other, "OTHER")):
    (v / "⚙️ Meta" / "Current Priorities.md").write_text(
        f"SENTINEL-PRIORITIES-{tag}\n", encoding="utf-8")
    (v / "⚙️ Meta" / "Open Loops.md").write_text(
        f"SENTINEL-LOOPS-{tag}\n", encoding="utf-8")

# worktree-archive-autoprep.py prep script, distinguishable per vault, so a
# wrong resolution is DETECTED rather than merely absent.
for v, tag in ((decoy, "DECOY"), (other, "OTHER")):
    (v / "⚙️ Meta" / "scripts" / "worktree-archive-prep.py").write_text(
        f"print('SENTINEL-PREP-RAN-IN-{tag}')\n", encoding="utf-8")
(other / ".claude" / "worktrees" / "slug1").mkdir(parents=True, exist_ok=True)

# surface-stale-automation-failures.py: three distinct failure-minutes inside
# the 72h window, in the OTHER vault ONLY. The decoy deliberately has no log,
# so a decoy-resolved scan finds nothing and the assertion is unambiguous.
now = datetime.now()
lines = [f"[{(now - timedelta(minutes=5 * i)).strftime('%Y-%m-%dT%H:%M:%S')}] FAIL snapshot errored\n"
         for i in range(1, 5)]
(other / "⚙️ Meta" / ".auto-snapshot.log").write_text("".join(lines), encoding="utf-8")

# The two session-close verifiers resolve through resolve_vault_root(), which
# keys on a CLAUDE.md declaring its OWN Session End/Close cascade.
(other / "CLAUDE.md").write_text(
    "# other vault\n\n## Session End -- capture cascade\n", encoding="utf-8")

print("FIXTURE-OK", flush=True)
PY
if [ $? -ne 0 ]; then
  echo "FAIL: fixture could not be built hermetically (see FIXTURE-UNUSABLE above)." >&2
  echo "      An ancestor of the tmpdir holds a Meta-suffixed folder, so the" >&2
  echo "      not-a-vault controls would resolve to it and assert nothing." >&2
  exit 1
fi

git_repo "$OTHER"
git_repo "$CODEREPO"

# --------------------------------------------------------------------------
# 0. structural: the tier is remediated, and this guard is not blind
# --------------------------------------------------------------------------
missing=()
for f in "${SEV_A[@]}"; do
  [ -f "$HOOKS/$f" ] || missing+=("$f")
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "FAIL  SEV-A hook(s) not found: ${missing[*]}" >&2
  echo "      Renamed or removed? Update this guard to track them, or it is blind." >&2
  exit 1
fi
pass "all ${#SEV_A[@]} SEV-A hooks present (guard is not blind)"

# Comment lines are excluded deliberately: the baseline's SEV-A section header
# now NAMES these files while explaining that their rows were deleted, and a
# plain substring match would read that prose as a live pin. A row is
# `<sha256> <TAG> <path>`; only those count.
pinned=()
for f in "${SEV_A[@]}"; do
  if grep -qE "^[0-9a-f]{64}[[:space:]]+[^[:space:]]+[[:space:]]+hooks/$f\$" "$BASELINE"; then
    pinned+=("$f")
  fi
done
if [ "${#pinned[@]}" -gt 0 ]; then
  fail "SEV-A row(s) still pinned in the ratchet: ${pinned[*]} -- a remediated file must be UNPINNED, never re-pinned"
else
  pass "all ${#SEV_A[@]} SEV-A rows are gone from vault-root-read-baseline.txt"
fi

sev_a_paths=()
for f in "${SEV_A[@]}"; do sev_a_paths+=("$HOOKS/$f"); done
if python3 "$GUARD" --check "${sev_a_paths[@]}" >"$OUT" 2>"$ERR"; then
  pass "no unsanctioned VAULT_ROOT read remains in any of the 12"
else
  fail "check-vault-root-reads --check still reports a violation in the SEV-A tier"
  show_streams
fi

# Every remediated hook must still REACH a sanctioned resolver. Without this a
# future edit could delete the resolver call, satisfy the lint by deleting the
# read too, and quietly restore a hardcoded root. build-runbook-check.py is the
# one exception: its VAULT_ROOT binding was dead code and needs no resolver.
resolved=0
for f in "${SEV_A[@]}"; do
  [ "$f" = "build-runbook-check.py" ] && continue
  if grep -qE 'vault_root_for|resolve_vault_root' "$HOOKS/$f"; then
    resolved=$((resolved + 1))
  else
    fail "$f no longer references a sanctioned resolver"
  fi
done
if [ "$resolved" -eq $(( ${#SEV_A[@]} - 1 )) ]; then
  pass "all $resolved vault-consuming SEV-A hooks route through a sanctioned resolver"
fi

if grep -qE '^[^#]*VAULT_ROOT' "$HOOKS/build-runbook-check.py"; then
  fail "build-runbook-check.py still binds VAULT_ROOT (the read was dead code; it should be gone)"
else
  pass "build-runbook-check.py carries no VAULT_ROOT binding at all"
fi

# PYTHON FLOOR. scripts/ci.sh's gate runs on Python 3.9, but a contributor's
# laptop is usually 3.12+. A PEP-604 `X | None` annotation is evaluated at
# def-time and raises TypeError on 3.9 unless the module carries
# `from __future__ import annotations` -- so the hook crashes at IMPORT and,
# under the hooks.json wrapper, silently does nothing. Exactly the failure mode
# this whole ticket is about, arriving by a different road.
#
# py_compile does NOT catch it: the annotation compiles fine and only blows up
# when the def executes. The behavioural assertions below DO catch it, but only
# when they run on 3.9 -- which means a dev on 3.12 sees green locally and red
# in CI. This check catches it on every interpreter. (Caught live: this suite
# reddened CI on 3.9 for four of these hooks, two of which were already broken
# on origin/main and had simply never been imported by a test.)
floor_bad="$(python3 - "$HOOKS" "${SEV_A[@]}" <<'PY'
import ast, sys
from pathlib import Path
hooks = Path(sys.argv[1])
bad = []
for name in sys.argv[2:]:
    tree = ast.parse((hooks / name).read_text(encoding="utf-8"))
    future = any(
        isinstance(n, ast.ImportFrom) and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )
    if future:
        continue
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        anns = [a.annotation for a in args.args + args.kwonlyargs + args.posonlyargs
                if a.annotation]
        if node.returns:
            anns.append(node.returns)
        if any(isinstance(s, ast.BinOp) and isinstance(s.op, ast.BitOr)
               for a in anns for s in ast.walk(a)):
            bad.append(f"{name}:{node.name}")
            break
print(" ".join(bad))
PY
)"
if [ -n "$floor_bad" ]; then
  fail "PEP-604 annotation without \`from __future__ import annotations\` -- TypeError at import on Python 3.9, the version scripts/ci.sh runs: $floor_bad"
else
  pass "no SEV-A hook uses a PEP-604 annotation that would crash the import on Python 3.9"
fi

# --------------------------------------------------------------------------
# 1. vault-context.py -- injects the OTHER vault's priorities, not the decoy's
# --------------------------------------------------------------------------
p=$(python3 -c 'import json,sys; print(json.dumps({"prompt":"what is the plan","cwd":sys.argv[1]}))' "$OTHER")
run_hook "$HOOKS/vault-context.py" "$p" "VAULT_ROOT=$DECOY"
if grep -q "SENTINEL-PRIORITIES-OTHER" "$OUT" && ! grep -q "SENTINEL-PRIORITIES-DECOY" "$OUT"; then
  pass "vault-context injects the cwd's vault, not \$VAULT_ROOT's (rc=$RC)"
else
  fail "vault-context did not inject the OTHER vault (rc=$RC)"
  show_streams
fi

run_hook "$HOOKS/vault-context.py" \
  "$(python3 -c 'import json,sys; print(json.dumps({"prompt":"what is the plan","cwd":sys.argv[1]}))' "$CODEREPO")" \
  -u VAULT_ROOT
if [ ! -s "$OUT" ]; then
  pass "vault-context injects nothing when no vault is resolvable (no phantom ~/vault)"
else
  fail "vault-context injected context with no vault resolvable"
  show_streams
fi

# --------------------------------------------------------------------------
# 2. block-worktree-shared-edit.py -- blocks a worktree write in ANY vault
# --------------------------------------------------------------------------
run_hook "$HOOKS/block-worktree-shared-edit.py" \
  "$(payload_write "$OTHER/.claude/worktrees/slug1/⚙️ Meta/rules/foo.md" "$OTHER")" \
  "VAULT_ROOT=$DECOY"
if [ "$RC" -eq 2 ] && grep -q "BLOCKED" "$ERR"; then
  pass "block-worktree-shared-edit blocks a shared-canonical write in a non-\$VAULT_ROOT vault (rc=$RC)"
else
  fail "block-worktree-shared-edit failed OPEN on the OTHER vault's worktree (rc=$RC)"
  show_streams
fi

run_hook "$HOOKS/block-worktree-shared-edit.py" \
  "$(payload_write "$CODEREPO/.claude/worktrees/slug1/CLAUDE.md" "$CODEREPO")" \
  "VAULT_ROOT=$DECOY"
if [ "$RC" -eq 0 ]; then
  pass "block-worktree-shared-edit stays out of a plain code repo's worktree (rc=$RC)"
else
  fail "block-worktree-shared-edit over-fired on a non-vault worktree (rc=$RC)"
  show_streams
fi

# --------------------------------------------------------------------------
# 3. block-raw-vault-git.py -- mutating git op in ANY vault
# --------------------------------------------------------------------------
run_hook "$HOOKS/block-raw-vault-git.py" \
  "$(payload_bash 'git add "note.md"' "$OTHER")" "VAULT_ROOT=$DECOY"
if [ "$RC" -eq 2 ]; then
  pass "block-raw-vault-git blocks a raw git add in a non-\$VAULT_ROOT vault (rc=$RC)"
else
  fail "block-raw-vault-git failed OPEN on the OTHER vault (rc=$RC)"
  show_streams
fi

run_hook "$HOOKS/block-raw-vault-git.py" \
  "$(payload_bash "git -C \"$OTHER\" commit -m x" "$CODEREPO")" "VAULT_ROOT=$DECOY"
if [ "$RC" -eq 2 ]; then
  pass "block-raw-vault-git follows -C into a non-\$VAULT_ROOT vault from a non-vault cwd (rc=$RC)"
else
  fail "block-raw-vault-git missed a -C retarget into the OTHER vault (rc=$RC)"
  show_streams
fi

run_hook "$HOOKS/block-raw-vault-git.py" \
  "$(payload_bash 'git add "note.md"' "$CODEREPO")" "VAULT_ROOT=$DECOY"
if [ "$RC" -eq 0 ]; then
  pass "block-raw-vault-git lets a plain code repo through (rc=$RC)"
else
  fail "block-raw-vault-git over-fired on a non-vault repo (rc=$RC)"
  show_streams
fi

# --------------------------------------------------------------------------
# 4. block-vault-git-fullwalk.py -- full-tree stage in ANY vault
# --------------------------------------------------------------------------
run_hook "$HOOKS/block-vault-git-fullwalk.py" \
  "$(payload_bash 'git add -A' "$OTHER")" "VAULT_ROOT=$DECOY"
if [ "$RC" -eq 2 ]; then
  pass "block-vault-git-fullwalk blocks git add -A in a non-\$VAULT_ROOT vault (rc=$RC)"
else
  fail "block-vault-git-fullwalk failed OPEN on the OTHER vault (rc=$RC)"
  show_streams
fi

run_hook "$HOOKS/block-vault-git-fullwalk.py" \
  "$(payload_bash 'git add -A' "$CODEREPO")" "VAULT_ROOT=$DECOY"
if [ "$RC" -eq 0 ]; then
  pass "block-vault-git-fullwalk leaves a standard-sized repo's git add -A alone (rc=$RC)"
else
  fail "block-vault-git-fullwalk over-fired on a non-vault repo (rc=$RC)"
  show_streams
fi

# --------------------------------------------------------------------------
# 5. vault-command-nudges.py -- both scoping models, on ANY vault
# --------------------------------------------------------------------------
run_hook "$HOOKS/vault-command-nudges.py" \
  "$(payload_bash 'git push' "$OTHER")" "VAULT_ROOT=$DECOY"
if [ "$RC" -eq 2 ] && grep -q "BLOCKED" "$ERR"; then
  pass "vault-command-nudges (repo-scoped) blocks git push in a non-\$VAULT_ROOT vault (rc=$RC)"
else
  fail "vault-command-nudges repo-scoped rule failed OPEN on the OTHER vault (rc=$RC)"
  show_streams
fi

run_hook "$HOOKS/vault-command-nudges.py" \
  "$(payload_bash "grep foo \"$OTHER/note.md\"" "$CODEREPO")" "VAULT_ROOT=$DECOY"
if [ "$RC" -eq 2 ] && grep -q "NUDGE" "$ERR"; then
  pass "vault-command-nudges (namespace-scoped) catches a path in a non-\$VAULT_ROOT vault (rc=$RC)"
else
  fail "vault-command-nudges namespace rule missed a path inside the OTHER vault (rc=$RC)"
  show_streams
fi

run_hook "$HOOKS/vault-command-nudges.py" \
  "$(payload_bash 'grep foo note.md' "$CODEREPO")" "VAULT_ROOT=$DECOY"
if [ "$RC" -eq 0 ]; then
  pass "vault-command-nudges stays quiet outside every vault namespace (rc=$RC)"
else
  fail "vault-command-nudges over-fired outside any vault (rc=$RC)"
  show_streams
fi

# --------------------------------------------------------------------------
# 6. create-dev-repo-checkpoint.py -- excludes a vault that is NOT $VAULT_ROOT
#    This one has teeth: checkpoint() runs `git add -A`, the exact full-tree
#    stage block-vault-git-fullwalk.py exists to keep out of a vault. Before
#    MYC-3529 the exclusion compared against a single env-named vault, so a
#    vault checked out under ~/dev sailed through both gates.
# --------------------------------------------------------------------------
python3 - "$FAKEHOME" <<'PY'
from pathlib import Path
home = Path(__import__("sys").argv[1])
(home / "dev" / "vault-under-dev" / "⚙️ Meta").mkdir(parents=True, exist_ok=True)
(home / "dev" / "plain-repo").mkdir(parents=True, exist_ok=True)
PY
git_repo "$FAKEHOME/dev/vault-under-dev"
git_repo "$FAKEHOME/dev/plain-repo"
echo dirty >> "$FAKEHOME/dev/vault-under-dev/seed.txt"
echo dirty >> "$FAKEHOME/dev/plain-repo/seed.txt"

run_hook "$HOOKS/create-dev-repo-checkpoint.py" \
  "$(python3 -c 'import json,sys; print(json.dumps({"cwd":sys.argv[1]}))' "$FAKEHOME/dev/vault-under-dev")" \
  "VAULT_ROOT=$DECOY" "HOME=$FAKEHOME" "USERPROFILE=$FAKEHOME"
if git -C "$FAKEHOME/dev/vault-under-dev" stash list | grep -q claude-checkpoint; then
  fail "create-dev-repo-checkpoint stashed (git add -A) inside a vault that is not \$VAULT_ROOT"
  show_streams
else
  pass "create-dev-repo-checkpoint excludes a non-\$VAULT_ROOT vault under ~/dev (rc=$RC)"
fi

run_hook "$HOOKS/create-dev-repo-checkpoint.py" \
  "$(python3 -c 'import json,sys; print(json.dumps({"cwd":sys.argv[1]}))' "$FAKEHOME/dev/plain-repo")" \
  "VAULT_ROOT=$DECOY" "HOME=$FAKEHOME" "USERPROFILE=$FAKEHOME"
if git -C "$FAKEHOME/dev/plain-repo" stash list | grep -q claude-checkpoint; then
  pass "create-dev-repo-checkpoint still checkpoints a real ~/dev code repo (control: the exclusion above is not vacuous)"
else
  fail "create-dev-repo-checkpoint did not checkpoint a plain ~/dev repo -- the vault-exclusion assertion above proves nothing"
  show_streams
fi

# --------------------------------------------------------------------------
# 7. worktree-archive-autoprep.py -- runs the OTHER vault's prep script
# --------------------------------------------------------------------------
(
  cd "$OTHER/.claude/worktrees/slug1" || exit 1
  printf '{}' | env "${CLEAN_ENV[@]}" "VAULT_ROOT=$DECOY" \
    python3 "$HOOKS/worktree-archive-autoprep.py"
) >"$OUT" 2>"$ERR"
RC=$?
if grep -q "SENTINEL-PREP-RAN-IN-OTHER" "$ERR" && ! grep -q "SENTINEL-PREP-RAN-IN-DECOY" "$ERR"; then
  pass "worktree-archive-autoprep runs the owning vault's prep script, not \$VAULT_ROOT's (rc=$RC)"
else
  fail "worktree-archive-autoprep did not reach the OTHER vault's prep script (rc=$RC)"
  show_streams
fi

# --------------------------------------------------------------------------
# 8. surface-stale-automation-failures.py -- reads the OTHER vault's runner log
# --------------------------------------------------------------------------
run_hook "$HOOKS/surface-stale-automation-failures.py" \
  "$(python3 -c 'import json,sys; print(json.dumps({"cwd":sys.argv[1]}))' "$OTHER")" \
  "VAULT_ROOT=$DECOY" "HOME=$FAKEHOME" "USERPROFILE=$FAKEHOME"
if grep -q "auto-snapshot" "$OUT"; then
  pass "surface-stale-automation-failures surfaces a non-\$VAULT_ROOT vault's silent failures (rc=$RC)"
else
  fail "surface-stale-automation-failures reported nothing while the OTHER vault was failing hourly (rc=$RC)"
  show_streams
fi

run_hook "$HOOKS/surface-stale-automation-failures.py" \
  "$(python3 -c 'import json,sys; print(json.dumps({"cwd":sys.argv[1]}))' "$DECOY")" \
  "VAULT_ROOT=$DECOY" "HOME=$FAKEHOME" "USERPROFILE=$FAKEHOME"
if grep -q "auto-snapshot" "$OUT"; then
  fail "surface-stale-automation-failures reported the OTHER vault's failures for a session in the decoy vault"
  show_streams
else
  pass "surface-stale-automation-failures does not leak one vault's failures into another's session (rc=$RC)"
fi

# --------------------------------------------------------------------------
# 9 + 10. the two session-close verifiers -- import-time root is repo-aware
#    Their per-invocation rebind was already repo-aware (MYC-1270 / 2026-06-30);
#    what MYC-3529 removes is the naive env read that SEEDED the module globals,
#    which answered for ~/vault on any import that did not run main().
# --------------------------------------------------------------------------
import_root() {  # <hook-file> <cwd> -> prints the module's resolved VAULT_ROOT
  ( cd "$2" || exit 1
    env "${CLEAN_ENV[@]}" "VAULT_ROOT=$DECOY" python3 - "$1" <<'PY'
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("hook_under_test", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(str(Path(mod.VAULT_ROOT).resolve()).replace(chr(92), "/"))
PY
  )
}

for hook in verify-session-close-cascade.py verify-discoverability-on-close.py; do
  got="$(import_root "$HOOKS/$hook" "$OTHER" 2>"$ERR")"
  want="$(python3 -c 'import sys;from pathlib import Path;print(str(Path(sys.argv[1]).resolve()).replace(chr(92),"/"))' "$OTHER")"
  if [ "$got" = "$want" ]; then
    pass "$hook seeds VAULT_ROOT from the session's repo-vault, not \$VAULT_ROOT"
  else
    fail "$hook resolved '$got' at import; expected the repo-vault '$want'"
    sed 's/^/  /' "$ERR" >&2
  fi
done

# --------------------------------------------------------------------------
# 11. auto-capture-public-ships.py -- files ships into the OTHER vault
#
#     NOTE, and it is not this ticket's to fix: this hook cannot be run
#     end-to-end on any machine. Line 34 is
#         TZ = ZoneInfo("America/user-local-tz")
#     an unsubstituted template placeholder that is not an IANA zone, so the
#     module raises ZoneInfoNotFoundError at IMPORT, before main() is reached.
#     That is a separate, pre-existing defect (nothing in the repo substitutes
#     it). The vault-root fix is still assertable: stub zoneinfo, import, and
#     ask pending_dir() where the day's ships would be filed.
# --------------------------------------------------------------------------
got="$( cd "$OTHER" || exit 1
  env "${CLEAN_ENV[@]}" "VAULT_ROOT=$DECOY" python3 - "$HOOKS/auto-capture-public-ships.py" <<'PY' 2>&1
import importlib.util, sys, types
fake = types.ModuleType("zoneinfo")
class ZoneInfo:  # placeholder-tolerant stand-in; see the note above
    def __init__(self, key): self.key = key
fake.ZoneInfo = ZoneInfo
sys.modules["zoneinfo"] = fake
spec = importlib.util.spec_from_file_location("hook_under_test", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(str(mod.pending_dir()).replace(chr(92), "/"))
PY
)"
case "$got" in
  "$OTHER"*)
    pass "auto-capture-public-ships files ships into the cwd's vault, not \$VAULT_ROOT's" ;;
  *)
    fail "auto-capture-public-ships resolved '$got'; expected a path under '$OTHER'" ;;
esac

# --------------------------------------------------------------------------
# 12. build-runbook-check.py -- behaviour is identical with and without
#     $VAULT_ROOT, which is the point: it never needed a vault at all.
# --------------------------------------------------------------------------
agent_payload=$(python3 -c 'import json; print(json.dumps({"tool_name":"Agent","tool_input":{"description":"build the MCP connector","prompt":"ship it"}}))')
run_hook "$HOOKS/build-runbook-check.py" "$agent_payload" "VAULT_ROOT=$DECOY" "HOME=$FAKEHOME" "USERPROFILE=$FAKEHOME"
with_env="$(cat "$ERR")"
run_hook "$HOOKS/build-runbook-check.py" "$agent_payload" -u VAULT_ROOT "HOME=$FAKEHOME" "USERPROFILE=$FAKEHOME"
without_env="$(cat "$ERR")"
if [ -n "$with_env" ] && [ "$with_env" = "$without_env" ]; then
  pass "build-runbook-check warns identically with and without \$VAULT_ROOT (vault-independent by construction)"
else
  fail "build-runbook-check behaviour still depends on \$VAULT_ROOT (or it stopped warning at all)"
  echo "  --- with \$VAULT_ROOT ---" >&2; printf '%s\n' "$with_env" | sed 's/^/  /' >&2
  echo "  --- without ---" >&2; printf '%s\n' "$without_env" | sed 's/^/  /' >&2
fi

# --------------------------------------------------------------------------
# 13. the fleet ratchet still holds after the 12 rows were deleted
# --------------------------------------------------------------------------
if python3 "$GUARD" --all >"$OUT" 2>"$ERR"; then
  pass "check-vault-root-reads --all passes with the SEV-A tier unpinned"
else
  fail "check-vault-root-reads --all fails after unpinning the SEV-A tier"
  show_streams
fi

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "All assertions passed. Every MYC-3529 hook resolves its vault per target."
  exit 0
fi
echo "$FAILURES assertion(s) FAILED -- a remediated hook is still answering for \$VAULT_ROOT." >&2
exit 1
