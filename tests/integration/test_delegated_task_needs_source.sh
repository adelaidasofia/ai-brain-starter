#!/usr/bin/env bash
# Reproducible-activation + firing-contract gate for the
# warn-delegated-task-needs-source hookify rule template. Two halves, each with
# negative controls, mirroring test_cloud_safe_file_walkers.sh:
#
#   A. FIRING CONTRACT - extract the rule's two regex conditions from the shipped
#      template and run them the way the hookify engine does: re.IGNORECASE +
#      regex.search, all conditions ANDed (see the plugin's compile_regex +
#      "all conditions must match"). Proves it fires on a to-do / task /
#      delegation / backlog file with an [owner:: X] line and no [[link]]/URL;
#      stays silent when a wikilink or URL is on the line, on a non-task
#      filename, on an unowned line, and on a checked box; and is name-agnostic
#      (accented + CJK owner names fire). The IGNORECASE mirror is load-bearing:
#      the real file is "Team To-dos.md" (capital T) and the pattern is lowercase
#      to-?dos?, so a case-sensitive test would wrongly read the rule as dead.
#
#   B. ACTIVATION - a rule is only useful if it reaches a fresh machine. Proves
#      activation.json classifies every template exactly once (the one
#      declaration for the governed set), the rule is in the `default`
#      (auto-activate) list, and the REAL installer copies the default rule (and
#      NOT an opt-in one) into a throwaway ~/.claude, preserves a user's
#      customized copy (copy-if-absent), and fails loud under --fail-on-missing
#      when the manifest names a template that is not on disk.
#
# Stdlib python3 + bash only (no PyYAML, no network, no git). Tmpdir on exit.
# Run: bash tests/integration/test_delegated_task_needs_source.sh  (0 = pass)
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
RULE="$REPO_ROOT/templates/hookify-rules/hookify.warn-delegated-task-needs-source.local.md"
INSTALLER="$REPO_ROOT/scripts/install-hooks-user-level.py"
HOOKS_JSON="$REPO_ROOT/hooks.json"
DEFAULT_RULE="hookify.warn-delegated-task-needs-source.local.md"
OPT_IN_RULE="hookify.fact-check-template.local.md"

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL  $1 :: ${2:-}"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ---- A. FIRING CONTRACT ------------------------------------------------------
echo "=== A. firing contract (regex mirrors hookify: IGNORECASE + search, ANDed) ==="

# A0: rule file contract. Beyond name/event/action + the two condition fields,
# assert the rule is ENABLED and EVERY operator is regex_match. Both guard a
# silent-no-op the regex cases below would miss: a rule shipped `enabled: false`
# never loads, and an operator swapped to `contains`/`equals` changes firing from
# regex to substring while the extracted-pattern test stays a false green.
total_ops=$(grep -cE '^[[:space:]]*operator:' "$RULE")
regex_ops=$(grep -cE '^[[:space:]]*operator: regex_match$' "$RULE")
if [ -f "$RULE" ] \
   && grep -q '^name: warn-delegated-task-needs-source$' "$RULE" \
   && grep -q '^event: file$' "$RULE" \
   && grep -q '^action: warn$' "$RULE" \
   && grep -q '^enabled: true$' "$RULE" \
   && grep -q 'field: file_path' "$RULE" \
   && grep -q 'field: content' "$RULE" \
   && [ "$total_ops" -ge 2 ] && [ "$total_ops" -eq "$regex_ops" ]; then
  ok "rule file contract (name/event/action/enabled + file_path & content, all regex_match)"
else
  bad "rule file contract" "name/event/action/enabled, a condition field, or a non-regex_match operator (ops=$total_ops regex=$regex_ops)"
fi

# A1: positive + negative firing cases, driven off the SHIPPED patterns. Extract
# both single-quoted YAML patterns with stdlib only (associate each pattern with
# the most recent `field:`), then run the engine's IGNORECASE + AND semantics.
if python3 - "$RULE" <<'PY'
import re, sys
from pathlib import Path

lines = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
pats, field = {}, None
for ln in lines:
    m = re.match(r"\s*-?\s*field:\s*(\S+)", ln)
    if m:
        field = m.group(1); continue
    m = re.match(r"\s*pattern:\s*'(.*)'\s*$", ln)
    if m and field:
        pats[field] = m.group(1).replace("''", "'"); field = None
assert "file_path" in pats and "content" in pats, f"extracted only {sorted(pats)}"

# hookify compile_regex() uses re.IGNORECASE; a rule matches only when ALL of its
# conditions do (logical AND).
file_re = re.compile(pats["file_path"], re.IGNORECASE)
content_re = re.compile(pats["content"], re.IGNORECASE)
def fires(path, content):
    return bool(file_re.search(path)) and bool(content_re.search(content))

# Non-ASCII owner names are injected from escapes so this file stays pure ASCII
# while still proving genuinely accented + CJK names fire (the name-agnostic claim).
NAMES = {"<ACCENT>": "Jos\u00e9 Garc\u00eda", "<UMLAUT>": "Zo\u00eb M\u00fcller", "<CJK>": "\u7530\u4e2d"}
def sub(s):
    for k, v in NAMES.items():
        s = s.replace(k, v)
    return s

cases = [
  # (path, content, expect_fire, label)
  ("Team To-dos.md",     "- [ ] Call the vendor [owner:: Sam] [area:: ops]",            True,  "todo file + owner, no link -> FIRE"),
  ("Sprint tasks.md",    "- [ ] Draft the brief [owner:: Alex]",                        True,  "tasks file -> FIRE"),
  ("delegation.md",      "- [ ] Ship the deck [owner:: Robin]",                         True,  "delegation file -> FIRE"),
  ("Product backlog.md", "- [ ] Wire the API [owner:: Jordan]",                         True,  "backlog file -> FIRE"),
  ("todos.md",           "- [ ] Revisar el documento [owner:: <ACCENT>]",              True,  "accented owner -> FIRE (name-agnostic)"),
  ("tasks.md",           "- [ ] Review [owner:: <UMLAUT>]",                             True,  "diaeresis owner -> FIRE (name-agnostic)"),
  ("tasks.md",           "- [ ] Review [owner:: <CJK>]",                                True,  "CJK owner -> FIRE (name-agnostic)"),
  ("Team To-dos.md",     "- [ ] Call vendor [owner:: Sam] see [[Vendor Brief]]",        False, "owner + wikilink -> SILENT"),
  ("backlog.md",         "- [ ] Ship copy [owner:: Casey] https://example.com/brief",  False, "owner + https URL -> SILENT"),
  ("delegation.md",      "- [ ] Ship copy [owner:: Casey] http://example.com/b",       False, "owner + http URL -> SILENT"),
  ("Strategy Notes.md",  "- [ ] Do the thing [owner:: Sam]",                            False, "non-task filename -> SILENT (filename gate)"),
  ("Team To-dos.md",     "- [ ] Call vendor [area:: ops]",                              False, "no owner -> SILENT"),
  ("Team To-dos.md",     "- [x] Done [owner:: Sam]",                                    False, "checked box -> SILENT (open tasks only)"),
  ("Team To-dos.md",     "- [ ] Task A [owner:: Sam]\n- [ ] Task B [owner:: Alex] [[Brief]]",  True,  "per-line: an unlinked line fires even when a sibling has a link"),
  ("Team To-dos.md",     "- [ ] Task A [owner:: Sam] [[BriefA]]\n- [ ] Task B [owner:: Alex] https://ex.co", False, "every owner line linked -> SILENT"),
]
mismatches = [lbl for (p, c, exp, lbl) in cases if fires(p, sub(c)) is not exp]
if mismatches:
    print("MISMATCH: " + " | ".join(mismatches), file=sys.stderr)
    sys.exit(1)
print(f"{len(cases)} firing cases matched the shipped patterns")
PY
then ok "firing cases match shipped patterns (positive + negative controls)"
else bad "firing cases" "a case disagreed with the shipped regex (see MISMATCH above)"; fi

# ---- B. ACTIVATION -----------------------------------------------------------
echo "=== B. activation (manifest governs the set; the REAL installer copies default) ==="

# B1: manifest classifies every template exactly once, no dangling entry, no
# duplicate, and the rule is in the default (auto-activate) list.
if python3 - "$REPO_ROOT" <<'PY'
import json, sys
from pathlib import Path

tdir = Path(sys.argv[1]) / "templates" / "hookify-rules"
manifest = json.loads((tdir / "activation.json").read_text(encoding="utf-8"))
default = list(manifest.get("default", []))
opt_in = list(manifest.get("opt_in", []))
on_disk = {p.name for p in tdir.glob("hookify.*.local.md")}
classified = default + opt_in
declared = set(classified)

problems = []
dup = {n for n in declared if classified.count(n) > 1}
if dup:
    problems.append(f"listed more than once: {sorted(dup)}")
dangling = declared - on_disk
if dangling:
    problems.append(f"manifest names non-existent template(s): {sorted(dangling)}")
unclassified = on_disk - declared
if unclassified:
    problems.append(f"template(s) on disk not classified in the manifest: {sorted(unclassified)}")
if "hookify.warn-delegated-task-needs-source.local.md" not in default:
    problems.append("warn-delegated-task-needs-source is not in the default (auto-activate) list")
if problems:
    print(" ; ".join(problems), file=sys.stderr)
    sys.exit(1)
print(f"manifest clean: {len(default)} default + {len(opt_in)} opt_in = {len(on_disk)} templates, partitioned")
PY
then ok "manifest classifies every template exactly once; rule is a default"
else bad "manifest completeness" "see stderr above"; fi

# B2: the REAL installer copies the default rule into a throwaway ~/.claude and
# leaves the opt-in templates alone.
H1="$TMP/home1"; mkdir -p "$H1/.claude"
run_sandboxed "$H1" python3 "$INSTALLER" --hooks-source "$HOOKS_JSON" \
  --settings "$H1/.claude/settings.json" --quiet >/dev/null 2>&1
if [ -f "$H1/.claude/$DEFAULT_RULE" ]; then
  ok "installer activated the default rule into ~/.claude"
else
  bad "default rule not activated" "$H1/.claude/$DEFAULT_RULE absent after install"
fi
if [ -f "$H1/.claude/$OPT_IN_RULE" ]; then
  bad "opt-in rule wrongly activated" "$OPT_IN_RULE was copied but is opt-in"
else
  ok "installer left the opt-in rule (fact-check-template) alone"
fi

# B3: copy-if-absent - a user's customized copy is never overwritten on re-install.
H2="$TMP/home2"; mkdir -p "$H2/.claude"
printf '%s\n' "CUSTOMIZED BY USER - do not clobber" > "$H2/.claude/$DEFAULT_RULE"
run_sandboxed "$H2" python3 "$INSTALLER" --hooks-source "$HOOKS_JSON" \
  --settings "$H2/.claude/settings.json" --quiet >/dev/null 2>&1
if grep -q "CUSTOMIZED BY USER" "$H2/.claude/$DEFAULT_RULE"; then
  ok "copy-if-absent: a user's customized rule survives re-install"
else
  bad "copy-if-absent" "the user's customized rule was overwritten"
fi

# B4: idempotent - a second install into the same home exits 0 and keeps one copy.
run_sandboxed "$H1" python3 "$INSTALLER" --hooks-source "$HOOKS_JSON" \
  --settings "$H1/.claude/settings.json" --quiet >/dev/null 2>&1
rc_second=$?
count_default=$(find "$H1/.claude" -maxdepth 1 -name "$DEFAULT_RULE" | wc -l | tr -d ' ')
if [ "$rc_second" -eq 0 ] && [ "$count_default" = "1" ]; then
  ok "idempotent: second install exits 0 and keeps exactly one copy"
else
  bad "idempotent" "rc=$rc_second, copies=$count_default"
fi

# B5: NEGATIVE CONTROL - a guard earns trust only by failing on the thing it
# catches. Point the installer at a manifest whose default names a missing
# template and pass --fail-on-missing: it must exit nonzero AND say why.
FAKE="$TMP/fakerepo"; mkdir -p "$FAKE/templates/hookify-rules"
cp "$HOOKS_JSON" "$FAKE/hooks.json"
cat > "$FAKE/templates/hookify-rules/activation.json" <<'JSON'
{ "default": ["hookify.does-not-exist.local.md"], "opt_in": [] }
JSON
H3="$TMP/home3"; mkdir -p "$H3/.claude"
ERR3="$TMP/err3.log"
run_sandboxed "$H3" python3 "$INSTALLER" --hooks-source "$FAKE/hooks.json" \
  --settings "$H3/.claude/settings.json" --fail-on-missing --quiet >/dev/null 2>"$ERR3"
rc_missing=$?
if [ "$rc_missing" -ne 0 ] && grep -qi "activation manifest names default template" "$ERR3"; then
  ok "fail-loud: --fail-on-missing exits nonzero and names the missing default template"
else
  bad "fail-loud negative control" "rc=$rc_missing; stderr: $(cat "$ERR3")"
fi

echo
echo "=== summary: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
