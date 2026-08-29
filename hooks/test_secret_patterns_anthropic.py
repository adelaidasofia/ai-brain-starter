#!/usr/bin/env python3
"""Regression tests for the Anthropic credential pattern (`sk-ant-`).

Why this file exists
--------------------
The pattern required an `api\\d{2}` infix, so it matched `sk-ant-api03-...` and
was blind to `sk-ant-oat01-...` -- the OAuth token Claude Code itself issues and
exports as CLAUDE_CODE_OAUTH_TOKEN. `scan()` returned zero hits and `redact()`
passed the token through intact. The same class as the `nvapi-` miss: the one
credential shape the product's own auth path uses was the one shape no guard
could see.

Why the FIRST fix was not enough, which is the more useful lesson
----------------------------------------------------------------
The widening initially used `[a-z]{3}\\d{2}` and described itself as matching "by
property rather than by an enumerated list". It did not. `[a-z]{3}\\d{2}` is
itself a CLOSED set wearing the language of an open one, and it silently missed
`admin01` (5 letters), `oath01` (4) and `oat1` (1 digit) -- each surviving
redaction ENTIRELY, exactly the failure the widening existed to remove. An
adversarial review measured it. The lesson is that a structural claim in a
comment is a testable assertion, and this file is where it gets tested.

Design note, inherited from the nvidia suite: each positive case varies the
ENCODING/CONTEXT, not just the token. A suite that constructs a single spelling
is byte-indistinguishable from one that merely pins the defence -- green, named
for the property, and false. It also varies the INFIX, because the infix is
where this pattern has actually failed twice.

Run: python3 hooks/test_secret_patterns_anthropic.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))

from _lib.secret_patterns import PATTERNS, redact, scan  # noqa: E402

PATTERN_NAME = "anthropic-api-key"

# Real-shaped, non-live. 46 trailing chars, comfortably over the {40,} floor.
BODY = "Kp7Vn2Qr9Xt4Bm6Zc8La1Wd3Hf5Jg0Ys7Ue2Ni4Ao6Br1Cm"
OAUTH = f"sk-ant-oat01-{BODY}"
APIKEY = f"sk-ant-api03-{BODY}"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


print("pattern is registered:")
check(
    "anthropic-api-key present in PATTERNS",
    any(p.name == PATTERN_NAME for p in PATTERNS),
    "-- the pattern was removed; every sk-ant- guard is blind again",
)

print("\ninfix shapes (this is where the pattern has failed twice):")
# Every one of these is a credential. A regex that misses any of them is a
# closed set, whatever its comment claims.
INFIXES = {
    "api03 (the original, must not regress)": APIKEY,
    "oat01 (CLAUDE_CODE_OAUTH_TOKEN)": OAUTH,
    "admin01 (5 letters -- missed by [a-z]{3})": f"sk-ant-admin01-{BODY}",
    "oath01 (4 letters -- missed by [a-z]{3})": f"sk-ant-oath01-{BODY}",
    "oat1 (1 digit -- missed by \\d{2})": f"sk-ant-oat1-{BODY}",
    "a future two-letter infix": f"sk-ant-zz09-{BODY}",
}
for label, text in INFIXES.items():
    hits = scan(text)
    check(
        f"caught: {label}",
        any(name == PATTERN_NAME for name, _ in hits),
        f"-- scan returned {hits}",
    )

print("\npositive cases (encoding varies, token is constant):")
POSITIVE = {
    "bare token": OAUTH,
    "shell export": f"export CLAUDE_CODE_OAUTH_TOKEN={OAUTH}",
    "process listing": f"5123 claude --token {OAUTH}",
    "json value": f'{{"oauth_token": "{OAUTH}"}}',
    "quoted header value": f"REQUEST_HEADER='Authorization: Bearer {OAUTH}'",
    "env-file line": f"CLAUDE_CODE_OAUTH_TOKEN={OAUTH}\n",
    "followed by shell delimiter": f"TOKEN={OAUTH};echo done",
    "inside a longer log line": f"2026-08-29 INFO auth ok token={OAUTH} tier=1",
}
for label, text in POSITIVE.items():
    hits = scan(text)
    check(
        f"caught in {label}",
        any(name == PATTERN_NAME for name, _ in hits),
        f"-- scan returned {hits}",
    )

print("\nnegative cases (must NOT fire -- these are not credentials):")
NEGATIVE = {
    # No digits in the infix: a word, not a credential shape. This boundary is
    # legitimate -- unlike the letter-count boundary, it hides no real shape.
    "no-digit infix": "sk-ant-oauth-" + "z" * 50,
    # Under the {40,} floor. That floor is what keeps prose and short
    # placeholders quiet, and it is load-bearing for false-positive suppression.
    "short placeholder": "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-YOUR_TOKEN_HERE",
    "too short to be a token": "sk-ant-oat01-abc123",
    "bare shape in prose": "The OAuth token is shaped sk-ant-oat01- plus a long token.",
    "prefix alone": "sk-ant-",
}
for label, text in NEGATIVE.items():
    hits = scan(text)
    check(
        f"silent on {label}",
        not any(name == PATTERN_NAME for name, _ in hits),
        f"-- scan returned {hits}",
    )

print("\nknown, ACCEPTED false positive (documented, not a bug):")
# A placeholder longer than the {40,} floor DOES match. This is deliberate:
# redacting a placeholder in a transcript costs nothing, missing a live
# credential does not. Pinned so the trade-off stays a decision, not a surprise.
long_placeholder = "sk-ant-oat01-YOUR_OAUTH_TOKEN_GOES_HERE_REPLACE_ME_BEFORE_USE"
check(
    "long placeholder is redacted (accepted FP, asymmetric cost)",
    any(name == PATTERN_NAME for name, _ in scan(long_placeholder)),
    "-- if this now passes silently, the {40,} floor moved; re-read the trade-off",
)

print("\nredaction:")
redacted, _ = redact(f"export CLAUDE_CODE_OAUTH_TOKEN={OAUTH}")
check("token does not survive redaction", OAUTH not in redacted, f"-- got {redacted!r}")
check(
    "redaction is labelled",
    "REDACTED-anthropic-api-key" in redacted,
    f"-- got {redacted!r}",
)

# Idempotency: re-redacting redacted text must be a no-op. `redact()` MUTATES
# the user's session JSONL at SessionEnd, so a marker that itself matched a
# pattern would corrupt the transcript on a second scrub pass.
twice, hits2 = redact(redacted)
check("redaction is idempotent", twice == redacted and hits2 == [], f"-- got {hits2}")

api_redacted, _ = redact(APIKEY)
check(
    "api shape still redacted (regression guard on the widening)",
    APIKEY not in api_redacted,
    f"-- got {api_redacted!r}",
)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("All anthropic-api-key pattern checks passed.")
