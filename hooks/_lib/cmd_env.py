"""Convenience wrapper: does `command` carry a leading `<var>=<value>` prefix?

WHY THIS EXISTS. A PreToolUse(Bash) gate runs in the HOOK process, whose
environment is the Claude Code SESSION env. An inline `VAR=1 <cmd>` prefix a
user or agent types lives ONLY in the command STRING and never reaches the
hook's `os.environ`. A gate that advertises an inline bypass in its own block
message (`STALE_CHECKOUT_BYPASS=1 ...`, `JOURNAL_CONTEXT_BYPASS=1 ...`) but
reads only `os.environ` is offering an escape hatch that can never fire --
which trains people to disable the guard some other way instead. Bug class
HOOK-READS-SESSION-ENV-NOT-COMMAND-ENV (env sibling of
HOOK-RESOLVES-SESSION-CWD-NOT-COMMAND-CWD).

NOT A NEW PARSER. The quote-aware segment split and leading-assignment walk
already live in `_lib/shell_parse.py` (`leading_env_assigns`), used elsewhere
in this repo for the same "does this shell command carry VAR=val" question.
This module adds nothing but the one-line convenience call two hooks already
expect at this import path (`from cmd_env import inline_bypass`, with a local
fallback if the import fails) -- shipping it makes that import resolve to the
real, shared implementation instead of silently falling back every time.

Call shape, matching the fallback both callers already carry:

    try:
        from cmd_env import inline_bypass
    except Exception:
        def inline_bypass(command, var, value="1"):
            return False
    ...
    if os.environ.get(VAR) == "1" or inline_bypass(command, VAR):
        allow()
"""
from __future__ import annotations

try:                                        # _lib itself is on sys.path
    from shell_parse import leading_env_assigns
except ImportError:                        # hooks/ (the parent) is on sys.path
    from _lib.shell_parse import leading_env_assigns


def inline_bypass(command: str, var: str, value: str = "1") -> bool:
    """True iff `command` carries a leading `<var>=<value>` assignment.

    `leading_env_assigns` already handles quoting, shell-segment splitting,
    wrapper prefixes (`env`, `sudo`, ...), and heredoc bodies -- see its
    docstring in shell_parse.py for the exact contract. This is just the
    single-variable convenience read two callers already expect to import.
    """
    return leading_env_assigns(command).get(var) == value
