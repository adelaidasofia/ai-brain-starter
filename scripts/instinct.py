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
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import instinct_lib as il  # noqa: E402

OBSERVATIONS_PATH = Path(os.environ.get(
    "INSTINCT_OBSERVATIONS",
    str(Path.home() / ".claude" / "instinct" / "observations.jsonl"),
))


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
        if all(k in fm for k in il.MANAGED_KEYS):
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


def cmd_export(args, memory_dir: Path) -> int:
    try:
        import yaml
    except ImportError:
        print("ERROR: export needs PyYAML (pip install pyyaml).", file=sys.stderr)
        return 2
    today = _today()
    proj = args.project or il.current_project_id()
    out = []
    for path in il.iter_instinct_paths(memory_dir):
        inst = il.parse_instinct(path)
        fm = inst.fm
        p = fm.get("project_id", il.PROJECT_GLOBAL)
        if p not in (proj, il.PROJECT_GLOBAL) and not args.all:
            continue
        eff = round(_effective(inst, today), 3)
        if eff < args.min_confidence:
            continue
        out.append({
            "id": inst.slug,
            "trigger": fm.get("name", inst.slug),
            "confidence": eff,
            "domain": il.infer_domain(inst),
            "source_repo": p,
            "action": (fm.get("description", "") or "").strip(),
            "evidence": inst.body.strip()[:1200],
        })
    out.sort(key=lambda r: r["confidence"], reverse=True)
    doc = {"instinct_pack_version": 1, "exported_for_project": proj,
           "exported_count": len(out), "instincts": out}
    text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)
    if args.out:
        Path(args.out).expanduser().write_text(text, encoding="utf-8")
        print(f"export: {len(out)} instinct(s) -> {args.out}")
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

    sp = sub.add_parser("recompute", parents=[common])
    sp.add_argument("--dry-run", action="store_true"); sp.add_argument("--limit", type=int, default=20)

    sp = sub.add_parser("report", parents=[common])
    sp.add_argument("--project"); sp.add_argument("--min-confidence", type=float, default=0.0)
    sp.add_argument("--stale", action="store_true"); sp.add_argument("--json", action="store_true")
    sp.add_argument("--limit", type=int)

    sp = sub.add_parser("export", parents=[common])
    sp.add_argument("--project"); sp.add_argument("--min-confidence", type=float, default=0.0)
    sp.add_argument("--all", action="store_true"); sp.add_argument("--out")

    sp = sub.add_parser("import", parents=[common]); sp.add_argument("file"); sp.add_argument("--dry-run", action="store_true")

    sp = sub.add_parser("evolve", parents=[common]); sp.add_argument("--out")
    return p


DISPATCH = {
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
