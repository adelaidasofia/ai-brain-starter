#!/usr/bin/env python3
"""Integration + negative-control suite for the vault-relocation WATCHDOG (mode 2).

Exercises the whole delivery chain end to end, hermetically (temp dirs, fictional
paths, no network, no real ~/dev, no real ~/.claude):

  - relocate-vault.sh records the move in the manifest; relocate-sweep.py --watch reads it
  - ALARM fires on: a recreated old directory, an executed residual reference, a missing
    relocated vault root (fail-loud), and an old-path ref in a `.env` file (SCAN_EXTS parity)
  - a clean tree stays green (no false alarm)
  - hooks/relocate-watch-surface.py surfaces a systemMessage on drift, stays quiet otherwise,
    and is WALK-FREE (passes audit-sessionstart-boundedness.py --check)
  - the engine's own --watch-selftest controls pass

A guard earns trust only by failing on the thing it catches — every ALARM case here is
a planted defect the watch MUST flag. Run: python3 scripts/test-relocate-watch.py
(also wired into scripts/ci.sh as the named integration test `test_relocate_watch`).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SWEEP = ROOT / "scripts" / "relocate-sweep.py"
VAULT_SH = ROOT / "scripts" / "relocate-vault.sh"
SURFACE = ROOT / "hooks" / "relocate-watch-surface.py"
AUDIT = ROOT / "scripts" / "audit-sessionstart-boundedness.py"

PASS = 0
FAIL = 0


def ok(msg):
    global PASS
    PASS += 1
    print(f"PASS  {msg}")


def bad(msg, detail=""):
    global FAIL
    FAIL += 1
    print(f"FAIL  {msg} :: {detail}")


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _watch(cfg, *extra):
    return _run([sys.executable, str(SWEEP), "--watch", "--config-dir", cfg, *extra])


def _write_manifest(cfg, old, new, symlink=False):
    Path(cfg, "relocations.json").write_text(
        json.dumps([{"old": old, "new": new, "symlink": symlink, "at": "test"}])
    )


def _cache(cfg):
    try:
        return json.loads(Path(cfg, "relocate-watch-state.json").read_text())
    except (OSError, ValueError):
        return None


def _surfacer(cfg):
    env = dict(os.environ, CLAUDE_CONFIG_DIR=cfg, RELOCATE_WATCH_SURFACE_NO_REFRESH="1")
    return _run([sys.executable, str(SURFACE)], input="{}", env=env)


def _tmp():
    return tempfile.mkdtemp(prefix="test-relocate-watch-")


# --------------------------------------------------------------------------- #
# 1. engine in-script controls
# --------------------------------------------------------------------------- #
def case_engine_selftest():
    r = _run([sys.executable, str(SWEEP), "--watch-selftest"])
    if r.returncode == 0 and "WATCH SELF-TEST OK" in r.stdout:
        ok("engine --watch-selftest controls pass")
    else:
        bad("engine --watch-selftest", r.stdout + r.stderr)


# --------------------------------------------------------------------------- #
# 2. no manifest → clean no-op
# --------------------------------------------------------------------------- #
def case_no_manifest():
    t = _tmp()
    try:
        cfg = os.path.join(t, "cfg")
        os.makedirs(cfg)
        r = _watch(cfg, "--no-auto-discover")
        if r.returncode == 0 and "nothing to watch" in r.stdout:
            ok("no manifest → clean no-op (exit 0)")
        else:
            bad("no manifest no-op", f"rc={r.returncode} {r.stdout}")
    finally:
        shutil.rmtree(t, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 3. relocate-vault.sh writes the manifest; watch reads it CLEAN
# --------------------------------------------------------------------------- #
def case_relocate_roundtrip():
    t = _tmp()
    try:
        cfg = os.path.join(t, "cfg")
        old = os.path.join(t, "Old Vault")
        new = os.path.join(t, "NewVault")
        os.makedirs(os.path.join(old, "sub"))
        Path(old, "sub", "a.md").write_text("x")
        r = _run(["bash", str(VAULT_SH), old, new, "--force", "--config-dir", cfg])
        manifest = os.path.join(cfg, "relocations.json")
        if r.returncode != 0 or not os.path.isfile(manifest):
            bad("relocate-vault writes manifest", f"rc={r.returncode} {r.stdout} {r.stderr}")
            return
        data = json.loads(Path(manifest).read_text())
        if not (data and data[0]["new"] == os.path.abspath(new) and data[0]["symlink"] is True):
            bad("manifest content", json.dumps(data))
            return
        ok("relocate-vault.sh records the move in relocations.json")
        # old is now the symlink relocate-vault left; new exists → CLEAN
        w = _watch(cfg, "--no-auto-discover")
        if w.returncode == 0 and "VERDICT: OK" in w.stdout:
            ok("watch reads manifest → CLEAN after a real relocation (old=symlink, new exists)")
        else:
            bad("watch clean after relocate", f"rc={w.returncode} {w.stdout}")
    finally:
        shutil.rmtree(t, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 4. NEG: a recreated old directory → ALARM
# --------------------------------------------------------------------------- #
def case_recreated_alarms():
    t = _tmp()
    try:
        cfg = os.path.join(t, "cfg")
        old = os.path.join(t, "old-vault")
        new = os.path.join(t, "new-vault")
        os.makedirs(cfg)
        os.makedirs(new)
        os.makedirs(old)  # a REAL directory at the old path = a recreator ran
        _write_manifest(cfg, old, new)
        w = _watch(cfg, "--no-auto-discover")
        c = _cache(cfg)
        if w.returncode == 1 and c and c["verdict"] == "ALARM" and c["recreated"]:
            ok("NEG control: recreated old dir → ALARM (exit 1) + cache records it")
        else:
            bad("recreated alarm", f"rc={w.returncode} cache={c}")
    finally:
        shutil.rmtree(t, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 5. NEG: an executed residual reference under a scan root → ALARM
# --------------------------------------------------------------------------- #
def case_executed_residual_alarms():
    t = _tmp()
    try:
        cfg = os.path.join(t, "cfg")
        scan = os.path.join(t, "scan")
        old = os.path.join(t, "old-vault")
        new = os.path.join(t, "new-vault")
        os.makedirs(cfg)
        os.makedirs(scan)
        os.makedirs(new)
        # old absent (symlink dropped); a shell command still hardcodes it = recreator-in-waiting
        Path(scan, "recreator.sh").write_text('#!/bin/bash\nmkdir -p "%s/sub"\n' % old)
        _write_manifest(cfg, old, new)
        w = _watch(cfg, "--no-auto-discover", "--root", scan)
        c = _cache(cfg)
        if w.returncode == 1 and c and c["verdict"] == "ALARM" and c["executed"] >= 1:
            ok("NEG control: executed residual ref → ALARM (exit 1)")
        else:
            bad("executed residual alarm", f"rc={w.returncode} cache={c}")
    finally:
        shutil.rmtree(t, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 6. .env / .envrc SCAN_EXTS parity — an old-path ref in a config-env file
# --------------------------------------------------------------------------- #
def case_env_parity_alarms():
    t = _tmp()
    try:
        cfg = os.path.join(t, "cfg")
        scan = os.path.join(t, "scan")
        old = os.path.join(t, "old-vault")
        new = os.path.join(t, "new-vault")
        os.makedirs(cfg)
        os.makedirs(scan)
        os.makedirs(new)
        Path(scan, "app.env").write_text('VAULT_ROOT="%s"\n' % old)   # .env in scope now
        Path(scan, ".envrc").write_text('export VAULT="%s"\n' % old)  # .envrc too
        _write_manifest(cfg, old, new)
        w = _watch(cfg, "--no-auto-discover", "--root", scan)
        c = _cache(cfg)
        if w.returncode == 1 and c and c["executed"] >= 1:
            ok("file-type parity: old-path ref in a .env/.envrc → ALARM")
        else:
            bad(".env parity alarm", f"rc={w.returncode} cache={c}")
    finally:
        shutil.rmtree(t, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 7. fail-loud: a missing relocated vault root → ALARM
# --------------------------------------------------------------------------- #
def case_missing_root_failloud():
    t = _tmp()
    try:
        cfg = os.path.join(t, "cfg")
        old = os.path.join(t, "old-vault")
        new = os.path.join(t, "gone-vault")  # never created
        os.makedirs(cfg)
        _write_manifest(cfg, old, new)
        w = _watch(cfg, "--no-auto-discover")
        c = _cache(cfg)
        if w.returncode == 1 and c and c["missing_roots"]:
            ok("fail-loud: missing relocated vault root → ALARM")
        else:
            bad("missing-root fail-loud", f"rc={w.returncode} cache={c}")
    finally:
        shutil.rmtree(t, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 8. SessionStart surfacer behaviour
# --------------------------------------------------------------------------- #
def case_surfacer():
    # 8a. no manifest → quiet
    t = _tmp()
    try:
        cfg = os.path.join(t, "cfg")
        os.makedirs(cfg)
        r = _surfacer(cfg)
        if '"suppressOutput": true' in r.stdout and "systemMessage" not in r.stdout:
            ok("surfacer: no manifest → quiet")
        else:
            bad("surfacer quiet (no manifest)", r.stdout)
    finally:
        shutil.rmtree(t, ignore_errors=True)

    # 8b. recreated old dir → systemMessage
    t = _tmp()
    try:
        cfg = os.path.join(t, "cfg")
        old = os.path.join(t, "old-vault")
        new = os.path.join(t, "new-vault")
        os.makedirs(cfg)
        os.makedirs(new)
        os.makedirs(old)
        _write_manifest(cfg, old, new)
        r = _surfacer(cfg)
        if "systemMessage" in r.stdout and "RECREATED" in r.stdout:
            ok("surfacer: recreated old dir → systemMessage")
        else:
            bad("surfacer systemMessage (recreated)", r.stdout)
    finally:
        shutil.rmtree(t, ignore_errors=True)

    # 8c. clean (old absent) + fresh OK cache → quiet
    t = _tmp()
    try:
        cfg = os.path.join(t, "cfg")
        old = os.path.join(t, "old-vault")  # never created
        new = os.path.join(t, "new-vault")
        os.makedirs(cfg)
        os.makedirs(new)
        _write_manifest(cfg, old, new)
        Path(cfg, "relocate-watch-state.json").write_text(
            json.dumps({"ts": 9999999999, "verdict": "OK", "executed": 0,
                        "findings": [], "recreated": [], "missing_roots": []})
        )
        r = _surfacer(cfg)
        if '"suppressOutput": true' in r.stdout and "systemMessage" not in r.stdout:
            ok("surfacer: clean tree + OK cache → quiet")
        else:
            bad("surfacer quiet (clean)", r.stdout)
    finally:
        shutil.rmtree(t, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 9. the surfacer is WALK-FREE (bounded for SessionStart)
# --------------------------------------------------------------------------- #
def case_surfacer_bounded():
    if not AUDIT.is_file():
        ok("boundedness audit absent — skipped (CI enforces)")
        return
    r = _run([sys.executable, str(AUDIT), "--check", str(SURFACE)])
    if r.returncode == 0:
        ok("surfacer passes audit-sessionstart-boundedness --check (walk-free)")
    else:
        bad("surfacer boundedness", r.stdout + r.stderr)


def case_single_instance_lock():
    # The surfacer spawns --watch detached; several sessions starting at once must NOT
    # stack N concurrent corpus walks (the MYC-570 freeze class). A held lock → skip.
    try:
        import fcntl
    except ImportError:
        ok("single-instance lock — skipped (no fcntl on this platform)")
        return
    t = _tmp()
    try:
        cfg = os.path.join(t, "cfg")
        old = os.path.join(t, "old-vault")
        new = os.path.join(t, "new-vault")
        os.makedirs(cfg)
        os.makedirs(new)
        _write_manifest(cfg, old, new)
        lock = open(os.path.join(cfg, ".relocate-watch.lock"), "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # hold it
        r = _watch(cfg, "--no-auto-discover")
        if r.returncode == 0 and "skipping" in r.stdout:
            ok("single-instance lock: a concurrent --watch skips (no stacked corpus walk)")
        else:
            bad("single-instance lock skip", f"rc={r.returncode} {r.stdout}")
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        r2 = _watch(cfg, "--no-auto-discover")  # released → next run proceeds
        if r2.returncode == 0 and "recorded moves watched" in r2.stdout and "skipping" not in r2.stdout:
            ok("single-instance lock: released → the next --watch runs")
        else:
            bad("single-instance lock release", f"rc={r2.returncode} {r2.stdout}")
    finally:
        shutil.rmtree(t, ignore_errors=True)


def main():
    case_engine_selftest()
    case_single_instance_lock()
    case_no_manifest()
    case_relocate_roundtrip()
    case_recreated_alarms()
    case_executed_residual_alarms()
    case_env_parity_alarms()
    case_missing_root_failloud()
    case_surfacer()
    case_surfacer_bounded()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
