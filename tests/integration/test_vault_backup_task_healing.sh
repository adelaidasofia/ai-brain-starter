#!/usr/bin/env bash
# CI integration wrapper - runs the MYC-3528 scheduled-task self-heal suite
# (scripts/test-vault-backup-task-healing.ps1) as part of scripts/ci.sh. pwsh
# is preinstalled on GitHub's ubuntu-latest runner, so CI always exercises it.
# If pwsh is absent locally we LOUDLY skip (CI still enforces) rather than
# block a contributor's other gates - the same graceful-degradation pattern
# ci.sh uses for shellcheck, ruff, and the relocate-vault .ps1 suite.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

if ! command -v pwsh >/dev/null 2>&1; then
  echo "SKIP: pwsh not installed here; CI's ubuntu runner enforces the .ps1 behavioral tests."
  echo "      install: brew install --cask powershell (macOS) / https://aka.ms/powershell (other)"
  exit 0
fi

pwsh -NoProfile -File "$ROOT/scripts/test-vault-backup-task-healing.ps1"
