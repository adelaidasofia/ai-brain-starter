#!/usr/bin/env bash
# CI integration wrapper — runs the decode-class fail-open suite
# (scripts/test-decode-safe-reads.sh, MYC-4012) as part of `scripts/ci.sh`.
# Kept thin so the test logic has ONE home next to the guard it exercises.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
exec bash "$ROOT/scripts/test-decode-safe-reads.sh"
