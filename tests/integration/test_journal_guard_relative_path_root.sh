#!/usr/bin/env bash
#
# Integration test: warn-journal-saved-without-context.py must not stitch a vault
# root across a shell operator, and must resolve the relative-path write form.
#
# The bug (measured 2026-08-28, on a real journal save that was wrongly blocked):
# entries are written as
#
#     cd "<vault>" && cat > "<emoji> Journals/August 2026/e.md" << 'EOF'
#
# The journal path here is RELATIVE. _vault_root()'s optional emoji-prefix segment
# was `(?:[^/\n]*\s)?` -- bounded only on '/' and newline, so it happily swallowed
#
#     vault" && cat > "<emoji>
#
# and matched `Journals/`. Group 1 then stopped at `/Users/me`, i.e. the vault's
# PARENT. The hook looked for the preflight marker one directory too high.
#
# Both failure directions are real, and the quiet one is worse:
#   - parent has no Meta dir  -> marker never found -> a correct save is BLOCKED
#                                (loud, which is how this got caught at all)
#   - parent HAS a Meta dir   -> a stale marker satisfies the check -> a journal
#                                with NO context ships SILENTLY, which is the exact
#                                failure the guard exists to prevent
#
# The comment on _vault_root claimed "quotes bound the segment on the Bash path".
# Quotes bound GROUP 1. They did not bound the optional prefix segment. That gap
# is the whole bug.
#
# Asserted against the hook's OWN predicates, so this tests shipped code rather
# than a reimplementation. Every claim carries a control.
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
PY="${PYTHON:-python3}"

"$PY" - "$REPO_ROOT" <<'PYEOF'
import importlib.util, os, pathlib, sys, tempfile

repo = pathlib.Path(sys.argv[1])
path = repo / "hooks" / "warn-journal-saved-without-context.py"
spec = importlib.util.spec_from_file_location("journal_guard", path)
m = importlib.util.module_from_spec(spec)
sys.modules["journal_guard"] = m
try:
    spec.loader.exec_module(m)
except SystemExit:
    pass

vault_root, norm = m._vault_root, m._norm
EMOJI = "\U0001F4D3"
failures = 0

def check(label, got, want):
    global failures
    if got == want:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label} (got {got!r}, want {want!r})")
        failures += 1

# ---------------------------------------------------------------------------
# 1. _vault_root must not stitch a root across a shell operator.
#    A relative journal path carries NO absolute root, so the honest answer is
#    None. Returning the parent dir is worse than returning nothing, because the
#    caller cannot tell a wrong answer from a right one.
# ---------------------------------------------------------------------------
STITCH_CASES = [
    ("cd + emoji folder",
     'cd "/Users/me/vault" && cat > "%s Journals/August 2026/e.md" << \'EOF\'' % EMOJI),
    ("cd + plain folder",
     'cd "/Users/me/vault" && cat > "Journals/August 2026/e.md" << \'EOF\''),
    ("cd single-quoted + tee",
     "cd '/Users/me/vault' && tee 'Journals/August 2026/e.md'"),
    ("semicolon operator",
     'cd "/Users/me/vault"; cat > "%s Journals/August 2026/e.md"' % EMOJI),
]
for label, raw in STITCH_CASES:
    check("no stitched root: %s" % label, vault_root(norm(raw)), None)

# ---------------------------------------------------------------------------
# 2. _resolve_root must recover the real vault from the `cd` target / cwd, and
#    must reject a candidate that is not actually a vault.
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    tmp = os.path.realpath(tmp)
    vault = os.path.join(tmp, "vault")
    os.makedirs(os.path.join(vault, EMOJI + " Journals", "August 2026"))
    plain = os.path.join(tmp, "plainvault")
    os.makedirs(os.path.join(plain, "Journals", "August 2026"))
    notvault = os.path.join(tmp, "notavault")
    os.makedirs(notvault)

    cmd = 'cd "%s" && cat > "%s Journals/August 2026/e.md" << \'EOF\'' % (vault, EMOJI)
    check("resolve from cd target (emoji vault)", m._resolve_root(norm(cmd), None), vault)

    cmd_plain = 'cd "%s" && cat > "Journals/August 2026/e.md"' % plain
    check("resolve from cd target (plain Journals)", m._resolve_root(norm(cmd_plain), None), plain)

    # No cd in the command: the session cwd is the vault.
    check("resolve from payload cwd",
          m._resolve_root(norm('cat > "%s Journals/August 2026/e.md"' % EMOJI), vault), vault)

    # An absolute journal path still wins, and still resolves the same as before.
    abs_cmd = 'cat > "%s/%s Journals/August 2026/e.md"' % (vault, EMOJI)
    check("absolute path still wins over cwd", m._resolve_root(norm(abs_cmd), notvault), vault)

    # CONTROL: a directory with no Journals folder is NOT accepted as a vault.
    # Without this, any wrong candidate silently becomes the root -- which is
    # how the original bug produced a confident answer about the wrong place.
    check("CONTROL: non-vault cd target is rejected",
          m._resolve_root(norm('cd "%s" && cat > "Journals/August 2026/e.md"' % notvault), None),
          None)
    check("CONTROL: non-vault cwd is rejected",
          m._resolve_root(norm('cat > "Journals/August 2026/e.md"'), notvault), None)

    # CONTROL: the parent of a real vault must never validate. This is the exact
    # value the bug returned; it has to be rejected on its own merits, not merely
    # be unreachable by the current regex.
    check("CONTROL: vault parent is rejected as a root",
          m._resolve_root(norm('cd "%s" && cat > "Journals/August 2026/e.md"' % tmp), None),
          None)

# ---------------------------------------------------------------------------
# 3. CONTROLS: previously-correct behaviour must not regress.
# ---------------------------------------------------------------------------
check("CONTROL: POSIX absolute path root unchanged",
      vault_root(norm("/Users/me/vault/Journals/May 2026/e.md")), "/Users/me/vault")
check("CONTROL: Windows absolute path root unchanged",
      vault_root(norm(r"C:\Users\me\vault\Journals\May 2026\e.md")), "C:/Users/me/vault")
check("CONTROL: emoji vault folder root unchanged",
      vault_root(norm("/Users/me/v/%s Journals/May 2026/e.md" % EMOJI)), "/Users/me/v")
check("CONTROL: heredoc with absolute path root unchanged",
      vault_root(norm("cat > '/Users/me/vault/Journals/May 2026/e.md' <<EOF")), "/Users/me/vault")
check("CONTROL: a URL is still not parsed as a drive root",
      vault_root(norm("see http://host/x/Journals/May 2026/e.md")), "//host/x")

print()
if failures:
    print("FAILED (%d)" % failures)
    sys.exit(1)
print("ALL PASS (16 assertions, controls included)")
PYEOF
