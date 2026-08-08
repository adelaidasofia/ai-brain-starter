#!/usr/bin/env python3
"""SessionStart hook: surface when the DEPLOYED ai-brain-starter hook set in
~/.claude/settings.json has fallen BEHIND the COMMITTED hooks.json in the
checkout (MYC-2507). The client-facing twin of the maintainer-only
surface-deployed-hook-drift.py.

THE GAP THIS FILLS (bug class DEPLOY-FAILS-OPEN-SILENTLY-ON-CLIENT):
MYC-720 made the ai-brain-starter CHECKOUT reach every machine — the auto-update
(scripts/ai-brain-auto-update.sh) ff-pulls AND runs install-hooks-user-level.py
itself. But that deploy step is fail-OPEN: if it times out / errors / rolls back,
the updater emits a ONE-SHOT warning for that turn and moves on. The next ~6-day
cycle finds HEAD == origin/main, so it no-ops (no pull, no re-deploy) and the
warning is gone forever. Result: the checkout is current but ~/.claude/settings.json
is STALE — deployed hooks != committed hooks.json — the exact silent-drift class
MYC-720 fought, moved one level up. update-check.sh only checks CHECKOUT-behind
(HEAD..origin), never deployed==committed; the maintainer detector covers only
the maintainer's own private skills repo. So a paying client whose deploy silently failed has ZERO signal.

WHAT IT CHECKS (cheap, LOCAL only — no git, no network):
  committed = the ABS-owned hooks in this checkout's hooks.json
  deployed  = the ABS-owned hooks in ~/.claude/settings.json
  drift  ==  a committed hook is MISSING from settings.json on its event, OR a
             RETIRED hook is STILL wired there (the install should have removed it).
On drift: one line + the one-command fix. Healthy install: silent (neg-control).
A pinned install (~/.claude/.ai-brain-starter-pinned) is silent too — that file is
the auto-update system's advertised opt-out, and a pinned client never runs the
fail-open auto-deploy this hook watches, so nagging it would break the escape hatch.

WHY IMPORT, NOT COPY, THE OWNERSHIP LOGIC: which hooks are "ai-brain-starter's",
which are retired, and which depend on a vault is defined ONCE in
install-hooks-user-level.py (ABS_FINGERPRINTS / ABS_OWNED_BASENAMES / _is_retired /
_hook_depends_on_vault). A copied list here would rot the instant a hook is
added — the SAME deployed!=committed drift class this hook exists to catch. So we
import those predicates; a new hook is covered automatically once it is added to
the installer's ABS_* lists (which wiring it requires anyway).

SCOPE — NON-vault-dependent hooks only (for the MISSING direction): the auto-
update deploy runs install-hooks-user-level.py WITHOUT a vault path, which PRUNES
vault-content hooks (graph-context-hook.sh / session-end-hook.sh / write-hook.sh —
[VAULT_PATH] with no ~/.claude fallback); those are wired later by /setup-brain
with the real vault path, on a path this fail-open class never touches. Demanding
them here would false-fire on every no-vault / pre-setup client. So this covers
exactly the class the auto-update deploy can silently break, and stays quiet
whether or not a vault exists. Comparing by BASENAME (not full command string)
makes the diff invariant under [VAULT_PATH] substitution — no false positives from
resolved-vs-placeholder paths.

ALSO SURFACES SKILL-CONTENT DRIFT (MYC-3076): the same fail-open-silent class,
one artifact over. sync-skills.py copies updated skill CONTENT into the bare
~/.claude/skills/<name> copies that actually serve a skill — but only inside the
auto-update's `head != origin` branch, so once the clone is current, a lagging
bare copy never catches up and nothing says so (the 2026-07-14 daily-journal +
insights movement mechanics reached the clone but not /journal + /weekly). We
call sync-skills.classify_drift() (its own source of truth, reusing its symlink
/ .git-fork skip guards) and append an upstream-ahead report to the SAME update-
check message — no new SessionStart hook, so the footprint SLA (MYC-2348) is
untouched. Directional: a copy that LEADS upstream is never nagged as behind.

LIMITATION (honest): a detector can only fire once it is itself deployed, so it
cannot report its own first-time non-deployment. After the first successful
deploy it self-perpetuates and catches every subsequent stale deploy. Checkout-
behind (HEAD..origin) is a sibling concern covered by update-check.sh and the
maintainer detector's substrate-checkout section — deliberately NOT re-done here.

Stdlib-only (json/os/sys/importlib/pathlib). Any error -> emit `{}` (fail-open: a
SessionStart surfacer must never crash session start). Hermetically testable via
ABS_SKILL_DIR (committed hooks.json source) + HOME/ABS_SETTINGS_JSON (deployed
settings.json). Test + negative control: tests/integration/test_deployed_hooks_behind.sh.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

MAX_LISTED = 12
_PY = "py -3" if os.name == "nt" else "python3"
FIX_CMD = (
    f"{_PY} ~/.claude/skills/ai-brain-starter/scripts/"
    "install-hooks-user-level.py --quiet --fail-on-missing"
)


def _emit(message: str | None = None) -> None:
    """SessionStart additionalContext (reaches the model) or `{}` (silent)."""
    if message:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": message,
            }
        }))
    else:
        print(json.dumps({}))
    sys.exit(0)


def _skill_dir() -> Path:
    """The ai-brain-starter checkout this hook ships in — source of the COMMITTED
    hooks.json. ABS_SKILL_DIR overrides (hermetic tests); default is resolved from
    this file's location (hooks/<me>.py -> the checkout root)."""
    env = os.environ.get("ABS_SKILL_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def _load_module(basename: str, mod_name: str):
    """Import a script by filename (hyphenated -> importlib) from the checkout so
    this hook shares ONE source of truth with it. Returns the module or None."""
    candidates = [
        Path(__file__).resolve().parent.parent / "scripts" / basename,
        _skill_dir() / "scripts" / basename,
        Path.home() / ".claude" / "skills" / "ai-brain-starter" / "scripts" / basename,
    ]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        except Exception:
            continue
    return None


def _skill_drift_message() -> str | None:
    """MYC-3076: surface bare `~/.claude/skills/<name>` copies whose SKILL.md has
    fallen behind the clone's bundled copy. Uses sync-skills.classify_drift (the
    skill-model source of truth) so it reports exactly what a sync would touch.
    Honors the same pin escape-hatch; any error -> None (fail open)."""
    settings_path = Path(
        os.environ.get("ABS_SETTINGS_JSON")
        or (Path.home() / ".claude" / "settings.json")
    )
    if (settings_path.parent / ".ai-brain-starter-pinned").exists():
        return None
    ss = _load_module("sync-skills.py", "_abs_sync_skills")
    if ss is None:
        return None
    try:
        # Clone skills root = this checkout's own skills/ (ABS_SKILL_DIR-aware, so
        # tests stay hermetic). Install root = the bare copies, override-aware.
        clone_skills = _skill_dir() / "skills"
        install_root = Path(
            os.environ.get("ABS_SYNC_INSTALL_DIR")
            or (Path.home() / ".claude" / "skills")
        )
        return ss.drift_message(ss.classify_drift(clone_skills, install_root))
    except Exception:
        return None


def _load_installer():
    """Import the ownership predicates + retired lists from
    install-hooks-user-level.py so this hook shares ONE source of truth with the
    deploy step. Returns the module, or None (caller fails open)."""
    candidates = [
        Path(__file__).resolve().parent.parent / "scripts" / "install-hooks-user-level.py",
        _skill_dir() / "scripts" / "install-hooks-user-level.py",
        Path.home() / ".claude" / "skills" / "ai-brain-starter" / "scripts" / "install-hooks-user-level.py",
    ]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            spec = importlib.util.spec_from_file_location("_abs_installer", path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        except Exception:
            continue
    return None


def _load_json(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _iter_commands(config: dict):
    """Yield (event, command) for every hook command in a settings/hooks config,
    defensive against missing keys and non-list shapes."""
    for event, groups in (config.get("hooks") or {}).items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for hook in (group.get("hooks") or []):
                if not isinstance(hook, dict):
                    continue
                cmd = hook.get("command", "")
                if cmd:
                    yield event, cmd


def _committed_by_event(template: dict, owned_basenames, depends_on_vault) -> dict[str, set[str]]:
    """event -> set of ABS-owned, NON-vault-dependent basenames the checkout ships.
    Vault-dependent hooks are excluded (the auto-update deploy never wires them;
    /setup-brain does, on a different path)."""
    out: dict[str, set[str]] = {}
    for event, cmd in _iter_commands(template):
        if depends_on_vault(cmd):
            continue
        bns = owned_basenames(cmd)
        if bns:
            out.setdefault(event, set()).update(bns)
    return out


def _deployed_by_event(settings: dict, owned_basenames) -> dict[str, set[str]]:
    """event -> set of ABS-owned basenames wired in settings.json (vault or not —
    membership only; the missing-direction diff subtracts these)."""
    out: dict[str, set[str]] = {}
    for event, cmd in _iter_commands(settings):
        bns = owned_basenames(cmd)
        if bns:
            out.setdefault(event, set()).update(bns)
    return out


def _retired_present(settings: dict, is_retired, retired_basenames, retired_fingerprints) -> list[str]:
    """Human labels for RETIRED hooks still wired in settings.json (the deploy
    should have un-wired them). Sorted, de-duplicated."""
    labels: set[str] = set()
    for _event, cmd in _iter_commands(settings):
        if not is_retired(cmd):
            continue
        # Prefer a concrete retired basename; fall back to the fingerprint substring.
        matched = [bn for bn in retired_basenames if bn in cmd]
        if not matched:
            matched = [fp for fp in retired_fingerprints if fp in cmd]
        labels.add(matched[0] if matched else (cmd[:60] + "..."))
    return sorted(labels)


def _build_message(missing: dict[str, set[str]], retired: list[str]) -> str | None:
    if not missing and not retired:
        return None

    # Flatten missing to (basename, [events]) for a stable, de-duplicated listing.
    events_by_bn: dict[str, list[str]] = {}
    for event, bns in missing.items():
        for bn in bns:
            events_by_bn.setdefault(bn, []).append(event)
    missing_bns = sorted(events_by_bn)

    n = len(missing_bns) + len(retired)
    plural = "s" if n != 1 else ""
    lines = [
        "[ai-brain-starter update check]",
        "",
        (
            f"Housekeeping, nothing is broken: {n} background helper{plural} "
            f"from the latest AI Brain Starter update {'are' if n != 1 else 'is'} "
            f"not active yet on this machine (the automatic setup step didn't "
            f"finish last time). Everything else works normally. To finish the "
            f"update, run this one command:"
        ),
        "",
        "```",
        FIX_CMD,
        "```",
        "",
        (
            "Explain this to the user in one friendly sentence and offer to run "
            "the command for them — do not alarm them; this is routine "
            "maintenance, not an error."
        ),
    ]

    if missing_bns:
        m = len(missing_bns)
        plural_m = "s" if m != 1 else ""
        lines += [
            "",
            f"Updated but not yet active ({m} helper{plural_m}):",
        ]
        for bn in missing_bns[:MAX_LISTED]:
            evs = ", ".join(sorted(set(events_by_bn[bn])))
            lines.append(f"- `{bn}` (event: {evs})")
        if m > MAX_LISTED:
            lines.append(f"- ... and {m - MAX_LISTED} more")

    if retired:
        r = len(retired)
        plural_r = "s" if r != 1 else ""
        lines += [
            "",
            f"No longer shipped but still active ({r} helper{plural_r} — the "
            f"command above also removes {'them' if r != 1 else 'it'}):",
        ]
        for label in retired[:MAX_LISTED]:
            lines.append(f"- `{label}`")
        if r > MAX_LISTED:
            lines.append(f"- ... and {r - MAX_LISTED} more")

    return "\n".join(lines)


def _drift_message() -> str | None:
    installer = _load_installer()
    if installer is None:
        return None  # can't determine ownership -> fail open

    try:
        owned_basenames = installer._owned_basenames
        is_retired = installer._is_retired
        depends_on_vault = installer._hook_depends_on_vault
        retired_basenames = installer.ABS_RETIRED_BASENAMES
        retired_fingerprints = installer.ABS_RETIRED_FINGERPRINTS
    except AttributeError:
        return None  # installer shape changed -> fail open rather than misfire

    committed_path = _skill_dir() / "hooks.json"
    settings_path = Path(
        os.environ.get("ABS_SETTINGS_JSON")
        or (Path.home() / ".claude" / "settings.json")
    )

    # Honor the escape hatch the auto-update machinery advertises: a pinned user
    # has disabled auto-update (ai-brain-auto-update.py no-ops on this file), so
    # they manage deploys by hand and the fail-open auto-deploy this hook watches
    # can't even run for them. Nagging them would contradict the opt-out (operating
    # rule: A Gate Must Honor the Escape Hatch It Advertises). Pin lives next to
    # settings.json (~/.claude/.ai-brain-starter-pinned), same base the updater uses.
    if (settings_path.parent / ".ai-brain-starter-pinned").exists():
        return None

    template = _load_json(committed_path)
    settings = _load_json(settings_path)
    # No committed source or no deployed target -> cannot prove drift -> silent.
    if template is None or settings is None:
        return None

    committed = _committed_by_event(template, owned_basenames, depends_on_vault)
    deployed = _deployed_by_event(settings, owned_basenames)

    missing: dict[str, set[str]] = {}
    for event, bns in committed.items():
        gap = bns - deployed.get(event, set())
        if gap:
            missing[event] = gap

    retired = _retired_present(settings, is_retired, retired_basenames, retired_fingerprints)

    return _build_message(missing, retired)


def _stale_pull_message() -> "str | None":
    """MYC-3175: surface a clone that has not SUCCESSFULLY pulled in a long time.

    The third instance of this file's class, one artifact further out. The
    updater stamps `.ai-brain-starter-last-successful-pull` only when it has
    confirmed the clone current with origin. `.ai-brain-starter-last-update`
    keeps moving on every ATTEMPT, so a permanently-blocked clone looks busy;
    only the gap between the two reveals it.

    Why not "behind origin": a clone that cannot fetch cannot learn it is
    behind, so the obvious signal is exactly the one the failure suppresses.
    Elapsed-time-since-success needs no network and cannot be under-reported.

    Silent unless the stamp EXISTS and is older than ABS_STALE_PULL_DAYS
    (default 21 — three update intervals, so one or two missed cycles never
    nag). A missing stamp is a pre-seed install, not a freeze. A pinned install
    opted out. Any error -> None (fail open)."""
    state = Path(
        os.environ.get("ABS_UPDATE_STATE_DIR")
        or (Path(os.environ.get("ABS_SETTINGS_JSON")).parent
            if os.environ.get("ABS_SETTINGS_JSON")
            else Path.home() / ".claude")
    )
    try:
        if (state / ".ai-brain-starter-pinned").exists():
            return None
        stamp = state / ".ai-brain-starter-last-successful-pull"
        if not stamp.is_file():
            return None  # never seeded yet; not evidence of a freeze
        days = float(os.environ.get("ABS_STALE_PULL_DAYS", "21"))
        age_days = (time.time() - stamp.stat().st_mtime) / 86400.0
        if age_days <= days:
            return None
        return (
            f"[ai-brain-starter] This copy has not successfully updated in "
            f"{int(age_days)} days. It still CHECKS for updates, so nothing looks "
            "broken — but every attempt has been failing, which means new fixes "
            "(including guard and security fixes) are not reaching this machine. "
            "Most often a crashed git left a lock behind, or the checkout has "
            "local edits blocking a fast-forward. Diagnose with: "
            "cd ~/.claude/skills/ai-brain-starter && git status && git pull --ff-only"
        )
    except Exception:
        return None


def main() -> None:
    try:
        json.load(sys.stdin)
    except Exception:
        pass
    # Three fail-open checks under one "update check" surface: deployed hooks
    # behind (this file's original job) + skill content behind (MYC-3076) + the
    # clone itself no longer pulling (MYC-3175). Emit whichever fired, separated;
    # silent if none. Same hook, so SessionStart fan-out stays flat (MYC-2348).
    parts = [m for m in (_drift_message(), _skill_drift_message(),
                         _stale_pull_message()) if m]
    _emit("\n\n".join(parts) if parts else None)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    try:
        main()
    except Exception:
        # Absolute backstop: a SessionStart surfacer must never crash the session.
        print(json.dumps({}))
        sys.exit(0)
