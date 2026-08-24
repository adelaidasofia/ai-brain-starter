#!/usr/bin/env python3
"""check-vault-root-reads.py - ban naive VAULT_ROOT env reads in vault code.

THE BUG CLASS THIS EXISTS FOR
    `VAULT_ROOT` is routinely exported machine-wide (a shell profile, or Claude
    Code's settings.json `env` block, so every hook subprocess always sees it).
    It names exactly ONE vault. Any code that reads it naively therefore has two
    distinct failure modes, and BOTH are silent:

      1. UNSET  -> the near-universal default `str(Path.home() / "vault")` is
         wrong for every vault not literally named "vault". A guard written this
         way is inert on essentially every install, and says nothing while it is.
      2. SET    -> the env var wins over the vault the caller actually means. A
         file written into vault B resolves against vault A, the containment
         check returns False, and the guard skips the very file it exists for.

    Caught live on hooks/validate-handoff-frontmatter.py (#375/#404), which read
    the env var with a ~/vault default and was inert everywhere. Two unrelated
    users had already hand-patched it identically, each hardcoding their own
    absolute vault path -- when two people patch a defect the same way, the
    upstream default is the bug.

    MYC-2439 fixed 3 scripts; MYC-2504 fixes 5 more. Both are INSTANCE
    remediation. This is the class-level watchdog: the third layer of
    immediate remediation -> per-script hardening -> class-level guard, and the
    twin of scripts/check-meta-resolution.sh (the naive `*Meta` glob ban).

THE RULE
    A read of the VAULT_ROOT environment variable
        os.environ.get("VAULT_ROOT", ...) / os.getenv("VAULT_ROOT")
        os.environ["VAULT_ROOT"]
    is a VIOLATION unless one of the following is true:

      (1) it is lexically inside a sanctioned resolver definition (SANCTIONED
          below) -- the one place allowed to decide what the env var means;
      (2) it is an ARGUMENT to a sanctioned resolver call, e.g.
              resolve_vault_root(cwd, os.environ.get("VAULT_ROOT"))
          which is the hooks/_lib/vault_root.py contract: the env var is the
          FALLBACK, behind detection, never the primary signal;
      (3) it carries an explicit `# vault-root-ok: <reason>` exemption comment
          on the read's own line, the line above, or any line of a multi-line
          call. An empty reason is itself a violation -- an exemption with no
          argument is a rubber stamp.

    The sanctioned patterns differ by surface, and the difference matters:
      - A SCRIPT serves one vault, so `_resolve_vault_root()` prefers the
        script's own location and ignores a mismatched env var (aggregate-*.py).
      - A HOOK fires on files in ANY vault, so it must resolve PER FILE, from
        the target path, with the env var only as fallback
        (validate-handoff-frontmatter.py's vault_root_for). A hook is the
        higher-severity surface: a wrong root makes it fail silently OPEN.

WHY AST AND NOT GREP
    The twin is a regex over shell. This one is over Python, and Python lets the
    banned pattern appear legitimately as DATA -- in this module's own docstring,
    in a test fixture stored as a source string, in a comment explaining the bug.
    Parsing means a string is a string: only an executable read is ever flagged,
    so the guard needs no self-exclusion list and cannot be defeated by moving a
    fixture around. Real .py fixtures under tests/ are additionally out of the
    fleet scope (scripts/ + hooks/), so both fixture styles are safe.

THE LEGACY POPULATION
    This lint landed on a tree with a large pre-existing population (see
    scripts/vault-root-read-baseline.txt). Those rows are pinned by SHA-256, the
    same ratchet scripts/check-cloud-safe-file-walkers.py uses: a pinned file
    passes only while it is byte-identical, so ANY edit forces remediation, and
    a NEW file is never pinned and fails immediately. The baseline is the
    remediation backlog, tagged by severity -- not a set of silent pardons.

Modes:
    --all           fleet scan (scripts/ + hooks/) against the ratchet [default]
    --check PATH..  audit named files/dirs, no baseline (fixtures, tests)
    --report        enumerate every violation by tag, never fail
    --self-test     load-bearing positive/negative controls

Exit: 0 clean, 1 violation, 2 usage/IO error.

ASCII-only output on purpose -- see scripts/check-utf8-stdout.py.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO / "hooks"))

from _baseline_sections import check_read_counts, check_row_counts  # noqa: E402
from _lib.safe_read import safe_read_text  # noqa: E402

# The fleet this guard owns. scripts/ (vault-side CLIs) + hooks/ (the
# higher-severity surface: a hook fires on files in ANY vault). Git pathspec
# wildcards cross directory separators, so nested files are included.
FLEET_PATHSPECS = ("scripts/*.py", "hooks/*.py")

DEFAULT_BASELINE = REPO / "scripts" / "vault-root-read-baseline.txt"

# Functions allowed to read the env var, and calls allowed to receive it.
#
#   _resolve_vault_root  scripts/aggregate-{decisions,sessions}.py,
#                        scripts/decision-outcome-check.py -- prefers the
#                        script's own vault, ignores a mismatched env var
#                        unless VAULT_ROOT_FORCE=1.
#   resolve_vault_root   hooks/_lib/vault_root.py -- cwd-derived detection
#                        first, env var as fallback. Both the definition and a
#                        call site passing the env var IN are sanctioned.
#   vault_root_for       the per-file hook resolver
#                        (hooks/validate-handoff-frontmatter.py): detection
#                        from the TARGET FILE first, env var as fallback.
SANCTIONED = frozenset({
    "_resolve_vault_root",
    "resolve_vault_root",
    "vault_root_for",
    "_vault_root_for",
})

ENV_VAR = "VAULT_ROOT"

# `# vault-root-ok: <reason>`. The reason is mandatory: an exemption that does
# not say why is a rubber stamp, and is reported as its own violation kind.
EXEMPT_RE = re.compile(r"#\s*vault-root-ok\s*:\s*(?P<reason>.*)$")
MIN_REASON_CHARS = 3

# Baseline row: <sha256> <TAG> <path>. TAG is documented in the baseline header.
_SHA_LEN = 64


class Violation:
    __slots__ = ("path", "line", "kind", "snippet")

    def __init__(self, path: str, line: int, kind: str, snippet: str) -> None:
        self.path = path
        self.line = line
        self.kind = kind
        self.snippet = snippet

    def render(self) -> str:
        return f"  {self.path}:{self.line}: {self.kind}\n      {self.snippet}"


def _is_os_environ(node: ast.AST) -> bool:
    """True for `os.environ` or a bare `environ` (from os import environ)."""
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return isinstance(node.value, ast.Name) and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id == "environ"


def _module_str_constants(tree: ast.Module) -> dict:
    """Module-level `NAME = "literal"` bindings.

    Needed because the env var name is not always written inline. Real example:
    hooks/scan-prior-sessions-for-secrets.py binds `VAULT_ROOT_ENV = "VAULT_ROOT"`
    and reads `os.environ.get(VAULT_ROOT_ENV, "")`. A literal-only rule misses it,
    which would leave the guard with a one-line bypass -- exactly the false green
    this repo treats as worse than no guard at all.
    """
    consts: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        consts[target.id] = node.value.value
    return consts


def _const_str(node: ast.AST | None, consts: dict | None = None) -> str | None:
    """The string value of `node`: a literal, or a module-level string constant."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if consts and isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _reads_vault_root(node: ast.AST, consts: dict | None = None) -> bool:
    """True iff `node` is an executable read of the VAULT_ROOT env var.

    Name-equality (after resolving module-level string constants) means
    VAULT_ROOT_FORCE -- a different, benign knob -- is never matched, and a
    genuinely dynamic key is never guessed at.
    """
    if isinstance(node, ast.Call):
        func = node.func
        # os.environ.get("VAULT_ROOT", ...) / environ.get(...)
        if isinstance(func, ast.Attribute) and func.attr == "get" and _is_os_environ(func.value):
            return bool(node.args) and _const_str(node.args[0], consts) == ENV_VAR
        # os.getenv("VAULT_ROOT", ...) / getenv(...)
        is_getenv = (
            (isinstance(func, ast.Attribute) and func.attr == "getenv"
             and isinstance(func.value, ast.Name) and func.value.id == "os")
            or (isinstance(func, ast.Name) and func.id == "getenv")
        )
        if is_getenv:
            return bool(node.args) and _const_str(node.args[0], consts) == ENV_VAR
        return False
    # os.environ["VAULT_ROOT"] -- LOAD only. A STORE (os.environ["VAULT_ROOT"] = x)
    # SETS the variable for a child process; it is not a naive read of it.
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        if _is_os_environ(node.value):
            return _const_str(node.slice, consts) == ENV_VAR
    return False


def _sanctioned_call(node: ast.AST) -> bool:
    """True iff `node` is a call to a sanctioned resolver (env var as fallback)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return name in SANCTIONED


def _exemption(lines: list[str], start: int, end: int) -> tuple[bool, bool]:
    """Look for the exemption marker on the read, or in the comment block above it.

    The window is the read's own lines PLUS the contiguous run of comment-only
    lines immediately above it. A real justification rarely fits on one line, and
    requiring the marker to be the LAST line before the code would force every
    reason to be written upside down. A blank line or any code ends the block, so
    the marker still has to sit with the read it exempts.

    Returns (found, reason_ok). `found` without `reason_ok` is a bare marker --
    reported as its own violation kind, because an exemption that does not say
    why is a rubber stamp.
    """
    hi = min(len(lines), max(end, start))
    lo = start
    idx = start - 1
    while idx >= 1 and lines[idx - 1].lstrip().startswith("#"):
        lo = idx
        idx -= 1
    found = False
    for i in range(lo, hi + 1):
        m = EXEMPT_RE.search(lines[i - 1])
        if m:
            found = True
            if len(m.group("reason").strip()) >= MIN_REASON_CHARS:
                return True, True
    return found, False


def scan_source(source: str, rel: str) -> list[Violation]:
    """Every unsanctioned VAULT_ROOT read in `source`. Raises SyntaxError on bad input."""
    tree = ast.parse(source)
    lines = source.splitlines()
    consts = _module_str_constants(tree)
    found: list[Violation] = []

    def walk(node: ast.AST, in_resolver: bool, in_sanctioned_call: bool) -> None:
        child_resolver = in_resolver
        child_call = in_sanctioned_call

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in SANCTIONED:
                child_resolver = True
        elif _sanctioned_call(node):
            child_call = True

        if _reads_vault_root(node, consts):
            if not (in_resolver or in_sanctioned_call):
                start = node.lineno
                end = getattr(node, "end_lineno", None) or start
                has_marker, reason_ok = _exemption(lines, start, end)
                if not (has_marker and reason_ok):
                    kind = (
                        "vault-root-ok marker with no reason"
                        if has_marker else
                        "naive VAULT_ROOT read outside a sanctioned resolver"
                    )
                    snippet = lines[start - 1].strip() if start <= len(lines) else "?"
                    found.append(Violation(rel, start, kind, snippet))
            # A read is a leaf for this purpose; its own args cannot contain another.
            return

        for child in ast.iter_child_nodes(node):
            walk(child, child_resolver, child_call)

    walk(tree, False, False)
    found.sort(key=lambda v: v.line)
    return found


def normalize(source: str) -> str:
    """CRLF/CR -> LF.

    The baseline hash MUST be identical on every checkout or the ratchet is
    useless. This repo has no .gitattributes, so a Windows clone with
    core.autocrlf=true has CRLF on disk while the Linux CI runner has LF -- a
    raw-bytes hash pinned on one platform is 100% stale on the other, which
    reds the build for everyone with no real violation behind it. (Verified
    live: scripts/cloud-safe-walker-baseline.txt, which hashes unnormalized,
    reports every row stale on a Windows checkout today.) Normalizing makes
    the pin describe the CONTENT, not the checkout.
    """
    return source.replace("\r\n", "\n").replace("\r", "\n")


def scan_file(path: Path, rel: str) -> tuple[list[Violation], str]:
    """(violations, sha256-of-normalized-source) for one file.

    Reads through the shared bounded primitive (hooks/_lib/safe_read.py): this
    lint walks a whole tree, and an install living on a cloud-synced folder can
    hand back a placeholder file whose read blocks indefinitely. Raises OSError
    on an unreadable file so the caller can report it rather than skip silently.
    """
    result = safe_read_text(path, timeout=5.0, max_bytes=4_000_000)
    if not result.ok:
        raise OSError(f"unreadable ({result.status}{': ' + result.detail if result.detail else ''})")
    source = normalize(result.text or "")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return scan_source(source, rel), digest


# --------------------------------------------------------------------------
# baseline ratchet
# --------------------------------------------------------------------------

def load_baseline(path: Path) -> tuple[dict, str]:
    """(path -> (sha256, tag), the raw text). Raises ValueError on a malformed row.

    "Malformed" includes a section header whose declared FILE count no longer
    matches the rows beneath it. Those headers are the burn-down ledger a human
    reads to decide whether the backlog is shrinking, and nothing else here can
    notice when one goes stale: the loop below skips every `#` line, so a wrong
    count is invisible to the ratchet it annotates (see _baseline_sections.py).

    The text comes back because the READ counts in those same headers can only
    be verified after the scan -- check_all() does that once the ratchet is
    otherwise clean.
    """
    entries = {}
    if not path.is_file():
        raise ValueError(f"baseline not found: {path}")
    result = safe_read_text(path, timeout=5.0, max_bytes=1_000_000)
    if not result.ok:
        raise ValueError(f"baseline {path} is {result.status}")
    text = result.text or ""
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) != 3 or len(parts[0]) != _SHA_LEN:
            raise ValueError(f"invalid baseline row {path}:{line_no}: {raw!r}")
        entries[parts[2]] = (parts[0], parts[1])
    drift = check_row_counts(text, declares_reads=True)
    if drift:
        raise ValueError("stale section header(s) in {}:\n  {}".format(path, "\n  ".join(drift)))
    return entries, text


def fleet_files(root: Path) -> list[str]:
    # --others --exclude-standard so a brand-new, not-yet-committed script is in
    # scope too: the local pre-push gate must catch it before it is ever pushed,
    # not one commit later.
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
         "--exclude-standard", "--"] + list(FLEET_PATHSPECS),
        capture_output=True, timeout=30,
    )
    if proc.returncode != 0:
        raise OSError("git ls-files failed for the vault-root fleet")
    return sorted(os.fsdecode(item) for item in proc.stdout.split(b"\x00") if item)


def check_all(root: Path, baseline_path: Path, report_only: bool = False) -> int:
    try:
        baseline, baseline_text = ({}, "") if report_only else load_baseline(baseline_path)
        relative = fleet_files(root)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR vault-root fleet: {exc}", file=sys.stderr)
        return 2

    current: dict = {}
    unpinned: list[Violation] = []
    errors = 0

    for rel in relative:
        try:
            violations, digest = scan_file(root / rel, rel)
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            errors += 1
            print(f"ERROR {rel}: {exc}", file=sys.stderr)
            continue
        if not violations:
            continue
        current[rel] = (digest, violations)
        if report_only:
            continue
        pinned = baseline.get(rel)
        if pinned and pinned[0] == digest:
            continue
        unpinned.extend(violations)

    if report_only:
        total = sum(len(v) for _, v in current.values())
        print(f"vault-root read report: {len(current)} file(s), {total} unsanctioned read(s)")
        for rel in sorted(current):
            digest, violations = current[rel]
            print(f"  {rel}  [{digest[:12]}]")
            for v in violations:
                print(f"      L{v.line}: {v.snippet}")
        return 0

    if errors:
        return 2

    # A row is live only while its file still violates at exactly the pinned
    # bytes. Anything else in the baseline is stale. `current` also holds files
    # that are NOT pinned at all, so the membership test is required.
    still_pinned = {r for r in current if r in baseline and baseline[r][0] == current[r][0]}
    stale = sorted(set(baseline) - still_pinned)

    if unpinned or stale:
        print("::error::naive VAULT_ROOT read(s) - resolve through a sanctioned resolver:")
        for v in unpinned:
            print(v.render())
        for rel in stale:
            print(
                f"  STALE BASELINE {rel}: file changed, became clean, or was removed.\n"
                f"      Adopt the resolver (or `# vault-root-ok: <reason>`), then delete\n"
                f"      this row -- do NOT re-pin an edited file to keep it exempt."
            )
        print()
        print("  Fix, by surface:")
        print("    script (serves ONE vault):  _resolve_vault_root() -- prefer the")
        print("        script's own location; honor the env var only when it agrees")
        print("        (see scripts/aggregate-sessions.py).")
        print("    hook (fires on ANY vault):  resolve per TARGET FILE first, env var")
        print("        as fallback (see hooks/validate-handoff-frontmatter.py's")
        print("        vault_root_for). A hook with the wrong root fails silently OPEN.")
        print("    session-close cascade:  resolve_vault_root(cwd, os.environ.get(...))")
        print("        from hooks/_lib/vault_root.py.")
        print("    genuinely correct as-is:  add `# vault-root-ok: <reason>` on the line")
        print("        above. The reason is mandatory.")
        return 1

    # Every baseline row is now known to be pinned at its declared digest, so
    # each one's read count is exactly what its header claims it is. This is the
    # half of the ledger load_baseline() cannot check: it counts rows, not the
    # reads inside them, and the two drift independently (SEV-E was declaring 15
    # files / 17 reads over 12 files / 14 reads -- both numbers wrong, by
    # different amounts, for different reasons).
    reads_by_tag: dict = {}
    for rel, (_, tag) in baseline.items():
        reads_by_tag[tag] = reads_by_tag.get(tag, 0) + len(current[rel][1])
    read_drift = check_read_counts(baseline_text, reads_by_tag)
    if read_drift:
        print(f"::error::stale section header(s) in {baseline_path}:")
        for line in read_drift:
            print(f"  {line}")
        return 2

    pinned_reads = sum(len(v) for _, v in current.values())
    print(
        f"vault-root reads: OK ({len(relative)} file(s) scanned; "
        f"{len(current)} byte-pinned legacy file(s), {pinned_reads} pinned read(s))"
    )
    return 0


def check_paths(paths: list[Path]) -> int:
    """Audit explicit files/dirs with no baseline. Used by the negative controls."""
    targets: list[Path] = []
    for p in paths:
        if p.is_dir():
            targets.extend(sorted(p.rglob("*.py")))
        else:
            targets.append(p)
    if not targets:
        print("ERROR: no .py files to check", file=sys.stderr)
        return 2
    failures: list[Violation] = []
    for t in targets:
        try:
            violations, _ = scan_file(t, str(t))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            print(f"ERROR {t}: {exc}", file=sys.stderr)
            return 2
        failures.extend(violations)
    if failures:
        print("::error::naive VAULT_ROOT read(s):")
        for v in failures:
            print(v.render())
        return 1
    print(f"vault-root reads: OK ({len(targets)} file(s) scanned)")
    return 0


# --------------------------------------------------------------------------
# self-test
# --------------------------------------------------------------------------

_CASES = (
    # (name, source, expect_violation)
    (
        "naive get with ~/vault default is caught",
        "import os\n"
        "from pathlib import Path\n"
        "VAULT = Path(os.environ.get('VAULT_ROOT', str(Path.home() / 'vault')))\n",
        True,
    ),
    (
        "naive getenv is caught",
        "import os\n"
        "V = os.getenv('VAULT_ROOT')\n",
        True,
    ),
    (
        "subscript read is caught",
        "import os\n"
        "V = os.environ['VAULT_ROOT']\n",
        True,
    ),
    (
        "bare environ.get after `from os import environ` is caught",
        "from os import environ\n"
        "V = environ.get('VAULT_ROOT', '')\n",
        True,
    ),
    (
        "read inside _resolve_vault_root is sanctioned",
        "import os\n"
        "def _resolve_vault_root():\n"
        "    return os.environ.get('VAULT_ROOT')\n",
        False,
    ),
    (
        "read inside the per-file hook resolver is sanctioned",
        "import os\n"
        "def vault_root_for(path):\n"
        "    return os.environ.get('VAULT_ROOT') or detect(path)\n",
        False,
    ),
    (
        "env var passed INTO resolve_vault_root is sanctioned",
        "import os\n"
        "from _lib.vault_root import resolve_vault_root\n"
        "root = resolve_vault_root(cwd, os.environ.get('VAULT_ROOT'))\n",
        False,
    ),
    (
        "exemption comment with a reason passes",
        "import os\n"
        "# vault-root-ok: installer flag default; user passes --vault-path explicitly\n"
        "V = os.environ.get('VAULT_ROOT')\n",
        False,
    ),
    (
        "exemption comment on the same line passes",
        "import os\n"
        "V = os.environ.get('VAULT_ROOT')  # vault-root-ok: opt-in cross-vault scrub\n",
        False,
    ),
    (
        "exemption comment with NO reason is itself a violation",
        "import os\n"
        "# vault-root-ok:\n"
        "V = os.environ.get('VAULT_ROOT')\n",
        True,
    ),
    (
        "read indirected through a module-level constant is caught",
        "import os\n"
        "VAULT_ROOT_ENV = 'VAULT_ROOT'\n"
        "def _vault_root():\n"
        "    return os.environ.get(VAULT_ROOT_ENV, '')\n",
        True,
    ),
    (
        "a module constant naming a DIFFERENT var is not flagged",
        "import os\n"
        "OTHER_ENV = 'SNAPSHOTS_DIR'\n"
        "v = os.environ.get(OTHER_ENV, '')\n",
        False,
    ),
    (
        "VAULT_ROOT_FORCE is a different knob and is never flagged",
        "import os\n"
        "if os.environ.get('VAULT_ROOT_FORCE'):\n"
        "    pass\n",
        False,
    ),
    (
        "WRITING the env var for a child process is not a read",
        "import os\n"
        "os.environ['VAULT_ROOT'] = str(vault)\n",
        False,
    ),
    (
        "a non-os mapping named env is not the environment",
        "env = {}\n"
        "env['VAULT_ROOT'] = vault_root\n",
        False,
    ),
    (
        "the pattern quoted as DATA in a fixture string is not executable",
        "FIXTURE = '''\n"
        "import os\n"
        "VAULT = os.environ.get(\"VAULT_ROOT\", \"~/vault\")\n"
        "'''\n",
        False,
    ),
    (
        "the pattern named in a docstring is not executable",
        '"""The old code read os.environ.get("VAULT_ROOT", str(Path.home() / "vault"))."""\n',
        False,
    ),
    (
        "a naive read in a nested helper inside a resolver is still sanctioned",
        "import os\n"
        "def _resolve_vault_root():\n"
        "    def inner():\n"
        "        return os.environ.get('VAULT_ROOT')\n"
        "    return inner()\n",
        False,
    ),
    (
        "a naive read in a function merely NEAR a resolver is caught",
        "import os\n"
        "def _resolve_vault_root():\n"
        "    return None\n"
        "def other():\n"
        "    return os.environ.get('VAULT_ROOT', '~/vault')\n",
        True,
    ),
    (
        "marker at the TOP of a multi-line comment block still exempts the read",
        "import os\n"
        "# vault-root-ok: the installer runs before any vault is resolvable from\n"
        "# anything else; --vault-path is the explicit override and the default\n"
        "# is None, so unset skips substitution rather than guessing a vault.\n"
        "ap.add_argument('--vault-path', default=os.environ.get('VAULT_ROOT'))\n",
        False,
    ),
    (
        "a comment block separated from the read by a blank line does NOT exempt it",
        "import os\n"
        "# vault-root-ok: this reason belongs to something else entirely\n"
        "\n"
        "V = os.environ.get('VAULT_ROOT', '~/vault')\n",
        True,
    ),
    (
        "multi-line call with the marker above passes",
        "import os\n"
        "# vault-root-ok: argparse default; --vault-root overrides it\n"
        "V = os.environ.get(\n"
        "    'VAULT_ROOT',\n"
        "    '',\n"
        ")\n",
        False,
    ),
)


def self_test() -> int:
    failures = 0
    for name, source, expect in _CASES:
        try:
            got = bool(scan_source(source, "<self-test>"))
        except SyntaxError as exc:
            print(f"  FAIL {name}: fixture does not parse: {exc}")
            failures += 1
            continue
        if got != expect:
            verb = "expected a violation, got none" if expect else "expected clean, got a violation"
            print(f"  FAIL {name}: {verb}")
            failures += 1
        else:
            print(f"  ok   {name}")
    total = len(_CASES)
    if failures:
        print(f"check-vault-root-reads self-test: {failures} of {total} FAILED")
        return 1
    print(f"check-vault-root-reads self-test: {total}/{total} passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="fleet scan against the ratchet (default)")
    ap.add_argument("--check", nargs="+", type=Path, help="audit these files/dirs, no baseline")
    ap.add_argument("--report", action="store_true", help="enumerate violations, never fail")
    ap.add_argument("--self-test", action="store_true", help="run the pos/neg controls")
    ap.add_argument("--root", type=Path, default=REPO)
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.check:
        return check_paths([p for p in args.check])
    return check_all(args.root.resolve(), args.baseline.resolve(), report_only=args.report)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
