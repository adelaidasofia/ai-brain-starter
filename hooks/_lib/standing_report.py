"""Standing-inventory reporting for SessionStart surfacers.

WHY THIS EXISTS
---------------
Measured 2026-08-15: a SessionStart fire rendered 26,231 chars across ~26
surfacers, and the shape repeats. Each one reports a STANDING INVENTORY — 90
untracked automations, 15 unversioned skills, 5 stranded claim branches, a B2
replica 63 days stale — in full, every session, whether or not anything moved.

Two things go wrong at once:

  1. The full inventory is the RATIONALE, not the order. The reader needs
     "still 90, nothing changed" far more often than the enumeration.
  2. A finding-set that never changes trains itself into wallpaper. Once it is
     wallpaper, a set that DID change looks exactly the same as one that did
     not, which is the failure the surfacer exists to prevent.

So: speak in FULL when the finding-set changes, and in ONE line when it has not
— a line that carries the count AND how long it has been stuck, which is the
fact a standing alarm actually needs to convey and never does.

THIS IS NOT SILENCING
---------------------
Coverage is unchanged. Nothing is suppressed: every session still reports the
condition and the count. What drops is the re-enumeration of an unchanged list.
A change of ANY kind — one item added, removed, or edited — restores the full
render on the very next fire. The digest line also names the age, so a stuck
alarm gets LOUDER in meaning as it ages instead of fading.

FAIL-OPEN
---------
Every error path returns the full text. A broken condenser degrades to today's
behaviour (verbose), never to silence.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path

BYPASS_ENV = "STANDING_REPORT_BYPASS"
STATE_DIR_ENV = "STANDING_REPORT_STATE_DIR"


def _state_dir() -> Path:
    """Where the condenser remembers what it already said.

    Overridable, and resolved at CALL time rather than import time, because this
    state decides what a hook RENDERS. A test that drives a condensing surfacer
    otherwise reads and WRITES the developer's real ~/.claude/.standing-reports,
    so its result turns on whether that machine happened to render the same
    finding-set before - which is not a property of the code under test.

    That is exactly how #539 behaved: green on a clean CI runner, where no prior
    state exists so every run is a "changed" first render and the FULL section
    comes back; red on a developer box, where prior state makes the digest fire
    and the per-item enumeration the assertion looks for is gone. Same code,
    opposite verdicts, decided entirely by unpinned machine state.

    Sandboxing HOME is NOT a substitute. On Windows, Python's
    ntpath.expanduser() reads USERPROFILE and ignores HOME (MYC-3536), so
    Path.home() walks straight back out to the real profile.
    """
    return Path(
        os.environ.get(STATE_DIR_ENV)
        or (Path.home() / ".claude" / ".standing-reports")
    )


def _state_file(key: str) -> Path:
    safe = "".join(c for c in key if c.isalnum() or c in "-_")[:60] or "unknown"
    return _state_dir() / f"{safe}.json"


def _age_phrase(first_seen: str, n: int, exact: bool) -> str:
    """`exact` = n counts distinct sessions; otherwise it counts FIRES.

    The unit is stated rather than assumed. SessionStart fires ~2.5x per session
    and Stop fires once per turn, so a fire count labelled "sessions" overstates
    by a lot — it printed "93 session(s) today" on its first day live. A digest
    whose own number is wrong is worse than a digest with no number, so when the
    caller cannot supply a session id the label says what is actually counted.
    """
    unit = "session(s)" if exact else "fire(s)"
    try:
        d = (date.today() - date.fromisoformat(first_seen)).days
    except Exception:
        return f"unchanged for {n} {unit}"
    if d <= 0:
        return f"unchanged, {n} {unit} today"
    return f"unchanged for {d}d / {n} {unit}"


def report(key: str, full: str, summary: str, session_id: str | None = None) -> str:
    """Full text when the finding-set moved; a one-line digest when it did not.

    key      stable id for this surfacer, e.g. "untracked-tooling".
    full     the complete render (what the hook prints today).
    summary  ONE line carrying the count and the actionable order. The digest
             appends how long the set has been stuck and where the detail lives.
    """
    if os.environ.get(BYPASS_ENV) == "1" or not full:
        return full
    try:
        digest = hashlib.sha1(full.encode("utf-8", "replace")).hexdigest()[:16]
        path = _state_file(key)
        prev = {}
        if path.exists():
            try:
                prev = json.loads(path.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                prev = {}
        today = date.today().isoformat()
        if prev.get("hash") == digest:
            # Count distinct SESSIONS, not fires. SessionStart fires ~2.5x per
            # session (startup + resume + compact) and Stop fires once per turn,
            # so incrementing per invocation reported "93 session(s) today" on a
            # machine that had run a handful — a digest whose own number is
            # wrong is worse than no number. Seen 2026-08-16 on the first day
            # this shipped.
            seen_ids = prev.get("session_ids") or []
            sid = str(session_id) if session_id else ""
            if sid and sid not in seen_ids:
                seen_ids.append(sid)
                seen_ids = seen_ids[-200:]
            n = len(seen_ids) if seen_ids else int(prev.get("sessions", 1)) + 1
            first = prev.get("first_seen", today)
            prev.update({"sessions": n, "last_seen": today,
                         "session_ids": seen_ids})
            _write(path, prev)
            detail = prev.get("detail_path") or ""
            tail = f"  ·  detail: {detail}" if detail else ""
            digest_line = f"{summary}  [{_age_phrase(first, n, bool(seen_ids))}]{tail}"
            # A "condensed" form longer than what it replaces is not a
            # compression, it is a regression with extra steps. Short renders
            # are already at their floor; hand them back untouched.
            if len(digest_line) >= len(full):
                return full
            return digest_line
        _write(path, {"hash": digest, "first_seen": today, "last_seen": today,
                      "sessions": 1,
                      "session_ids": [str(session_id)] if session_id else [],
                      "detail_path": prev.get("detail_path", "")})
        return full
    except Exception:
        return full


def condense(key: str, full: str, session_id: str | None = None) -> str:
    """report() using the render's own FIRST LINE as the summary.

    Every one of these surfacers already opens with its count and its order
    ("[untracked-tooling] 90 deployed personal automation(s) a fresh install
    will not reproduce"). That line IS the summary, so wiring stays one call
    per hook and no summary is hand-maintained in two places.
    """
    if not full:
        return full
    lines = [ln.rstrip() for ln in full.splitlines() if ln.strip()]
    if not lines:
        return full
    summary = lines[0]
    # Some renders open with a bare "[tag]" header and put the COUNT on the next
    # line. Taking line 1 alone there produces a digest with no number in it —
    # quieter AND less informative, which is exactly the trade this must never
    # make. So when the opener carries no figure, pull in the first following
    # line that does.
    if not any(c.isdigit() for c in summary) or len(summary) < 50:
        for ln in lines[1:4]:
            if any(c.isdigit() for c in ln):
                extra = ln.strip()
                if len(extra) > 180:
                    extra = extra[:180].rsplit(" ", 1)[0] + "…"
                summary = f"{summary} {extra}" if summary.strip() else extra
                break
    return report(key, full, summary, session_id)


def set_detail_path(key: str, path: str) -> None:
    """Record where the full list can be read, for the digest line to cite."""
    try:
        p = _state_file(key)
        cur = {}
        if p.exists():
            try:
                cur = json.loads(p.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                cur = {}
        cur["detail_path"] = str(path)
        _write(p, cur)
    except Exception:
        pass


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)
