#Requires -Version 5.1
<#
  relocate-machinery-sidecar.ps1 - make a vault SAFE to keep inside OneDrive / a
  cloud-sync folder by moving every churning machinery dir OUT of the synced
  tree into a local sidecar, leaving only tiny static pointers/links behind.

  Windows parity of relocate-machinery-sidecar.sh (MYC-2383). Same contract,
  same manifest schema (machinery-sidecar/1), same --separate-git-dir + worktree
  repair sequencing. Two platform differences: (1) the link is a JUNCTION on
  Windows (privilege-free, not synced through by OneDrive), a symbolic link on
  macOS/Linux PowerShell; (2) the .sh '--nosync' mode is intentionally NOT ported
  - the '.nosync' suffix is an iCloud-only convention OneDrive ignores, so
  exposing it on Windows would be a silent no-op. Windows relocates to the
  sidecar (and is reversible with -Rollback).

  WHY
  ---
  A git-backed Obsidian vault inside OneDrive / iCloud melts the OS sync daemon -
  NOT the notes (markdown is tiny + low-churn) but the MACHINERY: a .git
  rewritten wholesale on 'git gc', per-session worktree checkouts (thousands of
  files each), and search/index caches. The fix is not "turn the cloud off". The
  fix is: the vault MAY be synced; the machinery NEVER is. This relocates the
  machinery and leaves the docs.

  WHAT IT MOVES
    .git              -> <sidecar>/git      via 'git init --separate-git-dir'
                         (leaves a one-line .git POINTER FILE - static, safe)
    .claude/worktrees -> <sidecar>/worktrees   (relocate + link)
    caches            -> <sidecar>/cache/<name> (relocate + link)

  SEQUENCING GOTCHA: 'git init --separate-git-dir' ORPHANS existing linked
  worktrees. So this REFUSES to run while any linked worktree or live Claude
  session exists, unless -Force. After relocation it runs 'git worktree repair'.

  SAFETY: idempotent (re-run = no-op report), reversible (-Rollback reads the
  manifest and puts everything back), -DryRun changes nothing, and it never
  deletes content - only moves it and leaves a link/pointer.

  Usage:
    relocate-machinery-sidecar.ps1 <vault-path> [options]
    relocate-machinery-sidecar.ps1 <vault-path> -Rollback

  Options:
    -Sidecar <dir>   sidecar root (default: $env:BRAIN_SIDECAR or ~/.brain-sidecar)
    -DryRun          print intended actions, change nothing
    -Rollback        reverse a previous relocation using the manifest
    -Force           proceed even if linked worktrees / live sessions exist
                     (DANGEROUS: separate-git-dir orphans live worktrees)
    -Quiet           only print warnings/errors + the final summary line
    -Help            this help

  Exit codes: 0 ok / no-op . 1 refused (live worktree/session) . 2 usage .
              3 not a git vault and could not init . 4 partial failure
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0)][string]$Vault,
  [string]$Sidecar,
  [switch]$DryRun,
  [switch]$Rollback,
  [switch]$Force,
  [switch]$Quiet,
  [switch]$Help
)

$ErrorActionPreference = "Stop"
$ScriptDir = $PSScriptRoot
$script:OnWindows = if ($null -eq $IsWindows) { $true } else { [bool]$IsWindows }

# ---- machinery the script relocates (relative to the vault root) -------------
$CacheDirs = @(
  ".smart-env",
  ".codegraph",
  "$([char]0x2699)$([char]0xFE0F) Meta/graphify-out",
  "graphify-out",
  "$([char]0x2699)$([char]0xFE0F) Meta/Sessions",
  "$([char]0x2699)$([char]0xFE0F) Meta/Worktree Snapshots",
  "$([char]0x2699)$([char]0xFE0F) Meta/logs",
  ".obsidian/.smart-env"
)
$WorktreesRel = ".claude/worktrees"

function Say  { param([string]$m) if (-not $Quiet) { Write-Host $m } }
function Warn { param([string]$m) Write-Host "WARN  $m" -ForegroundColor Yellow }
function Err  { param([string]$m) [Console]::Error.WriteLine("ERROR $m") }
function Invoke-OrDry { param([string]$What, [scriptblock]$Action)
  if ($DryRun) { Write-Host "DRY   $What" } else { & $Action }
}

# ---- python (manifest + live-session probe) ---------------------------------
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
  Err "python 3 required for the manifest"; exit 4
}
function Invoke-Py { param([string]$Code, [object[]]$PyArgs = @())
  $py = Get-PyExe
  return (& $py -c $Code @PyArgs)
}
function Get-AbsPath { param([string]$p)
  return (Invoke-Py 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' @($p))
}
function Get-PhysicalPath { param([string]$p)
  return (Invoke-Py 'import os,sys; print(os.path.realpath(os.path.expanduser(sys.argv[1])))' @($p))
}

function Get-HomeBase { if ($env:USERPROFILE) { return $env:USERPROFILE } else { return $HOME } }

function Test-ReparsePoint { param([string]$p)
  $it = Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
  if (-not $it) { return $false }
  return (($it.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
}

# Remove ONLY the reparse point (junction/symlink), never the target's contents.
# Remove-Item on a directory link can recurse into the target on Windows PS 5.1;
# DirectoryInfo.Delete() severs the link and leaves the target intact.
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
        Err "could not create a directory link at '$Link' -> '$Target'. A junction needs no special rights; a symbolic link needs Developer Mode or an elevated shell."
        throw
      }
    }
  } else {
    New-Item -ItemType SymbolicLink -Path $Link -Target $Target -ErrorAction Stop | Out-Null
  }
}

function Get-VaultPath { param([string]$rel)
  $relNative = $rel -replace '/', [System.IO.Path]::DirectorySeparatorChar
  return (Join-Path $VaultAbs $relNative)
}

function Get-Slug { param([string]$p)
  $base  = Split-Path -Leaf $p
  $clean = ($base -replace '[^A-Za-z0-9._-]', '-') -replace '-{2,}', '-'
  $clean = $clean.Trim('-')
  if (-not $clean) { $clean = "vault" }
  $sha   = [System.Security.Cryptography.SHA1]::Create()
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($p)
  $hex   = ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join ""
  return "$clean-$($hex.Substring(0, 8))"
}

# ---- manifest staging (TSV -> python JSON, dodges the PS single-element-array
#      JSON quirk and matches the .sh writer byte-for-byte) --------------------
$script:RecFile = $null
function Add-Record { param([string]$Type, [string]$Rel, [string]$Target, [string]$Mode)
  if (-not $script:RecFile) { $script:RecFile = [System.IO.Path]::GetTempFileName() }
  ("{0}`t{1}`t{2}`t{3}" -f $Type, $Rel, $Target, $Mode) |
    Out-File -LiteralPath $script:RecFile -Encoding UTF8 -Append
}
function Write-Manifest {
  if ($DryRun) { return }
  # An idempotent re-run (everything already relocated) records ZERO new moves.
  # Do NOT clobber a prior valid manifest with an empty one - that silently
  # breaks -Rollback. Only write when there is something to record, or when no
  # manifest exists yet.
  $hasRecords = $script:RecFile -and (Test-Path -LiteralPath $script:RecFile) -and ((Get-Item -LiteralPath $script:RecFile).Length -gt 0)
  if (-not $hasRecords -and (Test-Path -LiteralPath $Manifest)) {
    Say "  . no new moves; keeping existing manifest $Manifest"
    return
  }
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Manifest) | Out-Null
  $rec = if ($script:RecFile) { $script:RecFile } else { "" }
  $code = @'
import json, os, sys
rec, man, vault, sidecar, slug, nosync = sys.argv[1:7]
recs = []
if rec and os.path.isfile(rec):
    with open(rec, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            while len(parts) < 4:
                parts.append("")
            recs.append({"type": parts[0], "vault_rel": parts[1],
                         "target": parts[2], "mode": parts[3]})
doc = {"schema": "machinery-sidecar/1", "vault": vault, "sidecar": sidecar,
       "slug": slug, "nosync": nosync == "1", "moves": recs}
os.makedirs(os.path.dirname(man), exist_ok=True)
with open(man, "w", encoding="utf-8") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
    f.write("\n")
'@
  Invoke-Py $code @($rec, $Manifest, $VaultAbs, $Sidecar, $Slug, "0") | Out-Null
}

# ---- live-worktree / live-session guard -------------------------------------
function Get-LinkedWorktrees {
  $out = & git -C $VaultAbs worktree list --porcelain 2>$null
  if ($LASTEXITCODE -ne 0) { return @() }
  $n = 0; $res = @()
  foreach ($line in @($out)) {
    if ($line -like 'worktree *') { $n++; if ($n -gt 1) { $res += $line.Substring(9) } }
  }
  return $res
}
function Get-LiveSessions {
  $lock = Join-Path $VaultAbs ".claude/.session-lock.json"
  if (-not (Test-Path -LiteralPath $lock)) { return 0 }
  $code = @'
import json, os, sys, time
lock = sys.argv[1]
try:
    data = json.load(open(lock, encoding="utf-8"))
except Exception:
    raise SystemExit(0)
sessions = data.get("sessions", {}) if isinstance(data, dict) else {}
cut = time.time() - 35 * 60
self_pid = int(sys.argv[2] or 0)
n = 0
for s in sessions.values():
    if not isinstance(s, dict):
        continue
    pid = s.get("pid")
    la = s.get("last_activity_at")
    alive = False
    if isinstance(pid, int) and pid > 0 and pid != self_pid:
        try:
            os.kill(pid, 0); alive = True
        except ProcessLookupError:
            alive = False
        except Exception:
            alive = True
    recent = isinstance(la, (int, float)) and la >= cut
    if alive or recent:
        n += 1
print(n)
'@
  $out = Invoke-Py $code @($lock, "$PID")
  $val = 0; [int]::TryParse("$out".Trim(), [ref]$val) | Out-Null
  return $val
}
function Test-NoLive {
  $wts  = @(Get-LinkedWorktrees)
  $sess = Get-LiveSessions
  if ($wts.Count -gt 0 -or $sess -gt 0) {
    if ($Force) {
      Warn "linked worktrees and/or $sess live session(s) present - proceeding under -Force (separate-git-dir will orphan live worktrees)"
      return $true
    }
    Err "refusing: relocation must run in a no-live-worktree window."
    if ($wts.Count -gt 0) { Err "  linked worktrees still registered:"; $wts | ForEach-Object { [Console]::Error.WriteLine("    $_") } }
    if ($sess -gt 0) { Err "  $sess live Claude session(s) in $VaultAbs/.claude/.session-lock.json" }
    Err "  close all sessions + 'git worktree remove' scratch trees, then re-run. Override: -Force"
    return $false
  }
  return $true
}

# =============================================================================
# RELOCATE one cache/worktree dir (relocate + link)
# =============================================================================
function Move-CacheDir { param([string]$Rel, [string]$Dst, [string]$RType)
  $src = Get-VaultPath $Rel
  if (Test-ReparsePoint $src) { Say "  . $Rel already a link - skip"; return }
  if (-not (Test-Path -LiteralPath $src)) { return }   # absent -> nothing to do
  if (Test-Path -LiteralPath $Dst) {
    $bak = "$Dst.bak-$((Get-Date).ToString('yyyyMMddHHmmss'))"
    Invoke-OrDry "move existing $Dst -> $bak" { Move-Item -LiteralPath $Dst -Destination $bak }
    Warn "  . prior sidecar copy at $Dst backed up"
  }
  Invoke-OrDry "move $src -> $Dst + link" {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Dst) | Out-Null
    Move-Item -LiteralPath $src -Destination $Dst
    New-DirLink -Link $src -Target $Dst
  }
  Add-Record $RType $Rel $Dst "relocate"
  Say "  . $Rel -> $Dst + link"
}

# =============================================================================
# RELOCATE .git via --separate-git-dir
# =============================================================================
function Move-GitDir {
  $gitpath = Join-Path $VaultAbs ".git"
  if (Test-Path -LiteralPath $gitpath -PathType Leaf) {
    $cur = ""
    $first = Get-Content -LiteralPath $gitpath -TotalCount 1 -ErrorAction SilentlyContinue
    if ("$first" -match '^gitdir:\s*(.+)$') { $cur = $Matches[1] }
    Say "  . .git already a pointer (-> $cur) - skip"
    return $true
  }
  if (Test-Path -LiteralPath $gitpath -PathType Container) {
    Invoke-OrDry "git init --separate-git-dir $SideGit" {
      New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SideGit) | Out-Null
      & git -C $VaultAbs init --separate-git-dir $SideGit | Out-Null
    }
    if (-not $DryRun -and -not (Test-Path -LiteralPath $gitpath -PathType Leaf)) {
      Err "  separate-git-dir did not produce a .git pointer file"; return $false
    }
    Invoke-OrDry "git worktree repair" { & git -C $VaultAbs worktree repair *> $null }
    Add-Record "git" ".git" $SideGit "separated"
    Say "  . .git -> $SideGit (pointer file left in vault)"
    return $true
  }
  # no .git at all -> fresh repo with a separated gitdir
  Invoke-OrDry "git init --separate-git-dir $SideGit (fresh)" {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SideGit) | Out-Null
    & git -C $VaultAbs init --separate-git-dir $SideGit | Out-Null
  }
  Add-Record "git" ".git" $SideGit "fresh-init"
  Say "  . initialized fresh repo with gitdir at $SideGit"
  return $true
}

# =============================================================================
# ROLLBACK
# =============================================================================
function Invoke-Rollback {
  if (-not (Test-Path -LiteralPath $Manifest)) { Err "no manifest at $Manifest - nothing to roll back"; exit 2 }
  Say "Rolling back machinery sidecar for: $VaultAbs"
  Say "  manifest: $Manifest"
  $code = @'
import json, os, sys
man = sys.argv[1]
doc = json.load(open(man, encoding="utf-8"))
for m in reversed(doc.get("moves", [])):
    print("\t".join([m.get("type",""), m.get("vault_rel",""),
                     m.get("target",""), m.get("mode","")]))
'@
  $rows = Invoke-Py $code @($Manifest)
  foreach ($row in @($rows)) {
    if (-not "$row") { continue }
    $parts = "$row" -split "`t"
    while ($parts.Count -lt 4) { $parts += "" }
    $typ = $parts[0]; $rel = $parts[1]; $target = $parts[2]
    if (-not $typ) { continue }
    switch ($typ) {
      "git" {
        $ptr = Join-Path $VaultAbs ".git"
        if ((Test-Path -LiteralPath $ptr -PathType Leaf) -and (Test-Path -LiteralPath $target -PathType Container)) {
          Invoke-OrDry "restore .git dir from $target" {
            Remove-Item -LiteralPath $ptr -Force
            Move-Item -LiteralPath $target -Destination $ptr
            & git -C $VaultAbs config --unset core.worktree 2>$null | Out-Null
            & git -C $VaultAbs worktree repair *> $null
          }
          Say "  restored .git (dir) from $target"
        } else {
          Warn "  git rollback skipped (pointer or gitdir missing)"
        }
      }
      default {
        if ($typ -eq "cache" -or $typ -eq "worktrees") {
          $link = Get-VaultPath $rel
          if (Test-ReparsePoint $link) { Invoke-OrDry "remove link $link" { Remove-DirLink $link } }
          if (Test-Path -LiteralPath $target) {
            Invoke-OrDry "restore $rel from $target" {
              New-Item -ItemType Directory -Force -Path (Split-Path -Parent $link) | Out-Null
              Move-Item -LiteralPath $target -Destination $link
            }
          } else {
            Warn "  missing sidecar source: $target"
          }
          Say "  restored $rel"
        }
      }
    }
  }
  if (-not $DryRun) { Remove-Item -LiteralPath $Manifest -Force -ErrorAction SilentlyContinue }
  Say "Rollback complete."
}

# =============================================================================
# MAIN
# =============================================================================
if ($Help) {
  $inBlock = $false
  foreach ($line in (Get-Content -LiteralPath $PSCommandPath)) {
    if ($line -match '^\s*<#') { $inBlock = $true; continue }
    if ($line -match '^\s*#>') { break }
    if ($inBlock) { Write-Host $line }
  }
  exit 0
}

if (-not $Vault) { [Console]::Error.WriteLine("usage: relocate-machinery-sidecar.ps1 <vault-path> [options]"); exit 2 }
if (-not (Test-Path -LiteralPath $Vault -PathType Container)) { [Console]::Error.WriteLine("not a directory: $Vault"); exit 2 }

$VaultAbs = "$(Get-PhysicalPath $Vault)".Trim()

if (-not $Sidecar) {
  $Sidecar = if ($env:BRAIN_SIDECAR) { $env:BRAIN_SIDECAR }
             elseif ($env:MYCELIUM_SIDECAR) { $env:MYCELIUM_SIDECAR }
             else { Join-Path (Get-HomeBase) ".brain-sidecar" }
}
$Sidecar = "$(Get-AbsPath $Sidecar)".Trim()

$Slug     = Get-Slug $VaultAbs
$SideGit  = Join-Path (Join-Path $Sidecar "git") $Slug
$SideCache = Join-Path (Join-Path $Sidecar "cache") $Slug
$SideWt   = Join-Path (Join-Path $Sidecar "worktrees") $Slug
$Manifest = Join-Path (Join-Path $Sidecar "manifests") "$Slug.json"

if ($Rollback) { Invoke-Rollback; exit 0 }

# informational cloud detect (the whole point is the cloud is ALLOWED)
$cloud = ""
$ccs = Join-Path $ScriptDir "check-cloud-sync.py"
if (Test-Path -LiteralPath $ccs) {
  try {
    $py = Get-PyExe
    $cloud = "$(& $py $ccs --porcelain $VaultAbs 2>$null)".Trim()
  } catch { $cloud = "" }
}

Say "Machinery-sidecar relocation"
Say "  vault   : $VaultAbs"
Say "  sidecar : $Sidecar  (slug: $Slug)"
if ($cloud -like 'CLOUD_SYNC_RISK*') {
  Say "  cloud   : $($cloud -replace '^CLOUD_SYNC_RISK:?\s*','') - relocating machinery so the docs can sync safely"
} elseif ($cloud -eq 'OK_LOCAL') {
  Say "  cloud   : local disk (machinery relocation still valid; reduces in-tree churn)"
}
if ($DryRun) { Say "  mode    : -DryRun (no changes)" }

if (-not (Test-NoLive)) { exit 1 }

Say "Relocating .git ..."
if (-not (Move-GitDir)) { exit 4 }

Say "Relocating worktrees ..."
Move-CacheDir -Rel $WorktreesRel -Dst $SideWt -RType "worktrees"

Say "Relocating caches ..."
foreach ($rel in $CacheDirs) {
  Move-CacheDir -Rel $rel -Dst (Join-Path $SideCache ($rel -replace '/', [System.IO.Path]::DirectorySeparatorChar)) -RType "cache"
}

Write-Manifest
if ($script:RecFile -and (Test-Path -LiteralPath $script:RecFile)) { Remove-Item -LiteralPath $script:RecFile -Force -ErrorAction SilentlyContinue }

if ($DryRun) {
  Say "DRY-RUN complete - no changes made. Manifest would be: $Manifest"
} else {
  Say "Done. Manifest: $Manifest"
  Say "Reverse anytime: relocate-machinery-sidecar.ps1 `"$VaultAbs`" -Rollback"
}
exit 0
