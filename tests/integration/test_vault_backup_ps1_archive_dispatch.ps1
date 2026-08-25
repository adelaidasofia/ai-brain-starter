#!/usr/bin/env pwsh
# Test that `vault-backup.ps1 verify` opens the archive that is ACTUALLY there.
#
# THE BUG
# -------
# Both halves of the backup feature write into the same destination folder and
# share ~/.claude/.vault-backup.conf, but they do not write the same kind of
# archive: vault-backup.ps1 writes .zip/.zip.gpg, vault-backup.sh writes
# .tar.gz/.tar.gz.gpg. Verify only ever handled zip, so a vault whose snapshots
# came from the .sh half fed the archive straight to Expand-Archive and got:
#
#     ".gpg is not a supported archive file format. .zip is the only supported
#      archive file format."
#
# That is not an obscure combination. The install phases hand every user the
# .sh setup command (phases/phase-01-welcome.md, phases/phase-12-17-imports-
# rules.md), it runs fine on Windows under Git Bash, and surface-backup-status.py
# then hands that same user the .ps1 command on Windows. Nothing warns them the
# two do not interoperate.
#
# The failure mode is what makes it worth a test: the backup is FINE and the
# tool says it is broken, at the one moment a user goes looking for reassurance.
#
# COVERED HERE
#   A. .tar.gz snapshot verifies. THE REPORTED CASE. Paired with a NEGATIVE
#      CONTROL that Expand-Archive really does refuse that same file, so this
#      cannot pass because the fixture forgot to model the bug.
#   B. .zip snapshot still verifies. The "changes nothing that worked" claim.
#   C. An extension neither half writes fails with a sentence that names the
#      problem, instead of an archive-format exception about a .zip nobody asked
#      for.
#   D. An EMPTY .tar.gz still reports ZERO files. The whole point of verify is
#      opening the archive; teaching it a new format must not cost that.
#   E. A .gpg snapshot whose passphrase store was written by the .sh half points
#      at the command that works, instead of throwing a raw
#      CryptographicException that reads as a damaged secret.
#      SKIPPED (loudly) where gpg is absent: verify checks for gpg first.
#
# Runs on Windows PowerShell 5.1 and on pwsh 7 (macOS/Linux).
# Self-contained; no network; never writes outside its own temp dir, and never
# touches the real ~/.claude/.vault-backup.conf (VAULT_BACKUP_CONF is redirected).

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Script   = Join-Path (Join-Path $RepoRoot "scripts") "vault-backup.ps1"
if (-not (Test-Path -LiteralPath $Script)) {
    Write-Host "ERROR: $Script not found" -ForegroundColor Red
    exit 1
}

$PSExe = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
$IsWin = ($env:OS -eq "Windows_NT")

$Failures = 0
$Root = Join-Path ([IO.Path]::GetTempPath()) ("vbk-dispatch-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $Root | Out-Null

function Fail($m) { $script:Failures++; Write-Host "FAIL $m" -ForegroundColor Red }
function Pass($m) { Write-Host "PASS $m" -ForegroundColor Green }
function Skip($m) { Write-Host "SKIP $m" -ForegroundColor Yellow }

function Find-Tar {
    if ($IsWin) {
        $sys = Join-Path $env:SystemRoot "System32\tar.exe"
        if (Test-Path -LiteralPath $sys) { return $sys }
    }
    $c = Get-Command tar -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    return $null
}

# One disposable vault + destination + conf per case, so no case can inherit
# another's state (a stale last_verify would make a broken case look fine).
function New-Case {
    param([string]$name, [bool]$withContent = $true)
    $case  = Join-Path $Root $name
    $vault = Join-Path $case "vault"
    $dest  = Join-Path $case "dest"
    New-Item -ItemType Directory -Force -Path $vault | Out-Null
    New-Item -ItemType Directory -Force -Path $dest  | Out-Null
    if ($withContent) {
        Set-Content -LiteralPath (Join-Path $vault "CLAUDE.md") -Value "# test vault" -Encoding UTF8
        Set-Content -LiteralPath (Join-Path $vault "note.md")   -Value "hello"        -Encoding UTF8
    }
    $conf = Join-Path $case "conf.json"
    $key  = (Resolve-Path -LiteralPath $vault).Path
    $obj  = [pscustomobject]@{ vaults = [pscustomobject]@{} }
    $obj.vaults | Add-Member -NotePropertyName $key -NotePropertyValue ([pscustomobject]@{
        dest = $dest; encrypt = $false; keep = 7; last = "2026-01-01T00:00:00Z"
    }) -Force
    $obj | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $conf -Encoding UTF8
    return [pscustomobject]@{ Vault = $key; Dest = $dest; Conf = $conf }
}

function Invoke-Verify {
    param([pscustomobject]$c)
    $env:VAULT_BACKUP_CONF = $c.Conf
    $args = @("-NoProfile")
    if ($IsWin) { $args += @("-ExecutionPolicy", "Bypass") }
    $args += @("-File", $Script, "verify", "-Vault", $c.Vault)
    # EAP must drop to Continue around the child. Under 'Stop', Windows
    # PowerShell 5.1 turns a native command's stderr into a TERMINATING
    # NativeCommandError, so the first case whose script writes to stderr would
    # abort the whole suite instead of being reported as one FAIL -- which is
    # exactly what a regression looks like, and exactly when the other cases
    # matter most.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $PSExe @args 2>&1 | Out-String
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEAP
    }
    return [pscustomobject]@{ Code = $code; Out = $out }
}

$Tar = Find-Tar
if (-not $Tar) {
    Write-Host "ERROR: no tar on PATH; this suite cannot build its fixtures" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- A: .tar.gz --
$c = New-Case "a-targz"
$snap = Join-Path $c.Dest "vault-backup-20260101-120000.tar.gz"
& $Tar -czf $snap -C $c.Vault "CLAUDE.md" "note.md"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $snap)) {
    Fail "A: could not build the .tar.gz fixture"
} else {
    # NEGATIVE CONTROL FIRST: prove Expand-Archive really refuses this file. If
    # some future PowerShell learns tar, this control fails and tells us the
    # test stopped modelling the bug -- rather than passing for a stale reason.
    $ctl = Join-Path $Root "a-control"
    New-Item -ItemType Directory -Force -Path $ctl | Out-Null
    $refused = $false
    try { Expand-Archive -LiteralPath $snap -DestinationPath $ctl -Force } catch { $refused = $true }
    if (-not $refused) {
        Fail "A(control): Expand-Archive accepted a .tar.gz, so this case no longer models the bug"
    } else {
        Pass "A(control): Expand-Archive refuses a .tar.gz, as the bug requires"
    }

    $r = Invoke-Verify $c
    if ($r.Code -eq 0 -and $r.Out -match "Restore verified" -and $r.Out -match "CLAUDE\.md present") {
        Pass "A: a .tar.gz snapshot verifies"
    } else {
        Fail "A: .tar.gz did not verify (exit $($r.Code)): $($r.Out.Trim())"
    }
}

# ------------------------------------------------------------------- B: .zip --
$c = New-Case "b-zip"
$snap = Join-Path $c.Dest "vault-backup-20260101-120000.zip"
Compress-Archive -Path (Join-Path $c.Vault "*") -DestinationPath $snap -Force
$r = Invoke-Verify $c
if ($r.Code -eq 0 -and $r.Out -match "Restore verified") {
    Pass "B: a .zip snapshot still verifies"
} else {
    Fail "B: .zip regressed (exit $($r.Code)): $($r.Out.Trim())"
}

# ------------------------------------------------------- C: unknown extension --
$c = New-Case "c-unknown"
$snap = Join-Path $c.Dest "vault-backup-20260101-120000.rar"
Set-Content -LiteralPath $snap -Value "not really an archive" -Encoding UTF8
$r = Invoke-Verify $c
if ($r.Code -ne 0 -and $r.Out -match "don't know how to open") {
    Pass "C: an unknown extension says so plainly"
} else {
    Fail "C: expected a plain 'don't know how to open' (exit $($r.Code)): $($r.Out.Trim())"
}

# ---------------------------------------------------------- D: empty .tar.gz --
$c = New-Case "d-empty" $false
$empty = Join-Path $Root "d-empty-src"
New-Item -ItemType Directory -Force -Path $empty | Out-Null
$snap = Join-Path $c.Dest "vault-backup-20260101-120000.tar.gz"
& $Tar -czf $snap -C $empty "."
if ($LASTEXITCODE -ne 0) {
    Fail "D: could not build the empty .tar.gz fixture"
} else {
    $r = Invoke-Verify $c
    if ($r.Code -ne 0 -and $r.Out -match "ZERO files") {
        Pass "D: an empty .tar.gz is still reported as empty"
    } else {
        Fail "D: an empty archive passed as a good backup (exit $($r.Code)): $($r.Out.Trim())"
    }
}

# ------------------------------------------- E: .sh-written passphrase store --
if (-not (Get-Command gpg -ErrorAction SilentlyContinue)) {
    Skip "E: gpg not installed here; verify stops at its own gpg check before reaching the passphrase"
} else {
    $c = New-Case "e-shpass"
    $snap = Join-Path $c.Dest "vault-backup-20260101-120000.tar.gz.gpg"
    Set-Content -LiteralPath $snap -Value "encrypted bytes" -Encoding UTF8
    # Point the passphrase lookup at a disposable home and plant the .sh half's
    # format there: the passphrase itself, not a DPAPI blob.
    $home2 = Join-Path $Root "e-home"
    New-Item -ItemType Directory -Force -Path (Join-Path $home2 ".claude") | Out-Null
    Set-Content -LiteralPath (Join-Path $home2 ".claude\.vault-backup-pass-testslug") `
        -Value "s3cr3t-passphrase-not-a-dpapi-blob" -Encoding UTF8
    $conf = Get-Content -Raw -LiteralPath $c.Conf | ConvertFrom-Json
    $conf.vaults.($c.Vault) | Add-Member -NotePropertyName keychain_account -NotePropertyValue "testslug" -Force
    $conf.vaults.($c.Vault) | Add-Member -NotePropertyName encrypt -NotePropertyValue $true -Force
    $conf | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $c.Conf -Encoding UTF8

    $savedProfile = $env:USERPROFILE
    $env:USERPROFILE = $home2
    try { $r = Invoke-Verify $c } finally { $env:USERPROFILE = $savedProfile }

    if ($r.Code -ne 0 -and $r.Out -match "vault-backup\.sh" -and $r.Out -notmatch "CryptographicException") {
        Pass "E: a .sh-written passphrase store points at the command that works"
    } else {
        Fail "E: expected a pointer to vault-backup.sh (exit $($r.Code)): $($r.Out.Trim())"
    }
}

Remove-Item -Recurse -Force -LiteralPath $Root -ErrorAction SilentlyContinue

if ($Failures -gt 0) {
    Write-Host "$Failures case(s) failed" -ForegroundColor Red
    exit 1
}
Write-Host "all cases passed" -ForegroundColor Green
exit 0
