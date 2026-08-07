#!/usr/bin/env python3
"""
check-sync-folder-machinery.py

Detect the failure class where high-churn / high-count directories (git repos,
node_modules, build output, or any dir with thousands of files) live INSIDE a
cloud File-Provider-synced folder — iCloud "Desktop & Documents", iCloud Drive,
OneDrive, Dropbox, Box, or Google Drive.

WHY: consumer cloud File Providers (iCloud `bird`/`fileproviderd`, Google
`DriveFS`, OneDrive, Dropbox) sync documents well but choke catastrophically on
machinery. Git rewrites `.git/index.lock` and pack objects on every operation,
so the daemon can never converge -> a retry storm (fileproviderd pinned at
70-130% CPU for hours) + silent repo/vault corruption. The phantom-error backlog
can grow into the hundreds of thousands and survives even after you move the
churn out, until the sync DB is rebuilt.

This is a BROADER check than the per-session vault-location signal
(`worktree-footprint-signal.py`, which asks "is THE VAULT in a cloud folder?").
This one asks "is there ANY machinery in ANY synced root?" — so it also catches
side repos, build trees, and caches that aren't the vault but still melt the
daemon. It does a bounded filesystem walk, so it runs as a standalone audit /
periodic (weekly) check, NOT as a per-session hook.

RULE: machinery lives at a local home-root path (e.g. ~/dev, ~/code, ~/<vault>),
backed up by git remotes + a real, restored-and-verified backup. Cloud sync is
for documents only. Sync is not a backup.

Advisory ONLY — always exits 0. Never blocks. Runnable standalone as a
Mac-health audit.

Usage:
  check-sync-folder-machinery.py             # scan, human report
  check-sync-folder-machinery.py --json      # machine-readable findings
  check-sync-folder-machinery.py --self-test # negative control (proves it fires)
"""
import json
import os
import sqlite3
import subprocess
import sys
import time

# Single source for the DriveFS "Mirror" root read (MYC-1130) so this audit and
# _lib.worktree_safety.detect_cloud_sync can never drift on the Mirror signal.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.worktree_safety import drive_mirror_root_paths  # noqa: E402

HOME = os.path.expanduser("~")

# Directory names that mean "machinery, never sync this"
MARKER_DIRS = {".git", "node_modules", ".venv", "venv", "target",
               "__pycache__", ".next", "dist", "build", ".tox", ".gradle"}
# A plain folder this big is also too much for a File Provider to live-sync
FILE_COUNT_THRESHOLD = 5000
# Per-root safety budget so a streamed Google Drive can't hang the scan
ROOT_TIME_BUDGET_S = 6.0
MAX_DEPTH = 7
# Google Drive for Desktop "Mirror" roots stay at an arbitrary NATIVE path
# (e.g. ~/dev/<vault>) and never appear under ~/Library/CloudStorage, so a path
# walk structurally cannot see them. The authoritative signal is the DriveFS
# roots DB, where sync_type == 1 means Mirror (sync_type == 2 means Stream).
DRIVEFS_DB = os.path.join(
    HOME, "Library/Application Support/Google/DriveFS/root_preference_sqlite.db"
)
DRIVE_MIRROR_SYNC_TYPE = 1


def _defaults_bool(domain, key):
    try:
        out = subprocess.run(["defaults", "read", domain, key],
                             capture_output=True, text=True, timeout=5)
        return out.returncode == 0 and out.stdout.strip() == "1"
    except Exception:
        return False


def drive_mirror_roots(db_path=DRIVEFS_DB):
    """Native-path Google Drive 'Mirror' roots tagged for this scanner.

    Thin wrapper over the shared `_lib.worktree_safety.drive_mirror_root_paths`
    (the single source of the DriveFS sync_type=1 read; MYC-1130) so this audit
    and detect_cloud_sync can never drift on the Mirror-root signal.
    """
    return [(p, "GoogleDrive-Mirror") for p in drive_mirror_root_paths(db_path)]


def synced_roots():
    """Return [(path, provider)] of folders currently under cloud File Provider sync."""
    roots = []
    if _defaults_bool("com.apple.finder", "FXICloudDriveDesktop"):
        roots.append((os.path.join(HOME, "Desktop"), "iCloud Desktop&Documents"))
    if _defaults_bool("com.apple.finder", "FXICloudDriveDocuments"):
        roots.append((os.path.join(HOME, "Documents"), "iCloud Desktop&Documents"))
    icd = os.path.join(HOME, "Library/Mobile Documents/com~apple~CloudDocs")
    if os.path.isdir(icd):
        roots.append((icd, "iCloud Drive"))
    cs = os.path.join(HOME, "Library/CloudStorage")
    if os.path.isdir(cs):
        for entry in sorted(os.listdir(cs)):
            if entry.startswith("GoogleDrive-") or entry.startswith("Dropbox") \
               or entry.startswith("OneDrive") or entry.startswith("Box"):
                roots.append((os.path.join(cs, entry), entry.split("-")[0]))
    # Native-path Google Drive Mirror roots (sync_type=1) are invisible to the
    # CloudStorage walk above; the DriveFS DB is the only signal (MYC-705).
    roots.extend(drive_mirror_roots())
    # Keep only real dirs, de-duped by realpath (a Mirror root can coincide with
    # an iCloud D&D / CloudStorage root; scanning twice would double-report).
    seen, out = set(), []
    for p, prov in roots:
        if not os.path.isdir(p):
            continue
        rp = os.path.realpath(p)
        if rp in seen:
            continue
        seen.add(rp)
        out.append((p, prov))
    return out


def scan_root(root, provider):
    """Bounded walk: flag machinery markers + oversized plain dirs. Prunes into markers."""
    findings = []
    start = time.monotonic()
    base_depth = root.rstrip("/").count("/")
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        if time.monotonic() - start > ROOT_TIME_BUDGET_S:
            findings.append({"path": root, "provider": provider,
                             "reason": "scan-time-budget-exceeded (large/streamed tree)",
                             "severity": "info"})
            break
        if dirpath.count("/") - base_depth >= MAX_DEPTH:
            dirnames[:] = []
            continue
        hit = MARKER_DIRS.intersection(dirnames)
        for h in sorted(hit):
            findings.append({"path": os.path.join(dirpath, h), "provider": provider,
                             "reason": f"machinery dir '{h}' inside synced folder",
                             "severity": "high"})
        # don't descend into machinery we already flagged
        dirnames[:] = [d for d in dirnames if d not in MARKER_DIRS]
        if len(filenames) >= FILE_COUNT_THRESHOLD:
            findings.append({"path": dirpath, "provider": provider,
                             "reason": f"{len(filenames)}+ files in one synced dir",
                             "severity": "high"})
    return findings


def run_scan():
    findings = []
    for root, provider in synced_roots():
        findings.extend(scan_root(root, provider))
    return findings


def self_test():
    """Negative control: build a synced-like tree with a .git, prove we flag it."""
    import tempfile
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "myproject", ".git")
        os.makedirs(repo)
        big = os.path.join(tmp, "hugedir")
        os.makedirs(big)
        for i in range(FILE_COUNT_THRESHOLD + 5):
            open(os.path.join(big, f"f{i}"), "w").close()
        f = scan_root(tmp, "TEST")
        got_marker = any(x["reason"].startswith("machinery dir '.git'") for x in f)
        got_big = any("files in one synced dir" in x["reason"] for x in f)
        # control: a clean docs tree must produce NOTHING
        clean = os.path.join(tmp, "clean_docs")
        os.makedirs(clean)
        open(os.path.join(clean, "notes.md"), "w").close()
        f2 = scan_root(clean, "TEST")
        print(f"[self-test] detects .git inside synced folder: {got_marker}")
        print(f"[self-test] detects oversized synced dir:      {got_big}")
        print(f"[self-test] clean docs tree -> no findings:    {len(f2) == 0}")
        ok = got_marker and got_big and len(f2) == 0

        # Metadata-only negative control: a FIFO has the same forever-blocking
        # behavior as a placeholder if opened for content. This scanner must
        # finish without opening it, and the content-walker guard must therefore
        # keep classifying this file as metadata-only rather than demand fake
        # safe_read wiring.
        if hasattr(os, "mkfifo"):
            fifo_docs = os.path.join(tmp, "fifo_docs")
            os.makedirs(fifo_docs)
            try:
                os.mkfifo(os.path.join(fifo_docs, "offline-note.md"))
                started = time.monotonic()
                fifo_findings = scan_root(fifo_docs, "TEST")
                fifo_safe = not fifo_findings and time.monotonic() - started < 1.0
            except OSError:
                fifo_safe = True  # platform exposes mkfifo but filesystem rejects it
            print(f"[self-test] FIFO metadata is never content-read: {fifo_safe}")
            ok = ok and fifo_safe
        else:
            print("[self-test] FIFO metadata control: unavailable on this platform")

        # --- MYC-705: Drive Mirror-root (sync_type=1) DB detection ---
        mdb = os.path.join(tmp, "root_preference_sqlite.db")
        con = sqlite3.connect(mdb)
        # exact real DriveFS schema so the query is proven against the true shape
        con.execute(
            "CREATE TABLE roots (root_id INTEGER PRIMARY KEY, metadata BLOB, "
            "media_id TEXT NOT NULL, title TEXT NOT NULL, root_path TEXT NOT NULL, "
            "account_token TEXT NOT NULL, sync_type INTEGER NOT NULL, "
            "destination INTEGER NOT NULL, medium INTEGER NOT NULL, state INTEGER NOT NULL, "
            "one_shot BOOL NOT NULL, is_my_drive BOOL NOT NULL, doc_id TEXT NOT NULL, "
            "last_seen_absolute_path TEXT NOT NULL)"
        )
        mirror_git = os.path.join(tmp, "mirror_vault")      # Mirror + .git -> flag
        os.makedirs(os.path.join(mirror_git, ".git"))
        mirror_clean = os.path.join(tmp, "mirror_clean")    # Mirror, clean -> no finding
        os.makedirs(mirror_clean)
        open(os.path.join(mirror_clean, "notes.md"), "w").close()
        stream_git = os.path.join(tmp, "stream_vault")      # sync_type=2 -> IGNORED
        os.makedirs(os.path.join(stream_git, ".git"))

        def _ins(path, sync_type):
            con.execute(
                "INSERT INTO roots (media_id,title,root_path,account_token,sync_type,"
                "destination,medium,state,one_shot,is_my_drive,doc_id,last_seen_absolute_path)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("m", "t", path, "a", sync_type, 0, 0, 0, 0, 1, "d", path),
            )
        _ins(mirror_git, DRIVE_MIRROR_SYNC_TYPE)
        _ins(mirror_clean, DRIVE_MIRROR_SYNC_TYPE)
        _ins(stream_git, 2)
        con.commit()
        con.close()

        mpaths = {os.path.realpath(p) for p, _ in drive_mirror_roots(db_path=mdb)}
        got_mirror = os.path.realpath(mirror_git) in mpaths
        ignored_stream = os.path.realpath(stream_git) not in mpaths
        mirror_flagged = any(
            x["reason"].startswith("machinery dir '.git'")
            for x in scan_root(mirror_git, "GoogleDrive-Mirror")
        )
        clean_mirror_ok = len(scan_root(mirror_clean, "GoogleDrive-Mirror")) == 0
        failopen = drive_mirror_roots(db_path=os.path.join(tmp, "nope.db")) == []

        print(f"[self-test] DB: detects sync_type=1 Mirror root:  {got_mirror}")
        print(f"[self-test] DB: ignores sync_type!=1 (stream):    {ignored_stream}")
        print(f"[self-test] DB: .git in Mirror root flagged:      {mirror_flagged}")
        print(f"[self-test] DB: clean Mirror tree -> no findings: {clean_mirror_ok}")
        print(f"[self-test] DB: missing DB -> fail-open []:       {failopen}")
        ok = ok and got_mirror and ignored_stream and mirror_flagged \
            and clean_mirror_ok and failopen
    print("[self-test] PASS" if ok else "[self-test] FAIL")
    return 0 if ok else 1


def main():
    if "--self-test" in sys.argv:
        return self_test()
    findings = run_scan()
    high = [f for f in findings if f["severity"] == "high"]
    if "--json" in sys.argv:
        print(json.dumps({"findings": findings, "high_count": len(high)}, indent=2))
        return 0
    if not high:
        print("[sync-guard] clean — no machinery found in any cloud-synced folder.")
        for f in findings:
            print(f"[sync-guard] note: {f['reason']} @ {f['path']}")
        return 0
    print("[sync-guard] ⚠️  MACHINERY FOUND IN CLOUD-SYNCED FOLDERS")
    print("[sync-guard] This is the fileproviderd-storm + corruption risk class.")
    for f in high:
        print(f"[sync-guard]   • [{f['provider']}] {f['reason']}")
        print(f"[sync-guard]     {f['path']}")
    print("[sync-guard] FIX: move it to a local home-root path (e.g. ~/dev, ~/code),")
    print("[sync-guard]      back up via git remote + a verified backup. Cloud sync is")
    print("[sync-guard]      for documents only — and sync is not a backup.")
    return 0  # advisory: never block


if __name__ == "__main__":
    sys.exit(main())
