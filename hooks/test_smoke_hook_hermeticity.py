#!/usr/bin/env python3
"""A smoke-test invocation must pin every PINNABLE HOME-rooted path its hook reads.

WHY
---
scripts/post-install-smoke-test.sh drives real hooks. A hook that resolves
runtime state under Path.home() reads AND WRITES the operator's real ~/.claude
unless the invocation pins it. When that state decides what the hook RENDERS, the
check's verdict stops being a property of the code under test.

Measured 2026-09-01 on the dev-hub-refresh check. It pinned DEV_HUB_REFRESH_STATE
(the fixture) and left three siblings unpinned. Three identical runs, zero code
change, gave PASS / FAIL / FAIL: the first found no prior hash so the condenser in
_lib/standing_report returned the full render and WROTE that hash; the rest
matched it, condensed, and the per-offender line the assertion looks for was gone.
It also wrote a synthetic hash into the operator's live
~/.claude/.standing-reports/dev-hub-refresh.json. Same shape as #539, one
indirection out -- #539 fixed the library's own suite, and the shell smoke test
kept the hole because nothing checked the class.

TWO KINDS OF HOME-ROOTED PATH, AND THE NAME MATTERS
---------------------------------------------------
PINNABLE   gated by an env override (`os.environ.get("X") or Path.home()/...`).
           An invocation that omits X is a defect this file FAILS on.
UNPINNABLE no override exists, so no invocation can redirect it. Reported against
           a RATCHET: the ones that ship today are listed with a reason, and a NEW
           one fails. Silence here would be dishonest -- an empty override set for
           a hook that touches Path.home() at all must not read the same as a
           genuinely clean one.

The first version of this file had only the PINNABLE half and titled itself "every
HOME-rooted state path", which is the name-broader-than-scope defect it exists to
catch. Adversarial review, 2026-09-01: the smoke test writes
.closing-signal-smoke.json into the operator's live ~/.claude on every run via
detect-closing-signal.py's bare `Path.home()`, and this guard called that
invocation hermetic. Confirmed by re-running the smoke test's own fixture with a
sandboxed HOME.

Three legs, in the spirit of hooks/test_scrub_log_location.py:
  BEHAVIOUR  the analyzer bites on the verbatim pre-fix invocation      [BLOCKS]
  CLASS      every smoke-driven script pins every pinnable override      [BLOCKS]
  RATCHET    no NEW unpinnable HOME-rooted path is reached              [REPORTS]

The severity split is deliberate and was learned the hard way. A missing PIN is
fixed in the invocation this suite owns, so it blocks. An UNPINNABLE path is
fixed in the hook that owns it, so blocking makes any teammate's edit to a
shared sibling module red an unrelated PR: on 2026-09-01 the ratchet ejected
this very change from the merge queue over `_lib/worktree_safety.py`, which
PR #641 had just grown by 219 lines and which this change never touched. The
finding was correct; the severity was not. A gate that fails for reasons its
author cannot act on is the one people learn to bypass.

Sandboxing HOME is NOT an accepted remedy and this file does not accept it: on
Windows, ntpath.expanduser() reads USERPROFILE and ignores HOME (MYC-3536), so
Path.home() walks straight back out to the real profile. HOME sandboxing is used
BELOW only as a measurement instrument, never as the fix.

DELIBERATE OVER-APPROXIMATION (pinnable half)
---------------------------------------------
Detection counts overrides bound anywhere in the script's own module or in any
sibling module under hooks/ that it imports, without proving the smoke test's
particular code path reaches each one. The directions are not symmetric: a MISSED
override leaves a real non-hermeticity that surfaces as an unreproducible flake,
while an EXTRA one costs a single harmless env var on an invocation line.

Run: python3 hooks/test_smoke_hook_hermeticity.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
REPO = HOOKS.parent
SMOKE = REPO / "scripts" / "post-install-smoke-test.sh"

failures: list[str] = []
# Files the analyzer could not parse. An empty override set from an unreadable
# file is indistinguishable from a genuinely clean one, so these are reported
# rather than banked as coverage.
unparsed: set[str] = set()


notes: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def note(cond: bool, msg: str) -> None:
    """Report without failing.

    For findings whose REMEDY LIVES IN ANOTHER FILE. Blocking on those means any
    teammate editing a shared sibling module turns an unrelated PR red, and a
    gate that fails for reasons the author cannot act on is the one people learn
    to bypass -- see the repo's own over-strict-verification rule.
    """
    if not cond:
        notes.append(msg)


# ---------------------------------------------------------------------------
# RATCHET: HOME-rooted paths in smoke-driven scripts that no env var can redirect.
# Each entry is "<repo-relative path>::<attribute or call>" with the reason it is
# tolerated. Adding a line here is a deliberate act; a NEW unpinnable path that is
# not listed fails this suite. Shrink this list, never grow it casually.
# ---------------------------------------------------------------------------
UNPINNABLE_ALLOWLIST: dict[str, str] = {
    # Every entry below was READ at the cited line, not inferred from a name.
    "hooks/detect-closing-signal.py": (
        "write_closing_marker() at :781 resolves `home = Path.home()` with no "
        "override, so the smoke test's 'bye' probe writes "
        "~/.claude/.closing-signal-smoke.json into a LIVE ~/.claude on every run "
        "(confirmed under a sandboxed HOME with the smoke test's own fixture, "
        "2026-09-01). The marker is consumed by scripts/session-end-hook.sh, so a "
        "smoke run leaves an orphan close-marker. The only real fix is an override "
        "in the hook; this entry exists so that stays visible."
    ),
    "hooks/warn-stale-dev-checkout.py": (
        ":66 SEEN_DIR is HOME-rooted with no override. Unreached by the smoke probe "
        "today -- the /tmp path short-circuits first, measured 0 files written -- "
        "but it is not pinnable and must not read as clean."
    ),
    "hooks/context-budget-measure.py": (
        ":96-97 BASELINE_PATH / LASTWARN_PATH are HOME-rooted with no override; "
        "driven via --self-test."
    ),
    "hooks/_lib/dev_repo_scan.py": (
        ":56 CONFIG_PATH and :139 DEV_ROOT are HOME-rooted with no override, reached "
        "transitively. DEV_DRIFT_STATE, DEV_DRIFT_FETCH_STATE and LS_SWEEP_TOOL in "
        "the same module ARE pinnable and the CLASS leg enforces them."
    ),
    "hooks/_lib/worktree_safety.py": (
        "several bare `Path.home()` sites (the ~/.claude default at :394, a Google "
        "Drive preference probe, and two home-prefix string comparisons), none with "
        "an override. Reached transitively from the surfacer. Grew by 219 lines in "
        "PR #641; it is pre-existing debt in a module this suite does not own, "
        "which is exactly why the ratchet REPORTS rather than blocks."
    ),
    "hooks/_lib/vault_root.py": (
        "resolves VAULT_ROOT through several branches with HOME fallbacks; the smoke "
        "test pins VAULT_ROOT explicitly where it matters."
    ),
    "hooks/_lib/claude_project_key.py": (
        ":79 `(home or Path.home()) / '.claude' / 'projects'` -- the fallback takes a "
        "caller-supplied home, with no env override. Read-only key derivation."
    ),
    "hooks/lint-vault-frontmatter.py": (
        ":116 Path.home()/'.claude'/'skills'/... is a read-only lookup for the "
        "validator script."
    ),
    "scripts/heal-journal-guard.py": (
        ":205/:207 root scan and :381 reading ~/.claude/settings.json, all "
        "HOME-rooted with no override. This one genuinely READS the operator's live "
        "settings during a smoke run."
    ),
    "scripts/vault-schema-validator.py": (
        ":65 Path.home()/'.claude'/'skills'/.../'templates'/'schemas' is a read-only "
        "schema lookup."
    ),
}


# ---------------------------------------------------------------------------
# Import resolution: ANY sibling module under hooks/, in every spelling that ships.
#
# The first version filtered on `module.startswith("_lib")`, which is blind to the
# bare-sibling shape TWO smoke-driven hooks use today:
#     sys.path.insert(0, str(Path(__file__).resolve().parent / "_lib"))
#     from guard_telemetry import log_fire
# Adversarial review proved the consequence: drop a real pin AND switch the import
# to that prevailing idiom, and the guard returns "hermetic (0 failures)". A guard
# defeated by a one-line notation change is the defect it exists to catch, one
# layer up.
# ---------------------------------------------------------------------------
def _module_candidates(mod: str, level: int, owner: Path) -> list[Path]:
    """Every on-disk file `mod` could name, from `owner`'s perspective."""
    rel = Path(mod.replace(".", "/")) if mod else Path()
    roots: list[Path] = []
    if level:  # relative: from .x import y  /  from ..x import y
        base = owner.parent
        for _ in range(level - 1):
            base = base.parent
        roots.append(base)
    else:
        # absolute-looking, but these run with hooks/ and hooks/_lib on sys.path
        roots += [HOOKS, HOOKS / "_lib", owner.parent]
    out: list[Path] = []
    for root in roots:
        cand = root / rel
        out += [cand.with_suffix(".py"), cand / "__init__.py"]
    return out


def _imported_files(tree: ast.AST, owner: Path) -> list[Path]:
    found: list[Path] = []
    for node in ast.walk(tree):
        specs: list[tuple[str, int]] = []
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            specs.append((base, node.level or 0))
            # `from _lib import standing_report as sr` -> _lib.standing_report
            for a in node.names:
                if a.name != "*":
                    specs.append((f"{base}.{a.name}" if base else a.name,
                                  node.level or 0))
        elif isinstance(node, ast.Import):
            specs += [(a.name, 0) for a in node.names]
        for mod, level in specs:
            for cand in _module_candidates(mod, level, owner):
                try:
                    inside = cand.resolve().is_relative_to(HOOKS)
                except (OSError, ValueError):
                    inside = False
                if inside and cand.is_file():
                    found.append(cand)
    return found


# ---------------------------------------------------------------------------
# Override + HOME detection
# ---------------------------------------------------------------------------
def _string_consts(tree: ast.AST) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.value.value
    return out


def _env_read(node: ast.AST) -> ast.Call | None:
    """`os.environ.get(...)` / `environ.get(...)` / `os.getenv(...)`."""
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr in ("get", "getenv"):
        src = ast.unparse(f)
        if "environ" in src or "getenv" in src:
            return node
    if isinstance(f, ast.Name) and f.id == "getenv":
        return node
    return None


def _expands_a_tilde(sub: ast.AST) -> bool:
    """`expanduser("~/...")` on a literal tilde, in either spelling."""
    if not isinstance(sub, ast.Call):
        return False
    f = sub.func
    named = (isinstance(f, ast.Attribute) and f.attr == "expanduser") or \
            (isinstance(f, ast.Name) and f.id == "expanduser")
    if not named:
        return False
    for a in sub.args:
        if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                and a.value.startswith("~"):
            return True
    if isinstance(f, ast.Attribute):        # Path("~/...").expanduser()
        for c in ast.walk(f.value):
            if isinstance(c, ast.Constant) and isinstance(c.value, str) \
                    and c.value.startswith("~"):
                return True
    return False


def _home_nodes(node: ast.AST) -> list[ast.AST]:
    """Every Path.home() / expanduser(...) / environ['HOME'] in this subtree."""
    hits: list[ast.AST] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                and sub.func.attr == "home":
            hits.append(sub)
        elif _expands_a_tilde(sub):
            # expanduser() counts ONLY on a literal "~..." argument. Bare
            # `Path(env_value).expanduser()` normalises a path the CALLER
            # supplied -- that is not a HOME-rooted default, and counting it made
            # this guard demand a pin for ABS_DEV_ROOT, whose default is "".
            hits.append(sub)
        elif isinstance(sub, ast.Subscript):
            try:
                if isinstance(sub.slice, ast.Constant) and sub.slice.value == "HOME":
                    hits.append(sub)
            except AttributeError:
                pass
    return hits


def _mentions_home(node: ast.AST) -> bool:
    return bool(_home_nodes(node))


def _env_name(call: ast.Call, consts: dict[str, str]) -> str | None:
    if not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name) and arg.id in consts:
        return consts[arg.id]
    return None


def analyze(py: Path, _seen: set[Path] | None = None
            ) -> tuple[set[str], set[str]]:
    """(pinnable env overrides, repo-relative files with an UNPINNABLE home path)."""
    _seen = _seen if _seen is not None else set()
    py = py.resolve()
    if py in _seen or not py.is_file():
        return set(), set()
    _seen.add(py)
    # errors="replace" so the read CANNOT raise. UnicodeDecodeError subclasses
    # ValueError, not OSError, so a strict read would escape the handler below and
    # this guard would fail open on exactly the cp1252 files it should be loudest
    # about (the #313 class; scripts/audit-decode-safe-reads.py enforces).
    try:
        tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        unparsed.add(str(py))
        return set(), set()

    consts = _string_consts(tree)
    overrides: set[str] = set()
    gated: set[int] = set()          # id() of home nodes covered by an override

    def _record(call: ast.Call, home_scope: ast.AST) -> None:
        name = _env_name(call, consts)
        if name:
            overrides.add(name)
            for h in _home_nodes(home_scope):
                gated.add(id(h))

    for node in ast.walk(tree):
        # `os.environ.get(X) or (Path.home() / ...)`
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            if any(_mentions_home(v) for v in node.values):
                for v in node.values:
                    call = _env_read(v)
                    if call is not None:
                        _record(call, node)
        # `os.environ.get(X, Path.home() / ...)` and os.getenv(X, default)
        call = _env_read(node)
        if call is not None and len(call.args) >= 2 and _mentions_home(call.args[1]):
            _record(call, call)
        # `a if os.environ.get(X) else Path.home()/...`
        if isinstance(node, ast.IfExp) and _mentions_home(node):
            for sub in ast.walk(node.test):
                c = _env_read(sub)
                if c is not None:
                    _record(c, node)

    # Scope deliberately stops at the SIMPLE statement -- Assign/Return/Expr, never
    # a compound one. ast.FunctionDef and ast.If are THEMSELVES ast.stmt, so a
    # plain isinstance(node, ast.stmt) test still swallows an entire function body
    # and is the function-wide rule wearing a different name. A function-wide rule
    # first and is wrong in a way that matters: it pairs a HOME path with every
    # unrelated env read in the same body, so it demanded the smoke test pin
    # HEAL_JOURNAL_GUARD_BYPASS (a bypass flag), LS_SWEEP_TOOL and ABS_DEV_ROOT.
    # A guard that reports non-findings teaches people to skip it, and then it is
    # worth less than no guard. Precision here costs the two-statement
    # early-return shape -- and costs it SAFELY, because an override this misses
    # leaves its HOME node ungated, so the module surfaces in the RATCHET leg
    # below instead of going silent.
    _SIMPLE = (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Return, ast.Expr)
    for stmt in ast.walk(tree):
        if not isinstance(stmt, _SIMPLE):
            continue
        homes = _home_nodes(stmt)
        if not homes:
            continue
        for sub in ast.walk(stmt):
            c = _env_read(sub)
            if c is not None:
                name = _env_name(c, consts)
                if name:
                    overrides.add(name)
                    for h in homes:
                        gated.add(id(h))

    try:
        rel = str(py.relative_to(REPO))
    except ValueError:
        rel = str(py)
    unpinnable = {rel} if any(id(h) not in gated for h in _home_nodes(tree)) else set()

    for dep in _imported_files(tree, py):
        o, u = analyze(dep, _seen)
        overrides |= o
        unpinnable |= u
    return overrides, unpinnable


# ---------------------------------------------------------------------------
# Which scripts does the smoke test invoke, and with what command line?
# ---------------------------------------------------------------------------
# hooks/ AND scripts/: the smoke test drives heal-journal-guard.py,
# vault-schema-validator.py and test-closing-signals.py the same way, and one of
# those is a confirmed writer into the operator's real ~/.claude.
_REF = re.compile(r'\$\{?SKILL_DIR\}?/((?:hooks|scripts)/[A-Za-z0-9_.-]+\.py)')
# Assignment: quoted, single-quoted or bare, with an optional trailing comment.
_ASSIGN = re.compile(
    r'^\s*([A-Za-z_][A-Za-z0-9_]*)=(["\']?)'
    + _REF.pattern + r'\2\s*(?:#.*)?$')


def _strip_comment(s: str) -> str:
    """Drop a trailing # comment that is not inside quotes."""
    out, q = [], None
    for ch in s:
        if q:
            out.append(ch)
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out)


def _logical_lines(text: str) -> list[str]:
    """Join backslash continuations the way bash does.

    A line ending in an EVEN number of backslashes is not a continuation (the
    last one is escaped), and a comment line never continues. Getting this wrong
    silently drops a var->script mapping, which is the fail-open direction.
    """
    out: list[str] = []
    buf = ""
    for raw in text.splitlines():
        s = raw.rstrip("\r\n")
        stripped = s.rstrip()
        trailing = len(stripped) - len(stripped.rstrip("\\"))
        is_cont = (
            trailing % 2 == 1
            and s.endswith("\\")                     # no space after the backslash
            and not stripped.lstrip().startswith("#")
        )
        if is_cont:
            buf += stripped[:-1] + " "
            continue
        out.append(buf + s)
        buf = ""
    if buf:
        out.append(buf)
    return out


def smoke_invocations(text: str) -> list[tuple[str, str]]:
    """[(repo-relative script, the python3 command that runs it)]."""
    lines = _logical_lines(text)
    var_to: dict[str, set[str]] = {}
    for ln in lines:
        m = _ASSIGN.match(ln)
        if m:
            var_to.setdefault(m.group(1), set()).add(m.group(3))

    found: list[tuple[str, str]] = []
    for ln in lines:
        if "python3" not in ln:
            continue
        cmd = _strip_comment(ln)
        hits = set(_REF.findall(cmd))
        for var, targets in var_to.items():
            # quoted, braced and BARE uses all reach the interpreter identically
            if re.search(r'"\$' + var + r'"|\$\{' + var + r'\}|\$' + var + r'\b', cmd):
                # A var reassigned to several scripts is ambiguous; check ALL of
                # them rather than silently attributing the invocation to the last.
                hits |= targets
        found += [(h, cmd) for h in sorted(hits)]
    return found


def _pins(var: str, cmd: str) -> bool:
    """Is `var` set as a command-prefix assignment (not merely mentioned)?"""
    return re.search(r'(?<![A-Za-z0-9_])' + re.escape(var) + r'=', cmd) is not None


# --- BEHAVIOUR: the analyzer bites on its own incident ---------------------
_probe = HOOKS / "_lib" / "standing_report.py"
if _probe.is_file():
    check("STANDING_REPORT_STATE_DIR" in analyze(_probe)[0],
          "analyzer missed STANDING_REPORT_STATE_DIR in _lib/standing_report.py "
          "(constant-named env override) -- every CLASS result below is vacuous")

_surfacer = HOOKS / "dev-hub-refresh-on-session-start.py"
if _surfacer.is_file():
    got = analyze(_surfacer)[0]
    # All four the fix pins, named explicitly. The first version omitted
    # DEV_DRIFT_FETCH_STATE here, and that was the one pin no leg asserted by
    # name -- so losing it was invisible to every control in the file.
    for want in ("DEV_HUB_REFRESH_STATE", "DEV_DRIFT_STATE",
                 "STANDING_REPORT_STATE_DIR", "DEV_DRIFT_FETCH_STATE"):
        check(want in got,
              f"analyzer missed {want} on dev-hub-refresh-on-session-start.py "
              f"(saw {sorted(got)}) -- transitive resolution is broken")

# The bare-sibling import shape, which defeated the first version of this guard.
_stale = HOOKS / "warn-stale-dev-checkout.py"
if _stale.is_file() and (HOOKS / "_lib" / "guard_telemetry.py").is_file():
    check("GUARD_FIRES_LOG" in analyze(_stale)[0],
          "analyzer missed GUARD_FIRES_LOG on warn-stale-dev-checkout.py -- it is "
          "reached through `from guard_telemetry import log_fire` after a sys.path "
          "insert, and bound with a 2-arg default. Both shapes must resolve or a "
          "one-line import change silently defeats this guard")

# Negative control: the pre-fix invocation, VERBATIM, must be flagged. No
# hand-typed "good" twin -- a literal spelling of the fixed command would drift
# out of step with the real file and start asserting against itself. The CLASS
# leg over the actual smoke test is the positive control that cannot go stale.
_BAD = ('resp=$(echo \'{}\' | DEV_HUB_REFRESH_STATE="$STATE_F" '
        'python3 "$SURFACER" 2>/dev/null)')
if _surfacer.is_file():
    need = analyze(_surfacer)[0]
    missed = sorted(v for v in need if not _pins(v, _BAD))
    check(len(missed) >= 3,
          "negative control weakened: the pre-fix unpinned invocation should be "
          f"missing at least 3 overrides, saw {missed}. A control that bites only "
          "on TOTAL blindness passes straight through the partial blindness a "
          "single missed import actually produces")

# --- CLASS + RATCHET ------------------------------------------------------
if not SMOKE.is_file():
    failures.append(f"smoke test not found at {SMOKE}")
else:
    text = SMOKE.read_text(encoding="utf-8", errors="replace")
    invocations = smoke_invocations(text)
    check(bool(invocations),
          "found NO script invocations in the smoke test -- the parser is broken, "
          "not the smoke test clean")

    seen_unpinnable: dict[str, str] = {}
    for script, cmd in invocations:
        need, unpin = analyze(REPO / script)
        missing = sorted(v for v in need if not _pins(v, cmd))
        check(not missing,
              f"{script}: smoke-test invocation does not pin {', '.join(missing)} "
              f"-- it will read and WRITE the operator's real ~/.claude, and its "
              f"verdict will turn on state no fixture controls")
        for u in unpin:
            seen_unpinnable[u] = script

    # REPORTED, not failed. This fires on a HOME-rooted path with no override,
    # which is fixed in the hook that owns it -- never in the invocation this
    # suite is about. Measured 2026-09-01: it blocked in the merge_group context
    # on `_lib/worktree_safety.py` after PR #641 added 219 lines to that shared
    # module, and the merge queue ejected a PR that had not touched it. The
    # finding was CORRECT and the severity was WRONG.
    new_unpinnable = sorted(set(seen_unpinnable) - set(UNPINNABLE_ALLOWLIST))
    note(not new_unpinnable,
          "NEW unpinnable HOME-rooted path(s) reached by a smoke-driven script: "
          + "; ".join(f"{u} (via {seen_unpinnable[u]})" for u in new_unpinnable)
          + ". No env override can redirect these, so no invocation can make the "
            "check hermetic -- add the override to the script itself, or record it "
            "in UNPINNABLE_ALLOWLIST with the reason")

    # Also reported: an entry stops being reachable when someone else's import
    # change reshapes the graph, which is not this PR's defect either.
    stale_allow = sorted(set(UNPINNABLE_ALLOWLIST) - set(seen_unpinnable))
    note(not stale_allow,
          "UNPINNABLE_ALLOWLIST entries no longer reachable (the ratchet must "
          f"shrink, not accumulate): {', '.join(stale_allow)}")

    check(not unparsed,
          "these files did not parse, so their overrides were never checked and "
          "the clean verdict above does not cover them: "
          f"{', '.join(sorted(unparsed))}")

for n in notes:
    print(f"  NOTE {n}")
for f in failures:
    print(f"  FAIL {f}")
tail = f" ({len(notes)} note(s), not blocking)" if notes else ""
print(f"{'FAIL' if failures else 'ok'}  smoke-test script invocations are hermetic "
      f"({len(failures)} failure(s)){tail}")
sys.exit(1 if failures else 0)
