#!/usr/bin/env bash
# CI integration wrapper - runs the PowerShell bootstrap git-install suite
# (tests/integration/test_bootstrap_ps1_git_install.ps1, MYC-3895) as part of
# scripts/ci.sh. Same pattern as test_bootstrap_ps1_python_discovery.sh: pwsh is
# preinstalled on GitHub's ubuntu-latest runner, so CI always exercises it; if
# pwsh is absent locally we LOUDLY skip rather than block a contributor's other
# gates.
#
# NOTE ON COVERAGE: this run proves the LOGIC on pwsh 7 - the capability probe,
# the release-asset URI, the publisher check's reject path, and the structural
# claims about the shipped bootstrap.ps1. It structurally cannot prove the two
# Windows-only halves: Authenticode verification of a real signed binary (the
# cmdlet does not exist off Windows) and a PortableGit that actually unpacks and
# runs. windows-install.yml covers both, on a real windows-latest runner with
# git sealed off PATH. Neither runner alone is the gate.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

if ! command -v pwsh >/dev/null 2>&1; then
  echo "SKIP: pwsh not installed here; CI's ubuntu + windows runners enforce this suite."
  echo "      install: brew install --cask powershell (macOS) / https://aka.ms/powershell (other)"
  exit 0
fi

pwsh -NoProfile -File "$ROOT/tests/integration/test_bootstrap_ps1_git_install.ps1"
