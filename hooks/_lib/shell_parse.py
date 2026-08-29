"""shell_parse — quote-aware primitives for hooks that must reason about a
Bash command STRING before the shell runs it.

A PreToolUse(Bash) guard is handed one string and has to answer questions a
naive `re.split` cannot: which commands are actually in here, where would each
one run, and which of them carries the advertised bypass. Every hook that
hand-rolled that walk grew its own set of fail-opens, because all four of the
hard parts are invisible until they bite:

  - QUOTES. `echo "hi; cd /tmp" && git push` has ONE `cd`-looking token and it
    is inside a string. A splitter that ignores quotes forges a segment
    boundary there and reads a `cd` the shell never runs.
  - SEPARATORS. `cd X && cmd` runs cmd in X; `cd X || cmd` runs it in the OLD
    cwd, because the right side of `||` runs only when the left FAILED. A
    splitter that discards the operator cannot tell those apart, and every
    consumer read `cd /tmp || git push` as a push from /tmp.
  - HEREDOC BODIES. A heredoc body is data, never commands, but it is full of
    real operator characters. Truncating at the first `<<` instead (the older
    approach) threw away every command AFTER the heredoc, which is where the
    interesting one usually is.
  - `$VAR`. `W=/path; cd "$W" && git push` is the shape people actually write.
    Without expansion the target is the literal string `$W`, which resolves to
    no repo at all -- and a guard that reads "no repo" as "not my repo" fails
    open on precisely the command it exists to catch.

Shared primitive, per-caller policy: this module answers "what does the shell
see", never "should it be allowed". Callers keep their own scoping and
fail-open/fail-closed decisions, which differ by guard.
"""

from __future__ import annotations

import re
import shlex

__all__ = [
    "ASSIGN_RE",
    "ENV_ASSIGN_RE",
    "WRAPPER_PREFIXES",
    "expand_vars",
    "leading_env_assigns",
    "segment_bypass_flags",
    "split_segments_with_seps",
    "strip_heredoc_bodies",
    "strip_noncode",
    "tokens",
]

# Words that may sit in FRONT of a command without changing which program runs.
# `env` may additionally carry `VAR=VAL` arguments.
WRAPPER_PREFIXES = {"env", "command", "exec", "builtin", "nohup", "sudo", "time"}

ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

_VAR_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

# Strip body + closing delimiter; everything before `<<` (the actual command)
# is outside the match and survives.
_HEREDOC = re.compile(
    r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n"  # <<EOF + rest of line
    r".*?"                                                # body (non-greedy)
    r"\n[ \t]*\2[ \t]*(?=\n|$)",                          # closing delimiter line
    re.DOTALL,
)
# A herestring (`<<<`) is NOT a heredoc: no body, no closing delimiter. The
# distinction matters below, where a LEFTOVER `<<` means "unterminated heredoc,
# be conservative" -- treating `<<<` as unterminated would needlessly discard
# every command after a herestring.
_HEREDOC_OPEN = re.compile(r"(?<!<)<<(?!<)")


def expand_vars(value, variables):
    """Expand `$VAR` / `${VAR}` from assignments seen EARLIER in the same command.

    Only literal, already-seen assignments are expanded. An unknown name or a
    command substitution (`$(...)`, backticks) is left INTACT on purpose, so the
    caller can still SEE a `$` and know the target is unresolved rather than
    silently receiving a wrong path. That distinction is the whole value: a
    guard can then treat "unresolved" as ambiguous instead of as "not mine".
    """
    if not value or "$" not in value:
        return value
    out = value
    for _ in range(5):  # bounded: resolves VAR=$OTHER chains, never loops forever
        nxt = _VAR_REF.sub(
            lambda m: variables.get(m.group(1) or m.group(2), m.group(0)), out)
        if nxt == out:
            break
        out = nxt
    return out


def tokens(seg):
    """shlex tokens for one segment, falling back to a whitespace split when the
    segment is not lexable on its own (an unbalanced quote from slicing)."""
    try:
        return shlex.split(seg)
    except ValueError:
        return seg.split()


def strip_heredoc_bodies(command):
    """Remove heredoc BODIES (data, never commands) while KEEPING the commands
    that follow the closing delimiter.

    Replaces the older "truncate at the first `<<`" approach. Truncating threw
    away everything after the heredoc, so the dominant shape

        cat > notes.md <<'BODY'
        ...body...
        BODY
        cd some/repo
        git push

    hid BOTH the `cd` and the push from every parser that truncated. Writing
    body text to a FILE first is the recommended shape, so this is the common
    case, not an exotic one.

    Unterminated heredoc (an opener with no closing delimiter line) -> fall back
    to truncating at that opener, so an unmatched body can never be misread as
    commands. Strictly safer than either extreme.
    """
    if not command:
        return command
    stripped = _HEREDOC.sub("", command)
    m = _HEREDOC_OPEN.search(stripped)
    if m:                       # unterminated heredoc -> conservative truncation
        stripped = stripped[:m.start()]
    return stripped


def strip_noncode(command):
    """Reduce a command to the text a shell would actually parse as code: drop
    `#`-to-end-of-line comments outside quotes.

    A comment tail is TEXT THE SHELL NEVER RUNS, but the operators inside it are
    still real characters, so a splitter walks straight through them: in
    `git push # note ; cd /elsewhere` the `;` creates a segment boundary and
    ` cd /elsewhere` reads as a genuine `cd`. Stripping cuts BOTH ways -- it
    stops a phantom `cd` from moving a guard off its target (a fail-open), and
    stops one from dragging an unrelated target ON to it (a false block).

    Line continuations are NOT handled here: split_segments_with_seps deletes
    `\\`+newline for every consumer, so doing it again would be a second place
    to keep correct.

    Quote-aware, because the symmetric bug is over-stripping: `-m "issue #42"`
    and `-m "fix: a # b"` must survive untouched.
    """
    out, quote, i, n = [], None, 0, len(command)
    while i < n:
        c = command[i]
        if quote:
            out.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                out.append(command[i + 1]); i += 2; continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c; out.append(c); i += 1; continue
        if c == "\\" and i + 1 < n:
            out.append(c); out.append(command[i + 1]); i += 2; continue
        if c == "#" and (not out or out[-1].isspace()):
            while i < n and command[i] != "\n":     # to end of LINE, not of string
                i += 1
            continue
        out.append(c); i += 1
    return "".join(out)


def split_segments_with_seps(command):
    """Split a command on shell operators that are OUTSIDE quotes, reporting the
    operator BETWEEN each adjacent pair.

    Returns `[(sep_before, text), ...]`; `sep_before` is `""` for the first
    segment and otherwise the exact operator that separated it from the
    previous one: `&&`, `||`, `|`, `;`, `&`, a newline, `(` or `)`.

    WHY the separator is load-bearing: a `cd` does NOT unconditionally take
    effect for whatever follows it, and a splitter that throws the operator away
    cannot tell the cases apart:

        cd X && cmd    -> cmd runs in X           (cd succeeded)
        cd X || cmd    -> cmd runs in the OLD cwd (right side runs only on FAILURE)
        cd X |  cmd    -> cmd runs in the OLD cwd (subshell)
        cd X &  cmd    -> cmd runs in the OLD cwd (the cd was backgrounded)
        ( cd X ) cmd   -> cmd runs in the OLD cwd (subshell scope)
        cd X ;  cmd    -> AMBIGUOUS: X if the cd succeeded, the OLD cwd if not

    LINE CONTINUATIONS are DELETED here, outside single quotes, because a shell
    deletes `\\`+newline before any other parsing: `git \\<newline>push` IS
    `git push`. Keeping the backslash left a non-whitespace byte between a verb
    and its argument, so a consumer's `verb\\s+arg` pattern silently stopped
    matching a command that had merely been wrapped at 80 columns. Consequence
    to know: the segments no longer reconstruct the input BYTE-for-byte -- they
    reconstruct what a shell would run.
    """
    segs, cur, sep = [], [], ""
    i, n, quote = 0, len(command), None
    while i < n:
        c = command[i]
        if quote:                      # inside a quote: copy verbatim to the close
            if c == "\\" and quote == '"' and i + 1 < n and command[i + 1] == "\n":
                i += 2; continue           # line continuation: deleted, even in " "
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < n and command[i + 1] in '"\\$`':
                cur.append(command[i + 1]); i += 2; continue   # \" \\ \$ \` in " "
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c; cur.append(c); i += 1; continue
        if c == "\\" and i + 1 < n:     # escaped operator outside quotes -> literal
            if command[i + 1] == "\n":
                i += 2; continue           # LINE CONTINUATION -> delete both
            cur.append(c); cur.append(command[i + 1]); i += 2; continue
        two = command[i:i + 2]
        if two in ("&&", "||"):
            segs.append((sep, "".join(cur))); cur = []; sep = two; i += 2; continue
        if c in ("|", ";", "\n", "&", "(", ")"):
            segs.append((sep, "".join(cur))); cur = []; sep = c; i += 1; continue
        cur.append(c); i += 1
    segs.append((sep, "".join(cur)))
    return segs


def leading_env_assigns(command):
    """Dict of `VAR=value` assignments that PREFIX a real command in `command`.

    `FOO=1 BAR=2 git push`           -> {'FOO': '1', 'BAR': '2'}
    `export FOO=1; git push`         -> {'FOO': '1'}   (exported: reaches later)
    `git push ; FOO=1`               -> {}             (bare: prefixes nothing)
    `git status && BSB=1 git switch` -> {'BSB': '1'}   (union across segments)
    `echo 'FOO=1 git push'`          -> {}             (quoted, not a real assign)

    An inline `VAR=1 <cmd>` prefix lives ONLY in the command STRING and never
    reaches the hook process's `os.environ`. A gate that advertises an inline
    bypass must therefore read the token from HERE, not from os.environ alone,
    or the bypass printed in its own block message can never fire -- which
    trains people to channel-switch around the gate instead.

    Skips transparent wrappers the way a shell does. Empty dict when none or
    unparseable. Heredoc (`<<` anywhere): only line 1 up to the first `<<` is
    parsed, so a heredoc BODY line that merely looks like `X=1 cmd` can never
    be counted.
    """
    if not command or "=" not in command:
        return {}
    if "<<" in command:
        command = command.split("\n", 1)[0].split("<<", 1)[0]
    assigns = {}
    for _sep, seg in split_segments_with_seps(command):
        seg = seg.strip()
        if not seg:
            continue
        toks = tokens(seg)
        if not toks:
            continue
        exported = toks[0] == "export"
        i = 1 if exported else 0
        here = {}
        while i < len(toks) and (ENV_ASSIGN_RE.match(toks[i])
                                 or toks[i] in WRAPPER_PREFIXES):
            if ENV_ASSIGN_RE.match(toks[i]):
                k, _, v = toks[i].partition("=")
                here[k] = v
            i += 1
        # These must PREFIX A REAL COMMAND. A bare trailing `VAR=1` sets only a
        # SHELL variable -- unexported, so it never reaches any child process's
        # environment and cannot be a bypass for one. Counting it meant
        # `<gated cmd> ; GATE_BYPASS=1` silently disarmed a gate for a command
        # that had ALREADY RUN. `export VAR=1` DOES reach later commands.
        if here and (exported or i < len(toks)):
            assigns.update(here)
    return assigns


def segment_bypass_flags(segments, var, value="1"):
    """Per-segment bypass truth for an ALREADY-SPLIT command: `[bool, ...]`,
    aligned to `segments`.

    `leading_env_assigns` unions across the whole line, which cannot answer the
    question a gate actually has -- "is the bypass on the command I am about to
    block?" -- so a real assignment ANYWHERE disarmed every rule, including one
    on an unrelated command (`git push && BYPASS=1 echo hi`).

    The caller passes its OWN segments because each gate sanitizes differently
    (heredoc bodies, comment tails). A bare `VAR=1` segment does not carry to
    later commands; an `export` does, and is honoured from that point on.
    """
    flags, exported = [], False
    for seg in segments:
        here = exported or leading_env_assigns(seg).get(var) == value
        toks = tokens(seg.strip())
        if toks and toks[0] == "export" and f"{var}={value}" in toks:
            exported = True
            here = True
        flags.append(here)
    return flags
