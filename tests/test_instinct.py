#!/usr/bin/env python3
"""
test_instinct.py — stdlib-only regression tests for the Instinct Engine.

Run: python3 tests/test_instinct.py
No pytest dependency. Exits non-zero on any failure. Operates entirely in a
temp dir — never touches real memory.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import instinct_lib as il      # noqa: E402
import instinct as cli         # noqa: E402

FAILS = []


def check(cond, msg):
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILS.append(msg)


def make_memory(tmp: Path) -> Path:
    md = tmp / "Agent Memory"
    md.mkdir(parents=True)
    (md / "feedback_voice_no_em_dash.md").write_text(
        "---\nname: No em dashes in external prose\n"
        "description: ban em dashes in Substack/LinkedIn/investor\n"
        "type: feedback\nstrength: explicit\n---\n"
        "Body line one.\nBody line two with a [[wikilink]].\n", encoding="utf-8")
    (md / "feedback_voice_humanizer.md").write_text(
        "---\nname: Run humanizer before external prose\n"
        "description: voice firewall pass on Substack drafts\n"
        "type: feedback\nstrength: correction\n---\nVoice body.\n", encoding="utf-8")
    (md / "discovery_some_tool.md").write_text(
        "---\nname: Some git discovery\n"
        "description: git worktree commit branch trick\n"
        "type: discovery\ncreated: 2026-01-01\n---\nGit body.\n", encoding="utf-8")
    return md


class Args:  # lightweight argparse stand-in
    def __init__(self, **kw):
        self.dry_run = False
        self.__dict__.update(kw)


def test_confidence_math():
    print("test_confidence_math")
    check(il.seed_confidence("explicit") == 0.90, "explicit seeds 0.90")
    check(il.seed_confidence("correction") == 0.75, "correction seeds 0.75")
    check(il.seed_confidence("implicit") == 0.50, "implicit seeds 0.50")
    check(il.seed_confidence(None) == il.SEED_DEFAULT, "no-strength seeds default")
    check(il.seed_confidence(None, "feedback", "you MUST always commit") == il.SEED_FEEDBACK_CODIFIED,
          "feedback + codified-rule language seeds high (0.82)")
    check(il.seed_confidence(None, "feedback", "a mild preference here") == il.SEED_FEEDBACK,
          "feedback w/o hard-rule language seeds 0.72")
    check(il.seed_confidence(None, "discovery", "an audit finding") == il.SEED_DISCOVERY,
          "discovery seeds 0.60")
    check(il.seed_confidence("explicit", "discovery", "x") == 0.90,
          "explicit strength overrides type-based seed")
    c0 = 0.5
    c1 = il.reinforce_confidence(c0)
    check(c0 < c1 < 1.0, f"reinforce increases + bounded ({c0}->{c1})")
    check(il.reinforce_confidence(0.99) <= il.CONF_CEIL, "reinforce respects ceiling")
    check(il.correct_confidence(0.8) == 0.4, "correct halves (0.8->0.4)")
    check(il.correct_confidence(0.05) >= il.CONF_FLOOR, "correct respects floor")
    today = date(2026, 5, 29)
    check(il.decayed_confidence(0.9, today - timedelta(days=10), today) == 0.9,
          "no decay within grace window")
    stale = il.decayed_confidence(0.9, today - timedelta(days=210), today)
    check(stale < 0.9, f"decay past grace erodes (0.9->{stale:.3f})")
    check(stale >= il.CONF_FLOOR, "decay respects floor")


def test_surgical_frontmatter():
    print("test_surgical_frontmatter")
    with tempfile.TemporaryDirectory() as t:
        md = make_memory(Path(t))
        p = md / "feedback_voice_no_em_dash.md"
        original = p.read_text()
        inst = il.parse_instinct(p)
        new = il.set_managed_fields(inst, {"confidence": 0.9, "observations": 1,
                                           "last_seen": date(2026, 5, 1),
                                           "project_id": "global"})
        il.write_instinct(inst, new)
        after = p.read_text()
        check("name: No em dashes in external prose" in after, "preserved name key")
        check("strength: explicit" in after, "preserved strength key")
        check("Body line two with a [[wikilink]]." in after, "preserved body verbatim")
        check("confidence: 0.9" in after, "added confidence")
        check("project_id: global" in after, "added project_id")
        bak = p.with_suffix(p.suffix + ".bak-instinct")
        check(bak.exists() and bak.read_text() == original, "one-time .bak-instinct of pre-state")
        # idempotency: re-applying identical values writes nothing new
        inst2 = il.parse_instinct(p)
        same = il.set_managed_fields(inst2, {"confidence": 0.9, "observations": 1,
                                             "last_seen": date(2026, 5, 1),
                                             "project_id": "global"})
        check(same == p.read_text(), "re-apply identical = no diff (idempotent)")


def test_backfill_and_correct():
    print("test_backfill_and_correct")
    with tempfile.TemporaryDirectory() as t:
        md = make_memory(Path(t))
        cli.cmd_backfill(Args(), md)
        # every instinct now has all managed keys
        for p in il.iter_instinct_paths(md):
            fm = il.parse_instinct(p).fm
            check(all(k in fm for k in il.MANAGED_KEYS), f"{p.name} has all managed keys")
        # explicit memory seeded 0.90
        em = il.parse_instinct(md / "feedback_voice_no_em_dash.md")
        check(il.parse_float(em.get("confidence")) == 0.9, "explicit backfilled to 0.90")
        # Done criterion: a corrected pattern's confidence drops on next run
        before = il.parse_float(il.parse_instinct(md / "feedback_voice_humanizer.md").get("confidence"))
        cli.cmd_correct(Args(ident="feedback_voice_humanizer"), md)
        after = il.parse_float(il.parse_instinct(md / "feedback_voice_humanizer.md").get("confidence"))
        check(after < before, f"correct drops confidence ({before}->{after}) [DONE criterion]")
        # reinforce climbs
        b2 = after
        cli.cmd_reinforce(Args(ident="feedback_voice_humanizer"), md)
        a2 = il.parse_float(il.parse_instinct(md / "feedback_voice_humanizer.md").get("confidence"))
        check(a2 > b2, f"reinforce climbs back ({b2}->{a2})")
        obs = il.parse_int(il.parse_instinct(md / "feedback_voice_humanizer.md").get("observations"))
        check(obs >= 2, f"reinforce bumped observations ({obs})")


def test_export_import_roundtrip():
    print("test_export_import_roundtrip")
    try:
        import yaml  # noqa
    except ImportError:
        # PyYAML is an OPTIONAL dep (only the export/import skill needs it). The CI
        # gate runs stdlib-only (setup-python, no site-packages), so skip this
        # feature-test when yaml is absent rather than failing the whole gate — same
        # fail-open posture the gate uses for ruff/shellcheck. Runs fully wherever
        # PyYAML is installed (local dev, a real install). (MYC-2959 follow-up.)
        print("  [SKIP] test_export_import_roundtrip: PyYAML not installed (optional dep)")
        return
    with tempfile.TemporaryDirectory() as t:
        md = make_memory(Path(t))
        cli.cmd_backfill(Args(), md)
        out = Path(t) / "pack.yaml"
        cli.cmd_export(Args(project=None, min_confidence=0.0, all=True, out=str(out)), md)
        check(out.is_file(), "export wrote a YAML pack")
        doc = yaml.safe_load(out.read_text())
        check(doc.get("exported_count", 0) >= 3, "pack carries >=3 instincts")
        keys = set(doc["instincts"][0].keys())
        check({"id", "trigger", "confidence", "domain", "source_repo"} <= keys,
              "instinct carries id/trigger/confidence/domain/source_repo")
        # import into a FRESH memory dir
        fresh = Path(t) / "fresh" / "Agent Memory"
        fresh.mkdir(parents=True)
        cli.cmd_import(Args(file=str(out)), fresh)
        inh = fresh / "inherited"
        check(inh.is_dir() and any(inh.glob("*.md")), "import created inherited/*.md")
        n_inh = len(list(inh.glob("*.md")))
        # re-import same pack: equal confidence -> all skipped (no new files)
        cli.cmd_import(Args(file=str(out)), fresh)
        check(len(list(inh.glob("*.md"))) == n_inh, "re-import equal-confidence = skipped (idempotent)")
        # higher-confidence import updates an existing local
        # reinforce one local then re-export higher, import into md (where it exists)
        target = "feedback_voice_no_em_dash"
        before = il.parse_float(il.parse_instinct(md / f"{target}.md").get("confidence"))
        # craft a higher-confidence pack
        hi = Path(t) / "hi.yaml"
        hi.write_text(yaml.safe_dump({"instincts": [
            {"id": target, "trigger": "x", "confidence": 0.99, "domain": "voice", "source_repo": "global",
             "action": "a", "evidence": "e"}]}), encoding="utf-8")
        cli.cmd_import(Args(file=str(hi)), md)
        after = il.parse_float(il.parse_instinct(md / f"{target}.md").get("confidence"))
        check(after > before, f"higher-confidence import updates local ({before}->{after})")


def test_evolve():
    print("test_evolve")
    with tempfile.TemporaryDirectory() as t:
        md = make_memory(Path(t))
        # add more voice instincts so the voice cluster crosses the propose bar
        for i in range(3):
            (md / f"feedback_voice_extra_{i}.md").write_text(
                f"---\nname: voice rule {i}\ndescription: substack prose tone humanizer rule {i}\n"
                f"type: feedback\nstrength: explicit\n---\nvoice body {i}\n", encoding="utf-8")
        cli.cmd_backfill(Args(), md)
        out = Path(t) / "proposals"
        rc = cli.cmd_evolve(Args(out=str(out)), md)
        check(rc == 0, "evolve ran")
        proposals = list(out.glob("proposed-skill-*.md")) if out.is_dir() else []
        check(len(proposals) >= 1, f"evolve wrote >=1 proposed skill file ({len(proposals)})")
        if proposals:
            txt = proposals[0].read_text()
            check("status: proposed" in txt and "Member instincts" in txt,
                  "proposal scaffold well-formed")


def test_project_scoping():
    print("test_project_scoping")
    with tempfile.TemporaryDirectory() as t:
        md = make_memory(Path(t))
        cli.cmd_backfill(Args(), md)
        # tag one instinct to a specific project
        p = md / "discovery_some_tool.md"
        inst = il.parse_instinct(p)
        il.write_instinct(inst, il.set_managed_fields(inst, {"project_id": "repo:concierge"}))
        # report filtered to a DIFFERENT project: the repo:concierge one is hidden,
        # globals still show
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_report(Args(project="repo:other", min_confidence=0.0, stale=False,
                                json=True, limit=None), md)
        import json as _json
        data = _json.loads(buf.getvalue())
        slugs = {r["slug"] for r in data["instincts"]}
        check("discovery_some_tool" not in slugs, "project-scoped instinct hidden in other project")
        check("feedback_voice_no_em_dash" in slugs, "global instinct still visible everywhere")


def test_reseed():
    print("test_reseed")
    with tempfile.TemporaryDirectory() as t:
        md = Path(t) / "Agent Memory"
        md.mkdir(parents=True)
        # a feedback rule backfilled under the OLD flat 0.60 seed, codified body
        (md / "feedback_codified_rule.md").write_text(
            "---\nname: always commit\ntype: feedback\nconfidence: 0.6\n"
            "observations: 1\nlast_seen: 2026-05-01\nproject_id: global\n---\n"
            "You must ALWAYS commit when done. Never skip.\n", encoding="utf-8")
        # a reinforced instinct that must NOT be reset
        (md / "feedback_earned.md").write_text(
            "---\nname: earned\ntype: feedback\nconfidence: 0.95\n"
            "observations: 5\nlast_seen: 2026-05-20\nproject_id: global\n---\nbody\n", encoding="utf-8")
        # an explicit-strength instinct that must NOT be reset
        (md / "feedback_explicit.md").write_text(
            "---\nname: explicit\ntype: feedback\nstrength: explicit\nconfidence: 0.9\n"
            "observations: 1\nlast_seen: 2026-05-20\nproject_id: global\n---\nbody\n", encoding="utf-8")
        cli.cmd_reseed(Args(no_backup=True), md)
        c1 = il.parse_float(il.parse_instinct(md / "feedback_codified_rule.md").get("confidence"))
        check(c1 == il.SEED_FEEDBACK_CODIFIED,
              f"reseed lifts codified feedback 0.6 -> {il.SEED_FEEDBACK_CODIFIED} (got {c1})")
        ls1 = il.parse_date(il.parse_instinct(md / "feedback_codified_rule.md").get("last_seen"))
        check(ls1 == cli._today(), "reseed resets last_seen to engine-birth (today)")
        c2 = il.parse_float(il.parse_instinct(md / "feedback_earned.md").get("confidence"))
        check(c2 == 0.95, "reseed leaves reinforced (observations>1) instinct untouched")
        c3 = il.parse_float(il.parse_instinct(md / "feedback_explicit.md").get("confidence"))
        check(c3 == 0.9, "reseed leaves explicit-strength instinct untouched")


def test_cli_invocation():
    print("test_cli_invocation")
    import subprocess
    with tempfile.TemporaryDirectory() as t:
        md = make_memory(Path(t))
        cli_path = str(ROOT / "scripts" / "instinct.py")
        # --memory-dir must work AFTER the subcommand (the natural position)
        r = subprocess.run([sys.executable, cli_path, "backfill", "--memory-dir", str(md), "--no-backup"],
                           capture_output=True, text=True)
        check(r.returncode == 0, f"`backfill --memory-dir X` exits 0 (rc={r.returncode}; {r.stderr.strip()[:80]})")
        r = subprocess.run([sys.executable, cli_path, "report", "--memory-dir", str(md), "--json"],
                           capture_output=True, text=True)
        check(r.returncode == 0 and '"instincts"' in r.stdout, "`report --json` exits 0 with JSON")
        # --no-backup leaves no .bak-instinct files
        check(not list(md.glob("*.bak-instinct")), "--no-backup leaves no .bak-instinct siblings")


def main():
    print("=== Instinct Engine regression tests ===")
    test_confidence_math()
    test_surgical_frontmatter()
    test_backfill_and_correct()
    test_export_import_roundtrip()
    test_evolve()
    test_project_scoping()
    test_reseed()
    test_cli_invocation()
    print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURE(S): ' + '; '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
