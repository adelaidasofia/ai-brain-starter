#!/usr/bin/env python3
"""Vendor the pinned open-source High-Rise framework into ai-brain-starter.

ai-brain-starter CONSUMES the High-Rise framework - the canonical 34-floor
model (floors, tiers, elevators, shadow twins) plus the journaling and coaching
methodologies - as a downstream dependency of its single upstream source of
truth: Fundacion-Lontananza/high-rise (public, MIT). It does NOT hold a
divergent copy. The framework files live under vendor/high-rise/, pinned to a
tagged release, and are refreshed by THIS script from that upstream.

Why vendored, not a git submodule
---------------------------------
The substrate installs by `git clone`ing THIS repo into
~/.claude/skills/ai-brain-starter (see bootstrap.sh). A submodule would force
every user onto `git clone --recursive` - and silently break the many installs
that don't pass it - and would reach the network at install time. Vendored
files ship INSIDE the clone: the framework is present offline, at a pinned
version, with zero install-time network. That is the install-safe mechanism.

Pinned by TAG, never HEAD
-------------------------
The pin - upstream repo, tag, the resolved commit that tag pointed at, and a
sha256 for every vendored file - lives in vendor/high-rise/PIN.json. A refresh
re-fetches the SAME pinned tag and must reproduce those hashes. Moving to a new
upstream release is a deliberate, reviewable act: `--tag vX.Y.Z`.

Usage
-----
  sync-high-rise.py               Refresh vendor/high-rise/ from the CURRENT
                                  pinned tag (PIN.json). Re-fetches the tag and
                                  rewrites the vendored files + hashes.
  sync-high-rise.py --tag v0.2.0  Re-pin to a new upstream tag, then refresh.
  sync-high-rise.py --check       Offline drift guard (CI): verify every
                                  vendored file exists and its sha256 matches
                                  PIN.json. No network. Exit 2 on any drift.
  sync-high-rise.py --dry-run     Show what a refresh would change; write nothing.

Exit codes: 0 success / clean; 2 drift or error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Canonical upstream + the exact set of files ai-brain-starter vendors from it.
# high-rise ships exactly these; if it grows a file we want, add it here.
UPSTREAM_REPO = "https://github.com/Fundacion-Lontananza/high-rise.git"
UPSTREAM_SLUG = "Fundacion-Lontananza/high-rise"
VENDORED_FILES = (
    "floors.md",
    "methodology/journaling.md",
    "methodology/coaching.md",
)

ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = ROOT / "vendor" / "high-rise"
PIN_FILE = VENDOR_DIR / "PIN.json"


def _normalize(data: bytes) -> bytes:
    """CRLF/CR -> LF."""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _content_sha256(data: bytes) -> str:
    """Hash the vendored CONTENT, not the checkout.

    This repo has no per-file eol pin its clones are guaranteed to honor, so a
    Windows clone with core.autocrlf=true has CRLF on disk while PIN.json was
    recorded from LF.  Hashing raw bytes then reports every vendored file as
    "edited locally" when nothing was touched.  Worse, the advertised remedy is
    to re-sync -- which would write CRLF hashes into PIN.json and invert the
    breakage onto every other platform.  Same defect class as the cloud-safe
    walker ratchet (#411); the pin must describe content or it describes the
    machine that last ran the sync.
    """
    return hashlib.sha256(_normalize(data)).hexdigest()


def _load_pin() -> dict:
    if not PIN_FILE.exists():
        die(
            f"no pin at {PIN_FILE.relative_to(ROOT)} - run with --tag vX.Y.Z to "
            f"create the initial pin."
        )
    try:
        return json.loads(PIN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        die(f"cannot read pin {PIN_FILE.relative_to(ROOT)}: {exc}")
    return {}  # unreachable; keeps type-checkers happy


def die(msg: str) -> None:
    print(f"sync-high-rise: ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
def check() -> int:
    """Offline drift guard: vendored files must match PIN.json sha256s.

    Catches a hand-edit to a vendored file (the framework is upstream's to
    change, never ours to patch locally) and a vendored file that went missing.
    Does not touch the network - that is what CI runs on every PR.
    """
    pin = _load_pin()
    recorded = pin.get("files", {})
    if not recorded:
        die("pin has no 'files' map - re-create it with --tag vX.Y.Z.")

    problems = []
    for rel in VENDORED_FILES:
        want = recorded.get(rel)
        if want is None:
            problems.append(f"{rel}: not recorded in PIN.json")
            continue
        dest = VENDOR_DIR / rel
        if not dest.exists():
            problems.append(f"{rel}: vendored file missing at {dest.relative_to(ROOT)}")
            continue
        got = _content_sha256(dest.read_bytes())
        if got != want:
            problems.append(
                f"{rel}: sha256 drift - vendored file was edited locally "
                f"(expected {want[:12]}..., found {got[:12]}...). "
                f"The framework is upstream's; edit {UPSTREAM_SLUG} and re-sync, "
                f"do not patch vendor/ by hand."
            )
    # A file recorded in the pin but no longer in our manifest is also drift.
    for rel in recorded:
        if rel not in VENDORED_FILES:
            problems.append(f"{rel}: recorded in PIN.json but not in VENDORED_FILES manifest")

    if problems:
        print(
            f"sync-high-rise: DRIFT against pin {pin.get('tag', '?')} "
            f"({pin.get('commit', '?')[:12]}):",
            file=sys.stderr,
        )
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2

    print(
        f"sync-high-rise: OK - {len(VENDORED_FILES)} vendored file(s) match pin "
        f"{pin.get('tag', '?')} ({pin.get('commit', '?')[:12]})."
    )
    return 0


# ---------------------------------------------------------------------------
def _clone_tag(tag: str, workdir: Path) -> str:
    """Shallow-clone exactly `tag` into workdir; return the resolved commit sha."""
    dest = workdir / "high-rise"
    try:
        subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", "--branch", tag,
             UPSTREAM_REPO, str(dest)],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        die("git is not installed / not on PATH.")
    except subprocess.CalledProcessError as exc:
        die(
            f"could not clone {UPSTREAM_SLUG} at tag '{tag}'. Does the tag exist?\n"
            f"  {exc.stderr.strip()}"
        )
    sha = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return sha


def refresh(tag: str, *, dry_run: bool) -> int:
    """Re-fetch `tag` from upstream and rewrite vendor/high-rise/ + PIN.json."""
    with tempfile.TemporaryDirectory(prefix="sync-high-rise-") as tmp:
        src = Path(tmp) / "high-rise"
        commit = _clone_tag(tag, Path(tmp))

        # Verify upstream still ships everything we vendor BEFORE writing anything.
        missing = [rel for rel in VENDORED_FILES if not (src / rel).exists()]
        if missing:
            die(
                f"{UPSTREAM_SLUG}@{tag} is missing vendored file(s): "
                f"{', '.join(missing)}. Update VENDORED_FILES or the tag."
            )

        new_hashes: dict[str, str] = {}
        changes = []
        for rel in VENDORED_FILES:
            data = (src / rel).read_bytes()
            new_hashes[rel] = _content_sha256(data)
            dest = VENDOR_DIR / rel
            old = dest.read_bytes() if dest.exists() else None
            # Compare content, for the same reason the pin hashes content: on a
            # CRLF checkout a raw compare reports every file as changed even
            # when upstream shipped nothing new.
            if old is None or _normalize(old) != _normalize(data):
                changes.append(("update" if old is not None else "add", rel))
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)

        pin = {
            "_comment": (
                "Pinned vendor of the open-source High-Rise framework. "
                "Do not edit vendored files by hand - edit upstream "
                f"({UPSTREAM_SLUG}) and run scripts/sync-high-rise.py. "
                "Re-pin with --tag vX.Y.Z."
            ),
            "repo": UPSTREAM_SLUG,
            "tag": tag,
            "commit": commit,
            "files": new_hashes,
        }
        pin_json = json.dumps(pin, indent=2, ensure_ascii=False) + "\n"
        pin_changed = (not PIN_FILE.exists()) or PIN_FILE.read_text(encoding="utf-8") != pin_json
        if not dry_run:
            VENDOR_DIR.mkdir(parents=True, exist_ok=True)
            PIN_FILE.write_text(pin_json, encoding="utf-8")

        verb = "Would refresh" if dry_run else "Refreshed"
        print(f"{verb} vendor/high-rise/ from {UPSTREAM_SLUG}@{tag} ({commit[:12]})")
        if changes:
            for kind, rel in changes:
                print(f"  {kind}: vendor/high-rise/{rel}")
        else:
            print("  (vendored files already up to date)")
        if pin_changed:
            print("  update: vendor/high-rise/PIN.json")
        if dry_run and (changes or pin_changed):
            print("  (dry-run: nothing written)")
    return 0


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Vendor the pinned High-Rise framework into ai-brain-starter.",
    )
    ap.add_argument("--tag", metavar="vX.Y.Z",
                    help="Re-pin to this upstream tag, then refresh. "
                         "Omit to refresh from the current pinned tag.")
    ap.add_argument("--check", action="store_true",
                    help="Offline drift guard: vendored files must match PIN.json.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what a refresh would change; write nothing.")
    args = ap.parse_args()

    if args.check:
        if args.tag or args.dry_run:
            die("--check takes no other options.")
        return check()

    tag = args.tag or _load_pin().get("tag")
    if not tag:
        die("pin has no tag - pass --tag vX.Y.Z to create the initial pin.")
    return refresh(tag, dry_run=args.dry_run)


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print
    # (accented floor name in a path) can't crash the CLI.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
