#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
context-budget-measure.py — SessionStart guard: measure the always-loaded TEXT
layer and warn when it grows.

The always-loaded text layer — global ~/.claude/CLAUDE.md + the project's
CLAUDE.md + MEMORY.md + the project's CONTEXT.md — is paid in tokens on EVERY
turn of EVERY session, forever. It is the most expensive context in the system
and the one with no measurement guard: plugin / MCP load is measured
(token-economics), MEMORY.md has its own cliff guard, but the CLAUDE.md kernel
files themselves silently grow until a new session opens already-full.

This hook measures that layer each SessionStart and:
  • warns if a file exceeds its hard ceiling (default: global CLAUDE.md > 40 KB),
  • warns if the TOTAL grew past a stored baseline (drift detection),
  • silently RATCHETS the baseline DOWN when the layer shrinks (records new low),
  • stays silent when healthy.

Ratchet semantics: baseline = the lowest total seen (the floor). Growth above
floor + tolerance warns and does NOT move the floor — it keeps surfacing until
you slim back, OR run `--accept` to acknowledge an intentional add as the new
floor. A shrink updates the floor silently. This is a measure-and-warn ratchet,
NOT a governance framework — match the guard to the recurrence rate.

Scope safety (the floor must reflect a real project session, not whatever cwd
the command happened to run in). `discover()` finds the project CLAUDE.md /
MEMORY.md / CONTEXT.md by walking up from cwd; run from a NON-project cwd (e.g.
a bare home dir) it finds ONLY the global kernel, so a naive `--accept` / silent
ratchet there would record a global-only floor that clobbers a real multi-file
floor and silently kills drift detection. Two guards prevent that:
  • a baseline is recorded ONLY from a project-anchored discovery (at least one
    project-scoped file present) — `--accept` from a non-project cwd refuses
    fail-loud + exits nonzero; the SessionStart ratchet just skips,
  • the recorded baseline persists its file-KIND set, so a later discovery that
    is a strict SUBSET of those kinds (a narrower scope) never silently ratchets
    the floor down.
Bug class: GUARD-RECORDS-BASELINE-FROM-WRONG-SCOPE (cwd-relative-fragility).

Fail-open: a SessionStart nudge must NEVER crash-block a session.
Freq-capped: warns at most once per day.
Bypass:  CONTEXT_BUDGET_BYPASS=1
Accept an intentional growth as the new floor:  context-budget-measure.py --accept
Tune ceilings (bytes):  CONTEXT_BUDGET_GLOBAL_CEILING (default 40000)

Modes:
  (no args, reads stdin JSON)  SessionStart hook mode — emits additionalContext or no-ops
  --report                     print the table to stdout (human / CLI)
  --accept                     set the baseline floor to the current total
                               (refuses + exits nonzero from a non-project cwd)
  --self-test                  positive + negative control; exit 0 iff all pass

Bug class: ALWAYS-LOADED-TEXT-UNMEASURED (sibling of ARTIFACT-WITHOUT-MEASUREMENT).
MEMORY.md remediation is owned by session-start-context.py — this hook measures
MEMORY.md for the total but does NOT duplicate its cliff warning.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

# --- tunables (env-overridable) ------------------------------------------------
GLOBAL_CEILING = int(os.environ.get("CONTEXT_BUDGET_GLOBAL_CEILING", 40000))
# growth tolerance: absorb normal small adds, catch real drift
TOL_BYTES = int(os.environ.get("CONTEXT_BUDGET_TOL_BYTES", 4096))
TOL_FRAC = float(os.environ.get("CONTEXT_BUDGET_TOL_FRAC", 0.02))

# The global kernel is loaded from a fixed path regardless of cwd, so it is the
# one file that is ALWAYS discoverable. Any OTHER kind means we found a real
# project — see is_project_anchored().
GLOBAL_KIND = "global CLAUDE.md"

HOME = Path.home()
BASELINE_PATH = HOME / ".claude" / ".context-budget-baseline.json"
LASTWARN_PATH = HOME / ".claude" / ".context-budget-last-warn"

HOOK_NAME = "context-budget-measure"

# Guard-fleet telemetry: emit ONE fire per run so fleet telemetry can tell this
# guard is ALIVE, not silently dead. The drift/ratchet half of this very guard
# was silently dead before the scope fix — and a guard whose healthy path is
# silent reads as UNINSTRUMENTED ("cannot conclude dead"). Logging every run
# (incl. the healthy ratchet) moves it to FIRING, so a future silent-death shows
# up as a fire-count drop instead of going unnoticed. Fail-open: any install
# without the telemetry lib (e.g. a public install) degrades to a no-op; the
# emit must never raise or change the guard's behavior.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "_lib"))
    from guard_telemetry import log_fire
except Exception:
    def log_fire(*_a, **_k):
        return


def noop():
    print(json.dumps({"continue": True, "suppressOutput": True}))
    sys.exit(0)


# --- file discovery (generic, name-free) --------------------------------------
def _memory_md(cwd: str) -> Path | None:
    """Locate MEMORY.md the way Claude Code loads it (sanitized-cwd projects dir),
    falling back to the in-vault canonical path. Mirrors session-start-context.py."""
    cands: list[Path] = []
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        cands.append(Path(env) / "⚙️ Meta/Agent Memory/MEMORY.md")
    try:
        san = "-" + str(Path(cwd)).replace("/", "-")
        cands.append(HOME / ".claude/projects" / san / "memory/MEMORY.md")
        m = re.match(r"(.*)--claude-worktrees-[^/]+$", san)
        if m:
            cands.append(HOME / ".claude/projects" / m.group(1) / "memory/MEMORY.md")
    except Exception:
        pass
    try:
        cur = Path(cwd)
        for _ in range(10):
            cands.append(cur / "⚙️ Meta/Agent Memory/MEMORY.md")
            if cur.parent == cur:
                break
            cur = cur.parent
    except Exception:
        pass
    for c in cands:
        try:
            if c.is_file():
                return c
        except Exception:
            continue
    return None


def _project_file(cwd: str, name: str) -> Path | None:
    """Find <name> (CLAUDE.md / CONTEXT.md) at the project root: CLAUDE_PROJECT_DIR
    first, else walk up from cwd."""
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        p = Path(env) / name
        try:
            if p.is_file():
                return p
        except Exception:
            pass
    try:
        cur = Path(cwd)
        for _ in range(10):
            p = cur / name
            if p.is_file():
                return p
            if cur.parent == cur:
                break
            cur = cur.parent
    except Exception:
        pass
    return None


def discover(cwd: str) -> list[dict]:
    """Return the always-loaded text files present, each with size + ceiling.

    `kind` drives reporting; `ceiling` is the hard per-file warn threshold (None =
    baseline-drift only); `defer_to` names a guard that owns this file's hard
    warning so we don't double-nag (MEMORY.md -> session-start-context.py)."""
    items: list[dict] = []

    def add(path: Path | None, kind: str, ceiling: int | None, defer_to: str | None = None):
        if path is None:
            return
        try:
            size = path.stat().st_size
        except Exception:
            return
        items.append({"path": str(path), "kind": kind, "bytes": size,
                      "ceiling": ceiling, "defer_to": defer_to})

    add(HOME / ".claude" / "CLAUDE.md", GLOBAL_KIND, GLOBAL_CEILING)
    add(_project_file(cwd, "CLAUDE.md"), "project CLAUDE.md", None)
    add(_memory_md(cwd), "MEMORY.md", None, defer_to="session-start-context.py")
    add(_project_file(cwd, "CONTEXT.md"), "project CONTEXT.md", None)
    # de-dup by resolved path (global and project CLAUDE.md can coincide)
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        try:
            rp = str(Path(it["path"]).resolve())
        except Exception:
            rp = it["path"]
        if rp in seen:
            continue
        seen.add(rp)
        out.append(it)
    return out


def is_project_anchored(items: list[dict]) -> bool:
    """True iff discovery found at least one project-scoped file (not just the
    global kernel). A bare home / non-project cwd discovers only the global
    CLAUDE.md, so a baseline recorded there is a too-narrow floor that would
    clobber a real multi-file floor and silently kill drift detection."""
    return any(it["kind"] != GLOBAL_KIND for it in items)


def safe_to_record(items: list[dict], ev: dict) -> bool:
    """A discovery is safe to persist as the baseline floor only when it is
    project-anchored AND does not narrow the scope of an existing baseline.
    This is the single gate both the silent ratchet and (Guard A half) --accept
    consult before writing."""
    return is_project_anchored(items) and not ev["scope_narrowed"]


# --- baseline persistence ------------------------------------------------------
def load_baseline() -> dict | None:
    try:
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_baseline(total: int, files: dict, kinds: list[str] | None = None) -> None:
    try:
        BASELINE_PATH.write_text(
            json.dumps({"total": total, "files": files, "kinds": kinds or []},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def warned_today() -> bool:
    try:
        return LASTWARN_PATH.read_text(encoding="utf-8").strip() == date.today().isoformat()
    except Exception:
        return False


def mark_warned() -> None:
    try:
        LASTWARN_PATH.write_text(date.today().isoformat(), encoding="utf-8")
    except Exception:
        pass


# --- core evaluation -----------------------------------------------------------
def evaluate(items: list[dict], baseline: dict | None) -> dict:
    """Pure decision function (no I/O) so the self-test can drive it directly.

    Returns {total, ceiling_hits, grew, growth_bytes, ratchet_down, base_total,
    cur_kinds, scope_narrowed, missing_kinds}.

    scope_narrowed = the recorded baseline knew about a file KIND we no longer
    discover (current kinds are a strict subset of the baseline's). A drop in
    total that comes from missing files — not a real slim — must NOT ratchet the
    floor down to a narrower scope."""
    total = sum(it["bytes"] for it in items)
    ceiling_hits = [it for it in items
                    if it["ceiling"] is not None and it["bytes"] > it["ceiling"]]
    base_total = baseline.get("total") if baseline else None

    cur_kinds = [it["kind"] for it in items]
    cur_kset = set(cur_kinds)
    base_kinds = set((baseline or {}).get("kinds") or [])
    missing_kinds = sorted(base_kinds - cur_kset)
    # only meaningful once a baseline persisted its kinds (older baselines have
    # none -> base_kinds empty -> never narrowed, the safe backward-compat default)
    scope_narrowed = bool(missing_kinds)

    grew = False
    growth = 0
    ratchet_down = False
    if base_total is None:
        # first run: establish the floor; do not claim growth
        ratchet_down = True
    elif total <= base_total:
        ratchet_down = True
    else:
        tol = max(TOL_BYTES, int(base_total * TOL_FRAC))
        if total > base_total + tol:
            grew = True
            growth = total - base_total
    return {"total": total, "ceiling_hits": ceiling_hits, "grew": grew,
            "growth_bytes": growth, "ratchet_down": ratchet_down,
            "base_total": base_total, "cur_kinds": cur_kinds,
            "scope_narrowed": scope_narrowed, "missing_kinds": missing_kinds}


def _kb(n: int) -> str:
    return f"{n / 1000:.1f} KB"


def _tok(n: int) -> str:
    return f"~{round(n / 4):,} tok"


def render_report(items: list[dict], ev: dict) -> str:
    lines = ["Always-loaded text layer:"]
    for it in sorted(items, key=lambda x: -x["bytes"]):
        flag = ""
        if it["ceiling"] is not None and it["bytes"] > it["ceiling"]:
            flag = f"  🔴 over {_kb(it['ceiling'])} ceiling"
        elif it["defer_to"]:
            flag = f"  (cliff guard: {it['defer_to']})"
        lines.append(f"  {_kb(it['bytes']):>9}  {_tok(it['bytes']):>12}  {it['kind']}{flag}")
    lines.append(f"  {'—' * 7}")
    lines.append(f"  {_kb(ev['total']):>9}  {_tok(ev['total']):>12}  TOTAL")
    if ev["base_total"] is not None:
        delta = ev["total"] - ev["base_total"]
        sign = "+" if delta >= 0 else ""
        lines.append(f"  baseline floor {_kb(ev['base_total'])} (Δ {sign}{delta} B)")
    if ev["missing_kinds"]:
        lines.append(
            f"  ⚠️ narrower scope than the floor — missing {ev['missing_kinds']} "
            f"(run from your project root; the floor is NOT being ratcheted here)"
        )
    return "\n".join(lines)


def build_warning(items: list[dict], ev: dict) -> str | None:
    """The additionalContext message, or None when healthy."""
    if not ev["ceiling_hits"] and not ev["grew"]:
        return None
    parts = [render_report(items, ev), ""]
    if ev["ceiling_hits"]:
        for it in ev["ceiling_hits"]:
            over = it["bytes"] - it["ceiling"]
            parts.append(
                f"🔴 {it['kind']} is {_kb(it['bytes'])} — {over} B over the "
                f"{_kb(it['ceiling'])} ceiling. The always-loaded kernel pays this "
                f"every turn; move rationale into linked rule files and keep the "
                f"kernel to one-line pointers."
            )
    if ev["grew"]:
        parts.append(
            f"⚠️ Total always-loaded text grew {ev['growth_bytes']} B past the "
            f"baseline floor ({_kb(ev['base_total'])} → {_kb(ev['total'])}). Slim "
            f"back, or run `context-budget-measure.py --accept` to set this as the "
            f"new floor if the growth is intentional."
        )
    parts.append("Guard: context-budget-measure.py (MYC-619). Bypass: CONTEXT_BUDGET_BYPASS=1")
    return "\n".join(parts)


# --- modes ---------------------------------------------------------------------
def hook_mode():
    if os.environ.get("CONTEXT_BUDGET_BYPASS") == "1":
        log_fire(HOOK_NAME, status="bypassed")
        noop()
    cwd = os.getcwd()
    try:
        raw = sys.stdin.read()
        if raw.strip():
            data = json.loads(raw)
            cwd = data.get("cwd") or data.get("workspace") or cwd
    except Exception:
        pass

    items = discover(cwd)
    if not items:
        log_fire(HOOK_NAME, status="no-files")
        noop()
    baseline = load_baseline()
    ev = evaluate(items, baseline)
    files_map = {it["path"]: it["bytes"] for it in items}

    # Ratchet the floor DOWN silently when at/below baseline (records the new
    # low) — but ONLY from a scope that is safe to record. A non-project cwd
    # (global-only discovery) or a narrower kind-set than the floor would
    # otherwise clobber a good multi-file floor and silently disable drift
    # detection. This is a deliberate, evaluated skip (the floor is preserved),
    # not a silent no-op: being in a non-project cwd is legitimate and not
    # actionable, so hook mode stays quiet; --report / --accept carry the signal.
    if ev["ratchet_down"] and safe_to_record(items, ev):
        save_baseline(ev["total"], files_map, ev["cur_kinds"])
        ratchet_status = "ratcheted"
    elif ev["ratchet_down"]:
        # wanted to ratchet but the scope guard blocked it (non-project / narrowed
        # discovery) — the MYC-1243 fix firing; emit it so the guard is observable.
        ratchet_status = "scope-skipped"
    else:
        ratchet_status = "above-floor"

    msg = build_warning(items, ev)
    if msg is None:
        log_fire(HOOK_NAME, status=ratchet_status)
        noop()
    if warned_today():
        log_fire(HOOK_NAME, status="warn-capped", detail=ratchet_status)
        noop()

    mark_warned()
    log_fire(HOOK_NAME, status="warned", detail=ratchet_status)
    total_kb = _kb(ev["total"])
    print(json.dumps({
        "systemMessage": f"📏 Always-loaded context {total_kb} — context-budget guard flagged growth/ceiling. See details.",
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": msg},
    }))


def report_mode(cwd: str | None = None):
    items = discover(cwd or os.getcwd())
    ev = evaluate(items, load_baseline())
    print(render_report(items, ev))
    msg = build_warning(items, ev)
    if msg:
        print("\n" + msg)


def accept_mode(cwd: str | None = None) -> int:
    """Record the current always-loaded total as the baseline floor.

    Refuses (nonzero) when discovery is NOT project-anchored — i.e. the command
    is running from a non-project cwd where only the global kernel is visible.
    Recording there would set a too-narrow floor that masks real drift in every
    project session. A narrowing relative to an existing multi-file baseline from
    a real project root is allowed (intentional slim) but noted."""
    cwd = cwd or os.getcwd()
    items = discover(cwd)
    if not is_project_anchored(items):
        n = len(items)
        print(
            f"refusing to record a baseline: this looks like a non-project cwd — "
            f"discovered only the global kernel ({n} file{'' if n == 1 else 's'}, "
            f"no project CLAUDE.md / MEMORY.md / CONTEXT.md). A baseline recorded "
            f"here would be a too-narrow floor that masks real drift in your "
            f"project sessions. Re-run --accept from your project root.",
            file=sys.stderr,
        )
        log_fire(HOOK_NAME, status="accept-refused", files=len(items))
        return 1
    total = sum(it["bytes"] for it in items)
    base = load_baseline()
    missing = sorted(set((base or {}).get("kinds") or []) - {it["kind"] for it in items})
    if missing:
        print(
            f"note: recording a narrower floor than the previous baseline — "
            f"missing {missing}. Recording anyway (explicit --accept from a "
            f"project root)."
        )
    save_baseline(total, {it["path"]: it["bytes"] for it in items},
                  [it["kind"] for it in items])
    kinds = ", ".join(it["kind"] for it in items)
    print(f"baseline floor set to {_kb(total)} ({total} B), {len(items)} files: {kinds}")
    log_fire(HOOK_NAME, status="accepted", files=len(items))
    return 0


def self_test() -> int:
    """Positive (over-ceiling fires) + negative (lean is silent) + drift + shrink
    + scope-safety (the GUARD-RECORDS-BASELINE-FROM-WRONG-SCOPE fix). Drives the
    pure evaluate()/predicates so no real home files are touched, plus end-to-end
    discover()/accept_mode() against a synthesized temp HOME."""
    global HOME, BASELINE_PATH, log_fire
    fails = []

    def mk(bytes_, kind, ceiling, defer=None):
        return {"path": f"/tmp/{kind}", "kind": kind, "bytes": bytes_,
                "ceiling": ceiling, "defer_to": defer}

    # 1. POSITIVE: a global CLAUDE.md over the 40KB ceiling must fire.
    items = [mk(74100, GLOBAL_KIND, GLOBAL_CEILING),
             mk(10000, "project CONTEXT.md", None)]
    ev = evaluate(items, {"total": 84100})  # baseline == current -> no growth
    if not ev["ceiling_hits"] or build_warning(items, ev) is None:
        fails.append("POSITIVE: over-ceiling did not fire")

    # 2. NEGATIVE: a lean layer at/under the floor and ceilings must be silent.
    items = [mk(30000, GLOBAL_KIND, GLOBAL_CEILING),
             mk(8000, "project CONTEXT.md", None)]
    ev = evaluate(items, {"total": 40000})  # current 38000 < baseline -> shrink
    if build_warning(items, ev) is not None:
        fails.append("NEGATIVE: lean layer warned when it should be silent")
    if not ev["ratchet_down"]:
        fails.append("SHRINK: did not ratchet the floor down")

    # 3. DRIFT: total grows past floor + tolerance -> warn (no ceiling hit).
    items = [mk(20000, GLOBAL_KIND, GLOBAL_CEILING),
             mk(20000, "project CLAUDE.md", None)]
    ev = evaluate(items, {"total": 30000})  # +10000 over 30000 floor >> tol
    if not ev["grew"] or build_warning(items, ev) is None:
        fails.append("DRIFT: growth past baseline did not fire")

    # 4. TOLERANCE: a tiny add under tolerance must NOT warn.
    items = [mk(30500, GLOBAL_KIND, GLOBAL_CEILING)]
    ev = evaluate(items, {"total": 30000})  # +500 < max(4096, 2%) tol
    if ev["grew"]:
        fails.append("TOLERANCE: tiny add false-fired")

    # 5. ANCHOR: project-anchored detection (global-only vs has-project-file).
    if is_project_anchored([mk(40000, GLOBAL_KIND, GLOBAL_CEILING)]):
        fails.append("ANCHOR: global-only discovery wrongly deemed project-anchored")
    if not is_project_anchored([mk(40000, GLOBAL_KIND, GLOBAL_CEILING),
                                mk(5000, "project CLAUDE.md", None)]):
        fails.append("ANCHOR: project file present but not anchored")

    # 6. SCOPE-NARROWED: a baseline recorded with 4 kinds, then a global-only
    #    discovery, must register narrowed AND must NOT be safe to record — the
    #    silent ratchet has to skip rather than clobber the multi-file floor.
    base4_kinds = [GLOBAL_KIND, "project CLAUDE.md", "MEMORY.md", "project CONTEXT.md"]
    items = [mk(39751, GLOBAL_KIND, GLOBAL_CEILING)]  # the real-world bug input
    ev = evaluate(items, {"total": 134000, "kinds": base4_kinds})
    if not ev["scope_narrowed"]:
        fails.append("SCOPE: global-only vs 4-kind baseline not flagged as narrowed")
    if not ev["ratchet_down"]:
        fails.append("SCOPE: 39.8K < 134K should compute ratchet_down (pre-guard)")
    if safe_to_record(items, ev):
        fails.append("SCOPE: global-only discovery was wrongly deemed safe to record")

    # 7. NOT-NARROWED: a real multi-file discovery at/under a same-kind floor is
    #    safe to record (the legitimate shrink path still ratchets).
    items = [mk(30000, GLOBAL_KIND, GLOBAL_CEILING),
             mk(8000, "project CLAUDE.md", None)]
    ev = evaluate(items, {"total": 50000, "kinds": [GLOBAL_KIND, "project CLAUDE.md"]})
    if ev["scope_narrowed"] or not safe_to_record(items, ev):
        fails.append("NOT-NARROWED: same-kind shrink wrongly blocked from ratchet")

    # 8. END-TO-END: real discover() + a synthesized over-ceiling global file in a
    #    temp HOME proves the I/O path, not just the pure function. Isolate from a
    #    live CLAUDE_PROJECT_DIR so the real vault files can't leak into the check.
    old_home, old_base = HOME, BASELINE_PATH
    old_proj = os.environ.pop("CLAUDE_PROJECT_DIR", None)
    try:
        with tempfile.TemporaryDirectory() as td:
            fake_global = Path(td) / ".claude"
            fake_global.mkdir(parents=True)
            (fake_global / "CLAUDE.md").write_text("x" * 50000, encoding="utf-8")
            HOME = Path(td)
            BASELINE_PATH = Path(td) / ".claude" / ".context-budget-baseline.json"
            its = discover(td)
            g = [i for i in its if i["kind"] == GLOBAL_KIND]
            ev2 = evaluate(its, None)
            if not g or not ev2["ceiling_hits"]:
                fails.append("E2E: temp-home over-ceiling not detected")
    except Exception as e:
        fails.append(f"E2E: raised {e}")
    finally:
        HOME, BASELINE_PATH = old_home, old_base
        if old_proj is not None:
            os.environ["CLAUDE_PROJECT_DIR"] = old_proj

    # 9. E2E ACCEPT — negative control (the regression this ticket fixes): from a
    #    non-project cwd, --accept must REFUSE and must NOT write a baseline file;
    #    positive control: from a project cwd (has a CLAUDE.md) it accepts and
    #    persists the kind set.
    old_home, old_base = HOME, BASELINE_PATH
    old_proj = os.environ.pop("CLAUDE_PROJECT_DIR", None)
    saved_log_fire = log_fire
    log_fire = lambda *_a, **_k: None  # accept_mode emits a fire; don't pollute real telemetry during the test  # noqa: E731
    try:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / ".claude").mkdir(parents=True)
            (tdp / ".claude" / "CLAUDE.md").write_text("g" * 39751, encoding="utf-8")
            HOME = tdp
            BASELINE_PATH = tdp / ".claude" / ".context-budget-baseline.json"

            # NEGATIVE: a bare subdir with no project CLAUDE.md/MEMORY/CONTEXT.
            bare = tdp / "elsewhere"
            bare.mkdir()
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc = accept_mode(cwd=str(bare))
            if rc == 0:
                fails.append("E2E-ACCEPT-NEG: --accept from non-project cwd did not refuse")
            if BASELINE_PATH.exists():
                fails.append("E2E-ACCEPT-NEG: refusal still wrote a baseline file")

            # POSITIVE: a project cwd with a CLAUDE.md -> accept + persist kinds.
            proj = tdp / "proj"
            proj.mkdir()
            (proj / "CLAUDE.md").write_text("p" * 6000, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                rc2 = accept_mode(cwd=str(proj))
            if rc2 != 0:
                fails.append("E2E-ACCEPT-POS: --accept from project cwd refused unexpectedly")
            saved = load_baseline()
            if not saved or "project CLAUDE.md" not in (saved.get("kinds") or []):
                fails.append("E2E-ACCEPT-POS: accepted baseline missing persisted kinds")
    except Exception as e:
        fails.append(f"E2E-ACCEPT: raised {e}")
    finally:
        HOME, BASELINE_PATH = old_home, old_base
        log_fire = saved_log_fire
        if old_proj is not None:
            os.environ["CLAUDE_PROJECT_DIR"] = old_proj

    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("OK: context-budget-measure self-test passed "
          "(positive + negative + drift + tolerance + anchor + scope + e2e + accept-refusal)")
    return 0


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    if "--report" in sys.argv:
        report_mode()
        return
    if "--accept" in sys.argv:
        sys.exit(accept_mode())
    hook_mode()


if __name__ == "__main__":
    # Windows cp1252-console safety (ai-brain-starter#313; hooks/ sweep #314).
    # A hook that print()s non-ASCII raises UnicodeEncodeError on a cp1252
    # console: the gate then fails silently OPEN, or denies the tool call with
    # no legible cause. Idempotent; a no-op on an already-UTF-8 console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # never crash-block a session start
        noop()
