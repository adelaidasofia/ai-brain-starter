#!/usr/bin/env python3
"""Precise forward guard for recursive Python walkers that read file content.

The guard follows local helper calls with Python's AST.  A function is in scope
only when its reachable code both recursively walks and reads filesystem content.
Metadata-only walkers are clean.  In-scope walkers pass only when that same call
surface reaches the shared ``safe_read`` primitive.

Modes:
  --check FILE [FILE ...]  audit real files; 0 clean, 1 unsafe, 2 unreadable/bad
  --all                    audit the whole Git Python fleet against a hash ratchet
  --self-test              run load-bearing safe/unsafe/metadata controls

This is intentionally not an ``os.walk`` plus ``read_text`` regex.  Those tokens
may live in unrelated functions, examples, or write-only setup code.  Treating
those as violations would train maintainers to ignore the warning.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hooks"))

from _lib.safe_read import safe_read_text  # noqa: E402


SAFE_READ_NAMES = {"safe_read", "safe_read_bytes", "safe_read_text"}
DIRECT_READ_METHODS = {"read_text", "read_bytes"}
COPY_READERS = {"copy", "copy2", "copyfile", "copyfileobj"}


@dataclass
class _Imports:
    os_modules: set[str] = field(default_factory=lambda: {"os"})
    os_walk_names: set[str] = field(default_factory=set)
    # Modules whose `.walk()` traverses an IN-MEMORY tree, never a filesystem.
    # Starts EMPTY and is populated only by a real `import ast [as X]`, unlike
    # os_modules/shutil_modules which seed their own names. Seeding "ast" here
    # would carve out `import os as ast; ast.walk(...)`, a real walker wearing
    # the name. A module imported inside a function is not seen by this
    # top-level scan, so its `.walk` stays flagged: conservative in the safe
    # direction.
    ast_modules: set[str] = field(default_factory=set)
    shutil_modules: set[str] = field(default_factory=lambda: {"shutil"})
    shutil_copy_names: dict[str, str] = field(default_factory=dict)
    safe_names: set[str] = field(default_factory=set)
    safe_modules: set[str] = field(default_factory=set)


@dataclass
class _Summary:
    walkers: set[str] = field(default_factory=set)
    readers: set[str] = field(default_factory=set)
    safe_reads: set[str] = field(default_factory=set)
    callees: set[str] = field(default_factory=set)

    def merge(self, other: "_Summary") -> bool:
        before = (len(self.walkers), len(self.readers), len(self.safe_reads), len(self.callees))
        self.walkers.update(other.walkers)
        self.readers.update(other.readers)
        self.safe_reads.update(other.safe_reads)
        self.callees.update(other.callees)
        after = (len(self.walkers), len(self.readers), len(self.safe_reads), len(self.callees))
        return before != after


class _BodyVisitor(ast.NodeVisitor):
    """Collect direct behavior without descending into nested function bodies."""

    def __init__(
        self,
        current_scope: str,
        current_class: str | None,
        direct_functions: dict[str, set[str]],
        methods: dict[str, set[str]],
        imports: _Imports,
    ) -> None:
        self.current_scope = current_scope
        self.current_class = current_class
        self.direct_functions = direct_functions
        self.methods = methods
        self.imports = imports
        self.summary = _Summary()
        self.call_aliases: dict[str, set[str]] = {}
        self.reader_aliases: dict[str, set[str]] = {}
        self.walker_aliases: dict[str, set[str]] = {}
        self.safe_aliases: set[str] = set()
        self.control_depth = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    @staticmethod
    def _dotted(node: ast.AST) -> str:
        parts: list[str] = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))

    @staticmethod
    def _constant_string(node: ast.AST | None) -> str | None:
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    def _open_reads(self, node: ast.Call) -> bool:
        mode_node: ast.AST | None = None
        if len(node.args) >= 2:
            mode_node = node.args[1]
        for kw in node.keywords:
            if kw.arg == "mode":
                mode_node = kw.value
        if mode_node is None:
            return True
        mode = self._constant_string(mode_node)
        if mode is None:
            return True  # dynamic mode cannot prove write-only
        return "r" in mode or "+" in mode

    def _os_open_reads(self, node: ast.Call) -> bool:
        flags_node: ast.AST | None = node.args[1] if len(node.args) >= 2 else None
        for kw in node.keywords:
            if kw.arg == "flags":
                flags_node = kw.value
        if flags_node is None:
            return True

        def flag_terms(part: ast.AST) -> set[str] | None:
            if isinstance(part, ast.BinOp) and isinstance(part.op, ast.BitOr):
                left = flag_terms(part.left)
                right = flag_terms(part.right)
                return None if left is None or right is None else left | right
            dotted = self._dotted(part)
            if isinstance(part, (ast.Name, ast.Attribute)) and dotted:
                return {dotted.rsplit(".", 1)[-1]}
            return None

        terms = flag_terms(flags_node)
        dotted_names = {self._dotted(part) for part in ast.walk(flags_node)}
        dotted_leaves = {name.rsplit(".", 1)[-1] for name in dotted_names if name}
        has_directory_flag = any(
            name.rsplit(".", 1)[-1] == "O_DIRECTORY"
            for name in dotted_names
            if name
        ) or any(
            isinstance(part, ast.Constant) and part.value == "O_DIRECTORY"
            for part in ast.walk(flags_node)
        )
        if has_directory_flag and "O_RDWR" not in dotted_leaves and not any(
            isinstance(part, ast.IfExp) for part in ast.walk(flags_node)
        ):
            return False  # opening a directory fd is metadata, not content
        write_only_terms = {
            "O_WRONLY", "O_APPEND", "O_BINARY", "O_CLOEXEC", "O_CREAT",
            "O_DSYNC", "O_EXCL", "O_NOFOLLOW", "O_NONBLOCK", "O_SYNC",
            "O_TEMPORARY", "O_TRUNC",
        }
        return not (terms and "O_WRONLY" in terms and terms <= write_only_terms)

    def _is_recursive_glob(self, node: ast.Call, dotted: str) -> bool:
        if dotted.rsplit(".", 1)[-1] not in {"glob", "iglob"}:
            return False
        for arg in node.args:
            value = self._constant_string(arg)
            if value and "**" in value:
                return True
        return any(
            kw.arg == "recursive" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )

    def _resolve_local_callable(self, node: ast.AST) -> set[str]:
        if isinstance(node, ast.Name):
            return set(self.direct_functions.get(node.id, set())) | set(
                self.call_aliases.get(node.id, set())
            )
        if isinstance(node, ast.Attribute):
            receiver = node.value
            if (
                self.current_class
                and isinstance(receiver, ast.Name)
                and receiver.id in {"self", "cls"}
            ):
                exact = f"{self.current_class}.{node.attr}"
                if exact in self.methods.get(node.attr, set()):
                    return {exact}
            return set(self.methods.get(node.attr, set()))
        if isinstance(node, ast.IfExp):
            return self._resolve_local_callable(node.body) | self._resolve_local_callable(
                node.orelse
            )
        return set()

    def _safe_callable(self, node: ast.AST) -> bool:
        dotted = self._dotted(node)
        if isinstance(node, ast.Name):
            return node.id in self.imports.safe_names or node.id in self.safe_aliases
        return any(
            dotted == f"{module}.{name}"
            for module in self.imports.safe_modules
            for name in SAFE_READ_NAMES
        )

    def _reader_callable(self, node: ast.AST) -> set[str]:
        dotted = self._dotted(node)
        leaf = dotted.rsplit(".", 1)[-1]
        if isinstance(node, ast.Name) and node.id == "open":
            return {"open(read)-alias"}
        if isinstance(node, ast.Attribute) and leaf in DIRECT_READ_METHODS:
            return {leaf + "-alias"}
        imported_copy = self.imports.shutil_copy_names.get(leaf)
        if leaf in COPY_READERS | {"copytree"} and (
            any(dotted.startswith(f"{module}.") for module in self.imports.shutil_modules)
            or imported_copy == leaf
        ):
            return {leaf + "-alias"}
        return set()

    def _walker_callable(self, node: ast.AST) -> set[str]:
        dotted = self._dotted(node)
        leaf = dotted.rsplit(".", 1)[-1]
        if (
            dotted in {
                f"{module}.{name}"
                for module in self.imports.os_modules
                for name in ("walk", "fwalk")
            }
            or (isinstance(node, ast.Name) and node.id in self.imports.os_walk_names)
        ):
            return {"os.walk-alias"}
        if isinstance(node, ast.Attribute) and leaf in {"walk", "rglob"}:
            return {leaf + "-alias"}
        imported_copy = self.imports.shutil_copy_names.get(leaf)
        if leaf == "copytree" and (
            any(dotted.startswith(f"{module}.") for module in self.imports.shutil_modules)
            or imported_copy == "copytree"
        ):
            return {"copytree-alias"}
        return set()

    def _record_assignment(self, name: str, value: ast.AST) -> None:
        resolved = self._resolve_local_callable(value)
        readers = self._reader_callable(value)
        walkers = self._walker_callable(value)
        safe = self._safe_callable(value)
        if self.control_depth:
            if resolved:
                self.call_aliases.setdefault(name, set()).update(resolved)
            if readers:
                self.reader_aliases.setdefault(name, set()).update(readers)
            if walkers:
                self.walker_aliases.setdefault(name, set()).update(walkers)
            if safe:
                self.safe_aliases.add(name)
            return
        if resolved:
            self.call_aliases[name] = set(resolved)
        else:
            self.call_aliases.pop(name, None)
        if readers:
            self.reader_aliases[name] = set(readers)
        else:
            self.reader_aliases.pop(name, None)
        if walkers:
            self.walker_aliases[name] = set(walkers)
        else:
            self.walker_aliases.pop(name, None)
        if safe:
            self.safe_aliases.add(name)
        else:
            self.safe_aliases.discard(name)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._record_assignment(target.id, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            if node.value is not None:
                self._record_assignment(node.target.id, node.value)
            elif not self.control_depth:
                self.call_aliases.pop(node.target.id, None)
                self.reader_aliases.pop(node.target.id, None)
                self.walker_aliases.pop(node.target.id, None)
                self.safe_aliases.discard(node.target.id)
        self.generic_visit(node)

    def _visit_conditional(self, *blocks: list[ast.stmt]) -> None:
        self.control_depth += 1
        try:
            for block in blocks:
                for statement in block:
                    self.visit(statement)
        finally:
            self.control_depth -= 1

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        self._visit_conditional(node.body, node.orelse)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._visit_conditional(node.body, node.orelse)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit(node.iter)
        self._visit_conditional(node.body, node.orelse)

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self._visit_conditional(node.body, node.orelse)

    def visit_Try(self, node: ast.Try) -> None:
        blocks = [node.body, node.orelse, node.finalbody]
        blocks.extend(handler.body for handler in node.handlers)
        self._visit_conditional(*blocks)

    def visit_Call(self, node: ast.Call) -> None:
        dotted = self._dotted(node.func)
        leaf = dotted.rsplit(".", 1)[-1]

        alias_readers = (
            self.reader_aliases.get(node.func.id, set())
            if isinstance(node.func, ast.Name)
            else set()
        )
        alias_walkers = (
            self.walker_aliases.get(node.func.id, set())
            if isinstance(node.func, ast.Name)
            else set()
        )
        self.summary.readers.update(alias_readers)
        self.summary.walkers.update(alias_walkers)

        os_walker = (
            dotted in {
                f"{module}.{name}"
                for module in self.imports.os_modules
                for name in ("walk", "fwalk")
            }
            or (isinstance(node.func, ast.Name) and node.func.id in self.imports.os_walk_names)
        )
        # `.walk()` on ANY object was treated as Path.walk. That matches by NAME
        # against an open set: `ast.walk(tree)` traverses a parse tree in memory
        # and cannot reach a filesystem, yet a lint that parses Python AND reads
        # source files (which is what most of scripts/check-*.py are) was
        # reported UNSAFE for pairing it with read_text. A false UNSAFE on a
        # guard like this one is expensive: the reader's cheapest resolution is
        # to weaken something real. Carve out only the in-memory walkers,
        # resolved through the import table (see _Imports.ast_modules).
        path_walker = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "walk"
            and dotted not in {f"{module}.walk" for module in self.imports.ast_modules}
        )
        imported_copy = self.imports.shutil_copy_names.get(leaf)
        copytree = (
            dotted in {f"{module}.copytree" for module in self.imports.shutil_modules}
            or imported_copy == "copytree"
        )
        if os_walker:
            self.summary.walkers.add("os.walk")
        elif leaf == "rglob":
            self.summary.walkers.add("rglob")
        elif path_walker:
            self.summary.walkers.add("Path.walk")
        elif self._is_recursive_glob(node, dotted):
            self.summary.walkers.add("recursive-glob")
        if copytree:
            # copytree owns the recursion and opens file content internally; it
            # cannot inherit the shared safe_read boundary from a caller.
            self.summary.walkers.add("shutil.copytree")
            self.summary.readers.add("copytree")

        safe_name_call = isinstance(node.func, ast.Name) and (
            node.func.id in self.imports.safe_names or node.func.id in self.safe_aliases
        )
        safe_module_call = any(
            dotted == f"{module}.{name}"
            for module in self.imports.safe_modules
            for name in SAFE_READ_NAMES
        )
        if safe_name_call or safe_module_call:
            self.summary.safe_reads.add(leaf)
        elif alias_readers:
            pass
        elif leaf in SAFE_READ_NAMES:
            self.summary.readers.add(f"untrusted-{leaf}")
        elif leaf in DIRECT_READ_METHODS:
            self.summary.readers.add(leaf)
        elif leaf == "open":
            os_open = dotted in {
                f"{module}.open" for module in self.imports.os_modules
            }
            if (self._os_open_reads(node) if os_open else self._open_reads(node)):
                self.summary.readers.add("open(read)")
        elif (
            leaf in COPY_READERS
            and (
                any(dotted.startswith(f"{module}.") for module in self.imports.shutil_modules)
                or imported_copy == leaf
            )
        ):
            self.summary.readers.add(leaf)
        elif dotted.endswith("subprocess.run") or dotted.endswith("subprocess.Popen"):
            literals = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            if "--stdin-paths" in literals:
                self.summary.readers.add("git hash-object --stdin-paths")

        if isinstance(node.func, ast.Name):
            self.summary.callees.update(self._resolve_local_callable(node.func))
        elif isinstance(node.func, ast.Attribute):
            receiver = node.func.value
            if (
                self.current_class
                and isinstance(receiver, ast.Name)
                and receiver.id in {"self", "cls"}
            ):
                exact = f"{self.current_class}.{node.func.attr}"
                if exact in self.methods.get(node.func.attr, set()):
                    self.summary.callees.add(exact)
                else:
                    # The method may be inherited from a local base class. A
                    # same-leaf fallback is conservative and avoids silently
                    # treating inherited raw readers as safe.
                    self.summary.callees.update(self.methods.get(node.func.attr, set()))
            else:
                # Dynamic receivers cannot be resolved exactly without executing
                # the program. Follow every same-leaf local method so ambiguity
                # fails safe instead of selecting whichever AST node came last.
                self.summary.callees.update(self.methods.get(node.func.attr, set()))
        self.generic_visit(node)


def _direct_summary(
    node: ast.AST,
    current_scope: str,
    current_class: str | None,
    direct_functions: dict[str, set[str]],
    methods: dict[str, set[str]],
    imports: _Imports,
) -> _Summary:
    visitor = _BodyVisitor(
        current_scope,
        current_class,
        direct_functions,
        methods,
        imports,
    )
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for statement in node.body:
            visitor.visit(statement)
    else:
        visitor.visit(node)
    return visitor.summary


def _function_nodes(tree: ast.Module) -> tuple[dict[str, ast.AST], dict[str, set[str]], dict[str, set[str]]]:
    """Return qualified local functions plus conservative call-resolution maps."""
    functions: dict[str, ast.AST] = {}
    direct: dict[str, set[str]] = {}
    methods: dict[str, set[str]] = {}

    def add_function(node: ast.FunctionDef | ast.AsyncFunctionDef, prefix: str, class_name: str | None) -> None:
        qualname = f"{prefix}.{node.name}" if prefix else node.name
        functions[qualname] = node
        if class_name:
            methods.setdefault(node.name, set()).add(qualname)
        else:
            direct.setdefault(node.name, set()).add(qualname)
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add_function(child, f"{qualname}.<locals>", None)
            elif isinstance(child, ast.ClassDef):
                add_class(child, f"{qualname}.<locals>")

    def add_class(node: ast.ClassDef, prefix: str = "") -> None:
        class_name = f"{prefix}.{node.name}" if prefix else node.name
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add_function(child, class_name, class_name)
            elif isinstance(child, ast.ClassDef):
                add_class(child, class_name)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add_function(node, "", None)
        elif isinstance(node, ast.ClassDef):
            add_class(node)
    return functions, direct, methods


def _imports(tree: ast.Module, local_function_names: set[str]) -> _Imports:
    imports = _Imports()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                if alias.name == "os":
                    imports.os_modules.add(local)
                elif alias.name == "ast":
                    imports.ast_modules.add(local)
                elif alias.name == "shutil":
                    imports.shutil_modules.add(local)
                elif alias.name == "_lib.safe_read" or alias.name.endswith("._lib.safe_read"):
                    imports.safe_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "os":
                for alias in node.names:
                    if alias.name in {"walk", "fwalk"}:
                        imports.os_walk_names.add(alias.asname or alias.name)
            elif module == "shutil":
                for alias in node.names:
                    if alias.name in COPY_READERS | {"copytree"}:
                        imports.shutil_copy_names[alias.asname or alias.name] = alias.name
            canonical_safe_module = bool(
                module == "_lib.safe_read"
                or module.endswith("._lib.safe_read")
                or (node.level == 1 and module == "safe_read")
            )
            if canonical_safe_module:
                for alias in node.names:
                    if alias.name in SAFE_READ_NAMES:
                        imports.safe_names.add(alias.asname or alias.name)
    # A local function with the same name shadows the imported primitive. It is
    # not a trusted boundary merely because a canonical import also exists.
    imports.safe_names.difference_update(local_function_names)
    return imports


def evaluate(source: str, filename: str = "<memory>") -> dict:
    """Return AST findings without touching the filesystem."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return {"ok": False, "parse_error": f"line {exc.lineno}: {exc.msg}", "findings": []}

    functions, direct_functions, methods = _function_nodes(tree)
    imports = _imports(tree, set(direct_functions) | set(methods))

    summaries = {
        name: _direct_summary(
            node,
            name,
            name.rsplit(".", 1)[0] if "." in name and "<locals>" not in name else None,
            direct_functions,
            methods,
            imports,
        )
        for name, node in functions.items()
    }
    module = ast.Module(
        body=[n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))],
        type_ignores=[],
    )
    summaries["<module>"] = _direct_summary(
        module,
        "<module>",
        None,
        direct_functions,
        methods,
        imports,
    )

    direct = {name: _Summary(set(s.walkers), set(s.readers), set(s.safe_reads), set(s.callees))
              for name, s in summaries.items()}
    changed = True
    while changed:
        changed = False
        for name, summary in summaries.items():
            for callee in list(summary.callees):
                target = summaries.get(callee)
                if target is not None and summary.merge(target):
                    changed = True

    findings = []
    for name, summary in summaries.items():
        if summary.walkers and summary.readers:
            findings.append({
                "scope": name,
                "walkers": sorted(summary.walkers),
                "readers": sorted(summary.readers),
            })

    # Report the narrowest scopes. A caller inherits a helper's exact violation;
    # repeating both adds noise without changing the remediation.
    direct_violations = {
        name for name, summary in direct.items()
        if summary.walkers and summary.readers
    }
    if direct_violations:
        findings = [f for f in findings if f["scope"] in direct_violations]
    return {"ok": not findings, "parse_error": None, "findings": findings}


def normalize(source: str) -> str:
    """CRLF/CR -> LF.

    The ratchet hash MUST be identical on every checkout or the pin is useless.
    This repo has no .gitattributes, so a Windows clone with core.autocrlf=true
    has CRLF on disk while the Linux CI runner has LF.  A raw-decode hash pinned
    on one platform is 100% stale on the other: every reviewed row reds the gate
    with no real violation behind it, which trains maintainers to ignore it.
    Normalizing makes the pin describe the CONTENT, not the checkout.
    """
    return source.replace("\r\n", "\n").replace("\r", "\n")


def check_file(path: Path) -> tuple[int, dict]:
    result = safe_read_text(path, timeout=5.0, max_bytes=1_000_000)
    if not result.ok:
        return 2, {"ok": False, "read_error": result.status, "findings": []}
    source = normalize(result.text or "")
    verdict = evaluate(source, str(path))
    verdict["sha256"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if verdict.get("parse_error"):
        return 2, verdict
    return (0 if verdict["ok"] else 1), verdict


def _load_baseline(path: Path) -> dict[str, str]:
    result = safe_read_text(path, timeout=5.0, max_bytes=1_000_000)
    if not result.ok:
        raise ValueError(f"baseline {path} is {result.status}")
    entries: dict[str, str] = {}
    for line_no, raw in enumerate((result.text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise ValueError(f"invalid baseline row {path}:{line_no}")
        entries[parts[1]] = parts[0]
    return entries


def check_all(root: Path, baseline_path: Path) -> int:
    """Audit every tracked/untracked product Python file with a hash ratchet."""
    try:
        baseline = _load_baseline(baseline_path)
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others",
             "--exclude-standard", "-z", "--", "*.py"],
            capture_output=True,
            timeout=20,
        )
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR cloud-safe walker fleet: {exc}", file=sys.stderr)
        return 2
    if proc.returncode != 0:
        print("ERROR cloud-safe walker fleet: git ls-files failed", file=sys.stderr)
        return 2

    relative_files = sorted({
        os.fsdecode(item)
        for item in proc.stdout.split(b"\x00")
        if item
    })
    current_unsafe: dict[str, str] = {}
    failures = 0
    errors = 0
    for rel in relative_files:
        rc, verdict = check_file(root / rel)
        if rc == 2:
            errors += 1
            print(f"ERROR {rel}: {verdict.get('read_error') or verdict.get('parse_error')}", file=sys.stderr)
            continue
        if rc == 0:
            continue
        digest = verdict["sha256"]
        current_unsafe[rel] = digest
        if baseline.get(rel) == digest:
            continue
        failures += 1
        for finding in verdict["findings"]:
            print(
                f"UNSAFE {rel}:{finding['scope']}: recursive {','.join(finding['walkers'])} "
                f"reaches {','.join(finding['readers'])} without shared safe_read",
                file=sys.stderr,
            )

    stale = sorted(set(baseline) - {
        rel for rel, digest in current_unsafe.items() if baseline.get(rel) == digest
    })
    for rel in stale:
        failures += 1
        print(
            f"STALE BASELINE {rel}: file changed, became clean, or was removed; "
            "adopt safe_read or refresh/remove this reviewed row",
            file=sys.stderr,
        )

    if errors:
        return 2
    if failures:
        return 1
    print(
        f"CLEAN fleet: {len(relative_files)} Python files; "
        f"{len(current_unsafe)} content-pinned legacy exception(s)"
    )
    return 0


def self_test() -> int:
    cases = [
        (
            "unsafe direct read",
            "from pathlib import Path\n"
            "def scan(root):\n"
            "    for path in Path(root).rglob('*.md'):\n"
            "        path.read_text()\n",
            False,
        ),
        (
            "unsafe helper read",
            "import os\n"
            "def load(path): return open(path).read()\n"
            "def scan(root):\n"
            "    for base, dirs, files in os.walk(root):\n"
            "        for name in files: load(os.path.join(base, name))\n",
            False,
        ),
        (
            "unsafe helper through local callable alias",
            "import os\n"
            "def raw(path): return open(path).read()\n"
            "def scan(root):\n"
            "    reader = raw\n"
            "    for base, dirs, files in os.walk(root): reader(files[0])\n",
            False,
        ),
        (
            "conditional reassignment preserves possible raw helper",
            "import os\nfrom _lib.safe_read import safe_read_text\n"
            "def raw(path): return open(path).read()\n"
            "def scan(root, safe):\n"
            "    reader = raw\n"
            "    if safe: reader = safe_read_text\n"
            "    for base, dirs, files in os.walk(root): reader(files[0])\n",
            False,
        ),
        (
            "built-in open callable alias is a reader",
            "import os\n"
            "def scan(root):\n"
            "    reader = open\n"
            "    for base, dirs, files in os.walk(root): reader(files[0]).read()\n",
            False,
        ),
        (
            "os.walk callable alias is recursive",
            "import os\n"
            "def scan(root):\n"
            "    walker = os.walk\n"
            "    for base, dirs, files in walker(root): open(files[0]).read()\n",
            False,
        ),
        (
            "shared primitive",
            "import os\nfrom _lib.safe_read import safe_read_text\n"
            "def scan(root):\n"
            "    for base, dirs, files in os.walk(root):\n"
            "        for name in files: safe_read_text(os.path.join(base, name))\n",
            True,
        ),
        (
            "shared primitive through helper",
            "import os\nfrom _lib.safe_read import safe_read_bytes\n"
            "def load(path): return safe_read_bytes(path)\n"
            "def scan(root):\n"
            "    for base, dirs, files in os.walk(root):\n"
            "        for name in files: load(os.path.join(base, name))\n",
            True,
        ),
        (
            "lookalike helper is not shared primitive",
            "import os\n"
            "def safe_read_text(path): return open(path).read()\n"
            "def scan(root):\n"
            "    for base, dirs, files in os.walk(root):\n"
            "        for name in files: safe_read_text(os.path.join(base, name))\n",
            False,
        ),
        (
            "mixed safe and raw reads still fail",
            "import os\nfrom _lib.safe_read import safe_read_text\n"
            "def scan(root):\n"
            "    for base, dirs, files in os.walk(root):\n"
            "        for name in files:\n"
            "            safe_read_text(os.path.join(base, name))\n"
            "            open(os.path.join(base, name)).read()\n",
            False,
        ),
        (
            "class helper raw read is followed",
            "import os\n"
            "class Scanner:\n"
            "    def load(self, path): return open(path).read()\n"
            "    def scan(self, root):\n"
            "        for base, dirs, files in os.walk(root):\n"
            "            for name in files: self.load(os.path.join(base, name))\n",
            False,
        ),
        (
            "Path instance walk is recursive",
            "from pathlib import Path\n"
            "def scan(root):\n"
            "    for base, dirs, files in Path(root).walk():\n"
            "        Path(base, files[0]).read_text()\n",
            False,
        ),
        (
            "assigned Path walk is recursive",
            "from pathlib import Path\n"
            "def scan(root):\n"
            "    tree = Path(root)\n"
            "    for base, dirs, files in tree.walk():\n"
            "        open(Path(base, files[0])).read()\n",
            False,
        ),
        (
            "aliased os module walk is recursive",
            "import os as filesystem\n"
            "def scan(root):\n"
            "    for base, dirs, files in filesystem.walk(root):\n"
            "        open(files[0]).read()\n",
            False,
        ),
        (
            "from-imported os walk is recursive",
            "from os import walk as traverse\n"
            "def scan(root):\n"
            "    for base, dirs, files in traverse(root):\n"
            "        open(files[0]).read()\n",
            False,
        ),
        (
            "copytree is an internal recursive reader",
            "import shutil\n"
            "def clone(source, target): shutil.copytree(source, target)\n",
            False,
        ),
        (
            "from-imported recursive glob is followed",
            "from glob import glob\n"
            "def scan(root):\n"
            "    for path in glob(root + '/**/*', recursive=True): open(path).read()\n",
            False,
        ),
        (
            "duplicate class method leaves resolve by owning class",
            "import os\n"
            "class Unsafe:\n"
            "    def load(self, path): return open(path).read()\n"
            "    def scan(self, root):\n"
            "        for base, dirs, files in os.walk(root): self.load(files[0])\n"
            "class Safe:\n"
            "    def load(self, path): return path\n",
            False,
        ),
        (
            "inherited class helper raw read is followed",
            "import os\n"
            "class Base:\n"
            "    def load(self, path): return open(path).read()\n"
            "class Scanner(Base):\n"
            "    def scan(self, root):\n"
            "        for base, dirs, files in os.walk(root): self.load(files[0])\n",
            False,
        ),
        (
            "bogus safe_read module is not trusted",
            "import os\nfrom bogus.safe_read import safe_read_text\n"
            "def scan(root):\n"
            "    for base, dirs, files in os.walk(root):\n"
            "        for name in files: safe_read_text(os.path.join(base, name))\n",
            False,
        ),
        (
            "metadata-only walker",
            "import os\n"
            "def count(root):\n"
            "    total = 0\n"
            "    for base, dirs, files in os.walk(root): total += len(files)\n"
            "    return total\n",
            True,
        ),
        (
            "write-only fixture",
            "import os\n"
            "def seed(root):\n"
            "    for base, dirs, files in os.walk(root):\n"
            "        open(os.path.join(base, 'marker'), 'w').write('x')\n",
            True,
        ),
        (
            "write-only os.open fixture",
            "import os\n"
            "def seed(root):\n"
            "    for base, dirs, files in os.walk(root):\n"
            "        fd = os.open(os.path.join(base, 'marker'), os.O_WRONLY | os.O_CREAT)\n"
            "        os.close(fd)\n",
            True,
        ),
        (
            "conditional os.open flags remain a possible read",
            "import os\n"
            "def scan(root, write):\n"
            "    for base, dirs, files in os.walk(root):\n"
            "        os.open(files[0], os.O_WRONLY if write else os.O_RDONLY)\n",
            False,
        ),
        (
            "directory os.open with read-write flags remains a possible read",
            "import os\n"
            "def scan(root):\n"
            "    for base, dirs, files in os.walk(root):\n"
            "        os.open(base, os.O_DIRECTORY | os.O_RDWR)\n",
            False,
        ),
    ]
    failed = 0
    for label, source, expected in cases:
        actual = bool(evaluate(source)["ok"])
        passed = actual == expected
        print(("PASS" if passed else "FAIL"), label)
        failed += 0 if passed else 1
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", nargs="+", type=Path)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=ROOT / "scripts" / "cloud-safe-walker-baseline.txt",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.all:
        return check_all(args.root.resolve(), args.baseline.resolve())
    if not args.check:
        parser.error("one of --check, --all, or --self-test is required")

    worst = 0
    for path in args.check:
        rc, verdict = check_file(path)
        worst = max(worst, rc)
        if rc == 0:
            print(f"CLEAN {path}")
        elif rc == 2:
            reason = verdict.get("parse_error") or verdict.get("read_error") or "unknown"
            print(f"ERROR {path}: {reason}", file=sys.stderr)
        else:
            for finding in verdict["findings"]:
                print(
                    f"UNSAFE {path}:{finding['scope']}: recursive {','.join(finding['walkers'])} "
                    f"reaches {','.join(finding['readers'])} without safe_read",
                    file=sys.stderr,
                )
    return worst


if __name__ == "__main__":
    sys.exit(main())
