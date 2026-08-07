#!/usr/bin/env bash
# CI integration wrapper - runs the utf8-console-guard regression suite
# (scripts/test-utf8-console-guard.sh) as part of scripts/ci.sh. Kept thin so the
# test logic has ONE home next to the lint it exercises.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
exec bash "$ROOT/scripts/test-utf8-console-guard.sh"
