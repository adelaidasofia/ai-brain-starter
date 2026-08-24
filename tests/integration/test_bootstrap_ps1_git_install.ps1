#!/usr/bin/env pwsh
# Test bootstrap.ps1's git install (Windows half of MYC-3895).
#
# Bug (2026-08-12 cohort install session): the install's FIRST command was
# `git clone`, and git is the one prerequisite neither bootstrap installed. The
# session transcript records the wall verbatim: "Git and Node.js CLI installs
# require administrator rights." Five participants ended a paid session still
# un-installed, because every no-admin recovery path already built into
# bootstrap.ps1 was unreachable: the script containing them could not be
# fetched.
#
# PR #488 fixed the FETCH (the entry command falls back to a zip, so
# bootstrap.ps1 now starts with no git). PR #502 fixed the macOS/Linux side of
# the same gap in bootstrap.sh. bootstrap.ps1 was still the half with NO git
# install at all: it calls `git clone` / `git pull` / `git fetch` itself, and
# `winget install -e --id Git.Git` never appeared anywhere in it. On a locked
# down laptop winget's Git package is a per-machine install an unelevated run
# cannot complete, which is exactly the reported wall.
#
# Fix: winget first (correct when it is allowed), then the official PortableGit
# self-extracting archive unpacked under LOCALAPPDATA with the USER PATH wired,
# needing no elevation at all - the same shape as the Node ZIP fallback that
# already ships a few sections below.
#
# Covered here:
#   A. The git block ships in bootstrap.ps1 between its markers and is
#      extractable, so this suite cannot drift from the shipped source.
#   B. Test-WorkingGit: PRESENCE IS NOT CAPABILITY. A `git` that EXISTS on PATH
#      and exits non-zero (a half-removed Git for Windows leaves exactly that,
#      and it is the same class as the macOS CLT stub bootstrap.sh guards) must
#      read ABSENT. Paired with a NEGATIVE CONTROL asserting a runnable git
#      reads WORKING, so this cannot pass by always answering false.
#   C. Sealed, empty PATH -> ABSENT. Doubles as the PATH-seal positive control:
#      if the runner's own real git leaked past the seal, this case fails.
#   D. Get-PortableGitUri points at the OFFICIAL git-for-windows release asset
#      and switches on architecture, with a control proving the arch argument
#      actually changes the answer (a hardcoded x64 URL would silently ship an
#      emulated git to every Windows-on-ARM laptop).
#   E. Test-TrustedGitArchive: the fallback DOWNLOADS AND RUNS an executable, so
#      the publisher check must bite. An unsigned file is rejected, and - on
#      Windows, where the check is real - a VALIDLY SIGNED binary from the wrong
#      publisher is rejected too. That second one is the control proving the
#      Status check is not the whole test. The positive half (a real PortableGit
#      accepted) cannot be proven here without a 60MB download; it lives in the
#      windows-install job, which fetches the real asset.
#   F. STRUCTURAL, over the shipped bootstrap.ps1:
#      F1 the git section runs BEFORE the clone/self-update section (a git
#         installed after the clone cannot help the run that needed it);
#      F2 the git path calls Err NOWHERE - the Done predicate for this ticket is
#         "0 entries in the red FAILED list", and git is not required for the
#         rest of the install, so a blocked git DEGRADES the run (auto-update
#         off) and must never be reported as a failure;
#      F3 the winget package id is named (the preferred path);
#      F4 the fallback wires the user PATH (a git unpacked where nothing looks
#         is invisible - the exact bug the Node ZIP fallback had to fix);
#      F5 nothing on the git path asks for elevation;
#      F6 the signature is verified BEFORE the downloaded binary is executed.
#
# PATH is sealed to a stub directory alone, so the runner's own git cannot
# decide any outcome. Runs on Windows PowerShell 5.1 and on pwsh 7
# (macOS/Linux); stubs are .cmd on Windows and /bin/sh scripts elsewhere.
#
# Self-contained; no network; never writes outside its own temp dir.

$ErrorActionPreference = "Stop"

$RepoRoot  = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Bootstrap = Join-Path $RepoRoot "bootstrap.ps1"
if (-not (Test-Path -LiteralPath $Bootstrap)) {
    Write-Host "ERROR: $Bootstrap not found" -ForegroundColor Red
    exit 1
}

$OnWindows = ($PSVersionTable.PSEdition -eq "Desktop") -or ($env:OS -eq "Windows_NT")
$script:Failures = @()

# Structural checks must read CODE, not prose. Without this, a comment
# EXPLAINING why a banned construct is absent ("gated on X, not on Y") matches
# the ban on Y and fails the very change it documents - which is exactly what
# happened while writing this suite.
function Remove-PsComments([string]$Text) {
    $keep = @()
    foreach ($line in ($Text -split "`n")) {
        if ($line -match '^\s*#') { continue }
        $keep += $line
    }
    return ($keep -join "`n")
}

function Check([bool]$Ok, [string]$Msg) {
    if ($Ok) {
        Write-Host "  ok    $Msg"
    } else {
        Write-Host "  FAIL  $Msg" -ForegroundColor Red
        $script:Failures += $Msg
    }
}

# A git stub: prints one line and exits with a fixed code. Test-WorkingGit only
# reads the exit code, so no stub has to imitate git's output format.
function New-GitStub([string]$Dir, [int]$ExitCode, [string]$EchoLine, [string]$StderrLine) {
    if ($OnWindows) {
        $p = Join-Path $Dir "git.cmd"
        $body = "@echo off`r`n"
        if ($EchoLine)   { $body += "echo $EchoLine`r`n" }
        if ($StderrLine) { $body += "echo $StderrLine 1>&2`r`n" }
        $body += "exit /b $ExitCode`r`n"
        [System.IO.File]::WriteAllText($p, $body, [System.Text.Encoding]::ASCII)
    } else {
        $p = Join-Path $Dir "git"
        $body = "#!/bin/sh`n"
        if ($EchoLine)   { $body += "echo '$EchoLine'`n" }
        if ($StderrLine) { $body += "echo '$StderrLine' >&2`n" }
        $body += "exit $ExitCode`n"
        [System.IO.File]::WriteAllText($p, $body, [System.Text.Encoding]::ASCII)
        & /bin/chmod "+x" $p
    }
    return $p
}

$TmpRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("abs-ps1-git-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $TmpRoot | Out-Null
$PathSaved = $env:PATH

function New-Case([string]$Name) {
    $d = Join-Path $TmpRoot $Name
    New-Item -ItemType Directory -Force -Path $d | Out-Null
    $env:PATH = $d                 # sealed: nothing but this directory
    return $d
}

try {
    # --- A. the block ships in bootstrap.ps1 and is extractable ---------------
    Write-Host "A. git block is present and extractable"
    $src = [System.IO.File]::ReadAllText($Bootstrap)
    $startMark = "# ai-brain:install-git:start"
    $endMark   = "# ai-brain:install-git:end"
    $si = $src.IndexOf($startMark)
    $ei = $src.IndexOf($endMark)
    Check ($si -ge 0) "bootstrap.ps1 carries the $startMark marker"
    Check ($ei -gt $si) "bootstrap.ps1 carries the $endMark marker after it"
    if ($si -lt 0 -or $ei -le $si) {
        Write-Host "ERROR: cannot extract the git block; the rest of this test would be vacuous." -ForegroundColor Red
        exit 1
    }
    $block = $src.Substring($si, $ei - $si)
    Check ($block -match "function Test-WorkingGit")       "extracted block defines Test-WorkingGit"
    Check ($block -match "function Get-PortableGitUri")    "extracted block defines Get-PortableGitUri"
    Check ($block -match "function Test-TrustedGitArchive") "extracted block defines Test-TrustedGitArchive"
    Check ($block -match "function Install-PortableGit")   "extracted block defines Install-PortableGit"
    # Dot-sourced in isolation, so a bootstrap-only helper leaking in would make
    # this suite pass while the shipped script throws.
    Check ($block -notmatch "(?m)^\s*(Warn|Log|Ok|Err|Hdr|Dry)\s") "extracted block calls no bootstrap-only helper"

    $blockFile = Join-Path $TmpRoot "gitblock.ps1"
    [System.IO.File]::WriteAllText($blockFile, $block, (New-Object System.Text.UTF8Encoding($false)))
    . $blockFile

    # --- B. presence is not capability ---------------------------------------
    Write-Host "B. a git that exists on PATH but cannot run"
    $d = New-Case "b"
    # Writes to STDERR and exits non-zero, like a git.exe shim left behind by a
    # half-removed install. The stderr half matters: under
    # $ErrorActionPreference = "Stop" a native command writing to stderr is what
    # crashed this bootstrap before (Bug 1 in windows-install.yml), so this
    # doubles as a regression control on the probe surviving a noisy candidate.
    $null = New-GitStub $d 1 "" "fatal: not a git repository (or any of the parent directories)"
    Check (-not (Test-WorkingGit)) "a git that exits non-zero reads ABSENT"

    Write-Host "B2. NEGATIVE CONTROL: a runnable git reads WORKING"
    $d = New-Case "b2"
    $null = New-GitStub $d 0 "git version 2.99.0" ""
    Check (Test-WorkingGit) "control: a runnable git reads WORKING (the probe is not always-false)"

    # --- C. sealed empty PATH (also the seal control) ------------------------
    Write-Host "C. nothing named git anywhere on PATH"
    $null = New-Case "c"
    Check (-not (Test-WorkingGit)) "reports ABSENT (seal control: no real git leaked in)"

    # --- D. the official asset, per architecture ------------------------------
    Write-Host "D. Get-PortableGitUri"
    $x64   = Get-PortableGitUri -Arch "AMD64"
    $arm   = Get-PortableGitUri -Arch "ARM64"
    Check ($x64 -like "https://github.com/git-for-windows/git/releases/download/*") "x64 URI is an official git-for-windows release asset (got '$x64')"
    Check ($arm -like "https://github.com/git-for-windows/git/releases/download/*") "arm64 URI is an official git-for-windows release asset (got '$arm')"
    Check ($x64 -match "PortableGit-.*64-bit\.7z\.exe$") "x64 URI names the PortableGit 64-bit self-extracting archive"
    Check ($arm -match "PortableGit-.*arm64\.7z\.exe$")  "arm64 URI names the PortableGit arm64 self-extracting archive"
    # CONTROL: the parameter is actually consulted. A hardcoded URL would pass
    # every check above and still hand an emulated x64 git to every ARM laptop.
    Check ($x64 -ne $arm) "control: architecture changes the answer (not a hardcoded URL)"

    # --- E. the publisher check bites ----------------------------------------
    Write-Host "E. Test-TrustedGitArchive"
    $unsigned = Join-Path $TmpRoot "not-really-git.exe"
    [System.IO.File]::WriteAllText($unsigned, "this is not a signed binary", [System.Text.Encoding]::ASCII)
    Check (-not (Test-TrustedGitArchive $unsigned)) "an unsigned file is REJECTED"
    if ($OnWindows) {
        Check ([bool](Get-Command Get-AuthenticodeSignature -ErrorAction SilentlyContinue)) "control: Get-AuthenticodeSignature exists here, so the check above was real"
        # A binary Windows itself signed: Status is Valid, publisher is wrong.
        # Rejecting it is what proves the publisher half is not decoration.
        $msSigned = Join-Path $env:WINDIR "System32\notepad.exe"
        if (Test-Path -LiteralPath $msSigned) {
            Check (-not (Test-TrustedGitArchive $msSigned)) "control: a VALIDLY SIGNED binary from the wrong publisher is REJECTED"
        } else {
            Write-Host "  note  $msSigned absent; skipped the wrong-publisher control"
        }
    } else {
        Write-Host "  note  not Windows: Get-AuthenticodeSignature does not exist, so the reject above is trivially true here."
        Write-Host "        windows-install.yml runs this same suite under PS 5.1, where it is the real check."
    }

    # --- F. structural, over the shipped bootstrap.ps1 -----------------------
    Write-Host "F. structural checks over the shipped bootstrap.ps1"
    $env:PATH = $PathSaved

    # F1: ordering. The git section must come before anything that USES git.
    $cloneIdx = $src.IndexOf('Test-Path "$SkillDir\.git"')
    Check ($cloneIdx -gt 0) "found the clone/self-update section to order against"
    Check ($si -lt $cloneIdx) "the git section runs BEFORE the clone/self-update section"

    # The git driver region: from the block start to the next section header.
    $pyIdx = $src.IndexOf("# ai-brain:pick-python:start")
    Check ($pyIdx -gt $si) "the Python section follows the git section"
    $gitRegion = Remove-PsComments $src.Substring($si, $pyIdx - $si)

    # F2: THE DONE PREDICATE. git is optional for everything except auto-update,
    # so a blocked git must never land in the red actionable-failures list.
    $errCalls = [regex]::Matches($gitRegion, "(?m)^\s*Err\s")
    Check ($errCalls.Count -eq 0) "the git path calls Err nowhere (found $($errCalls.Count)) - a blocked git degrades, never fails"

    # F3: winget first - it is the correct install whenever it is allowed.
    # Asserted on the INVOCATION, not on the package id appearing somewhere: the
    # id also appears in the IT-facing guidance text, so a bare id match stayed
    # green with the winget call itself mutated away (caught by this suite's own
    # mutation controls while it was being written).
    Check ($gitRegion -match "Run-Native \{ winget install -e --id Git\.Git") "winget install -e --id Git.Git is actually invoked"
    # And the same id reaches the human, so a blocked machine can hand IT the
    # exact command instead of a description of it.
    Check ($gitRegion -match "approve.*winget install -e --id Git\.Git") "the IT-facing guidance names the exact winget command"

    # F4: unpacking git where nothing looks for it installs nothing.
    Check ($gitRegion -match "Add-UserPathEntry") "the fallback wires the user PATH"

    # F5: the whole point is that this works without an admin password.
    Check ($gitRegion -notmatch "msiexec") "the git path never shells out to msiexec"
    Check ($gitRegion -notmatch "Invoke-Installer") "the git path never uses the elevation-requiring installer helper"
    Check ($gitRegion -notmatch "Test-Elevated") "the git path does not branch on elevation (the fallback needs none)"

    # F7: the archive-adopt branch must ask the CAPABILITY question too. A
    # PortableGit left half-unpacked by an interrupted run answers `Have git`
    # and then fails every git call Adopt-ArchiveInstall makes.
    $adoptIdx = $src.IndexOf("Adopt-ArchiveInstall -Dir")
    Check ($adoptIdx -gt 0) "found the archive-adopt branch"
    $adoptRegion = Remove-PsComments $src.Substring($cloneIdx, $adoptIdx - $cloneIdx)
    Check ($adoptRegion -match "Test-WorkingGit") "the archive-adopt branch gates on Test-WorkingGit"
    Check ($adoptRegion -notmatch "Have git") "the archive-adopt branch no longer gates on presence alone"

    # F6: verify before you execute. Ordering inside Install-PortableGit.
    $fnIdx = $block.IndexOf("function Install-PortableGit")
    Check ($fnIdx -ge 0) "found Install-PortableGit to order inside"
    $fnBody   = $block.Substring($fnIdx)
    $trustIdx = $fnBody.IndexOf("Test-TrustedGitArchive")
    $runIdx   = $fnBody.IndexOf("Start-Process")
    Check ($trustIdx -ge 0) "Install-PortableGit consults the publisher check"
    Check ($runIdx -ge 0)   "Install-PortableGit runs the self-extracting archive"
    Check ($trustIdx -lt $runIdx) "the signature is verified BEFORE the downloaded binary is executed"
}
finally {
    $env:PATH = $PathSaved
    Remove-Item -Recurse -Force -LiteralPath $TmpRoot -ErrorAction SilentlyContinue
}

Write-Host ""
if ($script:Failures.Count -gt 0) {
    Write-Host "FAILED - bootstrap.ps1 git install ($($script:Failures.Count) check(s)):" -ForegroundColor Red
    foreach ($f in $script:Failures) { Write-Host "  x $f" -ForegroundColor Red }
    exit 1
}
Write-Host "PASSED - bootstrap.ps1 git install" -ForegroundColor Green
exit 0
