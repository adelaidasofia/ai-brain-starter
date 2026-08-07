#!/usr/bin/env python3
"""LOUD tripwire: this session is running inside an Obsidian-vault git worktree.

The bug (MYC-575): the Claude Desktop app's per-session "worktree" checkbox
creates a `.claude/worktrees/<slug>/` checkout. On a CODE repo that is cheap and
correct. On an OBSIDIAN VAULT (repo-root == vault-root, ~6.5K+ files) that
checkout lands INSIDE Obsidian's watched tree -> 250%+ CPU, 2+ GB RAM, crash --
AND the worktree can be silently archived/deleted mid-session (observed twice:
MYC-555, then again 2026-06-17 during a repo-evaluation). Work survives ONLY
because the discipline (edit shared/canonical files at the MAIN vault path +
commit) was followed; a less-careful session would lose worktree-only files.

The real fix is to stop the Desktop app creating the vault worktree at all
(MYC-575, operator-gated: a Desktop toggle / settings lever / upstream request).
A hook can't un-create the worktree the session already started in -- but it CAN
make the bad state LOUD on the first turn so the human aborts and relaunches
plain BEFORE the melt compounds and BEFORE anything is silently lost. This is the
enforcement/tripwire layer; it self-quiets to a no-op once the source fix holds
(a plain vault session never trips it).

Detection reads THREE independent channels; first hit wins. The `.obsidian/`
gate (applied in main()) distinguishes vault from code repo in ALL THREE, so a
code-repo worktree (Desktop `.claude/worktrees/` on a ~/dev repo, or the
sibling-dir `claude-dev-worktree` `~/dev/<repo>-<slug>/`) never fires.
  - Channel A (payload cwd): cwd is under a `.claude/worktrees/` segment. This
    is the terminal/CLI shape AND every tool-time payload (PreToolUse/UPS reliably
    carry the worktree cwd -- proven: check-cd-outside-worktree fires correctly there).
  - Channel B (transcript marker): cwd is the main root; the worktree id is in
    transcript_path as `--claude-worktrees-<slug>` (Desktop SessionStart, when the
    marker is present).
  - Channel C (process-cwd ground truth, payload-INDEPENDENT, ZERO subprocess): the
    melt happens because the SESSION RUNS INSIDE the worktree, so the hook's own
    `os.getcwd()` (the resolved physical path) carries the `/.claude/worktrees/`
    segment even when the harness reported cwd=main at SessionStart (A) with no
    transcript marker (B) -- exactly the 2026-06-17 miss. A linked worktree's git
    toplevel is always a PREFIX of os.getcwd(), so a literal segment match here is a
    complete superset of what `git rev-parse --show-toplevel` would reveal -- git
    was strictly redundant. It was ALSO a subprocess that fired on EVERY plain
    (non-worktree) UserPromptSubmit + PreToolUse(Bash) call -- the 99% case -- a
    fleet-wide hot-path tax (MYC-1176). C is now a pure string test: zero git, zero
    cost on the plain path. Skipped entirely at PreToolUse (the highest-fan-out
    event) where Channel A is authoritative (payload cwd reliably = the worktree).

Wiring: SessionStart (catches terminal launches + Desktop-with-marker early) PLUS
UserPromptSubmit + PreToolUse(Bash) (the tool-time events where the worktree cwd
is reliably present, so Channel A catches the Desktop-SessionStart-miss case; C
backstops SessionStart + UPS via os.getcwd()). A once-per-worktree-slug dedup
sentinel means it warns ONCE per session, not on every prompt/tool. Slugs are
unique per Desktop worktree, so once-per-slug == once-per-session.

Fail-open: any error -> silent continue (a nudge must never block a session).
Telemetry: emits one guard-fire record (_lib/guard_telemetry.log_fire) on the WARN
path only -- never the silent/plain hot path -- so guard-fleet-telemetry classifies
it FIRING not UNINSTRUMENTED (MYC-1176 item 6). Defensive import: no-op if the rail
is absent, so a stripped install never breaks.
Bypass: VAULT_WORKTREE_WARN_BYPASS=1.
Test knobs: VAULT_WORKTREE_WARN_NODEDUP=1 (skip dedup), VAULT_WORKTREE_WARN_STATE_DIR
(redirect the sentinel dir), GUARD_FIRES_LOG (redirect the telemetry sink).

Canonical: the maintainer's private skills repo (deployed as a copy to
~/.claude/hooks/ via install.sh). Negative-control test: warn-vault-session-in-worktree.test.sh.
Ports to ai-brain-starter (MYC-576 -- every installed vault hits the same melt);
guard_telemetry.py ports with it (MYC-809 -- port the rails, not just the pattern).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Telemetry rail (MYC-1176 item 6): emit a guard-fire on the WARN path so
# guard-fleet-telemetry classifies this hook FIRING, not UNINSTRUMENTED. Defensive
# import -- a missing rail must NEVER break this fail-open hook.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "_lib"))
    from guard_telemetry import log_fire as _log_fire
except Exception:  # pragma: no cover - rail absent on a stripped install
    def _log_fire(*_a, **_k):
        return False

WORKTREE_SEGMENT = "/.claude/worktrees/"
# Claude Code encodes the session cwd into the ~/.claude/projects/<dir> name by
# replacing path separators (and the leading dot of `.claude`) with dashes, so a
# worktree session's transcript path carries this literal marker -- WHEN the
# harness passes a worktree-rooted transcript_path (not guaranteed at SessionStart).
PROJECTS_WORKTREE_MARKER = "--claude-worktrees-"


def _silent() -> int:
    print(json.dumps({"continue": True, "suppressOutput": True}))
    return 0


def _read_payload() -> tuple[str, str, str]:
    """Hook stdin provides `cwd` + `transcript_path` + `hook_event_name`.
    Returns ("", "", "") on anything odd."""
    cwd = ""
    transcript = ""
    event = ""
    try:
        raw = sys.stdin.read()
        if raw.strip():
            data = json.loads(raw)
            if isinstance(data, dict):
                cwd = data.get("cwd") or data.get("workingDirectory") or ""
                transcript = data.get("transcript_path") or data.get("transcriptPath") or ""
                event = data.get("hook_event_name") or data.get("hookEventName") or ""
    except Exception:
        pass
    return cwd, transcript, event


def _norm(path: str) -> str:
    """Forward-slash normalization so WORKTREE_SEGMENT matches Windows paths."""
    return path.replace("\\", "/")


def _split_segment(path: str) -> tuple[str, Path]:
    """(worktree_root, main_root) from a path carrying WORKTREE_SEGMENT.

    Normalizes a deep cwd (a worktree subdir) back to the worktree ROOT so the
    dedup slug stays stable regardless of how far inside the session sits.
    """
    path = _norm(path)
    idx = path.index(WORKTREE_SEGMENT)
    main_root = path[:idx]
    slug = path[idx + len(WORKTREE_SEGMENT):].split("/", 1)[0]
    return str(Path(main_root) / ".claude" / "worktrees" / slug), Path(main_root)


def _detect(payload_cwd: str, transcript: str, event: str) -> tuple[str, Path] | tuple[None, None]:
    """(worktree_path, main_root) if a worktree session on any channel, else (None, None).

    ZERO subprocess -- every channel is a string/stat test (MYC-1176: the prior
    git-subprocess Channel C ran on every plain-session hot-path call, fleet-wide).
    The `.obsidian/` vault gate is applied by the caller, not here.
    """
    # Channel A -- any payload cwd that contains the worktree segment (terminal +
    # tool-time payloads; the reliable signal at UserPromptSubmit + PreToolUse).
    if payload_cwd and WORKTREE_SEGMENT in _norm(payload_cwd):
        return _split_segment(payload_cwd)

    # Channel B -- transcript marker (Desktop mode, when the marker is present).
    if payload_cwd and transcript and PROJECTS_WORKTREE_MARKER in transcript:
        slug = transcript.split(PROJECTS_WORKTREE_MARKER, 1)[1].split("/", 1)[0]
        if slug:
            return str(Path(payload_cwd) / ".claude" / "worktrees" / slug), Path(payload_cwd)

    # Channel C -- the hook's OWN process cwd is inside a .claude/worktrees/ checkout.
    # The melt's ground truth: the session RUNS inside the worktree, so os.getcwd()
    # (resolved physical path) carries the segment even when the harness reported
    # cwd=main at SessionStart (A) with no marker (B) -- the 2026-06-17 miss. A
    # worktree's git toplevel is always a PREFIX of os.getcwd(), so this literal
    # match is a complete superset of `git rev-parse --show-toplevel` -- no
    # subprocess. Skipped at PreToolUse (highest-fan-out event) where Channel A is
    # authoritative (payload cwd reliably = the worktree there).
    if event != "PreToolUse":
        try:
            proc_cwd = os.getcwd()
        except OSError:
            proc_cwd = ""
        if proc_cwd and WORKTREE_SEGMENT in _norm(proc_cwd):
            return _split_segment(proc_cwd)

    return None, None


def _already_warned(slug: str) -> bool:
    """Once-per-worktree-slug dedup so the re-firing events (UPS each turn / every Bash)
    warn ONCE. Slugs are unique per Desktop worktree, so once-per-slug == once-per-session.
    Stale sentinels for archived worktrees are harmless (a slug never recurs)."""
    if os.environ.get("VAULT_WORKTREE_WARN_NODEDUP") == "1":
        return False
    state_dir = os.environ.get("VAULT_WORKTREE_WARN_STATE_DIR") or tempfile.gettempdir()
    sentinel = Path(state_dir) / f".vault-worktree-warned-{slug}"
    try:
        if sentinel.exists():
            return True
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
    except OSError:
        pass  # fail-open: if we cannot record, warn (better loud than silent)
    return False


def main() -> int:
    if os.environ.get("VAULT_WORKTREE_WARN_BYPASS") == "1":
        return _silent()

    payload_cwd, transcript, event = _read_payload()
    worktree_path, main_root = _detect(payload_cwd, transcript, event)
    if worktree_path is None:
        return _silent()  # not a worktree session on any channel

    try:
        is_vault = (main_root / ".obsidian").is_dir()
    except OSError:
        return _silent()
    if not is_vault:
        return _silent()  # a code-repo worktree -- cheap + correct, not the melt

    slug = Path(worktree_path).name
    if _already_warned(slug):
        return _silent()  # already warned this worktree -- do not nag every turn

    body = (
        "SURFACE THIS TO THE USER IMMEDIATELY, before any other work:\n\n"
        "🛑 [vault-worktree] This vault session is running INSIDE a git worktree:\n"
        f"    {worktree_path}\n\n"
        "The vault must NEVER run in worktree mode. The Desktop app's\n"
        "per-session worktree checkbox created a multi-thousand-file checkout inside\n"
        "Obsidian's watched tree → 250%+ CPU / 2+ GB RAM melt, AND this worktree can\n"
        "be SILENTLY DELETED mid-session (that is exactly what happened in the\n"
        "sessions that prompted this guard).\n\n"
        "DO THIS NOW:\n"
        "  1. Close this session.\n"
        f"  2. Relaunch the vault PLAIN: `cd {main_root} && claude`\n"
        "     (or, in the Desktop app, open the vault with the worktree box UNCHECKED).\n\n"
        "UNTIL YOU DO: any file created that lives ONLY in this worktree is discarded\n"
        f"when the worktree is archived. Edit shared/canonical files at the MAIN vault\n"
        f"path ({main_root}/...) and commit via vault-safe-commit.sh — never the\n"
        "worktree path. The Stop-hook snapshot net backstops divergent files, but the\n"
        "only safe state is a plain (non-worktree) vault session.\n\n"
        "Bypass this warning: VAULT_WORKTREE_WARN_BYPASS=1"
    )
    # Telemetry on the WARN path ONLY (never the silent/plain hot path) -- MYC-1176 item 6.
    _log_fire("warn-vault-session-in-worktree", status="warned", slug=slug, event=event or "?")
    print(json.dumps({"continue": True, "additionalContext": body}))
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(main())
    except Exception:
        # Fail-open: never let a nudge crash a session start / tool call.
        try:
            print(json.dumps({"continue": True, "suppressOutput": True}))
        except Exception:
            pass
        sys.exit(0)
