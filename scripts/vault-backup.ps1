#Requires -Version 5.1
<#
  vault-backup.ps1 - one-command, provider-agnostic, off-machine backup for a brain vault (Windows).

  Windows parity of vault-backup.sh. The gap it closes: a brain in active daily
  use whose only copy is the local disk it runs on. One disk failure = everything
  gone. This writes ONE compressed archive per run to a destination you already
  have (an external drive, or a OneDrive / Google Drive / Dropbox folder), so it
  is genuinely off-machine. One file, not the churning git tree, so a cloud
  folder syncs it without a storm.

    setup   pick a destination (+ optional encryption + a daily scheduled task), take the first snapshot now.
    run     take one snapshot now (what the scheduled task calls; non-interactive).
    verify  restore the newest snapshot to a temp dir and confirm it actually extracts.
    status  show where backups go, how fresh they are, and the canonical verdict.

  Config:  ~/.claude/.vault-backup.conf  (JSON, keyed by resolved vault path - shared with check-vault-backup.py)
  Marker:  ~/.claude/.vault-backup-last  (ISO8601 of the last successful run)

  Encryption (-Encrypt) uses gpg with the passphrase stored encrypted-at-rest via
  Windows DPAPI (current user). Excludes regenerable machine-exhaust dirs; keeps
  notes + .git. Archives are .zip (or .zip.gpg when encrypted).
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0)][string]$Command = "status",
  [string]$Vault,
  [string]$Dest,
  [switch]$Encrypt,
  [int]$Keep = 7,
  [string]$Schedule = "daily"
)

$ErrorActionPreference = "Stop"
$Conf   = if ($env:VAULT_BACKUP_CONF) { $env:VAULT_BACKUP_CONF } else { Join-Path $env:USERPROFILE ".claude\.vault-backup.conf" }
$Marker = if ($env:VAULT_BACKUP_MARKER) { $env:VAULT_BACKUP_MARKER } else { Join-Path $env:USERPROFILE ".claude\.vault-backup-last" }
$Stem   = "vault-backup"
$ExcludeDirs = @(".git\worktrees", ".claude\worktrees", ".smart-env", ".codegraph", "node_modules", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".trash")

function Say($m)  { Write-Host $m }
function Ok($m)   { Write-Host "OK   $m"   -ForegroundColor Green }
function Warn($m) { Write-Host "WARN $m"   -ForegroundColor Yellow }
function Die($m)  { Write-Host "ERROR $m"  -ForegroundColor Red; exit 1 }
function IsoNow   { (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ") }

function Resolve-Vault {
  param([string]$v)
  if (-not $v) { $v = if ($env:VAULT_PATH) { $env:VAULT_PATH } else { (Get-Location).Path } }
  if (-not (Test-Path -LiteralPath $v -PathType Container)) { Die "vault path is not a directory: $v" }
  return (Resolve-Path -LiteralPath $v).Path
}

function Read-Conf {
  if (Test-Path -LiteralPath $Conf) {
    try { return (Get-Content -Raw -LiteralPath $Conf | ConvertFrom-Json) } catch { return [pscustomobject]@{} }
  }
  return [pscustomobject]@{}
}

function Get-ConfEntry {
  param([string]$key)
  $d = Read-Conf
  if ($d.PSObject.Properties.Name -contains "vaults" -and $d.vaults) {
    if ($d.vaults.PSObject.Properties.Name -contains $key) { return $d.vaults.$key }
  }
  return $null
}

function Set-ConfEntry {
  param([string]$key, [hashtable]$fields)
  $d = Read-Conf
  if (-not ($d.PSObject.Properties.Name -contains "vaults") -or -not $d.vaults) {
    $d | Add-Member -NotePropertyName vaults -NotePropertyValue ([pscustomobject]@{}) -Force
  }
  $entry = if ($d.vaults.PSObject.Properties.Name -contains $key) { $d.vaults.$key } else { [pscustomobject]@{} }
  foreach ($k in $fields.Keys) {
    $entry | Add-Member -NotePropertyName $k -NotePropertyValue $fields[$k] -Force
  }
  $d.vaults | Add-Member -NotePropertyName $key -NotePropertyValue $entry -Force
  $dir = Split-Path -Parent $Conf
  if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  $d | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Conf -Encoding UTF8
}

function Slug-For { param([string]$p)
  $base = (Split-Path -Leaf $p) -replace '[ /\\]', '_'
  $md5  = [System.Security.Cryptography.MD5]::Create()
  $hash = ($md5.ComputeHash([Text.Encoding]::UTF8.GetBytes($p)) | ForEach-Object { $_.ToString("x2") }) -join ""
  return "$base-$($hash.Substring(0,8))"
}

# Passphrase at rest via DPAPI (current-user scoped). Never plaintext on disk.
function Store-Passphrase { param([string]$slug, [string]$plain)
  $secure = ConvertTo-SecureString $plain -AsPlainText -Force
  $enc = $secure | ConvertFrom-SecureString
  $pf = Join-Path $env:USERPROFILE ".claude\.vault-backup-pass-$slug"
  Set-Content -LiteralPath $pf -Value $enc -Encoding UTF8
  return $pf
}
function Get-Passphrase { param([string]$slug)
  $pf = Join-Path $env:USERPROFILE ".claude\.vault-backup-pass-$slug"
  if (-not (Test-Path -LiteralPath $pf)) { return $null }
  $secure = (Get-Content -Raw -LiteralPath $pf) | ConvertTo-SecureString
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function New-Archive {
  param([string]$vault, [string]$outBase, [bool]$enc, [string]$slug)
  # Stage a copy that excludes machine-exhaust, then zip the staging dir.
  $stage = Join-Path ([IO.Path]::GetTempPath()) ("vbk-" + [Guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Force -Path $stage | Out-Null
  try {
    $xd = @()
    foreach ($d in $ExcludeDirs) { $xd += "/XD"; $xd += (Join-Path $vault $d) }
    # robocopy mirrors the tree minus excluded dirs; /NFL /NDL /NJH /NJS = quiet.
    & robocopy $vault $stage /E @xd /XF "*.zip" "*.zip.gpg" /NFL /NDL /NJH /NJS /NP | Out-Null
    # robocopy exit codes 0-7 are success; >=8 is failure.
    if ($LASTEXITCODE -ge 8) { Die "robocopy staging failed (code $LASTEXITCODE)" }
    $zip = "$outBase.zip"
    if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
    Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -CompressionLevel Optimal -Force
    if ($enc) {
      $gpg = Get-Command gpg -ErrorAction SilentlyContinue
      if (-not $gpg) { Die "-Encrypt needs gpg (Gpg4win) installed and on PATH" }
      $pass = Get-Passphrase $slug
      if (-not $pass) { Die "could not read backup passphrase" }
      $out = "$outBase.zip.gpg"
      & $gpg.Source --batch --yes --pinentry-mode loopback --passphrase $pass -c --cipher-algo AES256 -o $out $zip 2>$null
      Remove-Item -LiteralPath $zip -Force
      return $out
    }
    return $zip
  } finally {
    Remove-Item -Recurse -Force -LiteralPath $stage -ErrorAction SilentlyContinue
  }
}

function Invoke-Rotate { param([string]$dest, [int]$keep)
  $arcs = Get-ChildItem -LiteralPath $dest -Filter "$Stem-*" -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending
  if ($arcs.Count -gt $keep) {
    $arcs | Select-Object -Skip $keep | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
  }
}

function Cmd-Setup {
  $v = Resolve-Vault $Vault
  Say "Backing up: $v"
  $d = $Dest
  if (-not $d) {
    Say ""
    Say "Where should the off-machine backup go? Give a folder you already have"
    Say "somewhere OTHER than this machine's single disk - an external drive, or a"
    Say "OneDrive / Google Drive / Dropbox folder (one daily file syncs fine)."
    $d = Read-Host "Destination folder"
    if (-not $d) { Die "no destination given" }
  }
  if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
  $d = (Resolve-Path -LiteralPath $d).Path
  if ($d.StartsWith($v, [StringComparison]::OrdinalIgnoreCase)) { Die "destination is inside the vault. Pick a folder off this machine." }
  if ($d -match "OneDrive|Dropbox|Google ?Drive|CloudStorage|Box|[A-Z]:\\\\") { Ok "Destination chosen." } else { Warn "Destination may be on this same disk; an external drive or cloud folder protects against disk failure." }

  $slug = Slug-For $v
  if ($Encrypt) {
    if (-not (Get-Command gpg -ErrorAction SilentlyContinue)) { Die "-Encrypt needs gpg (Gpg4win) installed" }
    $p1 = Read-Host "Set a backup passphrase (stored encrypted via DPAPI)" -AsSecureString
    $p2 = Read-Host "Confirm passphrase" -AsSecureString
    $s1 = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p1))
    $s2 = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($p2))
    if (-not $s1) { Die "empty passphrase" }
    if ($s1 -ne $s2) { Die "passphrases do not match" }
    Store-Passphrase $slug $s1 | Out-Null
    Ok "Passphrase stored (DPAPI). Daily runs read it with no prompt."
  }

  Set-ConfEntry $v @{ dest = $d; archive_stem = $Stem; encrypt = [bool]$Encrypt; keep = $Keep; keychain_account = $slug; store_kind = "dpapi" }
  Ok "Saved config -> $Conf"

  Say ""
  Say "Taking the first snapshot..."
  $script:Vault = $v
  Cmd-Run

  if ($Schedule -eq "daily") {
    $taskName = "ai-brain-starter vault-backup $slug"
    try {
      # $PSCommandPath, NOT $MyInvocation.MyCommand.Path. We are inside a
      # function, where the latter describes the FUNCTION's invocation and is
      # EMPTY - so this registered `-File ""` and the task could never run.
      $self = $PSCommandPath
      if (-not $self) { throw "cannot resolve this script's own path" }

      # pwsh is PowerShell 7 and is NOT on a stock Windows install; the task
      # then dies with 0x80070002 (file not found) on the INTERPRETER.
      $exe = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
      if (-not $exe) { $exe = (Get-Command powershell.exe -ErrorAction SilentlyContinue).Source }
      if (-not $exe) { throw "no PowerShell interpreter found on PATH" }

      $action  = New-ScheduledTaskAction -Execute $exe -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$self`" run -Vault `"$v`""
      $trigger = New-ScheduledTaskTrigger -Daily -At 3am
      # Task Scheduler's DEFAULT settings refuse to start on battery, so on a
      # laptop the 03:00 run is refused every night (0x800710E0) and the vault
      # silently goes months without a snapshot. StartWhenAvailable catches up
      # a window missed while the machine was asleep.
      $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
      Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

      # Registering is not running. Register-ScheduledTask SUCCEEDS on an action
      # that can never execute, which is exactly how this shipped broken: setup
      # printed "Backup is live" over a task that never fired once. Read back
      # what we actually registered and prove both halves resolve.
      $reg = (Get-ScheduledTask -TaskName $taskName -ErrorAction Stop).Actions[0]
      $regFile = [regex]::Match($reg.Arguments, '-File\s+"([^"]+)"').Groups[1].Value
      if (-not $regFile -or -not (Test-Path -LiteralPath $regFile)) {
        throw "registered task points at a script path that does not exist: '$regFile'"
      }
      if (-not (Get-Command $reg.Execute -ErrorAction SilentlyContinue)) {
        throw "registered task points at an interpreter that does not resolve: '$($reg.Execute)'"
      }
      Ok "Daily backup scheduled (03:00 local), verified runnable."
    } catch {
      Warn "Could not register a working scheduled task: $($_.Exception.Message)"
      Warn "Run it yourself, or re-run setup: $PSCommandPath run -Vault `"$v`""
    }
  }
  Say ""
  Ok "Backup is live. Now prove it restores (do this once):"
  $hintExe = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
  if (-not $hintExe) { $hintExe = "powershell" }
  Say "    $hintExe `"$PSCommandPath`" verify -Vault `"$v`""
}

function Cmd-Run {
  $v = Resolve-Vault $Vault
  $e = Get-ConfEntry $v
  if (-not $e) { Die "vault not configured. Run: vault-backup.ps1 setup -Vault `"$v`"" }
  $dest = $e.dest
  if (-not (Test-Path -LiteralPath $dest)) { Die "backup destination unreachable: $dest (external disk unplugged?)" }
  $slug = if ($e.keychain_account) { $e.keychain_account } else { Slug-For $v }
  $stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
  $outBase = Join-Path $dest "$Stem-$stamp"
  $out = New-Archive $v $outBase ([bool]$e.encrypt) $slug
  $info = Get-Item -LiteralPath $out
  if ($info.Length -lt 256) { Remove-Item -LiteralPath $out -Force; Die "archive is suspiciously small" }
  $keep = if ($e.keep) { [int]$e.keep } else { 7 }
  Invoke-Rotate $dest $keep
  Set-ConfEntry $v @{ last = (IsoNow) }
  IsoNow | Set-Content -LiteralPath $Marker -Encoding UTF8
  Ok ("Snapshot: {0} ({1:N1} MB)" -f $out, ($info.Length / 1MB))
}

function Cmd-Verify {
  $v = Resolve-Vault $Vault
  $e = Get-ConfEntry $v
  if (-not $e) { Die "vault not configured. Run setup first." }
  $newest = Get-ChildItem -LiteralPath $e.dest -Filter "$Stem-*" -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $newest) { Die "no snapshot found in $($e.dest). Run: vault-backup.ps1 run -Vault `"$v`"" }
  $tmp = Join-Path ([IO.Path]::GetTempPath()) ("vbk-verify-" + [Guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  Say "Restoring $($newest.FullName) to a temp dir to prove it works..."
  try {
    if ($newest.Name -like "*.zip.gpg") {
      $gpg = Get-Command gpg -ErrorAction SilentlyContinue
      if (-not $gpg) { Die "cannot decrypt: gpg not found" }
      $slug = if ($e.keychain_account) { $e.keychain_account } else { Slug-For $v }
      $pass = Get-Passphrase $slug
      $zip = Join-Path $tmp "restore.zip"
      & $gpg.Source --batch --yes --pinentry-mode loopback --passphrase $pass -o $zip -d $newest.FullName 2>$null
      Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
      Remove-Item -LiteralPath $zip -Force
    } else {
      Expand-Archive -LiteralPath $newest.FullName -DestinationPath $tmp -Force
    }
    $count = (Get-ChildItem -Recurse -File -LiteralPath $tmp -ErrorAction SilentlyContinue).Count
    if ($count -lt 1) { Die "restore produced ZERO files - the backup is empty/broken" }
    $sentinel = if (Test-Path -LiteralPath (Join-Path $tmp "CLAUDE.md")) { ", CLAUDE.md present" } else { "" }
    Set-ConfEntry $v @{ last_verify = (IsoNow) }
    Ok "Restore verified: extracted $count file(s)$sentinel. Your backup actually restores."
  } finally {
    Remove-Item -Recurse -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
  }
}

function Cmd-Status {
  $v = Resolve-Vault $Vault
  Say "Vault: $v"
  $e = Get-ConfEntry $v
  if (-not $e) {
    Warn "No backup configured for this vault."
    Say  "Set one up: vault-backup.ps1 setup -Vault `"$v`""
  } else {
    $reach = if (Test-Path -LiteralPath $e.dest) { "(reachable)" } else { "(UNREACHABLE)" }
    Say "Destination: $($e.dest) $reach"
    Say "Encrypted:   $($e.encrypt)    Keep: $($e.keep)"
    $n = (Get-ChildItem -LiteralPath $e.dest -Filter "$Stem-*" -File -ErrorAction SilentlyContinue).Count
    Say "Snapshots:   $n in destination"
    Say "Last run:    $(if ($e.last) { $e.last } else { 'never' })"
    Say "Last verify: $(if ($e.last_verify) { $e.last_verify } else { 'never  (run: vault-backup.ps1 verify)' })"
  }
  $checker = Join-Path $PSScriptRoot "check-vault-backup.py"
  if (Test-Path -LiteralPath $checker) {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
    if ($py) { Say ""; & $py.Source $checker $v }
  }
}

switch ($Command.ToLower()) {
  "setup"  { Cmd-Setup }
  "run"    { Cmd-Run }
  "verify" { Cmd-Verify }
  "status" { Cmd-Status }
  default  { Die "unknown command: $Command (use setup|run|verify|status)" }
}
