#!/usr/bin/env python3
"""Controls for _lib/umbrella_map.

NAMED test_*.py AND AT hooks/ ON PURPOSE. scripts/ci.sh collects suites with
`git ls-files -- 'hooks/test_*.py' 'tests/test_*.py'`. An earlier suite in this
repo shipped as `<name>.test.py` and was never collected -- ci.sh documents that
incident in its own comments. A suite under hooks/_lib/ is invisible to that
glob twice over (wrong directory AND wrong naming), so this file lives here.
check-hook-activation.py exempts a `test_` basename, so being at hooks/ does not
make it a hook that must be wired.

WHAT THIS GUARDS. The map's failure mode is not "wrong text" -- it is CLAIMING A
SCOPE IT DOES NOT HAVE. A map that renders skills the machine lacks, or silently
drops skills it could not read, reads exactly like a correct map. The
predecessor was a hardcoded literal, so this was unfalsifiable by construction.

Run: python3 hooks/test_umbrella_map.py
"""
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "_lib"))

import _lib.umbrella_map as um  # noqa: E402

# PRECONDITION. Checks 1 and 2 below assert "renders nothing"; if the bypass is
# ambiently set they pass for the WRONG REASON and every mutant against those
# invariants survives silently. Assert the environment before relying on it.
_SAVED_BYPASS = os.environ.pop("UMBRELLA_MAP_BYPASS", None)

fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        fails.append(name)


def write_skill(root, name, body):
    d = pathlib.Path(root) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")


def fm(body):
    return um.parse_frontmatter(body)


print("negative controls (must render nothing):")
check("absent dir -> None", um.render_umbrella_map(pathlib.Path("/nonexistent-xyz")) is None)

with tempfile.TemporaryDirectory() as tmp:
    write_skill(tmp, "plain", "---\nname: plain\ndescription: Not an umbrella.\n---\n")
    check("dir with 0 declarations -> None (a REAL zero)",
          um.render_umbrella_map(pathlib.Path(tmp)) is None)
    write_skill(tmp, "real", "---\nname: real\ndescription: Yes.\numbrella: true\n---\n")
    os.environ["UMBRELLA_MAP_BYPASS"] = "1"
    check("bypass -> None", um.render_umbrella_map(pathlib.Path(tmp)) is None)
    os.environ.pop("UMBRELLA_MAP_BYPASS")
    # Control on the control: a bypass test passes trivially if the input could
    # never have rendered in the first place.
    check("...same dir DOES render once bypass is cleared",
          um.render_umbrella_map(pathlib.Path(tmp)) is not None)
    for falsy in ("0", "false", "no", "off", ""):
        os.environ["UMBRELLA_MAP_BYPASS"] = falsy
        check(f"bypass={falsy!r} does NOT suppress (bare truthiness would)",
              um.render_umbrella_map(pathlib.Path(tmp)) is not None)
        os.environ.pop("UMBRELLA_MAP_BYPASS")

print("declaration parsing:")
check("umbrella: false does not declare", not fm("---\na: b\numbrella: false\n---\n").get("umbrella") == "true")
check("bare `umbrella:` does not declare", fm("---\numbrella:\n---\n").get("umbrella") == "")
for v in ("true", "yes", "1", "on", "TRUE", "True"):
    check(f"_truthy accepts {v!r}", um._truthy(v))
for v in ("false", "no", "0", "off", "", "maybe"):
    check(f"_truthy rejects {v!r}", not um._truthy(v))
# A trailing YAML comment must not un-declare a skill: real YAML strips it, and
# without stripping `true  # note` is not truthy -- a valid declaration the map
# would ignore, failing identically to "not an umbrella".
check("trailing `# comment` stripped from a value",
      um._truthy(fm("---\numbrella: true  # routing umbrella\n---\n").get("umbrella", "")))
check("comment stripped from a group heading",
      fm("---\numbrella_group: Ops  # the group\n---\n").get("umbrella_group") == "Ops")
check("comment stripped from an order",
      fm("---\numbrella_order: 5 # first\n---\n").get("umbrella_order") == "5")
check("a QUOTED value keeps its #",
      fm("---\nx: 'a # b'\n---\n").get("x") == "a # b")
# An unclosed frontmatter scans to EOF, so a SKILL.md that DOCUMENTS this
# contract in a fenced block would promote itself and inherit the example group.
check("unclosed frontmatter parses as nothing",
      fm("---\nname: s\n\n```yaml\numbrella: true\numbrella_group: 'Fake'\n```\n") == {})
check("nested/indented `umbrella: true` never promotes",
      not um._truthy(fm("---\nname: s\nmeta:\n  umbrella: true\n---\n").get("umbrella", "")))
check("BOM before --- still parses", fm("﻿---\nname: s\numbrella: true\n---\n").get("name") == "s")
check("leading blank line before --- still parses", fm("\n---\nname: s\numbrella: true\n---\n").get("name") == "s")
check("CRLF parses", fm("---\r\nname: s\r\numbrella: true\r\n---\r\n").get("name") == "s")
check("description containing ': ' keeps its tail",
      fm("---\ndescription: 'Use when X: do Y'\n---\n").get("description") == "Use when X: do Y")
# A block-scalar indicator means the value is on the FOLLOWING lines; rendering
# the literal ">" as a skill's domain is worse than rendering none.
check("block scalar `>` yields empty, not '>'", fm("---\ndescription: >\n  Folded.\n---\n").get("description") == "")

print("domain + ordering:")
check("first_sentence stops at '. '", um.first_sentence("A. B.") == "A")
check("first_sentence stops at ' - '", um.first_sentence("A - B") == "A")
check("first_sentence stops at '; '", um.first_sentence("A; B") == "A")
check("first_sentence clips long text", len(um.first_sentence("x" * 300)) <= 96)

with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    # Group names chosen so ALPHABETICAL order is the REVERSE of declared order;
    # otherwise the ordering assertion passes under a sort that ignores
    # umbrella_order entirely -- a control that cannot vary.
    write_skill(root, "beta", "---\nname: beta\ndescription: B.\numbrella: true\n"
                              "umbrella_group: 'Aardvark layer'\numbrella_order: 20\n"
                              "umbrella_domain: 'b domain'\n---\n")
    write_skill(root, "alpha", "---\nname: alpha\ndescription: A.\numbrella: true\n"
                               "umbrella_group: 'Zebra layer'\numbrella_order: 10\n"
                               "umbrella_domain: 'a domain'\n---\n")
    write_skill(root, "gamma", "---\nname: gamma\n"
                               "description: derived domain here. Tail must be dropped.\n"
                               "umbrella: true\numbrella_group: 'Aardvark layer'\numbrella_order: 21\n---\n")
    write_skill(root, "defaulted", "---\nname: defaulted\ndescription: D.\numbrella: true\n"
                                   "umbrella_order: notanint\n---\n")
    write_skill(root, "decoy-malformed", "no frontmatter\numbrella: true\n")
    out = um.render_umbrella_map(root)
    check("counts only declared umbrellas (4)", "4 routing umbrellas" in out, repr(out[:70]))
    check("malformed frontmatter skipped", "decoy-malformed" not in out)
    check("domain fallback = first sentence", "/gamma — derived domain here" in out)
    check("fallback drops the tail", "Tail must be dropped" not in out)
    check("group ordered by earliest member, NOT alphabetically",
          out.index("Zebra layer:") < out.index("Aardvark layer:"))
    check("within-group order respected", out.index("/beta") < out.index("/gamma"))
    check("missing umbrella_group defaults to 'Skills'", "Skills:" in out)
    check("non-int umbrella_order falls back (renders, does not crash)", "/defaulted" in out)
    check("no unreadable warning on a clean tree", "unreadable" not in out)

with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    write_skill(root, "only", "---\nname: only\ndescription: O.\numbrella: true\n---\n")
    check("1 umbrella reads singular", "1 routing umbrella available" in um.render_umbrella_map(root))
    # name falls back to the DIRECTORY when frontmatter omits it
    write_skill(root, "dirnamed", "---\ndescription: X.\numbrella: true\n---\n")
    check("name falls back to the directory name", "/dirnamed" in um.render_umbrella_map(root))

print("layout controls (the maintainer-box blind spot):")
# A skills repo linked into ~/.claude/skills is FLAT; a cloned BUNDLE keeps its
# own tree one level deeper. A maintainer box has BOTH, so a flat-only scan
# passes there and renders NOTHING on a substrate-only install -- the client case.
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    write_skill(root / "some-bundle" / "skills", "nested-umbrella",
                "---\nname: nested-umbrella\ndescription: N.\numbrella: true\n---\n")
    out = um.render_umbrella_map(root)
    check("NESTED-only layout renders", out is not None, "flat-only scan returns None here")
    if out:
        check("nested skill is named", "/nested-umbrella" in out)

# Precedence is asserted on IDENTITY, not count: identical bodies pass under
# either winner. The bundle dir is named to sort BEFORE the flat entry, which is
# what an interleaved builder gets wrong.
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    mk = lambda d: f"---\nname: twice\ndescription: T.\numbrella: true\numbrella_domain: '{d}'\n---\n"
    write_skill(root, "twice", mk("FLAT-COPY"))
    write_skill(root / "aaa-bundle" / "skills", "twice", mk("NESTED-COPY"))
    out = um.render_umbrella_map(root)
    check("same skill flat+nested renders ONCE", out.count("/twice") == 1, f"{out.count('/twice')}x")
    check("FLAT copy wins even when the bundle sorts first", "FLAT-COPY" in out,
          "nested copy shadowed the flat one the harness actually loads")

# Loose non-directory entries are normal in a skills tree (measured: five .zip
# archives beside real skill dirs). They must not be counted as broken skills.
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    write_skill(root, "good", "---\nname: good\ndescription: G.\numbrella: true\n---\n")
    (root / "archive.zip").write_bytes(b"PK\x03\x04not a skill")
    (root / "bundle").mkdir()
    (root / "bundle" / "skills").mkdir()
    (root / "bundle" / "skills" / "inner.zip").write_bytes(b"PK\x03\x04")
    out = um.render_umbrella_map(root)
    check("loose .zip files are not reported as unreadable skills", "unreadable" not in out, repr(out[:90]))

print("scope honesty:")
# An empty result from a scan that could not READ is NOT the same as a real zero.
with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    write_skill(root, "s", "---\nname: s\ndescription: S.\numbrella: true\n---\n")
    (root / "s" / "SKILL.md").write_bytes(b"\x00\x01\x02binary")  # safe_read -> binary
    out = um.render_umbrella_map(root)
    check("an unreadable skill is COUNTED, not silently dropped",
          out is not None and "unreadable" in out,
          "a scan that read nothing rendered as a real zero" if out is None else repr(out[:90]))

print("scan budget (a stalled tree must not block session start):")
# safe_read's deadline is PER FILE (5s default) and there is no budget across a
# scan. On a stalled cloud-sync folder that is N x 5s of blocked session start
# -- and if every read fails, an empty list is indistinguishable from a genuine
# "nothing declared". Both halves are asserted here.
import time as _time
import _lib.safe_read as _sr

_real_read = um.safe_read_text


def _stalled(path, **kw):
    _time.sleep(min(kw.get("timeout", 5.0), 0.2))
    return _sr.SafeTextRead(pathlib.Path(path), "timeout")


try:
    um.safe_read_text = _stalled
    um._read_skill.__globals__["safe_read_text"] = _stalled
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        for i in range(120):
            write_skill(root, f"s{i:03d}", "---\numbrella: true\n---\n")
        t0 = _time.monotonic()
        out = um.render_umbrella_map(root)
        elapsed = _time.monotonic() - t0
        check("a stalled tree is bounded by the scan budget, not N x per-file deadline",
              elapsed < um.SCAN_BUDGET_S + 1.0,
              f"took {elapsed:.1f}s; unbounded would be 120 x {_sr.DEFAULT_TIMEOUT_S}s")
        check("a scan that read NOTHING does not masquerade as a real zero",
              out is not None and "unreadable" in out,
              "returned None, which is the 'nothing declared' signal")
finally:
    um.safe_read_text = _real_read
    um._read_skill.__globals__["safe_read_text"] = _real_read

print()
if _SAVED_BYPASS is not None:
    os.environ["UMBRELLA_MAP_BYPASS"] = _SAVED_BYPASS
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    sys.exit(1)
print("all checks passed")
