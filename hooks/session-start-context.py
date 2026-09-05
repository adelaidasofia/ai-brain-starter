#!/usr/bin/env python3
"""
Emit the SESSION START context as a JSON hookSpecificOutput payload.

Replaces a previous inline echo '{"hookSpecificOutput":...}' hook command
that triggered a known footgun on zsh: literal parentheses inside the
single-quoted JSON body were re-interpreted as subshell syntax in some
shell-runner contexts, blocking every UserPromptSubmit. Moving the JSON
into a Python script with json.dumps removes the shell-quoting surface
entirely.

Wired on SessionStart (NOT UserPromptSubmit): this is session-start guidance,
needed once. `once: true` is IGNORED in settings.json — the installer merges
this hooks.json into ~/.claude/settings.json, where a UPS `once` hook silently
re-fires EVERY message. Measured: this block + the instinct block re-injected
17x / 14x in one real session (MYC-2359). SessionStart fires once per
session-segment (startup / resume / post-compact), so the block lands in the
cached prefix and is served as cache-reads thereafter, not re-billed as fresh
tokens every turn.
"""
import json
import sys


CONTEXT = (
    "SESSION START: CLAUDE.md is already auto-loaded in your system prompt "
    "(do NOT re-read it). Read these two files: 1) Meta/Last Session.md "
    "2) Meta/Current Priorities.md.\n\n"
    "ALWAYS-ACTIVE RULES (apply every session, every message, regardless of "
    "work type):\n"
    "- Advisory panel: CLAUDE.md already contains the trigger rule. At ANY "
    "judgment moment, decisions, strategy, crises, trade-offs, client "
    "problems, cash flow, legal, fundraising, read advisory-panel.md and "
    "bring 3-5 voices BEFORE responding. Do not wait to be asked.\n"
    "- Efficiency rules (Meta/rules/efficiency.md): Contains 29+ rules "
    "including panel triggers, model routing, never-fabricate, humanizer, "
    "math and counting rules. Read on first session message.\n\n"
    "CONDITIONAL RULES (read when doing that type of work):\n"
    "- obsidian.md for vault edits\n"
    "- graphify.md for graph questions\n"
    "- tool-routing.md for task routing\n"
    "- meeting-workflow.md for meetings\n\n"
    "SESSION CLOSE: When the user says bye, done, thanks that's all, good "
    "night, ttyl, wrapping up, or equivalent in any language, the "
    "detect-closing-signal.py hook fires automatically and injects the "
    "full cascade with pre-resolved paths. Trust the injected context, "
    "don't re-read separate rule files.\n\n"
    "GRAPH ROUTING: If your vault has a knowledge graph, pick the right "
    "graph for the question scope BEFORE drilling into source files. Use "
    "/graphify query for targeted lookups instead of reading full reports. "
    "The keyword-triggered graph-context-hook.sh (if installed) will fire "
    "a second routing reminder with freshness info."
)


def _memory_index_warning() -> str:
    """Announce it when the memory index cannot be fully loaded.

    The loader is the honest place for this. Past a byte cliff the reader
    silently stops and entries below the cut are never seen; write-time tooling
    warns when the index grows, but a session that only READS it gets a
    truncated list and no signal. An index is proven by what LOADS, so the thing
    doing the loading announces what it could not load.

    Folded in here rather than shipped as its own SessionStart hook: that event
    is at its cold-start fan-out budget, and a housekeeping check does not earn
    a new subprocess on every session for the life of the install.

    Fail-open and cheap: any error yields no warning rather than a broken start.
    """
    try:
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "_lib"))
        import memory_index
        return memory_index.report()
    except Exception:
        return ""


def _umbrella_map() -> str:
    """The routing-umbrella map for THIS install, or "" if there is none.

    Folded in here rather than wired as its own SessionStart hook on purpose:
    footprint-budgets.json caps SessionStart fan-out at 19, the event measured
    exactly 19, and that budget's own rationale records the decision that the
    next addition should optimize fan-out rather than raise again. This hook
    already emits SessionStart context and already pays for the interpreter,
    so the map adds ZERO SessionStart fan-out (measured: footprint-sla-check
    --gate reports 19 / 19, exit 0, with this in the tree). Not free in wall
    clock, though: two bounded iterdir levels plus one capped read per skill.
    Fails open -- a missing map must never cost the session its start context.
    """
    try:
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "_lib"))
        import umbrella_map
        result = umbrella_map.render_umbrella_map()
        # isinstance, not truthiness: a non-str return would blow up the
        # concatenation in main() OUTSIDE this try, and the hook would exit 1
        # with no JSON at all -- the one outcome this function exists to avoid.
        return result if isinstance(result, str) else ""
    except BaseException:
        # BaseException, not Exception: a stray sys.exit() in an imported module
        # raises SystemExit, which `except Exception` does not catch. "Must
        # never cost the session its start context" has to mean every exit path.
        return ""



def _resolve_vault_paths(text: str) -> str:
    """Rewrite the relative `Meta/...` references into absolute vault paths.

    The CONTEXT block above names its files as `Meta/Last Session.md` etc.
    That path is only correct for a session whose cwd IS the vault AND whose
    Meta folder is literally named "Meta". Neither holds on a real install:
    Claude Code is commonly launched from $HOME, and the shipped vault
    template names the folder with an emoji prefix ("\u2699\ufe0f Meta"). The
    model was told to read two files that do not exist at the path given,
    got nothing, and started every session without its own priorities --
    silently, because a missing file reads as "nothing to report".

    Resolution goes through the SAME sanctioned per-target resolver every
    other consumer of the Meta-folder signature shares
    (hooks/_lib/vault_root.py:vault_root_for) instead of a second hand-rolled
    cwd walk-up plus a raw `os.environ.get("VAULT_ROOT")` read -- exactly the
    drift scripts/check-vault-root-reads.py exists to catch fleet-wide (a
    naive read is wrong whether VAULT_ROOT is unset, defaulting to a vault
    this install does not have, or set machine-wide, silently overriding the
    vault this session is actually in). vault_root_for already does cwd
    walk-up for a Meta-suffixed folder first, $VAULT_ROOT as the fallback,
    and collapses a `.claude/worktrees/<slug>/` checkout back to the real
    vault root either way, so a session started from inside one still
    resolves against the vault rather than the throwaway checkout.

    Fail-open -- any error, or no vault found at all, leaves the text exactly
    as it was. No path is ever fabricated: each of the three references is
    substituted only when its concrete target file exists on disk, so an
    absent file's literal `Meta/...` reference is left untouched rather than
    rewritten into a path that does not exist.
    """
    try:
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "_lib"))
        from vault_root import vault_root_for
        from pathlib import Path

        root = vault_root_for(Path.cwd())
        if root is None:
            return text
        meta = None
        for name in sorted(os.listdir(root)):
            candidate = os.path.join(root, name)
            if name.endswith("Meta") and os.path.isdir(candidate):
                meta = candidate
                break
        if meta is None:
            return text
        for rel in ("Last Session.md", "Current Priorities.md",
                    "rules/efficiency.md"):
            target = os.path.join(meta, *rel.split("/"))
            if os.path.isfile(target):
                text = text.replace("Meta/" + rel, target)
        return text
    except Exception:
        return text


def main() -> int:
    context = _resolve_vault_paths(CONTEXT)
    try:
        warning = _memory_index_warning()
    except Exception:
        warning = ""
    if warning:
        context = context + "\n\n" + warning
    try:
        umbrellas = _umbrella_map()
    except BaseException:
        umbrellas = ""
    if isinstance(umbrellas, str) and umbrellas:
        context = context + "\n\n" + umbrellas
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    sys.stdout.write(json.dumps(payload))
    return 0


if __name__ == "__main__":
    # UTF-8 console guard (ai-brain-starter#313 cp1252 crash class). This file
    # previously sat on utf8-stdout-baseline.txt as "SEV-4-json-encoded" -- safe
    # only because every write goes through json.dumps(), which escapes non-ASCII
    # by default. That reasoning is true today and is one raw print() away from
    # being false, and the payload now carries the umbrella map (em dashes and
    # arrows). Guarding is 5 lines; the baseline row is deleted rather than
    # re-pinned, which the checker explicitly asks for.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
