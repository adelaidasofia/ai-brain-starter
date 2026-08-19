#!/usr/bin/env python3
"""audit-decode-safe-reads.py - forward guard for the decode-class fail-open.

THE BUG CLASS
-------------
A guard reads a file inside `try: ... except OSError:`. `UnicodeDecodeError`
subclasses `ValueError`, NOT `OSError`, so on a machine whose default encoding is
not UTF-8 - a cp1252 Windows console reading a settings.json or a session
transcript full of non-ASCII paths - the decode error walks straight past that
handler. `hook_runner` then masks the traceback and the hook returns
`{"continue": true, "suppressOutput": true}`.

A guard whose job is to REFUSE therefore silently ALLOWS, and nothing anywhere
says so. The trigger is ordinary: an accented folder name, an emoji in a vault
path, a smart quote in a note title.

Adding `-X utf8` to the launcher removes the TRIGGER on the paths the launcher
covers. It does not fix the HANDLER, and any path that bypasses the launcher
re-exposes every one of them. A handler that cannot survive a decode error is
wrong on its own terms.

WHAT COUNTS AS SAFE
-------------------
1. An enclosing `except` that actually catches the class: `UnicodeDecodeError`,
   `UnicodeError`, `ValueError`, `Exception`, `BaseException`, or a bare `except`.
2. A read that CANNOT raise it: `errors=` set to a non-strict policy
   ("replace", "ignore", "surrogateescape", "backslashreplace"). Note that
   `encoding="utf-8"` alone does NOT make a read safe - it fixes WHICH codec is
   used, and a wrong byte under that codec still raises.
3. A binary read (`"rb"`), which decodes nothing.

WHAT THIS DOES NOT SEE - stated so a clean run is read honestly:
  * a handle opened inside the try and consumed OUTSIDE it (the decode happens
    at read/iterate time, and this anchors on the `open`);
  * a decode inside a function the try calls, rather than in the try body;
  * `subprocess` output decoded by the locale - a different mechanism with its
    own burn-down.

Modes:
  --all [--paths DIR ...]   Audit every .py under the paths (default: hooks/).
                            Exit 1 on any narrow-handler violation. The CI gate.
  --check FILE ...          Same verdict for specific files (add-time gate).
  --unguarded               Also list text reads with NO enclosing try. In a
                            wired hook that is masked into a fail-open too, but
                            it is a different fix, so it is reported and never
                            gates.
  --selftest                Positive + negative controls. A guard earns trust by
                            failing on the thing it catches.

Stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hooks"))

from _lib.safe_read import safe_read_text  # noqa: E402

# Handlers that genuinely catch a UnicodeDecodeError.
DECODE_SAFE = {
    "UnicodeDecodeError",
    "UnicodeError",
    "ValueError",
    "Exception",
    "BaseException",
}

# The narrow handlers this exists to find. Catching only these is the defect.
OSERROR_FAMILY = {
    "OSError",
    "IOError",
    "EnvironmentError",
    "FileNotFoundError",
    "PermissionError",
    "IsADirectoryError",
    "NotADirectoryError",
    "FileExistsError",
    "InterruptedError",
    "BlockingIOError",
    "TimeoutError",
    "ConnectionError",
}

NON_STRICT_ERRORS = {"replace", "ignore", "surrogateescape", "backslashreplace"}

# Modules whose `open` takes the FILE first and the mode second, like the
# builtin - unlike `Path.open`, where mode is first. Getting this backwards makes
# `io.open(p)` look like a write and exempts it silently.
MODULE_OPENERS = {
    "io", "gzip", "bz2", "lzma", "codecs", "tarfile", "zipfile", "shelve", "dbm",
}
# `os.open` returns a file DESCRIPTOR. It decodes nothing.
FD_OPENERS = {"os", "posix"}


def _name_of(node: ast.AST) -> str:
    """Dotted or bare exception name, e.g. `OSError` or `json.JSONDecodeError`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _handler_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return {"__bare__"}
    if isinstance(handler.type, ast.Tuple):
        return {_name_of(e) for e in handler.type.elts if _name_of(e)}
    n = _name_of(handler.type)
    return {n} if n else set()


def _try_is_decode_safe(node: ast.Try) -> bool:
    for h in node.handlers:
        names = _handler_names(h)
        if "__bare__" in names or names & DECODE_SAFE:
            return True
    return False


def _try_catches_oserror(node: ast.Try) -> bool:
    for h in node.handlers:
        if _handler_names(h) & OSERROR_FAMILY:
            return True
    return False


def _kw(call: ast.Call, name: str):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def _const_str(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _text_read_label(call: ast.Call) -> str | None:
    """Label for a call that DECODES text, else None.

    A read is exempt when `errors=` names a non-strict policy: that read cannot
    raise UnicodeDecodeError at all, so no handler is required.
    """
    func = call.func
    builtin_open = isinstance(func, ast.Name) and func.id == "open"
    # `Path.open(mode)` puts mode FIRST; builtin and module `open(file, mode)`
    # put it second. Reading the wrong slot silently mis-reads every append-only
    # log writer as a text READ (four of them, on the first run of this audit) —
    # and, the other way, exempts every `io.open(p)` as if it were a write.
    method_open = isinstance(func, ast.Attribute) and func.attr == "open"
    receiver = _name_of(func.value) if method_open and isinstance(func.value, ast.Name) else ""
    if method_open and receiver in FD_OPENERS:
        return None                       # a file descriptor decodes nothing
    module_open = method_open and receiver in MODULE_OPENERS
    is_open = builtin_open or method_open
    is_read_text = isinstance(func, ast.Attribute) and func.attr == "read_text"
    if not (is_open or is_read_text):
        return None

    if is_open:
        idx = 1 if (builtin_open or module_open) else 0
        mode_node = call.args[idx] if len(call.args) > idx else None
        mode = _const_str(mode_node) if mode_node is not None else None
        if mode is None:
            mode = _const_str(_kw(call, "mode"))
        if mode is None and mode_node is not None:
            return None  # a computed mode; cannot classify, and guessing is worse
        if mode is not None:
            if "b" in mode:
                return None            # binary: decodes nothing
            if not ("r" in mode or "+" in mode):
                return None            # "w"/"a"/"x": ENCODES, a different class

    errors = _const_str(_kw(call, "errors"))
    if errors in NON_STRICT_ERRORS:
        return None  # cannot raise the class

    return "open()" if is_open else ".read_text()"


class _Scanner(ast.NodeVisitor):
    """Collect text reads with the chain of `try` bodies enclosing each one."""

    def __init__(self) -> None:
        self.stack: list[ast.Try] = []
        self.reads: list[tuple[ast.Call, str, list[ast.Try]]] = []

    def visit_Try(self, node: ast.Try) -> None:
        self.stack.append(node)
        for stmt in node.body:
            self.visit(stmt)
        self.stack.pop()
        # Handlers / else / finally are NOT protected by this try's own handlers.
        for h in node.handlers:
            self.generic_visit(h)
        for stmt in node.orelse + node.finalbody:
            self.visit(stmt)

    def visit_Call(self, node: ast.Call) -> None:
        label = _text_read_label(node)
        if label:
            self.reads.append((node, label, list(self.stack)))
        self.generic_visit(node)


def audit_source(src: str) -> dict:
    """Verdict for one file. Keys: violations, unguarded, error."""
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return {"violations": [], "unguarded": [], "error": f"syntax error: {exc}"}
    sc = _Scanner()
    sc.visit(tree)
    violations, unguarded = [], []
    for call, label, tries in sc.reads:
        if not tries:
            unguarded.append((call.lineno, label))
            continue
        if any(_try_is_decode_safe(t) for t in tries):
            continue
        if any(_try_catches_oserror(t) for t in tries):
            violations.append((call.lineno, label))
        else:
            # some other narrow handler (KeyError, json.JSONDecodeError, ...);
            # the decode class escapes it just the same
            violations.append((call.lineno, label))
    return {"violations": violations, "unguarded": unguarded, "error": ""}


def audit_file(path: Path) -> dict:
    # Through the shared bounded reader, because this recursively walks a tree
    # that can live on a cloud-synced path — the same rule this repo enforces on
    # every other recursive content walker, and one a guard has no standing to
    # exempt itself from. A read that did not succeed is an ERROR, never an
    # empty file that scans clean.
    result = safe_read_text(path, timeout=5.0, max_bytes=2_000_000, errors="replace")
    if not result.ok:
        return {
            "violations": [],
            "unguarded": [],
            "error": f"unreadable ({result.status}{': ' + result.detail if result.detail else ''})",
        }
    out = audit_source(result.text or "")
    out["path"] = path
    return out


def _report(results: list[dict], show_unguarded: bool) -> int:
    bad = [r for r in results if r["violations"] or r["error"]]
    for r in sorted(results, key=lambda r: str(r["path"])):
        if r["error"]:
            print(f"ERROR {r['path']}: {r['error']}")
        for lineno, label in r["violations"]:
            print(
                f"FAIL  {r['path']}:{lineno}: {label} inside a try whose handlers do "
                f"not catch the decode class - UnicodeDecodeError escapes and the "
                f"guard fails open"
            )
        if show_unguarded:
            for lineno, label in r["unguarded"]:
                print(f"note  {r['path']}:{lineno}: {label} with no enclosing try")
    scanned = len(results)
    print(
        f"\nscanned {scanned} file(s); "
        f"{sum(len(r['violations']) for r in results)} violation(s) in {len(bad)} file(s)"
    )
    return 1 if bad else 0


# ---- self-test ---------------------------------------------------------------
_BITES = [
    (
        "narrow OSError around open()",
        "try:\n    with open(p) as f:\n        d = f.read()\nexcept OSError:\n    d = None\n",
    ),
    (
        "narrow OSError around read_text()",
        "try:\n    d = p.read_text()\nexcept OSError:\n    d = None\n",
    ),
    (
        "encoding= alone does not make it safe",
        "try:\n    d = p.read_text(encoding='utf-8')\nexcept OSError:\n    d = None\n",
    ),
    (
        "nested try whose own handler is still narrow",
        "try:\n    try:\n        d = p.read_text()\n    except FileNotFoundError:\n"
        "        d = None\nexcept OSError:\n    d = None\n",
    ),
    (
        "Path.open with no mode defaults to reading",
        "try:\n    d = p.open().read()\nexcept OSError:\n    d = None\n",
    ),
    (
        "Path.open('r') is a read",
        "try:\n    d = p.open('r').read()\nexcept OSError:\n    d = None\n",
    ),
    (
        "io.open takes the file first, like the builtin",
        "try:\n    d = io.open(p).read()\nexcept OSError:\n    d = None\n",
    ),
    (
        "io.open with a literal path is not a mode string",
        "try:\n    d = io.open('config.json').read()\nexcept OSError:\n    d = None\n",
    ),
    (
        "codecs.open likewise",
        "try:\n    d = codecs.open(p, 'r').read()\nexcept OSError:\n    d = None\n",
    ),
    (
        "read-write mode can still decode",
        "try:\n    d = open(p, 'r+').read()\nexcept OSError:\n    d = None\n",
    ),
    (
        "read in an except block, unprotected by the try it sits in",
        "try:\n    x = 1\nexcept OSError:\n    try:\n        d = p.read_text()\n"
        "    except PermissionError:\n        d = None\n",
    ),
]

_PASSES = [
    ("catches ValueError", "try:\n    d = p.read_text()\nexcept (OSError, ValueError):\n    d = None\n"),
    (
        "catches UnicodeDecodeError by name",
        "try:\n    d = p.read_text()\nexcept (OSError, UnicodeDecodeError):\n    d = None\n",
    ),
    ("catches Exception", "try:\n    d = p.read_text()\nexcept Exception:\n    d = None\n"),
    ("bare except", "try:\n    d = p.read_text()\nexcept:\n    d = None\n"),
    (
        "errors= makes the read incapable of raising",
        "try:\n    d = p.read_text(errors='replace')\nexcept OSError:\n    d = None\n",
    ),
    (
        "os.open returns a descriptor and decodes nothing",
        "try:\n    fd = os.open(p, os.O_RDONLY)\nexcept OSError:\n    fd = -1\n",
    ),
    (
        "io.open in binary is still binary",
        "try:\n    d = io.open(p, 'rb').read()\nexcept OSError:\n    d = None\n",
    ),
    (
        "binary read decodes nothing",
        "try:\n    d = open(p, 'rb').read()\nexcept OSError:\n    d = None\n",
    ),
    (
        "write mode encodes, it never decodes",
        "try:\n    open(p, 'w').write(s)\nexcept OSError:\n    pass\n",
    ),
    (
        "append mode likewise",
        "try:\n    open(p, 'a').write(s)\nexcept OSError:\n    pass\n",
    ),
    (
        "Path.open takes mode FIRST — an append writer is not a read",
        "try:\n    p.open('a').write(s)\nexcept OSError:\n    pass\n",
    ),
    (
        "Path.open append with an encoding kwarg is still a write",
        "try:\n    p.open('a', encoding='utf-8').write(s)\nexcept OSError:\n    pass\n",
    ),
    (
        "outer try catches the class, inner is narrow",
        "try:\n    try:\n        d = p.read_text()\n    except FileNotFoundError:\n"
        "        d = None\nexcept ValueError:\n    d = None\n",
    ),
]


def selftest() -> int:
    fails = 0
    for label, src in _BITES:
        v = audit_source(src)["violations"]
        if v:
            print(f"PASS  bites: {label}")
        else:
            print(f"FAIL  did NOT bite: {label}")
            fails += 1
    for label, src in _PASSES:
        r = audit_source(src)
        if not r["violations"]:
            print(f"PASS  clean: {label}")
        else:
            print(f"FAIL  false positive: {label} -> {r['violations']}")
            fails += 1
    # the unguarded class is reported, never gated
    r = audit_source("d = p.read_text()\n")
    if r["unguarded"] and not r["violations"]:
        print("PASS  an unguarded read is reported, not gated")
    else:
        print("FAIL  unguarded classification wrong")
        fails += 1
    print()
    if fails:
        print(f"{fails} FAILING")
        return 1
    print("ALL PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--check", nargs="+", default=None)
    ap.add_argument("--paths", nargs="+", default=None)
    ap.add_argument("--unguarded", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if args.check:
        files = [Path(f) for f in args.check]
    else:
        roots = [Path(p) for p in (args.paths or [ROOT / "hooks"])]
        files = sorted(
            f
            for root in roots
            for f in (root.rglob("*.py") if root.is_dir() else [root])
            if f.is_file()
        )
    if not files:
        print("ERROR no files to audit - refusing to report a clean scan of nothing")
        return 1
    return _report([audit_file(f) for f in files], args.unguarded)


if __name__ == "__main__":
    # A cp1252 Windows console raises UnicodeEncodeError the moment this prints a
    # non-ASCII character, and the hook dies with no legible cause
    # (ai-brain-starter#313). Idempotent; a no-op on an already-UTF-8 console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
