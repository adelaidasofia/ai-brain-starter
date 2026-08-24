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
         /setup-brain. For hooks that are genuinely conditional — installed
         only after a user decision this installer cannot make for them.

    Neither route = the hook cannot fire. Both routes = the phase doc's
    copy-if-the-user-opts-in is silently pre-empted by the installer, so the
    documented choice is a lie. Both are failures.

    Route 2 is a WEAK route and route 1 is preferred for anything installed by
    default. A `cp` in a markdown file is POSIX-only (it cannot run on native
    Windows at all) and model-executed (it happens if an agent reads the doc
    and chooses to). retry-budget.py, validate-mcp-json.py and vault-context.py
    took route 2 until 2026-08-13, when a Windows install turned up with all
    three absent; they are installer-deployed now.

    THE THIRD INVARIANT (2026-08-13)
    A WIRED hook taking route 2 carries an explicit
    `<!-- weak-route-ok: <name> <reason> -->` in the phase doc that copies it.
    Route 2 is POSIX-only and model-executed, so choosing it has a real Windows
    cost; this makes choosing it a decision somebody wrote down rather than a
    default nobody noticed. Unwired opt-in `cp` steps are documentation and are
    out of scope. See _undeclared_weak_routes().

    THE SECOND INVARIANT (2026-08-13)
    Every `_lib.<mod>` imported by an installer-deployed hook is in
    HOME_HOOKS_LIB_DEPS, so the dependency ships with its consumer. These hooks
    catch the ImportError and fall back to a no-op, which means a missing _lib
    produces a hook that runs, exits 0, and silently does nothing — the same
    invisible failure, one layer down. See _lib_dependency_violations().

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

import ast
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


def _load_installer_manifest() -> tuple[set[str], set[str]]:
    """(HOME_HOOKS_INSTALLER_DEPLOYS, HOME_HOOKS_LIB_DEPS), from the installer.

    Imported rather than duplicated: a second hand-maintained copy of the list
    is exactly the drift this lint exists to prevent.
    """
    spec = importlib.util.spec_from_file_location("_abs_installer", INSTALLER)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {INSTALLER}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # HOME_HOOKS_LIB_DEPS via getattr, and an absent one is EMPTY rather than an
    # error: an installer that predates the constant is a valid installer, and
    # scripts/test_windows_install_regressions.py deliberately synthesizes a
    # one-constant installer to exercise this guard in isolation. Crashing there
    # with an AttributeError traceback would be a worse failure mode than the
    # clean violation this lint exists to print.
    #
    # Empty is NOT a bypass. It cannot pass on a repo that actually needs the
    # manifest: _lib_dependency_violations() checks each import against this
    # set, so an empty one turns EVERY `_lib.<mod>` in a deployed hook into a
    # named violation. Deleting the constant from the real installer therefore
    # reddens this gate loudly, which is the point.
    return (set(mod.HOME_HOOKS_INSTALLER_DEPLOYS),
            set(getattr(mod, "HOME_HOOKS_LIB_DEPS", ())))


def _imported_lib_modules(body: str) -> set[str]:
    """Every `_lib` submodule a hook's source imports, in any spelling.

    Parsed with `ast`, not matched with a regex. Both spellings are in use in
    this repo's hooks/ — `from _lib.<mod> import x`, `import _lib.<mod>`, and
    `from _lib import <mod>` — so a guard that knows only one passes on the
    other. A regex covering both then over-matches: hooks/ already contains the
    line

        f"from _lib.secret_patterns import redact; from pathlib import Path; "

    inside a string, and a bare `from _lib import x` in a docstring sits at
    column 0 like real code. A false violation blocks CI on a correct repo,
    which is worse than the gap being closed. The AST sees imports and nothing
    that merely looks like one, and it handles parenthesized multi-line and
    aliased forms for free.

    A file that does not parse yields nothing: gate (a) py_compiles every
    tracked *.py and owns that failure, and a second, worse-worded report of it
    here would only muddy attribution.
    """
    try:
        tree = ast.parse(body)
    except (SyntaxError, ValueError):
        return set()
    mods: set[str] = set()
    for node in ast.walk(tree):
        # from _lib import a, b   /   from _lib.mod import x
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "_lib":
                mods.update(alias.name for alias in node.names)
            elif node.module.startswith("_lib."):
                mods.add(node.module.split(".")[1])
        # import _lib.mod  /  import _lib.mod as m
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("_lib."):
                    mods.add(alias.name.split(".")[1])
    return mods


def _lib_dependency_violations(deployed: set[str], lib_deps: set[str]) -> list[str]:
    """Every `_lib.<mod>` a DEPLOYED hook imports must ship with it.

    Second half of the same bug. A hook copied to ~/.claude/hooks/ without the
    package it imports does NOT crash: these hooks wrap the import in a
    try/except with a no-op fallback (correct — a context injector must never
    break a prompt), so the hook runs, exits 0, and does nothing. From the
    outside that is byte-identical to a hook with nothing to say.

    vault-context.py shipped exactly that way on EVERY platform: the phase doc
    copied the .py, nothing ever copied hooks/_lib/, and `vault_root_for` fell
    back to the stub returning None — no vault resolved, nothing injected, no
    error, forever.
    """
    problems: list[str] = []
    for name in sorted(deployed):
        src = HOOKS_DIR / name
        if not src.is_file():
            continue  # absence is already reported by the caller
        try:
            body = src.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for mod in sorted(_imported_lib_modules(body)):
            if f"{mod}.py" not in lib_deps:
                problems.append(
                    f"{name}: imports _lib.{mod}, but '{mod}.py' is not in "
                    "HOME_HOOKS_LIB_DEPS in scripts/install-hooks-user-level.py "
                    "— the hook deploys and its dependency does not. The import "
                    "fails into a silent no-op fallback, so the hook reports "
                    "healthy and never does anything."
                )
            elif not (HOOKS_DIR / "_lib" / f"{mod}.py").is_file():
                problems.append(
                    f"{name}: imports _lib.{mod}, but hooks/_lib/{mod}.py is not "
                    "in this repo — nothing can deploy a file that does not exist."
                )
    if lib_deps and "__init__.py" not in lib_deps:
        problems.append(
            "HOME_HOOKS_LIB_DEPS is missing '__init__.py' — without it the "
            "deployed _lib/ is not an importable package, so every "
            "`from _lib.X import ...` fails into its silent fallback."
        )
    return problems


def _referenced_home_hooks() -> set[str]:
    blob = HOOKS_JSON.read_text(encoding="utf-8")
    return set(_HOME_HOOK_RE.findall(blob))


# A phase doc that deliberately owns a copy step declares it, once, anywhere in
# the file. Naming the hook is required so the marker cannot be a blanket
# permission slip for whatever gets added later.
_WEAK_ROUTE_OK_RE = re.compile(r"<!--\s*weak-route-ok:\s*([\w.-]+\.(?:py|sh))\s+(.+?)-->", re.S)


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


def _weak_route_declarations() -> dict[str, str]:
    """basename -> the reason its phase doc gives for owning the copy."""
    declared: dict[str, str] = {}
    if not PHASES_DIR.is_dir():
        return declared
    for doc in sorted(PHASES_DIR.glob("*.md")):
        for name, reason in _WEAK_ROUTE_OK_RE.findall(doc.read_text(encoding="utf-8")):
            declared.setdefault(name, " ".join(reason.split()))
    return declared


def _undeclared_weak_routes(phase_copies: dict[str, str],
                            referenced: set[str]) -> list[str]:
    """The phase-doc route is WEAK. Taking it must be a declared choice.

    THE BUG THIS CLOSES, which is the one this whole lint already exists for,
    arriving one level up. On 2026-08-13 retry-budget.py, validate-mcp-json.py
    and vault-context.py turned up absent from a real Windows install. All three
    were correctly wired, and all three had a deploy route this lint accepted:
    three `cp` lines in phases/phase-05-context-layer.md. The lint was GREEN the
    entire time, because it asked "is there a route?" and the honest question is
    "is there a route that can actually run?"

    A phase-doc `cp` cannot run on native Windows (`cp` is not a command there),
    and it only runs at all if an agent reads the doc and chooses to. Paired
    with the `[ -f ]` guard on every one of these commands, a hook that never
    landed is indistinguishable from a hook the user switched off. Three of them
    were dead for months and nothing anywhere said so.

    So route 2 stays available — a genuinely conditional hook may need it — but
    it is no longer the kind of thing that can happen by accident. Declare it:

        <!-- weak-route-ok: some-hook.py the installer cannot own this because
             <reason>; on Windows it is <what happens / why that is acceptable> -->

    Nothing declares it today, because no WIRED hook uses route 2 today. The
    marker costs nothing until someone reaches for the weak route, which is
    exactly when a human should be looking.

    SCOPED TO WIRED HOOKS, deliberately. phases/ also carries `cp` steps for
    conditional opt-ins (permission-denied.py and friends) that hooks.json does
    NOT wire — the reader installs those by hand AND adds their own settings
    entry. Those are documentation, not a deploy route this lint governs, and
    flagging them was this check's own first false positive: caught by its
    controls before it shipped, which is the whole reason to run controls on a
    guard you just wrote.
    """
    declared = _weak_route_declarations()
    problems: list[str] = []
    for name, doc in sorted(phase_copies.items()):
        if name not in referenced:
            continue  # not wired by hooks.json -> outside this lint's contract
        if name not in declared:
            problems.append(
                f"{name}: deployed by a `cp` step in {doc} with no "
                f"`<!-- weak-route-ok: {name} <reason> -->` declaration. The "
                "phase-doc route is POSIX-only (it cannot run on native "
                "Windows) and model-executed (it runs only if an agent reads "
                "the doc and chooses to), and the `[ -f ]` guard turns both "
                "failures into silence. Prefer HOME_HOOKS_INSTALLER_DEPLOYS in "
                "scripts/install-hooks-user-level.py. If this hook genuinely "
                "cannot be installer-owned, declare why — including what "
                "Windows users get instead."
            )
    return problems


def main() -> int:
    if not HOOKS_JSON.is_file():
        print(f"::error::{HOOKS_JSON} not found", file=sys.stderr)
        return 1
    # Parse-check so a malformed template fails here rather than mid-install.
    json.loads(HOOKS_JSON.read_text(encoding="utf-8"))

    referenced = _referenced_home_hooks()
    installer_deploys, lib_deps = _load_installer_manifest()
    phase_copies = _phase_doc_copies()

    violations: list[str] = _lib_dependency_violations(installer_deploys, lib_deps)
    violations += _undeclared_weak_routes(phase_copies, referenced)

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
        f"{len(referenced & set(phase_copies))} phase doc); "
        f"{len(lib_deps)} _lib dependency file(s) ship with them."
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
