#!/usr/bin/env python3
"""
instinct.py — Instinct Engine v2 CLI.

The intelligent layer (`/patterns`, `/evolve`) decides WHAT to do; this CLI is
the deterministic mechanism that does it safely. Subcommands:

  backfill     Add confidence/observations/last_seen/project_id to any
               feedback_*/discovery_* memory missing them. Idempotent. Backed up.
  reinforce    A pattern was observed again with no contradiction -> confidence up.
  correct      The user corrected this pattern -> confidence down (sharp).
  decay        Apply staleness decay (catches up memories unseen past the grace
               window; non-compounding; advances last_seen only when value changes).
  recompute    decay + a report. The once-per-period maintenance entry point.
  report       List instincts by effective confidence (filters: --project,
               --min-confidence, --stale, --json).
  export       Emit a portable YAML instinct set (project-scoped + global,
               confidence-gated). Requires PyYAML.
  import       Merge a YAML instinct set with confidence-gated rules (higher wins,
               equal/lower skipped) into <memory>/inherited/. Requires PyYAML.
  evolve       Cluster instincts by domain; for high-confidence clusters, write a
               proposed Command/Skill/Agent scaffold.

Mutation safety: every file that gets a managed-field write keeps a one-time
<file>.bak-instinct snapshot of its pre-engine state. Runs are idempotent — a
second identical run writes nothing.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import instinct_lib as il  # noqa: E402

OBSERVATIONS_PATH = Path(os.environ.get(
    "INSTINCT_OBSERVATIONS",
    str(Path.home() / ".claude" / "instinct" / "observations.jsonl"),
))
INJECTIONS_PATH = Path(os.environ.get(
    "INSTINCT_INJECTIONS",
    str(Path.home() / ".claude" / "instinct" / "injections.jsonl"),
))
PROMOTE_STATE_PATH = Path(os.environ.get(
    "INSTINCT_PROMOTE_STATE",
    str(Path.home() / ".claude" / "instinct" / "promote-state.json"),
))
# A session still running when `promote` fires would be credited for the
# instincts it has injected SO FAR and then never revisited, silently losing
# every later segment. Leave recent sessions alone; the next run takes them.
PROMOTE_SETTLE_MINUTES = int(os.environ.get("INSTINCT_PROMOTE_SETTLE_MINUTES", "60"))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _find(memory_dir: Path, ident: str) -> Path | None:
    """Resolve a slug or path to an instinct file."""
    p = Path(ident)
    if p.is_file():
        return p
    for cand in (memory_dir / ident, memory_dir / f"{ident}.md"):
        if cand.is_file():
            return cand
    # fuzzy: unique stem match
    matches = [q for q in il.iter_instinct_paths(memory_dir) if ident in q.stem]
    return matches[0] if len(matches) == 1 else None


def _effective(inst: il.Instinct, today: date) -> float:
    c = il.parse_float(inst.get("confidence"), il.seed_confidence(inst.get("strength")))
    ls = il.parse_date(inst.get("last_seen")) or il.file_mtime_date(inst.path)
    return il.decayed_confidence(c, ls, today)


def _today() -> date:
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------
def cmd_backfill(args, memory_dir: Path) -> int:
    today = _today()
    touched = skipped = 0
    for path in il.iter_instinct_paths(memory_dir):
        inst = il.parse_instinct(path)
        fm = inst.fm
        if all(k in fm for k in il.SEED_KEYS):
            skipped += 1
            continue
        updates: dict[str, object] = {}
        if "confidence" not in fm:
            updates["confidence"] = round(
                il.seed_confidence(fm.get("strength"), fm.get("type"), inst.body), 3)
        if "observations" not in fm:
            updates["observations"] = 1
        if "last_seen" not in fm:
            # Engine birth = today. File mtime is a poor proxy for "last
            # observed": an active codified rule is in force regardless of when
            # the file was written, so decay must accrue from the engine's
            # first sight, not the file's age.
            updates["last_seen"] = _today()
        if "project_id" not in fm:
            updates["project_id"] = il.PROJECT_GLOBAL
        if "exposures" not in fm:
            updates["exposures"] = 0
        if "evidence" not in fm:
            # Say out loud what the number is: a seed until something
            # exercises it.
            updates["evidence"] = il.evidence_state(fm)
        if args.dry_run:
            print(f"WOULD backfill {path.name}: {updates}")
            touched += 1
            continue
        new_text = il.set_managed_fields(inst, updates)
        if il.write_instinct(inst, new_text, backup=not getattr(args, "no_backup", False)):
            touched += 1
    print(f"backfill: {touched} {'would be ' if args.dry_run else ''}updated, "
          f"{skipped} already complete (of "
          f"{sum(1 for _ in il.iter_instinct_paths(memory_dir))} instincts)")
    return 0


def cmd_reseed(args, memory_dir: Path) -> int:
    """Recompute the SEED confidence (type/content-aware) for instincts that
    have no explicit `strength:` and have never been reinforced
    (observations <= 1). Leaves strengthened or reinforced instincts alone —
    those carry earned signal that must not be reset."""
    changed = skipped = 0
    for path in il.iter_instinct_paths(memory_dir):
        inst = il.parse_instinct(path)
        fm = inst.fm
        if fm.get("strength"):
            skipped += 1
            continue
        if il.parse_int(fm.get("observations"), 0) > 1:
            skipped += 1
            continue
        new_c = round(il.seed_confidence(None, fm.get("type"), inst.body), 3)
        cur = il.parse_float(fm.get("confidence"))
        today = _today()
        conf_same = cur is not None and abs(cur - new_c) < 1e-6
        seen_same = il.parse_date(fm.get("last_seen")) == today
        if conf_same and seen_same:
            continue
        if args.dry_run:
            print(f"WOULD reseed {path.name}: confidence {cur} -> {new_c}, last_seen -> {today}")
            changed += 1
            continue
        new_text = il.set_managed_fields(inst, {"confidence": new_c, "last_seen": today})
        if il.write_instinct(inst, new_text, backup=not getattr(args, "no_backup", False)):
            changed += 1
    print(f"reseed: {changed} {'would be ' if args.dry_run else ''}reseeded, "
          f"{skipped} left untouched (strengthened or reinforced)")
    return 0


def _apply_op(memory_dir: Path, ident: str, op, label: str, dry: bool,
              bump_observations: bool, touch_seen: bool) -> int:
    path = _find(memory_dir, ident)
    if not path:
        print(f"ERROR: no instinct matches {ident!r} in {memory_dir}", file=sys.stderr)
        return 1
    inst = il.parse_instinct(path)
    fm = inst.fm
    cur = il.parse_float(fm.get("confidence"), il.seed_confidence(fm.get("strength")))
    new_c = round(op(cur), 3)
    updates: dict[str, object] = {"confidence": new_c}
    if bump_observations:
        updates["observations"] = il.parse_int(fm.get("observations"), 0) + 1
    if touch_seen:
        updates["last_seen"] = _today()
    if dry:
        print(f"WOULD {label} {path.name}: confidence {cur} -> {new_c}")
        return 0
    new_text = il.set_managed_fields(inst, updates)
    il.write_instinct(inst, new_text)
    print(f"{label} {path.name}: confidence {cur} -> {new_c} "
          f"(observations={updates.get('observations', fm.get('observations'))})")
    return 0


def cmd_reinforce(args, memory_dir: Path) -> int:
    return _apply_op(memory_dir, args.ident, il.reinforce_confidence, "reinforce",
                     args.dry_run, bump_observations=True, touch_seen=True)


def cmd_correct(args, memory_dir: Path) -> int:
    return _apply_op(memory_dir, args.ident, il.correct_confidence, "correct",
                     args.dry_run, bump_observations=False, touch_seen=True)


def cmd_decay(args, memory_dir: Path) -> int:
    today = _today()
    changed = 0
    for path in il.iter_instinct_paths(memory_dir):
        inst = il.parse_instinct(path)
        fm = inst.fm
        cur = il.parse_float(fm.get("confidence"))
        if cur is None:
            continue  # not backfilled yet; skip (run backfill first)
        ls = il.parse_date(fm.get("last_seen")) or il.file_mtime_date(path)
        new_c = round(il.decayed_confidence(cur, ls, today), 3)
        if abs(new_c - cur) < 1e-6:
            continue  # within grace or no drift — preserve last_seen
        if args.dry_run:
            print(f"WOULD decay {path.name}: {cur} -> {new_c} "
                  f"(last_seen {fm.get('last_seen')}, {(today - ls).days}d)")
            changed += 1
            continue
        # advance last_seen to today: the elapsed staleness is now consumed
        new_text = il.set_managed_fields(inst, {"confidence": new_c, "last_seen": today})
        if il.write_instinct(inst, new_text):
            changed += 1
    print(f"decay: {changed} instinct(s) {'would ' if args.dry_run else ''}decayed")
    return 0


def _read_ledger(path: Path) -> tuple[list[dict], int]:
    """Read a JSONL ledger plus its rotated `.prev` sibling.

    Returns (records, malformed_count). Malformed lines are COUNTED and
    reported by the caller, never silently dropped -- a ledger that quietly
    loses half its lines and still exits 0 is the SILENT-NO-OP bug class, and
    it would read exactly like "there was no evidence".
    """
    records: list[dict] = []
    malformed = 0
    for p in (Path(str(path) + ".prev"), path):
        if not p.is_file():
            continue
        try:
            with p.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        malformed += 1
                        continue
                    if isinstance(obj, dict):
                        records.append(obj)
                    else:
                        malformed += 1
        except OSError as exc:
            print(f"WARNING: could not read {p}: {exc}", file=sys.stderr)
    return records, malformed


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _load_promote_state() -> dict:
    try:
        return json.loads(PROMOTE_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_promote_state(state: dict) -> None:
    try:
        PROMOTE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROMOTE_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"WARNING: could not persist promote state: {exc}", file=sys.stderr)


def cmd_promote(args, memory_dir: Path) -> int:
    """Ledger -> confidence. The step that never ran.

    `reinforce`/`correct` are the engine's bidirectional update, but both are
    manual, so in practice neither fires and every stored confidence stays the
    seed it was born with -- a number derived from whether the memory's prose
    happened to contain "never" or "codified". This closes the loop on the half
    that IS deterministically observable.

    Reinforce, not correct. An exposure that produced no correction is weak
    positive evidence and it is genuinely in the ledger. A CORRECTION is not
    recorded anywhere on disk, so automating only the upward direction with no
    gate would manufacture 0.99s across the whole store. Hence, mirroring the
    shipped runtime loop (MYC-818 / MYC-916): promotion is gated on a COUNT of
    exposures, the climb is asymptotic, and `correct` stays human-driven.

    An exposure counts only when the session did real work (>= --min-session-calls
    tool calls in the observation ledger). A session that started and died
    immediately proves nothing about the instincts it loaded.
    """
    today = _today()
    now = datetime.now(timezone.utc)

    obs, obs_bad = _read_ledger(OBSERVATIONS_PATH)
    work = collections.Counter(str(o.get("session", "")) for o in obs)

    inj, inj_bad = _read_ledger(INJECTIONS_PATH)
    if not inj:
        print(f"promote: no injection records at {INJECTIONS_PATH}.")
        print("  The SessionStart hook writes them. If the engine was installed "
              "before this ledger existed, re-run the installer and let one "
              "session start; there is nothing to promote from yet.")
        return 0

    state = _load_promote_state()
    credited: list[str] = list(state.get("credited_sessions", []))
    credited_set = set(credited)

    deltas: collections.Counter = collections.Counter()
    seen_pairs: set[tuple[str, str]] = set()
    fresh_sessions: set[str] = set()
    n_already = n_idle = n_unsettled = n_unattributed = 0

    for rec in inj:
        sess = str(rec.get("session", ""))
        if not sess:
            n_unattributed += 1
            continue
        if sess in credited_set:
            n_already += 1
            continue
        ts = _parse_ts(rec.get("ts"))
        if ts and (now - ts).total_seconds() < PROMOTE_SETTLE_MINUTES * 60:
            n_unsettled += 1
            continue
        if work.get(sess, 0) < args.min_session_calls:
            n_idle += 1
            continue
        fresh_sessions.add(sess)
        slugs = list(rec.get("injected") or []) + list(rec.get("explored") or [])
        for slug in slugs:
            pair = (sess, str(slug))
            if pair in seen_pairs:
                continue  # one exposure per instinct per session, not per segment
            seen_pairs.add(pair)
            deltas[str(slug)] += 1

    touched = promoted = unbackfilled = 0
    for path in il.iter_instinct_paths(memory_dir):
        inst = il.parse_instinct(path)
        fm = inst.fm
        cur_conf = il.parse_float(fm.get("confidence"))
        if cur_conf is None:
            unbackfilled += 1
            continue
        delta = deltas.get(inst.slug, 0)
        prev_exp = il.parse_int(fm.get("exposures"), 0)
        new_exp = prev_exp + delta
        steps = il.promotion_steps(prev_exp, new_exp, args.every)

        updates: dict[str, object] = {}
        if delta:
            updates["exposures"] = new_exp
            updates["last_exercised"] = today
        if steps:
            c = cur_conf
            for _ in range(steps):
                c = il.reinforce_confidence(c)
            updates["confidence"] = round(c, 3)
            updates["observations"] = il.parse_int(fm.get("observations"), 0) + steps
            updates["last_seen"] = today

        # Provenance is recomputed for EVERY instinct, not just the touched
        # ones, so `evidence` is populated store-wide and a seed can never be
        # exported as though it were a measurement.
        merged = dict(fm)
        merged.update({k: str(v) for k, v in updates.items()})
        ev = il.evidence_state(merged)
        if fm.get("evidence") != ev:
            updates["evidence"] = ev
        if not updates:
            continue

        if args.dry_run:
            bits = [f"exposures {prev_exp}->{new_exp}"] if delta else []
            if steps:
                bits.append(f"confidence {cur_conf}->{updates['confidence']} "
                            f"(+{steps} step(s))")
            if "evidence" in updates:
                bits.append(f"evidence={updates['evidence']}")
            print(f"WOULD promote {path.name}: " + ", ".join(bits))
        else:
            if not il.write_instinct(inst, il.set_managed_fields(inst, updates)):
                continue
        touched += 1
        if steps:
            promoted += 1

    if not args.dry_run and fresh_sessions:
        credited.extend(sorted(fresh_sessions))
        state["credited_sessions"] = credited[-il.PROMOTE_SESSION_MEMORY:]
        state["last_run"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        _save_promote_state(state)

    verb = "would credit" if args.dry_run else "credited"
    print(f"promote: {verb} {len(fresh_sessions)} session(s) -> "
          f"{sum(deltas.values())} exposure(s) across {len(deltas)} instinct(s); "
          f"{touched} file(s) updated, {promoted} reinforced "
          f"(gate: 1 step per {args.every} exposures).")
    print(f"  skipped: {n_already} already-credited, {n_unsettled} still-recent "
          f"(< {PROMOTE_SETTLE_MINUTES}m), {n_idle} idle "
          f"(< {args.min_session_calls} tool calls), {n_unattributed} unattributed.")
    if obs_bad or inj_bad:
        print(f"  WARNING: {inj_bad} malformed injection line(s), "
              f"{obs_bad} malformed observation line(s) skipped.", file=sys.stderr)
    if unbackfilled:
        print(f"  {unbackfilled} instinct(s) have no stored confidence and were "
              f"SKIPPED -- they can never be promoted until backfilled. Run: "
              f"python3 {Path(__file__).name} backfill", file=sys.stderr)

    if not args.no_decay:
        print("---")
        cmd_decay(args, memory_dir)
    return 0


def cmd_recompute(args, memory_dir: Path) -> int:
    cmd_decay(args, memory_dir)
    print("---")
    args.min_confidence = 0.0
    args.project = None
    args.stale = False
    args.json = False
    args.limit = args.limit if getattr(args, "limit", None) else 20
    return cmd_report(args, memory_dir)


def cmd_report(args, memory_dir: Path) -> int:
    today = _today()
    rows = []
    cur_proj = il.current_project_id()
    for path in il.iter_instinct_paths(memory_dir):
        inst = il.parse_instinct(path)
        fm = inst.fm
        eff = round(_effective(inst, today), 3)
        proj = fm.get("project_id", il.PROJECT_GLOBAL)
        if args.project and proj not in (args.project, il.PROJECT_GLOBAL):
            continue
        if eff < args.min_confidence:
            continue
        stored = il.parse_float(fm.get("confidence"))
        ls = il.parse_date(fm.get("last_seen")) or il.file_mtime_date(path)
        is_stale = stored is not None and eff < stored - 1e-6
        if args.stale and not is_stale:
            continue
        rows.append({
            "slug": inst.slug,
            "domain": il.infer_domain(inst),
            "confidence": eff,
            "stored": stored,
            "observations": il.parse_int(fm.get("observations"), 0),
            "last_seen": ls.isoformat(),
            "project_id": proj,
            "stale": is_stale,
        })
    rows.sort(key=lambda r: r["confidence"], reverse=True)
    if getattr(args, "limit", None):
        rows = rows[: args.limit]
    if args.json:
        print(json.dumps({"current_project": cur_proj, "instincts": rows}, indent=2))
        return 0
    print(f"# Instinct report ({len(rows)} shown; current project = {cur_proj})")
    print(f"{'conf':>5}  {'obs':>3}  {'domain':<9} {'project':<14} {'last_seen':<10} slug")
    for r in rows:
        flag = " (stale)" if r["stale"] else ""
        print(f"{r['confidence']:>5.2f}  {r['observations']:>3}  {r['domain']:<9} "
              f"{r['project_id'][:14]:<14} {r['last_seen']:<10} {r['slug']}{flag}")
    return 0


# ---------------------------------------------------------------------------
# --shareable: type filter + disclosure scan for `export`
# ---------------------------------------------------------------------------
# Only these two types are PROVEN transferable. `reference`/`project` are
# audit bookmarks (private companies, tickets, artifact URLs) that happen to
# live in the same feedback_*/discovery_* files the Instinct Engine scans; an
# instinct with no resolvable type at all is treated the same as an unsafe
# one. This is an ALLOW-list, not a denylist of {reference, project}, on
# purpose: a denylist only stops the types it already knows about, and a
# brand-new type value tomorrow would sail straight through it unexamined.
SHAREABLE_TYPES = frozenset({"feedback", "discovery"})

# Deliberately conservative. A false positive here costs a re-run; a missed
# one ships a disclosure. Two matching STYLES on purpose:
#   substring     catches a compound brand token wherever it appears, even
#                 inside a longer word (see the ondeplan note below).
#   word_boundary catches a short bare name/word WITHOUT matching it as a
#                 substring of something unrelated.
# CRITICAL: `\bonde\b` does NOT match inside "ondeplan" (no word boundary
# between "e" and "p"), which is exactly why the compound names
# (ondeplan/onde-platform/onde_team) are their own substring entries instead
# of being folded into the bare `\bonde\b` word-boundary pattern. Merging
# them back into one word-boundary pattern reopens the exact leak this gate
# exists to close. All patterns are matched case-insensitively -- these are
# names and tickets, and there is no safe casing to assume away.
DISCLOSURE_PATTERNS: dict[str, tuple[str, ...]] = {
    "substring": (
        "ondeplan", "onde-platform", "onde_team", "mycelium", "myceliumai", "diazroa",
    ),
    "word_boundary": (
        r"\bonde\b", r"\badelaida\b", r"\bdiaz-roa\b", r"\bsergio\b", r"\bnelly\b",
        r"\bcolombia\b", r"\bbogot[aá]\b", r"\baccenture\b", r"\bpanorama\b",
    ),
    "regex": (
        r"MYC-\d+", r"OND-\d+", r"claude\.ai/code/artifact",
    ),
}


def _compile_disclosure_patterns() -> list[tuple[str, re.Pattern]]:
    compiled = []
    for raw in DISCLOSURE_PATTERNS["substring"]:
        compiled.append((raw, re.compile(re.escape(raw), re.IGNORECASE)))
    for raw in DISCLOSURE_PATTERNS["word_boundary"] + DISCLOSURE_PATTERNS["regex"]:
        compiled.append((raw, re.compile(raw, re.IGNORECASE)))
    return compiled


_DISCLOSURE_COMPILED = _compile_disclosure_patterns()


def _disclosure_hits(filename: str, body: str) -> list[str]:
    """Scan `filename` and `body` against every DISCLOSURE_PATTERNS entry.

    Returns one human-readable line per hit (empty list = clean). Checks
    ALL patterns against BOTH surfaces rather than stopping at the first hit,
    so a single failing run can report everything that needs fixing at once.
    """
    hits: list[str] = []
    for surface, text in (("filename", filename), ("body", body)):
        for raw, rx in _DISCLOSURE_COMPILED:
            m = rx.search(text)
            if m:
                hits.append(f"{surface} matched {raw!r} -> {m.group(0)!r}")
    return hits


def _resolve_type(inst: il.Instinct, yaml_mod) -> str | None:
    """The instinct's declared `type`, checked in both schemas real memory
    files use.

    Some instincts carry a flat top-level `type:` (written by `import`'s
    _write_inherited, and already read elsewhere via fm.get("type") for
    confidence seeding). Others -- including ones written by Claude's own
    project-memory tooling -- nest it under a `metadata:` mapping instead
    (measured live: a real vault discovery_*.md carries `metadata.type:
    project` despite its discovery_ filename, which is exactly the bookmark
    this filter exists to keep out of a shareable pack). Returns None when
    neither form is present; callers must treat that as UNKNOWN, never as
    safe -- that is what makes the SHAREABLE_TYPES check below fail closed.
    """
    flat = inst.fm.get("type")
    if flat:
        return str(flat).strip().lower()
    try:
        full = yaml_mod.safe_load("\n".join(inst.fm_lines)) or {}
    except yaml_mod.YAMLError:
        # Malformed frontmatter -> type is unproven, not "safe". A bare
        # `except Exception` here would ALSO swallow a genuine bug unrelated
        # to YAML (e.g. inst.fm_lines holding something un-joinable), and
        # silently treat a real defect as "exclude this instinct" instead of
        # surfacing it -- narrowed to the one error class this call can
        # actually raise for bad DATA, so anything else still fails loud.
        return None
    meta = full.get("metadata") if isinstance(full, dict) else None
    if isinstance(meta, dict) and meta.get("type"):
        return str(meta["type"]).strip().lower()
    return None


def cmd_export(args, memory_dir: Path) -> int:
    try:
        import yaml
    except ImportError:
        print("ERROR: export needs PyYAML (pip install pyyaml).", file=sys.stderr)
        return 2
    shareable = getattr(args, "shareable", False)
    today = _today()
    proj = args.project or il.current_project_id()
    out = []
    excluded_type = 0
    disclosure_report: list[str] = []
    for path in il.iter_instinct_paths(memory_dir):
        inst = il.parse_instinct(path)
        fm = inst.fm
        p = fm.get("project_id", il.PROJECT_GLOBAL)
        if p not in (proj, il.PROJECT_GLOBAL) and not args.all:
            continue
        eff = round(_effective(inst, today), 3)
        if eff < args.min_confidence:
            continue
        if shareable:
            itype = _resolve_type(inst, yaml)
            if itype not in SHAREABLE_TYPES:
                excluded_type += 1
                continue
            hits = _disclosure_hits(path.name, inst.body)
            if hits:
                disclosure_report.extend(f"{path.name}: {h}" for h in hits)
                continue
        out.append({
            "id": inst.slug,
            "trigger": fm.get("name", inst.slug),
            "confidence": eff,
            "domain": il.infer_domain(inst),
            "source_repo": p,
            # What the number is BASED ON. Without it a seeded 0.82 and an
            # earned 0.82 export identically, and the pack claims evidence it
            # does not have.
            "evidence_state": il.evidence_state(fm),
            "exposures": il.parse_int(fm.get("exposures"), 0),
            "action": (fm.get("description", "") or "").strip(),
            "evidence": inst.body.strip()[:1200],
        })

    if disclosure_report:
        # FAIL CLOSED, always -- never write a partial pack with only the
        # offender silently dropped (that is the SILENT-NO-OP bug class: a
        # pack that "worked" while quietly shipping less coverage than it
        # claimed, with nobody told which file was cut).
        print("ERROR: --shareable disclosure scan failed. Refusing to write any "
              "output. Fix or exclude the file(s) below and re-run:", file=sys.stderr)
        for line in disclosure_report:
            print(f"  {line}", file=sys.stderr)
        return 1

    out.sort(key=lambda r: r["confidence"], reverse=True)
    doc = {"instinct_pack_version": 1, "exported_for_project": proj,
           "exported_count": len(out), "instincts": out}
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)
    suffix = f" (--shareable excluded {excluded_type} reference/project/untyped)" if shareable else ""
    if args.out:
        Path(args.out).expanduser().write_text(text, encoding="utf-8")
        print(f"export: {len(out)} instinct(s) -> {args.out}{suffix}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_import(args, memory_dir: Path) -> int:
    try:
        import yaml
    except ImportError:
        print("ERROR: import needs PyYAML (pip install pyyaml).", file=sys.stderr)
        return 2
    src = Path(args.file).expanduser()
    if not src.is_file():
        print(f"ERROR: no such file {src}", file=sys.stderr)
        return 1
    doc = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    incoming = doc.get("instincts", [])
    inherited_dir = memory_dir / "inherited"
    today = _today()

    # index local instincts by slug
    local = {}
    for path in il.iter_instinct_paths(memory_dir):
        local[path.stem] = path
    for path in inherited_dir.glob("*.md") if inherited_dir.is_dir() else []:
        local.setdefault(path.stem, path)

    added = updated = skipped = 0
    for item in incoming:
        iid = str(item.get("id", "")).strip()
        if not iid:
            continue
        ic = il.parse_float(str(item.get("confidence")), 0.0) or 0.0
        if iid in local:
            inst = il.parse_instinct(local[iid])
            lc = il.parse_float(inst.get("confidence"),
                                il.seed_confidence(inst.get("strength")))
            if ic > lc + 1e-6:  # higher-confidence import wins
                if not args.dry_run:
                    il.write_instinct(inst, il.set_managed_fields(
                        inst, {"confidence": round(ic, 3), "last_seen": today}))
                print(f"update {iid}: confidence {lc} -> {round(ic, 3)} (imported higher)")
                updated += 1
            else:
                skipped += 1  # equal-or-lower import is skipped
        else:
            if not args.dry_run:
                inherited_dir.mkdir(parents=True, exist_ok=True)
                _write_inherited(inherited_dir / f"{iid}.md", item, today)
            print(f"add  {iid}: inherited (confidence {round(ic, 3)})")
            added += 1
    print(f"import: {added} added, {updated} updated, {skipped} skipped "
          f"({'dry-run' if args.dry_run else 'applied'})")
    return 0


def _write_inherited(path: Path, item: dict, today: date) -> None:
    iid = item.get("id", path.stem)
    fm = [
        "---",
        f"name: {item.get('trigger', iid)}",
        f"description: {item.get('action', '')[:200]}",
        "type: feedback",
        "memory_class: procedural",
        f"confidence: {round(il.parse_float(str(item.get('confidence')), 0.5) or 0.5, 3)}",
        "observations: 0",
        f"last_seen: {today.isoformat()}",
        f"project_id: {item.get('source_repo', il.PROJECT_GLOBAL)}",
        f"domain: {item.get('domain', 'general')}",
        "inherited: true",
        f"inherited_at: {today.isoformat()}",
        "---",
        "",
        "## Action",
        "",
        str(item.get("action", "")).strip(),
        "",
        "## Evidence",
        "",
        str(item.get("evidence", "")).strip(),
        "",
        f"*Inherited instinct (imported {today.isoformat()}). Review before relying on it.*",
        "",
    ]
    path.write_text("\n".join(fm), encoding="utf-8")


def cmd_evolve(args, memory_dir: Path) -> int:
    today = _today()
    clusters: dict[str, list] = {}
    for path in il.iter_instinct_paths(memory_dir):
        inst = il.parse_instinct(path)
        eff = _effective(inst, today)
        domain = il.infer_domain(inst)
        clusters.setdefault(domain, []).append((inst, round(eff, 3)))

    proposable = []
    for domain, members in sorted(clusters.items()):
        members.sort(key=lambda m: m[1], reverse=True)
        if len(members) < il.EVOLVE_MIN_CLUSTER:
            continue
        confs = sorted(m[1] for m in members)
        median = confs[len(confs) // 2]
        marker = "PROPOSE" if median >= il.EVOLVE_MIN_CONFIDENCE else "watch"
        print(f"[{marker}] domain={domain:<9} n={len(members):<3} median_conf={median:.2f}")
        for inst, eff in members[:6]:
            print(f"         {eff:.2f}  {inst.slug}")
        if median >= il.EVOLVE_MIN_CONFIDENCE:
            proposable.append((domain, members, median))

    if not proposable:
        print("\nNo cluster met the propose bar "
              f"(>= {il.EVOLVE_MIN_CLUSTER} instincts, median confidence "
              f">= {il.EVOLVE_MIN_CONFIDENCE}).")
        return 0

    out_dir = Path(args.out).expanduser() if args.out else (memory_dir.parent / "Instinct Proposals")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for domain, members, median in proposable:
        target = out_dir / f"proposed-skill-{domain}.md"
        target.write_text(_render_proposal(domain, members, median, today), encoding="utf-8")
        written.append(target)
    print(f"\nevolve: wrote {len(written)} proposed skill scaffold(s):")
    for w in written:
        print(f"  {w}")
    return 0


def _render_proposal(domain: str, members: list, median: float, today: date) -> str:
    lines = [
        "---",
        f"name: instinct-{domain}",
        f"description: PROPOSED skill auto-clustered from {len(members)} "
        f"high-confidence {domain} instincts (median confidence {median:.2f}). "
        "Review, refine, and adopt or discard.",
        "status: proposed",
        f"generated: {today.isoformat()}",
        f"domain: {domain}",
        f"source_instincts: {len(members)}",
        "---",
        "",
        f"# Proposed skill: `{domain}` instinct cluster",
        "",
        f"`/evolve` found **{len(members)}** instincts in the `{domain}` domain with a "
        f"median confidence of **{median:.2f}** (>= {il.EVOLVE_MIN_CONFIDENCE} propose bar). "
        "When a cluster of related, high-confidence instincts hardens, it is a candidate "
        "to promote into a single reusable structure (Command / Skill / Agent).",
        "",
        "## Member instincts",
        "",
    ]
    for inst, eff in members:
        name = inst.get("name", inst.slug)
        lines.append(f"- **{eff:.2f}** `{inst.slug}` — {name}")
    lines += [
        "",
        "## Suggested structure",
        "",
        "- **Command** if these instincts describe one repeatable procedure with a clear trigger.",
        "- **Skill** if they form a coherent body of guidance for a domain (most common).",
        "- **Agent** if the cluster describes an autonomous multi-step workflow.",
        "",
        "## Next step",
        "",
        f"Draft the `{domain}` skill body from the member instincts above, keeping each "
        "instinct's Action + Evidence. Then retire the individual memories or link them "
        "from the new skill. Delete this proposal once adopted or rejected.",
        "",
        f"*Auto-generated by `instinct.py evolve` on {today.isoformat()}.*",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    # --memory-dir lives on a shared parent so it is accepted AFTER the
    # subcommand (the natural position: `instinct.py backfill --memory-dir X`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--memory-dir", help="Agent Memory dir (default: auto-detect)")

    p = argparse.ArgumentParser(description="Instinct Engine v2 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("backfill", parents=[common])
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--no-backup", action="store_true",
                    help="skip .bak-instinct snapshots (use when files are git-tracked)")
    sp = sub.add_parser("reseed", parents=[common])
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--no-backup", action="store_true")
    sp = sub.add_parser("reinforce", parents=[common]); sp.add_argument("ident"); sp.add_argument("--dry-run", action="store_true")
    sp = sub.add_parser("correct", parents=[common]); sp.add_argument("ident"); sp.add_argument("--dry-run", action="store_true")
    sp = sub.add_parser("decay", parents=[common]); sp.add_argument("--dry-run", action="store_true")

    sp = sub.add_parser("promote", parents=[common])
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--every", type=int, default=il.PROMOTE_EVERY,
                    help="qualifying exposures per reinforce step "
                         f"(default {il.PROMOTE_EVERY})")
    sp.add_argument("--min-session-calls", type=int, default=il.PROMOTE_MIN_SESSION_CALLS,
                    help="tool calls a session must have made for its exposures "
                         f"to count (default {il.PROMOTE_MIN_SESSION_CALLS})")
    sp.add_argument("--no-decay", action="store_true",
                    help="skip the decay pass that normally follows")

    sp = sub.add_parser("recompute", parents=[common])
    sp.add_argument("--dry-run", action="store_true"); sp.add_argument("--limit", type=int, default=20)

    sp = sub.add_parser("report", parents=[common])
    sp.add_argument("--project"); sp.add_argument("--min-confidence", type=float, default=0.0)
    sp.add_argument("--stale", action="store_true"); sp.add_argument("--json", action="store_true")
    sp.add_argument("--limit", type=int)

    sp = sub.add_parser("export", parents=[common])
    sp.add_argument("--project"); sp.add_argument("--min-confidence", type=float, default=0.0)
    sp.add_argument("--all", action="store_true"); sp.add_argument("--out")
    sp.add_argument("--shareable", action="store_true",
                    help="exclude reference/project-type (and untyped) instincts, and fail "
                         "closed -- writing nothing -- if any surviving instinct's filename "
                         "or body matches a private brand/name/ticket disclosure pattern")

    sp = sub.add_parser("import", parents=[common]); sp.add_argument("file"); sp.add_argument("--dry-run", action="store_true")

    sp = sub.add_parser("evolve", parents=[common]); sp.add_argument("--out")
    return p


DISPATCH = {
    "promote": cmd_promote,
    "backfill": cmd_backfill, "reseed": cmd_reseed,
    "reinforce": cmd_reinforce, "correct": cmd_correct,
    "decay": cmd_decay, "recompute": cmd_recompute, "report": cmd_report,
    "export": cmd_export, "import": cmd_import, "evolve": cmd_evolve,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    memory_dir = il.resolve_memory_dir(args.memory_dir)
    if memory_dir is None:
        print("ERROR: could not locate Agent Memory dir. Pass --memory-dir or set "
              "$INSTINCT_MEMORY_DIR.", file=sys.stderr)
        return 2
    return DISPATCH[args.cmd](args, memory_dir)


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
