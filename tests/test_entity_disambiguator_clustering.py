#!/usr/bin/env python3
"""
test_entity_disambiguator_clustering.py — stdlib-only regression tests for the
banded candidate generation in scripts/entity-disambiguator.py.

Run: python3 tests/test_entity_disambiguator_clustering.py
No pytest dependency. Exits non-zero on any failure. Touches no real vault.

Why this file exists
--------------------
cluster_mentions used to compare every pair of normalized keys. On a 31k-key
vault that is 486M pairs at ~20us each, measured at 2.8 CPU-hours per run, and
the nightly job stopped finishing. Candidate generation is now banded.

A band is only safe if it is a NECESSARY condition for the leg it gates. Two
traps this file pins down:

  * The length band belongs to the Levenshtein leg ONLY. Bigram Jaccard has no
    length bound at all: "abab" and "abababab" have identical bigram sets and
    score 1.0 while their lengths differ by 2x. Gating the Jaccard leg on
    length silently drops those clusters. See test_jaccard_ignores_length.
  * lev_bounded must agree with a full Levenshtein for every distance at or
    below its cutoff, and report None above it. See test_lev_bounded_exact.
"""
from __future__ import annotations

import importlib.util
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "entity_disambiguator", ROOT / "scripts" / "entity-disambiguator.py"
)
ed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ed)

FAILS: list[str] = []


def check(cond, msg):
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILS.append(msg)


def reference_clusters(counts: Counter[str]) -> dict[str, str]:
    """All-pairs clustering: the definition the banded version must match."""
    by_norm: dict[str, list[str]] = defaultdict(list)
    for mention in counts:
        by_norm[ed.normalize_key(mention)].append(mention)
    keys = sorted(by_norm)
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    n = len(keys)
    for i in range(n):
        for j in range(i + 1, n):
            ki, kj = keys[i], keys[j]
            if not ki or not kj:
                continue
            if (ed.jaccard(ki, kj) >= ed.JACCARD_THRESHOLD
                    or ed.lev_ratio(ki, kj) <= ed.LEVENSHTEIN_RATIO_THRESHOLD):
                ra, rb = find(ki), find(kj)
                if ra != rb:
                    parent[rb] = ra

    clusters: dict[str, list[str]] = defaultdict(list)
    for k in keys:
        clusters[find(k)].extend(by_norm[k])
    aliases: dict[str, str] = {}
    for members in clusters.values():
        best = sorted(members, key=lambda m: (-counts[m], -len(m), m))[0]
        for m in members:
            aliases[m] = best
    return aliases


def test_lev_bounded_exact():
    print("lev_bounded agrees with full Levenshtein at and below its cutoff")
    rng = random.Random(1337)
    mismatches = 0
    for _ in range(4000):
        a = "".join(rng.choice("abcde") for _ in range(rng.randint(0, 9)))
        b = "".join(rng.choice("abcde") for _ in range(rng.randint(0, 9)))
        true = ed.levenshtein(a, b)
        for cutoff in range(0, 7):
            expected = true if true <= cutoff else None
            if ed.lev_bounded(a, b, cutoff) != expected:
                mismatches += 1
    check(mismatches == 0, f"0 mismatches across 28k assertions (got {mismatches})")


def test_jaccard_ignores_length():
    """A length band must not gate the Jaccard leg. Regression for a real near-miss."""
    print("cross-length Jaccard matches still cluster")
    counts = Counter({"abab": 5, "abababab": 2, "zzzzworkshop": 1})
    aliases = ed.cluster_mentions(counts)
    check(aliases["abab"] == aliases["abababab"],
          "identical bigram sets cluster across a 2x length gap")
    check(aliases["zzzzworkshop"] != aliases["abab"],
          "an unrelated key stays in its own cluster")
    check(aliases == reference_clusters(counts), "matches the all-pairs reference")


def test_matches_reference_on_noisy_corpus():
    print("banded clustering matches all-pairs on a corpus with seeded variants")
    rng = random.Random(7)
    stems = ["strategy", "onboarding", "retrieval", "consulting", "pipeline",
             "disambiguator", "clustering", "threshold", "workspace", "canonical"]
    counts: Counter[str] = Counter()
    for stem in stems:
        counts[stem] = rng.randint(2, 9)
        counts[stem + "s"] = rng.randint(1, 4)          # near-dupe via Levenshtein
        counts[stem.capitalize()] = rng.randint(1, 4)   # same normalized key
        counts[stem[:-1] + "x"] = rng.randint(1, 3)     # one substitution
    for i in range(120):                                # unrelated filler
        counts["filler%03d" % i] = 1
    check(ed.cluster_mentions(counts) == reference_clusters(counts),
          f"identical alias maps over {len(counts)} mentions")


def test_candidate_generation_stays_subquadratic():
    """Structural guard, not a wall-clock one: count the Levenshtein calls.

    A timing assertion false-reds on a loaded machine. Counting comparisons is
    deterministic, so this catches a regression to all-pairs on any hardware.
    """
    print("Levenshtein candidates stay far below the all-pairs count")
    counts = Counter()
    for i in range(1200):
        counts["entity%04d" % i] = 1        # length 10, one dense band
        counts["z" * (4 + i % 40) + "q%d" % i] = 1   # spread across lengths
    n = len({ed.normalize_key(m) for m in counts})
    all_pairs = n * (n - 1) // 2

    real = ed.lev_bounded
    calls = 0

    def counting(a, b, cutoff):
        nonlocal calls
        calls += 1
        return real(a, b, cutoff)

    ed.lev_bounded = counting
    try:
        ed.cluster_mentions(counts)
    finally:
        ed.lev_bounded = real
    ratio = calls / all_pairs
    print(f"      n={n} all_pairs={all_pairs:,} lev_calls={calls:,} ({ratio:.1%})")
    check(ratio < 0.35, f"lev candidates are {ratio:.1%} of all pairs, under the 35% ceiling")


def main() -> int:
    for fn in (test_lev_bounded_exact,
               test_jaccard_ignores_length,
               test_matches_reference_on_noisy_corpus,
               test_candidate_generation_stays_subquadratic):
        fn()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):")
        for f in FAILS:
            print("  -", f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
