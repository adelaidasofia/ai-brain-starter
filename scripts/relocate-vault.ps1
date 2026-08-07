#Requires -Version 5.1
<#
  relocate-vault.ps1 - move a vault OUT of a consumer cloud-sync folder onto a
  local disk, leave a directory link so existing references keep resolving, AND
  migrate Claude Code's path-keyed state (session transcripts + the agent-memory
  link) so prior session history survives the move.

  Windows parity of relocate-vault.sh (MYC-2383). Same contract, same Claude
  path-key transform (shelled out to python for byte-identical semantics), same
  relocations.json manifest the watchdog reads. The one platform difference is
  the LINK: on Windows the old path is left as a JUNCTION (privilege-free, app-
  transparent, and NOT followed/synced by OneDrive - which is the whole point),
  falling back to a symbolic link only if a junction cannot be created. On
  macOS/Linux PowerShell (used by the test harness) it leaves a symbolic link,
  matching the .sh.

  WHY
  ---
  A git-backed Obsidian/AI-brain vault inside OneDrive / iCloud / Dropbox /
  Google Drive / Box melts the OS sync daemon - the high-churn .git plus per-
  session worktree checkouts generate millions of file events. The supported fix
  (Shape A in docs/CLOUD_SYNC.md) is to move the vault onto a local disk and
  leave a link. A RAW move does the move but SILENTLY ORPHANS Claude Code
  history: Claude stores per-project state under <config>/projects/<key>, where
  <key> is the absolute cwd with every non-alphanumeric character replaced by
  '-'. Moving the vault changes the key, so prior transcripts read "Session
  history unavailable" and the agent-memory link dangles. This helper does the
  move AND re-homes that state (copy, never move - old keys stay as a backup).

  Usage:
    relocate-vault.ps1 <old-vault-path> <new-vault-path> [options]
    relocate-vault.ps1 -MigrateClaudeState <old-abs-path> <new-abs-path>
      (state-only: the vault is already at the new path; just fix Claude history)
    relocate-vault.ps1 -Sweep <old-path> [<new-path>]
      (report residual references to the old path - classified - with a go/no-go)
    relocate-vault.ps1 -DropSymlink <old-path> [<new-path>]
      (retire the old-path link, but ONLY when the sweep finds zero executed refs)

  Options:
    -DryRun              print intended actions, change nothing
    -NoSymlink           do NOT leave a link at the old path (default leaves one)
    -Force               skip the soft gates (Obsidian-running, active-session).
                         target-exists and source-already-a-link stay FATAL.
    -ConfigDir <dir>     Claude Code config dir (default: $env:CLAUDE_CONFIG_DIR
                         or ~/.claude)
    -Help                this help

  -Sweep / -DropSymlink shell out to scripts/relocate-sweep.py (same dir).

  Exit codes: 0 ok / no-op / GO . 1 refused (gate) / NO-GO . 2 usage . 4 partial failure
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0)][string]$Old,
  [Parameter(Position = 1)][string]$New,
  [switch]$DryRun,
  [switch]$NoSymlink,
  [switch]$Force,
  [string]$ConfigDir,
  [switch]$MigrateClaudeState,
  [switch]$Sweep,
  [switch]$DropSymlink,
  [Parameter(ValueFromRemainingArguments = $true)][string[]]$SweepExtra,
  [switch]$Help
)

$ErrorActionPreference = "Stop"
$ScriptDir = $PSScriptRoot

# On Windows PowerShell 5.1, $IsWindows does not exist (-> $null), and that shell
# only ever runs on Windows; on pwsh 6+ $IsWindows is the real automatic var.
$script:OnWindows = if ($null -eq $IsWindows) { $true } else { [bool]$IsWindows }

if (-not $ConfigDir) {
  if ($env:CLAUDE_CONFIG_DIR) {
    $ConfigDir = $env:CLAUDE_CONFIG_DIR
  } else {
    $base = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
    $ConfigDir = Join-Path $base ".claude"
  }
}

function Say  { param([string]$m) Write-Host $m }
function Warn { param([string]$m) Write-Host "WARN  $m" -ForegroundColor Yellow }
function Die  { param([string]$m, [int]$code = 1) Write-Host "relocate-vault: REFUSE - $m" -ForegroundColor Red; exit $code }

# Obsidian-running probe with a test seam (parity with the .sh sibling's
# obsidian_running). Default is the real Get-Process probe, so production
# behavior is unchanged. $env:RELOCATE_VAULT_OBSIDIAN: 'running' -> report
# running, 'absent' -> report not running, unset/anything else -> real probe.
function Test-ObsidianRunning {
  switch ("$($env:RELOCATE_VAULT_OBSIDIAN)") {
    "running" { return $true }
    "absent"  { return $false }
    default   { return [bool](Get-Process -Name Obsidian -ErrorAction SilentlyContinue) }
  }
}

# ---- python (path-key transform + manifest, for byte-identical semantics) -----
$script:PyExe = $null
function Get-PyExe {
  if ($script:PyExe) { return $script:PyExe }
  foreach ($n in @("py", "python", "python3")) {
    $c = Get-Command $n -ErrorAction SilentlyContinue
    if ($c) {
      try {
        $v = (& $c.Source -c "import sys;print(sys.version_info[0])" 2>$null)
        if ("$v".Trim() -eq "3") { $script:PyExe = $c.Source; return $script:PyExe }
      } catch { }
    }
  }
  Die "python 3 is required (path-key transform + manifest) and was not found on PATH. Install it (e.g. 'winget install Python.Python.3.12') and re-run." 4
}
function Invoke-Py {
  param([string]$Code, [object[]]$PyArgs = @())
  $py = Get-PyExe
  return (& $py -c $Code @PyArgs)
}

function Get-AbsPath { param([string]$p)
  return (Invoke-Py 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' @($p))
}
function Get-PhysicalPath { param([string]$p)
  return (Invoke-Py 'import os,sys; print(os.path.realpath(os.path.expanduser(sys.argv[1])))' @($p))
}
# Claude Code's per-project dir name = the absolute cwd run through
# replace(/[^A-Za-z0-9]/g, "-"). Resolve the ANCESTRY but NEVER follow a leaf
# link: post-move the old leaf IS the link we leave behind. Matches the .sh.
function Get-ProjKey { param([string]$p)
  $code = @'
import os,re,sys
p = os.path.abspath(os.path.expanduser(sys.argv[1]))
resolved = os.path.join(os.path.realpath(os.path.dirname(p)), os.path.basename(p))
print(re.sub(r"[^a-zA-Z0-9]", "-", resolved))
'@
  return (Invoke-Py $code @($p))
}

function Test-ReparsePoint { param([string]$p)
  $it = Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
  if (-not $it) { return $false }
  return (($it.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
}

# Remove ONLY the reparse point (junction/symlink), never the target's contents.
# Remove-Item on a directory link can recurse into the target on Windows PS 5.1.
function Remove-DirLink { param([string]$p)
  $it = Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
  if ($it) { $it.Delete() }
}

# Cross-platform directory link. Windows: JUNCTION (no privilege, OneDrive does
# not sync through it), symlink fallback. Elsewhere: symbolic link.
function New-DirLink { param([string]$Link, [string]$Target)
  if ($script:OnWindows) {
    try {
      New-Item -ItemType Junction -Path $Link -Target $Target -ErrorAction Stop | Out-Null
      return
    } catch {
      try {
        New-Item -ItemType SymbolicLink -Path $Link -Target $Target -ErrorAction Stop | Out-Null
        return
      } catch {
        Die "could not create a directory link at '$Link' -> '$Target'. A junction needs no special rights; a symbolic link needs Developer Mode or an elevated shell. Enable Developer Mode (Settings > Privacy and security > For developers) and re-run." 4
      }
    }
  } else {
    New-Item -ItemType SymbolicLink -Path $Link -Target $Target -ErrorAction Stop | Out-Null
  }
}

# ---- migrate Claude Code path-keyed state (sessions + agent memory) -----------
function Move-ClaudeState { param([string]$OldAbs, [string]$NewAbs)
  $projdir = Join-Path $ConfigDir "projects"
  if (-not (Test-Path -LiteralPath $projdir)) {
    Say "  . no Claude Code projects dir ($projdir) - nothing to migrate"; return
  }
  $oldkey = "$(Get-ProjKey $OldAbs)".Trim()
  $newkey = "$(Get-ProjKey $NewAbs)".Trim()
  if ($oldkey -eq $newkey) {
    Say "  . old and new path keys are identical - nothing to migrate"; return
  }

  $matched = 0; $keys = 0; $files = 0
  Get-ChildItem -LiteralPath $projdir -Directory -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name.StartsWith($oldkey) } | ForEach-Object {
      $matched++
      $nb = $newkey + $_.Name.Substring($oldkey.Length)
      $nd = Join-Path $projdir $nb
      if ($DryRun) { Say "  . would migrate $($_.Name) -> $nb"; $keys++; return }
      New-Item -ItemType Directory -Force -Path $nd | Out-Null
      $before = (Get-ChildItem -LiteralPath $nd -Filter *.jsonl -File -ErrorAction SilentlyContinue | Measure-Object).Count
      Get-ChildItem -LiteralPath $_.FullName -Filter *.jsonl -File -ErrorAction SilentlyContinue | ForEach-Object {
        $dest = Join-Path $nd $_.Name
        if (-not (Test-Path -LiteralPath $dest)) { Copy-Item -LiteralPath $_.FullName -Destination $dest }
      }
      $after = (Get-ChildItem -LiteralPath $nd -Filter *.jsonl -File -ErrorAction SilentlyContinue | Measure-Object).Count
      $keys++; $files += ($after - $before)
    }

  if ($matched -eq 0) {
    Warn "  . no Claude Code session history found under old key '$oldkey*' in $projdir."
    Warn "    Nothing to migrate, OR the path-key encoding differs from what was expected -"
    Warn "    inspect $projdir if you expected prior sessions here."
    return
  }
  if ($DryRun) { Say "  . would migrate transcripts across $keys project key(s) -> new path key" }
  else { Say "  . migrated $files transcript(s) across $keys project key(s) -> new path key" }

  # Re-home the agent-memory link under the new base key. Prefer repointing an
  # existing old-key link; fall back to the ai-brain-starter memory convention.
  $oldmem = Join-Path (Join-Path $projdir $oldkey) "memory"
  $newmem = Join-Path (Join-Path $projdir $newkey) "memory"
  if (-not (Test-Path -LiteralPath $newmem)) {
    $target = $null
    if (Test-ReparsePoint $oldmem) {
      $t = @((Get-Item -LiteralPath $oldmem -Force).Target)[0]
      if ($t) {
        if ($t -eq $OldAbs) {
          $target = $NewAbs
        } elseif ($t.StartsWith($OldAbs + [System.IO.Path]::DirectorySeparatorChar) -or $t.StartsWith($OldAbs + "/")) {
          $target = $NewAbs + $t.Substring($OldAbs.Length)
        } else {
          $target = $t
        }
      }
    } else {
      # ai-brain-starter memory convention: "<gear> Meta/Agent Memory". Build the
      # gear + variation-selector from char codes so the source stays ASCII-clean
      # (no literal emoji byte to depend on the editor's encoding).
      $gear = [string][char]0x2699 + [string][char]0xFE0F
      $target = Join-Path $NewAbs (Join-Path "$gear Meta" "Agent Memory")
    }
    if ($target -and (Test-Path -LiteralPath $target -PathType Container)) {
      if ($DryRun) {
        Say "  . would re-link agent-memory -> $target"
      } else {
        New-Item -ItemType Directory -Force -Path (Join-Path $projdir $newkey) | Out-Null
        New-DirLink -Link $newmem -Target $target
        Say "  . agent-memory re-linked -> $target"
      }
    }
  }
}

# ---- record the move in the relocation manifest (the watchdog's source of truth) ---
function Write-RelocationRecord { param([string]$OldAbs, [string]$NewAbs, [bool]$Symlink)
  $manifest = Join-Path $ConfigDir "relocations.json"
  if ($DryRun) { Say "  . would record the move in $manifest (the watchdog reads this)"; return }
  New-Item -ItemType Directory -Force -Path $ConfigDir -ErrorAction SilentlyContinue | Out-Null
  $code = @'
import json, os, sys, tempfile, time
manifest, old, new, symlink = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
try:
    data = json.load(open(manifest)) if os.path.isfile(manifest) else []
    if not isinstance(data, list):
        data = []
except (OSError, ValueError):
    data = []
data = [e for e in data if not (isinstance(e, dict) and e.get("old") == old)]
data.append({"old": old, "new": new, "symlink": symlink,
             "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
d = os.path.dirname(manifest) or "."
fd, tmp = tempfile.mkstemp(dir=d, prefix=".relocations.")
with os.fdopen(fd, "w") as f:
    json.dump(data, f, indent=2)
os.replace(tmp, manifest)
'@
  $sym = if ($Symlink) { "1" } else { "0" }
  try {
    Invoke-Py $code @($manifest, $OldAbs, $NewAbs, $sym) | Out-Null
    Say "  . recorded the move in $manifest (scripts/relocate-sweep.py --watch reads this)"
  } catch {
    Warn "could not record the move in $manifest (the watchdog will not see it until recorded)"
  }
}

function Invoke-Sweep { param([string]$OldP, [string]$NewP, [string[]]$Extra)
  $sweepPy = Join-Path $ScriptDir "relocate-sweep.py"
  if (-not (Test-Path -LiteralPath $sweepPy)) { Die "relocate-sweep.py not found next to this script ($sweepPy)" }
  $py = Get-PyExe
  $a = @($sweepPy, "--old", $OldP)
  if ($NewP) { $a += @("--new", $NewP) }
  if ($Extra) { $a += $Extra }
  & $py @a
  return $LASTEXITCODE
}

# =============================================================================
if ($Help) {
  # Print the file's leading block-comment (the usage doc) between <# and #>.
  $inBlock = $false
  foreach ($line in (Get-Content -LiteralPath $PSCommandPath)) {
    if ($line -match '^\s*<#') { $inBlock = $true; continue }
    if ($line -match '^\s*#>') { break }
    if ($inBlock) { Say $line }
  }
  exit 0
}

# state-only mode -------------------------------------------------------------
if ($MigrateClaudeState) {
  if (-not $Old -or -not $New) {
    [Console]::Error.WriteLine("usage: relocate-vault.ps1 -MigrateClaudeState <old-abs-path> <new-abs-path>"); exit 2
  }
  $OldA = "$(Get-AbsPath $Old)".Trim()
  $NewA = "$(Get-AbsPath $New)".Trim()
  Say "relocate-vault: migrating Claude Code state only ($OldA -> $NewA)"
  Move-ClaudeState -OldAbs $OldA -NewAbs $NewA
  $sym = Test-ReparsePoint $OldA
  Write-RelocationRecord -OldAbs $OldA -NewAbs $NewA -Symlink $sym
  exit 0
}

# sweep mode ------------------------------------------------------------------
if ($Sweep) {
  if (-not $Old) { [Console]::Error.WriteLine("usage: relocate-vault.ps1 -Sweep <old-path> [<new-path>]"); exit 2 }
  $rc = Invoke-Sweep -OldP $Old -NewP $New -Extra $SweepExtra
  exit $rc
}

# drop-symlink mode -----------------------------------------------------------
if ($DropSymlink) {
  if (-not $Old) { [Console]::Error.WriteLine("usage: relocate-vault.ps1 -DropSymlink <old-path> [<new-path>]"); exit 2 }
  if (-not (Test-ReparsePoint $Old)) { Die "old path '$Old' is not a link - nothing to drop (relocate first, or it is already gone)" }
  if (-not $New) { $New = @((Get-Item -LiteralPath $Old -Force).Target)[0] }
  Say "relocate-vault: sweeping for residual references before retiring the link at '$Old' ..."
  $rc = Invoke-Sweep -OldP $Old -NewP $New -Extra $SweepExtra
  if ($rc -ne 0) {
    Die "NO-GO - executed references still resolve the old path (see report above). Repoint them, then re-run -DropSymlink." 1
  }
  if ($DryRun) { Say "DRY  would: remove '$Old' (link) - sweep returned GO (zero executed references)."; exit 0 }
  Remove-DirLink $Old
  Write-RelocationRecord -OldAbs $Old -NewAbs $New -Symlink $false
  Say "relocate-vault: retired the link '$Old' - sweep returned GO (zero executed references)."
  exit 0
}

# full relocate ---------------------------------------------------------------
if (-not $Old -or -not $New) {
  [Console]::Error.WriteLine("usage: relocate-vault.ps1 <old-vault-path> <new-vault-path> [-DryRun] [-Force] [-NoSymlink]"); exit 2
}
if (-not (Test-Path -LiteralPath $Old)) { Die "source '$Old' not found (already moved?)" }
if (Test-ReparsePoint $Old) { Die "source '$Old' is already a link (already relocated?)" }
if (-not (Test-Path -LiteralPath $Old -PathType Container)) { Die "source '$Old' is not a directory" }

$OldAbs = "$(Get-PhysicalPath $Old)".Trim()
$NewAbs = "$(Get-AbsPath $New)".Trim()
if ($OldAbs -eq $NewAbs) { Die "source and target are the same path" }
if (Test-Path -LiteralPath $NewAbs) { Die "target '$NewAbs' already exists (will not overwrite)" }

# Soft gates - skippable with -Force.
if (-not $Force) {
  if (Test-ObsidianRunning) {
    Die "Obsidian is running - quit it first (moving an open vault is unsafe), or pass -Force"
  }
  $wt = Join-Path $OldAbs ".claude/worktrees"
  if (Test-Path -LiteralPath $wt -PathType Container) {
    $cut = (Get-Date).AddMinutes(-10)
    $active = Get-ChildItem -LiteralPath $wt -Recurse -File -Force -ErrorAction SilentlyContinue |
      Where-Object { $_.LastWriteTime -gt $cut } | Select-Object -First 1
    if ($active) { Die "an active Claude session is writing under .claude/worktrees (touched <10 min) - close it, or pass -Force" }
  }
}

Say ""
Say "relocate-vault: BACK UP FIRST. A vault is often your one irreplaceable asset."
Say "  Stand up + VERIFY an off-machine backup before moving:"
Say "    pwsh -File scripts/vault-backup.ps1 setup ; pwsh -File scripts/vault-backup.ps1 verify"
Say ""

if ($DryRun) {
  Say "DRY  would: move '$OldAbs' -> '$NewAbs'"
  if (-not $NoSymlink) { Say "DRY  would: link '$OldAbs' -> '$NewAbs' (junction on Windows)" }
  Move-ClaudeState -OldAbs $OldAbs -NewAbs $NewAbs
  Write-RelocationRecord -OldAbs $OldAbs -NewAbs $NewAbs -Symlink (-not $NoSymlink)
  Say "DRY-RUN complete - no changes made."
  exit 0
}

Say "relocate-vault: moving '$OldAbs' -> '$NewAbs'"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $NewAbs) | Out-Null
Move-Item -LiteralPath $OldAbs -Destination $NewAbs
if (-not (Test-Path -LiteralPath $NewAbs -PathType Container)) { Die "POST: '$NewAbs' missing after move" 4 }

if (-not $NoSymlink) {
  New-DirLink -Link $OldAbs -Target $NewAbs
  if ((Test-ReparsePoint $OldAbs)) {
    Say "relocate-vault: left a link '$OldAbs' -> '$NewAbs' (old references keep resolving; the sync daemon sees a tiny reparse point, not the churn)"
  } else {
    Warn "POST: expected link not in place at $OldAbs"
  }
}

Say "relocate-vault: migrating Claude Code session history + agent memory ..."
Move-ClaudeState -OldAbs $OldAbs -NewAbs $NewAbs

Write-RelocationRecord -OldAbs $OldAbs -NewAbs $NewAbs -Symlink (-not $NoSymlink)

Say ""
Say "relocate-vault: DONE."
Say "  - Vault now lives at:  $NewAbs   (outside the sync folder)"
if (-not $NoSymlink) { Say "  - Old path is a link:  $OldAbs -> $NewAbs   (scripts/hooks/CLAUDE.md keep working)" }
Say "  - Reopen Obsidian; if it lost the vault, open $NewAbs"
Say "  - Reopen Claude Code in the vault - prior sessions appear in the picker"
Say "    (history was re-homed to the new path key; the old keys are kept as a backup)"
Say "  - Re-run a backup against the new path:  `$env:VAULT_PATH=`"$NewAbs`"; pwsh -File scripts/vault-backup.ps1 run"
exit 0
