#!/usr/bin/env bash
# test_dry_run_purity.sh — a dry run must NEVER mutate the machine.
#
# WHY: a real user ran `bash bootstrap.sh --dry-run` as a "safe preview" and
# Homebrew, Node, gh, pipx, and graphify were ACTUALLY INSTALLED — the
# Homebrew branch even documented it ("or dry-run: install for real"). Their
# assistant then (correctly) warned them not to trust --dry-run at all. This
# gate makes that class of regression impossible to reintroduce silently.
#
# Two layers:
#   T1  BEHAVIORAL: run the real bootstrap.sh --dry-run in a hermetic sandbox
#       (fake HOME + recording stubs for every mutating command). Any stub
#       invocation of a mutating subcommand lands in a violations file. The
#       run must exit 0 with ZERO violations and print [dry-run] preview lines.
#   T2  STRUCTURAL: the behavioral layer can't reach install branches (the
#       stubs make every tool look "already installed"), so separately assert
#       each known install command in bootstrap.sh sits inside a section that
#       checks DRY_RUN first.
#
# Run: bash tests/integration/test_dry_run_purity.sh  (0 = pass, 1 = fail)
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# HOME alone does not sandbox ~ on Windows — see lib/sandbox_home.sh.
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
BOOTSTRAP="$REPO_ROOT/bootstrap.sh"
[ -f "$BOOTSTRAP" ] || { echo "ERROR: $BOOTSTRAP not found" >&2; exit 1; }

PASS=0; FAIL=0
ok(){ printf '  PASS: %s\n' "$1"; PASS=$((PASS+1)); }
no(){ printf '  FAIL: %s\n' "$1"; FAIL=$((FAIL+1)); }
TMPROOT="$(mktemp -d)"; trap 'rm -rf "$TMPROOT"' EXIT

# ---- T1. behavioral: hermetic --dry-run leaves zero mutation attempts --------
FAKEHOME="$TMPROOT/home"; STUB="$TMPROOT/stub"; VIOLATIONS="$TMPROOT/violations.log"
mkdir -p "$FAKEHOME/.claude" "$STUB"
: > "$VIOLATIONS"

# Recording stub: mutating invocations append to the violations file; benign
# read-only invocations (version probes, list queries) exit 0 quietly.
make_stub() {  # $1=name  $2=egrep pattern of MUTATING first-args ("." = all)
  cat > "$STUB/$1" <<STUBEOF
#!/usr/bin/env bash
if printf '%s' "\$*" | grep -qE '$2'; then
  echo "$1 \$*" >> "$VIOLATIONS"
fi
exit 0
STUBEOF
  chmod +x "$STUB/$1"
}

make_stub brew      '^(install|tap|upgrade|uninstall)'
make_stub npm       '^(install|uninstall|update)'
make_stub pipx      '^(install|uninstall|upgrade|ensurepath)'
make_stub pip3      '^(install|uninstall)'
make_stub sudo      '.'
make_stub snap      '^install'
make_stub flatpak   '^install'
make_stub gh        '^(auth login|repo|issue create)'
make_stub graphify  '^install'
make_stub fastmcp   '^(install|run)'
make_stub obsidian  '.'
make_stub node      'NEVERMATCH'   # exists so `have node` is true; -v probes fine
make_stub crontab   '.'
make_stub launchctl '^(load|bootstrap)'
# claude: plugin/mcp WRITES are violations; list/--version reads are fine.
cat > "$STUB/claude" <<STUBEOF
#!/usr/bin/env bash
case "\$*" in
  "plugin marketplace add"*|"plugin install"*|"mcp add"*) echo "claude \$*" >> "$VIOLATIONS" ;;
esac
exit 0
STUBEOF
chmod +x "$STUB/claude"

OUT="$(cd "$TMPROOT" && run_sandboxed "$FAKEHOME" env PATH="$STUB:/usr/bin:/bin" \
      EMAIL_GATE_BYPASS=1 PREFLIGHT_BYPASS=1 \
      bash "$BOOTSTRAP" --dry-run 2>&1)"; RC=$?

if [ "$RC" = "0" ]; then
  ok "T1a: --dry-run exits 0 in the sandbox"
else
  no "T1a: --dry-run exited $RC :: $(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"
fi
if [ ! -s "$VIOLATIONS" ]; then
  ok "T1b: zero mutation attempts recorded during --dry-run"
else
  no "T1b: --dry-run attempted mutations: $(head -5 "$VIOLATIONS" | tr '\n' '; ')"
fi
if printf '%s' "$OUT" | grep -q '\[dry-run\]'; then
  ok "T1c: dry-run preview lines are printed"
else
  no "T1c: no [dry-run] preview lines in output"
fi
if [ ! -f "$FAKEHOME/.claude/settings.json" ] && [ ! -f "$FAKEHOME/.claude/.mcp.json" ]; then
  ok "T1d: no settings.json / .mcp.json written under --dry-run"
else
  no "T1d: --dry-run wrote config files into HOME"
fi

# ---- T2. structural: every install command sits behind a DRY_RUN check -------
# The sandbox above never reaches the install branches (stubs make every tool
# look present), so pin the guards at the source level: within the 40 lines
# BEFORE each mutating command there must be a DRY_RUN test.
T2=$(python3 - "$BOOTSTRAP" <<'PY'
import sys, re
lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
tokens = [
    "Homebrew/install/HEAD/install.sh",
    "brew install python@3.12",
    "brew install node",
    "npm install -g @anthropic-ai/claude-code",
    "brew install pipx",
    "brew install gh",
    "brew install --cask obsidian",
    "snap install obsidian",
    "pipx install graphifyy",
    "pipx install fastmcp",
]
bad = []
for tok in tokens:
    hits = [i for i, l in enumerate(lines) if tok in l and not l.lstrip().startswith("#")]
    # ignore hits inside dry/echo/log strings (preview lines mention commands)
    real = [i for i in hits if not re.search(r'\b(dry|log|warn|err|ok)\s', lines[i].lstrip()[:6])]
    if not real:
        bad.append(f"{tok}: not found (section removed? update this test)")
        continue
    for i in real:
        window = "\n".join(lines[max(0, i - 40):i])
        if "DRY_RUN" not in window:
            bad.append(f"{tok}: line {i+1} has no DRY_RUN check in the 40 lines above")
print("OK" if not bad else "BAD: " + " | ".join(bad))
PY
)
if [ "$T2" = "OK" ]; then
  ok "T2: every install command is behind a DRY_RUN guard"
else
  no "T2: $T2"
fi

echo
echo "test_dry_run_purity: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
