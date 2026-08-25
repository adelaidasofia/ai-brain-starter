#!/usr/bin/env python3
"""Regression tests for the NVIDIA build-API key pattern (`nvapi-`).

Why this file exists
--------------------
A live NVIDIA key reached a session transcript through a process listing,
and was then found sitting in 8+ transcript files including backup copies.
Every secret guard reported a confident CLEAN over all of them.

The cause was not a weak regex. The shape had been written into a prose
checklist a human reads, and never into executable code -- so the one
credential type the auth guidance tells you to use was the one shape no
scanner could see. A guard cannot catch what was only ever documented.

These tests pin the executable half of that contract.

Design note: each positive case varies the ENCODING/CONTEXT, not just the
token. A regression test that constructs a single spelling is
byte-indistinguishable from one that merely pins the defence -- green,
named for the property, and false. The token travels differently in a
shell assignment, a `ps` line, JSON, and a quoted value, and the pattern
must survive all of them.

Run: python3 hooks/test_secret_patterns_nvidia.py
"""

from __future__ import annotations

import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))

from _lib.secret_patterns import PATTERNS, redact, scan  # noqa: E402

PATTERN_NAME = "nvidia-api-key"

# Real-shaped, non-live. NVIDIA build keys are `nvapi-` + a long token.
KEY = "nvapi-" + "Xy7Zq2Lm9Rt4Bv6Nc8Ka1Pd3Wf5Hj0Gs7Ue2Yi4Ao6Bn1Cm"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


print("pattern is registered:")
check(
    "nvidia-api-key present in PATTERNS",
    any(p.name == PATTERN_NAME for p in PATTERNS),
    "-- the pattern was removed; every nvapi- guard is blind again",
)

print("\npositive cases (encoding varies, token is constant):")
# Each entry is a DIFFERENT way the same credential actually travels.
POSITIVE = {
    "bare token": KEY,
    "shell export": f"export NVIDIA_API_KEY={KEY}",
    "process listing (the shape that leaked)": f"5123 python nvidia.sh --key {KEY}",
    "json value": f'{{"nvidia_api_key": "{KEY}"}}',
    "single-quoted header value": f"REQUEST_HEADER='Authorization: Bearer {KEY}'",
    "env-file line": f"NVIDIA_API_KEY={KEY}\n",
    "followed by shell delimiter": f"KEY={KEY};echo done",
    "inside a longer log line": f"2026-08-23 INFO auth ok key={KEY} tier=2",
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
    "placeholder": "NVIDIA_API_KEY=nvapi-YOUR_KEY_HERE",
    "bare prefix": "keys start with nvapi- and are long",
    "too short": "nvapi-abc123",
    "near-miss prefix": "navapi-" + "a" * 50,
    "prose mentioning the shape": "The scrub list covers nvapi- tokens.",
}
for label, text in NEGATIVE.items():
    hits = [h for h in scan(text) if h[0] == PATTERN_NAME]
    check(f"no false positive on {label}", not hits, f"-- got {hits}")

print("\nredaction:")
redacted, hits = redact(f"export NVIDIA_API_KEY={KEY}")
check("key does not survive redaction", KEY not in redacted, f"-- got {redacted!r}")
check("redaction is labelled", "REDACTED-nvidia-api-key" in redacted, f"-- got {redacted!r}")

# Idempotency: re-redacting redacted text must be a no-op. A marker that
# itself matches a pattern would loop or corrupt on a second scrub pass.
twice, hits2 = redact(redacted)
check("redaction is idempotent", twice == redacted and hits2 == [], f"-- got {hits2}")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("All nvidia-api-key pattern checks passed.")
