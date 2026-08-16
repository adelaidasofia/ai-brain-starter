#!/usr/bin/env bash
# Static guard: no tracked shell script may use a bash-4-only feature.
#
# THE BUG THIS LOCKS OUT
#
# macOS ships bash 3.2.57 (2007) as /bin/bash and that is the ONLY bash on an
# unmodified Mac. scripts/PORTABILITY.md section 4 has banned bash-4 features
# since it was written. Nothing enforced it, and on 2026-08-15
# tests/integration/test_home_sandbox_hermeticity.sh was found building its
# offender list with `mapfile`. Under /bin/bash the builtin does not exist; the
# script ran `set -uo pipefail` without `-e`, so the failure was NOT fatal. The
# array stayed unset, its `${#...[@]}` test errored to stderr, and the check
# that was the file's entire purpose -- "every HOME site pairs with a
# USERPROFILE" -- never evaluated. The run still printed PASS=6 FAIL=0 and
# exited 0. The static half of the Windows HOME-sandbox gate had been dead on
# every Mac since it shipped, reporting success the whole time.
#
# WHY THE TWO GATES THAT ALREADY EXIST CANNOT CATCH THIS -- both verified, not
# assumed, before this file was written:
#
#   * The `bash32-syntax` job in .github/workflows/lint.yml runs a real
#     /bin/bash 3.2 on macos-latest, but only `-n` (parse). All four constructs
#     below PARSE cleanly on 3.2 and fail at RUN time: mapfile/readarray exit
#     127 "command not found", `declare -A` exits 2 "invalid option", `${v^^}`
#     exits 1 "bad substitution". `bash -n` is green on every one.
#   * `scripts/shellcheck.sh` has no bash-version model at all: shellcheck
#     -S style exits 0 on a file using both `mapfile` and `declare -A`.
#
# So the property is unreachable from either runner, and CI on ubuntu (bash 5)
# is green by construction for the whole class. A STATIC scan is the answer
# precisely because it asks about the CODE, not about the interpreter that
# happens to be running: it gives the same verdict on any runner.
#
# SCOPE -- what this deliberately does NOT check, so the coverage is not
# silently narrower than the name suggests:
#   * `|&` and `&>>` (also bash 4+) are NOT scanned. Several .sh here embed
#     Python heredocs whose regexes contain those exact byte pairs
#     (e.g. re.split(r"\|\||&&|[|;&]", cmd)), and flagging them would be noise
#     that trains people to bypass the gate. They have never appeared as real
#     shell here.
#   * `"${arr[@]}"` on an empty array under `set -u` (PORTABILITY.md section 4)
#     is a runtime property, not a lexical one, and cannot be decided by a scan.
#
# Stdlib bash + python3 only. Exit 0 = pass.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT" || exit 1

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL  $1 :: $2"; }

# Every check must report exactly once; asserted at the bottom. This file runs
# `set -u` without `-e` on purpose (each check should report, not stop the run),
# which is exactly the condition that let the original bug hide. Bump when
# adding a check.
EXPECTED_CHECKS=11

# Files allowed to use a banned construct, each with a reason. Format:
# "<path>  <reason>". Empty is the healthy state -- a row here is a debt, not a
# preference, and the fix is almost always the portable idiom in
# scripts/PORTABILITY.md section 4. Silence is the only banned state: a file
# that needs an exception gets a row, never a quiet carve-out in the scanner.
read -r -d '' BASH32_EXEMPT <<'EOF' || true
EOF

run_scan() {  # run_scan <extra-file-to-scan-instead-of-the-repo-or-empty>
  python3 - "$1" <<'PY'
import os, re, subprocess, sys

extra = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None

EXEMPT = set()
for line in os.environ.get("BASH32_EXEMPT", "").splitlines():
    line = line.strip()
    if line:
        EXEMPT.add(line.split()[0])

# The bash-4-only constructs banned by scripts/PORTABILITY.md section 4. Each
# carries the portable replacement, because a gate that only says "no" gets
# worked around.
BANNED = [
    (re.compile(r'\b(?:mapfile|readarray)\b'),
     "mapfile/readarray are bash 4+ (readarray -d is 4.4+); macOS /bin/bash has neither. "
     "Use a read loop: arr=(); while IFS= read -r x || [ -n \"$x\" ]; do arr+=(\"$x\"); done < <(...)"),
    (re.compile(r'\b(?:declare|local|typeset)\s+-[A-Za-z]*A[A-Za-z]*\b'),
     "associative arrays (declare -A) are bash 4+. Use parallel indexed arrays, or a "
     "python3/awk helper."),
    (re.compile(r'\$\{[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]*\])?(?:\^\^|,,|\^|,)'),
     "${v^^} / ${v,,} case conversion is bash 4+. Use tr '[:lower:]' '[:upper:]'."),
]

# Strip comments so the RULE can be discussed in the files it governs. Without
# this the guard flags scripts/shellcheck.sh and the hermeticity test, both of
# which name `mapfile` in a comment explaining why they avoid it -- and a guard
# whose first act is to flag the documentation of its own rule gets disabled.
# Heuristic, deliberately: a '#' is a comment start when it is outside single
# and double quotes and begins the line or follows whitespace.
def strip_comment(line):
    sq = dq = 0
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\":
            i += 2
            continue
        if c == "'" and dq % 2 == 0:
            sq += 1
        elif c == '"' and sq % 2 == 0:
            dq += 1
        elif c == "#" and sq % 2 == 0 and dq % 2 == 0 and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
        i += 1
    return line

def tracked(*patterns):
    out = subprocess.run(["git", "ls-files", "-z", "--"] + list(patterns),
                         capture_output=True, text=True).stdout
    return [p for p in out.split("\0") if p]

# Scope by PROPERTY (is this a shell script?), never by NAME (*.sh).
#
# The first version of this gate globbed '*.sh', which is a denylist against an
# open set: a hook named `rotate-logs` with no extension, or a `.bash`, carries
# `mapfile` just as well and is invisible to a suffix match. Measured when this
# was written: zero such files tracked here -- so this closes the hole while it
# is still theoretical rather than after a file walks through it. The existing
# `bash32-syntax` CI job has the same `-name '*.sh'` scope and the same hole.
#
# A shebang is the property. The regex needs a word-boundary `sh` token, so it
# matches sh/bash/ksh/zsh/ash/dash invocations but not `python3`, and not the
# embedded-but-not-bounded `sh` in `fish` or `osascript`. Controlled below in
# both directions.
SHEBANG_RE = re.compile(rb'^#!.*\b(?:ba|k|z|a|da)?sh\b')

def is_shell(path):
    if path.endswith((".sh", ".bash", ".ksh", ".zsh")):
        return True
    try:
        with open(path, "rb") as fh:
            first = fh.read(128).split(b"\n", 1)[0]
    except OSError:
        return False
    return bool(SHEBANG_RE.match(first))

if extra:
    # Controls scan the named fixture regardless of classification, so the
    # detector checks stay independent of the scope checks. ISSHELL reports what
    # the SCOPE rule would have decided, so it can be asserted on its own.
    files = [extra]
    print(f"ISSHELL={1 if is_shell(extra) else 0}")
else:
    files = [p for p in tracked() if is_shell(p)]

# This file spells out every pattern it hunts for, in prose and in regex, and
# its controls are deliberate offenders. Scanning itself would be self-indicting.
SELF = "tests/integration/test_bash32_portability.sh"

offenders, files_checked = [], 0
for f in files:
    if not extra and (f == SELF or f in EXEMPT):
        continue
    try:
        lines = open(f, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        continue
    files_checked += 1
    for i, raw in enumerate(lines):
        line = strip_comment(raw)
        if not line.strip():
            continue
        for rx, fix in BANNED:
            if rx.search(line):
                offenders.append(f"{f}:{i+1}: {line.strip()[:80]}  ->  {fix}")
                break

print(f"FILES={files_checked}")
for o in offenders:
    print(f"OFFENDER={o}")
PY
}

# bash-3.2-safe array fill: no mapfile (this file, of all files, must obey its
# own rule). The `|| [ -n "$line" ]` tail keeps the final record when command
# substitution has stripped the trailing newline.
collect_offenders() {  # collect_offenders <run_scan-output>
  OFFENDERS=()
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] && OFFENDERS+=("$line")
  done < <(printf '%s' "$1" | sed -n 's/^OFFENDER=//p')
}

export BASH32_EXEMPT

echo "=== 1. no tracked shell script uses a bash-4-only feature ==="
OUT="$(run_scan "")"
FILES="$(printf '%s' "$OUT" | sed -n 's/^FILES=//p')"
collect_offenders "$OUT"

# A scan that reaches zero files reports "no offenders" in exactly the same
# words as a clean repo. Assert it actually looked.
if [ "${FILES:-0}" -lt 20 ]; then
  bad "detector reached the repo" "scanned ${FILES:-0} shell file(s) — this repo tracks ~190; the scope rule or the git ls-files call has rotted"
else
  ok "scanned $FILES tracked shell file(s)"
fi

if [ "${#OFFENDERS[@]}" -eq 0 ]; then
  ok "every tracked shell file is bash-3.2 clean"
else
  printf '      %s\n' "${OFFENDERS[@]}" >&2
  bad "bash-4-only feature" "${#OFFENDERS[@]} site(s) use a construct macOS /bin/bash (3.2) cannot run. Each line above names its portable replacement; see scripts/PORTABILITY.md section 4"
fi

echo "=== 2. POSITIVE CONTROLS: each banned construct is actually detected ==="
CTL="$(mktemp -d)"
trap 'rm -rf "$CTL"' EXIT

# One control per pattern, not one for the set. A single control proves the
# SEARCH runs; it says nothing about the other patterns' VOCABULARY, and a
# rotted regex reads as "the repo is clean".
control() {  # control <label> <fixture-basename> <expected-count> <body>
  printf '%s\n' "$4" > "$CTL/$2"
  local n
  n="$(run_scan "$CTL/$2" | grep -c '^OFFENDER=')"
  if [ "$n" -eq "$3" ]; then
    ok "$1"
  else
    bad "control: $1" "expected $3 offending site(s), got $n"
  fi
}

control "mapfile is caught"      "m.sh"  1 'mapfile -t A < <(echo x)'
control "readarray is caught"    "r.sh"  1 'readarray -t A < <(echo x)'
control "declare -A is caught"   "d.sh"  1 'declare -A MAP'
control "\${v^^} is caught"      "c.sh"  1 'echo "${name^^}"'

echo "=== 3. NEGATIVE CONTROLS: the detector does not bite what it should not ==="

# The portable idioms PORTABILITY.md tells people to use must stay clean, or the
# gate punishes the fix it recommends.
control "the portable read-loop idiom is not flagged" "clean.sh" 0 'files=()
while IFS= read -r -d "" f; do files+=("$f"); done < <(git ls-files -z)
upper="$(printf "%s" "$name" | tr "[:lower:]" "[:upper:]")"
if [ "${#files[@]}" -eq 0 ]; then echo none; fi'

# The false-positive class that would otherwise flag scripts/shellcheck.sh and
# the hermeticity guard on their own explanatory comments.
control "a comment naming the banned construct is not flagged" "cmt.sh" 0 '# Built bash-3.2-safe: no mapfile / readarray, no declare -A, no ${v^^}.
echo ok   # mapfile would go here on bash 4'

echo "=== 4. SCOPE CONTROLS: shell files are found by property, not by suffix ==="

# The detector controls above all use .sh fixtures, so they prove the REGEXES
# and say nothing about WHICH FILES get scanned. These assert the scope rule
# itself, in both directions -- an over-narrow scope is how a gate reports a
# clean repo it never fully read.
scope_control() {  # scope_control <label> <fixture-name> <expected-isshell> <body>
  printf '%s\n' "$4" > "$CTL/$2"
  local v
  v="$(run_scan "$CTL/$2" | sed -n 's/^ISSHELL=//p')"
  if [ "${v:-x}" = "$3" ]; then
    ok "$1"
  else
    bad "scope: $1" "expected ISSHELL=$3, got '${v:-<none>}'"
  fi
}

# The case a '*.sh' glob misses: hooks/rotate-logs.sh was the real offender this
# gate caught, and nothing but convention stops the next one shipping without
# the suffix.
scope_control "an extensionless #!/bin/bash file IS in scope" "rotate-logs" 1 '#!/bin/bash
mapfile -t X < <(echo y)'

# The other direction. A python file may legitimately contain the token
# `mapfile` (a dict name, a docstring); scanning it would be a false positive.
scope_control "a #!/usr/bin/env python3 file is NOT in scope" "tool.py" 0 '#!/usr/bin/env python3
mapfile = {}'

# `fish` and `osascript` both CONTAIN the letters sh. The word-boundary in
# SHEBANG_RE is the only thing keeping them out, and a careless loosening of
# that regex would silently widen scope to every AppleScript in the tree.
scope_control "a #!/usr/bin/osascript file is NOT in scope" "notify" 0 '#!/usr/bin/osascript
display notification "hi"'

echo
echo "PASS=$PASS FAIL=$FAIL"
# Fail loud when a check did not EVALUATE. `set -u` without `-e` means a check
# killed mid-run prints to stderr and simply never reports, and the surviving
# checks render a clean PASS=N FAIL=0 indistinguishable from real success. That
# is the shape of the bug this whole file exists to prevent; it must not be the
# shape of this file's own failure.
if [ "$((PASS + FAIL))" -ne "$EXPECTED_CHECKS" ]; then
  echo "FAIL  check-count invariant :: only $((PASS + FAIL)) of $EXPECTED_CHECKS checks reported — one did not evaluate. Read stderr above. This is NOT a pass." >&2
  exit 1
fi
[ "$FAIL" -eq 0 ] || exit 1
echo "PASS: every tracked shell file runs on macOS /bin/bash 3.2 (scripts/PORTABILITY.md section 4)"
