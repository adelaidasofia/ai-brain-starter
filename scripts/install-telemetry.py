#!/usr/bin/env python3
"""install-telemetry.py — append + summarize per-phase install events.

Two subcommands. `append` writes one JSON line per phase to
`~/.claude/.ai-brain-starter-install.jsonl` AND updates the progress
marker at `~/.claude/.ai-brain-starter-progress.json`. `summarize`
reads the JSONL and prints a per-phase + per-install table (time-on-phase,
skip rate, new-improvisation count) so the maintainer can spot patterns
across many installs without reading the raw log.

This is the Goldsmith-pattern tracking layer codified after a friend-test
surfaced 5 Claude improvisations that the SKILL.md didn't anticipate.
The signal worth tracking is the RATE OF NEW failure modes per install
— if it trends to zero across N workshops, the architecture is converging;
if it stays flat, the install is too long and the BANNED PATTERNS table
is treating symptoms instead of the cause.

Usage:
  install-telemetry.py append PHASE OUTCOME [--workshop] [--new-improvisation STR] [--redirected]
  install-telemetry.py summarize [--since YYYY-MM-DD] [--workshop-only]

Outcomes: completed | skipped_on_user_no | errored

PHASE is a string identifier like "0", "10a", "23.5", "24".

No network. No third-party deps. Stdlib only. Safe to call from bash.
"""

from __future__ import annotations

import argparse
import collections
import datetime as _dt
import json
import os
import pathlib
import sys
from typing import Any

LOG_PATH = pathlib.Path.home() / ".claude" / ".ai-brain-starter-install.jsonl"
PROGRESS_PATH = pathlib.Path.home() / ".claude" / ".ai-brain-starter-progress.json"
PROGRESS_VERSION = 1
VALID_OUTCOMES = {"completed", "skipped_on_user_no", "errored"}


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_parent(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def cmd_append(args: argparse.Namespace) -> int:
    if args.outcome not in VALID_OUTCOMES:
        print(
            f"error: outcome must be one of {sorted(VALID_OUTCOMES)}, got {args.outcome!r}",
            file=sys.stderr,
        )
        return 2

    event: dict[str, Any] = {
        "ts": _now_iso(),
        "phase": args.phase,
        "outcome": args.outcome,
        "user_redirected": bool(args.redirected),
        "new_improvisation_seen": args.new_improvisation,
    }

    _ensure_parent(LOG_PATH)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

    if args.outcome == "completed":
        _ensure_parent(PROGRESS_PATH)
        PROGRESS_PATH.write_text(
            json.dumps(
                {
                    "last_completed_phase": args.phase,
                    "ts": event["ts"],
                    "version": PROGRESS_VERSION,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    print(f"logged phase={args.phase} outcome={args.outcome}")
    return 0


def _read_events() -> list[dict[str, Any]]:
    if not LOG_PATH.exists():
        return []
    events: list[dict[str, Any]] = []
    for line_no, raw in enumerate(LOG_PATH.read_text(encoding="utf-8").splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            events.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            print(
                f"warning: line {line_no} not valid JSON, skipping ({exc})",
                file=sys.stderr,
            )
    return events


def _ts(event: dict[str, Any]) -> _dt.datetime | None:
    raw = event.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        return _dt.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
    except ValueError:
        return None


def cmd_summarize(args: argparse.Namespace) -> int:
    events = _read_events()
    if not events:
        print("no events logged at", LOG_PATH)
        return 0

    if args.since:
        try:
            since = _dt.datetime.strptime(args.since, "%Y-%m-%d").replace(
                tzinfo=_dt.timezone.utc
            )
        except ValueError:
            print(f"error: --since must be YYYY-MM-DD, got {args.since!r}", file=sys.stderr)
            return 2
        events = [e for e in events if (t := _ts(e)) and t >= since]

    if not events:
        print("no events match the filters")
        return 0

    by_phase: dict[str, collections.Counter[str]] = collections.defaultdict(
        collections.Counter
    )
    new_improvisations: list[tuple[str, str, str]] = []
    redirect_count = 0
    for e in events:
        phase = str(e.get("phase", "?"))
        outcome = str(e.get("outcome", "?"))
        by_phase[phase][outcome] += 1
        imp = e.get("new_improvisation_seen")
        if isinstance(imp, str) and imp.strip():
            new_improvisations.append((str(e.get("ts", "")), phase, imp.strip()))
        if e.get("user_redirected"):
            redirect_count += 1

    total_events = len(events)
    print(f"events: {total_events}")
    print(f"distinct phases touched: {len(by_phase)}")
    print(f"user redirections (across all phases): {redirect_count}")
    print(f"new improvisations surfaced: {len(new_improvisations)}")
    print()
    print(f"{'phase':<10} {'completed':>9} {'skipped':>9} {'errored':>9}")
    for phase in sorted(by_phase.keys()):
        counts = by_phase[phase]
        print(
            f"{phase:<10} {counts.get('completed', 0):>9} "
            f"{counts.get('skipped_on_user_no', 0):>9} "
            f"{counts.get('errored', 0):>9}"
        )

    if new_improvisations:
        print()
        print("new improvisations (chronological — these are next BANNED-PATTERN candidates):")
        for ts, phase, msg in new_improvisations[-20:]:
            print(f"  {ts}  phase={phase}  {msg}")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_append = sub.add_parser("append", help="record one phase event")
    p_append.add_argument("phase", help='phase identifier (e.g. "0", "10a", "23.5")')
    p_append.add_argument(
        "outcome",
        help=f'one of {sorted(VALID_OUTCOMES)}',
    )
    p_append.add_argument(
        "--new-improvisation",
        default=None,
        help="short description of an improvisation Claude did that the skill did not prescribe",
    )
    p_append.add_argument(
        "--redirected",
        action="store_true",
        help="user redirected mid-phase (correction signal)",
    )
    p_append.set_defaults(func=cmd_append)

    p_sum = sub.add_parser("summarize", help="aggregate per-phase + per-install stats")
    p_sum.add_argument("--since", default=None, help="filter events after YYYY-MM-DD (UTC)")
    p_sum.set_defaults(func=cmd_summarize)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
