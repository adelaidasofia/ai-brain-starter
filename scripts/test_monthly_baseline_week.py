#!/usr/bin/env python3
"""monthly-baseline.py: weeks, a localized journal folder, and honest gym math.

Four contracts, each of which was a live defect before this test existed:

1. `--week` accepts ANY date in the target week and snaps back to Monday, so
   the caller never has to compute the Monday itself.
2. The journal folder is auto-detected, including an emoji-prefixed, localized,
   PLURAL name ("📓 Diarios"). The hardcoded English "📓 Journals" made every
   localized vault report zero entries -- the same defect #341 fixed for
   build-journal-index.py and never fixed here.
3. A habit field describes the day it is written in, so a MORNING `gym: false`
   is "not yet", not "did not happen". Counting those as hard negatives made a
   real 4-entry week report 0% gym and a -100% ANOMALY when the truth was 1 of
   4. `gym_confirmed: true` opts an entry out of that demotion.
4. A rate built from n days moves in steps of 100/n points. When one step is
   wider than the stable band, NO attainable value can read "stable" and the
   metric cries anomaly every single period. That is resolution, not signal,
   and it must say so.

Also proves the floor scale is read from the vault's own floor notes (the
folder `generate_floor_stubs.py` writes) rather than hardcoded here.

Auto-discovered by scripts/ci.sh via the scripts/test_*.py glob.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "scripts" / "monthly-baseline.py"

FLOOR_NOTES = {
    "Reason.md": "---\nfloor_number: 24\nfloor_level: Middle\naliases: [razon, razón]\n---\n# Reason\n",
    "Enthusiasm.md": "---\nfloor_number: 31\nfloor_level: High\n---\n# Enthusiasm\n",
}


def entry(date, hour, floor="Reason", **fields):
    lines = ["---", "type: journal", "creationDate: {}T{}".format(date, hour),
             "floor: {}".format(floor)]
    for k, v in fields.items():
        lines.append("{}: {}".format(k, v))
    lines += ["---", "", "## Journal", "",
              "a plain sentence in the journaler's own voice.", ""]
    return "\n".join(lines)


# Target week: Mon 2026-04-06 .. Sun 2026-04-12.
TARGET = {
    # morning false -> demoted to no-data (contract 3)
    "mon.md": entry("2026-04-06", "08:05", gym="false", gym_week=1),
    # evening false -> a real negative
    "tue.md": entry("2026-04-07", "20:00", gym="false", gym_week=1),
    # morning false, but verified after the fact -> stays a real negative
    "wed.md": entry("2026-04-08", "08:00", gym="false", gym_confirmed="true"),
    "thu.md": entry("2026-04-09", "21:00", gym="true", gym_week=2),
    # no gym field at all, and a frontmatter contradiction for the floor check
    "fri.md": entry("2026-04-10", "19:00", floor="Reason", floor_num=99),
}

# Baseline: 6 evening entries before the target week, gym 3 true / 3 false
# -> baseline gym_rate_pct = 50.0, so the stable band is 5.0pp wide.
BASE = {
    "b1.md": entry("2026-03-23", "21:00", gym="true"),
    "b2.md": entry("2026-03-24", "21:00", gym="true"),
    "b3.md": entry("2026-03-25", "21:00", gym="true"),
    "b4.md": entry("2026-03-30", "21:00", gym="false"),
    "b5.md": entry("2026-03-31", "21:00", gym="false"),
    "b6.md": entry("2026-04-01", "21:00", gym="false"),
}


def build_vault(root, files):
    """A vault whose journal folder is emoji-prefixed, Spanish AND plural."""
    journal = root / "📓 Diarios"
    journal.mkdir(parents=True)
    floors = root / "floors"
    floors.mkdir()
    for name, text in FLOOR_NOTES.items():
        (floors / name).write_text(text, encoding="utf-8")
    for name, text in files.items():
        (journal / name).write_text(text, encoding="utf-8")
    return root


def run(vault, *args):
    """Invoke exactly as insights/SKILL.md documents: no --journal-dir."""
    p = subprocess.run(
        [sys.executable, str(BASELINE), "--vault-root", str(vault)] + list(args),
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        raise AssertionError("monthly-baseline failed ({}):\n{}".format(
            p.returncode, p.stderr))
    return json.loads(p.stdout)


def metric(report, name):
    for m in report["metrics"]["comparison"]:
        if m["metric"] == name:
            return m
    raise AssertionError("metric {} missing from comparison".format(name))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        both = dict(BASE)
        both.update(TARGET)
        vault = build_vault(Path(tmp) / "vault", both)

        # --- contract 1 + 2: a Wednesday resolves the week, folder auto-detected
        r = run(vault, "--week", "2026-04-08")
        period = r["period"]
        assert period["kind"] == "week", period
        assert period["start"] == "2026-04-06", period
        assert period["end"] == "2026-04-12", period
        assert period["n_entries"] == 5, period
        print("OK: --week snaps any weekday back to Monday; "
              "'📓 Diarios' auto-detected without --journal-dir")

        # --- floor scale comes from the vault's own notes, not from this script
        raw = r["metrics"]["period_raw"]
        assert raw["landed_floor_mean"] == 24.0, raw["landed_floor_mean"]
        assert any("99" in c for c in r["consistency"]), r["consistency"]
        print("OK: floor numbers + the floor_num contradiction come from the "
              "vault's floor notes")

        # --- contract 3: the morning false is no-data, the opt-out still counts
        assert raw["gym_known_n"] == 3, raw
        assert raw["gym_premature_n"] == 1, raw
        assert raw["gym_rate_pct"] == 33.3, raw
        assert raw["gym_days_week_field"] == 2, raw
        assert raw["gym_weeks_with_field"] == 1, raw
        print("OK: a morning `gym: false` reads as no-data, `gym_confirmed` "
              "opts out, and `gym_week` is preferred over the daily boolean")

        # --- contract 4: 3 known days => 33.3pp steps against a 5.0pp band
        m = metric(r, "gym_rate_pct")
        assert m["status"] == "insufficient_resolution", m
        assert m["resolution_pp"] == 33.3, m
        print("OK: a metric coarser than its own stable band reports "
              "insufficient_resolution instead of a permanent anomaly")

        # --- a thin baseline refuses to compare at all
        thin = build_vault(Path(tmp) / "thin", TARGET)
        r2 = run(thin, "--week", "2026-04-06")
        assert all(m["status"] == "insufficient_baseline"
                   for m in r2["metrics"]["comparison"]), r2["metrics"]["comparison"]
        assert r2["warnings"], r2["warnings"]
        print("OK: a baseline under --min-baseline reports "
              "insufficient_baseline rather than a number")

    print("ALL PASS: monthly-baseline weeks, localized journal folder, "
          "habit-field honesty, resolution guard.")


if __name__ == "__main__":
    main()
