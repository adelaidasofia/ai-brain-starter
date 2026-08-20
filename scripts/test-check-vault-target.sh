#!/usr/bin/env bash
# Negative-control suite for scripts/check-vault-target.py and the callers that
# route through it (MYC-4028: the relocate helpers would git-init a repo over
# $HOME, putting ~/.ssh and ~/.aws inside a git working tree).
#
# A guard earns trust only by FAILING on the thing it catches, so every REFUSE
# case below is paired with a PASS case proving the guard is not simply
# always-on. Two cases are adversarial and matter more than the rest:
#
#   E2  --force must NOT open REFUSE_HOME. An escape hatch on this rule is
#       exactly what gets reached for in a hurry, which is why there is none.
#   E4  --rollback must stay UNGATED. A machine already damaged this way still
#       has to be able to undo it, and a guard that blocks the repair path
#       traps every user it was meant to protect.
#
# Run: bash scripts/test-check-vault-target.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
CVT="$HERE/check-vault-target.py"
SIDECAR_SH="$HERE/relocate-machinery-sidecar.sh"
VAULT_SH="$HERE/relocate-vault.sh"
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$ROOT/tests/integration/lib/sandbox_home.sh"

fails=0
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; fails=$((fails + 1)); }

# want_token LABEL EXPECTED_PREFIX EXPECTED_RC -- <argv...>
# Never pipes the command: in zsh $PIPESTATUS is empty and a piped gate reports
# the pager's status, which is a fake green.
want_token() {
  local label="$1" want_tok="$2" want_rc="$3" out rc=0
  shift 4  # label, token, rc, and the literal --
  out="$("$@" 2>&1)" || rc=$?
  if [ "$rc" != "$want_rc" ]; then
    fail "$label — exit $rc, wanted $want_rc (said: ${out:-<nothing>})"
    return
  fi
  case "$out" in
    "$want_tok"*) pass "$label — $want_tok (exit $rc)" ;;
    *) fail "$label — wanted token $want_tok, got: ${out:-<nothing>}" ;;
  esac
}

# want_rc LABEL EXPECTED_RC -- <argv...>   (end-to-end; output is not asserted)
want_rc() {
  local label="$1" want="$2" out rc=0
  shift 3
  out="$("$@" 2>&1)" || rc=$?
  if [ "$rc" = "$want" ]; then pass "$label — exit $rc"
  else fail "$label — exit $rc, wanted $want (said: $(printf '%s' "$out" | head -2 | tr '\n' ' '))"; fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAKEHOME="$TMP/home"
mkdir -p "$FAKEHOME"

echo "=== check-vault-target: refusals, negative controls, adversarial cases ==="
echo
echo "--- A. the checker itself: MUST REFUSE ---"
want_token "A1 \$HOME"                REFUSE_HOME        1 -- \
  run_sandboxed "$FAKEHOME" python3 "$CVT" --porcelain "$FAKEHOME"
want_token "A2 filesystem root"       REFUSE_HOME        1 -- \
  run_sandboxed "$FAKEHOME" python3 "$CVT" --porcelain "/"
want_token "A3 ancestor of \$HOME"    REFUSE_HOME        1 -- \
  run_sandboxed "$FAKEHOME" python3 "$CVT" --porcelain "$TMP"
# macOS /tmp is a symlink to /private/tmp: a literal string match on the system
# root list reads it as an ordinary directory and waves it through.
want_token "A4 symlinked system root" REFUSE_HOME        1 -- \
  run_sandboxed "$FAKEHOME" python3 "$CVT" --porcelain "/tmp"

mkdir -p "$TMP/creds/.ssh"
want_token "A5 credential material"   REFUSE_CREDENTIALS 1 -- \
  run_sandboxed "$FAKEHOME" python3 "$CVT" --porcelain "$TMP/creds"

mkdir -p "$TMP/bare"
want_token "A6 bare dir + --for-init" REFUSE_NOT_A_VAULT 1 -- \
  run_sandboxed "$FAKEHOME" python3 "$CVT" --porcelain --for-init "$TMP/bare"

echo
echo "--- B. NEGATIVE CONTROLS: the guard must NOT be always-on ---"
# Without --for-init a bare directory is fine: relocating an EXISTING repo is
# not the irreversible half, so the tighter rule applies only to creation.
want_token "B1 bare dir, no --for-init"  OK_VAULT 0 -- \
  run_sandboxed "$FAKEHOME" python3 "$CVT" --porcelain "$TMP/bare"

mkdir -p "$TMP/v_md"; printf 'note\n' > "$TMP/v_md/note.md"
want_token "B2 markdown vault"           OK_VAULT 0 -- \
  run_sandboxed "$FAKEHOME" python3 "$CVT" --porcelain --for-init "$TMP/v_md"

mkdir -p "$TMP/v_claude"; printf '# brain\n' > "$TMP/v_claude/CLAUDE.md"
want_token "B3 CLAUDE.md vault"          OK_VAULT 0 -- \
  run_sandboxed "$FAKEHOME" python3 "$CVT" --porcelain --for-init "$TMP/v_claude"

mkdir -p "$TMP/v_meta/⚙️ Meta"
want_token "B4 Meta-folder vault"        OK_VAULT 0 -- \
  run_sandboxed "$FAKEHOME" python3 "$CVT" --porcelain --for-init "$TMP/v_meta"

mkdir -p "$TMP/v_obs/.obsidian"
want_token "B5 .obsidian vault"          OK_VAULT 0 -- \
  run_sandboxed "$FAKEHOME" python3 "$CVT" --porcelain --for-init "$TMP/v_obs"

# A cache dir alone is NOT evidence: any tool can drop a .codegraph anywhere,
# which is precisely how $HOME came to look like a vault (MYC-4028).
mkdir -p "$TMP/cacheonly/.codegraph"
want_token "B6 cache dir is not evidence" REFUSE_NOT_A_VAULT 1 -- \
  run_sandboxed "$FAKEHOME" python3 "$CVT" --porcelain --for-init "$TMP/cacheonly"

echo
echo "--- C. end-to-end: relocate-machinery-sidecar.sh ---"
want_rc "C1 sidecar helper refuses \$HOME" 1 -- \
  run_sandboxed "$FAKEHOME" bash "$SIDECAR_SH" "$FAKEHOME" --sidecar "$TMP/side_c1"
if [ -e "$FAKEHOME/.git" ]; then
  fail "C2 no repo created in \$HOME — .git EXISTS after the refusal"
else
  pass "C2 no repo created in \$HOME"
fi

echo
echo "--- D. end-to-end: relocate-vault.sh ---"
want_rc "D1 vault helper refuses \$HOME" 1 -- \
  run_sandboxed "$FAKEHOME" bash "$VAULT_SH" "$FAKEHOME" "$TMP/dest_d1" --dry-run

echo
echo "--- E. adversarial ---"
# E1: a genuine vault must still relocate. If this fails the guard is useless
# no matter how many refusals above are green.
REAL="$TMP/realvault"
mkdir -p "$REAL/⚙️ Meta/Sessions" "$REAL/.codegraph"
printf 'note\n' > "$REAL/note.md"
git -C "$REAL" init -q >/dev/null 2>&1
git -C "$REAL" config user.email t@t.test
git -C "$REAL" config user.name Test
git -C "$REAL" config commit.gpgsign false
git -C "$REAL" add note.md >/dev/null 2>&1
git -C "$REAL" commit -qm init >/dev/null 2>&1
want_rc "E1 real vault still relocates" 0 -- \
  run_sandboxed "$FAKEHOME" bash "$SIDECAR_SH" "$REAL" --sidecar "$TMP/side_e1"
if [ -f "$REAL/.git" ] && [ ! -d "$REAL/.git" ]; then
  pass "E1b .git became a pointer file"
else
  fail "E1b .git is not a pointer file after a successful relocation"
fi

# E2: THE one that matters. --force must not open REFUSE_HOME.
want_rc "E2 --force does NOT open \$HOME" 1 -- \
  run_sandboxed "$FAKEHOME" bash "$SIDECAR_SH" "$FAKEHOME" --force --sidecar "$TMP/side_e2"

# E3: fail-closed. A missing checker must refuse, not wave through — the whole
# point is that a broken install cannot silently disable the guard.
CLONE="$TMP/clone"
mkdir -p "$CLONE"
cp "$SIDECAR_SH" "$CLONE/"
cp "$HERE/check-cloud-sync.py" "$CLONE/" 2>/dev/null || true
mkdir -p "$TMP/e3vault"; printf 'n\n' > "$TMP/e3vault/note.md"
want_rc "E3 missing checker fails CLOSED" 1 -- \
  run_sandboxed "$FAKEHOME" bash "$CLONE/relocate-machinery-sidecar.sh" "$TMP/e3vault" --sidecar "$TMP/side_e3"

# E4: the repair path must stay reachable. A user already damaged this way runs
# --rollback against a home-shaped path; gating it would trap them permanently.
# The guard must not be what stops this (any non-1 exit means it did not fire;
# rollback's own "nothing to roll back" is exit 2 and is a legitimate answer).
rb_out="$(run_sandboxed "$FAKEHOME" bash "$SIDECAR_SH" "$FAKEHOME" --rollback --sidecar "$TMP/side_e4" 2>&1)"
rb_rc=$?
if printf '%s' "$rb_out" | grep -q 'There is NO --force for this one'; then
  fail "E4 --rollback was blocked by the vault-target guard (exit $rb_rc) — repair path unreachable"
else
  pass "E4 --rollback is NOT gated by the vault-target guard (exit $rb_rc)"
fi

echo
echo "--- F. the repair path actually repairs ---"
# rollback_git used to ignore the recorded mode and always `mv` the sidecar
# gitdir back to $VAULT/.git. For a fresh-init that is not an undo: it
# MATERIALISES a real .git directory in a place that never had a repo. Anyone
# hit by MYC-4028 who ran the documented repair would have ended up with $HOME
# still a git repo, with the objects now inside their home instead of the
# sidecar. Strictly worse than the pointer file they started with.
FRESH="$TMP/freshvault"
mkdir -p "$FRESH"; printf 'note\n' > "$FRESH/note.md"   # vault evidence, no .git
run_sandboxed "$FAKEHOME" bash "$SIDECAR_SH" "$FRESH" --sidecar "$TMP/side_f" >/dev/null 2>&1
if [ -f "$FRESH/.git" ] && [ ! -d "$FRESH/.git" ]; then
  pass "F1 fresh-init produced a .git pointer"
else
  fail "F1 fresh-init did not produce a .git pointer — fixture is wrong, F2/F3 prove nothing"
fi
if grep -q '"mode": "fresh-init"' "$TMP"/side_f/manifests/*.json 2>/dev/null; then
  pass "F2 the record says fresh-init"
else
  fail "F2 the record does not say fresh-init — rollback cannot tell the modes apart"
fi
run_sandboxed "$FAKEHOME" bash "$SIDECAR_SH" "$FRESH" --sidecar "$TMP/side_f" --rollback >/dev/null 2>&1
if [ -e "$FRESH/.git" ]; then
  fail "F3 after rollback .git still exists — the vault is STILL a git repo"
else
  pass "F3 after rollback the vault is no longer a git repo"
fi
# ...and the undo must not be a delete. Commits may have been made in that repo.
if [ -d "$TMP/side_f/git" ] && [ -n "$(ls -A "$TMP/side_f/git" 2>/dev/null)" ]; then
  pass "F4 the created repo was KEPT in the sidecar, not deleted"
else
  fail "F4 rollback DELETED the created repo — any commits made in it are gone"
fi

echo "--- G. detector: a machine ALREADY in this state ---"
# The guard stops NEW damage. It says nothing about machines already holding a
# repo over $HOME — and that population cannot be enumerated, so the detector is
# the only way they ever find out. Fires on HARM (exposed credentials), not on
# shape, so a deliberate dotfiles repo with a sane .gitignore stays silent.
G_OUT="$(HOOK_PY="$ROOT/hooks/worktree-footprint-signal.py" python3 - <<'PYEOF'
import importlib.util, os, subprocess, sys, tempfile
from pathlib import Path
p = Path(os.environ["HOOK_PY"]); sys.path.insert(0, str(p.parent))
spec = importlib.util.spec_from_file_location("wfs", p)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

def fired(home):
    os.environ["HOME"] = str(home)
    return bool(m.home_is_a_repo_alert())

def key(d):
    (d / ".ssh").mkdir(parents=True, exist_ok=True)
    (d / ".ssh/id_ed25519").write_text("k")

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    h = td / "healthy"; h.mkdir(); key(h)
    print("G1", "PASS" if not fired(h) else "FAIL", "healthy home stays silent")

    hit = td / "hit"; hit.mkdir(); key(hit)
    subprocess.run(["git", "init", "-q", str(hit)], check=True)
    print("G2", "PASS" if fired(hit) else "FAIL", "exposed credentials in a home repo FIRE")

    dot = td / "dotfiles"; dot.mkdir(); key(dot)
    subprocess.run(["git", "init", "-q", str(dot)], check=True)
    (dot / ".gitignore").write_text(".ssh/\n.aws/\n.netrc\n")
    print("G3", "PASS" if not fired(dot) else "FAIL", "dotfiles repo that ignores its secrets stays silent")

    bare = td / "bare"; bare.mkdir()
    subprocess.run(["git", "init", "-q", str(bare)], check=True)
    print("G4", "PASS" if not fired(bare) else "FAIL", "home repo with no credentials present stays silent")

    os.environ["HOME_REPO_ALERT_BYPASS"] = "1"
    print("G5", "PASS" if not fired(hit) else "FAIL", "bypass silences a genuine hit")
    del os.environ["HOME_REPO_ALERT_BYPASS"]
PYEOF
)"
while IFS= read -r line; do
  case "$line" in
    *" PASS "*) pass "${line#* PASS }" ;;
    *" FAIL "*) fail "${line#* FAIL }" ;;
  esac
done <<< "$G_OUT"

echo
if [ "$fails" -eq 0 ]; then
  echo "ALL PASS — check-vault-target"
  exit 0
fi
echo "$fails FAILURE(S) — check-vault-target"
exit 1
