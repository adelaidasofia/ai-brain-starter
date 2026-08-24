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

# Like expect_block, but for rules whose severity is `nudge` rather than
# `block`. Both exit 2; only the tag on the message differs (NUDGE vs BLOCKED),
# so asserting on "BLOCKED" would fail a nudge rule that fired perfectly well.
# The tag is still asserted -- just the right one -- so this cannot degrade into
# "exit 2 for any reason at all".
expect_fire() {  # <label> <command> <cwd>
  run_rm "$2" "$3"
  if [ "$RC" -eq 2 ] && grep -qE "BLOCKED|NUDGE" "$ERR"; then
    pass "$1"
  else
    fail "$1 -- expected the rule to fire, got rc=$RC"
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
# 1c. a vault reached through a SYMLINKED ancestor is still a vault
#
#     vault_root_for() hands back a RESOLVED root, while cwd and the command's
#     path tokens arrive spelled exactly as written. _is_under compared the two
#     LEXICALLY only, so one symlink anywhere above the vault made it weigh
#     /var/... against /private/var/... and answer False. Every namespace-scoped
#     rule (grep, find) then silently un-scoped itself: no match, no message,
#     exit 0 -- inert in the one place it is supposed to speak.
#
#     That is not a corner case on a Mac: /var -> /private/var is where macOS
#     puts TMPDIR, so the whole class was live on every Mac while Linux CI
#     stayed green. The sibling helper _same_dir already carried the lexical-OR-
#     realpath pair, with macOS named in its docstring; _is_under was the half
#     of that fix that never landed, which is why `rm -rf` fired here and the
#     nudges did not.
#
#     Asserted through an EXPLICIT symlink, not by leaning on whatever the
#     platform's tmpdir happens to be, so it is proven on Linux and macOS
#     alike. Skipped only where the OS refuses to make one (unprivileged
#     Windows), which is reported rather than passed over in silence.
# --------------------------------------------------------------------------
if ln -s "$VAULT" "$TMP/link-to-brain" 2>/dev/null && [ -d "$TMP/link-to-brain" ]; then
  expect_fire "a namespace rule fires from a cwd reached via a symlink to the vault" \
    'grep foo note.md' "$TMP/link-to-brain"
  expect_fire "a namespace rule fires on a symlinked vault path named in the command" \
    "find \"$TMP/link-to-brain\" -name '*.md'" "$CODEREPO"
else
  echo "SKIP: symlinks unavailable on this host, so the symlinked-vault case is unproven here" >&2
fi

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
# The scanner goes to a FILE, then runs -- rather than being piped through a
# heredoc nested inside "$(...)". bash 3.2 (macOS's /bin/bash, and the only bash
# on a stock Mac) parses a nested heredoc BODY as shell text while hunting for
# the closing paren, ignoring the <<'PY' quoting that makes the body literal
# everywhere else. The `["\']` class below carries an ODD number of ' chars, so
# that scan opened a quote it never closed and took down the WHOLE FILE with
# "unexpected EOF while looking for matching `)'" -- every assertion in it, not
# just this one. bash 4+ treats the body as opaque and never saw it, so Linux CI
# stayed green while the suite was unrunnable on any unmodified Mac.
# Keep this heredoc unnested; do not inline it back into a substitution.
SCAN_PY="$TMP/dollar_scan.py"
cat > "$SCAN_PY" <<'PY'
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
dollar_hits="$(python3 "$SCAN_PY" "$HOOK")"
if [ -z "$dollar_hits" ]; then
  pass "no unescaped shell \$VAR survives inside a regex literal in this hook"
else
  fail "a shell \$VAR is sitting unescaped in a regex literal (\$ is an anchor; the branch is dead): $dollar_hits"
fi

# --------------------------------------------------------------------------
# 5. EVERY rule in the table has a positive control
#
#     This is the generalised lesson, and it is the reason the rm -rf defect
#     survived as long as it did. The rule was in RULES, it was wired, it was
#     documented in the module docstring, and it read fine. Nothing anywhere
#     asserted that it could actually FIRE, so "enforced" and "inert" were
#     indistinguishable from the outside -- by inspection, by lint, and by CI.
#
#     Auditing this file's five rules while fixing rm -rf found that `git
#     status` had NO positive control either (it does fire -- checked -- but
#     nobody had ever proven it). One untested blocking rule is how you get
#     the next silent no-op.
#
#     So: a sample per rule, run inside a real vault, each asserted to block.
#     The COUNT is asserted too, which is the part that keeps this honest --
#     add a sixth rule and this test fails until it gets a control of its own.
# --------------------------------------------------------------------------
git -C "$VAULT" init -q 2>/dev/null
git -C "$VAULT" config user.email t@t
git -C "$VAULT" config user.name t
echo seed > "$VAULT/seed.txt"
git -C "$VAULT" add seed.txt >/dev/null 2>&1
git -C "$VAULT" -c commit.gpgsign=false commit -qm init >/dev/null 2>&1

rule_count="$(python3 -c '
import importlib.util, sys
spec = importlib.util.spec_from_file_location("h", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(len(m.RULES))' "$HOOK")"

# One positive control per rule, in table order.
RULE_SAMPLES=(
  "git push"
  "git status"
  "rm -rf \"$VAULT\""
  "grep foo note.md"
  "find . -name '*.md'"
)

if [ "$rule_count" != "${#RULE_SAMPLES[@]}" ]; then
  fail "RULES has $rule_count entries but this test carries ${#RULE_SAMPLES[@]} positive control(s) -- a rule with no proof that it fires is how the rm -rf defect stayed invisible. Add a sample."
else
  pass "every one of the $rule_count rules has a positive control"
fi

fired_all=1
for sample in "${RULE_SAMPLES[@]}"; do
  run_rm "$sample" "$VAULT"
  if [ "$RC" -ne 2 ]; then
    fail "rule sample does not fire inside a vault: $sample (rc=$RC)"
    fired_all=0
  fi
done
if [ "$fired_all" -eq 1 ]; then
  pass "all ${#RULE_SAMPLES[@]} rules actually fire on a live vault (none is silently inert)"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "All assertions passed. rm -rf is blocked by resolved TARGET, not by folder name."
  exit 0
fi
echo "$FAILURES assertion(s) FAILED." >&2
exit 1
