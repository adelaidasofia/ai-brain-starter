#!/usr/bin/env bash
# CI integration wrapper — runs the repair-path fixture
# (scripts/test-repair-sidecar-note-deletion.sh) as part of `scripts/ci.sh`.
# Kept thin so the test logic has ONE home next to the script it exercises.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
exec bash "$ROOT/scripts/test-repair-sidecar-note-deletion.sh"
