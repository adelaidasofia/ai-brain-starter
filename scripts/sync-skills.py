#!/usr/bin/env python3
"""sync-skills.py — propagate skill updates from the ai-brain-starter repo
into the user's installed ~/.claude/skills/ directory.

Cross-platform (macOS / Linux / Windows) successor to sync-skills.sh, which is
now a thin delegator to this file. The bash version could not run on native
Windows, so Windows installs never received skill-content updates after a pull.

Runs after `git pull` on the starter repo. For each skill bundled under
skills/, syncs every file into the corresponding installed skill folder. Never
destroys user customizations without recovery: any installed file that differs
from the incoming repo file is backed up to <file>.bak-YYYY-MM-DD-HHMM before
being overwritten.

Honors the NEVER-fail-silently rule: writes a structured summary to the
starter repo's .sync.log and prints it to stdout so the session-start hook can
surface it to Claude (who surfaces it to the user).

Skip guards (all preserved from the shell version):
  - Installed skill dir is a symlink        -> managed elsewhere, skip.
  - Installed skill dir has its own .git    -> independently managed fork, skip.
  - Destination file (or a parent dir) is a symlink -> maintainer workflow, skip.

Env overrides for hermetic tests: ABS_SYNC_STARTER_DIR, ABS_SYNC_INSTALL_DIR.

Usage: python3 ~/.claude/skills/ai-brain-starter/scripts/sync-skills.py
Exit:  0 clean, 2 if any file operation errored (so the hook can surface it).
"""

from __future__ import annotations

import filecmp
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _starter_dir() -> Path:
    return Path(os.environ.get("ABS_SYNC_STARTER_DIR")
                or (Path.home() / ".claude" / "skills" / "ai-brain-starter"))


def _install_dir() -> Path:
    return Path(os.environ.get("ABS_SYNC_INSTALL_DIR")
                or (Path.home() / ".claude" / "skills"))


# --------------------------------------------------------------------------
# .driftignore — the user's "this one is deliberately mine" list.
#
# vault-repo-drift-check.sh has honored `$REPO_ROOT/.driftignore` since it
# shipped, and .driftignore.example documents `skills/my-private-skill` as a
# supported pattern. This file did not read it, so a skill the user had
# explicitly declared personal was still reported every session AND was still a
# sync target. The gap has a name in the wild: a NAME COLLISION between a
# bundled skill and a user's own skill of the same name. Renaming the bundled
# copy (the correct fix, since two skills cannot share one slug) leaves the
# user's unrelated skill sitting at the bundled skill's old name, where this
# script compares them heading-for-heading and calls the result "diverged" —
# inviting a reconcile that would destroy one of the two skills.
#
# Ignoring is deliberately BIDIRECTIONAL: an ignored skill is skipped by BOTH
# the drift report and the sync. Silencing the report while still overwriting
# the file on the next `git pull` would be the worse of the two failure modes,
# because it removes the warning and keeps the hazard.

def _driftignore_path(repo_root: Path) -> Path:
    return repo_root / ".driftignore"


def load_driftignore(repo_root: Path) -> list[str]:
    """Substring patterns from `<repo_root>/.driftignore`, one per line, `#`
    comments and blank lines dropped. Missing or unreadable file -> no patterns
    (fail open: an ignore list that cannot be read must never start ignoring
    everything, and must never crash a sync)."""
    patterns: list[str] = []
    try:
        text = _driftignore_path(repo_root).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return patterns
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            patterns.append(stripped)
    return patterns


def is_ignored(skill_name: str, patterns: list[str]) -> bool:
    """True if any pattern is a substring of the path this script emits for the
    skill (`skills/<name>`). Same matching rule as vault-repo-drift-check.sh, so
    one .driftignore governs both checks and a pattern means the same thing in
    each."""
    emitted = f"skills/{skill_name}"
    return any(p in emitted for p in patterns)


class SyncReport:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.updated: list[str] = []
        self.backed_up: list[str] = []
        self.skipped: list[str] = []
        self.errors: list[str] = []


def _any_symlink(path: Path, levels: int = 3) -> bool:
    """True if the path or up to `levels-1` of its parents is a symlink.
    Mirrors the shell guard: never follow a symlinked destination — a
    symlinked install means a maintainer manages the skill upstream, and
    writing through it would clobber their private working tree."""
    p = path
    for _ in range(levels):
        if p.is_symlink():
            return True
        p = p.parent
    return False


def sync_file(src: Path, dest: Path, skill_name: str, stamp: str, r: SyncReport) -> None:
    if not src.is_file():
        return
    if _any_symlink(dest):
        r.skipped.append(f"{skill_name}: {dest.name} (symlinked install, maintainer workflow)")
        return
    if dest.is_file():
        if filecmp.cmp(str(src), str(dest), shallow=False):
            return  # identical — no-op, no noise
        bak = dest.with_name(dest.name + f".bak-{stamp}")
        try:
            shutil.copy2(str(dest), str(bak))
            r.backed_up.append(str(bak))
        except OSError:
            r.errors.append(f"could not back up {dest} before overwrite")
            return
        try:
            shutil.copy2(str(src), str(dest))
            r.updated.append(f"{skill_name}: {dest.name}")
        except OSError:
            r.errors.append(f"could not overwrite {dest} (backup still at {bak})")
    else:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dest))
            r.created.append(f"{skill_name}: {dest.name}")
        except OSError:
            r.errors.append(f"could not create {dest}")


def sync_skill_folder(source_dir: Path, dest_dir: Path, skill_name: str,
                      stamp: str, r: SyncReport) -> None:
    if not source_dir.is_dir():
        return
    if dest_dir.is_symlink():
        r.skipped.append(f"{skill_name}: {dest_dir} is a symlink (managed elsewhere)")
        return
    # An installed skill with its own .git is an independently managed fork —
    # overwriting from the starter would clobber the user's commits.
    if (dest_dir / ".git").exists():
        r.skipped.append(f"{skill_name}: {dest_dir} has its own git repo (independently managed)")
        return
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        r.errors.append(f"could not create {dest_dir}")
        return
    for root, _dirs, files in os.walk(str(source_dir)):
        for name in files:
            src_file = Path(root) / name
            rel = src_file.relative_to(source_dir)
            sync_file(src_file, dest_dir / rel, skill_name, stamp, r)


# --------------------------------------------------------------------------
# Drift DETECTION (MYC-3076) — the read-only twin of the sync above.
#
# sync-skills.py propagates skill content ONLY when the auto-update actually
# moves the clone's HEAD (ai-brain-auto-update.py runs it inside the
# `head != origin` branch). Once the clone reaches origin/main by any path, sync
# never re-fires, so the clone can sit AHEAD of the bare `~/.claude/skills/<name>`
# copies that actually serve a skill — with zero signal. That is
# LIVE-SKILL-COPY-DRIFT (the sequel to MYC-720's clone drift, one level up):
# the 2026-07-14 daily-journal + insights movement mechanics reached the clone
# but not the copies serving /journal + /weekly.
#
# classify_drift() reports it, DIRECTIONALLY: it names sections the clone has
# that a bare copy LACKS (upstream-ahead) and never flags a copy that only LEADS
# upstream (a local edit later upstreamed — e.g. the array-floor form), so the
# surface can never nag a user to overwrite their own newer work. It reuses the
# EXACT skip guards the sync uses (symlink / .git-fork), so what it reports is
# exactly what a sync would touch — one source of truth.

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")


def _headings(text: str) -> list[str]:
    """Ordered, whitespace-collapsed section headings from a SKILL.md body."""
    out: list[str] = []
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            out.append(" ".join(m.group(1).split()))
    return out


def _dedup(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def classify_drift(clone_skills_root: Path, install_root: Path,
                   ignore_patterns: list[str] | None = None) -> list[dict]:
    """Compare each bundled skill's SKILL.md in the clone against the installed
    bare copy. Returns a sorted list of dicts for skills that DIFFER and are not
    skip-guarded; identical/synced, clone-only, and skip-guarded skills are
    omitted. Each dict is {name, status, missing_sections, extra_sections}:

      behind    - the clone has section(s) the bare copy lacks, and none the
                  other way (the clean upstream-ahead case; the headline signal).
      content   - same section set, different body (upstream text improved).
      diverged  - both sides have unique sections (a local fork AND upstream
                  moved; needs a human merge, never a blind overwrite).
      leads     - the bare copy has section(s) the clone lacks and none the
                  other way (the copy is AHEAD — never surfaced as "behind").

    missing_sections = clone-only headings (what an apply would add).
    extra_sections   = bare-only headings (local sections an apply would drop).

    Skills matched by `.driftignore` are omitted entirely. `ignore_patterns`
    defaults to the list at the repo root that owns `clone_skills_root`, which
    keeps the lookup hermetic under the test suite's temporary checkouts.
    """
    results: list[dict] = []
    if not clone_skills_root.is_dir():
        return results
    if ignore_patterns is None:
        ignore_patterns = load_driftignore(clone_skills_root.parent)
    for skill_dir in sorted(clone_skills_root.iterdir()):
        if not skill_dir.is_dir():
            continue
        name = skill_dir.name
        if is_ignored(name, ignore_patterns):
            continue  # declared personal in .driftignore -> not drift, not ours
        src = skill_dir / "SKILL.md"
        if not src.is_file():
            continue  # nothing to compare for this skill
        dest_dir = install_root / name
        if not dest_dir.exists():
            continue  # no bare copy installed — the clone's own copy is what runs
        # Mirror sync_skill_folder's overwrite skip guards EXACTLY: a symlinked or
        # independently-git-managed install is not one sync would touch, so it is
        # not drift we can (or should) act on.
        if _any_symlink(dest_dir) or (dest_dir / ".git").exists():
            continue
        dest = dest_dir / "SKILL.md"
        try:
            if dest.is_file():
                if filecmp.cmp(str(src), str(dest), shallow=False):
                    continue  # identical — in sync, no drift
                dest_text = dest.read_text(encoding="utf-8", errors="replace")
            else:
                dest_text = ""  # dir exists but SKILL.md absent -> fully behind
            src_text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # unreadable -> can't prove drift -> stay silent (fail open)

        src_headings = _headings(src_text)
        dest_headings = _headings(dest_text)
        clone_norm = {h.lower() for h in src_headings}
        bare_norm = {h.lower() for h in dest_headings}
        missing = _dedup([h for h in src_headings if h.lower() not in bare_norm])
        extra = _dedup([h for h in dest_headings if h.lower() not in clone_norm])

        if missing and not extra:
            status = "behind"
        elif extra and not missing:
            status = "leads"
        elif missing and extra:
            status = "diverged"
        else:
            status = "content"  # same heading set, body changed
        results.append({
            "name": name,
            "status": status,
            "missing_sections": missing,
            "extra_sections": extra,
        })
    return results


def drift_message(drifts: list[dict]) -> str | None:
    """Human-facing SessionStart report for skill-content drift, or None when
    everything matches the canonical checkout. Canonical (`ai-brain-starter`) is
    the source of the BEST version; every copy should converge to it and every
    improvement should flow UP into it, so ALL clients — including paid ones —
    run the best version. So this reports drift in BOTH directions, with the
    correct (opposite) action for each:

      behind / content -> the copy is OLDER than canonical: apply canonical
                          (sync-skills.py, backup-first).
      leads            -> the copy is AHEAD of canonical: an improvement is
                          trapped locally where clients can't get it. Upstream
                          it (PR to ai-brain-starter) — or, if it is a personal
                          customization, move it into a config file. Do NOT sync
                          canonical down over it; that would delete the newer work.
      diverged         -> both moved: reconcile by hand (upstream the local part,
                          THEN apply canonical), never a blind overwrite.
    """
    behind = sorted([d for d in drifts if d["status"] in ("behind", "content")],
                    key=lambda d: d["name"])
    leads = sorted([d for d in drifts if d["status"] == "leads"], key=lambda d: d["name"])
    diverged = sorted([d for d in drifts if d["status"] == "diverged"], key=lambda d: d["name"])
    if not (behind or leads or diverged):
        return None
    py = "py -3" if os.name == "nt" else "python3"
    fix = f"{py} ~/.claude/skills/ai-brain-starter/scripts/sync-skills.py"

    lines = ["[ai-brain-starter skill-content check]", ""]

    if behind:
        n = len(behind)
        s = "s" if n != 1 else ""
        lines += [
            (f"{n} installed skill{s} {'are' if n != 1 else 'is'} running an OLDER "
             f"version than the canonical AI Brain Starter copy on this machine — the "
             f"latest improvements haven't reached the skill{s} you actually use yet. "
             f"Apply them (your copy is backed up to <file>.bak-YYYY-MM-DD-HHMM first):"),
            "", "```", fix, "```", "", "Behind canonical:",
        ]
        for d in behind:
            if d["status"] == "content":
                lines.append(f"- `{d['name']}` — updated content (same sections, newer text)")
            else:
                secs = ", ".join(d["missing_sections"][:6]) or "(body changes)"
                lines.append(f"- `{d['name']}` — missing new section(s): {secs}")

    if leads:
        n = len(leads)
        s = "s" if n != 1 else ""
        if behind:
            lines.append("")
        lines += [
            (f"{n} installed skill{s} {'are' if n != 1 else 'is'} AHEAD of canonical — "
             f"an improvement is sitting in your local copy where clients (including paid "
             f"ones) can't get it. Get it to everyone: if it is a general improvement, PR "
             f"it into ai-brain-starter so canonical becomes the best version; if it is a "
             f"personal customization, move it into a config file so the skill stays "
             f"canonical. Do NOT run sync-skills.py on {'these' if n != 1 else 'this'} — "
             f"it would overwrite the newer local version with the older canonical one."),
            "", "Ahead of canonical (upstream, don't sync down):",
        ]
        for d in leads:
            secs = ", ".join(d["extra_sections"][:6]) or "(body changes)"
            lines.append(f"- `{d['name']}` — local-only content: {secs}")

    if diverged:
        n = len(diverged)
        s = "s" if n != 1 else ""
        if behind or leads:
            lines.append("")
        lines += [
            (f"{n} installed skill{s} {'have' if n != 1 else 'has'} DIVERGED (both "
             f"canonical AND the local copy changed) — reconcile by hand: upstream the "
             f"local part, then apply canonical. Never a blind overwrite in either "
             f"direction."),
            "", "Diverged (reconcile):",
        ]
        for d in diverged:
            up = ", ".join(d["missing_sections"][:4]) or "(body)"
            loc = ", ".join(d["extra_sections"][:4]) or "(body)"
            lines.append(f"- `{d['name']}` — new upstream: {up}; local-only: {loc}")

    lines += [
        "",
        ("Explain this to the user in one friendly sentence and offer to act — routine "
         "maintenance, not an error. 'Behind' = one command to apply. 'Ahead' = an "
         "improvement to upstream so every client gets it (offer to open the PR); never "
         "sync it down. 'Diverged' = show the diff and reconcile."),
    ]
    return "\n".join(lines)


def check_mode() -> int:
    """`--check`: report skill-content drift, change NOTHING, always exit 0.
    The read-only path the SessionStart surfacer and CI can call safely."""
    drifts = classify_drift(_starter_dir() / "skills", _install_dir())
    msg = drift_message(drifts)
    print(msg if msg else
          "skill-content check: all installed skill copies are in sync with the checkout.")
    return 0


def main() -> int:
    starter = _starter_dir()
    install = _install_dir()
    stamp = time.strftime("%Y-%m-%d-%H%M")
    if not starter.is_dir():
        print(f"ERROR: ai-brain-starter repo not found at {starter}", file=sys.stderr)
        return 1
    try:
        install.mkdir(parents=True, exist_ok=True)
    except OSError:
        print(f"ERROR: could not create install dir {install}", file=sys.stderr)
        return 1

    r = SyncReport()
    ignore_patterns = load_driftignore(starter)
    skills_root = starter / "skills"
    if skills_root.is_dir():
        for skill_dir in sorted(skills_root.iterdir()):
            if not skill_dir.is_dir():
                continue
            if is_ignored(skill_dir.name, ignore_patterns):
                r.skipped.append(f"{skill_dir.name}: matched .driftignore (declared personal)")
                continue
            sync_skill_folder(skill_dir, install / skill_dir.name,
                              skill_dir.name, stamp, r)

    lines = [
        f"=== sync-skills run at {stamp} ===",
        f"Created: {len(r.created)} file(s)",
        *[f"  + {f}" for f in r.created],
        f"Updated: {len(r.updated)} file(s)",
        *[f"  ~ {f}" for f in r.updated],
        f"Backed up: {len(r.backed_up)} file(s) (local customizations preserved)",
        *[f"  b {f}" for f in r.backed_up],
        f"Skipped: {len(r.skipped)} skill(s)",
        *[f"  s {f}" for f in r.skipped],
        f"Errors: {len(r.errors)}",
        *[f"  ! {f}" for f in r.errors],
        "",
    ]
    summary = "\n".join(lines)
    print(summary)
    try:
        with (starter / ".sync.log").open("a", encoding="utf-8") as fh:
            fh.write(summary + "\n")
    except OSError:
        pass

    # --- Also refresh the vault's own <meta>/scripts/ (the skill->vault half) ---
    # sync-skills syncs skill -> ~/.claude/skills; sync-vault-scripts.sh is the
    # other half that previously went stale because <meta>/scripts/ was only ever
    # populated at setup. It self-resolves the vault (--vault / $VAULT_ROOT /
    # settings.json) and is a non-fatal no-op when none is set up. Best-effort by
    # design: a vault-side hiccup (or no bash, e.g. native Windows) must NEVER
    # flip this script's exit code, so failures are swallowed.
    vault_sync = starter / "scripts" / "sync-vault-scripts.sh"
    if vault_sync.is_file():
        try:
            proc = subprocess.run(
                ["bash", str(vault_sync), "--quiet"],
                capture_output=True,
                # UTF-8 explicitly, never the console code page: this child
                # prints the vault path, and every vault path contains "⚙️ Meta".
                # Under `text=True` alone the decode uses cp1252 on a Spanish or
                # French Windows (and ASCII in a C-locale pipe), where U+FE0F's
                # 0x8F byte is unmapped -> UnicodeDecodeError, and the whole
                # vault-script sync is lost to an exception this block swallows.
                text=True, encoding="utf-8", errors="replace",
            )
            for line in (proc.stdout or "").splitlines():
                print(f"[vault-scripts] {line}")
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
            pass  # bash missing (Windows) or spawn failure — non-fatal by design

    return 2 if r.errors else 0


USAGE = """usage: sync-skills.py [--check | --help]

Propagate skill updates from the ai-brain-starter checkout into the user's
installed ~/.claude/skills/ directory.

  (no arguments)  Perform the sync. WRITES: every installed skill file that
                  differs from the checkout is overwritten, after being backed
                  up to <file>.bak-YYYY-MM-DD-HHMM.
  --check         Read-only drift report. Makes no changes. Use this first.
  -h, --help      Show this message.

Exit codes: 0 ok / clean, 1 drift found (--check), 2 errors or bad arguments."""


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    # --check is the read-only drift report (MYC-3076); default is the sync.
    #
    # Guard the argument surface. This dispatch used to be a bare
    # `"--check" in sys.argv[1:]`, so EVERY other argument -- including the
    # reflexive `--help` -- fell through to the mutating sync. Reaching for
    # `--help` to find out whether a dry-run exists would rewrite every
    # installed skill instead of answering the question. A tool that mutates
    # by default must not treat an unrecognized flag as consent.
    _args = sys.argv[1:]
    if _args in ([], ["--check"]):
        sys.exit(check_mode() if _args == ["--check"] else main())
    if _args in (["-h"], ["--help"]):
        print(USAGE)
        sys.exit(0)
    print(f"sync-skills: unrecognized argument(s): {' '.join(_args)}\n", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    sys.exit(2)
