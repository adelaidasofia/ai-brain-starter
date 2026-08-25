#!/usr/bin/env pwsh
# Test bootstrap.ps1's Python interpreter discovery (Windows side of #538).
#
# Bug (ai-brain-starter#290): bootstrap.ps1 detected Python by testing ONE name:
#
#     $v = (python --version 2>&1) -replace 'Python ',''
#     if ([version]$v -ge [version]"3.10") { $pythonOk = $true }
#
# On Windows that is wrong three separate ways:
#   1. `py -3.12` (the Python Launcher) is the canonical multi-version resolver
#      and was never consulted, so a box with 3.12 installed but `python` unset
#      or shadowed read as "no Python at all" and got a redundant install.
#   2. The WindowsApps `python` STUB is on PATH and opens the Microsoft Store
#      instead of running an interpreter. It answers a presence check, and its
#      output does not parse as a [version], so the cast threw and $pythonOk
#      stayed $false -- installing a Python that was already there.
#   3. A real but too-old `python` (3.9) shadows a newer one later in PATH, and
#      installing 3.12 cannot change what the NAME `python` resolves to.
#
# Covered here:
#   A. The resolver is present in the shipped bootstrap.ps1 and is extractable.
#   B. THE REPORTED CASE: `python` is a Store-style stub, `py -3.12` works ->
#      the launcher's interpreter is chosen. Paired with a NEGATIVE CONTROL
#      asserting that stub really does fail the probe on its own, so this cannot
#      pass because the fixture forgot to model the bug.
#   C. `python` already qualifies -> it is kept, and a newer `py -3.14` does NOT
#      displace it. This is the "changes nothing for working installs" claim.
#   D. `python` present but too old -> falls through to the launcher (+ control).
#   E. Newest-first ordering among several `py -3.x`.
#   F. Nothing qualifies -> reports failure instead of silently selecting a
#      too-old interpreter. Doubles as the PATH-seal positive control: if the
#      runner's own real Python leaked past the seal, this case would pass.
#   G. AI_BRAIN_PYTHON names an interpreter no search would find, and WINS over
#      a qualifying `python`.
#   H. AI_BRAIN_PYTHON set but not a 3.10+ interpreter -> flagged, search
#      continues (a silently ignored override is the bug class this repo calls
#      SILENT-NO-OP).
#   I. STRUCTURAL: no bare `python` invocation is left in bootstrap.ps1, and the
#      resolved interpreter is actually used at the call sites. One missed call
#      site silently runs the wrong interpreter, which is the whole defect.
#
# Resolve-Python is EXTRACTED from the real bootstrap.ps1 between its
# `ai-brain:pick-python` markers, never reimplemented here, so this test cannot
# drift from the shipped source -- same technique as the bash-side
# test_bootstrap_python_discovery.sh.
#
# PATH is sealed to a stub directory alone, so the runner's own interpreters
# cannot decide any outcome (case F is the control proving the seal holds).
# Runs on Windows PowerShell 5.1 and on pwsh 7 (macOS/Linux); stubs are .cmd on
# Windows and /bin/sh scripts elsewhere.
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

function Check([bool]$Ok, [string]$Msg) {
    if ($Ok) {
        Write-Host "  ok    $Msg"
    } else {
        Write-Host "  FAIL  $Msg" -ForegroundColor Red
        $script:Failures += $Msg
    }
}

# --- stub interpreters --------------------------------------------------------
# A stub prints ONE line to stdout and exits with a fixed code. That is enough
# for both probes the resolver makes, because the sys.executable probe only ever
# runs after the version probe passed:
#   version probe     -> only the exit code is read
#   sys.executable    -> only the first stdout line is read
# So no stub ever has to parse the Python source it is handed, which keeps the
# cmd.exe side free of quoting hazards.
function New-InterpreterStub([string]$Dir, [string]$Name, [int]$ExitCode, [string]$EchoLine, [string]$StderrLine) {
    if ($OnWindows) {
        $p = Join-Path $Dir "$Name.cmd"
        $body = "@echo off`r`n"
        if ($EchoLine)   { $body += "echo $EchoLine`r`n" }
        if ($StderrLine) { $body += "echo $StderrLine 1>&2`r`n" }
        $body += "exit /b $ExitCode`r`n"
        [System.IO.File]::WriteAllText($p, $body, [System.Text.Encoding]::ASCII)
    } else {
        $p = Join-Path $Dir $Name
        $body = "#!/bin/sh`n"
        if ($EchoLine)   { $body += "echo '$EchoLine'`n" }
        if ($StderrLine) { $body += "echo '$StderrLine' >&2`n" }
        $body += "exit $ExitCode`n"
        [System.IO.File]::WriteAllText($p, $body, [System.Text.Encoding]::ASCII)
        & /bin/chmod "+x" $p
    }
    return $p
}

# The Python Launcher: dispatches on its FIRST argument (-3.12, -3.13, ...) and
# prints the interpreter it selected. An unknown version exits non-zero, which
# is what the real launcher does when that version is not installed.
function New-PyLauncherStub([string]$Dir, [hashtable]$VersionMap) {
    if ($OnWindows) {
        $p = Join-Path $Dir "py.cmd"
        $body = "@echo off`r`n"
        foreach ($k in $VersionMap.Keys) {
            $body += "if `"%1`"==`"$k`" ( echo $($VersionMap[$k]) & exit /b 0 )`r`n"
        }
        $body += "exit /b 103`r`n"
        [System.IO.File]::WriteAllText($p, $body, [System.Text.Encoding]::ASCII)
    } else {
        $p = Join-Path $Dir "py"
        $body = "#!/bin/sh`ncase `"`$1`" in`n"
        foreach ($k in $VersionMap.Keys) {
            $body += "  $k) echo '$($VersionMap[$k])'; exit 0 ;;`n"
        }
        $body += "esac`nexit 103`n"
        [System.IO.File]::WriteAllText($p, $body, [System.Text.Encoding]::ASCII)
        & /bin/chmod "+x" $p
    }
    return $p
}

$TmpRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("abs-ps1-py-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $TmpRoot | Out-Null
$PathSaved     = $env:PATH
$OverrideSaved = $env:AI_BRAIN_PYTHON

function New-Case([string]$Name) {
    $d = Join-Path $TmpRoot $Name
    New-Item -ItemType Directory -Force -Path $d | Out-Null
    $env:PATH = $d                 # sealed: nothing but this directory
    $env:AI_BRAIN_PYTHON = $null
    $script:PythonExe             = $null
    $script:PythonArgs            = @()
    $script:PythonOverrideIgnored = $false
    return $d
}

try {
    # --- A. the resolver ships in bootstrap.ps1 and is extractable ------------
    Write-Host "A. resolver is present and extractable"
    $src = [System.IO.File]::ReadAllText($Bootstrap)
    $startMark = "# ai-brain:pick-python:start"
    $endMark   = "# ai-brain:pick-python:end"
    $si = $src.IndexOf($startMark)
    $ei = $src.IndexOf($endMark)
    Check ($si -ge 0) "bootstrap.ps1 carries the $startMark marker"
    Check ($ei -gt $si) "bootstrap.ps1 carries the $endMark marker after it"
    if ($si -lt 0 -or $ei -le $si) {
        Write-Host "ERROR: cannot extract the resolver; the rest of this test would be vacuous." -ForegroundColor Red
        exit 1
    }
    $block = $src.Substring($si, $ei - $si)
    Check ($block -match "function Resolve-Python")       "extracted block defines Resolve-Python"
    Check ($block -match "function Test-PythonCandidate") "extracted block defines Test-PythonCandidate"
    # The block is dot-sourced in isolation, so a bootstrap-only helper leaking
    # into it would make this test pass while the shipped script throws.
    Check ($block -notmatch "(?m)^\s*(Warn|Log|Ok|Err|Hdr|Dry)\s") "extracted block calls no bootstrap-only helper"
    Check ($block -match "py") "extracted block consults the py launcher"

    $blockFile = Join-Path $TmpRoot "resolver.ps1"
    [System.IO.File]::WriteAllText($blockFile, $block, (New-Object System.Text.UTF8Encoding($false)))
    . $blockFile

    # --- B. THE REPORTED CASE ------------------------------------------------
    Write-Host "B. Store-stub python + working py -3.12"
    $d = New-Case "b"
    # Writes to STDERR and exits 9009, exactly like the real WindowsApps alias.
    # The stderr half matters: under $ErrorActionPreference = "Stop" a native
    # command writing to stderr is what crashed this bootstrap before (Bug 1 in
    # windows-install.yml), so this doubles as a regression control on the probe
    # surviving a noisy candidate.
    $storeStub = New-InterpreterStub $d "python" 9009 "" "Python was not found; run without arguments to install from the Microsoft Store"
    $p312      = New-InterpreterStub $d "python312" 0 ""
    $null      = New-PyLauncherStub  $d @{ "-3.12" = $p312 }
    # NEGATIVE CONTROL: the fixture really does model the bug.
    Check (-not (Test-PythonCandidate $storeStub @())) "control: the Store-style stub FAILS the probe on its own"
    $ok = Resolve-Python
    Check $ok "resolver reports success"
    Check ($script:PythonExe -eq $p312) "picked the launcher's 3.12 (got '$($script:PythonExe)')"
    Check ($script:PythonArgs.Count -eq 0) "collapsed 'py -3.12' to a plain exe (no prefix args to forget)"

    # --- C. a qualifying python is kept --------------------------------------
    Write-Host "C. qualifying python is kept, newer py does not displace it"
    $d = New-Case "c"
    $pyth = New-InterpreterStub $d "python" 0 ""
    $p314 = New-InterpreterStub $d "python314" 0 ""
    $null = New-PyLauncherStub  $d @{ "-3.14" = $p314 }
    $ok = Resolve-Python
    Check $ok "resolver reports success"
    Check ($script:PythonExe -eq $pyth) "kept 'python' (got '$($script:PythonExe)')"

    # --- D. too-old python falls through to the launcher ---------------------
    Write-Host "D. python present but older than 3.10"
    $d = New-Case "d"
    $old  = New-InterpreterStub $d "python" 1 ""    # exits 1 => below 3.10
    $p312 = New-InterpreterStub $d "python312" 0 ""
    $null = New-PyLauncherStub  $d @{ "-3.12" = $p312 }
    Check (-not (Test-PythonCandidate $old @())) "control: the too-old stub FAILS the probe on its own"
    $ok = Resolve-Python
    Check $ok "resolver reports success"
    Check ($script:PythonExe -eq $p312) "skipped the too-old python (got '$($script:PythonExe)')"

    # --- E. newest-first among launcher versions -----------------------------
    Write-Host "E. newest-first ordering across py -3.x"
    $d = New-Case "e"
    $p312 = New-InterpreterStub $d "python312" 0 ""
    $p313 = New-InterpreterStub $d "python313" 0 ""
    $null = New-PyLauncherStub  $d @{ "-3.12" = $p312; "-3.13" = $p313 }
    $ok = Resolve-Python
    Check $ok "resolver reports success"
    Check ($script:PythonExe -eq $p313) "took 3.13 over 3.12 (got '$($script:PythonExe)')"

    # --- F. nothing qualifies (also the PATH-seal control) -------------------
    Write-Host "F. nothing qualifying on PATH"
    $d = New-Case "f"
    $ok = Resolve-Python
    Check (-not $ok) "resolver reports FAILURE rather than picking something unusable"
    Check ($null -eq $script:PythonExe) "PythonExe left null (seal control: no real interpreter leaked in)"

    # --- G. AI_BRAIN_PYTHON wins ---------------------------------------------
    Write-Host "G. AI_BRAIN_PYTHON names an interpreter no search would find"
    $d = New-Case "g"
    $hidden = New-InterpreterStub $TmpRoot "conda-ish-python" 0 ""   # NOT on PATH
    $null   = New-InterpreterStub $d "python" 0 ""                   # would otherwise win
    $env:AI_BRAIN_PYTHON = $hidden
    $ok = Resolve-Python
    Check $ok "resolver reports success"
    Check ($script:PythonExe -eq $hidden) "override chosen over a qualifying python (got '$($script:PythonExe)')"
    Check (-not $script:PythonOverrideIgnored) "override not flagged as ignored"

    # --- H. AI_BRAIN_PYTHON set but unusable ---------------------------------
    Write-Host "H. AI_BRAIN_PYTHON set but not a 3.10+ interpreter"
    $d = New-Case "h"
    $badOverride = New-InterpreterStub $TmpRoot "too-old-override" 1 ""
    $good        = New-InterpreterStub $d "python" 0 ""
    $env:AI_BRAIN_PYTHON = $badOverride
    $ok = Resolve-Python
    Check $ok "resolver still finds a working interpreter"
    Check ($script:PythonExe -eq $good) "fell back to the PATH python (got '$($script:PythonExe)')"
    Check ($script:PythonOverrideIgnored) "ignored override is FLAGGED, not swallowed"

    # --- I. structural: no bare python invocation survives --------------------
    Write-Host "I. structural check over the shipped bootstrap.ps1"
    $env:PATH = $PathSaved
    $bare = @()
    $lineNo = 0
    foreach ($line in ([System.IO.File]::ReadAllLines($Bootstrap))) {
        $lineNo++
        if ($line -match '^\s*#') { continue }                 # comment
        if ($line -match '(\||&|\{|^)\s*python3?\s+-') { $bare += "$lineNo : $($line.Trim())" }
        elseif ($line -match '\(\s*python3?\s+--version') { $bare += "$lineNo : $($line.Trim())" }
    }
    Check ($bare.Count -eq 0) "no bare python invocation left in bootstrap.ps1"
    foreach ($b in $bare) { Write-Host "        $b" -ForegroundColor Red }
    $routed = ([regex]::Matches($src, '&\s+\$PythonExe\s+@PythonArgs')).Count
    Check ($routed -ge 5) "resolved interpreter is used at the call sites (found $routed, want >= 5)"
}
finally {
    $env:PATH = $PathSaved
    if ($OverrideSaved) { $env:AI_BRAIN_PYTHON = $OverrideSaved } else { $env:AI_BRAIN_PYTHON = $null }
    Remove-Item -Recurse -Force -LiteralPath $TmpRoot -ErrorAction SilentlyContinue
}

Write-Host ""
if ($script:Failures.Count -gt 0) {
    Write-Host "FAILED - bootstrap.ps1 Python discovery ($($script:Failures.Count) check(s)):" -ForegroundColor Red
    foreach ($f in $script:Failures) { Write-Host "  x $f" -ForegroundColor Red }
    exit 1
}
Write-Host "PASSED - bootstrap.ps1 Python discovery" -ForegroundColor Green
exit 0
