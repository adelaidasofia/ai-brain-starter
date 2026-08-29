#!/usr/bin/env python3
"""audit-sessionstart-boundedness.py - forward guard for the SessionStart freeze class.

A SessionStart hook fires once per Claude Code session. A machine running N
concurrent sessions runs every SessionStart hook N times at once. So a single
SessionStart hook that walks a data corpus (the user's vault, the
~/.claude/projects transcript tree, ...) without concurrency + wall-clock bounds
is a per-session multiplier: under N sessions it becomes N concurrent corpus
walks.

On 2026-06-05 that exact shape hard-froze a machine (load 36, total freeze): the
corpus-walk secret scan ran on SessionStart, stamped its 6h cooldown AFTER the
slow walk, so every session that started mid-walk blew past the cooldown and
launched its own full walk; four concurrent multi-minute walks pegged the CPU.
MYC-512 moved that scan off SessionStart, MYC-514 hardened it, and
tests/integration/test_sessionstart_freeze_class_excluded.sh locks those two
SPECIFIC hooks in place. This guard closes the FORWARD gap those leave open:
nothing stopped the NEXT new SessionStart hook from reintroducing the class.

The invariant (docs/HOOK_FLEET_RESOURCE_GOVERNANCE.md): a SessionStart hook that
performs a RECURSIVE / corpus-scale filesystem walk must carry ALL THREE
bounded-hook guards, OR declare an explicit small-scope exemption.

  1. single-instance lock      - fcntl.flock (py) / flock (sh): at most one runs
                                 at a time; a concurrent session backs off
                                 instead of starting a second walk.
  2. cooldown stamped AT START - the cooldown marker is claimed BEFORE the walk,
                                 so a session that starts mid-walk sees the marker
                                 and skips. (Stamping the marker AFTER the walk is
                                 the precise 2026-06-05 bug.)
  3. wall-clock deadline       - signal.alarm / a time-budget break / a subprocess
                                 timeout: a single pass can never run away.

  Exemption - for a walk that is small BY CONSTRUCTION (e.g. one worktree-snapshot
  directory, not a data corpus), add a co-located, greppable, REVIEWABLE
  annotation in the hook source:

        # sessionstart-walk-bounded: <reason the walked root is small / bounded>

  The reason is REQUIRED (a bare token does not exempt). `--all` lists every
  exemption so they can be audited. The annotation downgrades the requirement; it
  never silently drops the check.

A hook that does NOT recursive-walk - it reads a marker, does one `iterdir` level,
or runs a bounded subprocess - needs none of this. It is bounded by construction
and passes clean.

Modes:
  --all [--hooks-json P] [--hooks-dir D [D ...]]
                    Audit the canonical SessionStart set (default: the SessionStart
                    block of <repo>/hooks.json, resolved against <repo>/hooks/ AND
                    <repo>/scripts/ — a SessionStart hook may ship in either, and
                    heal-journal-guard.py ships in scripts/, so a hooks/-only
                    resolver skipped it silently). Exit 1 if any wired hook is an
                    unguarded, unexempt corpus walker; exit 2 if any wired hook
                    could not be resolved and read. This is the CI gate.
  --check FILE      Add-time gate for ONE candidate SessionStart hook script: exit
                    1 if it recursive-walks without the guards or the exemption.
                    Advisory - fails OPEN (exit 0) on its own read error. Used by
                    the write-time hookify rule.
  --selftest        Positive + negative controls: a corpus walk with no guards
                    BITES; one stamping AFTER the walk BITES; one with all three
                    guards PASSES; one carrying the exemption annotation PASSES; a
                    no-walk hook PASSES; a shell walk with no timeout BITES. Plus
                    resolver controls: the Windows launcher-shim command form
                    resolves to the TARGET hook (not to hook_runner.py), and an
                    unresolvable command still resolves to None (skipped, never
                    counted bounded). Exit 1 on any wrong verdict.
  --json            machine-readable output (with --all).

Stdlib only. Reads are bounded (1 MB cap, binary skip) - cloud-safe-walk
compliant. --all / --selftest fail LOUD (exit 2) on an internal error so a broken
detector can never silently pass CI; --check fails OPEN (it is advisory).

Exit codes (--all / --settings):
  0  every wired SessionStart hook was READ and is bounded or declared-bounded
  1  at least one hook is an unguarded, unexempt corpus walker
  2  UNEVALUATED - a hook was skipped (unresolvable command, missing file,
     unreadable source), or an internal error. NEVER 0: an audit is only as
     strong as the fraction of the set it actually opened, and "6 of 25 read,
     0 unguarded" is not "the fleet is bounded". This mirrors
     scripts/audit-guard-activation-roots.py, which already refuses to call an
     empty comparison a pass ("Nothing was compared - that is not the same as
     everything matching"). MYC-3879: run against a live settings.json this
     printed "6 resolved - 6 clean, 0 unguarded; 19 unresolved" followed by a
     bare OK and exit 0. Nineteen wired commands were never opened and the
     verdict said PASS.

Provenance: MYC-571 (parent incident MYC-570, the 2026-06-05 Mac-freeze);
verdict + resolution-root fixes MYC-3879.
Canonical guarded example: hooks/scan-prior-sessions-for-secrets.py.
"""
# exit-contract: ENFORCING

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MAX_READ = 1_000_000  # 1 MB bounded read (cloud-safe-walk: never block on a placeholder)

# ---- recursive / corpus-scale walk signals ------------------------------------
# Single-level iterdir / os.listdir is deliberately NOT here: one directory level
# is bounded by construction and is never the freeze class.
WALK_PY = [
    (r"\bos\.walk\s*\(", "os.walk"),
    (r"(?<!os)\.walk\s*\(", "Path.walk"),          # pathlib.Path.walk (py3.12+)
    (r"\.rglob\s*\(", "rglob"),
    (r"\.glob\s*\(\s*['\"][^'\"]*\*\*", "glob('**')"),
    (r"\bglob\.(?:i)?glob\s*\([^)]*\*\*", "glob.glob('**')"),
]
WALK_SH = [
    (r"\bgrep\s+-[a-zA-Z]*[rR]\b", "grep -r"),
    (r"\bls\s+-[a-zA-Z]*R\b", "ls -R"),
    (r"\brg\b(?![^\n]*--files-with-matches[^\n]*\bNUL\b)[^\n]*\s[\"'$~/.]", "rg"),
]
# `find` is recursive UNLESS it is depth-pinned (-maxdepth 0/1/2). A shallow
# `find ~/x -maxdepth 2` is not the deep-corpus-walk freeze class; deeper or
# unbounded `find` is still a walk. (Tightened MYC-1113 so the diagnose check does
# not cry wolf on the common shallow-find idiom — over-strict teaches bypass.)
FIND_SH = r"\bfind\s+[\"'$~/.]"
FIND_SHALLOW = r"-maxdepth\s+[0-2]\b"

ANNOT_EXEMPT = r"#\s*sessionstart-walk-bounded:\s*\S"  # token + non-empty reason

# ---- guard recognizers (python) -----------------------------------------------
FLOCK_PY = r"\bfcntl\.flock\b|\bFileLock\b|\bfilelock\.|\bflock\s*\("
DEADLINE_PY = (
    r"\bsignal\.alarm\s*\(|\bsignal\.setitimer\b|\bthreading\.Timer\b|"
    r"\bdeadline\b|\b\w*BUDGET\w*\b|\btimeout\s*=|"
    r"\btime\.(?:time|monotonic)\s*\(\s*\)\s*>"
)
STAMP_CALL_PY = [
    r"\b_stamp\s*\(",
    r"\b_write_epoch\s*\(",
    r"\.touch\s*\(",
    r"\bos\.utime\s*\(",
]
STAMP_WRITE_PY = r"\.write_text\s*\(|\.write_bytes\s*\(|\.write\s*\("
STAMP_MARKERISH = r"(?i)marker|cooldown|stamp|\.last|attempt|\.ran\b"
STAMP_ANNOT = r"#\s*stamp-at-start\b"

# ---- guard recognizers (shell) ------------------------------------------------
FLOCK_SH = r"\bflock\b"
DEADLINE_SH = r"\btimeout\s+[0-9-]|\bperl\b[^\n]*\balarm\b|\$SECONDS\b"
STAMP_LINE_SH = (
    r">\s*[\"']?\$?\{?\w*(?:MARKER|marker|stamp|cooldown|last|ran)\w*"
    r"|\bdate\s+\+%s\b[^\n]*>"
    r"|\btouch\b\s+[\"'$~/.]"
)


def _read_bounded(path: Path) -> str:
    """Read up to MAX_READ bytes; skip binary/oversize/unreadable. Fail-open."""
    try:
        if path.stat().st_size > MAX_READ:
            return ""
        b = path.read_bytes()
        if b"\x00" in b[:4096]:
            return ""
        return b.decode("utf-8", "ignore")
    except Exception:
        return ""


def _detect_lang(name: str, src: str) -> str:
    if name.endswith(".py"):
        return "py"
    if name.endswith((".sh", ".bash")):
        return "sh"
    first = src.lstrip().splitlines()[0] if src.strip() else ""
    if first.startswith("#!"):
        if "python" in first:
            return "py"
        if "sh" in first or "bash" in first:
            return "sh"
    return "py"


def _main_scope_start(lines: list[str]) -> int:
    """Index of `def main(`; stamp-vs-walk ordering is judged within the execution
    path (main / module level), so a helper def body above main - whose textual
    order is not its call order - cannot spoof 'stamped at start'."""
    for i, ln in enumerate(lines):
        if re.match(r"\s*(?:async\s+)?def\s+main\s*\(", ln):
            return i
    return 0


def _first_line(lines: list[str], patterns: list[str], start: int = 0) -> int | None:
    """Lowest index >= start matching any pattern, skipping comment + def lines."""
    rxs = [re.compile(p) for p in patterns]
    for i in range(start, len(lines)):
        s = lines[i].lstrip()
        if s.startswith("#"):
            continue
        if s.startswith(("def ", "async def ")):
            continue
        if any(rx.search(lines[i]) for rx in rxs):
            return i
    return None


def _stamp_before_walk(lines: list[str], walk_line: int | None, scope: int) -> bool:
    """True if a marker-stamp idiom appears in the execution path BEFORE the walk."""
    if walk_line is None:
        return False  # walk not located in the execution path -> cannot prove order
    call_rx = [re.compile(p) for p in STAMP_CALL_PY]
    write_rx = re.compile(STAMP_WRITE_PY)
    marker_rx = re.compile(STAMP_MARKERISH)
    for i in range(scope, walk_line):
        s = lines[i].lstrip()
        if s.startswith("#") or s.startswith(("def ", "async def ")):
            continue
        if any(rx.search(lines[i]) for rx in call_rx):
            return True
        if write_rx.search(lines[i]) and marker_rx.search(lines[i]):
            return True
    return False


def _exempt_reason(src: str) -> str:
    m = re.search(r"#\s*sessionstart-walk-bounded:\s*(.+)", src)
    return m.group(1).strip()[:120] if m else ""


def evaluate(src: str, lang: str = "py") -> dict:
    """Pure verdict for one hook source. Keys: walk(bool), walks(list), ok(bool),
    exempt(bool), exempt_reason(str), missing(list[str])."""
    if not src:
        return dict(walk=False, walks=[], ok=True, exempt=False, exempt_reason="", missing=[])
    lines = src.splitlines()

    if lang == "sh":
        walks = [lbl for rx, lbl in WALK_SH if re.search(rx, src)]
        if re.search(FIND_SH, src) and not re.search(FIND_SHALLOW, src):
            walks.append("find")
        walks = sorted(set(walks))
    else:
        walks = sorted({lbl for rx, lbl in WALK_PY if re.search(rx, src)})
        # subprocess shell-out to a recursive walker: `find` without -maxdepth 1,
        # or `grep -r`. Native rglob froze the 2026-06-05 machine; a shelled walk
        # is the same corpus cost.
        sub = r"(?:subprocess\.\w+|Popen|check_output|os\.system|os\.popen)\s*\([\s\S]{0,120}?"
        if re.search(sub + r"['\"]grep['\"][\s\S]{0,40}?-[a-zA-Z]*[rR]\b", src):
            walks.append("subprocess grep -r")
        if re.search(sub + r"['\"]find['\"]", src) and not re.search(r"-maxdepth\s+[01]\b", src):
            walks.append("subprocess find")
        walks = sorted(set(walks))

    if not walks:
        return dict(walk=False, walks=[], ok=True, exempt=False, exempt_reason="", missing=[])

    if re.search(ANNOT_EXEMPT, src):
        return dict(walk=True, walks=walks, ok=True, exempt=True,
                    exempt_reason=_exempt_reason(src), missing=[])

    scope = _main_scope_start(lines)
    if lang == "sh":
        flock_ok = re.search(FLOCK_SH, src) is not None
        deadline_ok = re.search(DEADLINE_SH, src) is not None
        walk_line = _first_line(lines, [rx for rx, _ in WALK_SH] + [FIND_SH], scope)
        stamp_line = _first_line(lines, [STAMP_LINE_SH], scope)
        stamp_ok = (re.search(STAMP_ANNOT, src) is not None
                    or (stamp_line is not None and walk_line is not None and stamp_line < walk_line))
    else:
        flock_ok = re.search(FLOCK_PY, src) is not None
        deadline_ok = re.search(DEADLINE_PY, src) is not None
        walk_line = _first_line(lines, [rx for rx, _ in WALK_PY], scope)
        stamp_ok = (re.search(STAMP_ANNOT, src) is not None
                    or _stamp_before_walk(lines, walk_line, scope))

    missing = []
    if not flock_ok:
        missing.append("single-instance lock (fcntl.flock / flock)")
    if not stamp_ok:
        missing.append("cooldown stamped-at-START (marker write before the walk)")
    if not deadline_ok:
        missing.append("wall-clock deadline / budget")
    return dict(walk=True, walks=walks, ok=(not missing), exempt=False,
                exempt_reason="", missing=missing)


# ---- SessionStart set resolution ----------------------------------------------
# hook_runner.py is the Windows installer's launcher shim, never a hook itself —
# see resolve_command_path() for the full why. Both resolvers in this file have to
# skip it, or a platformized hooks.json resolves EVERY entry to the launcher.
_RUNNER_SHIM_RE = re.compile(r"(?:^|[\\/])hook_runner\.py$", re.IGNORECASE)


def _basename(cmd: str) -> str | None:
    """The hook script's basename — the launcher's, only when it launches nothing.

    --all reads a hooks.json, which ships in POSIX form, so the shim skip is
    latent there today. It is still wrong to leave: pointed at a platformized
    file, EVERY entry would resolve to hook_runner.py, none would be found under
    --hooks-dir, and the gate would report an empty fleet and exit 0 — the silent
    pass this whole script exists to prevent."""
    names = re.findall(r"([\w.\-]+\.(?:py|sh|bash))", cmd)
    if not names:
        return None
    return next((n for n in names if not _RUNNER_SHIM_RE.search(n)), names[0])


def sessionstart_basenames(hooks_json: Path) -> list[str]:
    try:
        data = json.loads(hooks_json.read_text(encoding="utf-8"))
    except Exception:
        return []
    names: list[str] = []
    for block in (data.get("hooks", {}).get("SessionStart") or []):
        for hook in (block.get("hooks") or []):
            bn = _basename(hook.get("command", "") or "")
            if bn and bn not in names:
                names.append(bn)
    return names


def _cite() -> str:
    return ("Make it bounded by construction (see hooks/scan-prior-sessions-for-secrets.py "
            "and docs/HOOK_FLEET_RESOURCE_GOVERNANCE.md): add a single-instance "
            "fcntl.flock, stamp the cooldown marker BEFORE the walk, and break on a "
            "wall-clock deadline. If the walked root is small by construction, declare "
            "it: `# sessionstart-walk-bounded: <reason>`.")


# ---- modes --------------------------------------------------------------------
def _resolve_in_dirs(bn: str, dirs: list[Path]) -> Path | None:
    """First existing <dir>/<basename> across the resolution roots, else None.

    Why a LIST and not one hooks/ directory (MYC-3879): SessionStart hooks ship
    from two places. heal-journal-guard.py is wired on SessionStart and lives in
    <repo>/scripts, so a hooks/-only resolver could not open it and dropped it
    into the "not found - skipped" bucket, where the old verdict ignored it."""
    for d in dirs:
        f = d / bn
        if f.exists():
            return f
    return None


def cmd_all(hooks_json: Path, hooks_dirs: list[Path], as_json: bool) -> int:
    names = sessionstart_basenames(hooks_json)
    rows, reds, exempts, unresolved = [], [], [], []
    for bn in names:
        f = _resolve_in_dirs(bn, hooks_dirs)
        if f is None:
            unresolved.append(bn)
            continue
        src = _read_bounded(f)
        if not src:
            # Unreadable / binary / oversize. Previously indistinguishable from
            # "clean" because evaluate("") returns ok=True with no walk. An
            # unread file is UNEVALUATED, not bounded.
            unresolved.append(bn)
            continue
        v = evaluate(src, _detect_lang(bn, src))
        rows.append((bn, v))
        if v["walk"] and not v["ok"] and not v["exempt"]:
            reds.append((bn, v))
        elif v["exempt"]:
            exempts.append((bn, v))

    if as_json:
        print(json.dumps(dict(
            hooks_json=str(hooks_json),
            hooks_dirs=[str(d) for d in hooks_dirs],
            audited=[bn for bn, _ in rows],
            unresolved=unresolved,
            verdict=("FAIL" if reds else "UNEVALUATED" if unresolved else "PASS"),
            red=[dict(hook=bn, walks=v["walks"], missing=v["missing"]) for bn, v in reds],
            exempt=[dict(hook=bn, reason=v["exempt_reason"]) for bn, v in exempts],
        ), indent=2))
        return 1 if reds else (2 if unresolved else 0)

    print("=" * 78)
    print("SessionStart boundedness audit (MYC-571) - corpus walks need the 3 guards")
    print("=" * 78)
    print(f"hooks.json: {hooks_json}")
    print(f"resolution roots: {', '.join(str(d) for d in hooks_dirs)}")
    print(f"{len(rows)} of {len(names)} SessionStart hook(s) audited; "
          f"{len(reds)} unguarded corpus walk(s); {len(exempts)} declared-bounded; "
          f"{len(unresolved)} NOT EVALUATED.\n")
    for bn, v in rows:
        if v["walk"] and not v["ok"] and not v["exempt"]:
            mark = "RED "
        elif v["exempt"]:
            mark = "ex  "
        elif v["walk"]:
            mark = "ok* "
        else:
            mark = "ok  "
        detail = ""
        if v["walks"]:
            detail = f" walk={'+'.join(v['walks'])}"
        if v["exempt"]:
            detail += f"  bounded: {v['exempt_reason']}"
        if v["walk"] and not v["ok"] and not v["exempt"]:
            detail += f"  MISSING: {', '.join(v['missing'])}"
        print(f"  [{mark}] {bn}{detail}")
    if reds:
        print("\n" + _cite())
        return 1
    if unresolved:
        # NOT a note, and NOT exit 0. This used to print "user-level only,
        # skipped" and pass — an assumption about hooks nothing had opened.
        print(f"\nUNEVALUATED: {len(unresolved)} wired SessionStart hook(s) could not be "
              f"resolved and read under {', '.join(str(d) for d in hooks_dirs)}:")
        for bn in unresolved:
            print(f"    - {bn}")
        print("Nothing was checked for those - that is not the same as them being\n"
              "bounded. Ship the hook into a resolution root, or pass the root that\n"
              "holds it with --hooks-dir. (heal-journal-guard.py is exactly this\n"
              "class: SessionStart-wired, shipped in scripts/, invisible to a\n"
              "hooks/-only audit that then reported OK.)")
        return 2
    print("\nAll SessionStart corpus walks are guarded or declared-bounded. OK.")
    return 0


def cmd_check(path: str) -> int:
    p = Path(path)
    src = _read_bounded(p)
    if not src:
        return 0  # advisory: unreadable / binary -> fail open
    v = evaluate(src, _detect_lang(p.name, src))
    if v["walk"] and not v["ok"] and not v["exempt"]:
        print(f"WARN [sessionstart-boundedness] {p.name} does a recursive walk "
              f"({'+'.join(v['walks'])}). IF this hook is (or will be) wired on "
              f"SessionStart, it is missing: {', '.join(v['missing'])}.")
        print("     " + _cite())
        return 1
    return 0


# ---- effective wired-set audit (the real harm surface) ------------------------
def sessionstart_commands(settings_path: Path) -> list[str]:
    """Raw SessionStart hook command strings from a settings.json / hooks.json."""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[str] = []
    for block in (data.get("hooks", {}).get("SessionStart") or []):
        for hook in (block.get("hooks") or []):
            c = (hook.get("command") or "").strip()
            if c:
                out.append(c)
    return out


# Every script path a command names, quoted or bare, in command order. THREE path
# forms have to parse, because the Windows leg of the installer
# (install-hooks-user-level.py) rewrites every hook command into a launcher shim:
#
#   POSIX bare      python3 ~/.claude/hooks/x.py 2>/dev/null || echo '{...}'
#   POSIX guarded   [ -f ~/.claude/hooks/x.sh ] && bash ~/.claude/hooks/x.sh
#   Windows shim    py -3 "C:\...\scripts\hook_runner.py" --fallback silent "C:\...\hooks\x.py"
#
# The pre-fix matcher required a literal `python`/`python3`/`bash`/`sh`/`zsh`
# token, OR a forward slash inside the quotes, OR a `~`/`/` first character. The
# Windows form has none of the three (`py -3` is the launcher, paths are
# backslashed drive letters), so resolve_command_path() returned None for it and
# the EFFECTIVE-set audit evaluated essentially nothing on every Windows install:
# measured on a real box, 19 of 25 wired SessionStart commands resolved to
# nothing. Same silent-no-op class as issues #370/#375/#485 (MYC-3879).
#
# Kept structurally parallel to _GUARD_PATH_RE in scripts/heal-journal-guard.py:
# quoted alternatives first (a Windows home MAY contain a space), bare last (an
# unquoted path ends at whitespace or a shell operator). Roots accepted: `~`,
# POSIX `/`, a UNC `\\share`, a drive letter. Over-matching stays harmless — the
# caller still stats the path — but a BARE path may only start where a token can,
# else `./hooks/x.py` would match at its inner `/` and a cwd-relative command
# would resolve to a bogus absolute `/hooks/x.py`.
_SCRIPT_TOKEN_RE = re.compile(
    r"\"([^\"\n]+\.(?:py|sh|bash))\""
    r"|'([^'\n]+\.(?:py|sh|bash))'"
    r"|(?<![^\s'\"|&;(=])((?:~|/|\\\\|[A-Za-z]:[\\/])[^\s'\"|&;]*\.(?:py|sh|bash))"
)
_DRIVE_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")  # _RUNNER_SHIM_RE: see _basename above


def _script_tokens(cmd: str) -> list[str]:
    """Script-looking paths in `cmd`, in command order."""
    return [next(g for g in m.groups() if g) for m in _SCRIPT_TOKEN_RE.finditer(cmd)]


def _absolute_or_none(raw: str) -> Path | None:
    """Expand `~` and accept a path that is absolute in EITHER flavour.

    Path.is_absolute() is flavour-BOUND: on Windows `/a/b` is not absolute (no
    drive) and on POSIX `C:\\a\\b` is not absolute (no root). Leaning on it alone
    would make this verdict depend on which OS runs the auditor; judging the
    string keeps resolution platform-independent and unit-testable from POSIX CI.
    A path that does not exist on THIS host is still counted unresolved by the
    caller, which stats it.

    Anything relative / cwd-dependent stays None: reported as unresolved, NEVER
    silently treated as bounded."""
    p = raw.strip()
    if p.startswith("~"):
        p = str(Path.home()) + p[1:]
    if _DRIVE_ABS_RE.match(p) or p.startswith(("/", "\\\\")):
        return Path(p)
    return Path(p) if Path(p).is_absolute() else None


def resolve_command_path(cmd: str) -> Path | None:
    """Best-effort resolve the script a SessionStart command runs, to an ABSOLUTE
    path. Handles `python3 ~/.claude/hooks/x.py --flag`, quoted paths, a leading
    `[ -f X ] && ...` guard, Windows drive-letter paths, and the Windows launcher
    shim. Returns None when no path is found OR it is not absolute after
    expanduser — a relative/cwd-dependent command is reported as unresolved,
    NEVER silently treated as bounded.

    hook_runner.py is a LAUNCHER, not a hook: on Windows every command reads
    `py -3 "<abs>/scripts/hook_runner.py" --fallback silent "<abs>/hooks/x.py"`,
    and the shim only re-execs the target with the payload on stdin. So a command
    that goes through the shim resolves to its TARGET — the first script path
    AFTER the shim, matching hook_runner's own argv handling
    (`[--fallback MODE] <target> [extra...]`). Resolving to the shim instead
    would be WORSE than not resolving at all: the launcher is walk-free and
    identical for all 19 hooks, so the audit would read one trivially-clean file
    19 times and report a clean fleet having never opened a hook. That is not
    hypothetical — the installer's launcher is `python`/`python3` where `py` is
    absent, and the pre-fix matcher DID resolve that form, straight to the shim."""
    toks = _script_tokens(cmd)
    if not toks:
        return None
    shim = next((i for i, t in enumerate(toks) if _RUNNER_SHIM_RE.search(t)), None)
    if shim is not None:
        target = next((t for t in toks[shim + 1:] if not _RUNNER_SHIM_RE.search(t)), None)
        if target:  # no target at all -> the shim IS what runs; fall through
            return _absolute_or_none(target)
    return _absolute_or_none(toks[0])


def cmd_settings(settings_path: Path, porcelain: bool) -> int:
    """Audit the EFFECTIVE wired SessionStart set in a real settings.json — the
    surface where the freeze actually happens, vs the canonical hooks.json
    template that --all checks (MYC-1113). Resolves each hook by its full command
    path.

    Exit: 0 = every wired command was READ and is bounded/declared; 1 = >=1
    unguarded corpus walker; 2 = UNEVALUATED — settings.json missing /
    unparseable, OR at least one wired command was never opened (MYC-3879).

    That last case used to exit 0. On a live Windows settings.json it printed
    "6 resolved hook(s) — 6 clean, 0 unguarded; 19 unresolved" and then "OK."
    Nineteen SessionStart commands went unread and the verdict was PASS, so a
    freeze-class hook among them could never have been reported. The skip count
    was already on screen; only the EXIT CODE decides what CI and diagnose.sh
    believe, and it said everything was fine."""
    p = Path(settings_path)
    if not p.exists():
        print("ERROR:not-found" if porcelain else f"ERROR: settings.json not found: {p}")
        return 2
    try:
        json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print("ERROR:unparseable" if porcelain else f"ERROR: settings.json unparseable: {e}")
        return 2
    reds, exempts, clean = [], 0, 0
    # (why-it-was-skipped, what-was-skipped). Kept as a list, not a bare count:
    # "19 unresolved" is unactionable, and an unactionable number is what let
    # this stay at 19 for months.
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for c in sessionstart_commands(p):
        rp = resolve_command_path(c)
        if rp is None:
            skipped.append(("no absolute script path in the command", c[:96]))
            continue
        if str(rp) in seen:
            continue
        seen.add(str(rp))
        if not rp.exists():
            skipped.append(("script not on disk", str(rp)))
            continue
        src = _read_bounded(rp)
        if not src:
            skipped.append(("source unreadable / binary / over 1 MB", str(rp)))
            continue
        v = evaluate(src, _detect_lang(rp.name, src))
        if v["walk"] and not v["ok"] and not v["exempt"]:
            reds.append((rp.name, v))
        elif v["exempt"]:
            exempts += 1
        else:
            clean += 1
    if porcelain:
        # A found freeze-class hook outranks an incomplete audit: UNGUARDED is
        # a fact, PARTIAL is an absence of facts.
        if reds:
            print("UNGUARDED:" + str(len(reds)) + ":" + ",".join(n for n, _ in reds))
            return 1
        if skipped:
            print(f"PARTIAL:{clean}:{exempts}:{len(skipped)}")
            return 2
        print(f"OK:{clean}:{exempts}:0")
        return 0
    total = clean + exempts + len(reds)
    print(f"Effective SessionStart set ({p}): {total} resolved hook(s) — "
          f"{clean} clean, {exempts} declared-bounded, {len(reds)} unguarded; "
          f"{len(skipped)} NOT EVALUATED.")
    for n, v in reds:
        print(f"  RED  {n}: walk={'+'.join(v['walks'])}  MISSING: {', '.join(v['missing'])}")
    if reds:
        print("\n" + _cite())
        return 1
    if skipped:
        print(f"\nUNEVALUATED: {len(skipped)} wired SessionStart command(s) were never "
              f"opened, so nothing is known about them:")
        for why, what in skipped:
            print(f"    - {what}\n        ({why})")
        print("A verdict over the fraction that happened to resolve is not a verdict\n"
              "over the fleet. Every line above could be a corpus walker and this\n"
              "audit would look identical. Fix the wiring (absolute paths), install\n"
              "the missing scripts, then re-run - do NOT read this as bounded.")
        return 2
    print("All resolved SessionStart corpus walks are guarded or declared-bounded. OK.")
    return 0


# ---- selftest (positive + negative controls) ----------------------------------
_FX_NO_GUARDS = """#!/usr/bin/env python3
import os
def main():
    for r, d, f in os.walk(os.path.expanduser('~/vault')):
        pass
"""
_FX_STAMP_AFTER = """#!/usr/bin/env python3
import fcntl, time
from pathlib import Path
MARKER = Path('~/.last').expanduser()
def main():
    fh = open('/tmp/x.lock', 'w'); fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    deadline = time.time() + 60
    for p in Path('~/vault').expanduser().rglob('*.md'):
        if time.time() > deadline: break
    MARKER.write_text(str(time.time()))   # BUG: marker stamped AFTER the walk
"""
_FX_GUARDED = """#!/usr/bin/env python3
import fcntl, time
from pathlib import Path
MARKER = Path('~/.last').expanduser()
def _stamp(): MARKER.write_text(str(time.time()))
def main():
    fh = open('/tmp/x.lock', 'w'); fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    _stamp()                              # stamped BEFORE the walk
    deadline = time.time() + 60
    for p in Path('~/vault').expanduser().rglob('*.md'):
        if time.time() > deadline: break
"""
_FX_EXEMPT = """#!/usr/bin/env python3
from pathlib import Path
# sessionstart-walk-bounded: rglob is over one worktree-snapshot dir (machinery), not a corpus
def main():
    for p in Path('~/.claude/state/snapshots').expanduser().rglob('*'):
        pass
"""
_FX_NO_WALK = """#!/usr/bin/env python3
from pathlib import Path
def main():
    for p in Path('~/.claude/worktrees').expanduser().iterdir():
        pass
"""
_FX_SH_NO_TIMEOUT = """#!/usr/bin/env bash
find ~/vault -name '*.md' | while read -r f; do grep -r secret "$f"; done
"""
_FX_SH_OK = """#!/usr/bin/env bash
# sessionstart-walk-bounded: find pinned to one machinery dir level
find ~/.claude/state -maxdepth 1 -name '*.json'
"""
_FX_PATH_WALK = """#!/usr/bin/env python3
from pathlib import Path
def main():
    for _root, _dirs, _files in Path('~/vault').expanduser().walk():  # py3.12 Path.walk, no guards
        pass
"""
_FX_SUBPROC_GREP = """#!/usr/bin/env python3
import subprocess
def main():
    subprocess.run(['grep', '-r', 'secret', '/data/corpus'])  # shelled recursive walk, no guards
"""
_FX_SH_MAXDEPTH2 = """#!/usr/bin/env bash
find ~/x -maxdepth 2 -name '.draft' -type f   # depth-bounded, not the corpus-walk freeze class
"""

# ---- resolver fixtures (the Windows launcher-shim form, MYC-3879) -------------
# EXACTLY the shape install-hooks-user-level.py emits on Windows:
#     " ".join([*launcher, f'"{runner}"', "--fallback", fb, f'"{target}"'])
# Hermetic absolute paths — never this machine's home — so the controls assert the
# same thing on Windows and in POSIX CI. The win-* cases all resolve to None on
# the pre-fix matcher: they are the negative control for that bug.
_RX_RUNNER = r"C:\Users\dev\.claude\skills\ai-brain-starter\scripts\hook_runner.py"
_RX_TARGET = r"C:\Users\dev\.claude\skills\ai-brain-starter\hooks\session-start-context.py"
_RX_TARGET_SPACED = r"C:\Users\dev user\.claude\skills\ai-brain-starter\hooks\inject-instinct-context.py"
_FX_WIN_SHIM = f'py -3 "{_RX_RUNNER}" --fallback silent "{_RX_TARGET}"'
_FX_WIN_SHIM_SPACED = f'py -3 "{_RX_RUNNER}" --fallback allow "{_RX_TARGET_SPACED}"'
_FX_WIN_SHIM_PY3 = f'python3 "{_RX_RUNNER}" --fallback silent "{_RX_TARGET}"'
_FX_POSIX_CHAIN = ("python3 ~/.claude/skills/ai-brain-starter/hooks/surface-backup-status.py "
                   "2>/dev/null || echo '{\"continue\":true,\"suppressOutput\":true}'")
_FX_POSIX_GUARD = ("[ -f ~/.claude/hooks/check-claude-code-version.sh ] && "
                   "bash ~/.claude/hooks/check-claude-code-version.sh || true")


def _selftest_resolver() -> list[str]:
    """Controls for resolve_command_path(). Returns failure lines (empty == pass).

    Every case is an EQUALITY assertion, so a resolver that resolves NOTHING
    cannot pass one by accident — the shape this guards against is precisely the
    MYC-3879 bug, where 19 of 25 wired Windows commands silently resolved to None
    and the effective-set audit evaluated an empty fleet while still printing OK."""
    fails: list[str] = []

    # --- premise guards: fixtures that rot into proving nothing must fail LOUD --
    if _RX_RUNNER == _RX_TARGET or not _RX_RUNNER.endswith("hook_runner.py"):
        fails.append("premise: the shim fixtures must name hook_runner.py AND a "
                     "DIFFERENT target, else 'resolves to the target' is vacuous")
    for lbl, fx, tgt in (("silent", _FX_WIN_SHIM, _RX_TARGET),
                         ("allow+space", _FX_WIN_SHIM_SPACED, _RX_TARGET_SPACED),
                         ("python3-launcher", _FX_WIN_SHIM_PY3, _RX_TARGET)):
        if not (_RX_RUNNER in fx and tgt in fx and fx.index(_RX_RUNNER) < fx.index(tgt)):
            fails.append(f"premise[{lbl}]: fixture must carry the launcher BEFORE a "
                         f"different target: {fx!r}")
        if "--fallback" not in fx or '"' not in fx:
            fails.append(f"premise[{lbl}]: fixture is not the installer's quoted "
                         f"`--fallback` launcher form: {fx!r}")
    # Parity with the EMITTER. These fixtures only mean something while the Windows
    # installer still wraps hooks in hook_runner.py with --fallback; if it stops,
    # the shim rule above is stale and this must fail rather than keep asserting a
    # shape nobody writes. An ABSENT installer is a standalone deployed copy of
    # this script — the structural guards above still hold, so that is not a fail.
    installer = Path(__file__).resolve().parent / "install-hooks-user-level.py"
    if installer.exists():
        isrc = _read_bounded(installer)
        if not ("hook_runner" in isrc and "--fallback" in isrc):
            fails.append("premise: install-hooks-user-level.py no longer emits the "
                         "hook_runner `--fallback` launcher form — the shim rule in "
                         "resolve_command_path() is stale")

    home = str(Path.home())
    cases = [
        # Windows: must resolve to the TARGET hook, never to the launcher shim.
        ("win shim -> target, not launcher", _FX_WIN_SHIM, Path(_RX_TARGET)),
        ("win shim, space in home", _FX_WIN_SHIM_SPACED, Path(_RX_TARGET_SPACED)),
        ("win shim, python3 launcher", _FX_WIN_SHIM_PY3, Path(_RX_TARGET)),
        ("win bare drive-letter path", r"py -3 C:\Users\dev\.claude\hooks\x.py",
         Path(r"C:\Users\dev\.claude\hooks\x.py")),
        ("win shim, no target -> the shim", f'py -3 "{_RX_RUNNER}"', Path(_RX_RUNNER)),
        # POSIX forms that already worked: regression controls.
        ("posix tilde + fallback chain", _FX_POSIX_CHAIN,
         Path(home + "/.claude/skills/ai-brain-starter/hooks/surface-backup-status.py")),
        ("posix [ -f ] && guard form", _FX_POSIX_GUARD,
         Path(home + "/.claude/hooks/check-claude-code-version.sh")),
        ("posix absolute path", "python3 /Users/d/.claude/hooks/x.py",
         Path("/Users/d/.claude/hooks/x.py")),
        # Unresolvable stays unresolvable — reported as SKIPPED, never as bounded.
        ("relative -> unresolved", "python3 hooks/x.py", None),
        ("dot-relative -> unresolved", "python3 ./hooks/x.py", None),
        ("no script named -> unresolved", "echo hello", None),
    ]
    for label, cmd, want in cases:
        got = resolve_command_path(cmd)
        if got != want:
            fails.append(f"resolve[{label}]: wanted {want!r}, got {got!r}  cmd={cmd!r}")

    # _basename() is the OTHER resolver (--all): it must skip the shim too, or a
    # platformized hooks.json resolves every entry to the launcher and --all
    # audits an empty fleet.
    for label, cmd, want in (("win shim", _FX_WIN_SHIM, "session-start-context.py"),
                             ("posix chain", _FX_POSIX_CHAIN, "surface-backup-status.py"),
                             ("shim only", f'py -3 "{_RX_RUNNER}"', "hook_runner.py"),
                             ("no script", "echo hello", None)):
        got = _basename(cmd)
        if got != want:
            fails.append(f"basename[{label}]: wanted {want!r}, got {got!r}")
    return fails


def cmd_selftest() -> int:
    cases = [
        ("corpus walk, no guards",        _FX_NO_GUARDS,     "py", True),   # must BITE
        ("corpus walk, stamp AFTER walk", _FX_STAMP_AFTER,   "py", True),   # must BITE (the 2026-06-05 bug)
        ("py Path.walk, no guards",       _FX_PATH_WALK,     "py", True),   # must BITE (py3.12 walk)
        ("py subprocess grep -r",         _FX_SUBPROC_GREP,  "py", True),   # must BITE (shelled walk)
        ("corpus walk, all 3 guards",     _FX_GUARDED,       "py", False),  # must PASS
        ("walk + exemption annotation",   _FX_EXEMPT,        "py", False),  # must PASS
        ("no recursive walk (iterdir)",   _FX_NO_WALK,       "py", False),  # must PASS
        ("shell walk, no timeout/flock",  _FX_SH_NO_TIMEOUT, "sh", True),   # must BITE
        ("shell, shallow + exempt",       _FX_SH_OK,         "sh", False),  # must PASS
        ("shell find -maxdepth 2",        _FX_SH_MAXDEPTH2,  "sh", False),  # must PASS (depth-bounded)
    ]
    fails = []
    for label, src, lang, want_bite in cases:
        v = evaluate(src, lang)
        bites = v["walk"] and not v["ok"] and not v["exempt"]
        if bites != want_bite:
            fails.append(f"{label}: wanted {'BITE' if want_bite else 'PASS'}, "
                         f"got {'BITE' if bites else 'PASS'} (walks={v['walks']}, "
                         f"missing={v['missing']}, exempt={v['exempt']})")
    fails += _selftest_resolver()
    if fails:
        print("SELFTEST FAIL:")
        for f in fails:
            print("  - " + f)
        return 1
    print("SELFTEST PASS: detector bites unguarded + stamp-after corpus walks, "
          "passes 3-guard / exempt / no-walk hooks (py + sh); resolver reaches the "
          "TARGET hook through the Windows launcher shim and still skips "
          "unresolvable commands.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Forward guard: SessionStart hooks that "
                                             "corpus-walk must be flock + stamp-at-start "
                                             "+ wall-bounded, or declared-bounded (MYC-571).")
    here = Path(__file__).resolve().parent
    repo = here.parent
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--check", metavar="FILE")
    ap.add_argument("--settings", metavar="PATH",
                    help="audit the EFFECTIVE wired SessionStart set in a real settings.json")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--porcelain", action="store_true", help="one-line verdict (with --settings)")
    ap.add_argument("--hooks-json", default=str(repo / "hooks.json"))
    # nargs="+": a SessionStart hook may ship from hooks/ OR scripts/, so the
    # default is BOTH. A single-value `--hooks-dir X` still parses exactly as
    # before (existing callers and tests are unaffected) and REPLACES the
    # default pair, which keeps a hermetic fixture run from silently reaching
    # into the real repo.
    ap.add_argument("--hooks-dir", nargs="+", default=[str(repo / "hooks"),
                                                       str(repo / "scripts")],
                    help="one or more roots to resolve wired hook basenames "
                         "against (default: <repo>/hooks <repo>/scripts)")
    a = ap.parse_args()

    # --check is advisory: fail OPEN on its own error.
    if a.check:
        try:
            return cmd_check(a.check)
        except Exception as e:
            print(f"[sessionstart-boundedness] non-fatal: {e}", file=sys.stderr)
            return 0

    # --all / --selftest are gates: fail LOUD (exit 2) on an internal error so a
    # broken detector can never silently pass CI.
    try:
        if a.selftest:
            return cmd_selftest()
        if a.settings:
            return cmd_settings(Path(a.settings), a.porcelain)
        if a.all:
            dirs = [a.hooks_dir] if isinstance(a.hooks_dir, str) else list(a.hooks_dir)
            return cmd_all(Path(a.hooks_json), [Path(d) for d in dirs], a.json)
    except Exception as e:
        print(f"[sessionstart-boundedness] FATAL: {e}", file=sys.stderr)
        return 2
    ap.print_help()
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
