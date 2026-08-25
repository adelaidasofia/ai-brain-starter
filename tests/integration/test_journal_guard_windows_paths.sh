#!/usr/bin/env bash
#
# Integration test: warn-journal-saved-without-context.py resolves Windows paths.
#
# Every path pattern in that hook is written with forward slashes. On Windows a
# journal write arrives as `C:\vault\Journals\May 2026\x.md`, which matches none
# of them, so the gate never opens: no warning, no error, no signal at all. A
# guard that silently does nothing on a whole platform is worse than an absent
# one, because the user believes it is running.
#
# Two layers have to hold, and a fix to only the first LOOKS correct while still
# failing open one step later:
#   1. JOURNAL_PATH_RE must match the path (the gate).
#   2. _vault_root() must return a usable root, which means accepting a `C:/`
#      drive root and not only a leading `/`.
#
# Asserted against the hook's OWN predicates, so this tests shipped code rather
# than a reimplementation of it. Every claim carries a control.
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
PY="${PYTHON:-python3}"

"$PY" - "$REPO_ROOT" <<'PYEOF'
import importlib.util, pathlib, sys

repo = pathlib.Path(sys.argv[1])
path = repo / "hooks" / "warn-journal-saved-without-context.py"
spec = importlib.util.spec_from_file_location("journal_guard", path)
m = importlib.util.module_from_spec(spec)
sys.modules["journal_guard"] = m
try:
    spec.loader.exec_module(m)
except SystemExit:
    pass

rx, vault_root, norm = m.JOURNAL_PATH_RE, m._vault_root, m._norm
failures = 0

def check(label, got, want):
    global failures
    if got == want:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label} (got {got!r}, want {want!r})")
        failures += 1

CASES = [
    ("POSIX file path",      "/Users/me/vault/Journals/May 2026/e.md",              "/Users/me/vault"),
    ("Windows file path",    r"C:\Users\me\vault\Journals\May 2026\e.md",           "C:/Users/me/vault"),
    ("POSIX bash heredoc",   "cat > '/Users/me/vault/Journals/May 2026/e.md' <<EOF", "/Users/me/vault"),
    ("Windows bash heredoc", r"cat > 'C:\Users\me\vault\Journals\May 2026\e.md' <<EOF", "C:/Users/me/vault"),
    ("emoji vault folder",   "/Users/me/v/📓 Journals/May 2026/e.md",                "/Users/me/v"),
]
for label, raw, want_root in CASES:
    t = norm(raw)
    check(f"{label}: gate opens", bool(rx.search(t)), True)
    check(f"{label}: vault root resolves", vault_root(t), want_root)

# --- controls: things that must NOT change ----------------------------------
check("CONTROL: non-journal path does not open the gate",
      bool(rx.search(norm("/Users/me/vault/Notes/x.md"))), False)
check("CONTROL: Windows non-journal path does not open the gate",
      bool(rx.search(norm(r"C:\Users\me\vault\Notes\x.md"))), False)
# A URL's `p:` must never be read as a drive letter. Behaviour here is
# unchanged from before the drive-letter alternative was added.
check("CONTROL: a URL is not parsed as a drive root",
      vault_root(norm("see http://host/x/Journals/May 2026/e.md")), "//host/x")

print()
if failures:
    print(f"FAILED ({failures})")
    sys.exit(1)
print("ALL PASS (13 assertions, controls included)")
PYEOF
