"""Shared scanner for the inline-bypass-REACHABILITY class.

A PreToolUse(Bash) gate that honors a `*_BYPASS` env var MUST also consult the
COMMAND STRING for it: an inline `VAR=1 <cmd>` prefix lives only in the
command, never in the hook process's (session) env. A gate that reads
os.environ only advertises a bypass that can't fire -- training people to
disable the guard some other way instead, which is worse than no advertised
bypass at all.

Single source of truth for "is hook X in the broken class?", shared by:
  - test_bypass_reachability_watchdog.py  (CI: scans THIS repo's tracked
                                            hooks/ dir)
  - surface-bypass-unreachable.py         (SessionStart: scans the DEPLOYED
                                            union ~/.claude/hooks -- catches a
                                            broken hook tracked in a DIFFERENT
                                            source repo, or untracked entirely)

WHY BOTH ARE NEEDED. Hooks deploy to ~/.claude/hooks from more than one source
repo. No single repo's CI sees the deployed union, so a per-repo CI watchdog is
structurally blind to a hook that shipped from elsewhere, or was hand-edited
after install. The deployed-surface surfacer is the catch-all for that gap;
the CI watchdog is what stops a NEW instance of the class from merging here.
"""
from __future__ import annotations

import glob
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:                                        # _lib itself is on sys.path
    from safe_read import safe_read_text
except ImportError:                        # hooks/ (the parent) is on sys.path
    from _lib.safe_read import safe_read_text

# Reads a `*_BYPASS` env var via os.environ, written INLINE as a literal.
ENV_BYPASS_RE = re.compile(r'os\.environ(?:\.get\(|\[)["\'][A-Z0-9_]*BYPASS')
# ...or INDIRECTLY, through a constant bound to the name:
#     BYPASS = "SOME_GUARD_BYPASS"
#     if os.environ.get(BYPASS) == "1": ...
# Hoisting the name into a constant is BETTER style, and a scanner that only
# matched the literal-string shape above would call it invisible: it gates a
# Bash command, advertises an inline bypass, and never spells the env-var name
# where a literal-only regex could see it. Widening to catch this shape only
# ever ADDS hooks to `enforce` -- it cannot mask a violator the literal form
# already caught, so it is a strictly safer scan, never a weaker one.
_ENV_BYPASS_VAR_RE = re.compile(
    r'os\.environ(?:\.get\(|\[)\s*([A-Za-z_][A-Za-z0-9_]*)')
_BYPASS_CONST_RE = re.compile(
    r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*["\'][A-Z0-9_]*BYPASS["\']', re.M)
# References the Bash tool (so it gates Bash commands).
_BASH_RE = re.compile(r'tool_name.*Bash|"Bash"|\'Bash\'')
# ...or reads tool_input's "command" key without ever spelling "Bash" literally
# -- true of a gate that relies entirely on its hooks.json PreToolUse:Bash
# matcher for tool-type filtering instead of a redundant self-check. "command"
# is a Bash-only key among Claude Code's standard tool_input shapes (Write and
# Edit carry file_path/content/old_string/new_string, Task carries prompt), so
# this is a narrow, low-false-positive alternative to the literal-string
# check, not a looser substitute for it -- either signal is sufficient.
_COMMAND_KEY_RE = re.compile(r'\.get\(\s*["\']command["\']|\[\s*["\']command["\']\s*\]')
# Consults the command string for the bypass (the fix shape). Matches this
# repo's actual call sites: the shared `_lib/cmd_env.inline_bypass` wrapper,
# the `_lib/shell_parse.leading_env_assigns` primitive it wraps (called
# directly by a couple of hooks), and every hook that keeps its own local
# `_inline_bypass` copy instead of the shared one -- `inline_bypass` matches
# as a substring of `_inline_bypass` too, so a local copy is not penalized for
# being local, only for not consulting the command at all.
#
# Requires an actual CALL (`name(`), not a bare mention, and not the `def
# name(` that DEFINES it -- every real caller in this repo ships a `def
# inline_bypass(...): return False` fallback for when _lib is unreachable, so
# a call-only match without the `(?<!def )` exclusion still matched that
# definition's own signature. Proven live in two steps: reverting a fixed
# hook's `or inline_bypass(cmd, VAR)` back to os.environ-only, while leaving
# the now-dead `from cmd_env import inline_bypass` line in place, first left
# the scanner reporting the file clean (a bare mention read as "consults");
# requiring a call closed that but the file's OWN fallback `def
# inline_bypass(...)` line then satisfied the call-shaped regex too. The
# fleet behavioral test (test_bypass_reachability_watchdog.py) caught the
# regression correctly at every step, which is why that layer exists
# alongside this one -- this is a text scanner, not control-flow analysis,
# and cannot see whether a matched call is on a path that actually runs (e.g.
# `if False: inline_bypass(...)` would still pass). That residual gap is
# accepted, same as every other lightweight scanner in this repo
# (check-hook-negative-control.py's own docstring names the identical limit
# for its check).
CONSULTS_CMD_RE = re.compile(
    r'(?<!def )inline_bypass\s*\(|(?<!def )leading_env_assigns\s*\('
    r'|(?<!def )segment_bypass_flags\s*\(|cmd_env\.\w+\s*\(')


def is_bash_gate(src):
    """True if the hook extracts a Bash command to act on (vs a Stop/
    SessionStart surfacer that touches neither tool_input.command nor Bash)."""
    if "tool_input" not in src or "command" not in src:
        return False
    return _BASH_RE.search(src) is not None or _COMMAND_KEY_RE.search(src) is not None


def reads_bypass_env(src):
    """True if the hook gates on a `*_BYPASS` env var, however it spells it.

    Two shapes, both real in a deployed fleet: the name inline as a literal,
    and the name hoisted into a module constant. Missing the second lets a
    live violator sit outside `enforce` while this scan reports clean.
    """
    if ENV_BYPASS_RE.search(src) is not None:
        return True
    consts = set(_BYPASS_CONST_RE.findall(src))
    if not consts:
        return False
    return any(v in consts for v in _ENV_BYPASS_VAR_RE.findall(src))


def consults_command(src):
    return CONSULTS_CMD_RE.search(src) is not None


def scan_hooks(hooks_dir, exempt=frozenset()):
    """Scan a hooks dir. Returns (enforce, violators):

    - enforce:   hooks that are Bash-gates AND read a `*_BYPASS` env (in scope).
    - violators: the subset that NEVER consult the command for the bypass
                 (the broken class -- os.environ-only).

    Skips `test_*.py` and any basename in `exempt`. Unreadable files are
    skipped (never raises) so a surfacer can't crash session start -- reads go
    through the cloud-safe bounded primitive (`_lib/safe_read.py`) rather than
    a bare `open()`, since the deployed-surface caller walks a real user
    directory that can hold a cloud placeholder or a stalled mount.
    """
    enforce, violators = [], []
    for path in sorted(glob.glob(os.path.join(hooks_dir, "*.py"))):
        name = os.path.basename(path)
        if name.startswith("test_") or name in exempt:
            continue
        res = safe_read_text(path, timeout=5.0, max_bytes=4_000_000, errors="replace")
        if not res.ok:
            continue
        src = res.text or ""
        if not (reads_bypass_env(src) and is_bash_gate(src)):
            continue
        enforce.append(name)
        if not consults_command(src):
            violators.append(name)
    return enforce, violators
