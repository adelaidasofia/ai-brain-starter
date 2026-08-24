#!/usr/bin/env bash
# CI integration wrapper - runs the PowerShell bootstrap Python-discovery suite
# (tests/integration/test_bootstrap_ps1_python_discovery.ps1, ai-brain-starter#290)
# as part of scripts/ci.sh. Same pattern as test_relocate_ps1.sh: pwsh is
# preinstalled on GitHub's ubuntu-latest runner, so CI always exercises it; if
# pwsh is absent locally we LOUDLY skip rather than block a contributor's other
# gates.
#
# NOTE ON COVERAGE: this run proves the resolver's LOGIC on pwsh 7. It cannot
# prove the two things that are Windows-only - Windows PowerShell 5.1 semantics
# and a real `py` launcher - because a Linux runner structurally cannot express
# them. windows-install.yml runs the SAME .ps1 under 5.1 on windows-latest for
# that half. Neither runner alone is the gate.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

if ! command -v pwsh >/dev/null 2>&1; then
  echo "SKIP: pwsh not installed here; CI's ubuntu + windows runners enforce this suite."
  echo "      install: brew install --cask powershell (macOS) / https://aka.ms/powershell (other)"
  exit 0
fi

pwsh -NoProfile -File "$ROOT/tests/integration/test_bootstrap_ps1_python_discovery.ps1"
