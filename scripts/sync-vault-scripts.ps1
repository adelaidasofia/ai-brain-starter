# sync-vault-scripts.ps1 - Windows port of sync-vault-scripts.sh.
#
# Propagates updated vault-side scripts from the ai-brain-starter repo into the
# user's vault <meta>/scripts/ directory, so an EXISTING install gets new/fixed
# vault scripts (e.g. journal-preflight.py) that setup only copied once.
#
# ZERO-DRIFT MANIFEST: this reads the SAME allow-list as the bash version by
# parsing the VAULT_SCRIPTS=( ... ) block out of sync-vault-scripts.sh. There is
# no second hardcoded list to drift out of sync (the self-test parses the .sh
# array too, so both platforms and the test share one source of truth).
#
# CONTRACT (mirrors the .sh): idempotent (identical dest = no-op); non-destructive
# (a dest that DIFFERS is backed up to <file>.bak-YYYY-MM-DD-HHMM before overwrite);
# source-absent is non-fatal; no vault resolvable = non-fatal no-op (exit 0).
#
# THE SOURCE MUST BE TRUSTWORTHY ($env:ABS_CLONE_PATCHES_STASHED)
# This script assumes the repo holds what the user expects it to hold. bootstrap
# auto-stashes local uncommitted changes before its git pull, and in that window
# the checkout is pristine upstream with the user's patches REMOVED, so running
# here copies the UNPATCHED file over the patched one in the vault. Reported on
# Windows 11: session-close-runner.sh and vault-safe-commit.sh under
# "<gear> Meta/scripts" were both reverted this way. The .bak is written and
# holds the good version, which is exactly why it goes unnoticed: the run prints
# "Updated: 2" like any healthy sync. bootstrap.ps1 sets this variable around
# the pull and we refuse to propagate while it is set. Stale-but-working beats
# silently-regressed.
#
# VAULT RESOLUTION (zero-arg friendly, for the update flow):
#   1. -Vault PATH   2. $env:VAULT_ROOT   3. parse ~/.claude/settings.json hooks.
#
# USAGE:  pwsh scripts/sync-vault-scripts.ps1 [-Vault PATH] [-DryRun] [-Quiet]
# EXIT:   0 = clean / nothing to do / vault not resolvable; 2 = a copy/backup error.
#
# Compatible with Windows PowerShell 5.1 and PowerShell 7+ (no PS7-only syntax).

param(
    [string]$Vault = "",
    [switch]$DryRun,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$Stamp = Get-Date -Format "yyyy-MM-dd-HHmm"
$ScriptDir = Split-Path -Parent $PSCommandPath
$StarterDir = if ($env:STARTER_DIR) { $env:STARTER_DIR } else { Split-Path -Parent $ScriptDir }

function Note($m) { if (-not $Quiet) { Write-Output $m } }

# --- REFUSE to propagate from a knowingly-degraded checkout -------------------
# See "THE SOURCE MUST BE TRUSTWORTHY" in the header. Written with Write-Output,
# NOT Note: this must print even under -Quiet, because bootstrap calls it with
# -Quiet and the entire failure mode is that the step looks routine.
if ($env:ABS_CLONE_PATCHES_STASHED) {
    Write-Output "sync-vault-scripts: SKIPPED - not propagating to the vault."
    Write-Output "  This bootstrap run stashed your local changes to $StarterDir before"
    Write-Output "  pulling, so the scripts here are currently the UNPATCHED upstream copies."
    Write-Output "  Copying them into the vault would overwrite your patched vault scripts"
    Write-Output "  with the versions you patched them to fix. Your vault is untouched."
    Write-Output "  Your changes are in: git stash -> $($env:ABS_CLONE_PATCHES_STASHED)"
    Write-Output "  To restore them and then sync the vault:"
    Write-Output "    cd `"$StarterDir`"; git stash pop; pwsh scripts/sync-vault-scripts.ps1"
    exit 0
}

# --- pick a REAL python interpreter (for the shared meta resolver) ------------
# 'py' first: the Windows launcher is always a real Python. A bare 'python3' on
# PATH is often the Microsoft Store app-execution shim, which resolves via
# Get-Command but prints nothing and pops the Store, so the resolver call comes
# back empty and the sync misreads it as "no Meta folder". Probe each candidate
# and only trust one that actually reports Python 3 (mirrors relocate-vault.ps1).
$Py = $null
foreach ($c in @("py", "python", "python3")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
        $probe = (& $cmd.Source -c "import sys; print(sys.version_info[0])" 2>$null | Select-Object -First 1)
        if ("$probe".Trim() -eq "3") { $Py = $cmd.Source; break }
    } catch { }
}

# Read one line of native-python stdout as UTF-8. The resolver emits the meta
# path as UTF-8 (it can contain the settings emoji); Windows PowerShell would
# otherwise decode native output with the OEM code page and mangle the path.
# Save and restore the console encoding so this cannot leak to the parent shell.
function Invoke-PyLine {
    param([string]$PyExe, [object[]]$PyArgs)
    $prevEnc = [Console]::OutputEncoding
    try {
        try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
        return (& $PyExe @PyArgs 2>$null | Select-Object -First 1)
    } finally {
        try { [Console]::OutputEncoding = $prevEnc } catch { }
    }
}

# --- resolve the vault root ---------------------------------------------------
if (-not $Vault) { $Vault = $env:VAULT_ROOT }
if (-not $Vault) {
    $settings = Join-Path $HOME ".claude/settings.json"
    if ((Test-Path $settings) -and $Py) {
        # Reuse the exact regex the .sh uses: the installed hook commands embed
        # "<vault>/⚙️ Meta/scripts/..." - grab that prefix. POSIX root, Windows
        # drive letter or UNC share, either separator; byte-identical to
        # heal-journal-guard.py's _VAULT_FROM_CMD_RE, whose self-test pins it.
        # This is the WINDOWS leg, so a `/`-only pattern never resolved a vault
        # here and the sync silently skipped as "no vault resolved" (#370/#394).
        $pyCode = @'
import json, re, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
for _ev, groups in (data.get("hooks") or {}).items():
    for g in groups:
        for h in g.get("hooks", []):
            m = re.search(r"((?:[A-Za-z]:)?[\\/][^'\"]+?)[\\/](?:⚙️ Meta|Meta)[\\/]scripts[\\/]", h.get("command",""))
            if m:
                print(m.group(1)); sys.exit(0)
sys.exit(1)
'@
        try { $Vault = (Invoke-PyLine $Py @("-c", $pyCode, $settings)) } catch { $Vault = "" }
    }
}
if ((-not $Vault) -or (-not (Test-Path $Vault -PathType Container))) {
    Note "sync-vault-scripts: no vault resolved (-Vault / `$env:VAULT_ROOT / settings.json all empty) - skipping (non-fatal)."
    exit 0
}

# --- resolve the vault meta dir via the SHARED python resolver ----------------
$Meta = ""
if ($Py) {
    $resolver = Join-Path $ScriptDir "_meta_resolver.py"
    if (Test-Path $resolver) {
        try { $Meta = (Invoke-PyLine $Py @($resolver, $Vault, "scripts", "Decisions", "Sessions")) } catch { $Meta = "" }
    }
}
if (-not $Meta) {
    Note "sync-vault-scripts: no Meta folder in $Vault - skipping (non-fatal)."
    exit 0
}
$DestDir = Join-Path $Meta "scripts"

# --- parse the manifest out of the .sh (single source of truth) ---------------
$ShFile = Join-Path $ScriptDir "sync-vault-scripts.sh"
if (-not (Test-Path $ShFile)) {
    Note "sync-vault-scripts: manifest source $ShFile missing - skipping (non-fatal)."
    exit 0
}
$scripts = @()
$inArray = $false
foreach ($line in (Get-Content -LiteralPath $ShFile)) {
    if ($line -match 'VAULT_SCRIPTS=\(') { $inArray = $true; continue }
    if ($inArray) {
        if ($line -match '^\s*\)') { break }
        $m = [regex]::Match($line, '"([^"]+\.(?:py|sh))"')
        if ($m.Success) { $scripts += $m.Groups[1].Value }
    }
}
if ($scripts.Count -eq 0) {
    Note "sync-vault-scripts: parsed 0 scripts from manifest - skipping (non-fatal)."
    exit 0
}

if (-not $DryRun) { New-Item -ItemType Directory -Force -Path $DestDir | Out-Null }

$Created = @(); $Updated = @(); $BackedUp = @(); $Absent = @(); $Errors = @()

foreach ($name in $scripts) {
    $src = Join-Path (Join-Path $StarterDir "scripts") $name
    $dest = Join-Path $DestDir $name
    if (-not (Test-Path $src)) { $Absent += $name; continue }
    if (Test-Path $dest) {
        $same = $false
        try { $same = ((Get-FileHash $src).Hash -eq (Get-FileHash $dest).Hash) } catch { $same = $false }
        if ($same) { continue }
        if ($DryRun) { $Updated += "$name (would update)"; continue }
        try {
            Copy-Item -LiteralPath $dest -Destination "$dest.bak-$Stamp" -Force
            $BackedUp += "$dest.bak-$Stamp"
            Copy-Item -LiteralPath $src -Destination $dest -Force
            $Updated += $name
        } catch { $Errors += "could not update $dest" }
    } else {
        if ($DryRun) { $Created += "$name (would create)"; continue }
        try { Copy-Item -LiteralPath $src -Destination $dest -Force; $Created += $name }
        catch { $Errors += "could not create $dest" }
    }
}

Note "=== sync-vault-scripts.ps1 @ $Stamp ==="
Note "vault: $Vault"
Note "meta:  $Meta"
Note ("Created:   {0}" -f $Created.Count);   foreach ($f in $Created)  { Note "  + $f" }
Note ("Updated:   {0}" -f $Updated.Count);   foreach ($f in $Updated)  { Note "  ~ $f" }
Note ("Backed up: {0}" -f $BackedUp.Count);  foreach ($f in $BackedUp) { Note "  b $f" }
Note ("Absent:    {0}" -f $Absent.Count)
Note ("Errors:    {0}" -f $Errors.Count);    foreach ($f in $Errors)   { Note "  ! $f" }

if ($Errors.Count -gt 0) { exit 2 }
exit 0
