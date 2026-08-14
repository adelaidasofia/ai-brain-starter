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

# --- report ----------------------------------------------------------------
if FAILS:
    print("FAIL: " + ", ".join(FAILS), file=sys.stderr)
    sys.exit(1)
print("test_install_idempotent OK: identical-hook collapse is correct, "
      "narrow, and a fixed point")
