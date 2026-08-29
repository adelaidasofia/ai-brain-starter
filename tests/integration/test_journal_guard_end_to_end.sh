#!/usr/bin/env bash
#
# End-to-end control for warn-journal-saved-without-context.py.
#
# The unit test next door asserts on _vault_root / _resolve_root. That proves the
# resolver, not the guard. This drives the SHIPPED hook over real stdin against a
# real temp vault, so the two directions are proven on the artifact people run:
#
#   NEGATIVE CONTROL (the load-bearing one): marker ABSENT -> must DENY.
#     A guard earns trust only by failing on the thing it catches. A fix that
#     makes the block go away is indistinguishable from a fix that makes the
#     guard useless, unless this case is asserted to still fire.
#
#   POSITIVE CASE: marker PRESENT -> must ALLOW.
#     This is the 2026-08-28 regression: the relative-path write form
#     `cd "<vault>" && cat > "<emoji> Journals/<Month>/e.md"` resolved the root
#     to the vault's PARENT, found no marker there, and blocked a correct save.
#
# Blocking form is a JSON permissionDecision on exit 0 (see the hook's own note),
# so "denied" is read from the payload, never from the exit code.
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
PY="${PYTHON:-python3}"

"$PY" - "$REPO_ROOT" <<'PYEOF'
import json, os, subprocess, sys, tempfile

repo = sys.argv[1]
hook = os.path.join(repo, "hooks", "warn-journal-saved-without-context.py")
EMOJI = "\U0001F4D3"
DATE = "2026-08-28"
failures = 0

def check(label, got, want):
    global failures
    if got == want:
        print("PASS  %s" % label)
    else:
        print("FAIL  %s (got %r, want %r)" % (label, got, want))
        failures += 1

def run(tool_name, tool_input, cwd):
    """Returns True if the hook DENIED the write."""
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool_name,
               "tool_input": tool_input, "cwd": cwd}
    env = dict(os.environ)
    env.pop("JOURNAL_CONTEXT_BYPASS", None)   # the harness itself must not bypass
    p = subprocess.run([sys.executable, hook], input=json.dumps(payload),
                       capture_output=True, text=True, env=env)
    if p.returncode != 0:
        return "CRASH: rc=%d %s" % (p.returncode, p.stderr.strip()[:200])
    if not p.stdout.strip():
        return False
    try:
        out = json.loads(p.stdout)
    except ValueError:
        return "CRASH: non-JSON stdout %r" % p.stdout[:200]
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"

with tempfile.TemporaryDirectory() as tmp:
    tmp = os.path.realpath(tmp)
    vault = os.path.join(tmp, "vault")
    jdir = os.path.join(vault, EMOJI + " Journals", "August 2026")
    os.makedirs(jdir)
    entry_rel = "%s Journals/August 2026/An Entry.md" % EMOJI
    entry_abs = os.path.join(jdir, "An Entry.md")
    body = "---\ntype: journal\ncreationDate: %sT22:45\n---\n\n## Journal\nx\n" % DATE
    bash_rel = 'cd "%s" && cat > "%s" << \'EOF\'\n%sEOF' % (vault, entry_rel, body)
    bash_abs = 'cat > "%s" << \'EOF\'\n%sEOF' % (entry_abs, body)
    marker_dir = os.path.join(vault, "⚙️ Meta", ".journal-context")
    marker = os.path.join(marker_dir, "%s.json" % DATE)

    # === NEGATIVE CONTROL: no marker anywhere -> every write form must DENY ===
    check("NEGATIVE CONTROL: relative-path bash write is DENIED without a marker",
          run("Bash", {"command": bash_rel}, vault), True)
    check("NEGATIVE CONTROL: absolute-path bash write is DENIED without a marker",
          run("Bash", {"command": bash_abs}, vault), True)
    check("NEGATIVE CONTROL: Write tool is DENIED without a marker",
          run("Write", {"file_path": entry_abs, "content": body}, vault), True)

    # === The bug: a marker in the vault's PARENT must NOT satisfy the check. ===
    # This is the silent-false-clean direction of the same defect. Before the fix
    # the root resolved to the parent, so this planted marker would have ALLOWED
    # a journal that had no context pulled at all.
    parent_marker_dir = os.path.join(tmp, "⚙️ Meta", ".journal-context")
    os.makedirs(parent_marker_dir)
    open(os.path.join(parent_marker_dir, "%s.json" % DATE), "w").write("{}")
    check("NEGATIVE CONTROL: a marker in the vault PARENT does not satisfy the check",
          run("Bash", {"command": bash_rel}, vault), True)

    # Remove the planted parent marker before the positive section. Leaving it
    # in place makes the positive cases pass on UNFIXED source for the wrong
    # reason (the parent marker satisfies the wrongly-resolved root), so the
    # assertion could not vary with the defect and would prove nothing.
    os.remove(os.path.join(parent_marker_dir, "%s.json" % DATE))

    # === POSITIVE: the real marker, in the real vault -> ALLOW ===
    os.makedirs(marker_dir)
    open(marker, "w").write('{"date": "%s"}' % DATE)
    check("relative-path bash write is ALLOWED with the marker (the 2026-08-28 regression)",
          run("Bash", {"command": bash_rel}, vault), False)
    check("absolute-path bash write is ALLOWED with the marker",
          run("Bash", {"command": bash_abs}, vault), False)
    check("Write tool is ALLOWED with the marker",
          run("Write", {"file_path": entry_abs, "content": body}, vault), False)

    # === CONTROLS: the gate must not open on things that are not journal saves ===
    check("CONTROL: a non-journal note write is not gated",
          run("Write", {"file_path": os.path.join(vault, "Notes", "x.md"), "content": "x"}, vault),
          False)
    check("CONTROL: reading a journal is not gated",
          run("Bash", {"command": 'cat "%s"' % entry_abs}, vault), False)
    os.remove(marker)
    check("CONTROL: inline bypass still allows the write with no marker",
          run("Bash", {"command": "JOURNAL_CONTEXT_BYPASS=1 " + bash_rel}, vault), False)
    check("CONTROL: unknown cwd and no absolute path -> fail OPEN, never a wrong block",
          run("Bash", {"command": 'cat > "Journals/August 2026/e.md"'}, tmp), False)

    # === The heredoc-body gate (2026-08-28, second defect, same session) ======
    # A write whose PAYLOAD mentions a journal path is not a journal save. The
    # gate used to scan the whole command, so writing a test or a doc that quotes
    # a journal path was blocked (that is how this defect was found: a write to
    # tests/integration/*.sh was denied because the test's own PROSE contained a
    # journal path). A guard that fires on unrelated writes teaches the operator
    # to reach for the bypass by habit, which is how a real block later gets
    # waved through.
    #
    # The body must carry an ABSOLUTE journal path. With a relative one the OLD
    # code fails open at root resolution and the assertion passes for a reason
    # that has nothing to do with the gate -- a control that cannot vary with the
    # defect proves nothing. With the absolute path the old code resolves the
    # vault, finds no marker, and denies, so each case genuinely flips.
    doc_body = "see %s/%s Journals/August 2026/An Entry.md for the format\n" % (vault, EMOJI)
    target = os.path.join(tmp, "t.sh")
    HEREDOC_FORMS = [
        ("quoted delimiter",   "cat > %s << 'EOF'\n%sEOF" % (target, doc_body)),
        ("unquoted delimiter", "cat > %s << EOF\n%sEOF" % (target, doc_body)),
        ("dash-suppressed",    "cat > %s <<-EOF\n%sEOF" % (target, doc_body)),
        ("double-quoted",      'cat > %s << "EOF"\n%sEOF' % (target, doc_body)),
        ("custom delimiter",   "cat > %s << 'TESTEOF'\n%sTESTEOF" % (target, doc_body)),
    ]
    for label, cmd in HEREDOC_FORMS:
        check("CONTROL: non-journal write quoting a journal path in a %s heredoc is not gated"
              % label, run("Bash", {"command": cmd}, vault), False)

    check("POSITIVE CONTROL: a real heredoc journal save is still gated (denied, no marker)",
          run("Bash", {"command": bash_rel}, vault), True)

print()
if failures:
    print("FAILED (%d)" % failures)
    sys.exit(1)
print("ALL PASS (17 assertions; 5 negative controls)")
PYEOF
