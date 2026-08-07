#!/usr/bin/env bash
# Dogfoods the REAL shipped hooks.json through the installer's --fail-on-missing
# verifier on a faithfully-reconstructed HEALTHY vault install (MYC-2558 follow-up).
#
# Why this exists on top of test_verify_fallback_chain_optional.sh: that test
# proves the classification LOGIC on a synthetic one-command hooks.json. It would
# NOT catch a false-positive introduced by some OTHER hook's command shape — the
# exact blind spot behind MYC-2558, where a healthy macOS/Linux vault install
# nagged "the hook re-install didn't finish cleanly" every ~6-day cycle. This
# test asserts the property against the ACTUAL artifact clients install, so any
# future hooks.json change that reintroduces the recurring-warning class fails CI.
#
# Faithful healthy-install state:
#   - ~/.claude/skills/ai-brain-starter/... home copies present (every install has these)
#   - vault-content hooks under <vault>/⚙️ Meta/scripts/... present (phase-05 wires them)
#   - the skill repo is NOT cloned into <vault>/.claude/skills/ai-brain-starter/, so the
#     vault-side copy of the ||-fallback hook is ABSENT — only the home copy backs it.
#
# Asserts:
#   (a) that healthy install -> `--fail-on-missing` exits 0.
#   (b) NEGATIVE CONTROL: delete one genuinely-required home hook -> exits 1.
#
# The installer's OWN path extractor enumerates what to create, so the test can
# never drift from however the installer parses commands. Self-contained; exit 0 = pass.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export REPO_ROOT

python3 <<'PY'
import importlib.util, os, tempfile, json, subprocess, sys
from pathlib import Path

repo = os.environ["REPO_ROOT"]
installer = repo + "/scripts/install-hooks-user-level.py"
spec = importlib.util.spec_from_file_location("ih", installer)
ih = importlib.util.module_from_spec(spec); spec.loader.exec_module(ih)

def fail(msg):
    print("FAIL:", msg); sys.exit(1)

TMP = Path(tempfile.mkdtemp()).resolve()
HOME = TMP / "home";      HOME.mkdir()
VAULT = TMP / "FakeVault"; VAULT.mkdir()
# Both vars, always: POSIX expanduser reads HOME, Windows (ntpath) reads
# USERPROFILE and ignores HOME entirely. Setting only HOME let this test install
# hook scripts into the developer's real ~/.claude/skills (MYC-3536).
env = dict(os.environ, HOME=str(HOME), USERPROFILE=str(HOME))
os.environ["HOME"] = str(HOME)  # module-level expanduser during enumeration
os.environ["USERPROFILE"] = str(HOME)

tmpl = json.load(open(repo + "/hooks.json"))
norm = ih.normalize_path_substitutions(tmpl, str(VAULT))
# Mirror the installer pipeline: [PYTHON] resolves to an absolute interpreter
# BEFORE merge/verify. Enumeration must run over the same substituted commands
# the real installer verifies, else the trailing-`python3` path extraction (and
# thus the stub set) diverges and a required path shows up spuriously missing.
norm = ih.substitute_python_interpreter(norm)
merged, _ = ih.merge_hooks({}, norm)

# Empty sandbox -> the verifier reports every referenced path as missing, which
# is exactly the enumeration of what a real install must have on disk.
req, opt = ih.verify_paths_on_disk(merged)
all_paths = [p for _, p, _ in req] + [p for _, p, _ in opt]
if not all_paths:
    fail("no script paths extracted from the real hooks.json — enumeration broke")

vres = str(VAULT.resolve())
def is_skill_in_vault(p):
    # Realistic: skill repo NOT cloned into the vault, so its vault-side copy is absent.
    return p.startswith(vres) and "/.claude/skills/ai-brain-starter/" in p

left_absent = []
for p in all_paths:
    if is_skill_in_vault(p):
        left_absent.append(p); continue
    fp = Path(p); fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("#!/usr/bin/env python3\nprint('{}')\n")

if not left_absent:
    fail("expected >=1 skill-in-vault ||-fallback copy to leave absent; found none "
         "(did the fallback hook change shape?)")

settings = TMP / "settings.json"
def run_installer():
    settings.write_text("{}")
    return subprocess.run(
        [sys.executable, installer, "--hooks-source", repo + "/hooks.json",
         "--vault-path", str(VAULT), "--settings", str(settings),
         "--fail-on-missing", "--quiet"],
        capture_output=True, text=True, env=env)

# (a) healthy install -> rc 0
r = run_installer()
if r.returncode != 0:
    fail("healthy vault install of the REAL hooks.json exited %d (want 0).\n"
         "STDOUT:\n%s\nSTDERR:\n%s" % (r.returncode, r.stdout[-1500:], r.stderr[-1500:]))

# (b) NEGATIVE CONTROL: a genuinely-missing required hook must still fail the gate.
victim = Path(os.path.expanduser("~/.claude/skills/ai-brain-starter/hooks/log-skill-usage.py"))
if not victim.is_file():
    fail("expected required home hook log-skill-usage.py present before neg-control delete "
         "(hook removed from template? update this test)")
victim.unlink()
r2 = run_installer()
if r2.returncode != 1:
    fail("NEGATIVE CONTROL: a genuinely-missing required hook should exit 1, got %d.\n"
         "STDOUT:\n%s" % (r2.returncode, r2.stdout[-1200:]))

print("PASS: REAL hooks.json healthy vault install -> rc 0 (%d ||-fallback vault copy(ies) "
      "left absent); neg control (missing required hook) -> rc 1" % len(left_absent))
PY
