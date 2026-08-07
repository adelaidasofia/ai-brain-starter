#!/usr/bin/env python3
"""footprint-sla-check.py - the Footprint SLA gate for the hook fleet (MYC-2358).

The substrate's `hooks.json` is what EVERY install runs (free self-install and
paid commercial install alike - same fleet, per docs/HOOK_FLEET_RESOURCE_GOVERNANCE.md).
Each wired hook is a COLD `python3` start. The felt cost of a hot event is
therefore interpreter-startup x fan-out: a `Write` fans out to several cold
starts, SessionStart to more. Without a budget gate, that fan-out silently
re-grows every time a hook is added, and the install slowly slows the machine -
bug class SLOW-INSTALL-FROM-LAZY-PLUMBING. This gate is the ratchet that holds
"100% value under a bounded footprint" for every future hook.

WHAT IS GATED (hard, deterministic, OS-independent) vs REPORTED (advisory)
-------------------------------------------------------------------------
Hooks for one event run CONCURRENTLY in Claude Code, so felt WALL-CLOCK latency
= MAX(hook), not SUM (this corrected the Stage 0 over-statement; see
docs/adr/0004-footprint-sla-gate.md). A wall-clock-ms gate would also be FLAKY on
a shared CI runner, and a gate that fails on timing noise teaches bypass
(over-strict-verification-teaches-bypass). So the HARD gate keys only on
DETERMINISTIC axes that parse committed source - no timing, no execution:

  HARD (block merge):
    A. per-event substrate cold-start FAN-OUT count vs budget. This is the
       CPU/battery axis (N cold `python3` spawns) AND a reliability axis (one
       slow hook stalls the whole event to its timeout). ALL substrate-python
       entries count: `once: true` is IGNORED in settings.json (this hooks.json's
       merge target), so a `once` hook fires every event, not once - it is counted,
       and separately FLAGGED as a dead-flag breach (MYC-2359). PreToolUse /
       PostToolUse are matcher-gated, so fan-out is computed PER TOOL.
    B. default-on background daemon count (idle-CPU axis) - a coarse structural
       tripwire: the default install path (bootstrap.sh) must wire 0 launchd/cron
       daemons (daemons are opt-in). Trips if a change makes one default-on.
    E. DEAD once:true flags. Any `once: true` in this (settings-merged) hooks.json
       is a no-op that silently makes a "once-per-session" hook re-fire every event
       - the recurring-injection bug class (MYC-2359). A hard breach: move the hook
       to SessionStart (fires once per session-segment -> cached prefix) or drop
       the flag. Static, no execution. Negative control in --selftest.

  ADVISORY (printed by --measure --execute, NEVER blocks CI):
    C. per-hook cold-start TIME (median vs the bare `python3 -c pass` floor) and
       the per-event felt = MAX(hook). Flaky on shared runners -> reported only.
    D. per-message injected TOKENS from the UserPromptSubmit context-injectors -
       the recurring-token axis (MYC-2359). Executes each UPS substrate hook with a
       NEUTRAL prompt in a sandboxed HOME and sums emitted additionalContext
       (tokens ~= bytes/4) vs an advisory budget. A well-behaved conditional hook
       emits nothing on a neutral prompt (-> 0); an UNCONDITIONAL stable injector
       shows its full block - which is why such injectors belong on SessionStart
       (once, cached), not UPS (every message, fresh tokens). Real-fleet number is
       install-dependent -> advisory; the LOGIC is gated deterministically by the
       --selftest pos/neg synthetic controls.

SessionStart *boundedness* (the corpus-walk freeze class) is a SEPARATE, already
-wired gate (scripts/audit-sessionstart-boundedness.py + test_sessionstart_boundedness.sh).
This gate does NOT duplicate it - it governs fan-out + footprint, that one
governs per-hook work shape.

Budgets live in footprint-budgets.json: budget = the clean-install measured
fan-out + a small headroom, so the gate ships GREEN (an auto-managed gate must
never ship known-red) and bites only on real growth. As Stage 2 (precise
triggers + async) reduces the fan-out, tighten the budgets with --update-budgets.

Command classification - a wired command is one of:
  * substrate-python : `python3 .../skills/ai-brain-starter/(hooks|scripts)/X.py`
                       - the real cold-start the budget governs. Ships in this repo.
  * userlevel-guarded: `[ -f ~/.claude/hooks/X.py ] && python3 ... || true` -
                       maintainer-personal user-level hook; the `[ -f ]` is FALSE
                       on a fresh install -> a shell builtin test, no python
                       cold-start. EXCLUDED from the budgeted count (matches the
                       Stage 0 "fresh-install excludes these" finding).
  * vault-script     : `bash '[VAULT_PATH]/...'` - a vault-side template script,
                       not in this repo; reported, not in the python budget.
  * inline-bash      : pure bash (the auto-update / pinned-echo entries); reported.

Modes:
  --gate [--json]       THE CI GATE. Compare current fan-out + daemon count vs
                        footprint-budgets.json. Exit 0 = within budget; exit 1 =
                        over (the gate BITES); exit 2 = internal error (missing /
                        unparseable hooks.json or budgets - fail LOUD, never a
                        silent green).
  --measure [--execute] Human report of the current fan-out (+ advisory timing /
                        injected-bytes when --execute). No pass/fail. Measures the
                        SHIPPED hooks.json template.
  --measure-live [--execute] [--settings P] [--event E]
                        (MYC-2359 -> MYC-2396, hardened MYC-2409) Axis D against the LIVE
                        ~/.claude/settings.json instead of the template: the per-message
                        injected-token cost an install ACTUALLY pays, including the user's
                        OWN + customized injectors and the non-.py (vault-script /
                        inline-bash) + per-tool (PreToolUse / PostToolUse) hooks the
                        template-only axis D skips. Flags any stable every-message injector
                        with a "belongs on SessionStart (cached prefix)" hint. Defaults to
                        --event UserPromptSubmit (the safe per-message headline; UPS hooks
                        run in the REAL repo so a cwd-conditional CONTEXT.md loader is
                        measured). --event all also probes the tool-WRITE events, which run
                        in a THROWAWAY dir so a write/git hook can't touch the real repo.
                        With --execute and NO shell (Windows / non-POSIX) it FAILS LOUD -
                        structural inventory + "UNMEASURED", never a misleading "Clean"
                        (MYC-2409). Advisory, install-specific, never blocks; LOGIC (incl.
                        the fail-loud no-shell control) gated by --selftest.
  --selftest            Positive + negative controls: a synthetic over-budget
                        fleet trips --gate; a within-budget one passes; a missing
                        budgets file fails loud (exit 2); a default-on daemon
                        trips axis B. Exit 1 on any wrong verdict, 2 on internal
                        error. This is the gate's built-in negative control.
  --update-budgets      Maintainer convenience: rewrite footprint-budgets.json to
                        the current measured fan-out + headroom. NOT run in CI.

Stdlib only. Reads are bounded (1 MB cap, binary skip) - cloud-safe-walk
compliant. --gate / --selftest fail LOUD (exit 2) on internal error so a broken
gate can never silently pass CI.

Provenance: MYC-2358 (epic MYC-2348 "100% value, near-zero felt footprint";
Stage 0 footprint report + corrections). Sibling-by-design with
scripts/audit-sessionstart-boundedness.py (MYC-571), whose proven shape this
mirrors.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_READ = 1_000_000  # 1 MB bounded read (cloud-safe-walk: never block on a placeholder)

# Hot events whose per-event fan-out is budgeted directly (not matcher-gated).
NON_MATCHER_EVENTS = ["SessionStart", "UserPromptSubmit", "Stop", "PreCompact", "SessionEnd"]
# Matcher-gated events whose fan-out is budgeted PER TOOL.
MATCHER_EVENTS = ["PreToolUse", "PostToolUse"]
# Representative tools to compute matcher-gated fan-out for. mcp__ is a class.
REPRESENTATIVE_TOOLS = ["Write", "Edit", "MultiEdit", "Bash", "Read", "Glob",
                        "Grep", "Task", "Agent", "Skill", "NotebookEdit",
                        "mcp__server__tool"]

# A wired command that runs a substrate hook/script via python3. Any path under
# `skills/ai-brain-starter/.../<name>.py` is the substrate signature (matched at
# any depth so a future hook in a new subdir can't silently escape the count); it
# matches even the `[VAULT_PATH]/... || ~/.claude/...` fallback form because both
# halves carry that segment.
SUBSTRATE_RE = re.compile(r"skills/ai-brain-starter/(?:[\w.\-]+/)*([\w.\-]+\.(?:py|sh))")
# A maintainer-personal user-level hook, guarded so it no-ops on a fresh install.
USERLEVEL_GUARD_RE = re.compile(r"\[\s*-f\s+[^\]]*~/\.claude/hooks/")
VAULT_PATH_RE = re.compile(r"\[VAULT_PATH\]")

# Daemon-ACTIVATION tokens (a default install must contain none). Deliberately
# only activation signals - `launchctl load/bootstrap`, `crontab -`, or a named
# daemon-installer invocation - NOT plist-CONTENT tokens like `<key>StartInterval`,
# which a bootstrap may legitimately write into a template for an OPT-IN installer
# without making anything default-on (that would be an over-strict false trip).
DAEMON_TOKENS = [
    r"launchctl\s+(?:load|bootstrap)\b",
    r"\bcrontab\s+-",
    r"install-closed-loop-daemon",
    r"install-dev-hub-refresh-daemon",
    r"install-vault-daily-maintenance",
]
DAEMON_RE = re.compile("|".join(DAEMON_TOKENS))


# ---- bounded read -------------------------------------------------------------
def _read_bounded(path: Path) -> str:
    """Read up to MAX_READ bytes; skip binary / oversize / unreadable. Fail-open."""
    try:
        if path.stat().st_size > MAX_READ:
            return ""
        b = path.read_bytes()
        if b"\x00" in b[:4096]:
            return ""
        return b.decode("utf-8", "ignore")
    except Exception:
        return ""


# ---- command classification ---------------------------------------------------
def classify_command(cmd: str) -> tuple[str, str | None]:
    """Return (klass, basename). klass in {substrate-python, userlevel-guarded,
    vault-script, inline-bash}. basename is set only for substrate-python."""
    m = SUBSTRATE_RE.search(cmd)
    if m:
        return "substrate-python", m.group(1)
    if USERLEVEL_GUARD_RE.search(cmd):
        return "userlevel-guarded", None
    if VAULT_PATH_RE.search(cmd):
        return "vault-script", None
    return "inline-bash", None


def resolve_substrate(basename: str, hooks_dir: Path, scripts_dir: Path) -> Path | None:
    """Resolve a substrate basename to its shipped file (hooks/ then scripts/)."""
    for d in (hooks_dir, scripts_dir):
        p = d / basename
        if p.exists():
            return p
    return None


# ---- fleet parsing ------------------------------------------------------------
def load_fleet(hooks_json: Path) -> dict:
    """Parse hooks.json -> {event: [ {matcher, entries:[{command, once}]} ]}.

    Raises on missing / unparseable: the gate must fail LOUD, never treat an
    unreadable fleet as an empty (passing) one."""
    data = json.loads(hooks_json.read_text(encoding="utf-8"))
    hooks = data.get("hooks", {})
    out: dict[str, list] = {}
    for event, blocks in hooks.items():
        rows = []
        for block in (blocks or []):
            entries = []
            for h in (block.get("hooks") or []):
                cmd = (h.get("command") or "").strip()
                if not cmd:
                    continue
                entries.append({"command": cmd, "once": bool(h.get("once"))})
            rows.append({"matcher": block.get("matcher"), "entries": entries})
        out[event] = rows
    return out


def _matcher_matches(matcher: str | None, tool: str) -> bool:
    """A None matcher (non-matcher event) matches nothing here - those events are
    counted directly, not per tool. A present matcher is a Claude Code tool-name
    regex / alternation; a tool fires the block iff it fullmatches."""
    if matcher is None:
        return False
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return False


def event_fanout(fleet: dict, event: str) -> dict:
    """Fan-out for a NON-matcher event. Returns counts by class + the budgeted
    metric (ALL substrate-python entries = the recurring cold-start cost) and any
    substrate basenames missing in the repo (a wiring-drift integrity note).

    `once: true` does NOT reduce the count: this hooks.json is merged into
    ~/.claude/settings.json by install-hooks-user-level.py, and `once` is IGNORED
    in settings files (only honored in skill frontmatter). A `once` UPS hook
    therefore fires EVERY message, not once per session (MYC-2359). `dead_once`
    counts those misleading flags so the gate can flag them; it never discounts
    the fan-out."""
    counts = {"substrate-python": 0, "userlevel-guarded": 0, "vault-script": 0,
              "inline-bash": 0}
    budgeted = 0          # ALL substrate-python (once is dead in settings.json -> recurring)
    dead_once = 0         # substrate-python entries carrying a (dead, ignored) once:true
    basenames: list[str] = []
    for block in fleet.get(event, []):
        for e in block["entries"]:
            klass, bn = classify_command(e["command"])
            counts[klass] += 1
            if klass == "substrate-python":
                basenames.append(bn) if bn else None
                budgeted += 1
                if e["once"]:
                    dead_once += 1
    return {"counts": counts, "budgeted": budgeted, "dead_once": dead_once,
            "entries": sum(counts.values()), "basenames": basenames}


def tool_fanout(fleet: dict, event: str, tool: str) -> dict:
    """Fan-out for a matcher-gated event, for one tool. Budgeted = ALL
    substrate-python blocks whose matcher fires for this tool. `once` is NOT
    discounted (it is ignored in settings.json; see event_fanout); a once flag on
    such an entry is counted in dead_once."""
    budgeted = 0
    dead_once = 0
    basenames: list[str] = []
    classes = {"substrate-python": 0, "userlevel-guarded": 0, "vault-script": 0,
               "inline-bash": 0}
    for block in fleet.get(event, []):
        if not _matcher_matches(block["matcher"], tool):
            continue
        for e in block["entries"]:
            klass, bn = classify_command(e["command"])
            classes[klass] += 1
            if klass == "substrate-python":
                budgeted += 1
                if e["once"]:
                    dead_once += 1
                if bn:
                    basenames.append(bn)
    return {"budgeted": budgeted, "dead_once": dead_once, "classes": classes,
            "basenames": basenames}


def daemon_count(bootstrap: Path) -> int:
    """Coarse structural tripwire: count daemon-activation tokens on the default
    install path. A default install must wire 0 (daemons are opt-in)."""
    src = _read_bounded(bootstrap)
    if not src:
        return 0
    return len(DAEMON_RE.findall(src))


def missing_in_repo(fleet: dict, hooks_dir: Path, scripts_dir: Path) -> list[str]:
    """Substrate basenames wired in hooks.json but absent from hooks/ + scripts/.
    A wiring-drift integrity note (reported), not a footprint failure."""
    missing: set[str] = set()
    for blocks in fleet.values():
        for block in blocks:
            for e in block["entries"]:
                klass, bn = classify_command(e["command"])
                if klass == "substrate-python" and bn and \
                        resolve_substrate(bn, hooks_dir, scripts_dir) is None:
                    missing.add(bn)
    return sorted(missing)


def dead_once_flags(fleet: dict) -> list[str]:
    """Entries carrying `once: true`. This hooks.json is merged into
    ~/.claude/settings.json by install-hooks-user-level.py, and `once` is IGNORED
    in settings files (only honored in skill frontmatter) - so the flag is DEAD:
    the hook fires at its event's FULL cadence (every message on UserPromptSubmit),
    not once per session. A stable per-session injector that relies on it therefore
    re-injects every turn instead of landing once in the cached prefix - the exact
    recurring-token waste MYC-2359 fixes (instinct + session-start-context were
    measured re-injecting 14x / 17x in one session). Returns "event:basename"
    labels; empty = clean. A hard gate breach: move the hook to SessionStart (fires
    once per session-segment) or drop the no-op flag."""
    out: list[str] = []
    for event, blocks in fleet.items():
        for block in blocks:
            for e in block["entries"]:
                if not e.get("once"):
                    continue
                _, bn = classify_command(e["command"])
                out.append(f"{event}:{bn or '<inline/non-substrate>'}")
    return sorted(out)


# ---- current footprint snapshot ----------------------------------------------
def snapshot(fleet: dict, bootstrap: Path) -> dict:
    """The deterministic current footprint: budgeted fan-out per event + per tool
    for matcher-gated events, plus the default-on daemon count."""
    per_event = {ev: event_fanout(fleet, ev)["budgeted"] for ev in NON_MATCHER_EVENTS}
    per_tool: dict[str, dict[str, int]] = {}
    for ev in MATCHER_EVENTS:
        per_tool[ev] = {t: tool_fanout(fleet, ev, t)["budgeted"] for t in REPRESENTATIVE_TOOLS}
    return {"per_event": per_event, "per_tool": per_tool,
            "daemons": daemon_count(bootstrap)}


# ---- budgets ------------------------------------------------------------------
def load_budgets(path: Path) -> dict:
    """Load the budgets file. Raises on missing / unparseable (gate fails loud)."""
    return json.loads(path.read_text(encoding="utf-8"))


def build_budgets(snap: dict, headroom: int, injected_tokens_ceiling: int = 100) -> dict:
    """Budgets = current measured + headroom (per-tool budgets only for tools with
    a nonzero fan-out, plus the mcp class). Records the measured baseline for
    transparency. `injected_tokens_per_message` is a FIXED advisory ceiling (not a
    measured ratchet - axis D execution is install/runner-dependent, so a ceiling is
    more honest than ratcheting on a flaky measurement); it is preserved across
    --update-budgets."""
    per_event = {ev: n + headroom for ev, n in snap["per_event"].items()}
    per_tool: dict[str, dict[str, int]] = {}
    for ev, tools in snap["per_tool"].items():
        per_tool[ev] = {t: n + headroom for t, n in tools.items() if n > 0}
    return {
        "_doc": "Footprint SLA budgets for the ai-brain-starter hook fleet. "
                "HARD axes (block merge): per-event/per-tool substrate cold-start "
                "fan-out + default-on daemon count + DEAD once:true flags (a once "
                "flag is ignored in settings.json -> a 'once' hook re-fires every "
                "event; move it to SessionStart). Advisory: per-hook timing + "
                "per-message injected tokens (axis D, injected_tokens_per_message "
                "ceiling) are reported by --measure --execute, gated in CI only via "
                "--selftest controls. See docs/adr/0004-footprint-sla-gate.md.",
        "_headroom": headroom,
        "_baseline_measured": {"per_event": snap["per_event"],
                               "per_tool": {ev: {t: n for t, n in tools.items() if n > 0}
                                            for ev, tools in snap["per_tool"].items()},
                               "daemons": snap["daemons"]},
        "fanout_per_event": per_event,
        "fanout_per_tool": per_tool,
        "default_on_daemons": snap["daemons"],
        "injected_tokens_per_message": injected_tokens_ceiling,
    }


# ---- the gate -----------------------------------------------------------------
def evaluate(snap: dict, budgets: dict) -> list[dict]:
    """Pure comparison: list of breaches. Each breach: {axis, key, measured,
    budget}. Empty list = within budget. A budget missing for a measured surface
    is itself a breach (a new surface must be added to the budgets file
    deliberately - never silently ungoverned)."""
    breaches: list[dict] = []
    bpe = budgets.get("fanout_per_event", {})
    for ev, measured in snap["per_event"].items():
        budget = bpe.get(ev)
        if budget is None:
            # a measured surface with no budget is ungoverned -> breach, so a new
            # surface must be added to the budgets file deliberately. A 0-fan-out
            # event needs no budget (matches the per-tool guard below).
            if measured > 0:
                breaches.append({"axis": "fanout_per_event", "key": ev,
                                 "measured": measured, "budget": None})
        elif measured > budget:
            breaches.append({"axis": "fanout_per_event", "key": ev,
                             "measured": measured, "budget": budget})
    bpt = budgets.get("fanout_per_tool", {})
    for ev, tools in snap["per_tool"].items():
        ev_budgets = bpt.get(ev, {})
        for tool, measured in tools.items():
            budget = ev_budgets.get(tool)
            if budget is None:
                # only govern tools the budgets file lists (those with real
                # fan-out); a tool with 0 measured + no budget is fine.
                if measured > 0:
                    breaches.append({"axis": "fanout_per_tool", "key": f"{ev}:{tool}",
                                     "measured": measured, "budget": None})
            elif measured > budget:
                breaches.append({"axis": "fanout_per_tool", "key": f"{ev}:{tool}",
                                 "measured": measured, "budget": budget})
    dbudget = budgets.get("default_on_daemons")
    if dbudget is not None and snap["daemons"] > dbudget:
        breaches.append({"axis": "default_on_daemons", "key": "bootstrap.sh",
                         "measured": snap["daemons"], "budget": dbudget})
    return breaches


def cmd_gate(hooks_json: Path, hooks_dir: Path, scripts_dir: Path,
             budgets_path: Path, bootstrap: Path, as_json: bool) -> int:
    fleet = load_fleet(hooks_json)          # raises -> fail loud (exit 2)
    budgets = load_budgets(budgets_path)    # raises -> fail loud (exit 2)
    snap = snapshot(fleet, bootstrap)
    breaches = evaluate(snap, budgets)
    drift = missing_in_repo(fleet, hooks_dir, scripts_dir)
    dead = dead_once_flags(fleet)

    if as_json:
        print(json.dumps({"snapshot": snap, "breaches": breaches,
                          "missing_in_repo": drift, "dead_once_flags": dead}, indent=2))
        return 1 if (breaches or dead) else 0

    print("=" * 78)
    print("Footprint SLA gate (MYC-2358) - hook fan-out + default-on daemons")
    print("=" * 78)
    print(f"hooks.json: {hooks_json}\nbudgets:    {budgets_path}\n")
    print("Per-event substrate cold-start fan-out (recurring; once:true is a DEAD "
          "flag in settings.json - counted, not excluded):")
    bpe = budgets.get("fanout_per_event", {})
    for ev in NON_MATCHER_EVENTS:
        m = snap["per_event"][ev]
        b = bpe.get(ev)
        flag = "  OVER" if (b is not None and m > b) else ("  NO-BUDGET" if b is None else "")
        print(f"    {ev:<18} {m:>3} / {('-' if b is None else b)!s:<3}{flag}")
    print("\nPer-tool fan-out for matcher-gated events (Write/Bash/... cold starts):")
    bpt = budgets.get("fanout_per_tool", {})
    for ev in MATCHER_EVENTS:
        for tool in REPRESENTATIVE_TOOLS:
            m = snap["per_tool"][ev][tool]
            b = bpt.get(ev, {}).get(tool)
            if b is None and m == 0:
                continue
            flag = "  OVER" if (b is not None and m > b) else ("  NO-BUDGET" if b is None else "")
            print(f"    {ev}:{tool:<16} {m:>3} / {('-' if b is None else b)!s:<3}{flag}")
    db = budgets.get("default_on_daemons")
    print(f"\nDefault-on daemons (bootstrap.sh): {snap['daemons']} / "
          f"{'-' if db is None else db}"
          f"{'  OVER' if (db is not None and snap['daemons'] > db) else ''}")
    if drift:
        print(f"\n  note: {len(drift)} substrate hook(s) wired but missing in repo "
              f"(hooks/+scripts/): {', '.join(drift)}")
    if dead:
        print("\nDEAD once:true FLAG(S) - ignored in settings.json (only honored in "
              "skill frontmatter), so the hook fires EVERY event, not once per session:")
        for label in dead:
            print(f"  - {label}: re-fires every {label.split(':')[0]}. Move it to "
                  f"SessionStart (fires once per session-segment -> cached prefix) or "
                  f"drop the no-op flag. (MYC-2359)")
    if breaches:
        print("\nFOOTPRINT BUDGET EXCEEDED:")
        for br in breaches:
            if br["budget"] is None:
                print(f"  - {br['axis']} '{br['key']}' has NO budget ({br['measured']} "
                      f"measured). Add it to footprint-budgets.json deliberately.")
            else:
                print(f"  - {br['axis']} '{br['key']}': {br['measured']} > budget "
                      f"{br['budget']}. Optimize the fan-out (Stage 2: precise "
                      f"triggers / async / dispatcher) or, if intentional, raise "
                      f"the budget with a one-line rationale.")
    if breaches or dead:
        return 1
    print("\nAll footprint axes within budget. OK.")
    return 0


# ---- measure (advisory) -------------------------------------------------------
def _time_script(path: Path, runs: int, env_home: str) -> float | None:
    """Median wall-clock ms of `python3 <path>` with empty stdin, in a sandboxed
    HOME, per-run timeout. Advisory only. None on failure."""
    import os
    samples = []
    env = dict(os.environ)
    env["HOME"] = env_home
    for _ in range(runs):
        t0 = time.perf_counter()
        try:
            subprocess.run([sys.executable, str(path)], input=b"",
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=20, env=env)
        except Exception:
            return None
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    return samples[len(samples) // 2]


def _estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token; stdlib-only, no tokenizer dep)."""
    return (len(text) + 3) // 4


def measure_injected_tokens(fleet: dict, hooks_dir: Path, scripts_dir: Path,
                            env_home: str, prompt: str = "ping") -> dict:
    """Axis D (MYC-2359): the per-MESSAGE recurring injected-token cost. Executes
    each UserPromptSubmit substrate hook with a NEUTRAL prompt payload in a
    sandboxed HOME, parses hookSpecificOutput.additionalContext, and sums tokens
    (~bytes/4). A well-behaved CONDITIONAL hook (love-language / meeting-workflow)
    emits nothing on a neutral prompt -> 0. An UNCONDITIONAL stable injector emits
    its full block every message -> counted: that block belongs on SessionStart
    (once, cached prefix), not UPS (every message, fresh tokens). SessionStart
    hooks are intentionally NOT measured here - they fire once per session-segment,
    not per message, so they cost ~0 per message. Returns {total_tokens, per_hook:
    [(basename, tokens)]}. Executes hooks -> advisory only, never the static gate."""
    import os
    env = dict(os.environ)
    env["HOME"] = env_home
    payload = json.dumps({"prompt": prompt,
                          "hook_event_name": "UserPromptSubmit"}).encode()
    per_hook: list[tuple[str, int]] = []
    seen: set[str] = set()
    for block in fleet.get("UserPromptSubmit", []):
        for e in block["entries"]:
            klass, bn = classify_command(e["command"])
            # Only execute .py injectors: a .sh basename (e.g. an inline-bash entry
            # that merely references a shell helper) is not a python additionalContext
            # injector, and running it via python3 would just error to 0.
            if klass != "substrate-python" or not bn or not bn.endswith(".py") or bn in seen:
                continue
            seen.add(bn)
            p = resolve_substrate(bn, hooks_dir, scripts_dir)
            if p is None:
                continue
            try:
                r = subprocess.run([sys.executable, str(p)], input=payload,
                                   capture_output=True, timeout=20, env=env)
                out = r.stdout.decode("utf-8", "ignore")
            except Exception:
                continue
            tokens = 0
            try:
                ac = (json.loads(out).get("hookSpecificOutput") or {}).get("additionalContext")
                if isinstance(ac, str):
                    tokens = _estimate_tokens(ac)
                elif isinstance(ac, list):
                    tokens = _estimate_tokens("".join(str(x) for x in ac))
            except Exception:
                tokens = 0
            per_hook.append((bn, tokens))
    return {"total_tokens": sum(t for _, t in per_hook), "per_hook": per_hook}


# ---- live settings.json measurement (axis D-live, MYC-2396) -------------------
# measure_injected_tokens (above) measures the SHIPPED hooks.json - repo-resolved
# .py substrate hooks. But the per-message injection an install ACTUALLY pays lives
# in its ~/.claude/settings.json: the installer merges hooks.json INTO it, and it
# ALSO holds the user's OWN + customized injectors (a CONTEXT.md auto-loader, a
# per-message version check, ...). A stable every-message injector wired there is
# invisible to the template-only axis D - so an install can carry the exact
# recurring-token waste MYC-2359 just fixed, uncaught. The functions below execute
# each wired injector EXACTLY as Claude Code does - the literal command string via
# the shell, the event payload on stdin, in a sandboxed HOME - which is what makes
# UNOWNED, non-.py (vault-script / inline-bash), and per-tool (PreToolUse /
# PostToolUse) injectors measurable uniformly (the resolve-a-repo-file path skips
# all three). Advisory + install-specific + execution-based per ADR 0006; the LOGIC
# is gated by the --selftest synthetic pos/neg controls, never the raw numbers.

# Injector-capable, RECURRING events (per message / per tool call). SessionStart is
# deliberately excluded: it is the relocate TARGET (fires once per session-segment ->
# cached prefix), not a per-message cost, and executing its fleet (skill sync, backups,
# git) would be heavy + side-effectful. The whole point is to move stable blocks HERE.
LIVE_EVENTS = ["UserPromptSubmit", "PreToolUse", "PostToolUse"]


def _neutral_payload(event: str, tool: str = "Write", cwd: str = "/tmp") -> bytes:
    """A benign, prompt/tool-INDEPENDENT hook payload for `event`. A well-behaved
    CONDITIONAL injector emits nothing on it (-> 0 tokens); an UNCONDITIONAL stable
    injector emits its full block regardless (-> counted - the signal it belongs on
    SessionStart). `cwd` is the realistic working dir so a cwd/scope-conditional
    injector (e.g. a per-repo CONTEXT.md loader) is measured for that repo, not read
    as 0 from an empty /tmp. tool_input carries a file_path/content AND a benign
    command so a Write-, Edit-, or Bash-consuming hook all find a benign field."""
    base = {"hook_event_name": event, "session_id": "footprint-measure",
            "cwd": cwd, "transcript_path": "/tmp/footprint-none.jsonl"}
    if event == "UserPromptSubmit":
        base["prompt"] = "ping"
    elif event in ("PreToolUse", "PostToolUse"):
        base["tool_name"] = tool
        base["tool_input"] = {"file_path": "/tmp/footprint-probe.txt",
                              "content": "x", "command": "true"}
        if event == "PostToolUse":
            base["tool_response"] = {"success": True}
    return json.dumps(base).encode()


def _injected_text(stdout: str, event: str) -> str:
    """The context a hook's stdout would inject. Primary: hookSpecificOutput.
    additionalContext (str or list) - the canonical field (ADR 0005 axis D). A valid
    JSON no-op ({"continue":true,"suppressOutput":true}) injects nothing -> ''. For
    UserPromptSubmit, raw NON-JSON stdout is ALSO added to context by the harness, so
    count it there (other events do not inject raw stdout)."""
    s = stdout.strip()
    if not s:
        return ""
    parsed = None
    try:
        parsed = json.loads(s)
    except Exception:
        # some hooks print human noise then a JSON line; take the last JSON line.
        for line in reversed(s.splitlines()):
            t = line.strip()
            if not t:
                continue
            try:
                parsed = json.loads(t)
                break
            except Exception:
                continue
    if isinstance(parsed, dict):
        ac = (parsed.get("hookSpecificOutput") or {}).get("additionalContext")
        if isinstance(ac, str):
            return ac
        if isinstance(ac, list):
            return "".join(str(x) for x in ac)
        return ""  # valid JSON, no additionalContext -> a no-op
    return s if event == "UserPromptSubmit" else ""


def _resolve_shell() -> str | None:
    """The POSIX shell `--measure-live --execute` runs hooks through. `bash` on PATH,
    else /bin/bash if present, else None. None means this platform cannot execute the
    hooks (e.g. Windows with no bash) - cmd_measure_live FAILS LOUD on that rather than
    running every hook to a swallowed error and reporting a misleading 'Clean' (the
    silent-false-negative MYC-2409 fixes)."""
    import os
    import shutil
    return shutil.which("bash") or ("/bin/bash" if os.path.exists("/bin/bash") else None)


def _run_command_capture(command: str, payload: bytes, env_home: str, probe_cwd: str,
                         shell: str, timeout: int = 10) -> str | None:
    """Execute the LITERAL wired command via `shell -c` with `payload` on stdin, HOME
    redirected to a throwaway sandbox dir (so ~/-relative WRITES land there, not in the
    real home) and the working dir set to `probe_cwd`. Returns stdout on success, or
    None when the hook COULD NOT BE RUN (shell missing, spawn error, timeout, crash).
    The None vs '' distinction is load-bearing: None = unmeasured (the caller must NOT
    report it as a clean 0); '' / JSON-no-op = ran and injected nothing (a real clean 0).
    Mirrors how Claude Code invokes a hook (shell command string + JSON stdin), so it
    measures non-.py / guarded / unowned forms uniformly. EXECUTION -> advisory only."""
    import os
    if not shell:
        return None
    env = dict(os.environ)
    env["HOME"] = env_home
    env["CLAUDE_PROJECT_DIR"] = probe_cwd
    try:
        r = subprocess.run([shell, "-c", command], input=payload, cwd=probe_cwd,
                           capture_output=True, timeout=timeout, env=env)
        return r.stdout.decode("utf-8", "ignore")
    except Exception:
        return None


def _load_owned_checker():
    """Return (is_owned_fn, source_label). ONE source of truth: import is_abs_owned
    from install-hooks-user-level.py (the installer IS the ownership authority - same
    ABS_FINGERPRINTS / ABS_OWNED_BASENAMES that decide what the installer may relocate).
    Falls back to a path heuristic if that module can't be loaded, and reports which was
    used (a fail-loud-ish breadcrumb, never a silent wrong tag)."""
    here = Path(__file__).resolve().parent
    inst = here / "install-hooks-user-level.py"
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_abs_installer_ownership", inst)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod.is_abs_owned, "installer"
    except Exception:
        return (lambda cmd: "skills/ai-brain-starter/" in cmd), "path-heuristic"


def _basename_of(command: str) -> str:
    """Display label: the last .py/.sh script basename in the command (the hook), else
    the first shell token (an inline-bash injector like `echo '{...}'`)."""
    import os
    m = re.findall(r"([\w.\-]+\.(?:py|sh))", command)
    if m:
        return os.path.basename(m[-1])
    toks = command.strip().split()
    return toks[0] if toks else "<empty>"


# Read-mostly representative tools, preferred when probing a matcher-gated block so a
# WRITE-gated hook is fired with a non-mutating tool where its matcher allows it (defense
# in depth on top of the tool-event cwd sandbox below).
READONLY_TOOLS = ["Read", "Glob", "Grep"]


def measure_live_injection(fleet: dict, events: list[str], env_home: str, is_owned,
                           timeout: int = 10, real_cwd: str | None = None,
                           tool_cwd: str | None = None, shell: str | None = None,
                           on_progress=None) -> dict:
    """Axis D-live (MYC-2396, hardened MYC-2409). For each event in `events`, execute
    every wired hook (deduped by literal command) with a neutral payload and sum injected
    tokens. Returns {event: {total_tokens, unmeasured, hooks: [{command, basename, owned,
    tokens, ran}]}}. Covers owned + unowned + non-.py + per-tool because it runs the
    LITERAL command via the shell, not a resolved repo file.

    cwd policy (the safety boundary): UserPromptSubmit hooks are read-only injectors that
    need the REAL repo cwd so a cwd-conditional injector (a per-repo CONTEXT.md loader) is
    measured, not read as 0. PreToolUse / PostToolUse hooks may WRITE (auto-commit, git
    stash, file writers), so they run in a THROWAWAY `tool_cwd` (default: the sandbox HOME
    dir) - a hook that stages/commits/writes hits the sandbox, never the user's real repo.

    `ran` records whether the hook actually executed (stdout was not None). An unmeasured
    hook (shell missing, timeout, crash) is NOT counted as a clean 0 - the caller surfaces
    it. `shell` is the resolved bash; None -> every hook is unmeasured (caller fails loud)."""
    import os
    if real_cwd is None:
        real_cwd = os.getcwd()
    if tool_cwd is None:
        tool_cwd = env_home  # throwaway: write-hooks can't reach the real repo
    out: dict[str, dict] = {}
    for event in events:
        cwd = real_cwd if event == "UserPromptSubmit" else tool_cwd
        seen: set[str] = set()
        hooks_res: list[dict] = []
        for block in fleet.get(event, []):
            tool = "Write"
            matcher = block.get("matcher")
            if matcher is not None:
                tool = (next((t for t in READONLY_TOOLS if _matcher_matches(matcher, t)), None)
                        or next((t for t in REPRESENTATIVE_TOOLS if _matcher_matches(matcher, t)), None))
                if tool is None:
                    continue
            payload = _neutral_payload(event, tool, cwd)
            for e in block["entries"]:
                cmd = e["command"]
                if cmd in seen:
                    continue
                seen.add(cmd)
                stdout = _run_command_capture(cmd, payload, env_home, cwd, shell, timeout)
                ran = stdout is not None
                tokens = _estimate_tokens(_injected_text(stdout or "", event)) if ran else 0
                bn = _basename_of(cmd)
                hooks_res.append({"command": cmd, "basename": bn,
                                  "owned": bool(is_owned(cmd)), "tokens": tokens, "ran": ran})
                if on_progress:
                    on_progress(event, bn, ran, tokens)
        out[event] = {"total_tokens": sum(h["tokens"] for h in hooks_res),
                      "unmeasured": sum(1 for h in hooks_res if not h["ran"]),
                      "hooks": hooks_res}
    return out


def cmd_measure_live(settings_path: Path, events: list[str], execute: bool,
                     timeout: int) -> int:
    """--measure-live (MYC-2396, hardened MYC-2409): the per-message injected-token cost
    of an install's ACTUAL ~/.claude/settings.json (owned + unowned), with
    relocate-to-SessionStart hints. Advisory; never blocks. Missing settings.json ->
    graceful note + exit 0; an unparseable one raises -> main() exits 2 (loud). When
    --execute cannot run the hooks (no bash / non-POSIX), it FAILS LOUD with structural
    inventory only - never a misleading 'Clean' (MYC-2409)."""
    if not settings_path.exists():
        print(f"No settings file at {settings_path} - nothing to measure.\n"
              f"(--measure-live audits an INSTALL's live ~/.claude/settings.json, where "
              f"the installer merges hooks.json and the user's own injectors live. A "
              f"clean CI box has none; run this on a real install.)")
        return 0
    import os
    real_cwd = os.getcwd()
    fleet = load_fleet(settings_path)  # settings.json shares the {"hooks": {...}} shape
    is_owned, owned_src = _load_owned_checker()
    tool_events = [e for e in events if e in ("PreToolUse", "PostToolUse")]

    print("Live settings.json injection footprint (axis D-live, MYC-2396; ADVISORY)")
    print(f"settings:         {settings_path}")
    print(f"ownership source: {owned_src}\n")
    print("Injector-capable hooks wired per recurring event (owned = ai-brain-starter "
          "substrate; unowned = your own / customized):")
    for event in events:
        n_owned = sum(1 for block in fleet.get(event, []) for e in block["entries"]
                      if is_owned(e["command"]))
        n_total = sum(len(block["entries"]) for block in fleet.get(event, []))
        print(f"    {event:<18} {n_total:>3} hook(s)  ({n_owned} owned, "
              f"{n_total - n_owned} unowned)")

    if not execute:
        print("\nStructural inventory only. Re-run with --execute to run each wired hook "
              "ONCE with a neutral payload and measure the per-message injected tokens.")
        return 0

    # FAIL LOUD, never a misleading 'Clean': if there is no shell to run the hooks
    # (Windows / non-POSIX with no bash), we cannot measure - say so plainly (MYC-2409).
    shell = _resolve_shell()
    if shell is None:
        print("\n!! CANNOT MEASURE: --execute needs a POSIX shell (bash) to run the wired "
              "hooks, and none was found on this platform (e.g. Windows without bash).\n"
              "   This is NOT a clean result - the per-message injection is UNMEASURED. "
              "The structural inventory above is all this platform can report.")
        return 0

    # Honest scope + side-effect disclosure (MYC-2409): UPS runs in your REAL repo (its
    # hooks are read-only injectors); tool-events run in a THROWAWAY dir so any write/git
    # hook can't touch your repo. HOME is sandboxed for all of them.
    print(f"\nProbing with `{shell}`. UserPromptSubmit hooks run in your real repo "
          f"({real_cwd}) so a cwd-conditional injector (CONTEXT.md loader) is measured; "
          f"HOME is redirected to a throwaway dir.")
    if tool_events:
        print(f"  Note: you opted into {tool_events} via --event. Those may include WRITE "
              f"hooks (auto-commit, git stash, file writers) - they run in a THROWAWAY "
              f"working dir (not your repo) so they cannot mutate your work.")

    def _progress(event, bn, ran, tokens):
        mark = f"{tokens}t" if ran else "UNMEASURED(could not run)"
        print(f"  [probe] {event}:{bn} -> {mark}", file=sys.stderr)

    with tempfile.TemporaryDirectory() as home:
        # tool-event sandbox cwd lives under the throwaway HOME (auto-cleaned).
        tool_cwd = home
        res = measure_live_injection(fleet, events, home, is_owned, timeout,
                                     real_cwd=real_cwd, tool_cwd=tool_cwd, shell=shell,
                                     on_progress=_progress)

    per_message_total = 0
    total_unmeasured = 0
    for event in events:
        data = res.get(event, {"total_tokens": 0, "hooks": [], "unmeasured": 0})
        per_msg = event == "UserPromptSubmit"
        cadence = "EVERY MESSAGE" if per_msg else f"every matching {event} call"
        unmeasured = data.get("unmeasured", 0)
        total_unmeasured += unmeasured
        if per_msg:
            per_message_total += data["total_tokens"]
        print(f"\n{event} - injected tokens ({cadence}): {data['total_tokens']}")
        nonzero = sorted((h for h in data["hooks"] if h["tokens"] > 0),
                         key=lambda h: -h["tokens"])
        for h in nonzero:
            tag = "owned/substrate" if h["owned"] else "UNOWNED (your hook)"
            print(f"    {h['basename']}: {h['tokens']} tokens  [{tag}]")
            if per_msg:
                print("      -> stable block injected EVERY MESSAGE. Move it to "
                      "SessionStart (fires once per session-segment -> cached prefix, "
                      "served as cache-reads), not UserPromptSubmit (fresh tokens every "
                      "turn). (MYC-2359 / ADR 0005)")
            else:
                print(f"      -> stable block injected on every {event}. If it is "
                      "prompt/tool-independent, move the stable part to SessionStart; "
                      "if it varies by input, gate it to emit only when relevant.")
        if unmeasured:
            print(f"    !! {unmeasured} hook(s) could not be executed (timeout/crash) - "
                  f"UNMEASURED, not counted as clean.")
        elif not nonzero:
            print("    (every wired hook ran and emitted nothing on a neutral payload - "
                  "no stable block re-injected. Clean.)")
    print(f"\nHeadline: ~{per_message_total} tokens injected EVERY MESSAGE by "
          f"UserPromptSubmit hooks (paid fresh per turn, and per turn x scale). 0 is the "
          f"goal - stable content belongs in the SessionStart cached prefix.")
    if total_unmeasured:
        print(f"WARNING: {total_unmeasured} hook(s) across all events could not be run - "
              f"the numbers above UNDER-count. Investigate (a slow hook hitting the "
              f"{timeout}s timeout, or a broken command) before trusting 'clean'.")
    print("\n(Advisory: execution-based + install/runner-dependent per ADR 0006. CI "
          "gates the LOGIC via --selftest pos/neg controls, not these raw numbers.)")
    return 0


def cmd_measure(hooks_json: Path, hooks_dir: Path, scripts_dir: Path,
                bootstrap: Path, execute: bool, budgets_path: Path) -> int:
    fleet = load_fleet(hooks_json)
    snap = snapshot(fleet, bootstrap)
    print("Footprint measurement (advisory - no pass/fail)\n")
    print("Per-event recurring substrate cold-start fan-out:")
    for ev in NON_MATCHER_EVENTS:
        fo = event_fanout(fleet, ev)
        extra = f"  (!{fo['dead_once']} DEAD once:true - fires every event)" if fo["dead_once"] else ""
        print(f"    {ev:<18} budgeted={fo['budgeted']:>3}  entries={fo['entries']:>3}  "
              f"classes={fo['counts']}{extra}")
    print("\nPer-tool matcher-gated fan-out:")
    for ev in MATCHER_EVENTS:
        for tool in REPRESENTATIVE_TOOLS:
            fo = tool_fanout(fleet, ev, tool)
            if fo["budgeted"] == 0 and sum(fo["classes"].values()) == 0:
                continue
            print(f"    {ev}:{tool:<16} budgeted={fo['budgeted']:>3}  classes={fo['classes']}")
    print(f"\nDefault-on daemons (bootstrap.sh): {snap['daemons']}")

    if not execute:
        print("\n(Advisory timing + injected-bytes skipped. Re-run with --execute to "
              "measure per-hook cold-start ms and per-event felt = MAX. CI does NOT "
              "execute hooks - the gate is the static --gate path.)")
        return 0

    # --- advisory C: per-hook timing + per-event felt = MAX ---
    floor = None
    with tempfile.TemporaryDirectory() as home:
        floor_samples = []
        for _ in range(5):
            t0 = time.perf_counter()
            try:
                subprocess.run([sys.executable, "-c", "pass"], timeout=20,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                break
            floor_samples.append((time.perf_counter() - t0) * 1000.0)
        if floor_samples:
            floor_samples.sort()
            floor = floor_samples[len(floor_samples) // 2]
        print(f"\nAdvisory timing (sandboxed HOME; flaky - NOT gated). "
              f"python3 -c pass floor = {floor:.1f} ms" if floor else
              "\nAdvisory timing unavailable (floor measurement failed).")
        if floor is not None:
            for ev in NON_MATCHER_EVENTS + MATCHER_EVENTS:
                times = []
                seen: set[str] = set()
                for block in fleet.get(ev, []):
                    for e in block["entries"]:
                        klass, bn = classify_command(e["command"])
                        if klass != "substrate-python" or not bn or bn in seen:
                            continue
                        seen.add(bn)
                        p = resolve_substrate(bn, hooks_dir, scripts_dir)
                        if p is None:
                            continue
                        ms = _time_script(p, 3, home)
                        if ms is not None:
                            times.append((bn, ms))
                if times:
                    felt_max = max(t for _, t in times)
                    print(f"    {ev:<18} felt=MAX {felt_max:6.1f} ms over "
                          f"{len(times)} hook(s); slowest "
                          f"{max(times, key=lambda x: x[1])[0]}")
    # --- advisory D: per-message injected tokens from UPS injectors (MYC-2359) ---
    itb = None
    try:
        itb = load_budgets(budgets_path).get("injected_tokens_per_message")
    except Exception:
        pass
    with tempfile.TemporaryDirectory() as home2:
        inj = measure_injected_tokens(fleet, hooks_dir, scripts_dir, home2)
    over = itb is not None and inj["total_tokens"] > itb
    print(f"\nPer-message injected tokens from UPS context-injectors "
          f"(axis D, MYC-2359; neutral prompt, sandboxed HOME):")
    print(f"    total = {inj['total_tokens']} tokens / "
          f"{('-' if itb is None else itb)} budget{'  OVER' if over else ''}")
    nonzero = [(bn, t) for bn, t in inj["per_hook"] if t > 0]
    if nonzero:
        for bn, t in nonzero:
            print(f"      {bn}: {t} tokens - UNCONDITIONAL on a neutral prompt; this "
                  f"stable block belongs on SessionStart (once, cached), not UPS.")
    else:
        print("      (every UPS substrate hook emits nothing on a neutral prompt - "
              "stable injectors live on SessionStart, served as cache-reads.)")
    print("\n(Axis C timing + axis D tokens are ADVISORY - install / runner "
          "dependent. CI gates the LOGIC via --selftest pos/neg controls, not these "
          "raw numbers.)")
    print("\nThis measured the SHIPPED hooks.json template. To measure your ACTUAL "
          "~/.claude/settings.json - including your OWN + customized injectors and "
          "non-.py / per-tool hooks the template gate never sees - run:\n"
          "    footprint-sla-check.py --measure-live --execute   (MYC-2396)")
    return 0


# ---- update-budgets (maintainer) ---------------------------------------------
def cmd_update_budgets(hooks_json: Path, bootstrap: Path, budgets_path: Path,
                       headroom: int) -> int:
    fleet = load_fleet(hooks_json)
    snap = snapshot(fleet, bootstrap)
    # Preserve the existing injected-token ceiling (a fixed advisory value, not a
    # ratchet) so a fan-out regen never silently resets it.
    ceiling = 100
    try:
        existing = load_budgets(budgets_path).get("injected_tokens_per_message")
        if isinstance(existing, int):
            ceiling = existing
    except Exception:
        pass
    budgets = build_budgets(snap, headroom, ceiling)
    budgets_path.write_text(json.dumps(budgets, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {budgets_path} (headroom={headroom}).")
    print(f"  per-event budgets: {budgets['fanout_per_event']}")
    print(f"  per-tool budgets:  {budgets['fanout_per_tool']}")
    print(f"  default_on_daemons: {budgets['default_on_daemons']}")
    print(f"  injected_tokens_per_message: {budgets['injected_tokens_per_message']}")
    return 0


# ---- selftest (positive + negative controls) ----------------------------------
_EVIL_HOOK = "python3 ~/.claude/skills/ai-brain-starter/hooks/evil-{i}.py 2>/dev/null || echo '{{}}'"


def _write_synthetic(d: Path, write_hooks: int, daemons: int) -> tuple[Path, Path]:
    """A synthetic hooks.json wiring `write_hooks` substrate-python hooks on Write,
    and a bootstrap.sh with `daemons` launchctl activations."""
    hj = d / "hooks.json"
    entries = [{"type": "command", "command": _EVIL_HOOK.format(i=i)}
               for i in range(write_hooks)]
    hj.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Write|Edit", "hooks": entries}]}}), encoding="utf-8")
    bs = d / "bootstrap.sh"
    bs.write_text("#!/usr/bin/env bash\n" +
                  "".join(f"launchctl load ~/Library/LaunchAgents/d{i}.plist\n"
                          for i in range(daemons)), encoding="utf-8")
    return hj, bs


def cmd_selftest() -> int:
    fails: list[str] = []

    # 0. classification unit checks - each known command form maps to its class.
    cases = [
        ("python3 ~/.claude/skills/ai-brain-starter/hooks/x.py 2>/dev/null || echo '{}'",
         "substrate-python", "x.py"),
        ("python3 ~/.claude/skills/ai-brain-starter/scripts/y.py 2>/dev/null || echo '{}'",
         "substrate-python", "y.py"),
        ("python3 '[VAULT_PATH]/.claude/skills/ai-brain-starter/hooks/z.py' || "
         "python3 ~/.claude/skills/ai-brain-starter/hooks/z.py || echo '{}'",
         "substrate-python", "z.py"),               # fallback form -> still substrate
        ("[ -f ~/.claude/hooks/retry-budget.py ] && python3 ~/.claude/hooks/retry-budget.py || true",
         "userlevel-guarded", None),
        ("bash '[VAULT_PATH]/⚙️ Meta/scripts/write-hook.sh'", "vault-script", None),
        ("if [ -f ~/.claude/.pinned ]; then echo '{}'; fi", "inline-bash", None),
    ]
    for cmd, want_k, want_bn in cases:
        k, bn = classify_command(cmd)
        if k != want_k or bn != want_bn:
            fails.append(f"classify {cmd[:40]!r}: got ({k},{bn}) want ({want_k},{want_bn})")

    # matcher matching
    if not _matcher_matches("Write|Edit|MultiEdit", "Write"):
        fails.append("matcher alternation should match Write")
    if not _matcher_matches("mcp__.*", "mcp__server__tool"):
        fails.append("matcher mcp__.* should match an mcp tool")
    if _matcher_matches("Bash", "Write"):
        fails.append("matcher Bash should NOT match Write")
    if _matcher_matches(None, "Write"):
        fails.append("None matcher should match no tool")

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        hooks_dir = d / "hooks"
        hooks_dir.mkdir()
        scripts_dir = d / "scripts"
        scripts_dir.mkdir()

        # 1. NEGATIVE CONTROL: a 30-hook Write fan-out vs a budget of 7 -> --gate BITES.
        hj, bs = _write_synthetic(d, write_hooks=30, daemons=0)
        budgets = {"fanout_per_event": {ev: 50 for ev in NON_MATCHER_EVENTS},
                   "fanout_per_tool": {"PreToolUse": {"Write": 7, "Edit": 7},
                                       "PostToolUse": {}},
                   "default_on_daemons": 0}
        bp = d / "footprint-budgets.json"
        bp.write_text(json.dumps(budgets), encoding="utf-8")
        rc = cmd_gate(hj, hooks_dir, scripts_dir, bp, bs, as_json=True)
        if rc != 1:
            fails.append(f"NEG: 30-hook Write fan-out vs budget 7 should exit 1, got {rc}")

        # 2. POSITIVE CONTROL: a 3-hook Write fan-out vs budget 7 -> --gate passes.
        hj2, bs2 = _write_synthetic(d, write_hooks=3, daemons=0)
        rc = cmd_gate(hj2, hooks_dir, scripts_dir, bp, bs2, as_json=True)
        if rc != 0:
            fails.append(f"POS: 3-hook Write fan-out vs budget 7 should exit 0, got {rc}")

        # 3. DAEMON NEGATIVE CONTROL: a default-on daemon vs budget 0 -> BITES.
        hj3, bs3 = _write_synthetic(d, write_hooks=3, daemons=2)
        rc = cmd_gate(hj3, hooks_dir, scripts_dir, bp, bs3, as_json=True)
        if rc != 1:
            fails.append(f"DAEMON-NEG: 2 default-on daemons vs budget 0 should exit 1, got {rc}")

        # 4. FAIL-LOUD: a missing budgets file -> cmd_gate raises -> caller exits 2.
        try:
            cmd_gate(hj2, hooks_dir, scripts_dir, d / "nope.json", bs2, as_json=True)
            fails.append("FAIL-LOUD: missing budgets file did not raise")
        except Exception:
            pass

        # 5. FAIL-LOUD: a missing hooks.json -> raises (fail loud, not empty-pass).
        try:
            cmd_gate(d / "nope-hooks.json", hooks_dir, scripts_dir, bp, bs2, as_json=True)
            fails.append("FAIL-LOUD: missing hooks.json did not raise")
        except Exception:
            pass

        # 6. NO-BUDGET BREACH: a measured tool with no budget entry is a breach
        #    (a new surface must be governed deliberately, never silently ungoverned).
        budgets_nobudget = {"fanout_per_event": {ev: 50 for ev in NON_MATCHER_EVENTS},
                            "fanout_per_tool": {"PreToolUse": {}, "PostToolUse": {}},
                            "default_on_daemons": 0}
        bp2 = d / "budgets-nobudget.json"
        bp2.write_text(json.dumps(budgets_nobudget), encoding="utf-8")
        rc = cmd_gate(hj2, hooks_dir, scripts_dir, bp2, bs2, as_json=True)
        if rc != 1:
            fails.append(f"NO-BUDGET: ungoverned Write fan-out should exit 1, got {rc}")

        # 7. DEAD-ONCE NEGATIVE CONTROL (MYC-2359): a once:true substrate hook on
        #    UserPromptSubmit is a DEAD flag in settings.json -> --gate BITES.
        #    Uses a dedicated clean (0-daemon) bootstrap so the ONLY breach is the
        #    dead-once flag (the synthetic bootstraps above share one path).
        bs_clean = d / "bootstrap-clean.sh"
        bs_clean.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        budgets_ok = {"fanout_per_event": {ev: 50 for ev in NON_MATCHER_EVENTS},
                      "fanout_per_tool": {"PreToolUse": {}, "PostToolUse": {}},
                      "default_on_daemons": 0}
        bp3 = d / "budgets-ok.json"
        bp3.write_text(json.dumps(budgets_ok), encoding="utf-8")
        ups_cmd = "python3 ~/.claude/skills/ai-brain-starter/hooks/inj.py 2>/dev/null || echo '{}'"
        hj_once = d / "hooks-once.json"
        hj_once.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": ups_cmd, "once": True}]}]}}),
            encoding="utf-8")
        rc = cmd_gate(hj_once, hooks_dir, scripts_dir, bp3, bs_clean, as_json=True)
        if rc != 1:
            fails.append(f"DEAD-ONCE-NEG: once:true UPS hook should exit 1, got {rc}")

        # 7b. DEAD-ONCE POSITIVE CONTROL: the SAME hook without once -> clean (the
        #     once flag, not the hook, is what trips the breach).
        hj_noonce = d / "hooks-noonce.json"
        hj_noonce.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": ups_cmd}]}]}}), encoding="utf-8")
        rc = cmd_gate(hj_noonce, hooks_dir, scripts_dir, bp3, bs_clean, as_json=True)
        if rc != 0:
            fails.append(f"DEAD-ONCE-POS: non-once UPS hook within budget should exit 0, got {rc}")

        # 8. AXIS D (MYC-2359) pos/neg: measure_injected_tokens scores an
        #    UNCONDITIONAL UPS injector at its full block, a CONDITIONAL one at 0 on
        #    a neutral prompt (it belongs on UPS; the unconditional one belongs on
        #    SessionStart, cached).
        (hooks_dir / "uncond.py").write_text(
            "import json\n"
            "print(json.dumps({'hookSpecificOutput': {'hookEventName': "
            "'UserPromptSubmit', 'additionalContext': 'X' * 4000}}))\n",
            encoding="utf-8")
        (hooks_dir / "cond.py").write_text(
            "import sys, json\n"
            "try:\n"
            "    pr = json.loads(sys.stdin.read()).get('prompt', '')\n"
            "except Exception:\n"
            "    pr = ''\n"
            "if 'magic-trigger' in pr:\n"
            "    print(json.dumps({'hookSpecificOutput': {'hookEventName': "
            "'UserPromptSubmit', 'additionalContext': 'Y' * 4000}}))\n"
            "else:\n"
            "    print(json.dumps({'continue': True, 'suppressOutput': True}))\n",
            encoding="utf-8")
        hj_inj = d / "hooks-inj.json"
        hj_inj.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [
            {"type": "command", "command": "python3 ~/.claude/skills/ai-brain-starter/hooks/uncond.py"},
            {"type": "command", "command": "python3 ~/.claude/skills/ai-brain-starter/hooks/cond.py"},
        ]}]}}), encoding="utf-8")
        with tempfile.TemporaryDirectory() as home_inj:
            inj = measure_injected_tokens(load_fleet(hj_inj), hooks_dir, scripts_dir,
                                          home_inj, prompt="ping")
        upm = dict(inj["per_hook"])
        if upm.get("uncond.py", 0) < 500:
            fails.append(f"AXIS-D-NEG: unconditional injector should measure >500 tokens, "
                         f"got {upm.get('uncond.py')}")
        if upm.get("cond.py", -1) != 0:
            fails.append(f"AXIS-D-POS: conditional injector should measure 0 on a neutral "
                         f"prompt, got {upm.get('cond.py')}")
        if inj["total_tokens"] <= 100:
            fails.append(f"AXIS-D: an unconditional block should exceed the 100-token "
                         f"ceiling, got total {inj['total_tokens']}")

        # 9. AXIS D-LIVE (MYC-2396) pos/neg: measure the LIVE settings.json path, which
        #    executes the LITERAL wired command via the shell. Proves coverage the
        #    template-only axis D lacks: UNOWNED hooks, non-.py (inline-bash) injectors,
        #    and per-tool (PreToolUse) injectors are all measured; a conditional hook and
        #    a JSON no-op score 0; the ownership tag loads from its single source of truth.
        is_owned_fn, owned_src = _load_owned_checker()
        # 9a. ownership: single source of truth (the installer) loads, and tags owned vs
        #     unowned correctly. A silent fallback to the heuristic is itself a failure.
        if owned_src != "installer":
            fails.append(f"AXIS-D-LIVE ownership: expected the installer as the single "
                         f"source of truth, got '{owned_src}' (install-hooks-user-level.py "
                         f"not importable / is_abs_owned renamed?)")
        if not is_owned_fn("python3 ~/.claude/skills/ai-brain-starter/hooks/log-skill-usage.py"):
            fails.append("AXIS-D-LIVE ownership: a substrate hook should tag as owned")
        if is_owned_fn("python3 ~/.claude/hooks/my-own-injector.py 2>/dev/null"):
            fails.append("AXIS-D-LIVE ownership: a user's own hook should tag as UNOWNED")
        # 9b. measurement: a synthetic live settings.json with one of each injector form.
        (d / "live_uncond.py").write_text(
            "import json\n"
            "print(json.dumps({'hookSpecificOutput': {'hookEventName': "
            "'UserPromptSubmit', 'additionalContext': 'U' * 4000}}))\n", encoding="utf-8")
        (d / "live_cond.py").write_text(
            "import sys, json\n"
            "try:\n    pr = json.loads(sys.stdin.read()).get('prompt', '')\n"
            "except Exception:\n    pr = ''\n"
            "if 'magic-trigger' in pr:\n"
            "    print(json.dumps({'hookSpecificOutput': {'hookEventName': "
            "'UserPromptSubmit', 'additionalContext': 'Y' * 4000}}))\n"
            "else:\n    print(json.dumps({'continue': True, 'suppressOutput': True}))\n",
            encoding="utf-8")
        (d / "live_pre.py").write_text(
            "import json\n"
            "print(json.dumps({'hookSpecificOutput': {'hookEventName': "
            "'PreToolUse', 'additionalContext': 'P' * 2000}}))\n", encoding="utf-8")
        uncond_cmd = f"python3 {d / 'live_uncond.py'}"          # owned? no -> UNOWNED
        cond_cmd = f"python3 {d / 'live_cond.py'} 2>/dev/null"  # conditional -> 0
        # a non-.py inline-bash injector: the exact form (vault-script / inline echo) the
        # template axis D skips (klass != substrate-python). It MUST still be measured.
        bash_inj_cmd = ("echo '{\"hookSpecificOutput\": {\"hookEventName\": "
                        "\"UserPromptSubmit\", \"additionalContext\": \"" + "B" * 1200 + "\"}}'")
        noop_cmd = "echo '{\"continue\": true, \"suppressOutput\": true}'"
        pre_cmd = f"python3 {d / 'live_pre.py'}"
        live_settings = d / "live-settings.json"
        live_settings.write_text(json.dumps({"hooks": {
            "UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": uncond_cmd},
                {"type": "command", "command": cond_cmd},
                {"type": "command", "command": bash_inj_cmd},
                {"type": "command", "command": noop_cmd},
            ]}],
            "PreToolUse": [{"matcher": "Write|Edit", "hooks": [
                {"type": "command", "command": pre_cmd},
            ]}],
        }}), encoding="utf-8")
        live_shell = _resolve_shell()
        with tempfile.TemporaryDirectory() as home_live:
            live = measure_live_injection(load_fleet(live_settings),
                                          ["UserPromptSubmit", "PreToolUse"],
                                          home_live, is_owned_fn, shell=live_shell)
        ups = {h["command"]: h for h in live["UserPromptSubmit"]["hooks"]}
        pre = {h["command"]: h for h in live["PreToolUse"]["hooks"]}
        if ups.get(uncond_cmd, {}).get("tokens", 0) < 500:
            fails.append(f"AXIS-D-LIVE-NEG: unconditional UPS injector should measure "
                         f">500 tokens, got {ups.get(uncond_cmd, {}).get('tokens')}")
        if ups.get(cond_cmd, {}).get("tokens", -1) != 0:
            fails.append(f"AXIS-D-LIVE-POS: conditional UPS injector should measure 0 on a "
                         f"neutral prompt, got {ups.get(cond_cmd, {}).get('tokens')}")
        if ups.get(bash_inj_cmd, {}).get("tokens", 0) < 200:
            fails.append(f"AXIS-D-LIVE non-.py: an inline-bash injector should be measured "
                         f"(>200 tokens), got {ups.get(bash_inj_cmd, {}).get('tokens')} - the "
                         f"template axis D skips this form")
        if ups.get(noop_cmd, {}).get("tokens", -1) != 0:
            fails.append(f"AXIS-D-LIVE: a JSON no-op should measure 0, got "
                         f"{ups.get(noop_cmd, {}).get('tokens')}")
        if pre.get(pre_cmd, {}).get("tokens", 0) < 200:
            fails.append(f"AXIS-D-LIVE per-tool: a PreToolUse injector should be measured "
                         f"(>200 tokens), got {pre.get(pre_cmd, {}).get('tokens')} - the "
                         f"template axis D only covers UserPromptSubmit")
        # unowned coverage: the synthetic hooks contain no substrate path -> UNOWNED, yet
        # they are measured. (The template axis D would never reach an unowned hook.)
        if not any(h["tokens"] > 0 and not h["owned"]
                   for h in live["UserPromptSubmit"]["hooks"]):
            fails.append("AXIS-D-LIVE unowned: an UNOWNED injector should be measured "
                         "(measurement must not filter by ownership)")
        # 9c. FAIL-LOUD NEGATIVE CONTROL (MYC-2409): when the shell is missing (the
        #     Windows / non-POSIX case), EVERY hook must come back ran=False / UNMEASURED -
        #     NOT a clean 0. This is the guard that proves the silent-false-negative is
        #     closed: a bogus shell path makes _run_command_capture return None for the
        #     SAME unconditional injector that scored high above.
        with tempfile.TemporaryDirectory() as home_noshell:
            noshell = measure_live_injection(load_fleet(live_settings), ["UserPromptSubmit"],
                                             home_noshell, is_owned_fn,
                                             shell="/nonexistent/bash-not-here")
        ns = noshell["UserPromptSubmit"]
        if ns.get("unmeasured", 0) != len(ns["hooks"]) or any(h["ran"] for h in ns["hooks"]):
            fails.append(f"AXIS-D-LIVE-FAILLOUD: with no shell, every hook must be "
                         f"UNMEASURED (ran=False); got unmeasured={ns.get('unmeasured')} of "
                         f"{len(ns['hooks'])}, any-ran={any(h['ran'] for h in ns['hooks'])}")
        if ns["total_tokens"] != 0 or any(h["tokens"] for h in ns["hooks"]):
            fails.append("AXIS-D-LIVE-FAILLOUD: an unmeasured hook must contribute 0 tokens "
                         "AND be flagged unmeasured (never a silent clean 0)")
        # _resolve_shell finds bash on this POSIX CI box (so the happy path above ran).
        if live_shell is None:
            fails.append("AXIS-D-LIVE: _resolve_shell found no bash on this box - the "
                         "measurement happy path could not run (selftest environment issue)")

    if fails:
        print("SELFTEST FAIL:")
        for f in fails:
            print("  - " + f)
        return 1
    print("SELFTEST PASS: gate bites over-budget fan-out + default-on daemons + "
          "ungoverned surfaces + DEAD once:true flags (MYC-2359), passes "
          "within-budget, fails loud on missing hooks.json/budgets; axis-D "
          "injected-token measurement scores an unconditional UPS injector high and "
          "a conditional one at 0; axis-D-LIVE (MYC-2396) measures unowned + non-.py "
          "(inline-bash) + per-tool injectors and scores a no-op/conditional at 0, "
          "ownership tag from its single source of truth; axis-D-LIVE FAILS LOUD with no "
          "shell (every hook UNMEASURED, never a silent clean 0 - MYC-2409); "
          "classification + matcher logic correct.")
    return 0


# ---- main ---------------------------------------------------------------------
def main() -> int:
    here = Path(__file__).resolve().parent
    repo = here.parent
    ap = argparse.ArgumentParser(description="Footprint SLA gate for the hook fleet (MYC-2358).")
    ap.add_argument("--gate", action="store_true", help="CI gate: fan-out + daemons vs budgets")
    ap.add_argument("--measure", action="store_true", help="advisory report (no pass/fail)")
    ap.add_argument("--measure-live", dest="measure_live", action="store_true",
                    help="(MYC-2396) advisory: per-message injected tokens of the LIVE "
                         "~/.claude/settings.json (owned + unowned + non-.py + per-tool)")
    ap.add_argument("--execute", action="store_true",
                    help="(with --measure) time hooks; (with --measure-live) run each "
                         "wired hook once with a neutral payload to measure injected tokens")
    ap.add_argument("--selftest", action="store_true", help="positive + negative controls")
    ap.add_argument("--update-budgets", action="store_true", help="(maintainer) rewrite budgets")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--headroom", type=int, default=2, help="budget = measured + headroom")
    ap.add_argument("--hooks-json", default=str(repo / "hooks.json"))
    ap.add_argument("--hooks-dir", default=str(repo / "hooks"))
    ap.add_argument("--scripts-dir", default=str(repo / "scripts"))
    ap.add_argument("--bootstrap", default=str(repo / "bootstrap.sh"))
    ap.add_argument("--budgets", default=str(repo / "footprint-budgets.json"))
    ap.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"),
                    help="(--measure-live) the live settings.json to audit")
    ap.add_argument("--event", default="UserPromptSubmit",
                    help="(--measure-live) 'all' or a comma list of "
                         f"{'/'.join(LIVE_EVENTS)} (default: UserPromptSubmit - the safe "
                         f"per-message headline; 'all' also probes the tool-WRITE events, "
                         f"which run in a throwaway dir)")
    ap.add_argument("--timeout", type=int, default=10,
                    help="(--measure-live --execute) per-hook execution timeout, seconds")
    a = ap.parse_args()

    # --measure-live event selection (validated against the injector-capable set).
    if a.event.strip().lower() == "all":
        live_events = list(LIVE_EVENTS)
    else:
        live_events = [e.strip() for e in a.event.split(",") if e.strip()]
        bad = [e for e in live_events if e not in LIVE_EVENTS]
        if bad:
            print(f"[footprint-sla] --event: unknown event(s) {bad}; "
                  f"valid: {LIVE_EVENTS} or 'all'", file=sys.stderr)
            return 2

    # --gate / --selftest / --update-budgets are gates: fail LOUD (exit 2) on any
    # internal error so a broken gate can never silently pass CI.
    try:
        if a.selftest:
            return cmd_selftest()
        if a.update_budgets:
            return cmd_update_budgets(Path(a.hooks_json), Path(a.bootstrap),
                                      Path(a.budgets), a.headroom)
        if a.measure_live:
            return cmd_measure_live(Path(a.settings), live_events, a.execute, a.timeout)
        if a.measure:
            return cmd_measure(Path(a.hooks_json), Path(a.hooks_dir),
                               Path(a.scripts_dir), Path(a.bootstrap), a.execute,
                               Path(a.budgets))
        if a.gate:
            return cmd_gate(Path(a.hooks_json), Path(a.hooks_dir), Path(a.scripts_dir),
                            Path(a.budgets), Path(a.bootstrap), a.json)
    except Exception as e:
        print(f"[footprint-sla] FATAL: {e}", file=sys.stderr)
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
