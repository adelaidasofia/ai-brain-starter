#!/usr/bin/env bash
# Test: templates/launchd/*.plist.template -- EnvironmentVariables/PATH key.
#
# Bug class: launchd hands a job a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin)
# with no Homebrew prefix. A client script that shells out to a brew-installed
# tool (gh, uv, node, a brew git) fails with "not found" under launchd even
# though it works in every interactive shell -- the exact failure took a daily
# job down here, invisibly, for days, because nothing distinguished "job
# crashed" from "job's PATH doesn't have what it needs" until someone read its
# own stderr log.
#
# The fix adds an explicit EnvironmentVariables/PATH key to all three shipped
# templates, prefixing the Homebrew locations (Apple Silicon, Intel,
# Linuxbrew) ahead of the launchd-default system directories.
#
# Validation deliberately does NOT regex-eyeball the XML text -- a regex can
# match a key that is commented out, malformed, or sitting in the wrong dict.
# Two independent parsers are used instead:
#   - plutil -lint: Apple's own tool (macOS only), the closest thing to "does
#     launchd itself accept this file". Skipped, not failed, off macOS.
#   - Python's plistlib: portable, so this control has REAL teeth on the
#     canonical ubuntu-latest CI gate, not just on a developer's Mac. Each
#     template's leading documentation comment is stripped before parsing:
#     two of the three templates' PRE-EXISTING header prose contain a literal
#     "--" inside a CLI-flag example (e.g. "--apply"), which XML forbids
#     inside comment text -- plutil tolerates it (confirmed: `plutil -lint`
#     reports OK on all three, unmodified), Python's strict expat parser does
#     not. Comments carry zero plist DATA, so stripping them before parsing
#     changes nothing about what a rendered, installed copy actually
#     contains -- this is normalization for the test, not a workaround for
#     anything the fix itself touches.
#
# Per template, two assertions:
#   1. plutil -lint accepts it (macOS only; SKIP elsewhere, noted above)
#   2. one Python check covering all of:
#      (a) plistlib parses the raw template (comments stripped)
#      (b) EnvironmentVariables/PATH exists and matches the required value
#          exactly -- Homebrew locations ahead of the launchd-default system
#          directories, in the specified order
#      (c) the template's own placeholder tokens ({{REPO_ROOT}} etc.) are
#          still present verbatim -- the fix must not disturb the installers'
#          sed-substitution contract
#      (d) a REAL sed substitution -- replicating byte-for-byte what
#          scripts/install-*.sh actually runs -- still parses afterward, with
#          the PATH key intact and zero leftover {{...}} tokens. This proves
#          the fix survives the actual installer rendering path, not just the
#          raw template.
#
# Static file validation only -- no real launchctl load/bootstrap anywhere in
# this file. Self-contained. Exit 0 = pass, exit 1 = fail.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPLATES_DIR="$REPO_ROOT/templates/launchd"

FAILURES=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1" >&2; FAILURES=$((FAILURES + 1)); }

EXPECTED_PATH="/opt/homebrew/bin:/usr/local/bin:/home/linuxbrew/.linuxbrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# name -> space-separated placeholder tokens that template uses (matches each
# scripts/install-*.sh's own `sed -e "s|{{X}}|...|g"` invocations verbatim).
TEMPLATE_NAMES=(
  "com.abs.closed-loop-daemon.plist.template"
  "com.abs.dev-hub-refresh.plist.template"
  "com.abs.vault-daily-maintenance.plist.template"
)
TEMPLATE_PLACEHOLDERS=(
  "REPO_ROOT VAULT_ROOT LOG_DIR"
  "REPO_ROOT LOG_DIR"
  "REPO_ROOT VAULT_ROOT LOG_DIR"
)

i=0
while [ "$i" -lt "${#TEMPLATE_NAMES[@]}" ]; do
  name="${TEMPLATE_NAMES[$i]}"
  placeholders="${TEMPLATE_PLACEHOLDERS[$i]}"
  path="$TEMPLATES_DIR/$name"
  i=$((i + 1))

  if [ ! -f "$path" ]; then
    fail "$name: template file not found at $path"
    continue
  fi

  # --- assertion 1: plutil -lint (macOS only) ------------------------------
  if command -v plutil >/dev/null 2>&1; then
    if plutil -lint "$path" >/dev/null 2>&1; then
      pass "$name: plutil -lint accepts the template"
    else
      fail "$name: plutil -lint rejected the template"
      plutil -lint "$path" >&2 || true
    fi
  else
    echo "SKIP: $name: plutil not available on this platform (non-macOS) -- assertion 2 (plistlib) still gives real coverage"
  fi

  # --- assertion 2: parse + PATH content + placeholders + real-render ------
  if python3 - "$path" "$placeholders" "$EXPECTED_PATH" <<'PY'
import plistlib
import re
import sys
from pathlib import Path

template_path = Path(sys.argv[1])
placeholders = sys.argv[2].split()
expected_path = sys.argv[3]

COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
raw = template_path.read_text(encoding="utf-8")
errors = []

# (a) + (b): raw template parses (comments stripped) and PATH is correct.
try:
    data = plistlib.loads(COMMENT_RE.sub("", raw).encode("utf-8"))
except Exception as exc:
    print(f"FAIL: {template_path.name}: does not parse as plist (comments stripped): {exc}", file=sys.stderr)
    sys.exit(1)

env = data.get("EnvironmentVariables")
if not isinstance(env, dict) or "PATH" not in env:
    errors.append("no EnvironmentVariables/PATH key in the parsed plist")
elif env["PATH"] != expected_path:
    errors.append(f"PATH = {env['PATH']!r}, expected {expected_path!r}")

# (c): every placeholder this template is supposed to carry is still there,
# verbatim, in the RAW (unstripped) text -- the installers' sed step depends
# on these tokens surviving byte-for-byte.
for ph in placeholders:
    token = "{{%s}}" % ph
    if token not in raw:
        errors.append(f"placeholder {token} missing from the raw template")

# (d): replicate the installers' OWN substitution exactly (a literal global
# string replace -- sed's `s|{{X}}|...|g` treats these tokens literally, no
# BRE metacharacters in play), then re-parse. Proves the fix survives the
# real rendering path, not just the pristine template.
FAKE_VALUES = {
    "REPO_ROOT": "/Users/tester/ai-brain-starter",
    "VAULT_ROOT": "/Users/tester/MyVault",
    "LOG_DIR": "/Users/tester/.local/state/ai-brain-starter",
}
rendered = raw
for ph in placeholders:
    rendered = rendered.replace("{{%s}}" % ph, FAKE_VALUES[ph])

if "{{" in rendered:
    errors.append("a {{...}} token survives after simulated sed substitution")

try:
    rendered_data = plistlib.loads(COMMENT_RE.sub("", rendered).encode("utf-8"))
except Exception as exc:
    errors.append(f"rendered (post-substitution) file does not parse as plist: {exc}")
else:
    r_env = rendered_data.get("EnvironmentVariables")
    if not isinstance(r_env, dict) or r_env.get("PATH") != expected_path:
        errors.append("PATH key did not survive simulated sed substitution intact")
    # And the substituted values actually landed where they should have.
    args = rendered_data.get("ProgramArguments") or []
    joined = " ".join(str(a) for a in args) + " " + str(rendered_data.get("StandardOutPath", ""))
    for ph, val in FAKE_VALUES.items():
        if ph in placeholders and val not in joined and val not in str(rendered_data):
            errors.append(f"substituted {ph} value never appears anywhere in the rendered plist")

if errors:
    for e in errors:
        print(f"FAIL: {template_path.name}: {e}", file=sys.stderr)
    sys.exit(1)
print(f"{template_path.name}: parses, PATH correct, placeholders intact, survives real substitution")
PY
  then
    pass "$name: parses (comments stripped) + PATH exact + placeholders survive real sed substitution"
  else
    fail "$name: one or more plist/PATH/placeholder/render checks failed -- see stderr above"
  fi
done

if [ "$FAILURES" -eq 0 ]; then
  echo "All assertions passed. Launchd template PATH env holds across all three templates."
  exit 0
else
  echo "$FAILURES assertion(s) failed." >&2
  exit 1
fi
