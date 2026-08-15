#!/usr/bin/env python3
"""
sessionstart-hook-snapshot-guard.py - warn if a SessionStart hook DISAPPEARED.

A SessionStart hook can silently vanish from settings.json - a linter, a manual
edit, or a parallel session can prune one, and the loss stays invisible until you
happen to notice the missing behavior. This guard snapshots which SessionStart
hooks are wired and warns inline when one disappears.

De-noised: the original diffed exact command STRINGS, so any cosmetic reword by a
concurrent session - `python3` <-> `/usr/bin/python3`, `~/` <-> absolute path,
adding/removing a `2>/dev/null || echo {...}` wrapper or a `[ -f X ] &&` guard -
false-flagged a hook as "missing" when the SAME SCRIPT was still wired. Cry-wolf
is how the ONE real drop gets scrolled past, so the guard now diffs SCRIPT
IDENTITY (script basename + normalized args) instead of raw command text.

WINDOWS: the identity function used to collapse the whole fleet into ONE identity
(MYC-3880). _script_identity() looked for a script path with `([~/][^\\s'\"]*\\.py)`
- rooted at `~` or `/` only. The Windows leg of the installer
(scripts/install-hooks-user-level.py) rewrites every hook into a launcher shim:

    py -3 "C:\\...\\scripts\\hook_runner.py" --fallback silent "C:\\...\\hooks\\<hook>.py"

which contains no `~` and no `/` at all (drive letter, all backslashes). So the
regex never matched, every Windows command fell through to the `c[:100]` trimmed
-text branch, and that 100-char prefix is the launcher preamble - byte-identical
for every hook. Measured on a real Windows install: 25 wired SessionStart
commands -> 7 identities instead of 20, with all 19 Windows-form commands sharing
ONE identity, 19/19 via the text branch. Any one of those 19 could be deleted
from settings.json and this guard stayed quiet, because the surviving siblings
kept the shared identity alive; it could only ever fire if all 19 vanished at
once. The guard's entire purpose was defeated on Windows, while reading healthy.
It was also unstable there: the collapsed identity embedded an absolute path, so
moving the clone invalidated the whole snapshot in one step.

Fix, same idiom as scripts/audit-sessionstart-boundedness.py (MYC-3879) and
scripts/heal-journal-guard.py (#375):
  - _SCRIPT_TOKEN_RE parses quoted paths first (a Windows home MAY contain a
    space) and bare paths last, rooted at `~`, POSIX `/`, a UNC `\\\\share` or a
    drive letter.
  - _RUNNER_SHIM_RE knows hook_runner.py is a LAUNCHER, not a hook, and resolves
    through it to the TARGET it launches. Resolving to the shim would be no
    better than the bug: the launcher is identical for all 19 hooks.
  - the basename is taken with PureWindowsPath, which splits BOTH separators on
    BOTH platforms, so an identity does not depend on which OS computes it.

Same SILENT-NO-OP family as #370/#375/#485 and MYC-3875/3876/3879: a guard that
reads healthy while asleep.

Behavior:
  - Snapshot SessionStart hook IDENTITIES to
    ~/.claude/state/sessionstart-hooks-snapshot.json (versioned: {"v":3,...}).
  - On each fire, diff current identities against the snapshot.
  - Any prior identity MISSING from current -> inline warning (snapshot NOT
    updated, so the warning persists until reconciled).
  - New identities only -> update snapshot silently (additions are fine).
  - First run / corrupt / OLDER-VERSION snapshot -> (re)baseline silently.
  - `--refresh` -> force-rewrite the snapshot to current and exit 0 (this is what
    "if intentional, refresh" means; the old in-band rerun never refreshed
    because the missing-branch skips the update).
  - `--self-test` -> hermetic controls for the identity function, exit 0/1.
    Touches neither settings.json nor the snapshot.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path, PureWindowsPath

SETTINGS = Path.home() / ".claude" / "settings.json"
STATE = Path.home() / ".claude" / "state" / "sessionstart-hooks-snapshot.json"

# Bumped 2 -> 3 with the Windows shim fix. A v2 snapshot's identities were
# computed by the OLD function, so they are not comparable to v3 ones and MUST
# NOT be re-normalized either: a v2 entry is already an identity, and the
# collapsed Windows one ('py -3 "C:\\...hook_runner.py" --fallback silent "C:\\')
# would re-normalize to garbage that matches nothing current - handing every
# Windows user a spurious "19 hooks missing" warning on the first run after the
# fix, which is the exact cry-wolf this guard exists to avoid. So an older
# version re-baselines silently (see _normalize_prior). The one-time cost is that
# a REAL drop straddling the upgrade is absorbed; that is strictly cheaper than a
# fleet-wide false alarm, and the next drop after the re-baseline still warns.
SNAPSHOT_V = 3

# Every script path a command names, quoted or bare, in command order. THREE path
# forms have to parse, because the Windows leg of the installer rewrites hooks
# into a launcher shim (see the module docstring):
#
#   POSIX bare      python3 ~/.claude/hooks/x.py 2>/dev/null || echo '{...}'
#   POSIX guarded   [ -f ~/.claude/hooks/x.sh ] && bash ~/.claude/hooks/x.sh
#   Windows shim    py -3 "C:\...\scripts\hook_runner.py" --fallback silent "C:\...\hooks\x.py"
#
# Kept structurally parallel to _SCRIPT_TOKEN_RE in
# scripts/audit-sessionstart-boundedness.py and _GUARD_PATH_RE in
# scripts/heal-journal-guard.py, so the three resolvers cannot drift: quoted
# alternatives first (a Windows home MAY contain a space), bare last (an unquoted
# path ends at whitespace or a shell operator). Roots accepted: `~`, POSIX `/`, a
# UNC `\\share`, a drive letter. The lookbehind means a BARE path may only start
# where a token can, else `./hooks/x.py` would match at its inner `/`.
_SCRIPT_TOKEN_RE = re.compile(
    r"\"([^\"\n]+\.(?:py|sh|bash))\""
    r"|'([^'\n]+\.(?:py|sh|bash))'"
    r"|(?<![^\s'\"|&;(=])((?:~|/|\\\\|[A-Za-z]:[\\/])[^\s'\"|&;]*\.(?:py|sh|bash))"
)
# hook_runner.py is the Windows installer's LAUNCHER, never a hook itself. It
# re-execs the target with the payload on stdin, so a command that goes through
# it identifies as its TARGET - the first script path AFTER the shim, matching
# hook_runner's own argv handling (`[--fallback MODE] <target> [extra...]`).
# Case-insensitive because Windows paths are.
_RUNNER_SHIM_RE = re.compile(r"(?:^|[\\/])hook_runner\.py$", re.IGNORECASE)

_LABEL_MAX = 120  # display only - identities themselves are never truncated


def _script_identity(cmd: str) -> str:
    """Normalize a hook command to a stable identity: 'basename' or
    'basename||args'. Collapses python-path / tilde-vs-absolute / [ -f X ] &&
    guard / 2>redirect / || fallback / Windows-launcher-shim variants of the same
    script.

    A command that names no script at all keeps its FULL text as its identity.
    That branch used to trim to `c[:100]`, which is a collision generator by
    construction and is precisely how the 19 Windows hooks fused into one: any
    two commands sharing a 100-char prefix become the same hook to this guard.
    Truncation belongs in the display path (_label), never in the key."""
    c = (cmd or "").strip()
    c = re.sub(r"^\[\s*-[fe]\s+[^\]]*\]\s*&&\s*", "", c)   # leading [ -f X ] && guard
    c = re.split(r"\s*\|\|\s*", c)[0]                       # trailing || true / || echo {...}
    c = re.sub(r"\s*2>\S+", "", c).strip()                  # 2>/dev/null

    # (token, end-offset). The offset is the match end, not find()+len(token), so
    # a QUOTED path's args start after the closing quote instead of at it.
    toks = [(next(g for g in m.groups() if g), m.end())
            for m in _SCRIPT_TOKEN_RE.finditer(c)]
    if not toks:
        return c                                            # non-script command: full text

    i = 0
    shim = next((k for k, (t, _) in enumerate(toks) if _RUNNER_SHIM_RE.search(t)), None)
    if shim is not None:
        target = next((k for k in range(shim + 1, len(toks))
                       if not _RUNNER_SHIM_RE.search(toks[k][0])), None)
        # No target at all -> the shim IS what runs; identify it as itself.
        i = target if target is not None else shim

    path, end = toks[i]
    after = c[end:].strip()
    # PureWindowsPath, not Path: it splits on BOTH `/` and `\` on BOTH platforms.
    # Plain Path() on POSIX would return the entire `C:\...\x.py` string as the
    # "basename", so the identity of a Windows command would depend on which OS
    # computed it - and the controls below would pass on Windows and fail in CI.
    base = PureWindowsPath(path).name
    return f"{base}||{after}" if after else base


def extract_sessionstart_commands(settings_path: Path) -> list[str]:
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[str] = []
    for block in (data.get("hooks", {}).get("SessionStart") or []):
        for hook in (block.get("hooks") or []):
            cmd = (hook.get("command") or "").strip()
            if cmd:
                out.append(cmd)
    return out


def _current_identities() -> set[str]:
    return {_script_identity(c) for c in extract_sessionstart_commands(SETTINGS)}


def _normalize_prior(raw: object) -> set[str] | None:
    """Prior identities from a parsed snapshot, or None to (re)baseline.

    Split out of _load_prior so the version rules are testable without touching
    ~/.claude/state. Three cases:
      - {"v": SNAPSHOT_V, ...} -> identities verbatim; re-normalizing them would
        be lossy (they are already identities, not commands).
      - a bare list          -> the pre-versioning format, RAW command strings;
        normalizing those through the current function is exact.
      - anything else, incl. an OLDER {"v": n} -> None. See SNAPSHOT_V."""
    if isinstance(raw, dict):
        if raw.get("v") == SNAPSHOT_V:
            return set(raw.get("identities", []))
        return None
    if isinstance(raw, list):
        return {_script_identity(c) for c in raw}
    return None


def _load_prior() -> set[str] | None:
    """Prior identities, or None if missing/corrupt/older-version."""
    try:
        raw = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return None
    return _normalize_prior(raw)


def _save(identities: set[str]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"v": SNAPSHOT_V, "identities": sorted(identities)},
                                indent=2),
                     encoding="utf-8")


def _label(identity: str) -> str:
    bn, _, args = identity.partition("||")
    text = f"{bn} {args}".strip()
    return text if len(text) <= _LABEL_MAX else text[:_LABEL_MAX - 1] + "\u2026"


# --------------------------------------------------------------------------- self-test
# Hermetic fixtures: EXACTLY the shape install-hooks-user-level.py emits on
# Windows -- " ".join([*launcher, f'"{runner}"', "--fallback", fb, f'"{target}"'])
# -- with absolute paths that are never this machine's home, so the controls
# assert the same thing on Windows and in POSIX CI.
_FX_RUNNER = r"C:\Users\dev\.claude\skills\ai-brain-starter\scripts\hook_runner.py"
_FX_HOOKS = r"C:\Users\dev\.claude\skills\ai-brain-starter\hooks"
_FX_HOOKS_SPACED = r"C:\Users\dev user\.claude\skills\ai-brain-starter\hooks"


def _win_cmd(basename: str, fb: str = "silent", launcher: str = "py -3",
             hooks_dir: str = _FX_HOOKS, extra: str = "") -> str:
    """One command in the installer's Windows launcher-shim form."""
    cmd = f'{launcher} "{_FX_RUNNER}" --fallback {fb} "{hooks_dir}\\{basename}"'
    return f"{cmd} {extra}" if extra else cmd


# The 19 hooks that shared ONE identity on the measured install. Real names, so a
# rename that removes them from the fleet shows up as a diff here too.
_FX_FLEET = [
    "session-start-context.py", "inject-instinct-context.py", "heal-journal-guard.py",
    "lint-claude-settings.py", "context-budget-measure.py", "enforce-worktree-cap.py",
    "first-week-checkin.py", "migrate-to-user-level.py", "relocate-watch-surface.py",
    "remediate-runaway-procs.py", "dev-hub-refresh-on-session-start.py",
    "surface-backup-status.py", "surface-connector-liveness.py",
    "surface-deployed-hooks-behind.py", "surface-orphan-claude-branches.py",
    "surface-orphan-worktree-snapshots.py", "surface-stranded-session-artifacts.py",
    "warn-vault-session-in-worktree.py", "worktree-footprint-signal.py",
]

_FX_POSIX_CHAIN = ("python3 ~/.claude/skills/ai-brain-starter/hooks/surface-backup-status.py "
                   "2>/dev/null || echo '{\"continue\":true,\"suppressOutput\":true}'")
_FX_POSIX_GUARD = ("[ -f ~/.claude/hooks/check-claude-code-version.sh ] && "
                   "bash ~/.claude/hooks/check-claude-code-version.sh || true")


def _self_test() -> int:
    """Controls for _script_identity() + the snapshot version rules.

    Every case is an EQUALITY assertion, so an identity function that resolves
    NOTHING cannot pass one by accident -- that is exactly the shape of the bug
    being fixed, where 19 wired Windows commands all answered with the same
    launcher preamble while the guard reported healthy."""
    fails: list[str] = []

    # -- premise guards: fixtures that rot into proving nothing must fail LOUD --
    if not _FX_RUNNER.endswith("hook_runner.py") or _FX_HOOKS in _FX_RUNNER:
        fails.append("premise: the shim fixture must name hook_runner.py OUTSIDE the "
                     "hooks dir, else 'resolves to the target, not the launcher' is vacuous")
    if len(set(_FX_FLEET)) != 19:
        fails.append(f"premise: the fleet fixture must be 19 DISTINCT hooks, got "
                     f"{len(set(_FX_FLEET))} distinct of {len(_FX_FLEET)}")
    # Parity with the EMITTER. These fixtures only mean something while the
    # Windows installer still wraps hooks in hook_runner.py with --fallback; if it
    # stops, the shim rule is stale and this must fail rather than keep asserting
    # a shape nobody writes. An ABSENT installer means a standalone deployed copy
    # of this hook (~/.claude/hooks/), where the structural guards above still
    # hold -- not a failure.
    installer = (Path(__file__).resolve().parent.parent
                 / "scripts" / "install-hooks-user-level.py")
    if installer.exists():
        try:
            isrc = installer.read_text(encoding="utf-8", errors="replace")
        except Exception:
            isrc = ""
        if isrc and not ("hook_runner" in isrc and "--fallback" in isrc):
            fails.append("premise: install-hooks-user-level.py no longer emits the "
                         "hook_runner `--fallback` launcher form -- the shim rule in "
                         "_script_identity() is stale")

    # -- THE regression: a Windows command identifies as its TARGET hook --------
    cases = [
        ("win shim -> target, not launcher", _win_cmd("session-start-context.py"),
         "session-start-context.py"),
        ("win shim, --fallback allow", _win_cmd("lint-claude-settings.py", fb="allow"),
         "lint-claude-settings.py"),
        ("win shim, space in home", _win_cmd("heal-journal-guard.py",
                                             hooks_dir=_FX_HOOKS_SPACED),
         "heal-journal-guard.py"),
        # The installer falls back to python/python3 where the `py` launcher is
        # absent -- that form must resolve identically.
        ("win shim, python3 launcher", _win_cmd("first-week-checkin.py",
                                                launcher="python3"),
         "first-week-checkin.py"),
        ("win shim + target args", _win_cmd("lint-claude-settings.py", extra="--test"),
         "lint-claude-settings.py||--test"),
        # No target -> the launcher IS what runs, so it identifies as itself.
        ("win shim, no target", f'py -3 "{_FX_RUNNER}"', "hook_runner.py"),
        ("win bare drive-letter path", r"py -3 C:\Users\dev\.claude\hooks\x.py", "x.py"),
        ("win UNC path", r'py -3 "\\srv\share\.claude\hooks\x.py"', "x.py"),
        # -- POSIX forms that already worked: de-noise regression controls -------
        ("posix tilde + fallback chain", _FX_POSIX_CHAIN, "surface-backup-status.py"),
        ("posix [ -f ] && guard form", _FX_POSIX_GUARD, "check-claude-code-version.sh"),
        ("posix absolute path", "python3 /home/u/.claude/hooks/x.py", "x.py"),
        ("posix absolute + args", "python3 /home/u/.claude/hooks/x.py --flag",
         "x.py||--flag"),
        ("no script named -> full text", "echo hello", "echo hello"),
    ]
    for label, cmd, want in cases:
        got = _script_identity(cmd)
        if got != want:
            fails.append(f"identity[{label}]: wanted {want!r}, got {got!r}  cmd={cmd!r}")

    # -- 19 distinct Windows commands MUST yield 19 distinct identities ---------
    # The pre-fix function yielded ONE for this exact input (measured: 19/19 fell
    # through to the trimmed-text branch and shared the launcher preamble), which
    # is why deleting any single hook could not be detected.
    fleet = [_win_cmd(b) for b in _FX_FLEET]
    got_fleet = {_script_identity(c) for c in fleet}
    if got_fleet != set(_FX_FLEET):
        fails.append(f"fleet: 19 distinct Windows commands must yield the 19 TARGET "
                     f"basenames; got {len(got_fleet)} identities "
                     f"(missing={sorted(set(_FX_FLEET) - got_fleet)[:3]}, "
                     f"extra={sorted(got_fleet - set(_FX_FLEET))[:3]})")
    # The bug's mechanism, asserted directly: dropping ONE hook must change the
    # identity SET. Under the old function it did not, for any of the 19.
    for drop in (0, 9, 18):
        survivors = {_script_identity(c) for i, c in enumerate(fleet) if i != drop}
        if _FX_FLEET[drop] in survivors or len(survivors) != 18:
            fails.append(f"drop-detect[{_FX_FLEET[drop]}]: removing one hook must remove "
                         f"its identity; got {len(survivors)} identities")

    # -- identity keys are never truncated (the collision class) ----------------
    # Two long non-script commands sharing a 100-char prefix must stay distinct.
    pre = "echo " + ("x" * 120)
    if _script_identity(pre + "AAA") == _script_identity(pre + "BBB"):
        fails.append("truncation: commands sharing a long prefix collided -- the "
                     "identity key is being trimmed again")

    # -- cross-platform equivalence: same hook, either wiring, same identity ----
    for base in ("surface-backup-status.py", "heal-journal-guard.py"):
        win = _script_identity(_win_cmd(base))
        posix = _script_identity(f"python3 ~/.claude/hooks/{base} 2>/dev/null || true")
        if win != posix:
            fails.append(f"parity[{base}]: windows={win!r} != posix={posix!r}")

    # -- snapshot version rules -------------------------------------------------
    for label, raw, want in (
        ("current version -> verbatim", {"v": SNAPSHOT_V, "identities": ["a.py"]}, {"a.py"}),
        ("legacy raw list -> normalized",
         ["python3 ~/.claude/hooks/a.py", _win_cmd("b.py")], {"a.py", "b.py"}),
        ("v2 -> re-baseline (None)", {"v": 2, "identities": ["a.py"]}, None),
        ("v1 -> re-baseline (None)", {"v": 1, "identities": ["a.py"]}, None),
        ("unversioned dict -> re-baseline", {"identities": ["a.py"]}, None),
        ("garbage -> re-baseline", "nope", None),
    ):
        got = _normalize_prior(raw)
        if got != want:
            fails.append(f"snapshot[{label}]: wanted {want!r}, got {got!r}")
    # The collapsed v2 Windows entry is the precise reason v2 cannot be
    # re-normalized: it would become an identity nothing current can match.
    collapsed = f'py -3 "{_FX_RUNNER}" --fallback silent "C:\\'
    if _normalize_prior({"v": 2, "identities": [collapsed]}) is not None:
        fails.append("snapshot: a v2 snapshot must re-baseline, never re-normalize -- "
                     "its collapsed Windows entry matches no current identity")

    if fails:
        print("SELF-TEST FAIL:")
        for f in fails:
            print("  - " + f)
        return 1
    print(f"SELF-TEST PASS: identity resolves through the Windows launcher shim to the "
          f"TARGET hook ({len(_FX_FLEET)} distinct commands -> {len(got_fleet)} distinct "
          f"identities), POSIX de-noise forms still collapse, keys are untruncated, and "
          f"a pre-v{SNAPSHOT_V} snapshot re-baselines instead of crying wolf.")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()

    if "--refresh" in sys.argv:
        _save(_current_identities())
        print("[sessionstart-hook-guard] snapshot refreshed to current SessionStart hooks.")
        return 0

    current = _current_identities()
    if not current:
        return 0  # settings malformed or no hooks; don't false-alarm

    prior = _load_prior()
    if prior is None:
        _save(current)                       # baseline / migrate / repair, silent
        return 0

    missing = prior - current
    if missing:
        print(f"[sessionstart-hook-guard] WARNING: {len(missing)} SessionStart hook(s) missing since last snapshot:")
        for ident in sorted(missing):
            print(f"  - {_label(ident)}")
        print("[sessionstart-hook-guard] If intentional: `python3 ~/.claude/hooks/sessionstart-hook-snapshot-guard.py --refresh`. If not: re-add the missing hook(s).")
        # Do NOT update snapshot - leave it so the warning persists until reconciled.
        return 0

    additions = current - prior
    if additions:
        _save(current)                       # additions are fine - absorb silently
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313/#314): an identity can carry a path
    # from a non-ASCII home, and print() would raise before the warning lands.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
