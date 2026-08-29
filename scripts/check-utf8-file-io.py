#!/usr/bin/env python3
"""check-utf8-file-io.py - fail-loud guard against locale-encoded FILE I/O.

THE GAP THIS FILLS
------------------
Two sibling lints already cover the Windows cp1252 class at its edges:

    check-utf8-stdout.py      - what a script PRINTS to the console
    check-utf8-subprocess.py  - what a script READS BACK from a subprocess

Nothing covered the third and most durable edge: what a script WRITES TO and
READS FROM A FILE. `open(p, "w")`, `Path.write_text(s)` and `Path.read_text()`
in text mode with no `encoding=` use `locale.getpreferredencoding(False)`, which
is **cp1252 on a stock Windows box** and UTF-8 on macOS/Linux. The artifact is
therefore encoded differently depending on whose machine produced it, while the
code reads identically on review.

HOW IT ACTUALLY BITES (all three measured, none of them announce themselves)
---------------------------------------------------------------------------
1. WRITE, silent corruption. `build-journal-index.py` wrote journal-index.json
   via `open(path, "w")` + `json.dump(..., ensure_ascii=False)`. On Windows a
   single accented title ("Reunión", "día") was written as cp1252 bytes into a
   file every consumer opens as UTF-8. The write raised NOTHING and the script
   printed "Indexed 2 entries" and exited 0; /weekly, /monthly, diagnose and
   insight-fact-check then died on
   `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xed`.

2. READ, silent mojibake. Reading a UTF-8 note under cp1252 usually does NOT
   raise: accents, em dashes, emoji and CJK all decode into WRONG characters
   ("Andrés" -> "AndrÃ©s"). Any regex or comparison then runs against text that
   is not what the file says, and under-matches quietly.

3. READ, silent skip. Five byte values are UNDEFINED in cp1252 (0x81, 0x8D,
   0x8F, 0x90, 0x9D). Curly quotes hit 0x9D and the gear emoji in this repo's
   own "⚙️ Meta" folder name hits 0x8F, so those reads RAISE - and a bare
   `except Exception: continue` around a read turns that into a skipped file
   that looks exactly like "nothing to do here".

WHY A MAINTAINER CANNOT SEE THIS
--------------------------------
`PYTHONUTF8=1` in the environment (and Windows' "Beta: Use Unicode UTF-8"
setting) forces UTF-8 mode, so `getpreferredencoding()` returns "utf-8" and the
whole class disappears. A box with it set can never reproduce a student's
report. That is not an exotic setup - it is set on the machine this lint was
written on. The bug is invisible where it is maintained and fatal where it is
installed, which is exactly why it needs a gate rather than vigilance.

THE PROVABLY-SAFE EXEMPTION
---------------------------
`json.dumps(x)` / `json.dump(x, f)` default to `ensure_ascii=True`, so the
payload is pure ASCII and cp1252 and UTF-8 agree byte for byte. Those sites are
NOT flagged - the same empirical exemption #417 applied to 42 of 78 hook files.
Passing `ensure_ascii=False` removes the exemption, because non-ASCII can then
reach the file.

SCOPE - state it, because a guard's scan scope IS its blind spot
----------------------------------------------------------------
    scripts/*.py, hooks/*.py, AND skills/**/scripts/*.py

The third pathspec is deliberate. check-utf8-stdout.py shipped scanning only
scripts/*.py while its docs claimed it covered every vault CLI; hooks/ - 113
tracked files - had never been looked at (MYC-3530). This repo also ships
Python under skills/, including the graphify pipeline that writes report and
graph artifacts derived from the user's own notes, so leaving it out would
repeat that exact mistake on day one.

THE RATCHET - scripts/utf8-file-io-baseline.txt
-----------------------------------------------
The pre-existing population is pinned by SHA-256 of newline-normalized content,
matching utf8-stdout-baseline.txt and vault-root-read-baseline.txt. A pinned
file may keep its violations until it is touched; EDITING it changes the hash
and fails the build until it is fixed. A file NOT in the baseline gets no grace
period at all. The baseline is a BACKLOG, NOT A SET OF PARDONS.

USAGE
    check-utf8-file-io.py                    # fleet scan against the baseline
    check-utf8-file-io.py FILE [FILE ...]    # lint named files, NO baseline
    check-utf8-file-io.py --report           # full inventory, always exit 0
    check-utf8-file-io.py --emit-baseline    # regenerate the baseline rows
"""
import ast
import hashlib
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = Path(__file__).resolve().parent / "utf8-file-io-baseline.txt"

# See "SCOPE" above. Kept as pathspecs so the scan set is greppable and pinned.
_SCAN_PATHSPECS = ("scripts/*.py", "hooks/*.py", "skills/**/scripts/*.py")


def normalize(source):
    """Newline-normalize so a CRLF checkout hashes the same as an LF one.

    The baseline hash MUST be identical on every checkout or the ratchet fires
    on Windows for reasons that have nothing to do with encoding. This repo has
    already been bitten by exactly that (#411).
    """
    return source.replace("\r\n", "\n").replace("\r", "\n")


def sha256_of(path):
    """SHA-256 over the newline-normalized text of `path`."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def tracked_files():
    """Tracked .py files across every scan pathspec, repo-relative, sorted."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "--", *_SCAN_PATHSPECS],
            capture_output=True, text=True, encoding="utf-8", check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return []
    return sorted(p for p in out.splitlines() if p.endswith(".py"))


def _payload_is_ascii_json(node):
    """True for json.dumps(...)/json.dump(...) that cannot emit non-ASCII.

    ensure_ascii defaults to True, so the output is pure ASCII and the locale
    encoding is irrelevant. An explicit ensure_ascii=False revokes this.
    """
    if not isinstance(node, ast.Call):
        return False
    if getattr(node.func, "attr", None) not in ("dumps", "dump"):
        return False
    if getattr(getattr(node.func, "value", None), "id", None) != "json":
        return False
    for kw in node.keywords:
        if kw.arg == "ensure_ascii":
            if isinstance(kw.value, ast.Constant) and kw.value.value is False:
                return False
            return False  # non-literal: cannot prove ASCII, so do not exempt
    return True


def _mode_of(call):
    """The literal mode string of an open() call ('' when not a literal)."""
    if len(call.args) > 1 and isinstance(call.args[1], ast.Constant):
        return call.args[1].value or ""
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return kw.value.value or ""
    return ""


def violations_in(path):
    """[(lineno, kind, detail)] - text-mode file I/O with no encoding=."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError):
        return []
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        # `os.open()` returns a file DESCRIPTOR (an int). It takes no encoding=
        # at all and performs no text decoding, so it is not text I/O -- but it
        # matches on the bare attribute name `open`, which made every caller a
        # false positive. The only way to silence one was to content-pin the
        # whole file in the baseline, which then went STALE on the next edit and
        # re-surfaced the same false positive as a fresh FAIL.
        if (name == "open"
                and isinstance(node.func, ast.Attribute)
                and getattr(node.func.value, "id", None) == "os"):
            continue
        if name not in ("open", "write_text", "read_text"):
            continue
        # `os.open` is NOT the builtin: it returns a raw file descriptor, has no
        # text mode, and rejects encoding= with a TypeError. Flagging it tells
        # the author to "pass encoding=utf-8" -- advice that breaks their code
        # and pushes a correct call into the baseline as if it were debt. The
        # qualifier is the whole difference; `p.open()` on a Path still counts.
        if (name == "open"
                and isinstance(node.func, ast.Attribute)
                and getattr(node.func.value, "id", None) == "os"):
            continue
        if any(kw.arg == "encoding" for kw in node.keywords):
            continue
        if name == "open":
            mode = _mode_of(node)
            if "b" in mode:
                continue  # binary: encoding is not applicable
            verb = "write" if ("w" in mode or "a" in mode or "+" in mode) else "read"
            found.append((node.lineno, "open", "%s mode=%r" % (verb, mode or "r")))
        elif name == "write_text":
            if node.args and _payload_is_ascii_json(node.args[0]):
                continue  # provably ASCII payload
            found.append((node.lineno, "write_text", "write"))
        else:  # read_text
            found.append((node.lineno, "read_text", "read"))
    return sorted(found)


def load_baseline(path):
    """path -> {relpath: sha256}. Raises ValueError if missing/malformed."""
    if not path.is_file():
        raise ValueError("baseline not found: {}".format(path))
    rows = {}
    for line_no, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) != 3:
            raise ValueError(
                "invalid baseline row {}:{}: {!r}".format(path, line_no, raw)
            )
        digest, _tag, rel = parts
        rows[rel] = digest
    return rows


def _scan(rels):
    """[(rel, [violations])] for files that have any."""
    out = []
    for rel in rels:
        p = REPO_ROOT / rel
        if not p.is_file():
            continue
        v = violations_in(p)
        if v:
            out.append((rel, v))
    return out


def self_test():
    """Prove the DETECTOR still bites, in both directions, before trusting a pass.

    A lint that silently stops detecting reports the same clean "OK" as a repo
    that is genuinely clean - the exact false-green shape this whole gate exists
    to close. So assert on a known-bad and a known-good sample every run.
    """
    import tempfile

    bad = '''
import json
from pathlib import Path
def a(p, d):
    with open(p, "w") as f:            # flag: write, no encoding
        json.dump(d, f, ensure_ascii=False)
def b(p, s):
    Path(p).write_text(s)              # flag: write_text, no encoding
def c(p):
    return Path(p).read_text()         # flag: read_text, no encoding
def d(p):
    with open(p) as f:                 # flag: read, no encoding
        return f.read()
'''
    good = '''
import json
from pathlib import Path
def a(p, d):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
def b(p, s):
    Path(p).write_text(s, encoding="utf-8")
def c(p):
    return Path(p).read_text(encoding="utf-8")
def d(p, raw):
    with open(p, "wb") as f:           # binary: encoding not applicable
        f.write(raw)
def e(p, obj):
    Path(p).write_text(json.dumps(obj))  # provably ASCII (ensure_ascii defaults True)
def f(p, raw):
    import os
    fd = os.open(p, os.O_WRONLY | os.O_CREAT, 0o600)   # fd, not text I/O
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
'''
    tmp = Path(tempfile.mkdtemp())
    failures = []

    bad_path = tmp / "bad.py"
    bad_path.write_text(bad, encoding="utf-8")
    got = violations_in(bad_path)
    if len(got) != 4:
        failures.append("expected 4 violations in the known-bad sample, got {}: {}"
                        .format(len(got), got))

    good_path = tmp / "good.py"
    good_path.write_text(good, encoding="utf-8")
    got = violations_in(good_path)
    if got:
        failures.append("known-good sample flagged {} time(s): {}".format(len(got), got))

    # ensure_ascii=False must REVOKE the json exemption
    rev = tmp / "rev.py"
    rev.write_text(
        "import json\nfrom pathlib import Path\n"
        "Path('x').write_text(json.dumps({}, ensure_ascii=False))\n",
        encoding="utf-8")
    if not violations_in(rev):
        failures.append("write_text(json.dumps(..., ensure_ascii=False)) was exempted "
                        "but non-ASCII can reach the file")

    # newline normalization: CRLF and LF content must hash identically
    lf, crlf = tmp / "lf.py", tmp / "crlf.py"
    lf.write_bytes(b"x = 1\ny = 2\n")
    crlf.write_bytes(b"x = 1\r\ny = 2\r\n")
    if sha256_of(lf) != sha256_of(crlf):
        failures.append("CRLF and LF content hash differently - the ratchet would "
                        "fire spuriously on a Windows checkout")

    # os.open is a raw file descriptor, not text I/O: no mode, no encoding=
    # (it raises TypeError on one). It must NOT be flagged -- and the BUILTIN
    # open two lines below it MUST still be, so the exemption is proven narrow
    # rather than a blanket mute of the word "open".
    osopen = tmp / "osopen.py"
    osopen.write_text("import os\nfd = os.open('/tmp/x', os.O_CREAT)\n",
                      encoding="utf-8")
    if violations_in(osopen):
        failures.append("os.open flagged as locale-encoded I/O: {}"
                        .format(violations_in(osopen)))

    mixed = tmp / "mixed.py"
    mixed.write_text("import os\nfd = os.open('/tmp/x', os.O_CREAT)\n"
                     "fh = open('/tmp/y')\n", encoding="utf-8")
    got = violations_in(mixed)
    if len(got) != 1 or got[0][0] != 3:
        failures.append("a builtin open beside an os.open must still be caught, "
                        "expected one hit on line 3, got {}".format(got))

    if failures:
        print("SELF-TEST FAIL:")
        for f in failures:
            print("  - {}".format(f))
        return 1
    print("OK - self-test: detector flags all 4 unsafe forms, clears all 5 safe "
          "forms, revokes the json exemption on ensure_ascii=False, and hashes "
          "CRLF == LF.")
    return 0


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}

    if "--self-test" in flags:
        return self_test()

    # Explicit files: no baseline, no pardons. Used by pre-commit on a diff.
    if args:
        bad = _scan([os.path.relpath(Path(a).resolve(), REPO_ROOT).replace(os.sep, "/")
                     for a in args])
        for rel, vs in bad:
            for lineno, kind, detail in vs:
                print("{}:{}: {} without encoding= ({})".format(rel, lineno, kind, detail))
        if bad:
            print("\n{} file(s) with locale-encoded file I/O. Pass encoding=\"utf-8\"."
                  .format(len(bad)))
            return 1
        print("OK - {} file(s) checked; all file I/O pins an encoding.".format(len(args)))
        return 0

    rels = tracked_files()
    if not rels:
        print("check-utf8-file-io: no tracked files matched {} - is this a git "
              "checkout?".format(", ".join(_SCAN_PATHSPECS)), file=sys.stderr)
        return 1
    offenders = _scan(rels)

    if "--emit-baseline" in flags:
        # Tiered + COUNTED sections, matching utf8-stdout-baseline.txt's format so
        # the shared burn-down check (scripts/_baseline_sections.py, gate (e1b))
        # validates this ledger too instead of vacuously passing a flat list.
        # SEV-A is the tier that can put wrong bytes ON DISK; SEV-B only mis-reads
        # them. Both are real; A is the one that damages an artifact someone else
        # then has to open.
        tiers = {"SEV-A-write": [], "SEV-B-read-only": []}
        for rel, vs in offenders:
            writes = any(kind == "write_text" or "write" in detail
                         for _ln, kind, detail in vs)
            tiers["SEV-A-write" if writes else "SEV-B-read-only"].append(rel)
        for tag in ("SEV-A-write", "SEV-B-read-only"):
            rows = sorted(tiers[tag])
            print("# ---- {}  ({} file(s)) ----".format(tag, len(rows)))
            for rel in rows:
                print("{} {} {}".format(sha256_of(REPO_ROOT / rel), tag, rel))
            print()
        return 0

    if "--report" in flags:
        total = sum(len(v) for _, v in offenders)
        print("== files with locale-encoded file I/O: {} ({} site(s)) =="
              .format(len(offenders), total))
        for rel, vs in offenders:
            print("  {}  [{} site(s)]".format(rel, len(vs)))
            for lineno, kind, detail in vs:
                print("      :{}  {} ({})".format(lineno, kind, detail))
        print("\n{} of {} scanned file(s) affected.".format(len(offenders), len(rels)))
        return 0

    try:
        baseline = load_baseline(DEFAULT_BASELINE)
    except ValueError as exc:
        print("check-utf8-file-io: {}".format(exc), file=sys.stderr)
        return 1

    violations, pardoned = [], []
    for rel, vs in offenders:
        pinned = baseline.get(rel)
        if pinned is None:
            violations.append((rel, vs, "not in baseline"))
        elif sha256_of(REPO_ROOT / rel) != pinned:
            violations.append((rel, vs, "baseline row is stale - file was edited"))
        else:
            pardoned.append(rel)

    stale = sorted(set(baseline) - set(pardoned))

    if violations:
        print("FAIL - locale-encoded file I/O ({} file(s)):".format(len(violations)))
        for rel, vs, why in violations:
            print("  {}  [{}]".format(rel, why))
            for lineno, kind, detail in vs:
                print("      :{}  {} without encoding= ({})".format(lineno, kind, detail))
        print("\nFix: pass encoding=\"utf-8\" explicitly. Text-mode I/O with no")
        print("encoding uses the locale - cp1252 on a stock Windows box - which")
        print("corrupts or mis-reads non-ASCII WITHOUT RAISING. See this file's")
        print("header for the three measured failure modes.")
        if stale:
            print("\n{} stale baseline row(s) (fixed or deleted - drop them):"
                  .format(len(stale)))
            for rel in stale:
                print("  {}".format(rel))
        return 1

    msg = ("OK - {} file(s) checked across {}; no NEW locale-encoded file I/O. "
           "{} content-pinned legacy file(s) remain - see {}.")
    print(msg.format(len(rels), " + ".join(_SCAN_PATHSPECS), len(pardoned),
                     DEFAULT_BASELINE.name))
    if stale:
        print("NOTE: {} stale baseline row(s) - now clean, drop them from the "
              "baseline: {}".format(len(stale), ", ".join(stale)))
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print
    # cannot crash the very lint that exists to prevent encoding crashes.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main(sys.argv[1:]))
