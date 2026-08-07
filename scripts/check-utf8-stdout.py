#!/usr/bin/env python3
"""check-utf8-stdout.py - fail-loud guard against the Windows cp1252 print crash.

A vault script that print()s the "gear Meta" emoji, an em dash, or an accented
name works on macOS/Linux (UTF-8 consoles) and silently ships. On a Windows
cp1252 console - or any C-locale pipe - the SAME print() raises
UnicodeEncodeError, the caller captures an empty string, and downstream logic
misreads it (ai-brain-starter#313: sync-vault-scripts.ps1 read the empty result
as "no Meta folder"). The fix is a 5-line reconfigure guard at the CLI entry:

    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass

This lint enforces that guard so the class cannot recur. It is the class-level
watchdog for the SILENT-NO-OP / cp1252-crash bug: PR #313 fixed the two files
that had already broken; this makes the NEXT one fail CI instead of a user's
Windows console.

SCOPE - which files this gate actually looks at (MYC-3530):
    scripts/*.py  AND  hooks/*.py  (see _SCAN_PATHSPECS).

    This header used to describe the target as "a vault script" and the usage
    line said "tracked scripts/*.py", while the CI wiring in scripts/ci.sh and
    .github/workflows/lint.yml both claimed the gate "fails ANY runnable vault
    CLI". Those two statements are not the same statement, and the second one
    was false: _tracked_scripts() only ever passed `scripts/*.py` to git
    ls-files, so hooks/ - 113 tracked Python files, 78 of them flaggable - had
    never been scanned by this lint at all. That was a SCOPE gap, not a
    predicate gap: the 78 are files the ORIGINAL, unwidened predicate would
    already have flagged. The gate simply never looked.

    hooks/ is the HIGHER-severity surface, not the lower one. A script that
    dies on a cp1252 console produces a bad answer for whoever ran it. A hook
    gates the tool call: a PreToolUse hook that raises UnicodeEncodeError
    mid-gate either fails silently OPEN (the protection the user believes is
    there is not) or denies every Write with no legible cause. That shape has
    already shipped here twice - #375, and #409 ("vault frontmatter lint denies
    every Write on Windows").

Rule (deterministic, near-zero false negative):
    A scanned file FAILS if ALL of:
      - it is a runnable CLI ............ has `if __name__ == "__main__":`
      - it writes to the console ........ a print() with no non-console file=,
                                          or sys.stdout/sys.stderr .write()
      - it can EMIT non-ASCII ........... EITHER of two independent signals:
          (1) literal source bytes ....... any byte > 0x7F anywhere in the file
                                           (the emoji, an em dash, an accented
                                           name - the exact bytes that crash)
          (2) a vault-path resolution .... it names a known Meta/vault-root
                                           resolver (_meta_resolver,
                                           find_meta_dir, detect_vault_root...),
                                           so a non-ASCII value arrives from the
                                           FILESYSTEM at runtime
      - it lacks the guard ............. no stdout/stderr .reconfigure(utf-8)
      - it is not opted out ............ no `# utf8-stdout-ok: <reason>` marker

Why signal (1) is not enough - the corrected premise (MYC-3520):
This guard used to state that "a genuinely ASCII-only CLI (no non-ASCII byte
anywhere) can never hit the crash". That is FALSE, and the counter-case was
already named a few lines above: the crash comes from the VALUE printed, not
from a literal in the source. Every vault's top-level folders are emoji-prefixed
("<gear> Meta/", "<target> Sales/", "<clipboard> Strategy/"). A CLI whose own
source is pure ASCII resolves one of those directories, prints the path, and
raises UnicodeEncodeError on a cp1252 console exactly like a script that had the
emoji inline. Six shipped scripts/*.py were in precisely that state
(demote-stale-procedural, entity-disambiguator, resolver-branch-merge-prompt,
resolver-conflict-report, rotate_graphify_backups, stale-rule-check); PR #404
guarded the instances by hand, but this predicate still could not SEE them, so
the seventh would have shipped the same way.

Why signal (2) is a grep and not dataflow: proving "this printed value derives
from that path" needs taint analysis this lint deliberately does not carry. The
cheap proxy - resolver named + prints to console + no guard - accepts false
positives on purpose. The remedy for a false positive is a 5-line idempotent
block that is a no-op on an already-UTF-8 console; the cost of a false negative
is a silent Windows crash that the caller misreads as "no Meta folder". Those
are not symmetric, so the predicate is tuned to over-flag.

Escape hatch: a script whose console output is provably ASCII-only despite a
non-ASCII comment or a resolver import can add `# utf8-stdout-ok: <reason>` on
its own line. The guard itself is a harmless no-op on POSIX (already UTF-8), so
adding it is almost always the right fix rather than a bypass.

THE RATCHET - scripts/utf8-stdout-baseline.txt (MYC-3530):
    Widening the scope to hooks/ surfaced 78 pre-existing violations at once.
    That is far past fix-in-one-PR size, and bulk-adding the 5-line guard from
    a grep is exactly what the MYC-3520 guardrail forbids. So the legacy
    population is PINNED by content hash, using the same mechanism as
    scripts/vault-root-read-baseline.txt (#407) and
    scripts/cloud-safe-walker-baseline.txt (#411):

      - A pinned file passes ONLY while its content still hashes to its row.
        Edit it at all and the row goes STALE and the build fails, so the next
        person to touch any of these files has to fix the guard.
      - A file NOT in the baseline has no grace period: a new or newly-flagged
        file fails on the first run. That is the whole point of the widening.
      - Rows only ever get DELETED. Re-pinning an edited file to keep it quiet
        converts the backlog into a permanent exemption.

    The hash is taken over NEWLINE-NORMALIZED content, not the bytes on disk.
    This repo has no .gitattributes, so a Windows clone with core.autocrlf=true
    has CRLF on disk while the Linux CI runner has LF; hashing raw would pin the
    CHECKOUT instead of the CONTENT and report every row stale on the other
    platform (that regression was live and shipped as #411 - read it before
    changing how this hashes).

Usage:
    check-utf8-stdout.py                 # lint scripts/*.py + hooks/*.py against
                                         #   the baseline; exit 1 on any violation
    check-utf8-stdout.py FILE [FILE ...] # lint the named files, NO baseline
                                         #   (fixtures and negative controls)
    check-utf8-stdout.py --report [glob] # classify every file, never fail (enumeration)
"""
import ast
import hashlib
import re
import subprocess
import sys
from pathlib import Path

# The guard is detected structurally: a .reconfigure(encoding="utf-8") call
# (single/double quotes, optional hyphen, flexible whitespace). Both files PR
# #313 fixed use exactly `.reconfigure(encoding="utf-8")`.
_GUARD_RE = re.compile(r"\.reconfigure\(\s*encoding\s*=\s*['\"]utf-?8['\"]", re.IGNORECASE)
_BYPASS_RE = re.compile(r"#\s*utf8-stdout-ok\b", re.IGNORECASE)

# Signal (2): the module names a resolver that hands back a path from the vault
# FILESYSTEM, where every top-level folder is emoji-prefixed. Substring match on
# purpose - `_find_meta_dir_helper`, `aggregator.find_meta_dir(...)` and
# `from _meta_resolver import ...` must all count, and a grep-level heuristic is
# the whole point (no AST/taint machinery). Every name below is a real resolver
# shipped in this repo; adding one here is how the guard learns a new entrypoint.
_RESOLVER_NAMES = (
    "_meta_resolver",          # scripts/_meta_resolver.py, the shared resolver
    "find_meta_dir",           # its function (and every *_helper alias of it)
    "resolve_meta_dir",
    "find_meta_name",          # hooks/surface-stranded-session-artifacts.py
    "find_vault_root",         # scripts/graph-liveness-check.py
    "find_repo_vault_root",    # hooks/_lib/vault_root.py
    "resolve_vault_root",      # hooks/_lib/vault_root.py, detect-closing-signal
    "resolve_vault_context",
    "detect_vault_root",       # scripts/granola_core.py
)
# NEGATIVE-CONTROL ANCHOR (scripts/test-utf8-console-guard.sh rewrites this ONE
# line to a never-matching pattern to prove the resolver branch is load-bearing;
# if you move or rename it, update that test - it fails loud when the anchor is
# missing rather than silently losing the control).
_RESOLVER_RE = re.compile("|".join(re.escape(n) for n in _RESOLVER_NAMES))


def _utf8_streams():
    """Reconfigure our own CLI streams; this lint prints file paths that can
    themselves contain the emoji, so it must not crash on the console it guards."""
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass


def _is_console_print(node):
    """True if this Call writes to the console (stdout/stderr)."""
    func = node.func
    # print(...) - console unless a non-stdout/stderr file= redirects it.
    if isinstance(func, ast.Name) and func.id == "print":
        for kw in node.keywords:
            if kw.arg == "file":
                val = kw.value
                # file=sys.stdout / file=sys.stderr stays console; anything else
                # (an open file, a StringIO) is not a console write.
                if isinstance(val, ast.Attribute) and val.attr in ("stdout", "stderr"):
                    return True
                return False
        return True
    # sys.stdout.write(...) / sys.stderr.write(...) / .writelines(...)
    if isinstance(func, ast.Attribute) and func.attr in ("write", "writelines"):
        owner = func.value
        if isinstance(owner, ast.Attribute) and owner.attr in ("stdout", "stderr"):
            return True
    return False


def classify(path):
    """Return a dict describing the file against the signals."""
    data = path.read_bytes()
    has_non_ascii = any(b > 0x7F for b in data)
    text = data.decode("utf-8", errors="replace")

    has_guard = bool(_GUARD_RE.search(text))
    has_bypass = bool(_BYPASS_RE.search(text))
    resolves_vault_path = bool(_RESOLVER_RE.search(text))

    is_cli = False
    prints_console = False
    parse_error = None
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:  # py_compile gate owns syntax; we just skip.
        parse_error = str(exc)
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test = node.test
                # if __name__ == "__main__":
                if (
                    isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__"
                ):
                    is_cli = True
            if isinstance(node, ast.Call) and _is_console_print(node):
                prints_console = True

    # Either emission signal is sufficient: non-ASCII already in the source, OR
    # a vault-path resolution that fetches non-ASCII from the filesystem.
    can_emit_non_ascii = has_non_ascii or resolves_vault_path
    flagged = (
        is_cli
        and prints_console
        and can_emit_non_ascii
        and not has_guard
        and not has_bypass
        and parse_error is None
    )
    return {
        "path": path,
        "is_cli": is_cli,
        "prints_console": prints_console,
        "has_non_ascii": has_non_ascii,
        "resolves_vault_path": resolves_vault_path,
        "has_guard": has_guard,
        "has_bypass": has_bypass,
        "parse_error": parse_error,
        "flagged": flagged,
    }


def normalize(source):
    """CRLF/CR -> LF.

    The baseline hash MUST be identical on every checkout or the ratchet is
    useless. This repo has no .gitattributes, so a Windows clone with
    core.autocrlf=true has CRLF on disk while the Linux CI runner has LF -- a
    raw-bytes hash pinned on one platform is 100% stale on the other, which reds
    the build for everyone with no real violation behind it. Shipped live once
    already (#411, scripts/cloud-safe-walker-baseline.txt). Normalizing makes
    the pin describe the CONTENT, not the checkout.
    """
    return source.replace("\r\n", "\n").replace("\r", "\n")


def content_digest(path):
    """SHA-256 over the newline-normalized text of `path`."""
    text = path.read_bytes().decode("utf-8", errors="replace")
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


# SCOPE ANCHOR (MYC-3530). The two roots this gate scans. scripts/ is the
# shipped CLI surface; hooks/ is the GATE surface, where the same crash either
# fails silently open or denies every write (#375, #409). hooks/ was outside the
# scan until MYC-3530 -- not because its files were clean, but because
# `_tracked_scripts()` only ever passed `scripts/*.py` to git ls-files.
#
# NEGATIVE-CONTROL ANCHOR (scripts/test-utf8-console-guard.sh rewrites this ONE
# line back to the scripts-only tuple to prove the widening is load-bearing; if
# you move or rename it, update that test - it fails loud when the anchor is
# missing rather than silently losing the control).
_SCAN_PATHSPECS = ("scripts/*.py", "hooks/*.py")

DEFAULT_BASELINE = Path(__file__).resolve().parent / "utf8-stdout-baseline.txt"


def _scanned_files():
    """Default target: scripts/*.py + hooks/*.py, resolved from the repo root.

    `--cached --others --exclude-standard` so a brand-new, not-yet-committed
    hook is in scope too: the local pre-push gate must catch it before it is
    ever pushed, not one commit later. Same idiom as
    scripts/check-vault-root-reads.py and scripts/check-cloud-safe-file-walkers.py.
    """
    root = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.check_output(
            ["git", "-C", str(root), "ls-files", "--cached", "--others",
             "--exclude-standard", "--", *_SCAN_PATHSPECS],
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Not a git checkout - fall back to a plain glob per scanned root. git
        # pathspec `*` crosses `/`, so mirror that with rglob to keep the two
        # discovery paths describing the same set (scripts/extractors/*.py,
        # hooks/_lib/*.py).
        found = []
        for spec in _SCAN_PATHSPECS:
            top, _, pattern = spec.partition("/")
            found.extend((root / top).rglob(pattern))
        return sorted(set(found))
    return sorted({root / line for line in out.splitlines() if line.strip()})


def load_baseline(path):
    """path -> (sha256, tag). Raises ValueError on a malformed or missing file."""
    if not path.is_file():
        raise ValueError("baseline not found: {}".format(path))
    entries = {}
    text = path.read_bytes().decode("utf-8", errors="replace")
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) != 3 or len(parts[0]) != 64:
            raise ValueError(
                "invalid baseline row {}:{}: {!r}".format(path, line_no, raw)
            )
        entries[parts[2]] = (parts[0], parts[1])
    return entries


def main(argv):
    _utf8_streams()
    report_mode = False
    args = list(argv)
    if args and args[0] == "--report":
        report_mode = True
        args = args[1:]

    # The baseline ratchet applies to the FLEET scan only. Explicit file
    # arguments mean "audit exactly these", which is how the fixtures and the
    # negative controls in scripts/test-utf8-console-guard.sh call this: a
    # pardon list has no meaning for a file outside the fleet.
    fleet_mode = not args
    root = Path(__file__).resolve().parent.parent
    if args:
        # In --report mode a bare glob string is allowed; otherwise treat as paths.
        if report_mode and len(args) == 1 and any(ch in args[0] for ch in "*?"):
            targets = sorted(Path().glob(args[0]))
        else:
            targets = [Path(a) for a in args]
    else:
        targets = _scanned_files()

    results = [classify(p) for p in targets if p.suffix == ".py" and p.is_file()]

    if report_mode:
        _print_report(results)
        return 0

    violations = [r for r in results if r["flagged"]]
    pardoned = {}
    stale = []
    if fleet_mode:
        try:
            baseline = load_baseline(DEFAULT_BASELINE)
        except (ValueError, OSError) as exc:
            print("::error::utf8 console guard: {}".format(exc), file=sys.stderr)
            return 2
        live = []
        for r in violations:
            rel = r["path"].relative_to(root).as_posix()
            digest = content_digest(r["path"])
            row = baseline.get(rel)
            if row and row[0] == digest:
                pardoned[rel] = row[1]
                continue
            live.append(r)
        violations = live
        # A row is live only while its file is STILL flagged at exactly the
        # pinned content. Anything else - guard added, file deleted, file
        # edited, content drifted - is stale and must be removed, not re-pinned.
        stale = sorted(set(baseline) - set(pardoned))

    if violations or stale:
        if violations:
            print(
                "::error::vault script/hook(s) print to a Windows-hostile console "
                "without the UTF-8 stdout/stderr guard (the ai-brain-starter#313 "
                "cp1252 crash class). Add the 5-line reconfigure block at the CLI "
                "entrypoint:",
                file=sys.stderr,
            )
            print(
                "    for _stream in (sys.stdout, sys.stderr):\n"
                "        try:\n"
                '            _stream.reconfigure(encoding="utf-8")  # Python 3.7+\n'
                "        except (AttributeError, ValueError):\n"
                "            pass",
                file=sys.stderr,
            )
        for r in violations:
            # Repo-relative when we can: a `::error file=` annotation only
            # attaches to the diff if the path is relative to the repo root, and
            # the fleet scan hands back absolute paths.
            try:
                rel = r["path"].relative_to(root).as_posix()
            except ValueError:
                rel = r["path"]
            if r["has_non_ascii"]:
                why = "carries non-ASCII source"
            else:
                why = (
                    "is ASCII-only in source but resolves a Meta/vault path "
                    "(the vault's own folders are emoji-prefixed, so the "
                    "non-ASCII arrives at runtime)"
                )
            print(
                "::error file={f}::{f} is a runnable CLI that writes to the "
                "console and {why}, but never reconfigures stdout/stderr to "
                "UTF-8 (add the guard, or `# utf8-stdout-ok: <reason>` if its "
                "console output is provably ASCII-only).".format(f=rel, why=why),
                file=sys.stderr,
            )
        for rel in stale:
            print(
                "::error file={f}::STALE BASELINE {f}: the row in {b} no longer "
                "matches. The file was edited, guarded, bypassed, or removed. "
                "Delete the row -- do NOT re-pin edited content to keep it "
                "exempt, that turns the backlog into a permanent "
                "exemption.".format(f=rel, b=DEFAULT_BASELINE.name),
                file=sys.stderr,
            )
        print(
            "\nFAILED: {n} unpinned file(s) missing the UTF-8 console guard, "
            "{s} stale baseline row(s).".format(n=len(violations), s=len(stale)),
            file=sys.stderr,
        )
        return 1

    if fleet_mode:
        by_tag = {}
        for tag in pardoned.values():
            by_tag[tag] = by_tag.get(tag, 0) + 1
        tags = ", ".join("{}={}".format(t, n) for t, n in sorted(by_tag.items()))
        print(
            "OK - {n} file(s) checked across {roots}; every unpinned printing CLI "
            "that can emit non-ASCII carries the UTF-8 console guard. "
            "{p} content-pinned legacy file(s) remain [{tags}] - see {b}.".format(
                n=len(results),
                roots=" + ".join(_SCAN_PATHSPECS),
                p=len(pardoned),
                tags=tags or "none",
                b=DEFAULT_BASELINE.name,
            )
        )
        return 0

    n_resolver = sum(1 for r in results if r["resolves_vault_path"] and r["is_cli"])
    print(
        "OK - {n} script(s) checked; every printing CLI that can emit non-ASCII "
        "carries the UTF-8 console guard ({r} of them via a runtime "
        "Meta/vault-path resolution, not a source literal).".format(
            n=len(results), r=n_resolver
        )
    )
    return 0


def _print_report(results):
    """Human enumeration: one row per file, with why it is / is not flagged."""
    flagged = [r for r in results if r["flagged"]]
    clean_cli = [r for r in results if r["is_cli"] and not r["flagged"]]
    non_cli = [r for r in results if not r["is_cli"]]

    def _reason(r):
        bits = []
        bits.append("cli" if r["is_cli"] else "lib")
        bits.append("print" if r["prints_console"] else "no-print")
        bits.append("non-ascii" if r["has_non_ascii"] else "ascii")
        if r["resolves_vault_path"]:
            bits.append("meta-resolver")
        bits.append("guard" if r["has_guard"] else "no-guard")
        if r["has_bypass"]:
            bits.append("bypass")
        if r["parse_error"]:
            bits.append("PARSE-ERR")
        return ",".join(bits)

    print("== FLAGGED (needs the guard) : {} ==".format(len(flagged)))
    for r in flagged:
        print("  FIX  {}  [{}]".format(r["path"], _reason(r)))
    print("\n== printing CLIs already safe : {} ==".format(len(clean_cli)))
    for r in clean_cli:
        print("  ok   {}  [{}]".format(r["path"], _reason(r)))
    print("\n== non-CLI / library files : {} ==".format(len(non_cli)))
    for r in non_cli:
        print("  -    {}  [{}]".format(r["path"], _reason(r)))
    ascii_only_resolver = [
        r for r in results
        if r["is_cli"] and r["prints_console"]
        and r["resolves_vault_path"] and not r["has_non_ascii"]
    ]
    print(
        "\nTotals: {} flagged, {} safe CLIs, {} non-CLI, {} files.".format(
            len(flagged), len(clean_cli), len(non_cli), len(results)
        )
    )
    print(
        "Of those, {} printing CLI(s) are ASCII-ONLY in source yet resolve a "
        "Meta/vault path - the population the old 'non-ASCII source' predicate "
        "was structurally blind to (MYC-3520).".format(len(ascii_only_resolver))
    )
    for r in ascii_only_resolver:
        print("       {}  [{}]".format(r["path"], "guard" if r["has_guard"] else "NO-GUARD"))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
