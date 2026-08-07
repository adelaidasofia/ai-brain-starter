#!/usr/bin/env python3
"""check-utf8-subprocess.py - the READ half of the cp1252 guard.

scripts/check-utf8-stdout.py (#313) fails a CLI that PRINTS non-ASCII without
reconfiguring its own stdout to UTF-8. This is the mirror image: a process that
READS a child's output without pinning the decode.

    subprocess.run([...], capture_output=True, text=True)      # <- the bug
    subprocess.run([...], capture_output=True, text=True,
                   encoding="utf-8", errors="replace")          # <- the fix

`text=True` alone decodes with locale.getpreferredencoding(False): cp1252 on a
Spanish/French/Portuguese Windows, cp437 under cmd.exe, ASCII in a C-locale
pipe. Nearly every child in this repo prints a VAULT PATH, and every vault path
contains "gear Meta" - U+FE0F, whose 0x8F byte is unmapped in cp1252. The
decode raises UnicodeDecodeError inside subprocess.run(), before the caller
sees a single byte.

WHY THIS IS A LINT AND NOT A TEST
    The failure is invisible on the machine that writes the code. A developer
    on macOS (UTF-8 locale) and CI on Linux (UTF-8 locale) both pass forever;
    the crash only exists on a Windows install in a non-English locale. It has
    now shipped twice:

      - #313, the WRITE side: sync-vault-scripts.ps1 read an empty result and
        misreported it as "no Meta folder".
      - 2026-07-31, the READ side: install-hooks-user-level.py reported "could
        NOT link Claude Code memory into the vault" on machines where the
        linker was fine, and the user's memory stayed in ~/.claude.

    The second one is what this gate exists to prevent a third time.

THE RATCHET - scripts/utf8-subprocess-baseline.txt
    Same shape as scripts/cloud-safe-walker-baseline.txt and
    scripts/vault-root-read-baseline.txt: each pre-existing offender is pinned
    by the SHA-256 of its LF-normalized source.

      - A file NOT in the baseline fails immediately. New code cannot add one.
      - A pinned file whose content CHANGES fails: if you are already editing
        it, fix the call while you are in there. That is how the backlog drains
        without a risky 40-file sweep.
      - A pinned file that becomes clean fails as STALE, so the baseline cannot
        rot into a permanent amnesty list.

    Hashes are computed on LF-normalized text so a CRLF checkout (every Windows
    clone - the platform this gate is FOR) produces the same digest.

SCOPE
    Tracked scripts/*.py and hooks/*.py, matching check-utf8-stdout.py. hooks/
    is the higher-severity surface: a hook that raises mid-gate either fails
    open (the protection the user believes in is not there) or denies every
    tool call with no legible cause.

OPT-OUT
    `# utf8-subprocess-ok: <reason>` on the subprocess.run line, for a child
    whose output is genuinely bytes-safe and where the locale decode is wanted.

USAGE
    python3 scripts/check-utf8-subprocess.py              # lint the fleet
    python3 scripts/check-utf8-subprocess.py --self-test  # prove it still bites
    python3 scripts/check-utf8-subprocess.py --write-baseline   # re-pin

Stdlib only (ast/hashlib/json/subprocess). No network, no writes except
--write-baseline.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hooks"))

from _lib.safe_read import safe_read_text  # noqa: E402

DEFAULT_BASELINE = ROOT / "scripts" / "utf8-subprocess-baseline.txt"

# The subprocess entry points that decode child output for you.
_DECODING_CALLS = {"run", "check_output", "Popen", "call", "check_call"}
_TEXT_KWARGS = {"text", "universal_newlines"}
OPT_OUT = "utf8-subprocess-ok:"


def normalize(source: str) -> str:
    """LF-normalize so a CRLF checkout hashes identically to an LF one."""
    return source.replace("\r\n", "\n").replace("\r", "\n")


def _is_subprocess_call(node: ast.Call) -> bool:
    """True for subprocess.<entry>(...) or a bare from-imported <entry>(...)."""
    fn = node.func
    if isinstance(fn, ast.Attribute) and fn.attr in _DECODING_CALLS:
        base = fn.value
        return isinstance(base, ast.Name) and base.id == "subprocess"
    return False


def find_violations(source: str) -> list[tuple[int, str]]:
    """(lineno, snippet) for each decoding subprocess call with no encoding=.

    A call is a violation when it asks for text mode (text=True /
    universal_newlines=True) but pins no encoding. `encoding=` present in ANY
    form - literal, variable, or **kwargs spread - clears it: the point is that
    the author thought about the decode.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_subprocess_call(node):
            continue
        kwargs = {k.arg for k in node.keywords if k.arg}
        # A **kwargs spread (arg is None) may carry encoding - the _TEXT_UTF8
        # dict idiom this repo now uses. Treat it as handled.
        spread = any(k.arg is None for k in node.keywords)
        if "encoding" in kwargs or spread:
            continue
        wants_text = False
        for kw in node.keywords:
            if kw.arg in _TEXT_KWARGS and isinstance(kw.value, ast.Constant) \
                    and kw.value.value is True:
                wants_text = True
        if not wants_text:
            continue  # bytes mode: no decode happens, nothing to pin
        span = lines[node.lineno - 1: (node.end_lineno or node.lineno)]
        if any(OPT_OUT in ln for ln in span):
            continue
        out.append((node.lineno, (span[0] if span else "").strip()[:100]))
    return out


def _tracked_files() -> list[Path]:
    try:
        res = subprocess.run(
            ["git", "ls-files", "--", "scripts/*.py", "hooks/*.py"],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [ROOT / p for p in res.stdout.split() if p]


def _read(path: Path) -> str | None:
    """Bounded read via the shared primitive (scripts/check-cloud-safe-file-walkers.py).

    This lint walks the whole tracked fleet, which is precisely the shape that
    primitive exists for: on a cloud-synced checkout a single dehydrated
    placeholder would otherwise block the gate or hand back partial bytes that
    silently change a pinned hash.
    """
    result = safe_read_text(path, timeout=5.0, max_bytes=1_000_000)
    return result.text if result.ok else None


def _digest(path: Path) -> str:
    return hashlib.sha256(
        normalize(_read(path) or "").encode("utf-8")
    ).hexdigest()


def _load_baseline(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    if not path.is_file():
        return entries
    for raw in (_read(path) or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            raise ValueError(f"invalid baseline row: {raw!r}")
        entries[parts[1]] = parts[0]
    return entries


def _scan() -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
    for f in _tracked_files():
        raw = _read(f)
        if raw is None:
            continue
        v = find_violations(normalize(raw))
        if v:
            found[f.relative_to(ROOT).as_posix()] = v
    return found


def write_baseline(path: Path) -> int:
    found = _scan()
    rows = [
        f"{_digest(ROOT / rel)} {rel}" for rel in sorted(found)
    ]
    path.write_text(
        "# SHA-256-pinned legacy subprocess calls that decode child output with the\n"
        "# LOCALE encoding instead of an explicit one. Editing a pinned file, or\n"
        "# adding a new offender, fails scripts/check-utf8-subprocess.py.\n"
        "# Fix by adding: encoding=\"utf-8\", errors=\"replace\"\n"
        + "\n".join(rows) + "\n",
        encoding="utf-8", newline="\n",
    )
    print(f"wrote {len(rows)} pinned file(s) to {path}")
    return 0


def self_test() -> int:
    """Positive and negative controls - a gate that cannot bite is not a gate."""
    cases = [
        ("bare text=True is a violation",
         "import subprocess\nsubprocess.run(['x'], capture_output=True, text=True)\n", 1),
        ("explicit encoding is clean",
         "import subprocess\nsubprocess.run(['x'], text=True, encoding='utf-8')\n", 0),
        ("universal_newlines is the same bug",
         "import subprocess\nsubprocess.run(['x'], universal_newlines=True)\n", 1),
        ("bytes mode has no decode",
         "import subprocess\nsubprocess.run(['x'], capture_output=True)\n", 0),
        ("**kwargs spread is treated as handled",
         "import subprocess\nsubprocess.run(['x'], text=True, **KW)\n", 0),
        ("opt-out marker suppresses",
         "import subprocess\nsubprocess.run(['x'], text=True)  # utf8-subprocess-ok: bytes-safe\n", 0),
        ("check_output counts",
         "import subprocess\nsubprocess.check_output(['x'], text=True)\n", 1),
        ("a non-subprocess run() is ignored",
         "runner.run(['x'], text=True)\n", 0),
    ]
    failed = 0
    for label, src, expected in cases:
        got = len(find_violations(src))
        if got == expected:
            print(f"PASS  {label}")
        else:
            print(f"FAIL  {label} :: expected {expected}, got {got}")
            failed += 1
    print(f"\n{'FAILED' if failed else 'OK'} - self-test {len(cases) - failed}/{len(cases)}")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    args = ap.parse_args()

    baseline_path = Path(args.baseline)
    if args.self_test:
        return self_test()
    if args.write_baseline:
        return write_baseline(baseline_path)

    baseline = _load_baseline(baseline_path)
    found = _scan()

    violations: list[str] = []
    for rel, sites in sorted(found.items()):
        pinned = baseline.get(rel)
        if pinned is None:
            violations.append(
                f"{rel}: {len(sites)} subprocess call(s) decode child output with the "
                f"locale encoding (line {sites[0][0]}: {sites[0][1]}). Add "
                'encoding="utf-8", errors="replace".'
            )
        elif pinned != _digest(ROOT / rel):
            violations.append(
                f"{rel}: pinned legacy file was EDITED and still decodes with the "
                f"locale encoding (line {sites[0][0]}). Fix the call while you are "
                "in here, or re-pin with --write-baseline."
            )
    for rel in sorted(set(baseline) - set(found)):
        violations.append(
            f"{rel}: STALE baseline row - the file is clean now (or gone). "
            "Remove the row with --write-baseline."
        )

    if violations:
        print("::error::child output decoded with the locale encoding - on a "
              "non-UTF-8 Windows console this raises UnicodeDecodeError on any "
              "vault path:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    pinned_sites = sum(len(v) for v in found.values())
    print(f"OK - {len(_tracked_files())} file(s) checked; no unpinned locale-decoded "
          f"subprocess calls. {len(found)} content-pinned legacy file(s) "
          f"({pinned_sites} call site(s)) remain - see {baseline_path.name}.")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): this lint prints paths and quoted
    # source lines, either of which may be non-ASCII.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
