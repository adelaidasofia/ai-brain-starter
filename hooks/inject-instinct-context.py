#!/usr/bin/env python3
"""
inject-instinct-context.py — SessionStart hook (once per session-segment).

Realizes the project-scoping half of the Instinct Engine: at session start,
load the high-confidence instincts whose `project_id` is the CURRENT project
OR `global`, and EXCLUDE instincts scoped to other projects. That exclusion is
the isolation feature — a repo-specific convention does not bleed into
unrelated work.

Wired on SessionStart, NOT UserPromptSubmit: the selection is prompt-INDEPENDENT
(stdin is only read for the session id below), so it is session-stable and must
be injected ONCE, not per message. `once: true` is ignored in settings.json (the
installer's merge target), so a UPS `once` hook silently re-fires every message —
this block was measured re-injecting 14x in one session (MYC-2359). SessionStart
fires once per session-segment (startup / resume / post-compact), landing the
block in the cached prefix → served as cache-reads thereafter, not fresh tokens
every turn.

TWO THINGS THIS HOOK DOES BEYOND RENDERING THE BLOCK:

1. **It writes an injection ledger.** The selection computed here is the only
   deterministic record that an instinct was actually PUT TO WORK, and it used
   to be discarded — leaving `confidence` with no feedback channel at all, so
   every stored number stayed the seed it was born with. One appended line per
   session-segment is what `instinct.py promote` later reads. This is the
   substrate's analog of the shipped runtime learning loop's retrieval CITATION
   signal (MYC-818 / MYC-916).

2. **It reserves EXPLORE slots.** Ranking purely by confidence is a closed
   loop: only injected instincts can earn exposures, only exposed instincts can
   be promoted, so the top-N freezes and the rest of the library can never
   acquire evidence no matter how good it is. A minority of the budget goes to
   in-scope instincts BELOW the floor with the fewest exposures, rotated by
   session id so different sessions sample different candidates. Explore picks
   are LABELLED in the block — an unproven instinct must not read like a
   confirmed one.

Silent if the engine isn't installed or nothing clears the confidence floor.
Fail-open: any error -> neutral passthrough, never blocks the prompt. The
ledger write is best-effort and can never fail the injection.

Tunables (env):
  INSTINCT_INJECT_MIN_CONFIDENCE  default 0.80
  INSTINCT_INJECT_LIMIT           default 12
  INSTINCT_INJECT_EXPLORE         default 3    (0 disables exploration)
  INSTINCT_INJECTIONS             default ~/.claude/instinct/injections.jsonl
"""
from __future__ import annotations

import json
import os
import sys
import zlib
from datetime import datetime, timezone

PASS = '{"continue": true, "suppressOutput": true}'
SCRIPTS = os.environ.get(
    "INSTINCT_SCRIPTS_DIR",
    os.path.expanduser("~/.claude/skills/ai-brain-starter/scripts"),
)
MIN_CONF = float(os.environ.get("INSTINCT_INJECT_MIN_CONFIDENCE", "0.80"))
LIMIT = int(os.environ.get("INSTINCT_INJECT_LIMIT", "12"))
EXPLORE = int(os.environ.get("INSTINCT_INJECT_EXPLORE", "3"))
# How many of the least-exercised candidates the explore rotation draws
# from. Bounded so the bias toward never-exercised instincts survives.
EXPLORE_POOL = int(os.environ.get("INSTINCT_INJECT_EXPLORE_POOL", "24"))
INJECTIONS_PATH = os.environ.get(
    "INSTINCT_INJECTIONS",
    os.path.join(os.path.expanduser("~"), ".claude", "instinct", "injections.jsonl"),
)


def _session_id(payload: str) -> str:
    """Best-effort session id from the SessionStart payload; '' if absent."""
    try:
        obj = json.loads(payload)
        if isinstance(obj, dict):
            return str(obj.get("session_id") or obj.get("sessionId") or "")[:8]
    except Exception:
        pass
    return ""


def _record(session: str, project: str, exploit: list, explore: list) -> None:
    """Append one injection record. Best-effort: never raises to the caller."""
    try:
        os.makedirs(os.path.dirname(INJECTIONS_PATH), exist_ok=True)
        line = json.dumps({
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session": session,
            "project": project,
            "floor": MIN_CONF,
            # STEM, not the display name: the stem is the stable file
            # identity `promote` resolves against. `name:` is free text and
            # is absent on most memories.
            "injected": [r[3] for r in exploit],
            "explored": [r[3] for r in explore],
        }, ensure_ascii=False)
        with open(INJECTIONS_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass  # a ledger must never degrade the thing it observes


def main() -> None:
    payload = ""
    try:
        payload = sys.stdin.read()
    except Exception:
        pass
    session = _session_id(payload)
    try:
        sys.path.insert(0, SCRIPTS)
        import instinct_lib as il
    except Exception:
        print(PASS)
        return
    try:
        md = il.resolve_memory_dir()
        if not md:
            print(PASS)
            return
        today = datetime.now(timezone.utc).date()
        proj = il.current_project_id()
        over, under = [], []
        for p in il.iter_instinct_paths(md):
            inst = il.parse_instinct(p)
            fm = inst.fm
            pid = fm.get("project_id", il.PROJECT_GLOBAL)
            if pid not in (proj, il.PROJECT_GLOBAL):
                continue  # ISOLATION: other-project instincts excluded
            c = il.parse_float(fm.get("confidence"), il.seed_confidence(fm.get("strength")))
            ls = il.parse_date(fm.get("last_seen")) or il.file_mtime_date(p)
            eff = il.decayed_confidence(c, ls, today)
            shown = fm.get("name", inst.slug)
            if eff >= MIN_CONF:
                over.append((round(eff, 2), pid, shown, inst.slug))
            else:
                under.append((il.parse_int(fm.get("exposures"), 0),
                              round(eff, 2), pid, shown, inst.slug))

        n_explore = max(0, min(EXPLORE, LIMIT - 1)) if under else 0
        over.sort(reverse=True)
        exploit = over[: LIMIT - n_explore]

        explore = []
        if n_explore:
            under.sort(key=lambda r: (r[0], r[4]))
            # Rotate WITHIN a bounded prefix, not the whole list. Rotating
            # everything makes the sort inert -- measured flat across all
            # ranks, so a never-exercised instinct was sampled no more often
            # than a well-exercised one, and "least-exercised first" described
            # a sort the next line threw away. The prefix keeps the bias; the
            # rotation inside it keeps different sessions sampling different
            # candidates.
            pool = under[: min(len(under), max(n_explore * 8, EXPLORE_POOL))]
            # crc32, NOT hash(): str hashing is salted per-process
            # (PYTHONHASHSEED), so hash() would make the rotation differ
            # between two runs of the SAME session and untestable.
            off = (zlib.crc32(session.encode()) % len(pool)) if session else 0
            rotated = pool[off:] + pool[:off]
            explore = [(r[1], r[2], r[3], r[4]) for r in rotated[:n_explore]]

        if not exploit and not explore:
            print(PASS)
            return
        _record(session, proj, exploit, explore)

        lines = [f"[instinct-engine] High-confidence instincts in scope "
                 f"(project={proj}; project-scoped + global only, "
                 f">= {MIN_CONF:.2f}):"]
        for eff, pid, name, _slug in exploit:
            tag = "" if pid == il.PROJECT_GLOBAL else f" [{pid}]"
            lines.append(f"- ({eff:.2f}) {name}{tag}")
        if explore:
            lines.append("Under evaluation (below the floor, least-exercised — "
                         "unproven, weigh accordingly):")
            for eff, pid, name, _slug in explore:
                tag = "" if pid == il.PROJECT_GLOBAL else f" [{pid}]"
                lines.append(f"- ({eff:.2f}) {name}{tag}")
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        }}))
    except Exception:
        print(PASS)


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): a non-ASCII instinct name must not
    # crash the hook on encode. Fail-open applies to output too.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
