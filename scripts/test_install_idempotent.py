#!/usr/bin/env python3
"""
test_install_idempotent.py - installing twice must not grow settings.json (MYC-3876).

Run: python3 scripts/test_install_idempotent.py
No pytest dependency. Exits non-zero on any failure. Touches no real settings.

THE BUG. dedupe_owned_hooks() keys on _owned_basenames(), so it can only collapse
hooks the template still declares. A hook wired by an OLDER hooks.json but since
dropped from both the template and ABS_OWNED_BASENAMES resolves to an EMPTY key,
is read as a user hook, and is therefore immortal - every later install appends
another copy that nothing reaps.

Measured on a real long-lived Windows account before the fix: 111 entries where a
fresh install writes 56, with NINE hooks present SEVEN times each (one POSIX form
plus six byte-identical Windows forms), while `Deduped:` printed 0 on every run.
At the measured ~59 ms marginal cost per hook past core saturation, the redundant
entries alone cost ~1.8 s on every session start.

These asserts fail on the pre-fix code and pass on dedupe_identical_hooks().
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    path = ROOT / "scripts" / "install-hooks-user-level.py"
    spec = importlib.util.spec_from_file_location("_abs_installer_test", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


inst = _load()

FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    if not cond:
        FAILS.append(name)


# An UNOWNED hook: absent from ABS_OWNED_BASENAMES and from the template. This is
# precisely the copy dedupe_owned_hooks() is structurally blind to.
UNOWNED = ('py -3 "C:\\a\\hook_runner.py" --fallback silent '
           '"C:\\a\\hooks\\surface-backup-status.py"')
POSIX = ("python3 ~/.claude/skills/ai-brain-starter/hooks/"
         "surface-backup-status.py 2>/dev/null || echo '{}'")


def settings(*commands, event="SessionStart", matcher=None):
    group = {"hooks": [{"type": "command", "command": c} for c in commands]}
    if matcher is not None:
        group["matcher"] = matcher
    return {"hooks": {event: [group]}}


def count(s):
    return sum(len(g.get("hooks", []))
               for groups in s.get("hooks", {}).values() for g in groups)


# --- premise ---------------------------------------------------------------
# If this ever starts returning a key, the bug shape changed and every assert
# below is testing nothing. Guard the premise, not just the fix.
check("premise-unowned-hook-is-really-unowned",
      not inst._owned_basenames(UNOWNED))

# The pre-existing pass cannot see it. This documents WHY a second pass exists.
_, owned_removed = inst.dedupe_owned_hooks(settings(UNOWNED, UNOWNED, UNOWNED))
check("premise-owned-dedup-is-blind-to-it", owned_removed == 0)

# --- the fix ---------------------------------------------------------------
cleaned, removed = inst.dedupe_identical_hooks(settings(*[UNOWNED] * 6))
check("six-identical-collapse-to-one", removed == 5 and count(cleaned) == 1)

# The wild shape: one POSIX form + six identical Windows forms. The POSIX variant
# MUST survive - it is a different string, and settings.json may be shared with a
# machine where it is the live form.
cleaned, removed = inst.dedupe_identical_hooks(settings(POSIX, *[UNOWNED] * 6))
kept = [h["command"] for h in cleaned["hooks"]["SessionStart"][0]["hooks"]]
check("wild-seven-copy-shape-collapses-to-two", removed == 5)
check("posix-variant-survives", kept == [POSIX, UNOWNED])

# --- must NOT over-reach ---------------------------------------------------
cleaned, removed = inst.dedupe_identical_hooks(
    settings("echo one", "echo two", "echo three"))
check("distinct-user-hooks-untouched", removed == 0 and count(cleaned) == 3)

# Same command, two matchers = two different trigger conditions, not a duplicate.
two_matchers = {"hooks": {"PreToolUse": [
    {"matcher": "Bash", "hooks": [{"type": "command", "command": UNOWNED}]},
    {"matcher": "Write", "hooks": [{"type": "command", "command": UNOWNED}]},
]}}
cleaned, removed = inst.dedupe_identical_hooks(two_matchers)
check("different-matchers-preserved", removed == 0 and count(cleaned) == 2)

# ...but the SAME matcher across two groups is a duplicate: a later install can
# append a new group rather than grow the existing one.
same_matcher = {"hooks": {"PreToolUse": [
    {"matcher": "Bash", "hooks": [{"type": "command", "command": UNOWNED}]},
    {"matcher": "Bash", "hooks": [{"type": "command", "command": UNOWNED}]},
]}}
cleaned, removed = inst.dedupe_identical_hooks(same_matcher)
check("same-matcher-across-groups-collapses", removed == 1 and count(cleaned) == 1)

# A group emptied by collapsing must be dropped, not left as a bare "Event": [].
emptied = {"hooks": {"SessionStart": [
    {"hooks": [{"type": "command", "command": UNOWNED}]},
    {"hooks": [{"type": "command", "command": UNOWNED}]},
]}}
cleaned, _ = inst.dedupe_identical_hooks(emptied)
check("emptied-group-dropped", len(cleaned["hooks"]["SessionStart"]) == 1)

# --- structural properties -------------------------------------------------
# Fixed point: a second run must change nothing, or repeated installs still
# drift, just more slowly.
once, _ = inst.dedupe_identical_hooks(settings(POSIX, *[UNOWNED] * 6))
twice, again = inst.dedupe_identical_hooks(once)
check("is-a-fixed-point", again == 0 and twice == once)

# Purity: the caller's dict must not be mutated.
src = settings(UNOWNED, UNOWNED)
before = json.dumps(src, sort_keys=True)
inst.dedupe_identical_hooks(src)
check("input-not-mutated", json.dumps(src, sort_keys=True) == before)

# Degenerate inputs.
c, r = inst.dedupe_identical_hooks({})
check("empty-settings-safe", r == 0 and c == {})
c, r = inst.dedupe_identical_hooks({"hooks": {}})
check("no-events-safe", r == 0)

for n in (2, 3, 7, 12):
    c, r = inst.dedupe_identical_hooks(settings(*[UNOWNED] * n))
    check(f"pile-of-{n}-collapses-to-one", r == n - 1 and count(c) == 1)

# --- "byte-identical" must mean the WHOLE entry, not just the command --------
# Keying on `command` alone made the docstring's warrant false: two entries
# sharing a command but carrying different timeouts are NOT the same
# configuration, and collapsing them silently discarded the longer timeout --
# on UNOWNED entries, which may be the user's own.
diff_timeout = {"hooks": {"SessionStart": [{"hooks": [
    {"type": "command", "command": UNOWNED, "timeout": 5},
    {"type": "command", "command": UNOWNED, "timeout": 600},
]}]}}
c, r = inst.dedupe_identical_hooks(diff_timeout)
check("differing-timeout-is-NOT-a-duplicate", r == 0 and count(c) == 2)
check("longer-timeout-survives",
      any(h.get("timeout") == 600
          for h in c["hooks"]["SessionStart"][0]["hooks"]))

same_timeout = {"hooks": {"SessionStart": [{"hooks": [
    {"type": "command", "command": UNOWNED, "timeout": 5},
    {"type": "command", "command": UNOWNED, "timeout": 5},
]}]}}
c, r = inst.dedupe_identical_hooks(same_timeout)
check("genuinely-identical-entries-still-collapse", r == 1 and count(c) == 1)

# --- the "deliberately narrow" claim, asserted rather than asserted-about ----
# Near-misses must survive. Without these, a future "helpful" normalisation
# (trim whitespace, casefold) could be added and no test would notice.
near_misses = [
    ("extra-inner-space", "py -3  x.py", "py -3 x.py"),
    ("trailing-space", "py -3 x.py ", "py -3 x.py"),
    ("case-difference", "PY -3 X.PY", "py -3 x.py"),
    ("quoting-difference", 'py -3 "x.py"', "py -3 x.py"),
]
for name, a, b in near_misses:
    c, r = inst.dedupe_identical_hooks(settings(a, b))
    check(f"near-miss-preserved-{name}", r == 0 and count(c) == 2)

# --- WIRING: main() must actually CALL the collapse --------------------------
# Everything above tests the function in isolation, so all of it still passes if
# the call site in main() is deleted or its result discarded. In a repo that
# carries an explicit artifact-without-activation doctrine, an unwired pass is
# the same defect as no pass. Drive the real installer end to end.
import os                     # noqa: E402
import subprocess             # noqa: E402
import sys as _sys            # noqa: E402
import tempfile               # noqa: E402

with tempfile.TemporaryDirectory() as td:
    home = Path(td) / "home"
    (home / ".claude").mkdir(parents=True)
    target = home / ".claude" / "settings.json"
    # Six byte-identical UNOWNED copies: invisible to dedupe_owned_hooks, so only
    # the new pass can remove them.
    target.write_text(json.dumps(settings(*[UNOWNED] * 6)), encoding="utf-8")

    env = dict(os.environ)
    env.update(USERPROFILE=str(home), HOME=str(home),
               HOMEDRIVE=str(home)[:2], HOMEPATH=str(home)[2:])
    proc = subprocess.run(
        [_sys.executable, str(ROOT / "scripts" / "install-hooks-user-level.py"),
         "--settings", str(target), "--quiet"],
        capture_output=True, env=env, timeout=180,
    )
    after = json.loads(target.read_text(encoding="utf-8"))
    survivors = [h for groups in after.get("hooks", {}).values()
                 for g in groups for h in g.get("hooks", [])
                 if h.get("command") == UNOWNED]
    check("WIRING-installer-run-succeeded", proc.returncode == 0)
    check("WIRING-installer-collapses-identical-copies", len(survivors) == 1)

# --- report ----------------------------------------------------------------
if FAILS:
    print("FAIL: " + ", ".join(FAILS), file=sys.stderr)
    sys.exit(1)
print("test_install_idempotent OK: identical-hook collapse is correct, "
      "narrow, and a fixed point")
