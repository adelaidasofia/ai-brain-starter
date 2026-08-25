#!/usr/bin/env bash
# CI integration wrapper - runs the PowerShell archive-dispatch suite
# (tests/integration/test_vault_backup_ps1_archive_dispatch.ps1) as part of
# scripts/ci.sh. Same pattern as test_bootstrap_ps1_python_discovery.sh: pwsh is
# preinstalled on GitHub's ubuntu-latest runner, so CI always exercises it; if
# pwsh is absent locally we LOUDLY skip rather than block a contributor's other
# gates.
#
# NOTE ON COVERAGE: on a Linux runner this proves the DISPATCH logic - which
# opener gets chosen for which archive, and that the empty-archive guard still
# bites. It cannot prove the Windows-only halves: Windows PowerShell 5.1
# semantics, and the System32 bsdtar preference that exists because Git for
# Windows puts GNU tar on PATH and GNU tar reads C:\... as a remote host.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

if ! command -v pwsh >/dev/null 2>&1; then
  echo "SKIP: pwsh not installed here; CI's ubuntu runner enforces this suite."
  echo "      install: brew install --cask powershell (macOS) / https://aka.ms/powershell (other)"
  exit 0
fi

pwsh -NoProfile -File "$ROOT/tests/integration/test_vault_backup_ps1_archive_dispatch.ps1"
