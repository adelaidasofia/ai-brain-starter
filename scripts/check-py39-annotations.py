#!/usr/bin/env python3
"""
Reject PEP 604 (`X | None`) annotations that Python 3.9 evaluates at runtime.

The canonical gate (`scripts/ci.sh`) deliberately pins **Python 3.9**, but developers
run 3.12+ locally, where `X | None` is legal everywhere. py_compile does not catch the
difference -- `X | None` is valid SYNTAX on 3.9 and raises TypeError only when the
annotation is EVALUATED, which happens at def time on import. So a file passes every
local check, passes py_compile in CI, and then dies the moment the gate imports it:

    TypeError: unsupported operand type(s) for |: 'types.GenericAlias' and 'NoneType'

`from __future__ import annotations` makes every annotation a lazy string, which is why
206 files in this repo already carry it. This check enforces that pairing.

Two cases, because the future import only defers ANNOTATIONS:
  * annotation context, no future import -> fixable by adding the import.
  * `X | None` OUTSIDE an annotation (a type alias, an isinstance arg) -> the future
    import does NOT help; the expression is evaluated regardless.

Deliberately NOT flagged: any other `a | b` outside an annotation. `|` is also the
bitwise/set-union operator, and `re.IGNORECASE | re.MULTILINE`, `set_a | set_b` and
`fcntl.LOCK_EX | fcntl.LOCK_NB` are indistinguishable from a type union without type
inference. A first cut of this check flagged 45 such lines across this repo -- every
one a legitimate operator. A gate that fires on correct code teaches people to bypass
it, so the non-annotation rule is narrowed to an operand that is the `None` LITERAL,
which is never valid bitwise input and is therefore certainly a type expression.

Usage:
  check-py39-annotations.py                # scan tracked Python
  check-py39-annotations.py --self-test    # prove the checker still bites
"""
# exit-contract: ENFORCING


from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(a.name == "annotations" for a in node.names)
        for node in tree.body
    )


def _is_pep604(node: ast.AST) -> bool:
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)


def _has_none_operand(node: ast.BinOp) -> bool:
    """`X | None` cannot be bitwise -- None supports no `|`. Certain type expression."""
    return any(
        isinstance(side, ast.Constant) and side.value is None
        for side in (node.left, node.right)
    )


def _annotation_nodes(tree: ast.Module):
    """Every expression the interpreter treats as an annotation."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
                if arg is not None and arg.annotation is not None:
                    yield arg.annotation
            if node.returns is not None:
                yield node.returns
        elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
            yield node.annotation


def scan(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []  # py_compile owns syntax; unreadable files are not this check's job

    ann_nodes = list(_annotation_nodes(tree))
    ann_ids = {id(n) for node in ann_nodes for n in ast.walk(node)}
    guarded = _has_future_annotations(tree)

    problems = []
    for node in ast.walk(tree):
        if not _is_pep604(node):
            continue
        in_annotation = id(node) in ann_ids
        if in_annotation and guarded:
            continue
        if in_annotation:
            problems.append(
                f"{path.relative_to(REPO)}:{node.lineno}: PEP 604 annotation without "
                f"`from __future__ import annotations` — TypeError on the 3.9 gate"
            )
        elif _has_none_operand(node):
            problems.append(
                f"{path.relative_to(REPO)}:{node.lineno}: `X | None` outside an annotation — "
                f"evaluated regardless of the future import; use typing.Optional"
            )
    return problems


def _self_test() -> int:
    import tempfile

    cases = [
        ("bare annotation must FAIL", "def f(x: int | None = None) -> str | None: ...\n", 1),
        ("guarded annotation must PASS",
         "from __future__ import annotations\ndef f(x: int | None = None) -> str | None: ...\n", 0),
        ("`X | None` type alias must FAIL even when guarded",
         "from __future__ import annotations\nAlias = int | None\n", 1),
        ("bitwise OR must PASS — it is not a type union",
         "import re\nFLAGS = re.IGNORECASE | re.MULTILINE\n", 0),
        ("set union must PASS", "def f(a, b):\n    return sorted(a | b)\n", 0),
        ("fcntl flag OR must PASS",
         "import fcntl\ndef f(fd):\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n", 0),
        ("annotated assignment must FAIL", "x: int | None = None\n", 1),
        ("no PEP 604 at all must PASS", "def f(x: int) -> str: ...\n", 0),
        ("3.9-legal PEP 585 alone must PASS", "def f(x: dict[int, str]) -> list[str]: ...\n", 0),
    ]
    failures = 0
    with tempfile.TemporaryDirectory(dir=REPO) as td:
        for name, src, expected in cases:
            p = Path(td) / "case.py"
            p.write_text(src, encoding="utf-8")
            got = len(scan(p))
            ok = (got > 0) == (expected > 0)
            print(f"  {'PASS' if ok else 'FAIL'}  {name}  (findings={got})")
            failures += 0 if ok else 1
    if failures:
        print(f"\nself-test FAILED ({failures})")
        return 1
    print("\nself-test OK - the checker bites on every case it claims to catch")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()

    out = subprocess.run(["git", "ls-files", "-z", "--", "*.py"], cwd=REPO,
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    files = [REPO / f for f in out.stdout.split("\0") if f]
    problems = [p for f in files for p in scan(f)]
    if problems:
        print(f"::error::PEP 604 annotations unsafe on the Python 3.9 gate ({len(problems)}):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"OK - {len(files)} tracked Python file(s); no PEP 604 unsafe on the 3.9 gate.")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
