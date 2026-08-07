#!/usr/bin/env python3
"""Alarm when main's plugin version is not what users actually receive.

THE CLASS THIS EXISTS FOR (MYC-3243, mycelium-studio). release.yml returned
`startup_failure` on every ref for eight days. Zero jobs ran, so not one release
gate fired. main carried the new version, the world kept the old one, and every
existing guard stayed silent -- because each of them was conditioned on a
release HAPPENING. Nothing was watching for SILENCE.

WHY THIS REPO HAS THE SAME SHAPE. ai-brain-starter is tag-triggered with a
publish step, and README (EN + ES) plus docs/RELEASE_PROCESS.md document a
stable user-facing install URL:

    claude --plugin-url .../releases/latest/download/ai-brain-starter.zip

If a version bump merges to main and the tag is never pushed -- or release.yml
rots the way studio's did -- that URL keeps serving the old plugin forever, and
nothing in CI notices. "main says X, users get Y" is exactly the class.

WHAT IT PROBES. The effective leaf, not a proxy for it: it downloads the archive
that URL actually serves and reads the version out of the plugin manifest INSIDE
it. A release can exist, be tagged correctly, and still ship a stale or broken
archive; comparing tag names would call that green.

OUTAGE IS NOT DRIFT. An unreachable endpoint exits non-zero with its own
message. Silence about a surface you could not measure is the failure mode this
whole family of guards exists to prevent, so it never degrades to "assume fine".

Run `--self-test` for the negative controls: the alarm must BITE on real drift,
and must stay quiet inside the normal bump-then-cut window.

ASCII-only output on purpose -- see scripts/check-utf8-stdout.py.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen

REPO = "mycelium-hq/ai-brain-starter"
ASSET_URL = f"https://github.com/{REPO}/releases/latest/download/ai-brain-starter.zip"
MANIFEST = ".claude-plugin/plugin.json"
# Hours main may sit ahead of the published archive before this reds. A normal
# bump-then-cut lands well inside this; two and a half months does not.
DEFAULT_GRACE_HOURS = 36

OK, WARN, RED = "ok", "warn", "red"


def decide(
    main_version: str,
    shipped_version: Optional[str],
    bump_age_hours: Optional[float],
    grace_hours: float,
) -> Tuple[str, str]:
    """Pure decision. Returns (level, message).

    shipped_version None means the published archive exists but carries no
    readable version -- treated as RED, never as "probably fine".
    """
    if not shipped_version:
        return RED, (
            "the latest release archive carries no readable plugin version.\n"
            f"main is {main_version}. The documented --plugin-url install path is "
            "serving something this check cannot identify, which is worse than drift."
        )

    if main_version == shipped_version:
        return OK, f"no drift -- users receive {shipped_version}, which is what main carries."

    # Divergence. Allow the normal bump-then-cut window before alarming.
    if bump_age_hours is not None and bump_age_hours <= grace_hours:
        return WARN, (
            f"main is {main_version}, users receive {shipped_version}, but the bump is "
            f"only {int(bump_age_hours)}h old -- inside the normal cut window "
            f"({int(grace_hours)}h). Not alarming yet."
        )

    age = f"for {int(bump_age_hours)}h" if bump_age_hours is not None else "since an unknown time"
    return RED, (
        f"SHIPPED-VERSION DRIFT -- main has been at {main_version} {age}, "
        f"but the documented install path still serves {shipped_version}.\n"
        "Whatever the cause, the release pipeline is NOT delivering.\n"
        "Nothing else alarms on this: release.yml only runs on a v* tag push, so if\n"
        "no tag is ever pushed -- or the workflow cannot start -- every other guard\n"
        "stays silent. That is the MYC-3243 failure mode.\n"
        f"Check: gh release list --repo {REPO} --limit 5"
    )


def read_main_version(root: Path) -> str:
    return json.loads((root / MANIFEST).read_text(encoding="utf-8"))["version"]


def read_shipped_version(url: str, timeout: int = 60) -> Optional[str]:
    """Download the archive users actually get and read the version inside it.

    Raises URLError/OSError on an unreachable endpoint -- the caller reports that
    as an outage, distinct from drift.
    """
    req = Request(url, headers={"User-Agent": "ai-brain-starter-drift-check"})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
        blob = resp.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            # Any layout: the archive roots at ai-brain-starter/, but do not
            # depend on that -- a packaging change must not silently no-op this.
            if name.endswith(MANIFEST) and not name.startswith("__MACOSX"):
                return json.loads(zf.read(name).decode("utf-8")).get("version") or None
    return None


def bump_age_hours(root: Path) -> Optional[float]:
    """Hours since the last commit touching the plugin manifest."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", MANIFEST],
            cwd=str(root), capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()
        if not out:
            return None
        stamp = datetime.fromisoformat(out)
        return (datetime.now(timezone.utc) - stamp).total_seconds() / 3600.0
    except Exception:
        return None


def self_test() -> int:
    """Negative controls. A guard earns trust only by failing on the thing it catches."""
    cases = [
        ("match -> quiet", ("1.5.1", "1.5.1", 999.0, 36.0), OK),
        ("real drift past grace -> RED", ("1.5.1", "1.3.0", 1800.0, 36.0), RED),
        ("drift inside the cut window -> quiet", ("1.5.1", "1.5.0", 2.0, 36.0), WARN),
        ("drift, bump age unknown -> RED", ("1.5.1", "1.3.0", None, 36.0), RED),
        ("no readable shipped version -> RED", ("1.5.1", None, 1.0, 36.0), RED),
        ("empty shipped version -> RED", ("1.5.1", "", 1.0, 36.0), RED),
        # main BEHIND the published archive is also a divergence: it means a
        # release was cut from something that is not main.
        ("shipped ahead of main -> RED", ("1.4.0", "1.5.1", 1800.0, 36.0), RED),
        ("exactly at the grace boundary -> quiet", ("1.5.1", "1.5.0", 36.0, 36.0), WARN),
        ("one hour past grace -> RED", ("1.5.1", "1.5.0", 37.0, 36.0), RED),
    ]
    failures = 0
    for name, args, expect in cases:
        level, _ = decide(*args)
        if level == expect:
            print(f"  ok   {name:44s} ({level})")
        else:
            print(f"  FAIL {name:44s} expected {expect}, got {level}")
            failures += 1
    print()
    if failures:
        print(f"check-shipped-version-drift self-test: {failures} FAILED")
        return 1
    print(f"check-shipped-version-drift self-test: {len(cases)}/{len(cases)} passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true", help="run the negative controls and exit")
    ap.add_argument("--grace-hours", type=float, default=DEFAULT_GRACE_HOURS)
    ap.add_argument("--url", default=ASSET_URL)
    ap.add_argument("--root", default=".", help="repo root holding .claude-plugin/plugin.json")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    root = Path(args.root).resolve()
    main_version = read_main_version(root)
    print(f"main version:      {main_version}")

    try:
        shipped = read_shipped_version(args.url)
    except (URLError, OSError, zipfile.BadZipFile) as exc:
        print(f"::error::cannot fetch or read {args.url} ({exc.__class__.__name__}: {exc})")
        print("::error::the documented --plugin-url install path is unreachable or corrupt.")
        print("::error::that is an OUTAGE, not drift -- users cannot install at all right now.")
        return 1
    print(f"shipped archive:   {shipped}")

    age = bump_age_hours(root)
    if age is not None:
        print(f"manifest last touched {int(age)}h ago (grace {int(args.grace_hours)}h)")

    level, message = decide(main_version, shipped, age, args.grace_hours)
    if level == OK:
        print(message)
        return 0
    if level == WARN:
        print(f"::warning::{message}")
        return 0
    for line in message.splitlines():
        print(f"::error::{line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
