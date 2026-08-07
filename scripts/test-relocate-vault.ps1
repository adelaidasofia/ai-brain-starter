#Requires -Version 5.1
<#
  Behavioral test for relocate-vault.ps1 (MYC-2383 - Windows parity of
  relocate-vault.sh). Runs under pwsh on any OS: on Windows the script leaves a
  junction, elsewhere a symbolic link; .NET flags both with the ReparsePoint
  attribute, so the link assertions hold cross-platform. The Junction-vs-symlink
  CHOICE is the only Windows-runtime-specific line and is exercised by hand on a
  Windows box; everything else (arg parse, move, Claude path-key migration,
  manifest record, refusal gates) is proven here.

  Every positive is paired with a negative control. Run: pwsh -File scripts/test-relocate-vault.ps1
#>
$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot
$RV   = Join-Path $Here "relocate-vault.ps1"
$script:fails = 0

# HERMETICITY: Obsidian-running is a SOFT gate in relocate-vault.ps1 (skippable
# with -Force). Its real probe (Get-Process -Name Obsidian) reads AMBIENT host
# state, so a developer with Obsidian open would see the no--Force cases below
# (the dry-run) die at that gate. Pin the probe to "absent" for the suite (child
# pwsh processes inherit this env var); Section 6 flips it to "running" to cover
# the refusal itself. Parity with the .sh suite's RELOCATE_VAULT_OBSIDIAN seam.
$env:RELOCATE_VAULT_OBSIDIAN = "absent"

function Check { param([string]$Label, [bool]$Cond)
  if ($Cond) { Write-Host "PASS  $Label" } else { Write-Host "FAIL  $Label"; $script:fails++ }
}

function Get-Py {
  foreach ($n in @("python3", "python")) {
    $c = Get-Command $n -ErrorAction SilentlyContinue
    if ($c) {
      $v = (& $c.Source -c "import sys;print(sys.version_info[0])" 2>$null)
      if ("$v".Trim() -eq "3") { return $c.Source }
    }
  }
  throw "python 3 required for the test"
}
$Py = Get-Py

# The exact path-key transform the script uses, so the test seeds the right key.
function Get-Key { param([string]$p)
  $code = @'
import os,re,sys
p = os.path.abspath(os.path.expanduser(sys.argv[1]))
resolved = os.path.join(os.path.realpath(os.path.dirname(p)), os.path.basename(p))
print(re.sub(r"[^a-zA-Z0-9]", "-", resolved))
'@
  return ("$(& $Py -c $code $p)").Trim()
}

# Run relocate-vault.ps1 as a SEPARATE process so its `exit` cannot kill us.
function Run-RV { param([string[]]$RvArgs, [string]$ConfigDir)
  $prev = $env:CLAUDE_CONFIG_DIR
  if ($ConfigDir) { $env:CLAUDE_CONFIG_DIR = $ConfigDir }
  try {
    $out = & pwsh -NoProfile -File $RV @RvArgs 2>&1
    return [pscustomobject]@{ rc = $LASTEXITCODE; out = ("$out" -join "`n") }
  } finally {
    if ($null -eq $prev) { Remove-Item Env:CLAUDE_CONFIG_DIR -ErrorAction SilentlyContinue }
    else { $env:CLAUDE_CONFIG_DIR = $prev }
  }
}

$TMP = Join-Path ([System.IO.Path]::GetTempPath()) ("rv-test-" + [Guid]::NewGuid().ToString("N").Substring(0, 10))
New-Item -ItemType Directory -Force -Path $TMP | Out-Null
try {
  # ---- 1. DRY-RUN changes nothing ------------------------------------------
  $old1 = Join-Path $TMP "cloud1/Brain"; New-Item -ItemType Directory -Force -Path $old1 | Out-Null
  Set-Content -LiteralPath (Join-Path $old1 "CLAUDE.md") -Value "# brain"
  New-Item -ItemType Directory -Force -Path (Join-Path $TMP "local1") | Out-Null
  $new1 = Join-Path $TMP "local1/Brain"
  $cfg1 = Join-Path $TMP "cfg1"
  $k1   = Get-Key $old1
  New-Item -ItemType Directory -Force -Path (Join-Path $cfg1 "projects/$k1") | Out-Null
  Set-Content -LiteralPath (Join-Path $cfg1 "projects/$k1/a.jsonl") -Value "{}"
  $r = Run-RV -RvArgs @($old1, $new1, "-DryRun") -ConfigDir $cfg1
  Check "dry-run: rc 0"                       ($r.rc -eq 0)
  Check "dry-run: announces DRY"              ($r.out -match "DRY")
  Check "dry-run: old still a real directory" ((Test-Path -LiteralPath $old1 -PathType Container) -and -not ((Get-Item -LiteralPath $old1 -Force).Attributes -band [IO.FileAttributes]::ReparsePoint))
  Check "dry-run: new not created"            (-not (Test-Path -LiteralPath $new1))

  # ---- 2. REAL relocate: move + link + Claude-state migration + manifest ----
  $old2 = Join-Path $TMP "cloud2/Brain"; New-Item -ItemType Directory -Force -Path $old2 | Out-Null
  Set-Content -LiteralPath (Join-Path $old2 "CLAUDE.md") -Value "# brain"
  New-Item -ItemType Directory -Force -Path (Join-Path $TMP "local2") | Out-Null
  $new2 = Join-Path $TMP "local2/Brain"
  $cfg2 = Join-Path $TMP "cfg2"
  $oldk = Get-Key $old2
  $newk = Get-Key $new2
  New-Item -ItemType Directory -Force -Path (Join-Path $cfg2 "projects/$oldk") | Out-Null
  Set-Content -LiteralPath (Join-Path $cfg2 "projects/$oldk/session.jsonl") -Value "{}"
  # Capture the physical old path BEFORE the move (after it, old2 is a link that
  # realpath would follow to new2). The manifest records this pre-move path.
  $physOld = ("$(& $Py -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' $old2)").Trim()
  $r = Run-RV -RvArgs @($old2, $new2, "-Force") -ConfigDir $cfg2
  Check "relocate: rc 0"                      ($r.rc -eq 0)
  Check "relocate: new is a real dir w/ notes" ((Test-Path -LiteralPath (Join-Path $new2 "CLAUDE.md")))
  $oldIsLink = (Test-Path -LiteralPath $old2) -and (((Get-Item -LiteralPath $old2 -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
  Check "relocate: old path is now a link"    $oldIsLink
  Check "relocate: transcript migrated to new key" (Test-Path -LiteralPath (Join-Path $cfg2 "projects/$newk/session.jsonl"))
  Check "relocate: old-key transcript kept as backup" (Test-Path -LiteralPath (Join-Path $cfg2 "projects/$oldk/session.jsonl"))
  $manifest = Join-Path $cfg2 "relocations.json"
  Check "relocate: relocations.json written"  (Test-Path -LiteralPath $manifest)
  if (Test-Path -LiteralPath $manifest) {
    $doc = Get-Content -Raw -LiteralPath $manifest | ConvertFrom-Json
    Check "relocate: manifest records the old path" (@($doc | ForEach-Object { $_.old }) -contains $physOld)
  }

  # ---- 3. NEGATIVE CONTROL: re-running on the now-linked old path REFUSES ----
  $r = Run-RV -RvArgs @($old2, (Join-Path $TMP "local2/Brain2"), "-Force") -ConfigDir $cfg2
  Check "refusal: source already a link -> rc 1" ($r.rc -eq 1)
  Check "refusal: explains 'already a link'"     ($r.out -match "already a link")

  # ---- 4. NEGATIVE CONTROL: target already exists REFUSES -------------------
  $old4 = Join-Path $TMP "cloud4/Brain"; New-Item -ItemType Directory -Force -Path $old4 | Out-Null
  Set-Content -LiteralPath (Join-Path $old4 "CLAUDE.md") -Value "# brain"
  $new4 = Join-Path $TMP "local4/Brain"; New-Item -ItemType Directory -Force -Path $new4 | Out-Null
  $r = Run-RV -RvArgs @($old4, $new4, "-Force") -ConfigDir (Join-Path $TMP "cfg4")
  Check "refusal: target exists -> rc 1"      ($r.rc -eq 1)
  Check "refusal: explains 'already exists'"  ($r.out -match "already exists")

  # ---- 5. state-only migration (vault already at the new path) --------------
  $old5 = Join-Path $TMP "cloud5/Brain"
  $new5 = Join-Path $TMP "local5/Brain"; New-Item -ItemType Directory -Force -Path $new5 | Out-Null
  $cfg5 = Join-Path $TMP "cfg5"
  $ok5 = Get-Key $old5
  $nk5 = Get-Key $new5
  New-Item -ItemType Directory -Force -Path (Join-Path $cfg5 "projects/$ok5") | Out-Null
  Set-Content -LiteralPath (Join-Path $cfg5 "projects/$ok5/t.jsonl") -Value "{}"
  $r = Run-RV -RvArgs @("-MigrateClaudeState", $old5, $new5) -ConfigDir $cfg5
  Check "state-only: rc 0"                    ($r.rc -eq 0)
  Check "state-only: transcript at new key"   (Test-Path -LiteralPath (Join-Path $cfg5 "projects/$nk5/t.jsonl"))

  # ---- 6. OBSIDIAN SOFT GATE: refuses while Obsidian runs; -Force overrides --
  # The suite pins RELOCATE_VAULT_OBSIDIAN=absent (top of file) so the dry-run
  # case runs on a dev box with Obsidian open. Here we flip it to "running" to
  # prove the gate itself still refuses, and that -Force escapes it -
  # deterministically, no real Obsidian. Parity with Section 10 of the .sh suite.
  $old6 = Join-Path $TMP "cloud6/Brain"; New-Item -ItemType Directory -Force -Path $old6 | Out-Null
  Set-Content -LiteralPath (Join-Path $old6 "CLAUDE.md") -Value "# brain"
  $new6 = Join-Path $TMP "local6/Brain"
  $cfg6 = Join-Path $TMP "cfg6"
  $env:RELOCATE_VAULT_OBSIDIAN = "running"
  try {
    $r = Run-RV -RvArgs @($old6, $new6) -ConfigDir $cfg6
    Check "obsidian gate: refuses while Obsidian runs -> rc 1" ($r.rc -eq 1)
    Check "obsidian gate: refusal names Obsidian"              ($r.out -match "Obsidian is running")
    Check "obsidian gate: target not created on refusal"       (-not (Test-Path -LiteralPath $new6))
    $r = Run-RV -RvArgs @($old6, $new6, "-Force") -ConfigDir $cfg6
    Check "obsidian gate: -Force overrides while Obsidian runs -> rc 0" ($r.rc -eq 0)
    Check "obsidian gate: -Force moved the vault (new exists)"          (Test-Path -LiteralPath (Join-Path $new6 "CLAUDE.md"))
  } finally {
    $env:RELOCATE_VAULT_OBSIDIAN = "absent"
  }
}
finally {
  Remove-Item -LiteralPath $TMP -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
if ($script:fails -gt 0) { Write-Host "FAILED: $($script:fails)"; exit 1 }
Write-Host "ALL TESTS PASSED"
exit 0
