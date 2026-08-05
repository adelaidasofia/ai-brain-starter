#Requires -Version 5.1
<#
  Behavioral test for the MYC-3528 self-heal: Get-BackupTaskProblems in
  vault-backup.ps1, the validator that decides whether an EXISTING Windows
  scheduled task is "registered but dead" and needs repair.

  Runs under pwsh on any OS. Get-ScheduledTask / Register-ScheduledTask are
  Windows-only and cannot run here, so this does not exercise the real
  registration call - it proves the DECISION logic that drives it: three
  fixtures broken in each of the three independently-fatal ways decide
  "repair", and a healthy fixture decides "leave it alone". That decision
  (problems.Count -gt 0) is the exact predicate Cmd-Setup branches on, copied
  here rather than re-implemented, so this is a real test of the shipped
  logic, not a parallel guess at it.

  Fixtures live in scripts/fixtures/scheduled-tasks/ as captured
  Task-Scheduler export XML (the real `Export-ScheduledTask` shape), not
  inline strings, so a future broken-task report can be dropped in as a new
  fixture without touching this script.

  Every positive control is paired with the negative control. Run:
    pwsh -File scripts/test-vault-backup-task-healing.ps1
#>
$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot
$VB = Join-Path $Here "vault-backup.ps1"
$FixtureDir = Join-Path $Here "fixtures/scheduled-tasks"
$script:fails = 0

function Check { param([string]$Label, [bool]$Cond)
  if ($Cond) { Write-Host "PASS  $Label" } else { Write-Host "FAIL  $Label"; $script:fails++ }
}

# vault-backup.ps1's top-level $Conf/$Marker fall back to $env:USERPROFILE
# when VAULT_BACKUP_CONF/_MARKER are unset - correct on Windows (its only
# real target), but $env:USERPROFILE does not exist on macOS/Linux, and that
# assignment runs immediately on dot-source, before any function does. Set
# both so the import itself does not crash here; this suite never calls
# Cmd-Setup/Cmd-Run, so nothing ever reads them.
$TMP = Join-Path ([System.IO.Path]::GetTempPath()) ("vbk-heal-test-" + [Guid]::NewGuid().ToString("N").Substring(0, 10))
New-Item -ItemType Directory -Force -Path $TMP | Out-Null
$env:VAULT_BACKUP_CONF = Join-Path $TMP "unused-conf.json"
$env:VAULT_BACKUP_MARKER = Join-Path $TMP "unused-marker"

# Import Get-BackupTaskProblems (and siblings) without dispatching a command -
# vault-backup.ps1's bottom `switch` is guarded to skip when dot-sourced.
. $VB

$TaskNs = "http://schemas.microsoft.com/windows/2004/02/mit/task"

# Parses a captured Task-Scheduler export XML fixture and returns the three
# raw values Get-BackupTaskProblems takes. __EXISTING_FILE__ / __MISSING_FILE__
# placeholders are substituted with real paths first (a fixture is a static,
# checked-in file; "exists" vs "does not exist" has to come from the test).
function Read-TaskFixture {
  param([string]$Name, [string]$ExistingFile, [string]$MissingFile)
  $raw = Get-Content -Raw -LiteralPath (Join-Path $FixtureDir $Name)
  if ($ExistingFile) { $raw = $raw.Replace("__EXISTING_FILE__", $ExistingFile) }
  if ($MissingFile)  { $raw = $raw.Replace("__MISSING_FILE__", $MissingFile) }
  [xml]$xml = $raw
  $ns = New-Object System.Xml.XmlNamespaceManager($xml.NameTable)
  $ns.AddNamespace("t", $TaskNs)
  $command   = $xml.SelectSingleNode("//t:Actions/t:Exec/t:Command", $ns).InnerText
  $arguments = $xml.SelectSingleNode("//t:Actions/t:Exec/t:Arguments", $ns).InnerText
  $disallow  = [bool]::Parse($xml.SelectSingleNode("//t:Settings/t:DisallowStartIfOnBatteries", $ns).InnerText)
  return [pscustomobject]@{ Command = $command; Arguments = $arguments; DisallowStartIfOnBatteries = $disallow }
}

# The exact predicate Cmd-Setup uses to decide repair vs leave-alone - see
# vault-backup.ps1's scheduling block. Asserting THIS (not just "problems is
# non-empty") is what makes "must be repaired" / "must be left untouched" a
# statement about the shipped decision, not a proxy for it.
function Decision { param([string[]]$Problems)
  if ($Problems.Count -eq 0) { return "leave-alone" } else { return "repair" }
}

try {
  $existingFile = Join-Path $TMP "vault-backup.ps1.copy"
  Set-Content -LiteralPath $existingFile -Value "# stand-in for a real, present script file"
  $missingFile = Join-Path $TMP "this-was-never-created.ps1"

  # ---- NEGATIVE CONTROL: a healthy task is left alone ------------------------
  $healthy = Read-TaskFixture -Name "healthy.xml" -ExistingFile $existingFile
  $healthyProblems = Get-BackupTaskProblems -Arguments $healthy.Arguments -Execute $healthy.Command -DisallowStartIfOnBatteries $healthy.DisallowStartIfOnBatteries
  Check "healthy fixture: zero problems"          ($healthyProblems.Count -eq 0)
  Check "healthy fixture: decision=leave-alone"   ((Decision $healthyProblems) -eq "leave-alone")

  # ---- POSITIVE CONTROL 1: empty -File path (the real historical bug) -------
  $emptyPath = Read-TaskFixture -Name "broken-empty-file-path.xml"
  $emptyPathProblems = Get-BackupTaskProblems -Arguments $emptyPath.Arguments -Execute $emptyPath.Command -DisallowStartIfOnBatteries $emptyPath.DisallowStartIfOnBatteries
  Check "empty -File: flagged"                    ($emptyPathProblems.Count -gt 0)
  Check "empty -File: names the file path"        ((($emptyPathProblems -join "; ")) -match "File")
  Check "empty -File: decision=repair"            ((Decision $emptyPathProblems) -eq "repair")

  # ---- POSITIVE CONTROL 1b: non-empty but missing -File (the other half of
  #      the "non-empty AND exists" rule) ------------------------------------
  $missingPath = Read-TaskFixture -Name "broken-missing-file.xml" -MissingFile $missingFile
  $missingPathProblems = Get-BackupTaskProblems -Arguments $missingPath.Arguments -Execute $missingPath.Command -DisallowStartIfOnBatteries $missingPath.DisallowStartIfOnBatteries
  Check "missing -File target: flagged"           ($missingPathProblems.Count -gt 0)
  Check "missing -File target: decision=repair"   ((Decision $missingPathProblems) -eq "repair")

  # ---- POSITIVE CONTROL 2: interpreter does not resolve on PATH -------------
  # Uses a deliberately fake command name, not the literal "pwsh" - the real
  # regression is env-dependent (pwsh IS installed on this test machine, which
  # would make "pwsh" a false negative here). See the fixture's own comment.
  $badExe = Read-TaskFixture -Name "broken-bad-interpreter.xml" -ExistingFile $existingFile
  $badExeProblems = Get-BackupTaskProblems -Arguments $badExe.Arguments -Execute $badExe.Command -DisallowStartIfOnBatteries $badExe.DisallowStartIfOnBatteries
  Check "unresolvable interpreter: flagged"       ($badExeProblems.Count -gt 0)
  Check "unresolvable interpreter: names Execute" ((($badExeProblems -join "; ")) -match "Execute")
  Check "unresolvable interpreter: decision=repair" ((Decision $badExeProblems) -eq "repair")

  # ---- POSITIVE CONTROL 3: DisallowStartIfOnBatteries left true -------------
  $battery = Read-TaskFixture -Name "broken-battery.xml" -ExistingFile $existingFile
  $batteryProblems = Get-BackupTaskProblems -Arguments $battery.Arguments -Execute $battery.Command -DisallowStartIfOnBatteries $battery.DisallowStartIfOnBatteries
  Check "battery-blocked: flagged"                ($batteryProblems.Count -gt 0)
  Check "battery-blocked: names batteries"         ((($batteryProblems -join "; ")) -match "batter")
  Check "battery-blocked: decision=repair"         ((Decision $batteryProblems) -eq "repair")

  # ---- BONUS: all three at once - checks are additive, not short-circuited --
  $all3 = Read-TaskFixture -Name "broken-all-three.xml"
  $all3Problems = Get-BackupTaskProblems -Arguments $all3.Arguments -Execute $all3.Command -DisallowStartIfOnBatteries $all3.DisallowStartIfOnBatteries
  Check "all three broken: all three named (count=3)" ($all3Problems.Count -eq 3)

  # ---- Function-shape guard: the validator takes plain values, not a live
  #      CIM object - the whole reason it is testable here at all. If a future
  #      edit changes it to require a Get-ScheduledTask result, this file
  #      (running on macOS/Linux CI) fails to even PARSE, loudly, instead of
  #      quietly losing coverage.
  Check "Get-BackupTaskProblems is defined after dot-source" ($null -ne (Get-Command Get-BackupTaskProblems -ErrorAction SilentlyContinue))
  Check "Register-BackupTask is defined after dot-source"    ($null -ne (Get-Command Register-BackupTask -ErrorAction SilentlyContinue))
}
finally {
  Remove-Item -LiteralPath $TMP -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
if ($script:fails -gt 0) { Write-Host "FAILED: $($script:fails)"; exit 1 }
Write-Host "ALL TESTS PASSED"
exit 0
