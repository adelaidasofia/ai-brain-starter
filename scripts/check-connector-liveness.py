#!/usr/bin/env python3
"""Guard: surface a connector that silently goes empty (the "0-vs-0" gap).

An ingest connector (Granola, WhatsApp, iMessage, Slack, Gmail, ...) can keep
exiting 0 while quietly returning 0 items after a vendor changes a surface. The
launchd exit-status watchdog catches NON-zero exits; it does NOT catch "returns
0 when it should return >0." Origin: granola_export returned 0 transcripts for
~3 weeks after Granola swapped its local storage (cache-v6.json -> encrypted
DB). The exit code stayed 0; it surfaced only because a human noticed. This is a
CLASS, not a one-off.

Signal source: connectors already persist their runs, so this reads real durable
history instead of running a live, credential-gated probe (which would soft-pass
forever the moment the credential rots):

  - WhatsApp / iMessage / Slack / Gmail (and any future ingest-* connector) write
    `External Inputs/<Source>/<scope>/<YYYY-MM-DD>.md` with a *count* frontmatter
    key (item_count / message_count / count). The filename date is authoritative.
  - Granola writes `Meeting Notes/<YYYY-MM-DD> - <title> - Transcript.md`
    (one file per meeting, no count; existence == data for that day).

Detection (distinguishing "broke" from "nothing happened" without a live
cross-signal): for each (source, scope) with an established track record, a
connector is OVERDUE when it has been silent LONGER than it has ever been silent
before AND longer than its configured cadence floor:

    data_days  = dates that produced data (count > 0, or a countless file/transcript)
    tolerance  = max(cadence_floor[source], longest historical gap between data_days)
    silence    = (now - last data_day)
    OVERDUE    = silence > tolerance        (and len(data_days) >= MIN_DATA_DAYS)

A regular daily connector (tolerance ~1-2d) trips within one cadence window of a
break; a genuinely sparse-but-on-rhythm connector (large historical gaps -> large
tolerance) does NOT false-alarm just because it is quiet. That asymmetry is the
whole point: 0-because-the-source-broke vs 0-because-nothing-happened.

This is a DIAGNOSTIC / SessionStart surface, not an install gate: a hit is a WARN
with the remedy, never a hard FAIL.

Usage:
  check-connector-liveness.py [--porcelain] [--now YYYY-MM-DD] [--config PATH] [VAULT]

  --now      Evaluate "today" as this date (default: today). The test seam.
  --config   JSON: {"cadence_days": {"slack": 2, ...}} merged over the defaults.
             If omitted, also reads <vault>/Meta/connector-cadence.json when present.
  VAULT      Vault root (default: walk up from CWD; else CWD).

Exit codes:
  0  OK    - every connector is fresh, or no connectors found
  1  HIT   - one or more connectors are silently overdue
  2  USAGE - bad arguments

Porcelain first token: OK_ALL_FRESH | SKIP_NO_CONNECTORS | CONNECTOR_GAP:<source>:<scope>:<days_silent>
  (one CONNECTOR_GAP line per overdue connector)
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

# A connector needs at least this many data-days of history before silence can be
# called a break. Below it there is no rhythm to judge against, so we stay quiet
# (a brand-new or one-off ingest must not trip the watchdog).
MIN_DATA_DAYS = 3

# Per-source cadence FLOOR in days: the minimum tolerance regardless of observed
# history, so a tight-cadence connector still gets a small grace before alarming.
# The effective tolerance is max(this floor, the connector's own longest gap).
DEFAULT_CADENCE_DAYS = {
    "granola": 3,
    "whatsapp": 3,
    "imessage": 3,
    "slack": 2,
    "gmail": 2,
    "health": 2,
    "linear": 3,
    "github": 7,
    "notion": 7,
    "youtube": 7,
}
FALLBACK_CADENCE_DAYS = 7

USAGE = "usage: check-connector-liveness.py [--porcelain] [--now YYYY-MM-DD] [--config PATH] [VAULT]"

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_GRANOLA_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) - .* - Transcript\.md$")
_COUNT_KEY_RE = re.compile(r"^[a-z_]*count\s*:\s*(-?\d+)\s*$")


def _parse_date(s: str) -> date | None:
    m = _DATE_RE.match(s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _frontmatter_count(path: Path) -> int | None:
    """Return the item count from an External-Inputs day file, or None if the
    file carries no *count* key (countless file == treat as data). Reads only the
    leading frontmatter block; no PyYAML dependency (ingest-* skills ship without
    it). Best-effort: an unreadable file is treated as countless (None)."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    lines = text.splitlines()
    # lines[0] is the opening '---'; scan to the closing '---'.
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = _COUNT_KEY_RE.match(line.strip())
        if m:
            return int(m.group(1))
    return None


def _data_days_for_external_scope(scope_dir: Path) -> list[date]:
    """Dates under External Inputs/<Source>/<scope>/ that produced data: a
    YYYY-MM-DD.md file whose count is missing or > 0 (count == 0 is a real
    'ran, got nothing' day and is NOT data)."""
    days: list[date] = []
    try:
        entries = sorted(scope_dir.iterdir())
    except OSError:
        return days
    for entry in entries:
        if not entry.name.endswith(".md"):
            continue
        d = _parse_date(entry.stem)
        if d is None:
            continue
        count = _frontmatter_count(entry)
        if count is None or count > 0:
            days.append(d)
    return days


def discover_external_inputs(vault: Path) -> dict[tuple[str, str], list[date]]:
    """Map (source, scope) -> sorted unique data-days for every connector that
    has landed files under External Inputs/."""
    out: dict[tuple[str, str], list[date]] = {}
    root = vault / "External Inputs"
    if not root.is_dir():
        return out
    try:
        sources = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return out
    for src_dir in sources:
        source = src_dir.name.lower()
        try:
            scopes = sorted(p for p in src_dir.iterdir() if p.is_dir())
        except OSError:
            continue
        for scope_dir in scopes:
            days = sorted(set(_data_days_for_external_scope(scope_dir)))
            if days:
                out[(source, scope_dir.name)] = days
    return out


def discover_granola(vault: Path) -> dict[tuple[str, str], list[date]]:
    """Granola does not use External Inputs; it drops one transcript per meeting
    into Meeting Notes/. Each transcript is a data-day."""
    for name in ("📝 Meeting Notes", "Meeting Notes"):
        mdir = vault / name
        if mdir.is_dir():
            break
    else:
        return {}
    days: list[date] = []
    try:
        entries = sorted(mdir.iterdir())
    except OSError:
        return {}
    for entry in entries:
        m = _GRANOLA_RE.match(entry.name)
        if not m:
            continue
        d = _parse_date(m.group(1))
        if d is not None:
            days.append(d)
    days = sorted(set(days))
    return {("granola", "meetings"): days} if days else {}


def _max_gap_days(days: list[date]) -> int:
    """Longest gap (in days) between consecutive data-days. 0 for a single day."""
    if len(days) < 2:
        return 0
    return max((days[i + 1] - days[i]).days for i in range(len(days) - 1))


def evaluate(
    connectors: dict[tuple[str, str], list[date]],
    now: date,
    cadence_days: dict[str, int],
) -> list[tuple[str, str, int, int]]:
    """Return [(source, scope, days_silent, tolerance)] for every overdue
    connector, sorted for stable output. A connector is overdue when it has an
    established rhythm and has been silent longer than the larger of its cadence
    floor and its own longest historical quiet stretch."""
    gaps: list[tuple[str, str, int, int]] = []
    for (source, scope), days in connectors.items():
        if len(days) < MIN_DATA_DAYS:
            continue
        floor = cadence_days.get(source, FALLBACK_CADENCE_DAYS)
        tolerance = max(floor, _max_gap_days(days))
        silence = (now - days[-1]).days
        if silence > tolerance:
            gaps.append((source, scope, silence, tolerance))
    return sorted(gaps)


def _detect_vault(start: Path) -> Path:
    """Walk up from `start` looking for a vault marker; fall back to `start`."""
    cur = start.resolve()
    for cand in [cur, *cur.parents]:
        if (cand / "External Inputs").is_dir() or (cand / "⚙️ Meta").is_dir() \
                or (cand / "Meta").is_dir():
            return cand
    return cur


def _load_cadence_config(config_path: Path | None, vault: Path) -> dict[str, int]:
    cadence = dict(DEFAULT_CADENCE_DAYS)
    paths: list[Path] = []
    if config_path is not None:
        paths.append(config_path)
    else:
        for name in ("⚙️ Meta", "Meta"):
            paths.append(vault / name / "connector-cadence.json")
    for p in paths:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        overrides = data.get("cadence_days", {})
        if isinstance(overrides, dict):
            for k, v in overrides.items():
                try:
                    cadence[str(k).lower()] = int(v)
                except (TypeError, ValueError):
                    continue
    return cadence


def main(argv):
    porcelain = "--porcelain" in argv
    args = [a for a in argv if a != "--porcelain"]

    now: date | None = None
    config_path: Path | None = None
    vault_arg: Path | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--now":
            i += 1
            if i >= len(args):
                print(USAGE, file=sys.stderr)
                return 2
            now = _parse_date(args[i])
            if now is None:
                print("--now must be YYYY-MM-DD", file=sys.stderr)
                return 2
        elif a == "--config":
            i += 1
            if i >= len(args):
                print(USAGE, file=sys.stderr)
                return 2
            config_path = Path(args[i]).expanduser()
        elif a.startswith("--"):
            print("unknown argument: {}".format(a), file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return 2
        else:
            vault_arg = Path(a).expanduser()
        i += 1

    if now is None:
        now = datetime.now().date()
    vault = vault_arg.resolve() if vault_arg is not None else _detect_vault(Path.cwd())
    cadence = _load_cadence_config(config_path, vault)

    connectors = discover_external_inputs(vault)
    connectors.update(discover_granola(vault))

    if not connectors:
        if porcelain:
            print("SKIP_NO_CONNECTORS")
        else:
            print("OK    No ingest connectors have landed data yet (nothing to watch).")
        return 0

    gaps = evaluate(connectors, now, cadence)

    if not gaps:
        if porcelain:
            print("OK_ALL_FRESH")
        else:
            print("OK    All {} connector(s) are producing data within their "
                  "expected cadence.".format(len(connectors)))
        return 0

    if porcelain:
        for source, scope, silence, _tol in gaps:
            print("CONNECTOR_GAP:{}:{}:{}".format(source, scope, silence))
        return 1

    print("WARN  {} connector(s) have silently stopped producing data "
          "(the 0-vs-0 gap):".format(len(gaps)))
    for source, scope, silence, tol in gaps:
        print("      - {}/{}: no new data for {} day(s) "
              "(longer than its {}-day tolerance).".format(source, scope, silence, tol))
    print("      A connector can exit 0 while returning 0 items after a vendor")
    print("      changes a surface. Check each source's auth/permissions and run")
    print("      its ingest skill manually; confirm it pulls >0 items, then")
    print("      re-run this check. See scripts/granola_sync.py for the origin case.")
    return 1


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
