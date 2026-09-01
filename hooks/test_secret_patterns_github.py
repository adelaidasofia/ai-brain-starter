#!/usr/bin/env python3
"""Regression tests for the GitHub token family (`ghp_` / `gho_` / `ghu_` /
`ghs_` / `ghr_`).

Why this file exists
--------------------
The pattern's regex was `gh[ps]_`. The description one line below it read
"GitHub classic PAT (ghp_ / ghs_) or OAuth (gho_)." The OAuth shape was
never in the character class, so `scan()` and `redact()` passed every
`gho_` token through intact -- and `ghu_` (user-to-server) and `ghr_`
(refresh) were missing from BOTH the regex and the description.

That is the same class the nvidia-api-key and npm-access-token entries
were added for: a credential shape that exists in the documentation a
human reads and in no code that executes. The docstring is what a
reviewer checks the guard against, so a wrong docstring does not merely
fail to help -- it actively certifies a hole.

The regex now matches by PROPERTY (`gh` + one letter + `_`) rather than
by an enumerated prefix list, because GitHub's prefix set is open. A
closed list against an open set re-opens this gap on the next prefix
minted; `test_future_prefix_is_covered` pins that property.

Design note: each positive case varies the ENCODING/CONTEXT, not just
the token. A suite that constructs one spelling per prefix is
byte-indistinguishable from one that merely pins the defence -- green,
named for the property, and false. A token travels differently in a
shell assignment, a git remote URL, a `ps` line, JSON, and an
Authorization header, and the pattern must survive all of them.

Run: python3 hooks/test_secret_patterns_github.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))

from _lib.secret_patterns import PATTERNS, redact, scan  # noqa: E402

PATTERN_NAME = "github-pat-classic"
FINE_GRAINED = "github-pat-fine-grained"

# The regex this pattern shipped with before the widening. Kept here as the
# NEGATIVE CONTROL: a vector that is green under BOTH regexes proves nothing
# about the fix, so the suite asserts which vectors actually discriminate.
_OLD_REGEX = re.compile(r"gh[ps]_[A-Za-z0-9]{36,}", re.ASCII)

# Real-shaped, non-live. GitHub tokens are a 4-char prefix + a long token;
# 36 is the documented floor for the classic shape.
TOKENS = {
    "ghp_": "ghp_" + "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk1Ll2",  # classic PAT
    "gho_": "gho_" + "Mm3Nn4Oo5Pp6Qq7Rr8Ss9Tt0Uu1Vv2Ww3Xx4",  # OAuth
    "ghu_": "ghu_" + "Yy5Zz6Aa7Bb8Cc9Dd0Ee1Ff2Gg3Hh4Ii5Jj6",  # user-to-server
    "ghs_": "ghs_" + "Kk7Ll8Mm9Nn0Oo1Pp2Qq3Rr4Ss5Tt6Uu7Vv8",  # server-to-server
    "ghr_": "ghr_" + "Ww9Xx0Yy1Zz2Aa3Bb4Cc5Dd6Ee7Ff8Gg9Hh0",  # refresh
}

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


print("pattern is registered:")
check(
    f"{PATTERN_NAME} present in PATTERNS",
    any(p.name == PATTERN_NAME for p in PATTERNS),
    "-- the pattern was removed; every GitHub token guard is blind again",
)

print("\nthe docstring is not lying (every prefix it names is actually matched):")
# The original defect was a description that advertised coverage the regex did
# not have. This binds the two together so the prose cannot drift again.
#
# The parser class here is DELIBERATELY WIDER than the pattern's own `[a-z]`:
# harvesting with `gh[a-z]_` would only ever find prefixes the regex matches by
# construction, making every assertion a tautology that cannot fail. Widened to
# `[A-Za-z0-9]` so a description that claims `gh9_` or `ghP_` -- a shape outside
# the regex's class -- is harvested and then FAILS here. A binding that cannot
# fail is not a binding.
_desc = next(p.description for p in PATTERNS if p.name == PATTERN_NAME)
_claimed = re.findall(r"\bgh[A-Za-z0-9]_", _desc)
check(
    "description actually names prefixes (binding is not vacuous)",
    len(_claimed) >= 5,
    f"-- harvested {_claimed}; the loop below would assert nothing",
)
for prefix in _claimed:
    sample = prefix + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp9Oo8"
    check(
        f"description names {prefix} and the regex matches it",
        any(name == PATTERN_NAME for name, _ in scan(sample)),
        f"-- description advertises {prefix} but scan({sample[:8]}...) returned nothing",
    )

# Negative control for the binding itself: an over-claiming description MUST be
# caught. This is the exact defect that shipped -- prose promising coverage the
# regex does not have -- so the guard against it needs its own proof of bite.
_over_claim = _desc + " Also covers ghZ_ and gh9_."
_harvested = re.findall(r"\bgh[A-Za-z0-9]_", _over_claim)
_unbacked = [
    pfx
    for pfx in _harvested
    if not any(n == PATTERN_NAME for n, _ in scan(pfx + "A" * 36))
]
check(
    "an over-claiming description would FAIL this binding",
    sorted(_unbacked) == ["gh9_", "ghZ_"],
    f"-- expected ghZ_/gh9_ to be flagged as unbacked, got {_unbacked}",
)

print("\npositive cases (encoding varies, every documented prefix covered):")
for prefix, token in TOKENS.items():
    carriers = {
        "bare token": token,
        "shell export": f"export GH_TOKEN={token}",
        "git remote URL": f"https://x-access-token:{token}@github.com/o/r.git",
        "process listing": f"4711 gh auth login --with-token {token}",
        "json value": f'{{"token": "{token}"}}',
        "authorization header": f"Authorization: Bearer {token}",
        "env-file line": f"GITHUB_TOKEN={token}\n",
        "followed by shell delimiter": f"T={token};echo done",
        "inside a longer log line": f"2026-09-01 INFO auth ok token={token} scope=repo",
    }
    for label, text in carriers.items():
        hits = scan(text)
        check(
            f"{prefix} caught in {label}",
            any(name == PATTERN_NAME for name, _ in hits),
            f"-- scan returned {hits}",
        )

print("\nnegative control (which vectors actually discriminate old vs new regex):")
# gho_/ghu_/ghr_ were invisible to the shipped regex. If these ever pass under
# the old pattern too, the vectors stopped testing the fix.
for prefix in ("gho_", "ghu_", "ghr_"):
    check(
        f"{prefix} was MISSED by the pre-fix regex (vector discriminates)",
        not _OLD_REGEX.search(TOKENS[prefix]),
        "-- vector no longer proves the widening",
    )
for prefix in ("ghp_", "ghs_"):
    check(
        f"{prefix} was already covered (regression guard, not new coverage)",
        bool(_OLD_REGEX.search(TOKENS[prefix])),
        "-- this vector was expected to be pre-existing coverage",
    )

print("\nthe prefix set is OPEN (property match, not an enumerated list):")
# GitHub has minted new prefixes before. An enumerated class would report a
# confident CLEAN over the next one; a structural match covers it on arrival.
check(
    "an undocumented gh?_ prefix is still covered",
    any(
        name == PATTERN_NAME
        for name, _ in scan("GH_TOKEN=ghz_" + "Qq1Ww2Ee3Rr4Tt5Yy6Uu7Ii8Oo9Pp0Aa1Ss2")
    ),
    "-- the class is closed again; the next prefix GitHub mints will be invisible",
)

print("\nknown residual (documented limitation, not a passing property):")
# `[A-Za-z0-9]{36,}` cannot cross `_`, so a benign `gh<letter>_` run abutting a
# real token with NO separator ends on the token's `gh?` and orphans its body.
# Contrived (needs two tokens concatenated), zero incidence in the repo corpus
# or 1.2 GB of transcripts -- but pinned here so it stays KNOWN. If a future
# change fixes it, this assertion flips and the comment must be updated.
_abutting = "gha_" + "X" * 33 + "ghp_" + "P" * 36
_red, _ = redact(_abutting)
check(
    "greedy-adjacency residual still behaves as documented",
    ("P" * 36) in _red,
    "-- the residual changed; update the comment on the pattern, this is now FIXED",
)
# The same shape WITH a separator -- the realistic case -- must redact fully.
_separated = "gha_" + "X" * 33 + " " + "ghp_" + "P" * 36
_red2, _ = redact(_separated)
check(
    "the same tokens separated by whitespace ARE both redacted",
    ("P" * 36) not in _red2,
    f"-- got {_red2!r}",
)

print("\nnegative cases (must NOT fire -- these are not credentials):")
NEGATIVE = {
    "placeholder": "GITHUB_TOKEN=gho_YOUR_TOKEN_HERE",
    "bare prefix": "OAuth tokens start with gho_ and are long",
    "too short": "gho_abc123",
    "prose mentioning the shape": "The scrub list covers ghp_ and gho_ tokens.",
    "near-miss prefix (uppercase)": "GHO_" + "A" * 36,
    "near-miss prefix (digit)": "gh1_" + "A" * 36,
}
for label, text in NEGATIVE.items():
    hits = [h for h in scan(text) if h[0] == PATTERN_NAME]
    check(f"no false positive on {label}", not hits, f"-- got {hits}")

print("\nno cross-firing with the fine-grained pattern:")
# `github_pat_...` has its own entry. If the structural classic match also fired
# on it the incident report would name the wrong credential type.
_fine = "github_pat_" + "A" * 22 + "_" + "b" * 60
_fine_hits = [name for name, _ in scan(_fine)]
check(
    "fine-grained token claimed only by the fine-grained pattern",
    _fine_hits == [FINE_GRAINED],
    f"-- got {_fine_hits}",
)

print("\nredaction:")
for prefix, token in TOKENS.items():
    redacted, _ = redact(f"export GH_TOKEN={token}")
    check(
        f"{prefix} does not survive redaction",
        token not in redacted,
        f"-- got {redacted!r}",
    )
    check(
        f"{prefix} redaction is labelled",
        "REDACTED-github-pat-classic" in redacted,
        f"-- got {redacted!r}",
    )
    # Idempotency: a marker that itself matched a pattern would loop or corrupt
    # on a second scrub pass (the SessionEnd scrub runs redact() repeatedly).
    twice, hits2 = redact(redacted)
    check(
        f"{prefix} redaction is idempotent",
        twice == redacted and hits2 == [],
        f"-- got {hits2}",
    )

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print(f"All GitHub token-family pattern checks passed ({len(TOKENS)} prefixes).")
