#!/usr/bin/env python3
"""Class watchdog: a Bash-command gate that honors a `*_BYPASS` env var MUST
also consult the COMMAND STRING for that bypass.

WHY THIS EXISTS
----------------
A PreToolUse(Bash) gate runs in the HOOK process, whose environment is the
Claude Code SESSION env. An inline `VAR=1 <cmd>` prefix a user or agent types
lives only in the command STRING and never reaches os.environ. A gate that
advertises an inline bypass in its own block message but reads only
os.environ is offering an escape hatch that can never fire -- a permanent
block with a fake exit, which trains people to disable the guard some other
way instead of using the one it names. Bug class
HOOK-READS-SESSION-ENV-NOT-COMMAND-ENV.

FOUR THINGS THIS FILE PROVES, IN ORDER
----------------------------------------
1. The SCANNER (hooks/_lib/bypass_scan.py) detects BOTH shapes a violation
   can take: the bypass name spelled as a literal string, and the bypass
   name hoisted into a module constant. Only the literal shape was checkable
   until this scanner shipped; a scanner that only saw literals would have
   missed 2 of the 5 real violators this repo shipped (check-py-import-
   precommit.py and session-lock.py both hoist BYPASS_ENV into a constant).
2. The CI WATCHDOG: this repo's own tracked hooks/ dir, scanned for real,
   must come back with zero violators. This is the assertion that fails the
   build if a new PreToolUse(Bash) bypass hook ever ships the broken shape.
3. FLEET PROOF: for each hook this change fixed, the exact real invocation
   (subprocess + stdin JSON, matching how Claude Code actually calls a hook)
   still blocks/warns with no bypass present, and now also allows/quiets
   with the inline `VAR=1 <cmd>` prefix -- never only the session-env form.
4. The SessionStart SURFACER (surface-bypass-unreachable.py) correctly
   reports a violator in a fixture "deployed hooks" directory, stays silent
   on a clean one, and stays silent when the directory does not exist.

check-py-import-precommit.py and session-lock.py are ALSO covered end-to-end
(both bypass forms, against real git-repo fixtures with a live sibling /
staged F821) in tests/integration/test_session_coordination_guards.sh --
extended there instead of duplicated here, since that file already builds
the fixtures both hooks need.

Run: python3 hooks/test_bypass_reachability_watchdog.py   (pure stdlib)
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "_lib"))
from bypass_scan import (  # noqa: E402  (shared with the SessionStart surfacer)
    consults_command,
    reads_bypass_env,
    scan_hooks,
)

# Hooks intentionally exempt from the "must consult command" rule, each with
# a reason. Keep this SMALL -- an exemption is a documented decision, not a
# dodge.
EXEMPT = frozenset({
    # (none today -- every Bash-gate bypass hook in this repo consults the
    #  command as of the change that added this watchdog.)
})

_fails = []


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'} {name}")
    if not cond:
        _fails.append(name)


def _run(hook_path, payload, env_extra=None, cwd=None):
    env = dict(os.environ)
    for var in ("BRANCH_SWITCH_BYPASS", "MCP_INLINE_SECRET_BYPASS",
                "PRECOMMIT_F821_BYPASS", "SIBLING_SESSION_LOCK_BYPASS",
                "CHAINED_STATE_CMD_BYPASS", "ABS_DEPLOYED_HOOKS_DIR"):
        env.pop(var, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, hook_path], input=json.dumps(payload),
        capture_output=True, text=True, env=env, cwd=cwd, timeout=30,
    )


def _load(basename, modname):
    path = os.path.join(_HERE, basename)
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 1. Detection-shape unit tests, on synthetic in-memory source. Fast, no I/O.
# ---------------------------------------------------------------------------

def test_reads_bypass_env_literal_shape():
    src = 'if os.environ.get("FOO_BYPASS") == "1":\n    return 0\n'
    check("literal os.environ.get(\"...BYPASS\") is detected",
          reads_bypass_env(src) is True)


def test_reads_bypass_env_constant_shape():
    # The shape that was INVISIBLE before this scanner's constant-detection
    # path existed: the name lives in a variable, not a string literal at
    # the read site. This is exactly how check-py-import-precommit.py and
    # session-lock.py spell their bypass.
    src = ('BYPASS_ENV = "FOO_BYPASS"\n'
           'if os.environ.get(BYPASS_ENV) == "1":\n    return 0\n')
    check("constant-hoisted os.environ.get(BYPASS_ENV) is detected",
          reads_bypass_env(src) is True)


def test_reads_bypass_env_ignores_unrelated_constants():
    # A constant that does NOT end in ...BYPASS, or one never passed to
    # os.environ.get/[, must not false-positive.
    src = ('OTHER = "FOO_BYPASS"\n'
           'TIMEOUT = 30\n'
           'if os.environ.get(TIMEOUT) == "1":\n    return 0\n')
    check("unrelated constant does not false-positive",
          reads_bypass_env(src) is False)


def test_consults_command_shapes():
    check("inline_bypass( call -> consults",
          consults_command('x = inline_bypass(cmd, "V")') is True)
    check("_inline_bypass( local-copy call -> consults (substring match)",
          consults_command('x = _inline_bypass(cmd, "V")') is True)
    check("leading_env_assigns( call -> consults",
          consults_command('x = leading_env_assigns(cmd)') is True)
    check("cmd_env import -> consults",
          consults_command('from cmd_env import inline_bypass') is True)
    check("segment_bypass_flags( call -> consults",
          consults_command('x = segment_bypass_flags(segs, "V")') is True)
    check("no consult marker -> does not consult",
          consults_command('x = os.environ.get("V")') is False)


# ---------------------------------------------------------------------------
# 2. scan_hooks() end to end, over real planted fixture files (never the
#    live repo tree for this test -- that is test_ci_self_scan below).
#    Requires REQUIRED CONTROL 1 (literal violator), REQUIRED CONTROL 2
#    (constant-spelling violator), and the POSITIVE (consults -> clean).
# ---------------------------------------------------------------------------

_VIOLATOR_LITERAL = '''\
import json, os, sys
def main():
    if os.environ.get("FOO_BYPASS") == "1":
        return 0
    payload = json.loads(sys.stdin.read())
    if payload.get("tool_name") != "Bash":
        return 0
    command = payload.get("tool_input", {}).get("command", "")
    return 2
'''

_VIOLATOR_CONST = '''\
import json, os, sys
BYPASS_ENV = "FOO_BYPASS"
def main():
    if os.environ.get(BYPASS_ENV) == "1":
        return 0
    payload = json.loads(sys.stdin.read())
    if payload.get("tool_name") != "Bash":
        return 0
    command = payload.get("tool_input", {}).get("command", "")
    return 2
'''

_CLEAN_HOOK = '''\
import json, os, sys
sys.path.insert(0, "_lib")
from cmd_env import inline_bypass
def main():
    payload = json.loads(sys.stdin.read())
    if payload.get("tool_name") != "Bash":
        return 0
    command = payload.get("tool_input", {}).get("command", "")
    if os.environ.get("FOO_BYPASS") == "1" or inline_bypass(command, "FOO_BYPASS"):
        return 0
    return 2
'''

_NOT_A_GATE = '''\
import os
def main():
    if os.environ.get("FOO_BYPASS") == "1":
        return
    print("session start stuff, no tool_input/command/Bash here")
'''


def test_scan_hooks_fixture_dir():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "violator-literal.py").write_text(_VIOLATOR_LITERAL)
        (Path(tmp) / "violator-const.py").write_text(_VIOLATOR_CONST)
        (Path(tmp) / "clean-hook.py").write_text(_CLEAN_HOOK)
        (Path(tmp) / "not-a-gate.py").write_text(_NOT_A_GATE)
        # Same content as the literal violator, but a test_ prefix: must be
        # skipped by scan_hooks regardless of what it contains.
        (Path(tmp) / "test_violator-literal.py").write_text(_VIOLATOR_LITERAL)

        enforce, violators = scan_hooks(tmp)

        check("REQUIRED CONTROL 1 (negative, literal): "
              "violator-literal.py is caught",
              "violator-literal.py" in violators)
        check("REQUIRED CONTROL 2 (negative, constant spelling): "
              "violator-const.py is caught",
              "violator-const.py" in violators)
        check("POSITIVE: clean-hook.py (consults command) is enforce, "
              "not a violator",
              "clean-hook.py" in enforce and "clean-hook.py" not in violators)
        check("not-a-gate.py (no tool_input/command) is excluded entirely",
              "not-a-gate.py" not in enforce)
        check("test_*.py is skipped even when its content is a violator",
              "test_violator-literal.py" not in enforce)
        check("exactly the 2 planted violators, nothing else",
              set(violators) == {"violator-literal.py", "violator-const.py"})
        check("exactly the 3 planted Bash-gate bypass hooks are enforce",
              set(enforce) == {"violator-literal.py", "violator-const.py",
                               "clean-hook.py"})


def test_scan_hooks_never_raises_on_unreadable():
    with tempfile.TemporaryDirectory() as tmp:
        # A directory named *.py is not a regular file; safe_read_text must
        # report it unreadable and scan_hooks must skip it, not raise.
        (Path(tmp) / "not-really-a-file.py").mkdir()
        try:
            enforce, violators = scan_hooks(tmp)
            ok = enforce == [] and violators == []
        except Exception as exc:  # pragma: no cover - the failure this proves
            ok = False
            print(f"    scan_hooks raised: {exc!r}")
        check("scan_hooks never raises on an unreadable entry", ok)


# ---------------------------------------------------------------------------
# 3. THE CI WATCHDOG: this repo's own tracked hooks/, scanned for real.
# ---------------------------------------------------------------------------

def test_no_bash_gate_reads_bypass_without_consulting_command():
    enforce, violators = scan_hooks(_HERE, exempt=EXEMPT)

    print(f"  enforce set ({len(enforce)} Bash-gate bypass hooks): {enforce}")
    check("at least 10 known Bash-gate bypass hooks are in scope (guard is live)",
          len(enforce) >= 10)
    check("every Bash-gate bypass hook in this repo consults the command "
          "for its bypass",
          not violators)
    if violators:
        print("    VIOLATORS (read bypass from os.environ only, never the "
              "command):")
        for v in violators:
            print(f"      - {v}  -> wire `from cmd_env import inline_bypass` "
                  "(hooks/_lib/cmd_env.py) and check "
                  "`os.environ.get(VAR) == '1' or inline_bypass(cmd, VAR)`")


# ---------------------------------------------------------------------------
# 4. FLEET PROOF -- the exact real invocation shape, per fixed hook.
#    check-py-import-precommit.py and session-lock.py are covered against
#    real git-repo fixtures in
#    tests/integration/test_session_coordination_guards.sh instead of here.
# ---------------------------------------------------------------------------

def test_mcp_inline_secret_fleet():
    hook = os.path.join(_HERE, "block-claude-mcp-inline-secret.py")
    secret_cmd = "claude mcp add x --env KEY=sk-ant-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIII"

    r = _run(hook, {"tool_name": "Bash", "tool_input": {"command": secret_cmd}})
    check("mcp-secret: secret add BLOCKS with no bypass (negative control)",
          r.returncode == 2 and "BLOCKED" in r.stderr)

    r = _run(hook, {"tool_name": "Bash",
                    "tool_input": {"command": "MCP_INLINE_SECRET_BYPASS=1 " + secret_cmd}})
    check("mcp-secret: inline prefix ALLOWS (rc 0, no BLOCKED)",
          r.returncode == 0 and "BLOCKED" not in r.stderr)

    r = _run(hook, {"tool_name": "Bash", "tool_input": {"command": secret_cmd}},
             env_extra={"MCP_INLINE_SECRET_BYPASS": "1"})
    check("mcp-secret: session env still ALLOWS (pre-existing path unbroken)",
          r.returncode == 0)


def test_chained_state_command_fleet():
    hook = os.path.join(_HERE, "warn-chained-state-command-truncated.py")
    # Detector C: a verification-gate command piped to tail, with its exit
    # status then read back via bare $? -- always the pager's status, never
    # the gate's. This hook never blocks (always exit 0); the SIGNAL is
    # whether it prints an advisory at all.
    risky_cmd = "ci-test 2>&1 | tail -80; echo $?"

    r = _run(hook, {"tool_name": "Bash", "tool_input": {"command": risky_cmd}})
    check("chained-state: fires (prints an advisory) with no bypass "
          "(negative control)",
          r.returncode == 0 and "permissionDecisionReason" in r.stdout)

    r = _run(hook, {"tool_name": "Bash",
                    "tool_input": {"command": "CHAINED_STATE_CMD_BYPASS=1 " + risky_cmd}})
    check("chained-state: inline prefix silences it (empty stdout, rc 0)",
          r.returncode == 0 and r.stdout.strip() == "")

    r = _run(hook, {"tool_name": "Bash", "tool_input": {"command": risky_cmd}},
             env_extra={"CHAINED_STATE_CMD_BYPASS": "1"})
    check("chained-state: session env still silences it (pre-existing path "
          "unbroken)",
          r.returncode == 0 and r.stdout.strip() == "")


def test_branch_switch_bypass_predicate():
    # Function-level: this hook's block path needs a real "module in flight"
    # git fixture to reach (find_modules_in_flight scans working-tree
    # status), so exercise the fixed predicate directly -- the same
    # narrow-unit approach block-branch-switch already uses for the rest of
    # its logic (there is no hooks/test_*.py for this hook yet; this is its
    # first test surface).
    mod = _load("block-branch-switch-with-untracked-build.py", "bsw_under_test")
    check("branch-switch: no bypass present -> False",
          mod._bypass("git checkout main") is False)
    check("branch-switch: inline BRANCH_SWITCH_BYPASS=1 prefix -> True",
          mod._bypass("BRANCH_SWITCH_BYPASS=1 git checkout main") is True)
    check("branch-switch: a quoted/non-leading token is not a bypass",
          mod._bypass("echo 'BRANCH_SWITCH_BYPASS=1' && git checkout main") is False)
    saved = os.environ.pop("BRANCH_SWITCH_BYPASS", None)
    try:
        os.environ["BRANCH_SWITCH_BYPASS"] = "1"
        check("branch-switch: session env still bypasses",
              mod._bypass("git checkout main") is True)
    finally:
        os.environ.pop("BRANCH_SWITCH_BYPASS", None)
        if saved is not None:
            os.environ["BRANCH_SWITCH_BYPASS"] = saved


def test_branch_switch_end_to_end_with_real_repo():
    # The full real invocation: a git repo with a genuine "module in flight"
    # (3+ untracked source files under one 2-segment dir), gated via a
    # dangerous `git checkout` naming the repo with `-C`.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "--allow-empty", "-q", "-m", "x"],
                       cwd=repo, check=True)
        voice = repo / "src" / "voice"
        voice.mkdir(parents=True)
        for name in ("a.py", "b.py", "c.py"):
            (voice / name).write_text("# untracked\n")

        hook = os.path.join(_HERE, "block-branch-switch-with-untracked-build.py")
        cmd = f"git -C {repo} checkout main"

        r = _run(hook, {"tool_name": "Bash", "tool_input": {"command": cmd}})
        blocked = json.loads(r.stdout or "{}").get(
            "hookSpecificOutput", {}).get("permissionDecision") == "deny"
        check("branch-switch e2e: module-in-flight + checkout BLOCKS "
              "(negative control)", blocked)

        r = _run(hook, {"tool_name": "Bash",
                        "tool_input": {"command": f"BRANCH_SWITCH_BYPASS=1 {cmd}"}})
        allowed = json.loads(r.stdout or "{}").get("hookSpecificOutput") is None
        check("branch-switch e2e: inline prefix ALLOWS", allowed)


# ---------------------------------------------------------------------------
# 5. The SessionStart SURFACER over a fixture "deployed hooks" directory.
# ---------------------------------------------------------------------------

def test_surfacer_reports_violator_and_stays_quiet_when_clean():
    surfacer = os.path.join(_HERE, "surface-bypass-unreachable.py")
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "violator-literal.py").write_text(_VIOLATOR_LITERAL)
        (Path(tmp) / "clean-hook.py").write_text(_CLEAN_HOOK)

        r = _run(surfacer, {}, env_extra={"ABS_DEPLOYED_HOOKS_DIR": tmp})
        out = json.loads(r.stdout or "{}")
        ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
        check("surfacer: reports the deployed violator by name",
              r.returncode == 0 and "violator-literal.py" in ctx)

        (Path(tmp) / "violator-literal.py").unlink()
        r = _run(surfacer, {}, env_extra={"ABS_DEPLOYED_HOOKS_DIR": tmp})
        check("surfacer: silent once the deployed dir is clean",
              r.returncode == 0 and json.loads(r.stdout or "{}") == {})

        missing = os.path.join(tmp, "does-not-exist")
        r = _run(surfacer, {}, env_extra={"ABS_DEPLOYED_HOOKS_DIR": missing})
        check("surfacer: silent when the deployed hooks dir does not exist "
              "(fresh install)",
              r.returncode == 0 and json.loads(r.stdout or "{}") == {})


def test_wired_via_surface_deployed_hooks_behind():
    # THE ACTUAL ACTIVATED SURFACE. surface-bypass-unreachable.py has no
    # SessionStart entry of its own -- SessionStart was already at its
    # footprint-SLA cap (19/19; scripts/footprint-sla-check.py --gate), the
    # exact situation TEMPLATE_ONLY documents for surface-stalled-git-
    # operation.py and surface-sync-guard-findings.py in scripts/
    # check-hook-activation.py. Same fix: it ships as a report BUILDER
    # (build_message) that an ALREADY-WIRED SessionStart hook calls at its
    # existing emission point. If this integration is missing, the scanner
    # and the standalone-invocation tests above are correct but DEAD --
    # nothing in a real session ever calls them.
    mod = _load("surface-deployed-hooks-behind.py", "sdb_under_test")
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "violator-literal.py").write_text(_VIOLATOR_LITERAL)
        saved = os.environ.get("ABS_DEPLOYED_HOOKS_DIR")
        os.environ["ABS_DEPLOYED_HOOKS_DIR"] = tmp
        try:
            msg = mod._bypass_unreachable_message()
        finally:
            if saved is None:
                os.environ.pop("ABS_DEPLOYED_HOOKS_DIR", None)
            else:
                os.environ["ABS_DEPLOYED_HOOKS_DIR"] = saved
        check("surface-deployed-hooks-behind.py reports the deployed "
              "violator via _bypass_unreachable_message() (the real, wired "
              "SessionStart path, not just the standalone module)",
              bool(msg) and "violator-literal.py" in (msg or ""))


if __name__ == "__main__":
    for fn in sorted((v for k, v in globals().items() if k.startswith("test_")),
                     key=lambda f: f.__name__):
        print(fn.__name__)
        fn()
    print()
    if _fails:
        print(f"FAILED ({len(_fails)}): {_fails}")
        sys.exit(1)
    print("ALL PASS")
    sys.exit(0)
