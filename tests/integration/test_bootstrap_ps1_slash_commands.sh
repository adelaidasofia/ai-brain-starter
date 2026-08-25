#!/usr/bin/env bash
# CI integration wrapper - runs the PowerShell slash-command install suite
# (tests/integration/test_bootstrap_ps1_slash_commands.ps1) as part of
# scripts/ci.sh. Same pattern as test_bootstrap_ps1_python_discovery.sh: pwsh is
# preinstalled on GitHub's ubuntu-latest runner, so CI always exercises it; if
# pwsh is absent locally we LOUDLY skip rather than block a contributor's other
# gates.
#
# NOTE ON COVERAGE: this run proves the block's LOGIC on pwsh 7 against a
# temp-dir fixture. It does NOT prove the real install wrote the real
# ~/.claude/commands on a real Windows box - windows-install.yml's
# "assert slash commands installed" step does that half, after running the
# actual bootstrap.ps1 under Windows PowerShell 5.1. Neither is the gate alone.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

if ! command -v pwsh >/dev/null 2>&1; then
  echo "SKIP: pwsh not installed here; CI's ubuntu + windows runners enforce this suite."
  echo "      install: brew install --cask powershell (macOS) / https://aka.ms/powershell (other)"
  exit 0
fi

pwsh -NoProfile -File "$ROOT/tests/integration/test_bootstrap_ps1_slash_commands.ps1"
