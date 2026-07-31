#!/usr/bin/env bash
# Test: vault-command-nudges.py blocks a destructive `rm` whose TARGET resolves
# to a vault root or one of its top-level folders -- however that target is
# spelled.
#
# WHAT WAS BROKEN
#   The rule advertised in the module docstring as
#       "Blocks `rm -rf` against the vault root or top-level emoji folders"
#   was, verbatim:
#       ...rm\s+(?:-[A-Za-z]*[rRf][A-Za-z]*\s+)+"?(?:$HOME/vault|⚙️ Meta|...)
#   Two independent defects, both silent:
#
#   1. `$HOME/vault` is a SHELL string in a PYTHON regex. `$` is an end-of-line
#      anchor, so that branch means "end of line, then the literal text
#      HOME/vault" and cannot match any input on any machine. The vault ROOT --
#      the single most destructive target there is -- was never blocked at all.
#   2. The emoji branches are literal names anchored immediately after the
#      flags, so only a bare relative `rm -rf "⚙️ Meta"` ever matched. The same
#      deletion spelled absolutely, `rm -rf "/Users/x/Brain/⚙️ Meta"`, passed.
#
#   Net: of the whole advertised rule, one spelling of one case worked.
#
# WHY A NAME-MATCHING TEST WOULD REPRODUCE THE BLIND SPOT
#   Asserting on the four hardcoded folder names would pass against the broken
#   rule for case 3 and prove nothing about 1 or 2. So the vault here is named
#   "Brain", its folders include a NON-hardcoded one ("Archive"), and
#   $VAULT_ROOT points at a DECOY vault in every single assertion. A rule that
#   pattern-matches names, or reads the env var, cannot pass this file.
#
# Bash + python3 only, no network. exit 0 = the rule matches resolved targets.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/vault-command-nudges.py"

FAILURES=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1" >&2; FAILURES=$((FAILURES + 1)); }

# python's tempfile, not `mktemp -d`: under Git Bash on Windows mktemp hands
# back an MSYS path ("/tmp/tmp.XXXX") that native python reinterprets against
# the current drive. Every assertion compares a path the SHELL built against a
# vault python RESOLVED, so both must agree on what a path is.
TMP="$(python3 -c 'import tempfile; print(tempfile.mkdtemp(prefix="rm-rf-target-").replace(chr(92), "/"))')"
if [ -z "$TMP" ] || [ ! -d "$TMP" ]; then
  echo "FAIL: could not create a tmpdir python and the shell both agree on" >&2
  exit 1
fi
trap 'rm -rf "$TMP"' EXIT
OUT="$TMP/out.txt"
ERR="$TMP/err.txt"
VAULT="$TMP/Brain"          # the vault under test -- NOT named "vault"
DECOY="$TMP/decoy-vault"    # what $VAULT_ROOT names in every assertion
CODEREPO="$TMP/coderepo"    # not a vault: the over-fire control
LOOKALIKE="$TMP/Brain-backup"

# --------------------------------------------------------------------------
# fixture (built in python: these paths carry emoji, and a portability gate
# should not depend on the shell's locale handling of them)
# --------------------------------------------------------------------------
python3 - "$TMP" <<'PY'
import sys
from pathlib import Path

tmp = Path(sys.argv[1])

# PRECONDITION. Detection walks UP from the target for a Meta-suffixed folder,
# so a stray "*Meta" directory above the tmpdir would make every not-a-vault
# control resolve to it and quietly stop asserting anything.
for anc in [tmp, *tmp.parents]:
    try:
        hit = next((c.name for c in anc.iterdir()
                    if c.is_dir() and c.name.endswith("Meta")), None)
    except OSError:
        continue
    if hit:
        print(f"FIXTURE-UNUSABLE {anc}/{hit}", flush=True)
        sys.exit(3)

for v in ("Brain", "decoy-vault"):
    (tmp / v / "⚙️ Meta").mkdir(parents=True, exist_ok=True)
# A top-level folder that is NOT one of the four names the old rule hardcoded.
# If this one blocks, the rule is resolving targets rather than matching names.
(tmp / "Brain" / "Archive").mkdir(exist_ok=True)
(tmp / "Brain" / "📓 Journals").mkdir(exist_ok=True)
(tmp / "Brain" / "⚙️ Meta" / "rules").mkdir(exist_ok=True)
(tmp / "coderepo" / "build").mkdir(parents=True, exist_ok=True)
# Same prefix as the vault, one directory up, no Meta folder -> not a vault.
(tmp / "Brain-backup" / "⚙️ Meta-ish").mkdir(parents=True, exist_ok=True)
print("FIXTURE-OK", flush=True)
PY
if [ $? -ne 0 ]; then
  echo "FAIL: fixture could not be built hermetically (see FIXTURE-UNUSABLE above)." >&2
  exit 1
fi

# CLAUDE_CWD is cleared: the hook prefers it over the payload's cwd, and an
# ambient value from the harness running this suite would retarget every
# assertion at the real machine.
run_rm() {  # <command> <cwd> -> sets RC
  local payload
  payload="$(python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[1]},"cwd":sys.argv[2]}))' "$1" "$2")"
  printf '%s' "$payload" | env -u CLAUDE_CWD "VAULT_ROOT=$DECOY" \
    python3 "$HOOK" >"$OUT" 2>"$ERR"
  RC=$?
}

expect_block() {  # <label> <command> <cwd>
  run_rm "$2" "$3"
  if [ "$RC" -eq 2 ] && grep -q "BLOCKED" "$ERR"; then
    pass "$1"
  else
    fail "$1 -- expected a block, got rc=$RC"
    sed 's/^/  /' "$ERR" >&2
  fi
}

expect_allow() {  # <label> <command> <cwd>
  run_rm "$2" "$3"
  if [ "$RC" -eq 0 ]; then
    pass "$1"
  else
    fail "$1 -- expected to pass, got rc=$RC"
    sed 's/^/  /' "$ERR" >&2
  fi
}

# --------------------------------------------------------------------------
# 1. positives: the vault root and its top-level folders, however spelled
# --------------------------------------------------------------------------
# Case 1 is the one the `$HOME/vault` branch could never match on any machine.
expect_block "abs path to the vault ROOT blocks" \
  "rm -rf \"$VAULT\"" "$CODEREPO"

# Case 2 is the one the name-anchored branches missed: same folder as case 3,
# spelled absolutely, from a cwd outside the vault entirely.
expect_block "abs path to a top-level emoji folder blocks" \
  "rm -rf \"$VAULT/⚙️ Meta\"" "$CODEREPO"

# Case 3 is the ONLY spelling the old rule ever caught -- kept so the rewrite
# cannot regress it.
expect_block "relative top-level folder from a vault cwd blocks" \
  'rm -rf "⚙️ Meta"' "$VAULT"

# The name-independence proof: "Archive" is in none of the four hardcoded
# branches, so this can only pass if targets are being RESOLVED.
expect_block "a top-level folder the old rule never named blocks" \
  "rm -rf \"$VAULT/Archive\"" "$CODEREPO"

expect_block "cd into the vault, then a relative rm, blocks" \
  "cd \"$VAULT\" && rm -rf \"📓 Journals\"" "$CODEREPO"

expect_block "unquoted (backslash-escaped space) spelling blocks" \
  "rm -rf $VAULT/⚙️\\ Meta" "$CODEREPO"

expect_block "flag order and a trailing slash do not evade (rm -fr dir/)" \
  "rm -fr \"$VAULT/⚙️ Meta/\"" "$CODEREPO"

expect_block "split flags and -- do not evade (rm -r -f -- dir)" \
  "rm -r -f -- \"$VAULT\"" "$CODEREPO"

expect_block "'rm -rf .' resolving to the vault root blocks" \
  'rm -rf .' "$VAULT"

# --------------------------------------------------------------------------
# 1b. the command does not have to START with `rm`
#
#     The predecessor's boundary was `(?:^|&&|\|\|?|;(?!;))\s*rm`, so `rm` had
#     to be the first word after a separator. `sudo rm -rf <vault>` -- plausibly
#     the single most likely spelling of this mistake -- did not match it, and
#     neither did a subshell, a brace group, or a plain newline between two
#     commands. Each of these was verified to pass unblocked before the fix.
# --------------------------------------------------------------------------
expect_block "a sudo-prefixed rm blocks" \
  "sudo rm -rf \"$VAULT\"" "$CODEREPO"

expect_block "sudo with its own flag+value blocks (sudo -u root rm)" \
  "sudo -u root rm -rf \"$VAULT\"" "$CODEREPO"

expect_block "an env-prefixed rm, with a VAR=VAL arg, blocks" \
  "env FOO=bar rm -rf \"$VAULT\"" "$CODEREPO"

expect_block "stacked wrappers block (sudo nohup rm)" \
  "sudo nohup rm -rf \"$VAULT\"" "$CODEREPO"

expect_block "a subshell is not a free pass" \
  "(rm -rf \"$VAULT\")" "$CODEREPO"

expect_block "a brace group is not a free pass" \
  "{ rm -rf \"$VAULT\"; }" "$CODEREPO"

# NEWLINE. This one is a regression guard on an internal inconsistency, not on
# the shell: _RM_RE is compiled here and ALSO handed to main() as a prefilter.
# If the compiled copy loses re.MULTILINE while main() keeps applying it, `^`
# means two different things in the two places -- the command prefilters as a
# hit, then no targets are found to judge, and the rule reports nothing at all.
expect_block "a newline-separated rm blocks (prefilter and decider agree on ^)" \
  "cd /tmp
rm -rf \"$VAULT\"" "$CODEREPO"

# --------------------------------------------------------------------------
# 2. negatives: the guard must stay out of everything that is not a vault
# --------------------------------------------------------------------------
# Widening the boundary above must not decay into "any command containing the
# word rm". These would resolve their operands against a VAULT cwd if they
# matched, so a false positive here is a false BLOCK, not a harmless one.
expect_allow "a commit message that merely quotes rm -rf passes" \
  'git commit -m "rm -rf the old thing"' "$VAULT"

expect_allow "an echo that merely mentions rm -rf passes" \
  'echo "run rm -rf later"' "$VAULT"

expect_allow "a command that merely starts with the letters rm passes" \
  'confirm -rf something' "$VAULT"

expect_allow "sudo rm -rf of a non-vault path still passes" \
  'sudo rm -rf build/' "$CODEREPO"

expect_allow "rm -rf build/ in a plain code repo passes" \
  'rm -rf build/' "$CODEREPO"

expect_allow "rm -rf of an abs path in a plain code repo passes" \
  "rm -rf \"$CODEREPO/build\"" "$TMP"

# Same name prefix as the vault, sitting beside it, but no Meta folder of its
# own -- a path-prefix guard would fire here; a resolving one must not.
expect_allow "rm -rf a similarly-named folder outside any vault passes" \
  "rm -rf \"$LOOKALIKE\"" "$CODEREPO"

# The documented boundary: below a top-level folder is the "explicit file
# paths" the block message tells you to use, so it must not itself block.
expect_allow "rm -rf deeper than a top-level folder passes" \
  "rm -rf \"$VAULT/⚙️ Meta/rules\"" "$CODEREPO"

expect_allow "a non-destructive command naming the vault passes" \
  "ls \"$VAULT\"" "$CODEREPO"

run_rm "VAULT_VALIDATOR_BYPASS=1 rm -rf \"$VAULT\"" "$CODEREPO"
if [ "$RC" -eq 0 ]; then
  pass "the documented VAULT_VALIDATOR_BYPASS=1 escape hatch still works"
else
  fail "VAULT_VALIDATOR_BYPASS=1 did not bypass the block (rc=$RC)"
fi

# --------------------------------------------------------------------------
# 3. the block names its target
#    A guard that blocks without saying WHAT it matched is one an agent
#    retries blind. It is also how case 1 stayed invisible for so long.
# --------------------------------------------------------------------------
run_rm "rm -rf \"$VAULT\"" "$CODEREPO"
if grep -q "Brain" "$ERR" && grep -q "vault root" "$ERR"; then
  pass "the block message names the resolved target and why it matched"
else
  fail "the block message does not name the resolved target"
  sed 's/^/  /' "$ERR" >&2
fi

# --------------------------------------------------------------------------
# 4. anti-regression: no shell $VAR left unescaped inside a regex literal
#    This is the defect class itself, not an instance of it. `$HOME/vault`
#    LOOKS like a path in a code review and is an end-of-line anchor followed
#    by dead literal text -- it type-checks, lints, compiles, and never fires.
# --------------------------------------------------------------------------
dollar_hits="$(python3 - "$HOOK" <<'PY'
import ast, re, sys
from pathlib import Path

BACKSLASH = chr(92)
src = Path(sys.argv[1]).read_text(encoding="utf-8")
var = re.compile(r"\$[A-Z_][A-Z0-9_]+")
bad = []
for node in ast.walk(ast.parse(src)):
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        continue
    seg = ast.get_source_segment(src, node) or ""
    m = re.match(r'^[a-zA-Z]{0,2}["\']', seg)
    if not m or "r" not in m.group(0).lower()[:-1]:
        continue  # not a raw literal
    for hit in var.finditer(node.value):
        if hit.start() and node.value[hit.start() - 1] == BACKSLASH:
            continue  # \$VAR -- a correct literal match
        bad.append(f"line {node.lineno}: {hit.group(0)}")
print("; ".join(bad))
PY
)"
if [ -z "$dollar_hits" ]; then
  pass "no unescaped shell \$VAR survives inside a regex literal in this hook"
else
  fail "a shell \$VAR is sitting unescaped in a regex literal (\$ is an anchor; the branch is dead): $dollar_hits"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "All assertions passed. rm -rf is blocked by resolved TARGET, not by folder name."
  exit 0
fi
echo "$FAILURES assertion(s) FAILED." >&2
exit 1
