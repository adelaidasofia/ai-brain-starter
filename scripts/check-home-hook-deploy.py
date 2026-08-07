#!/usr/bin/env python3
"""check-home-hook-deploy.py — every hook wired from ~/.claude/hooks/ must have
a route that actually puts it there.

THE BUG THIS CLOSES
    hooks.json invokes some hooks by their deployed home path:

        [ -f ~/.claude/hooks/pre-write-settings-lint.py ] && [PYTHON] ~/.claude/hooks/pre-write-settings-lint.py || true

    That `[ -f ]` guard is correct at RUNTIME — a user who deletes an optional
    hook should get silence, not an error on every prompt. But it also means a
    hook nobody ever COPIED to ~/.claude/hooks/ is indistinguishable from a hook
    the user turned off. It never fires, and nothing anywhere says so.

    pre-write-settings-lint.py, lint-claude-settings.py and
    check-claude-code-version.sh shipped that way: referenced by hooks.json,
    present in the repo's hooks/, copied by nothing — not the installer, not any
    phase doc. On a real install that is 11 wired references and 0 files on
    disk. The installer's own verification reported the install clean, because
    verify_paths_on_disk() only inspects ABS-owned commands and these were not
    owned. Shipped, wired, verified — and never once executed.

THE INVARIANT
    Every ~/.claude/hooks/<name> reference in hooks.json is covered by EXACTLY
    ONE of two deploy routes:

      1. INSTALLER — <name> is in install-hooks-user-level.py's
         HOME_HOOKS_INSTALLER_DEPLOYS, which copies it on every install.
         For substrate hooks that should be on every machine unconditionally.

      2. PHASE DOC — some phases/*.md carries a literal
         `cp .../hooks/<name> ~/.claude/hooks/` step, executed during
         /setup-brain. For hooks that are conditional, vault-dependent, or
         opt-in after a user decision (retry-budget.py, validate-mcp-json.py,
         vault-context.py).

    Neither route = the hook cannot fire. Both routes = the phase doc's
    copy-if-the-user-opts-in is silently pre-empted by the installer, so the
    documented choice is a lie. Both are failures.

    The reverse direction is checked too: an entry in
    HOME_HOOKS_INSTALLER_DEPLOYS that hooks.json no longer references, or that
    the repo no longer ships, is dead weight that will rot.

WHY A LINT AND NOT A TEST
    The failure is invisible by construction — the guard makes the missing file
    look like a healthy install, so no runtime assertion can catch it. It has to
    be caught where the reference is introduced. Same family as
    scripts/check-utf8-stdout.py (#313) and scripts/check-vault-root-reads.py
    (#375/#404): the SILENT-NO-OP class, caught statically.

USAGE
    python3 scripts/check-home-hook-deploy.py     # exit 1 on any violation

Stdlib only. No network, no git, no writes.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS_JSON = ROOT / "hooks.json"
HOOKS_DIR = ROOT / "hooks"
PHASES_DIR = ROOT / "phases"
INSTALLER = ROOT / "scripts" / "install-hooks-user-level.py"

# `~/.claude/hooks/<name>.py|sh` anywhere in a hook command.
_HOME_HOOK_RE = re.compile(r"~/\.claude/hooks/([\w.-]+\.(?:py|sh))")


def _load_installer_manifest() -> set[str]:
    """HOME_HOOKS_INSTALLER_DEPLOYS, read from the installer itself.

    Imported rather than duplicated: a second hand-maintained copy of the list
    is exactly the drift this lint exists to prevent.
    """
    spec = importlib.util.spec_from_file_location("_abs_installer", INSTALLER)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {INSTALLER}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return set(mod.HOME_HOOKS_INSTALLER_DEPLOYS)


def _referenced_home_hooks() -> set[str]:
    blob = HOOKS_JSON.read_text(encoding="utf-8")
    return set(_HOME_HOOK_RE.findall(blob))


def _phase_doc_copies() -> dict[str, str]:
    """basename -> phase doc that copies it into ~/.claude/hooks/.

    Matches a literal `cp <...>/hooks/<name> <...>~/.claude/hooks<...>` step, the
    shape every phase doc uses. Deliberately literal: a copy step the model has
    to infer is a copy step that will not happen the same way twice.
    """
    found: dict[str, str] = {}
    if not PHASES_DIR.is_dir():
        return found
    pattern = re.compile(
        r"^\s*cp\s+\S*hooks/([\w.-]+\.(?:py|sh))\s+\S*~/\.claude/hooks\S*\s*$",
        re.MULTILINE,
    )
    for doc in sorted(PHASES_DIR.glob("*.md")):
        for name in pattern.findall(doc.read_text(encoding="utf-8")):
            found.setdefault(name, doc.name)
    return found


def main() -> int:
    if not HOOKS_JSON.is_file():
        print(f"::error::{HOOKS_JSON} not found", file=sys.stderr)
        return 1
    # Parse-check so a malformed template fails here rather than mid-install.
    json.loads(HOOKS_JSON.read_text(encoding="utf-8"))

    referenced = _referenced_home_hooks()
    installer_deploys = _load_installer_manifest()
    phase_copies = _phase_doc_copies()

    violations: list[str] = []

    for name in sorted(referenced):
        by_installer = name in installer_deploys
        by_phase = name in phase_copies
        if by_installer and by_phase:
            violations.append(
                f"{name}: deployed by BOTH the installer and {phase_copies[name]}. "
                "The phase doc presents a choice the installer has already made. "
                "Pick one route."
            )
        elif not by_installer and not by_phase:
            violations.append(
                f"{name}: hooks.json wires ~/.claude/hooks/{name} but NOTHING "
                "deploys it there — not HOME_HOOKS_INSTALLER_DEPLOYS in "
                "scripts/install-hooks-user-level.py, and no `cp` step in "
                "phases/*.md. The [ -f ] guard makes the absence look healthy, "
                "so this hook silently never fires. Add it to one route."
            )
        if not (HOOKS_DIR / name).is_file():
            violations.append(
                f"{name}: hooks.json wires it, but hooks/{name} is not in this "
                "repo — nothing can deploy a file that does not exist."
            )

    # Dead manifest entries: promised by the installer, wanted by no one.
    for name in sorted(installer_deploys - referenced):
        violations.append(
            f"{name}: in HOME_HOOKS_INSTALLER_DEPLOYS but hooks.json no longer "
            "references ~/.claude/hooks/{0}. The installer copies a file no hook "
            "command runs — drop it from the manifest.".format(name)
        )

    if violations:
        print("::error::home-hook deploy gap — a wired hook that nothing installs "
              "is a hook that never fires:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    print(
        f"OK — {len(referenced)} ~/.claude/hooks/ reference(s) all have a deploy "
        f"route ({len(referenced & installer_deploys)} installer, "
        f"{len(referenced & set(phase_copies))} phase doc)."
    )
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print
    # (a hook name, a path) cannot crash the lint itself.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
