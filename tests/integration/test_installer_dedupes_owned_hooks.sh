#!/usr/bin/env bash
# test_installer_dedupes_owned_hooks.sh - the installer collapses a duplicate OWNED-hook
# copy and never touches a user's own hooks.
#
# The matcher-aware merge (which correctly registers a hook under every matcher it ships
# under) exposed a latent class: an owned hook whose stored command text drifts (bare
# `python3` -> absolute shim-safe interpreter) is not literally == the template command,
# so on a machine that already carried a second stale copy the replace touched only the
# first and a duplicate persisted. observe-tool-calls ships under two matchers and hit
# exactly this. Fix = own it (dedup by basename) + a dedupe pass that self-heals machines
# already carrying the duplicate.
#
# Controls (a guard earns trust only by FAILING/ACTING on the thing it catches):
#   1. observe-tool-calls.py is OWNED (else basename dedup can't recognize the drift copy).
#   2. NEGATIVE-ish: a group with two owned copies of the same script collapses to ONE,
#      keeping the freshest (last) copy.
#   3. POSITIVE: a user's own duplicate-looking hooks are left completely intact.
#   4. END-TO-END: a fresh install of the REAL hooks.json wires observe under BOTH of its
#      matchers, once each, and a re-run is idempotent (no dedup, no growth).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export REPO_ROOT

python3 <<'PY'
import importlib.util, os, json, sys
from pathlib import Path

repo = os.environ["REPO_ROOT"]
installer = repo + "/scripts/install-hooks-user-level.py"
spec = importlib.util.spec_from_file_location("ih", installer)
ih = importlib.util.module_from_spec(spec); spec.loader.exec_module(ih)

def fail(msg):
    print("FAIL:", msg); sys.exit(1)

OBS = "observe-tool-calls.py"

# 1. observe-tool-calls must be OWNED (the forward fix that prevents new duplicates).
if OBS not in ih.ABS_OWNED_BASENAMES:
    fail(f"{OBS} not in ABS_OWNED_BASENAMES - drift copies would not dedup by basename")
print("PASS: observe-tool-calls.py is owned")

# 2. A group with two owned copies of the same script (interpreter-path drift) collapses
#    to ONE, keeping the LAST (freshest = the just-merged template form).
dup = {"hooks": {"PreToolUse": [{"matcher": "X", "hooks": [
    {"type": "command", "command": f"python3 ~/.claude/skills/ai-brain-starter/hooks/{OBS}"},
    {"type": "command", "command": f"/opt/x/python3 ~/.claude/skills/ai-brain-starter/hooks/{OBS}"},
]}]}}
cleaned, removed = ih.dedupe_owned_hooks(dup)
kept = [h["command"] for g in cleaned["hooks"]["PreToolUse"] for h in g["hooks"] if OBS in h["command"]]
if not (removed == 1 and len(kept) == 1):
    fail(f"owned dup not collapsed (removed={removed}, kept={len(kept)})")
if "/opt/x/python3" not in kept[0]:
    fail("dedupe did not keep the LAST (freshest) copy")
print("PASS: duplicate owned hook collapsed to the freshest single copy (removed=1)")

# 3. A user's OWN duplicate-looking hooks are never touched.
user = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
    {"type": "command", "command": "echo mine"},
    {"type": "command", "command": "echo mine"},
]}]}}
_, r2 = ih.dedupe_owned_hooks(user)
if r2 != 0:
    fail(f"dedupe removed {r2} USER hook(s) - it must only touch owned hooks")
print("PASS: user (non-owned) hooks left intact")

# 4. END-TO-END over the REAL hooks.json: observe lands under BOTH matchers once each,
#    and it self-heals an already-duplicated machine + is idempotent.
tmpl = json.load(open(repo + "/hooks.json"))
tmpl = ih.normalize_path_substitutions(tmpl, str(Path.home()))
tmpl = ih.substitute_python_interpreter(tmpl)
absinterp = ih._posix_python()

def install(existing):
    m, _ = ih.merge_hooks(existing, tmpl)
    m, _ = ih.retire_stale_hooks(m)
    m, _ = ih.relocate_moved_hooks(m, tmpl)
    m, c = ih.dedupe_owned_hooks(m)
    return m, c

def obs_matchers(d):
    return sorted(g.get("matcher") for g in d.get("hooks", {}).get("PreToolUse", [])
                  for h in g.get("hooks", []) if OBS in h.get("command", ""))

fresh, cf = install({})
if len(obs_matchers(fresh)) != 2 or len(set(obs_matchers(fresh))) != 2:
    fail(f"fresh install did not wire observe under 2 distinct matchers: {obs_matchers(fresh)}")
if cf != 0:
    fail(f"fresh install spuriously deduped {cf}")
print("PASS: fresh install wires observe under both matchers, once each")

# already-duplicated machine (bare + absolute copy per matcher): self-heals to one each.
dmg = json.loads(json.dumps(tmpl))
for g in dmg["hooks"]["PreToolUse"]:
    extra = [dict(h, command=h["command"].replace(absinterp, "python3"))
             for h in g["hooks"] if OBS in h.get("command", "")]
    g["hooks"] = g["hooks"] + extra
healed, ch = install(dmg)
if len(obs_matchers(healed)) != 2 or ch < 2:
    fail(f"damaged machine not healed: matchers={obs_matchers(healed)} deduped={ch}")
print(f"PASS: already-duplicated machine self-heals to one per matcher (deduped={ch})")

# idempotent: re-running install over a clean result changes nothing.
again, ca = install(fresh)
if len(obs_matchers(again)) != 2 or ca != 0:
    fail(f"re-run not idempotent: matchers={obs_matchers(again)} deduped={ca}")
print("PASS: re-run is idempotent (no growth, no dedup)")

print("\nAll assertions passed. Owned-hook dedup collapses drift duplicates, keeps user hooks.")
PY
