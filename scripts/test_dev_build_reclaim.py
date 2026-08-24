#!/usr/bin/env python3
"""Controls for dev-build-reclaim (MYC-3727).

This script DELETES directories, so its controls are about what it REFUSES to
delete at least as much as what it reclaims. A reclaimer earns trust only by
preserving what it must never touch; every case below is a way this tool could
destroy real work.

  [0] idle worktree            -> node_modules/.venv/.next RECLAIMED (it works)
  [1] active worktree          -> nothing touched (in-flight work)
  [2] target/ without Cargo    -> PRESERVED (a source dir merely named "target")
  [3] target/ with Cargo.toml  -> RECLAIMED (real Rust output)
  [4] LIVE .session-lock       -> PRESERVED (a session is using it)
  [5] source + .git            -> never touched, ever
  [6] dry-run default          -> deletes NOTHING without --apply

Regressions this suite exists to keep closed (2026-08-05 — the disk still hit
2.3 GB free with the tool running green):

  [7] build-touched worktree   -> RECLAIMED (writing target/ must NOT read as an
                                  edit; the idle signal was contaminated by the
                                  very activity it was measuring)
  [8] edited-source worktree   -> PRESERVED (the [7] fix must not overreach)
  [9] pressure ladder          -> threshold tightens as free space falls
 [10] symlinked artifact       -> unlinked, and its TARGET never followed

Ported to the substrate with four controls the private original never had:

 [11] STALE .session-lock      -> does NOT protect (a crashed session must not
                                  shield a dead worktree's artifacts forever,
                                  which would defeat the 0-day pressure rung
                                  exactly when the disk is full)
 [12] proportional ladder      -> rungs scale with the VOLUME, so the tool is not
                                  calibrated to one machine's ~1 TB disk
 [13] truncated source walk    -> PRESERVED and REPORTED (a partial walk sees
                                  only a prefix of the source, so it can call an
                                  actively-edited worktree idle — an unmeasurable
                                  worktree must read as in-use, and say so)
 [14] manifest honors the log-dir override (a test must never write into the
      operator's real audit trail, where a fixture is indistinguishable from a
      real reclaim)

Run: python3 scripts/test_dev_build_reclaim.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
TOOL = SCRIPTS / "dev-build-reclaim.py"

# This suite walks a tree recursively (backdate) AND reads file content (the
# manifest), so it goes through the repo's shared cloud-safe primitive like every
# other recursive reader in the fleet — a dataless cloud placeholder must never
# be force-materialized just to run a test.
sys.path.insert(0, str(SCRIPTS.parent / "hooks"))
from _lib.safe_read import safe_read_text  # noqa: E402

FAILED: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
        FAILED.append(label)


def ok(label: str, cond: bool) -> None:
    check(label, bool(cond), True)


def load_module():
    spec = importlib.util.spec_from_file_location("dev_build_reclaim", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mkwt(root: Path, name: str) -> Path:
    """Build a worktree tree. Backdate SEPARATELY and always last."""
    wt = root / name
    (wt / "src").mkdir(parents=True)
    (wt / ".git").mkdir()
    (wt / "src" / "main.ts").write_text("real source", encoding="utf-8")
    (wt / ".git" / "HEAD").write_text("gitdir: whatever", encoding="utf-8")
    (wt / "package.json").write_text("{}", encoding="utf-8")
    for art, child in (("node_modules", "pkg/i.js"), (".venv", "lib/p.py"),
                       (".next", "cache/c")):
        p = wt / art / Path(child).parent
        p.mkdir(parents=True)
        (wt / art / child).write_text("x", encoding="utf-8")
    return wt


def backdate(path: Path, days: float) -> None:
    """Age a whole worktree. Recursive by design: the tool measures idleness from
    the newest SOURCE entry (files and sub-dirs), not a fixed probe list, so
    aging only a couple of probes would leave real files reading as active.
    The root goes LAST — touching a child bumps its parent."""
    old = time.time() - days * 86400
    paths = sorted(path.rglob("*"), key=lambda p: -len(p.parts))
    for p in paths + [path]:
        try:
            os.utime(p, (old, old), follow_symlinks=False)
        except (OSError, NotImplementedError):
            pass


def run(root: Path, *args: str, log_dir: Path | None = None):
    env = dict(os.environ, DEV_BUILD_RECLAIM_LOG_DIR=str(log_dir or (root / "_logs")))
    return subprocess.run(
        [sys.executable, str(TOOL), "--dev-root", str(root), *args],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)


def main() -> int:  # noqa: C901
    mod = load_module()

    # ---- [0] + [5] idle worktree reclaimed, source and git survive ----------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wt = mkwt(root, "idle-wt")
        backdate(wt, 30)
        run(root, "--apply", "--min-idle-days", "7")
        ok("[0] idle node_modules reclaimed", not (wt / "node_modules").exists())
        ok("[0] idle .venv reclaimed", not (wt / ".venv").exists())
        ok("[0] idle .next reclaimed", not (wt / ".next").exists())
        ok("[5] source file preserved", (wt / "src" / "main.ts").is_file())
        ok("[5] .git preserved", (wt / ".git" / "HEAD").is_file())

    # ---- [1] active worktree untouched -------------------------------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wt = mkwt(root, "active-wt")          # deliberately NOT backdated
        run(root, "--apply", "--min-idle-days", "7")
        ok("[1] active worktree preserved", (wt / "node_modules").is_dir())

    # ---- [2] target/ without Cargo.toml is source, not output ---------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wt = mkwt(root, "src-target")
        (wt / "target" / "keep").mkdir(parents=True)
        (wt / "target" / "keep" / "f.txt").write_text("source!", encoding="utf-8")
        backdate(wt, 30)
        run(root, "--apply", "--min-idle-days", "7")
        ok("[2] target/ without Cargo.toml preserved",
           (wt / "target" / "keep" / "f.txt").is_file())

    # ---- [3] target/ with Cargo.toml is real build output ------------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wt = mkwt(root, "rust-wt")
        (wt / "Cargo.toml").write_text("[package]", encoding="utf-8")
        (wt / "target" / "debug").mkdir(parents=True)
        (wt / "target" / "debug" / "bin").write_text("x", encoding="utf-8")
        backdate(wt, 30)
        run(root, "--apply", "--min-idle-days", "7")
        ok("[3] Rust target/ reclaimed", not (wt / "target").exists())

    # ---- [4] a LIVE session lock preserves ---------------------------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wt = mkwt(root, "locked-wt")
        backdate(wt, 30)
        (wt / ".session-lock").touch()        # fresh: written AFTER backdating
        r = run(root, "--apply", "--min-idle-days", "7")
        ok("[4] live session lock preserved", (wt / "node_modules").is_dir())
        # Preserved for the RIGHT reason. The lock is machinery, not source: if
        # it counted toward the idle reading the worktree would be spared by the
        # idle threshold instead, the lock check would never run, and a stale
        # lock would then keep the worktree looking "active" for a whole idle
        # window after its session died.
        ok("[4] the preservation is REPORTED, not silent",
           "live session lock" in r.stdout)

    # ---- [11] a STALE session lock does NOT protect ------------------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wt = mkwt(root, "stale-locked-wt")
        (wt / ".session-lock").touch()
        backdate(wt, 30)                      # ages the lock too
        run(root, "--apply", "--min-idle-days", "7")
        ok("[11] stale session lock does not shield artifacts forever",
           not (wt / "node_modules").exists())

    # ---- [6] dry-run deletes nothing ---------------------------------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wt = mkwt(root, "dry-wt")
        backdate(wt, 30)
        r = run(root, "--min-idle-days", "7")
        ok("[6] dry-run deleted nothing", (wt / "node_modules").is_dir())
        ok("[6] dry-run says so", "WOULD RECLAIM" in r.stdout)

    # ---- [7] a BUILD must not disguise an abandoned worktree as active -----
    # THE 2026-08-05 REGRESSION. Worktree dormant 30d, then a compile writes into
    # target/ — which bumps the ROOT dir's mtime. A root-probe metric read that as
    # "active" and protected the artifact forever. Reclaim must still fire.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wt = mkwt(root, "built-wt")
        (wt / "Cargo.toml").write_text("[package]", encoding="utf-8")
        backdate(wt, 30)
        (wt / "target" / "debug").mkdir(parents=True)      # the "build", now
        (wt / "target" / "debug" / "bin").write_text("x", encoding="utf-8")
        ok("[7] precondition: target/ exists before the run", (wt / "target").is_dir())
        run(root, "--apply", "--min-idle-days", "7")
        ok("[7] build-touched worktree still reclaimed", not (wt / "target").exists())
        ok("[7] its node_modules reclaimed too", not (wt / "node_modules").exists())
        ok("[7] source still preserved", (wt / "src" / "main.ts").is_file())

    # ---- [8] a REAL source edit still protects (the [7] fix must not overreach)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wt = mkwt(root, "edited-wt")
        backdate(wt, 30)
        (wt / "src" / "main.ts").write_text("someone is working here", encoding="utf-8")
        run(root, "--apply", "--min-idle-days", "7")
        ok("[8] edited worktree preserved", (wt / "node_modules").is_dir())

    # ---- [9] the absolute ladder tightens as free space falls --------------
    got = [mod.ladder_idle_days(f) for f in (500, 149, 79, 39, 19)]
    check("[9] ladder: none/7/3/1/0 as free space falls",
          got, [None, 7.0, 3.0, 1.0, 0.0])

    # ---- [12] the ladder scales with the VOLUME, not one machine's disk ----
    # A flat 150 GB trigger is comfortable on a 500 GB laptop and a five-alarm
    # fire on a 4 TB workstation. Rungs are max(absolute, pct-of-volume).
    check("[12] 4 TB volume: top rung triggers at 16% (~655 GB), not 150 GB",
          round(mod.ladder_caps(4096.0)[-1]), 655)
    check("[12] 256 GB laptop keeps the absolute floors (proportional is smaller)",
          mod.ladder_caps(256.0), [20.0, 40.0, 80.0, 150.0])
    check("[12] 500 GB free on a 4 TB volume is PRESSURE (7d), not healthy",
          mod.ladder_idle_days(500.0, 4096.0), 7.0)
    check("[12] 300 GB free on a 4 TB volume (7%) tightens further, to 3d",
          mod.ladder_idle_days(300.0, 4096.0), 3.0)
    check("[12] the same 300 GB free on a 1 TB volume is healthy (no-op)",
          mod.ladder_idle_days(300.0, 1024.0), None)
    check("[12] a small disk still bottoms out at the 0-day rung",
          mod.ladder_idle_days(15.0, 256.0), 0.0)

    # ---- [10] symlinked artifact unlinked, its target never followed -------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wt = mkwt(root, "sym-wt")
        outside = root / "outside-store" / "real"
        outside.mkdir(parents=True)
        (outside / "keep.txt").write_text("MUST SURVIVE", encoding="utf-8")
        import shutil as _sh
        _sh.rmtree(wt / "node_modules")
        try:
            (wt / "node_modules").symlink_to(root / "outside-store",
                                             target_is_directory=True)
        except (OSError, NotImplementedError):
            print("  skip [10] symlinks unavailable on this platform")
        else:
            backdate(wt, 30)
            run(root, "--apply", "--min-idle-days", "7")
            ok("[10] symlinked artifact unlinked",
               not (wt / "node_modules").exists() and not (wt / "node_modules").is_symlink())
            ok("[10] the link TARGET was never followed", (outside / "keep.txt").is_file())

    # ---- [13] a truncated source walk preserves, and says why --------------
    # Drives the REAL plan() with the bound lowered, because a control that only
    # checked the arithmetic would still pass if truncation were never wired to
    # the preserve decision.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wt = mkwt(root, "huge-wt")
        for i in range(40):
            (wt / "src" / f"f{i}.ts").write_text("x", encoding="utf-8")
        backdate(wt, 30)
        saved = mod.MAX_SOURCE_ENTRIES
        try:
            mod.MAX_SOURCE_ENTRIES = 3
            items, skipped = mod.plan(root, 7.0, time.time())
        finally:
            mod.MAX_SOURCE_ENTRIES = saved
        ok("[13] a worktree whose source cannot be measured is NOT reclaimed",
           items == [])
        ok("[13] and the preservation is reported, never a silent skip",
           any("only a prefix" in s for s in skipped))
        # ...and with the real bound the same worktree IS reclaimable, so [13]
        # proves truncation, not a broken fixture.
        items2, _ = mod.plan(root, 7.0, time.time())
        ok("[13] positive control: under the real bound it is reclaimable",
           len(items2) > 0)

    # ---- [16] an UNREADABLE source is unmeasurable, never "infinitely idle" --
    # Every lstat in the idle walk is guarded so one bad entry cannot abort a
    # fleet run. If they ALL fail, the newest-mtime stays 0.0 — which read as
    # infinitely idle and would delete the artifacts of a worktree the tool
    # could not measure at all. The guarded exception must not become a delete
    # authorization.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wt = mkwt(root, "unreadable-wt")
        backdate(wt, 30)
        real_lstat = mod.os.lstat

        def _deny(*_a, **_k):
            raise PermissionError(13, "Permission denied")

        try:
            mod.os.lstat = _deny
            items, skipped = mod.plan(root, 7.0, time.time())
        finally:
            mod.os.lstat = real_lstat
        ok("[16] a worktree whose source cannot be READ is not reclaimed",
           items == [])
        ok("[16] and the reason is reported, not a silent skip",
           any("could be read" in s for s in skipped))
        # Positive control: with lstat working, the same fixture IS reclaimable,
        # so [16] proves the unreadable path and not a dead fixture.
        items2, _ = mod.plan(root, 7.0, time.time())
        ok("[16] positive control: readable again, it is reclaimable",
           len(items2) > 0)

    # ---- [14] the manifest honors the log-dir override ---------------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wt = mkwt(root, "manifest-wt")
        backdate(wt, 30)
        logs = root / "audit-elsewhere"
        run(root, "--apply", "--min-idle-days", "7", log_dir=logs)
        manifests = list(logs.glob("build-reclaim-*.jsonl")) if logs.exists() else []
        ok("[14] a manifest was written to the override dir", len(manifests) == 1)
        if manifests:
            # A failed read is not an empty answer: classify it, never coalesce
            # to "" — an unreadable manifest would otherwise pass as "0 rows".
            read = safe_read_text(manifests[0])
            ok("[14] the manifest is readable", read.ok)
            rows = [json.loads(ln) for ln in (read.text or "").splitlines()
                    if ln.strip()]
            ok("[14] every reclaimed dir is in the audit trail", len(rows) >= 3)
            ok("[14] each row records what, how big, and how idle",
               all({"path", "size_mb", "idle_days", "ts"} <= set(r) for r in rows))

    # ---- [15] --min-free-gb keeps a healthy disk a strict no-op ------------
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wt = mkwt(root, "healthy-wt")
        backdate(wt, 30)
        r = run(root, "--apply", "--min-free-gb", "0")
        ok("[15] --min-free-gb 0 never deletes", (wt / "node_modules").is_dir())
        ok("[15] and reports the no-op", "no-op" in r.stdout)

    # ---- [17] the session lock is read in its REAL shape and REAL location ---
    # The guard that shipped probed `<wt>/.session-lock`. Measured 2026-08-06:
    # ZERO files of that name existed anywhere under the dev root, so the guard
    # was dead in production while reading as "no session here". These controls
    # use the shape session-lock.py actually writes — `.claude/.session-lock.json`,
    # a multi-session map keyed on `last_activity_at` — and the second puts it at
    # the git-common-dir root, where a sibling worktree's lock really lives.
    # Both would have passed silently before the fix.
    import subprocess as _sp
    from _lib.dev_repo_scan import has_live_session_lock as _live

    def lock_payload(age_sec: float, schema: str = "v2") -> dict:
        """A lock in the shape the WRITER actually produces.

        `v2` is what session-lock.py writes today: sessions is a **dict** keyed
        by session id. An earlier version of this control wrote a LIST, which is
        why it passed against a reader that could only handle lists — a control
        in the wrong shape proves nothing about the real file. The other shapes
        are kept because the reader must still handle a lock written by an older
        install mid-upgrade.
        """
        entry = {"started_at": time.time() - age_sec - 60,
                 "last_activity_at": time.time() - age_sec,
                 "cwd": "/tmp/x", "pid": 1234}
        if schema == "v2":
            return {"version": 2, "sessions": {"sid-1": entry}}
        if schema == "legacy-list":
            return {"version": 1, "sessions": [entry]}
        return {"session_id": "sid-1", **entry}          # v1 flat

    def write_real_lock(where: Path, age_sec: float, schema: str = "v2") -> None:
        (where / ".claude").mkdir(parents=True, exist_ok=True)
        (where / ".claude" / ".session-lock.json").write_text(
            json.dumps(lock_payload(age_sec, schema)), encoding="utf-8")

    # Every schema the lock has worn must read live. v2 is the load-bearing one:
    # the shipped reader iterated `sessions` as a list, so on the real v2 dict it
    # yielded string KEYS, failed every isinstance check, and reported "no live
    # session" on a file holding two.
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td)
        for schema in ("v2", "legacy-list", "v1-flat"):
            (probe / ".claude").mkdir(parents=True, exist_ok=True)
            (probe / ".claude" / ".session-lock.json").write_text(
                json.dumps(lock_payload(5, schema)), encoding="utf-8")
            ok(f"[17] lock schema `{schema}` reads as LIVE", _live(probe))
        # ...and the pre-fix list-only parse is proven inadequate for v2.
        v2 = lock_payload(5, "v2")
        listish = [s for s in v2.get("sessions", []) if isinstance(s, dict)]
        ok("[17] a list-only parse finds ZERO entries in the real v2 lock",
           listish == [])

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        wt = mkwt(root, "real-lock-wt")
        backdate(wt, 30)
        write_real_lock(wt, age_sec=5)
        ok("[17] a REAL .session-lock.json in the worktree is detected", _live(wt))
        # The load-bearing half: the RETIRED probe set would have found nothing,
        # so this control fails against the pre-fix code rather than passing anyway.
        ok("[17] and the retired `.session-lock` probe would have MISSED it",
           not (wt / ".session-lock").exists()
           and not (wt / ".claude" / ".session-lock").exists())
        items, skipped = mod.plan(root, 7.0, time.time())
        ok("[17] so the worktree is PRESERVED end-to-end", items == [])
        ok("[17] and reported as a live session lock",
           any("live session lock" in s for s in skipped))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # A sibling worktree keeps its lock at the MAIN checkout, never inside
        # itself. This is the case that was invisible in production.
        main = root / "mainrepo"
        main.mkdir()
        _sp.run(["git", "-C", str(main), "init", "-q"], check=True, capture_output=True)
        (main / "f.txt").write_text("x", encoding="utf-8")
        _sp.run(["git", "-C", str(main), "add", "-A"], check=True, capture_output=True)
        _sp.run(["git", "-C", str(main), "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-qm", "init"], check=True, capture_output=True)
        wt = root / "mainrepo-slug"
        _sp.run(["git", "-C", str(main), "worktree", "add", "-q", "--detach", str(wt)],
                check=True, capture_output=True)
        write_real_lock(main, age_sec=5)          # lock at the MAIN checkout only
        ok("[17] a worktree finds the lock at its git-common-dir root", _live(wt))
        write_real_lock(main, age_sec=99_999)     # same lock, now stale
        ok("[17] a STALE root lock does not shield forever", not _live(wt))

    if FAILED:
        print(f"\nFAILED — dev-build-reclaim (MYC-3727): {len(FAILED)} control(s)")
        for f in FAILED:
            print(f"  x {f}")
        return 1
    print("\nOK - dev-build-reclaim: reclaims idle build output; preserves "
          "in-flight work, live-locked worktrees, source-named target/, source "
          "and .git; a build cannot mask idleness; a stale lock cannot shield "
          "forever; the ladder scales with the volume; an unmeasurable worktree "
          "is preserved and reported; symlinks unlinked without being followed.")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): the failure output carries non-ASCII.
    # A test whose FAILURE output cannot print reads as a pass.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
