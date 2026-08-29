#!/usr/bin/env python3
"""
test_instinct.py — stdlib-only regression tests for the Instinct Engine.

Run: python3 tests/test_instinct.py
No pytest dependency. Exits non-zero on any failure. Operates entirely in a
temp dir — never touches real memory.
"""
from __future__ import annotations

import json
import os
import re
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
            check(all(k in fm for k in il.SEED_KEYS), f"{p.name} has all seed keys")
            check("last_exercised" not in fm,
                  f"{p.name} gets NO invented last_exercised at seed time")
            check(fm.get("evidence") == "seed", f"{p.name} is labelled a seed")
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


def _write_shareable_fixture(md: Path, name: str, frontmatter: str, body: str) -> Path:
    p = md / name
    p.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")
    return p


def test_shareable_export():
    """`export --shareable`: type-filters reference/project/untyped instincts
    and fails closed (writes NOTHING) if any surviving instinct's filename or
    body matches a private disclosure pattern.

    The reference-type fixture uses the NESTED `metadata: {type: ...}`
    frontmatter shape -- the shape real memory files actually carry (measured
    live against a real vault file: discovery_308_redirect_kills_failopen_
    reporters.md has `metadata.type: project` despite its discovery_
    filename -- an audit bookmark, not a transferable instinct). The other
    fixtures use the flat top-level `type:` shape (already read elsewhere via
    fm.get("type")), so both schemas this repo's memory files use get
    exercised.
    """
    print("test_shareable_export")
    try:
        import yaml
    except ImportError:
        print("  [SKIP] test_shareable_export: PyYAML not installed (optional dep)")
        return

    # Sanity anchor for the regression Control 3 exists to catch: a bare
    # word-boundary pattern for "onde" must NOT match inside "ondeplan" (no
    # boundary between "e" and "p"). If this ever stopped being true the
    # substring/word-boundary split in DISCLOSURE_PATTERNS would be pointless.
    check(re.search(r"\bonde\b", "ondeplan", re.IGNORECASE) is None,
          r"sanity: \bonde\b does NOT match inside 'ondeplan' (why substring is separate)")

    # --- CONTROL 1 + POSITIVE CONTROL: one pack, nothing disclosed ----------
    with tempfile.TemporaryDirectory() as t:
        md = Path(t) / "Agent Memory"
        md.mkdir(parents=True)
        _write_shareable_fixture(
            md, "discovery_audit_bookmark_reference_type.md",
            "name: audit bookmark example\n"
            "description: private audit note, not a transferable instinct\n"
            "metadata:\n"
            "  node_type: memory\n"
            "  confidence: 0.9\n"
            "  observations: 1\n"
            "  last_seen: 2026-01-01\n"
            "  project_id: global\n"
            "  type: reference",
            "Bookmark only. Nothing transferable here.")
        _write_shareable_fixture(
            md, "feedback_clean_shareable_example.md",
            "name: a perfectly ordinary rule\n"
            "description: ship the fix, verify the tests, done\n"
            "type: feedback\nstrength: explicit",
            "A perfectly ordinary instinct body with no private tokens.")
        cli.cmd_backfill(Args(no_backup=True), md)
        out = Path(t) / "pack.yaml"
        rc = cli.cmd_export(Args(project=None, min_confidence=0.0, all=True,
                                 out=str(out), shareable=True), md)
        check(rc == 0, f"clean fixture + one reference-typed fixture exports OK (rc={rc})")
        check(out.is_file(), "--shareable wrote the output file when nothing disclosed")
        doc = yaml.safe_load(out.read_text()) if out.is_file() else {}
        ids = {r["id"] for r in doc.get("instincts", [])}
        check("discovery_audit_bookmark_reference_type" not in ids,
              "CONTROL 1: metadata.type: reference EXCLUDED by --shareable")
        check("feedback_clean_shareable_example" in ids,
              "POSITIVE CONTROL: clean type: feedback fixture with no tokens EXPORTED")

    # --- CONTROL 2: a brand token in the body fails the WHOLE export --------
    with tempfile.TemporaryDirectory() as t:
        md = Path(t) / "Agent Memory"
        md.mkdir(parents=True)
        leak = _write_shareable_fixture(
            md, "feedback_accidental_brand_leak.md",
            "name: some rule\ndescription: a normal feedback rule\n"
            "type: feedback\nstrength: explicit",
            "Ship the mycelium runtime pattern here.")
        cli.cmd_backfill(Args(no_backup=True), md)
        out = Path(t) / "pack.yaml"
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = cli.cmd_export(Args(project=None, min_confidence=0.0, all=True,
                                     out=str(out), shareable=True), md)
        err = buf.getvalue()
        check(rc != 0, f"CONTROL 2: brand token 'mycelium' in body -> non-zero exit (rc={rc})")
        check(not out.exists(), "CONTROL 2: no output file written on disclosure hit")
        check(leak.name in err, "CONTROL 2: stderr names the offending file")
        check("mycelium" in err.lower(), "CONTROL 2: stderr names the matched token")

    # --- CONTROL 3: the ondeplan regression ----------------------------------
    # A fixture whose BODY says "ondeplan" must be caught by the substring
    # bucket even though the word-boundary \bonde\b pattern would miss it.
    with tempfile.TemporaryDirectory() as t:
        md = Path(t) / "Agent Memory"
        md.mkdir(parents=True)
        leak = _write_shareable_fixture(
            md, "feedback_regression_case_body_token.md",
            "name: some rule\ndescription: a normal feedback rule\n"
            "type: feedback\nstrength: explicit",
            "Verified this against the ondeplan e2e baselines.")
        cli.cmd_backfill(Args(no_backup=True), md)
        out = Path(t) / "pack.yaml"
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = cli.cmd_export(Args(project=None, min_confidence=0.0, all=True,
                                     out=str(out), shareable=True), md)
        err = buf.getvalue()
        check(rc != 0, f"CONTROL 3: 'ondeplan' in body -> non-zero exit (rc={rc})")
        check(not out.exists(), "CONTROL 3: no output file written on disclosure hit")
        check(leak.name in err, "CONTROL 3: stderr names the offending file")
        check("ondeplan" in err.lower(),
              "CONTROL 3: substring pattern catches 'ondeplan' in the body "
              "(the case a word-boundary-only pattern would silently miss)")


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



# ---------------------------------------------------------------------------
# promotion: ledger -> confidence
# ---------------------------------------------------------------------------
def _write_ledgers(tmp: Path, injections, observations):
    """Point the CLI's module-level ledger paths at temp files."""
    import json as _json
    inj = tmp / "injections.jsonl"
    obs = tmp / "observations.jsonl"
    inj.write_text("".join(_json.dumps(r) + "\n" for r in injections), encoding="utf-8")
    obs.write_text("".join(_json.dumps(r) + "\n" for r in observations), encoding="utf-8")
    cli.INJECTIONS_PATH = inj
    cli.OBSERVATIONS_PATH = obs
    cli.PROMOTE_STATE_PATH = tmp / "promote-state.json"
    return inj, obs


def _old_ts(minutes_ago=1440):
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _promote_args(**kw):
    a = Args(every=il.PROMOTE_EVERY,
             min_session_calls=il.PROMOTE_MIN_SESSION_CALLS,
             no_decay=True, reset_state=False)
    a.__dict__.update(kw)
    return a


def _busy(session, n=10):
    return [{"ts": _old_ts(), "session": session, "tool": "Bash"} for _ in range(n)]


def test_promotion_math():
    print("test_promotion_math")
    check(il.promotion_steps(0, 2) == 0, "2 exposures does not cross the 3-gate")
    check(il.promotion_steps(0, 3) == 1, "3 exposures earns exactly one step")
    check(il.promotion_steps(2, 4) == 1, "crossing 3 from 2->4 earns one step")
    check(il.promotion_steps(0, 7) == 2, "7 exposures earns two steps, not seven")
    check(il.promotion_steps(5, 5) == 0, "no exposures, no steps")
    check(il.promotion_steps(0, 9, every=0) == 0, "every=0 cannot divide by zero")
    check(il.evidence_state({}) == "seed", "no exposures -> seed")
    check(il.evidence_state({"exposures": "2"}) == "exercised", "exposures -> exercised")
    check(il.evidence_state({"observations": "4"}) == "reinforced",
          "observations>1 -> reinforced")
    check(il.evidence_state({"observations": "4", "corrections": "1"}) == "corrected",
          "a correction outranks a reinforce")


def test_promote_gate_and_liveness():
    print("test_promote_gate_and_liveness")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        md = make_memory(tmp)
        cli.cmd_backfill(Args(no_backup=True), md)
        slug = "feedback_voice_humanizer"
        before = il.parse_float(il.parse_instinct(md / f"{slug}.md").fm["confidence"])

        # two qualifying sessions -> exposure recorded, gate NOT crossed
        inj = [{"ts": _old_ts(), "session": "s1", "injected": [slug], "explored": []},
               {"ts": _old_ts(), "session": "s2", "injected": [slug], "explored": []}]
        _write_ledgers(tmp, inj, _busy("s1") + _busy("s2"))
        cli.cmd_promote(_promote_args(), md)
        fm = il.parse_instinct(md / f"{slug}.md").fm
        check(il.parse_int(fm.get("exposures"), 0) == 2, "two sessions -> 2 exposures")
        check(abs(il.parse_float(fm["confidence"]) - before) < 1e-9,
              "confidence UNCHANGED below the gate")
        check(fm.get("evidence") == "exercised", "below the gate reads as `exercised`")

        # third qualifying session crosses the gate exactly once
        inj.append({"ts": _old_ts(), "session": "s3", "injected": [slug], "explored": []})
        _write_ledgers(tmp, inj, _busy("s1") + _busy("s2") + _busy("s3"))
        cli.cmd_promote(_promote_args(), md)
        fm = il.parse_instinct(md / f"{slug}.md").fm
        after = il.parse_float(fm["confidence"])
        check(il.parse_int(fm.get("exposures"), 0) == 3, "third session -> 3 exposures")
        check(abs(after - round(il.reinforce_confidence(before), 3)) < 1e-9,
              f"gate crossed -> exactly ONE reinforce step ({before} -> {after})")
        check(fm.get("evidence") == "reinforced", "past the gate reads as `reinforced`")

        # idempotent: the same ledger a second time credits nothing
        cli.cmd_promote(_promote_args(), md)
        fm2 = il.parse_instinct(md / f"{slug}.md").fm
        check(il.parse_int(fm2.get("exposures"), 0) == 3,
              "re-running over the same ledger does NOT double-count")
        check(abs(il.parse_float(fm2["confidence"]) - after) < 1e-9,
              "re-run leaves confidence untouched")


def test_promote_rejects_weak_evidence():
    print("test_promote_rejects_weak_evidence")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        md = make_memory(tmp)
        cli.cmd_backfill(Args(no_backup=True), md)
        slug = "feedback_voice_no_em_dash"

        # THREE injections, but each session did almost no work: an aborted
        # session proves nothing about the instincts it loaded.
        inj = [{"ts": _old_ts(), "session": f"idle{i}", "injected": [slug], "explored": []}
               for i in range(3)]
        obs = []
        for i in range(3):
            obs += _busy(f"idle{i}", n=1)
        _write_ledgers(tmp, inj, obs)
        cli.cmd_promote(_promote_args(), md)
        fm = il.parse_instinct(md / f"{slug}.md").fm
        check(il.parse_int(fm.get("exposures"), 0) == 0,
              "idle sessions contribute ZERO exposures")
        check(fm.get("evidence") == "seed", "never exercised still reads as `seed`")

        # a record from a session that may still be running is left for next run
        _write_ledgers(tmp, [{"ts": _old_ts(minutes_ago=1), "session": "live",
                              "injected": [slug], "explored": []}], _busy("live"))
        cli.cmd_promote(_promote_args(), md)
        fm = il.parse_instinct(md / f"{slug}.md").fm
        check(il.parse_int(fm.get("exposures"), 0) == 0,
              "a still-recent session is not credited yet (settle window)")


def test_promote_credits_explored_slots():
    print("test_promote_credits_explored_slots")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        md = make_memory(tmp)
        cli.cmd_backfill(Args(no_backup=True), md)
        slug = "discovery_some_tool"
        # An EXPLORE pick was genuinely put in front of the agent, so it earns
        # exposure the same way an exploit pick does -- otherwise a below-floor
        # instinct could never acquire evidence and the top-N would freeze.
        inj = [{"ts": _old_ts(), "session": f"x{i}", "injected": [], "explored": [slug]}
               for i in range(3)]
        obs = []
        for i in range(3):
            obs += _busy(f"x{i}")
        _write_ledgers(tmp, inj, obs)
        cli.cmd_promote(_promote_args(), md)
        fm = il.parse_instinct(md / f"{slug}.md").fm
        check(il.parse_int(fm.get("exposures"), 0) == 3, "explored slots earn exposure")
        check(fm.get("evidence") == "reinforced", "an explored instinct can be promoted")


def test_injection_hook_writes_stems():
    print("test_injection_hook_writes_stems")
    import json as _json
    import subprocess
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        md = make_memory(tmp)
        cli.cmd_backfill(Args(no_backup=True), md)
        inj = tmp / "injections.jsonl"
        env = dict(os.environ,
                   INSTINCT_SCRIPTS_DIR=str(ROOT / "scripts"),
                   INSTINCT_MEMORY_DIR=str(md),
                   INSTINCT_INJECTIONS=str(inj),
                   INSTINCT_INJECT_MIN_CONFIDENCE="0.80",
                   INSTINCT_INJECT_LIMIT="12",
                   INSTINCT_INJECT_EXPLORE="2")
        r = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "inject-instinct-context.py")],
            input=_json.dumps({"session_id": "deadbeefcafe", "hook_event_name": "SessionStart"}),
            capture_output=True, text=True, env=env)
        check(r.returncode == 0, f"hook exits 0 (rc={r.returncode}; {r.stderr.strip()[:120]})")
        check(inj.is_file(), "hook wrote an injection ledger record")
        rec = _json.loads(inj.read_text(encoding="utf-8").strip().splitlines()[-1])
        check(rec.get("session") == "deadbeef", "record carries the 8-char session id")
        every = list(rec.get("injected", [])) + list(rec.get("explored", []))
        check(bool(every), "record names at least one instinct")
        stems = {p.stem for p in il.iter_instinct_paths(md)}
        check(all(x in stems for x in every),
              f"every ledger entry is a resolvable FILE STEM, not a display name ({every})")
        # the explore half must actually surface something below the floor
        check(bool(rec.get("explored")), "explore slots are populated below the floor")
        block = _json.loads(r.stdout)
        ctx = block.get("hookSpecificOutput", {}).get("additionalContext", "")
        check("Under evaluation" in ctx, "explore picks are LABELLED as unproven in the block")


def test_promote_reports_unbackfilled():
    print("test_promote_reports_unbackfilled")
    import io, contextlib
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        md = make_memory(tmp)  # deliberately NOT backfilled
        _write_ledgers(tmp, [{"ts": _old_ts(), "session": "s1",
                              "injected": ["feedback_voice_humanizer"], "explored": []}],
                       _busy("s1"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cli.cmd_promote(_promote_args(), md)
        msg = err.getvalue()
        check("no stored confidence" in msg and "backfill" in msg,
              "un-backfilled instincts are reported LOUDLY, not silently skipped")



def test_promote_state_three_ways():
    print("test_promote_state_three_ways")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        md = make_memory(tmp)
        cli.cmd_backfill(Args(no_backup=True), md)
        slug = "feedback_voice_humanizer"
        inj = [{"ts": _old_ts(), "session": f"s{i}", "injected": [slug], "explored": []}
               for i in range(3)]
        obs = []
        for i in range(3):
            obs += _busy(f"s{i}")
        _write_ledgers(tmp, inj, obs)
        cli.cmd_promote(_promote_args(), md)
        exp1 = il.parse_int(il.parse_instinct(md / f"{slug}.md").fm.get("exposures"), 0)
        check(exp1 == 3, "baseline: 3 exposures credited")

        # ABSENT state is a legitimate first run; CORRUPT state is not the
        # same answer. Reading a corrupt file as "nothing credited yet" would
        # re-credit every session still in the ledger.
        cli.PROMOTE_STATE_PATH.write_text("{ this is not json", encoding="utf-8")
        raised = False
        try:
            cli.cmd_promote(_promote_args(), md)
        except cli.PromoteStateError:
            raised = True
        check(raised, "a CORRUPT state file raises instead of silently re-crediting")
        exp2 = il.parse_int(il.parse_instinct(md / f"{slug}.md").fm.get("exposures"), 0)
        check(exp2 == exp1, "the refused run mutated nothing")
        rc = cli.main(["promote", "--memory-dir", str(md)])
        check(rc == 2, f"a refusal exits NON-ZERO so a cron log shows it (rc={rc})")

        # the cursor makes a truncated session set harmless
        cli.PROMOTE_STATE_PATH.write_text(
            json.dumps({"credited_sessions": [], "cursor_ts": _old_ts(minutes_ago=60)}),
            encoding="utf-8")
        cli.cmd_promote(_promote_args(), md)
        exp3 = il.parse_int(il.parse_instinct(md / f"{slug}.md").fm.get("exposures"), 0)
        check(exp3 == exp1,
              "records at/behind the cursor are NOT re-credited when the "
              f"session set is empty (got {exp3}, want {exp1})")


def test_promote_refuses_concurrent_run():
    print("test_promote_refuses_concurrent_run")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        md = make_memory(tmp)
        cli.cmd_backfill(Args(no_backup=True), md)
        _write_ledgers(tmp, [{"ts": _old_ts(), "session": "s1",
                              "injected": ["feedback_voice_humanizer"], "explored": []}],
                       _busy("s1"))
        lock = cli.PROMOTE_STATE_PATH.with_suffix(".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("", encoding="utf-8")
        raised = False
        try:
            cli.cmd_promote(_promote_args(), md)
        except cli.PromoteStateError:
            raised = True
        check(raised, "a held lock refuses a second concurrent pass")
        fm = il.parse_instinct(md / "feedback_voice_humanizer.md").fm
        check(il.parse_int(fm.get("exposures"), 0) == 0, "the refused pass credited nothing")
        lock.unlink()
        cli.cmd_promote(_promote_args(), md)
        fm = il.parse_instinct(md / "feedback_voice_humanizer.md").fm
        check(il.parse_int(fm.get("exposures"), 0) == 1,
              "once the lock is released the pass runs normally")
        check(not lock.exists(), "the lock is released on exit")


def main():
    print("=== Instinct Engine regression tests ===")
    test_confidence_math()
    test_surgical_frontmatter()
    test_backfill_and_correct()
    test_export_import_roundtrip()
    test_shareable_export()
    test_evolve()
    test_project_scoping()
    test_reseed()
    test_cli_invocation()
    test_promotion_math()
    test_promote_gate_and_liveness()
    test_promote_rejects_weak_evidence()
    test_promote_credits_explored_slots()
    test_injection_hook_writes_stems()
    test_promote_reports_unbackfilled()
    test_promote_state_three_ways()
    test_promote_refuses_concurrent_run()
    print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURE(S): ' + '; '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
