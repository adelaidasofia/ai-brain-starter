#!/usr/bin/env python3
"""
vault-schema-validator.py — frontmatter linter for vault files.

Validates YAML frontmatter in Decisions/, Sessions/, journal entries against
JSON schemas at templates/schemas/*.json. Catches malformed YAML BEFORE the
aggregators silently drop the file.

Same permanent-fix pattern that saved settings.json from the duplicate-key
bug: detect at the boundary, fail loud, never silently rot.

Usage:
  python3 scripts/vault-schema-validator.py [--vault-root PATH] [--type TYPE]
                                            [--file PATH] [--strict] [--fix]
                                            [--quiet]

Modes:
  --type {decision|session|journal|all}    which schemas to apply (default: all)
  --file PATH                              validate ONE specific file
  --vault-root PATH                        override vault root (default: $VAULT_ROOT or cwd)
  --strict                                 exit 2 on any error (for CI / PreToolUse)
  --fix                                    attempt safe auto-repairs (no destructive edits)
  --quiet                                  only print summary line
  --self-test                              run built-in fixture tests, exit 0/1

Output (default):
  - One line per file with errors
  - Summary at end
  - Exit 0 if clean, 1 if warnings only, 2 if --strict and any errors

Why this script exists: malformed YAML in a Decisions/ or Sessions/ frontmatter
silently breaks the aggregator. A YAML parse error produces no entry in
Last Session.md or Decision Log.md, and the user finds out weeks later when
they notice the file is missing from the rebuilt view. Same failure mode that
silently nukes a CRM record when a YAML parser swallows the error and the
write path re-marshals empty data over real content.

This script is the write-boundary defense. The companion PreToolUse hook
(hooks/lint-vault-frontmatter.py) runs this on every Write/Edit to catch
violations before they land.
"""
# exit-contract: ENFORCING


from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def log_debug(msg: str) -> None:
    if os.environ.get("VAULT_VALIDATOR_DEBUG") == "1":
        print(f"[vault-schema-validator] {msg}", file=sys.stderr)


def find_schemas_dir() -> Path:
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "templates" / "schemas",
        Path.home() / ".claude" / "skills" / "ai-brain-starter" / "templates" / "schemas",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    raise FileNotFoundError("templates/schemas/ not found")


def load_schema(schema_path: Path) -> dict:
    # Bounded read (shared safe_read): a schema on a cloud-synced vault could be a
    # placeholder or a stalled mount; never block on it (cloud-safe walker ratchet).
    res = safe_read_text(schema_path, timeout=5.0, max_bytes=1_000_000)
    if not res.ok:
        raise OSError(f"cannot read schema {schema_path}: {res.status} {res.detail}")
    return json.loads(res.text or "")


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _meta_resolver import find_meta_dir as _find_meta_dir_helper  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from _lib.safe_read import safe_read_text  # noqa: E402


def find_meta_dir(vault: Path) -> Path:
    return _find_meta_dir_helper(vault) or (vault / "Meta")


def find_journals_dir(vault: Path) -> Path | None:
    """Auto-detect Journals folder (with or without emoji prefix)."""
    if not vault.is_dir():
        return None
    for child in sorted(vault.iterdir()):
        if child.is_dir() and ("Journal" in child.name or "Daily Logs" in child.name):
            return child
    return None


def extract_frontmatter(text: str) -> tuple[str | None, str | None]:
    """Return (frontmatter_text, error_message_or_None)."""
    if not text.startswith("---"):
        return (None, None)  # no frontmatter, that's fine for many files
    # \r?\n throughout: this text arrives from safe_read_text, which decodes
    # bytes directly with no universal-newline translation. A vault authored or
    # touched on Windows genuinely contains CRLF on disk (the session-close
    # cascade's own pre-built session shell is one), so an LF-only pattern
    # reports perfectly valid frontmatter as unterminated.
    m = re.match(r"^---\r?\n(.*?)\r?\n---\s*", text, re.DOTALL)
    if not m:
        return (None, "frontmatter delimiter '---' not properly closed")
    return (m.group(1), None)


def _stringify_dates(obj):
    """Walk a parsed YAML structure and convert datetime.date/datetime objects
    to ISO-format strings. PyYAML auto-converts `2026-04-30` to date(), which
    breaks our schema's `type: string` checks. Treat dates as strings here."""
    import datetime as _dt
    if isinstance(obj, dict):
        return {k: _stringify_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_stringify_dates(v) for v in obj]
    if isinstance(obj, _dt.datetime):
        return obj.isoformat()
    if isinstance(obj, _dt.date):
        return obj.isoformat()
    return obj


def parse_yaml_safe(yaml_text: str) -> tuple[dict | None, str | None]:
    """Parse YAML, return (data_or_None, error_or_None)."""
    try:
        import yaml  # PyYAML
    except ImportError:
        return (None, "PyYAML not installed (pip install pyyaml)")
    try:
        data = yaml.safe_load(yaml_text)
        if data is None:
            return ({}, None)
        if not isinstance(data, dict):
            return (None, f"frontmatter must be a YAML mapping, got {type(data).__name__}")
        data = _stringify_dates(data)
        return (data, None)
    except yaml.YAMLError as e:
        return (None, f"YAML parse error: {e}")


def validate_against_schema(data: dict, schema: dict) -> list[str]:
    """Lightweight JSON-schema validator (no jsonschema dependency).

    Supports: type, const, enum, pattern, required, properties, oneOf,
    minimum, maximum, minLength, items, minItems, maxItems. Sufficient for
    our schemas; no need to pull in the full jsonschema package.
    """
    errors: list[str] = []
    return _validate(data, schema, "", errors) or errors


def _validate(value: Any, schema: dict, path: str, errors: list[str]) -> list[str]:
    if "oneOf" in schema:
        any_pass = False
        for sub in schema["oneOf"]:
            sub_errs: list[str] = []
            _validate(value, sub, path, sub_errs)
            if not sub_errs:
                any_pass = True
                break
        if not any_pass:
            errors.append(f"{path or '(root)'}: failed all oneOf alternatives")
        return errors

    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        type_map = {
            "string": str, "integer": int, "number": (int, float),
            "boolean": bool, "array": list, "object": dict, "null": type(None),
        }
        valid = any(
            isinstance(value, type_map[t])
            for t in types if t in type_map
        )
        if not valid:
            errors.append(f"{path or '(root)'}: expected type {types}, got {type(value).__name__}")
            return errors

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path or '(root)'}: must equal {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path or '(root)'}: must be one of {schema['enum']}, got {value!r}")
    if "pattern" in schema and isinstance(value, str):
        if not re.search(schema["pattern"], value):
            errors.append(f"{path or '(root)'}: does not match pattern {schema['pattern']}")
    if "minimum" in schema and isinstance(value, (int, float)):
        if value < schema["minimum"]:
            errors.append(f"{path or '(root)'}: {value} < minimum {schema['minimum']}")
    if "maximum" in schema and isinstance(value, (int, float)):
        if value > schema["maximum"]:
            errors.append(f"{path or '(root)'}: {value} > maximum {schema['maximum']}")
    if "minLength" in schema and isinstance(value, str):
        if len(value) < schema["minLength"]:
            errors.append(f"{path or '(root)'}: length {len(value)} < minLength {schema['minLength']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path or '(root)'}: {len(value)} items < minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path or '(root)'}: {len(value)} items > maxItems {schema['maxItems']}")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                _validate(item, schema["items"], f"{path}[{i}]", errors)

    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path or '(root)'}: missing required field '{req}'")
        for key, subschema in (schema.get("properties") or {}).items():
            if key in value:
                _validate(value[key], subschema, f"{path}.{key}" if path else key, errors)

    return errors


def validate_file(file_path: Path, schema: dict, schema_name: str, fix: bool = False) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for one file."""
    errors: list[str] = []
    warnings: list[str] = []
    # Bounded read (shared safe_read): the linter walks the vault, which may live
    # on a cloud mount; a per-file daemon+timeout means one placeholder can never
    # wedge the whole lint (cloud-safe-file-walkers ratchet).
    res = safe_read_text(file_path, timeout=5.0, max_bytes=1_000_000)
    if not res.ok:
        return ([f"{file_path}: cannot read ({res.status})"], [])
    text = res.text or ""
    fm, fm_err = extract_frontmatter(text)
    if fm_err:
        errors.append(f"{file_path}: {fm_err}")
        return (errors, warnings)
    if fm is None:
        # No frontmatter — ok for many files; only warn for type-required files
        if schema.get("required") and "type" in schema["required"]:
            warnings.append(f"{file_path}: missing frontmatter (expected for {schema_name})")
        return (errors, warnings)
    data, parse_err = parse_yaml_safe(fm)
    if parse_err:
        errors.append(f"{file_path}: {parse_err}")
        return (errors, warnings)
    file_errors = validate_against_schema(data, schema)
    for e in file_errors:
        errors.append(f"{file_path}: {e}")
    return (errors, warnings)


def discover_files(vault: Path, type_name: str) -> list[Path]:
    """Find files of a given type across the vault."""
    out = []
    if type_name == "decision":
        meta = find_meta_dir(vault)
        d = meta / "Decisions"
        if d.is_dir():
            out.extend(d.glob("*.md"))
    elif type_name == "session":
        meta = find_meta_dir(vault)
        d = meta / "Sessions"
        if d.is_dir():
            out.extend(d.glob("*.md"))
    elif type_name == "journal":
        j = find_journals_dir(vault)
        if j:
            out.extend(j.rglob("*.md"))
    return [f for f in out if f.is_file() and not f.name.startswith(".")]


def run_self_test() -> int:
    """Execute built-in fixtures. Exit 0 = pass, 1 = fail."""
    schemas_dir = find_schemas_dir()
    fixtures = [
        # (description, frontmatter, schema_name, should_pass)
        (
            "valid decision",
            "type: decision\ndecision_date: 2026-04-30\nstakes: high\nspeed: deliberate\nfloor: 16\n",
            "decision", True,
        ),
        (
            "decision missing type",
            "decision_date: 2026-04-30\nstakes: high\n",
            "decision", False,
        ),
        (
            "decision malformed YAML (unclosed bracket)",
            "type: decision\ndecision_date: 2026-04-30\ntags: [foo, bar\n",
            "decision", False,
        ),
        (
            "decision invalid stakes enum",
            "type: decision\nstakes: extreme\n",
            "decision", False,
        ),
        (
            "decision floor as string (Peace)",
            "type: decision\nfloor: Peace\n",
            "decision", True,
        ),
        (
            "session valid",
            'type: session\nworktree: main\nsession_date: 2026-04-30\nsession_label: "wrap"\n',
            "session", True,
        ),
        (
            "session invalid date format",
            "type: session\nsession_date: April 30 2026\n",
            "session", False,
        ),
        (
            "journal floor numeric",
            "creationDate: 2026-04-30\nfloor: 16\nenergy: 7\n",
            "journal", True,
        ),
        (
            "journal floor out of range",
            "creationDate: 2026-04-30\nfloor: 99\n",
            "journal", False,
        ),
        (
            "journal floor as array (elevator emotion)",
            "creationDate: 2026-04-30\nfloor: [Gratitude, Excitement]\n",
            "journal", True,
        ),
        (
            "journal floor empty array (rejected)",
            "creationDate: 2026-04-30\nfloor: []\n",
            "journal", False,
        ),
    ]
    schemas = {
        "decision": load_schema(schemas_dir / "decision.json"),
        "session": load_schema(schemas_dir / "session.json"),
        "journal": load_schema(schemas_dir / "journal.json"),
    }
    failures = 0
    for desc, fm_text, schema_name, should_pass in fixtures:
        data, parse_err = parse_yaml_safe(fm_text)
        if parse_err:
            actual_pass = False
        else:
            errs = validate_against_schema(data or {}, schemas[schema_name])
            actual_pass = len(errs) == 0
        if actual_pass != should_pass:
            failures += 1
            print(f"FAIL [{desc}]: expected pass={should_pass}, got pass={actual_pass}")
        else:
            print(f"PASS [{desc}]")
    # Line-ending regression (the bug behind the Windows total-denial incident).
    # safe_read_text decodes raw bytes with NO universal-newline translation, so a
    # file authored on Windows -- or a temp file written in text mode -- reaches
    # extract_frontmatter with \r\n intact. An LF-only delimiter pattern rejected
    # it as "not properly closed", which denied EVERY Write/Edit of a vault .md
    # file on Windows regardless of content.
    #
    # Asserting "no error" alone is not enough: a pattern that matched but captured
    # the wrong span would still look clean. Assert the captured frontmatter too.
    body = "creationDate: 2026-04-30\nfloor: 16"
    line_ending_cases = [
        ("LF line endings", "---\n" + body + "\n---\n\nbody\n"),
        ("CRLF line endings", ("---\n" + body + "\n---\n\nbody\n").replace("\n", "\r\n")),
    ]
    for desc, text in line_ending_cases:
        fm, err = extract_frontmatter(text)
        normalized = (fm or "").replace("\r\n", "\n")
        if err or fm is None:
            failures += 1
            print(f"FAIL [{desc}]: {err or 'no frontmatter extracted'}")
        elif normalized != body:
            failures += 1
            print(f"FAIL [{desc}]: captured {normalized!r}, expected {body!r}")
        else:
            print(f"PASS [{desc}]")

    total = len(fixtures) + len(line_ending_cases)
    if failures:
        print(f"\n{failures}/{total} fixtures failed.")
        return 1
    print(f"\n{total}/{total} fixtures passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    # vault-root-ok: CLI default for an explicit --vault-root flag on a standalone
    # validator that checks ANY vault by path. This script ships in the skill dir and
    # is never itself inside a vault, so a location-derived _resolve_vault_root() would
    # resolve to the skill rather than to the vault under test. In --file mode (how
    # lint-vault-frontmatter.py invokes it) the value is computed but never read: the
    # --file branch resolves the target path directly. Caller always wins.
    ap.add_argument("--vault-root", default=os.environ.get("VAULT_ROOT", os.getcwd()))
    ap.add_argument("--type", choices=["decision", "session", "journal", "all"], default="all")
    ap.add_argument("--file", help="validate one specific file")
    ap.add_argument("--strict", action="store_true", help="exit 2 on any error")
    ap.add_argument("--fix", action="store_true", help="attempt safe auto-repairs (placeholder)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--self-test", action="store_true", help="run built-in fixtures and exit")
    args = ap.parse_args()

    if args.self_test:
        return run_self_test()

    try:
        schemas_dir = find_schemas_dir()
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    schemas = {
        "decision": load_schema(schemas_dir / "decision.json"),
        "session": load_schema(schemas_dir / "session.json"),
        "journal": load_schema(schemas_dir / "journal.json"),
    }

    vault = Path(args.vault_root).resolve()

    if args.file:
        path = Path(args.file).resolve()
        # Auto-detect type from path if --type is 'all'
        type_name = args.type
        if type_name == "all":
            if "Decisions" in path.parts:
                type_name = "decision"
            elif "Sessions" in path.parts:
                type_name = "session"
            elif any("Journal" in p or "Daily Logs" in p for p in path.parts):
                type_name = "journal"
            else:
                if not args.quiet:
                    print(f"{path}: skipped (type unknown)")
                return 0
        errs, warns = validate_file(path, schemas[type_name], type_name, fix=args.fix)
        for e in errs:
            print(f"ERROR: {e}")
        for w in warns:
            print(f"WARN: {w}")
        if errs and args.strict:
            return 2
        return 1 if errs or warns else 0

    # Vault-wide scan
    types = ["decision", "session", "journal"] if args.type == "all" else [args.type]
    total_errors = 0
    total_warnings = 0
    files_scanned = 0
    for t in types:
        files = discover_files(vault, t)
        for f in files:
            files_scanned += 1
            errs, warns = validate_file(f, schemas[t], t, fix=args.fix)
            total_errors += len(errs)
            total_warnings += len(warns)
            if not args.quiet:
                for e in errs:
                    print(f"ERROR: {e}")
                for w in warns:
                    print(f"WARN: {w}")

    summary = f"\nScanned {files_scanned} files. Errors: {total_errors}. Warnings: {total_warnings}."
    print(summary)

    if total_errors and args.strict:
        return 2
    return 1 if total_errors or total_warnings else 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
