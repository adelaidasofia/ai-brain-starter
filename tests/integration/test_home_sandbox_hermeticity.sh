#!/usr/bin/env bash
# Static guard: a test that redirects HOME must redirect USERPROFILE with it.
#
# THE BUG THIS LOCKS OUT (MYC-3536)
#
# Tests sandbox HOME so the developer's real ~/.claude is never touched. On POSIX
# that works. On Windows it does not: Python resolves "~" through
# ntpath.expanduser, which reads USERPROFILE and ignores HOME entirely. So a test
# that sets only HOME looks sandboxed, passes review, and runs against the real
# ~/.claude on every Windows machine.
#
# It did. On 2026-07-30 the suite rewrote the real ~/.claude/settings.json,
# repointing 95 of 111 hook entries at hook_runner.py inside the throwaway git
# worktree the tests happened to run from. Deleting that worktree left every hook
# launching a file that no longer existed; CPython exits 2 for "can't open file",
# and exit 2 is Claude Code's intentional-BLOCK signal — so every tool call in
# later, unrelated sessions was denied, with nothing tying the failure back to
# the test run. Same fail-closed class as #375 and #409.
#
# SCOPE — three things the first version of this guard got wrong, all found by
# probing it rather than reading it:
#   1. It compared per FILE, not per SITE: a file that sandboxed three call sites
#      and leaked a fourth passed, because USERPROFILE appeared *somewhere*.
#      Pairing is now checked in a window around each HOME site.
#   2. It scanned only tests/integration/*.sh. Python suites set HOME too —
#      services/health-mcp/tests/test_shortcut_bridge.py redirects HOME so
#      db_path() (which reads Path.home()) lands in a tmpdir, which on Windows
#      means it read and wrote the developer's REAL health database.
#   3. Its own negative control only proved the detector fired, never that a
#      correctly-paired site was left alone.
#
# The companion runtime tripwire in scripts/ci.sh watches the real ~/.claude for
# actual writes. This static half catches the mistake at author time on Linux CI,
# where the runtime half is blind because HOME works there.
#
# Stdlib bash + python3 only. Exit 0 = pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 1

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL  $1 :: $2"; }

# Tests that assign HOME but are deliberately NOT converted. Each needs a reason.
# Rule: the test must not reach the installer/merge path or any other writer of
# ~/.claude. Verified 2026-07-31 by running the full suite under a decoy
# USERPROFILE and confirming none of these wrote into the decoy home. A test that
# starts touching ~/.claude comes OFF this list rather than staying on it.
read -r -d '' SANDBOX_EXEMPT <<'EOF' || true
tests/integration/test_granola_sync_offline.sh          offline Granola parse; HOME only picks the config dir
tests/integration/test_meeting_workflow_trigger_hook.sh  trigger-phrase matching against a temp vault
tests/integration/test_post_update_email_ask.sh         prompt-copy assertions; no ~/.claude write
tests/integration/test_resource_aware_session_close.sh  load-shedding arithmetic against a temp vault
tests/integration/test_scan_prior_failclosed_scrub.sh   scrubber runs entirely inside its own tmpdir
tests/integration/test_scan_prior_single_instance.sh    lockfile contention inside its own tmpdir
tests/integration/test_session_coordination_guards.sh   session lockfiles inside its own tmpdir
tests/integration/test_vault_script_sync.sh             import-closure check over repo files only
EOF

run_scan() {  # run_scan <extra-file-to-include-or-empty>
  python3 - "$1" <<'PY'
import os, re, subprocess, sys

extra = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None

EXEMPT = set()
for line in os.environ.get("SANDBOX_EXEMPT", "").splitlines():
    line = line.strip()
    if line:
        EXEMPT.add(line.split()[0])

# Pairing is checked by VALUE, not by proximity. A window-based check looks
# right and is not: given
#     export HOME="$SAFE"
#     export USERPROFILE="$SAFE"
#     HOME="$LEAKY" python3 thing.py     <-- the leak
# the third line sits one line below a USERPROFILE and passes any window rule.
# Requiring a USERPROFILE assigned the SAME value has no such hole.
#
# Captures the assigned value in every form these suites actually use:
#   shell:   HOME=x / export HOME=x / env HOME=x
#   python:  dict(os.environ, HOME=x) / os.environ["HOME"] = x
#            monkeypatch.setenv("HOME", x) / env={"HOME": x, ...}
def _val_res(name):
    return [
        re.compile(r'(?<![\w.])' + name + r'=([^\s,)]+)'),          # NAME=value
        re.compile(r'["\']' + name + r'["\']\s*[:,]\s*([^\s,})]+)'),  # "NAME": value / setenv("NAME", value)
        re.compile(r'\[["\']' + name + r'["\']\]\s*=\s*([^\s,)]+)'),  # os.environ["NAME"] = value
    ]

HOME_VAL = _val_res("HOME")
USER_VAL = _val_res("USERPROFILE")
HOME_RE = re.compile(r'(?:(?<![\w.])HOME=)|(?:["\']HOME["\'])')
# Going through the helper is pairing by construction.
HELPER_RE = re.compile(r'\b(sandbox_home|run_sandboxed)\b')

def values(line, regexes):
    out = set()
    for r in regexes:
        for m in r.finditer(line):
            v = m.group(1).strip().rstrip(',)').strip('"\'')
            if v:
                out.add(v)
    return out

def tracked(*patterns):
    out = subprocess.run(["git", "ls-files", "-z", "--"] + list(patterns),
                         capture_output=True, text=True).stdout
    return [p for p in out.split("\0") if p]

files = tracked(
    "tests/integration/test_*.sh",
    "tests/**/test_*.py", "tests/test_*.py",
    "scripts/test_*.py", "hooks/test_*.py",
    "services/**/test_*.py",
)
if extra:
    files = [extra]

offenders, sites_checked, files_checked = [], 0, 0

# This guard documents the very patterns it hunts for, and its negative-control
# fixtures are deliberate offenders. Scanning itself would be self-indicting.
SELF = "tests/integration/test_home_sandbox_hermeticity.sh"

for f in files:
    if not extra and f == SELF:
        continue
    try:
        lines = open(f, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        continue
    home_lines = [i for i, l in enumerate(lines) if HOME_RE.search(l)]
    if not home_lines:
        continue
    files_checked += 1
    if not extra and f in EXEMPT:
        continue
    # Every value USERPROFILE is assigned anywhere in the file.
    paired = set()
    for l in lines:
        paired |= values(l, USER_VAL)
    for i in home_lines:
        line = lines[i]
        if line.lstrip().startswith("#"):
            continue                      # a comment is not a call site
        sites_checked += 1
        if HELPER_RE.search(line):
            continue
        hv = values(line, HOME_VAL)
        # Unparseable HOME mention with no value -> not an assignment we can judge.
        if not hv:
            continue
        if hv & paired:
            continue
        offenders.append(f"{f}:{i+1}: {line.strip()[:88]}")

print(f"FILES={files_checked}")
print(f"SITES={sites_checked}")
for o in offenders:
    print(f"OFFENDER={o}")
PY
}

export SANDBOX_EXEMPT

echo "=== 1. every HOME site pairs with a USERPROFILE (shell + python suites) ==="
OUT="$(run_scan "")"
FILES="$(printf '%s' "$OUT" | sed -n 's/^FILES=//p')"
SITES="$(printf '%s' "$OUT" | sed -n 's/^SITES=//p')"
mapfile -t OFFENDERS < <(printf '%s' "$OUT" | sed -n 's/^OFFENDER=//p')

if [ "${FILES:-0}" -eq 0 ] || [ "${SITES:-0}" -eq 0 ]; then
  bad "detector matched something" "scanned $FILES file(s) / $SITES site(s) — the pattern must have rotted"
else
  ok "scanned $SITES HOME site(s) across $FILES file(s)"
fi

if [ "${#OFFENDERS[@]}" -eq 0 ]; then
  ok "every HOME site pairs with a USERPROFILE"
else
  printf '      %s\n' "${OFFENDERS[@]}" >&2
  bad "HOME-only sandbox" "${#OFFENDERS[@]} site(s) redirect HOME without USERPROFILE, so on Windows they run against the REAL ~/.claude. In shell use tests/integration/lib/sandbox_home.sh (sandbox_home / run_sandboxed); in Python set USERPROFILE alongside HOME"
fi

echo "=== 2. exempt list stays honest (no rows for files that no longer exist) ==="
stale=()
while read -r path _; do
  [ -n "$path" ] || continue
  [ -f "$REPO_ROOT/$path" ] || stale+=("$path")
done <<< "$SANDBOX_EXEMPT"
if [ "${#stale[@]}" -eq 0 ]; then
  ok "every exempt row names a real test"
else
  bad "stale exempt rows" "remove: ${stale[*]}"
fi

echo "=== 3. NEGATIVE CONTROLS: the detector bites, and only where it should ==="
CTL="$(mktemp -d)"
trap 'rm -rf "$CTL"' EXIT

# (a) the per-SITE control — the case the first version of this guard passed.
cat > "$CTL/mixed.sh" <<'EOF'
#!/usr/bin/env bash
export HOME="$SAFE"
export USERPROFILE="$SAFE"
HOME="$LEAKY" python3 thing.py
EOF
n="$(run_scan "$CTL/mixed.sh" | grep -c '^OFFENDER=')"
if [ "$n" -eq 1 ]; then
  ok "a file with one paired and one UNPAIRED site is caught (per-site, not per-file)"
else
  bad "per-site control" "expected exactly 1 offending site, got $n"
fi

# (b) a fully paired file must stay clean.
cat > "$CTL/clean.sh" <<'EOF'
#!/usr/bin/env bash
export HOME="$T"
export USERPROFILE="$T"
EOF
n="$(run_scan "$CTL/clean.sh" | grep -c '^OFFENDER=')"
if [ "$n" -eq 0 ]; then
  ok "a correctly paired file is not flagged"
else
  bad "false-positive control" "a paired file was flagged $n time(s)"
fi

# (c) the Python forms must be understood, not just shell.
cat > "$CTL/pytest_like.py" <<'EOF'
monkeypatch.setenv("HOME", str(tmp_path))
env = dict(os.environ, HOME=str(h))
EOF
n="$(run_scan "$CTL/pytest_like.py" | grep -c '^OFFENDER=')"
if [ "$n" -eq 2 ]; then
  ok "python setenv/dict HOME forms are detected"
else
  bad "python-form control" "expected 2 offending python sites, got $n"
fi

# (d) helper use is pairing by construction.
cat > "$CTL/helper.sh" <<'EOF'
#!/usr/bin/env bash
run_sandboxed "$T" env HOME="$T" python3 x.py
EOF
n="$(run_scan "$CTL/helper.sh" | grep -c '^OFFENDER=')"
if [ "$n" -eq 0 ]; then
  ok "a run_sandboxed call site is not flagged"
else
  bad "helper control" "a helper call site was flagged"
fi

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
echo "PASS: HOME sandboxing is hermetic on Windows as well as POSIX (MYC-3536)"
