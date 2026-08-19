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

  THE ONE RULE: IT NEVER MOVES ANYTHING GIT TRACKS
  ------------------------------------------------
  Every candidate is tested against the REAL git index before it is touched. A
  path holding tracked content is your NOTES, whatever it is named - moving it
  turns the directory into a link, 'git status' reports every file as deleted,
  and the next auto-commit/push propagates that deletion to every machine. So:
  tracked content -> never moved, reported, left exactly as it is. Genuinely
  ignored/untracked caches still move. If the index cannot be read, the path is
  left alone (a path I cannot prove is a cache is not a cache).

  WHAT IT MOVES (only when the path holds NO tracked content)
    .git              -> <sidecar>/git      via 'git init --separate-git-dir'
                         (leaves a one-line .git POINTER FILE - static, safe)
    .claude/worktrees -> <sidecar>/worktrees   (relocate + link)
    caches            -> <sidecar>/cache/<name> (relocate + link)
                         NOTE several candidate names hold TRACKED notes in a
                         real vault. The index test decides, never the name.

  SEQUENCING GOTCHA: 'git init --separate-git-dir' ORPHANS existing linked
  worktrees. So this REFUSES to run while any linked worktree or live Claude
  session exists, unless -Force. After relocation it runs 'git worktree repair'.
  The probes FAIL CLOSED: if git or the session lock cannot be read, that counts
  as LIVE and the run is refused. "I could not look" and "I looked and it was
  empty" are different answers.

  SAFETY: idempotent (re-run = no-op report), interrupt-safe (every move is
  written to an append-only journal in the sidecar BEFORE it happens, so
  -Rollback works from any kill point), reversible (-Rollback reads that record
  and puts everything back, exits non-zero and KEEPS the record if any leg
  fails), -DryRun changes nothing, and it never deletes content - only moves it
  and leaves a link/pointer.

  The SIDECAR DESTINATION is checked with the same cloud-sync detector as the
  vault: on a roaming / OneDrive-managed / network profile $HOME is itself
  synced, so the default ~/.brain-sidecar is too - relocating machinery from one
  synced tree into another does not fix the melt, so it is refused.

  Usage:
    relocate-machinery-sidecar.ps1 <vault-path> [options]
    relocate-machinery-sidecar.ps1 <vault-path> -Rollback

  Options:
    -Sidecar <dir>   sidecar root (default: $env:BRAIN_SIDECAR or ~/.brain-sidecar)
    -DryRun          print intended actions, change nothing
    -Rollback        reverse a previous relocation using the recorded moves
    -Force           proceed even if linked worktrees / live sessions exist, a
                     probe could not run, or the sidecar looks cloud-synced
                     (DANGEROUS: separate-git-dir orphans live worktrees)
    -Quiet           only print warnings/errors + the final summary line
    -Help            this help

  Exit codes: 0 ok / no-op . 1 refused (live worktree/session, unsafe sidecar,
              or a probe that could not run) . 2 usage / nothing to roll back .
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

# git's messages are classified below (e.g. "not a git repository"), so pin the
# locale; and make python's stdout UTF-8 so emoji vault paths survive the round
# trip through a cp1252 console (PS 5.1).
$env:LC_ALL = "C"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

# ---- machinery the script relocates (relative to the vault root) -------------
# CANDIDATES, not a denylist: each moves ONLY if git tracks nothing there.
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
# Never emits: callers return $true/$false/ints, and a stray pipeline object
# from an action would be swallowed into that return value.
function Invoke-OrDry { param([string]$What, [scriptblock]$Action)
  if ($DryRun) { Write-Host "DRY   $What" } else { & $Action | Out-Null }
}

# Test-only fault injection for the interrupt matrix in
# test-relocate-machinery-sidecar.ps1. Unset in production. Set to a step label
# to hard-kill this process right after that step (no finally, no cleanup) - the
# only honest way to prove -Rollback works from every kill point.
function Invoke-MaybeDie { param([string]$Label)
  if ("$($env:BRAIN_SIDECAR_TEST_KILL_AT)" -eq $Label) { Stop-Process -Id $PID -Force }
}

# ---- python (records + live-session probe) ----------------------------------
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
  Err "python 3 required for the move record"; exit 4
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
# Exact containment (case-insensitive on Windows), not string prefixing: /var vs
# /private/var and C:\Users vs c:\users both make a naive prefix test miss.
function Test-PathInside { param([string]$Child, [string]$Parent)
  $code = @'
import os, sys
child = os.path.realpath(os.path.expanduser(sys.argv[1]))
parent = os.path.realpath(os.path.expanduser(sys.argv[2]))
if os.name == "nt":
    child, parent = child.lower(), parent.lower()
inside = child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)
print("1" if inside else "0")
'@
  return (("$(Invoke-Py $code @($Child, $Parent))").Trim() -eq "1")
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

# ---- write-ahead journal ----------------------------------------------------
# One JSON object per move, appended AND fsynced BEFORE the move happens, so a
# kill at any point still leaves -Rollback a full map. A move this fails to
# record is a move that does NOT happen.
function Add-JournalRecord { param([string]$Type, [string]$Rel, [string]$Target, [string]$Mode)
  if ($DryRun) { return $true }
  $code = @'
import json, os, sys
jnl, typ, rel, target, mode = sys.argv[1:6]
os.makedirs(os.path.dirname(jnl), exist_ok=True)
with open(jnl, "a", encoding="utf-8") as f:
    f.write(json.dumps({"type": typ, "vault_rel": rel, "target": target,
                        "mode": mode}, ensure_ascii=False) + "\n")
    f.flush()
    os.fsync(f.fileno())
'@
  try {
    Invoke-Py $code @($Journal, $Type, $Rel, $Target, $Mode) | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "python exited $LASTEXITCODE" }
    return $true
  } catch {
    Err "could not record the move in ${Journal}: $_"
    return $false
  }
}

# Recorded moves from the journal (.jsonl) or a manifest (.json), as TSV rows.
# Later records win per (type, vault_rel) so a relocate -> rollback -> relocate
# cycle never replays a stale row.
$script:RecordReaderPy = @'
import json, os, sys
src, rev = sys.argv[1], sys.argv[2]
recs = []
if src.endswith(".jsonl"):
    with open(src, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except ValueError:
                continue  # a torn final line from a kill is not a reason to stop
else:
    with open(src, encoding="utf-8") as f:
        recs = json.load(f).get("moves", [])
seen, order = {}, []
for r in recs:
    if not isinstance(r, dict):
        continue
    k = (r.get("type", ""), r.get("vault_rel", ""))
    if k not in seen:
        order.append(k)
    seen[k] = r
rows = [seen[k] for k in order]
if rev == "1":
    rows.reverse()
for r in rows:
    print("\t".join([r.get("type", ""), r.get("vault_rel", ""),
                     r.get("target", ""), r.get("mode", "")]))
'@
function Get-RecordRows { param([string]$Src, [switch]$Reverse)
  $rev = if ($Reverse) { "1" } else { "0" }
  return (Invoke-Py $script:RecordReaderPy @($Src, $rev))
}

function Write-Manifest {
  if ($DryRun) { return }
  $hasJournal = (Test-Path -LiteralPath $Journal) -and ((Get-Item -LiteralPath $Journal).Length -gt 0)
  if (-not $hasJournal) {
    # An idempotent re-run records ZERO new moves. Do NOT clobber a prior valid
    # manifest with an empty one - that silently breaks -Rollback.
    if (Test-Path -LiteralPath $Manifest) { Say "  . no recorded moves; keeping existing manifest $Manifest" }
    return
  }
  $code = @'
import json, os, sys
jnl, man, vault, sidecar, slug = sys.argv[1:6]
recs = []
with open(jnl, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except ValueError:
            continue
seen, order = {}, []
for r in recs:
    if not isinstance(r, dict):
        continue
    k = (r.get("type", ""), r.get("vault_rel", ""))
    if k not in seen:
        order.append(k)
    seen[k] = r
moves = [{"type": seen[k].get("type", ""), "vault_rel": seen[k].get("vault_rel", ""),
          "target": seen[k].get("target", ""), "mode": seen[k].get("mode", "")}
         for k in order]
doc = {"schema": "machinery-sidecar/1", "vault": vault, "sidecar": sidecar,
       "slug": slug, "nosync": False, "moves": moves}
os.makedirs(os.path.dirname(man), exist_ok=True)
with open(man, "w", encoding="utf-8") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
    f.write("\n")
'@
  Invoke-Py $code @($Journal, $Manifest, $VaultAbs, $Sidecar, $Slug) | Out-Null
}

# ---- git probes (fail CLOSED: "could not look" != "nothing there") -----------
$script:GitState = "unknown"
function Get-GitState {   # repo | norepo | error
  $out = & git -C $VaultAbs rev-parse --git-dir 2>&1
  if ($LASTEXITCODE -eq 0) { return "repo" }
  if ("$out" -match "not a git repository") { return "norepo" }
  Warn "git could not read ${VaultAbs}: $out"
  return "error"
}

# Number of TRACKED files at a vault-relative path. -1 = the index could not be
# read at all, and the caller must then leave the path alone.
function Get-TrackedCount { param([string]$Rel)
  if ($script:GitState -eq "norepo") { return 0 }
  if ($script:GitState -ne "repo")   { return -1 }
  $out = & git -C $VaultAbs ls-files -- $Rel 2>$null
  if ($LASTEXITCODE -ne 0) { return -1 }
  return @($out | Where-Object { "$_" -ne "" }).Count
}

function Get-LinkedWorktrees {   # @() = none; $null = the probe could not run
  if ($script:GitState -eq "norepo") { return @() }
  if ($script:GitState -ne "repo")   { return $null }
  $out = & git -C $VaultAbs worktree list --porcelain 2>&1
  if ($LASTEXITCODE -ne 0) {
    Warn "could not list git worktrees in ${VaultAbs}: $out"
    return $null
  }
  $n = 0; $res = @()
  foreach ($line in @($out)) {
    if ($line -like 'worktree *') { $n++; if ($n -gt 1) { $res += $line.Substring(9) } }
  }
  return ,$res
}

function Get-LiveSessions {   # integer count; -1 = the lock could not be read
  $lock = Join-Path $VaultAbs ".claude/.session-lock.json"
  if (-not (Test-Path -LiteralPath $lock)) { return 0 }
  $code = @'
import json, os, sys, time
lock = sys.argv[1]
try:
    with open(lock, encoding="utf-8") as f:
        data = json.load(f)
except Exception as e:  # an unreadable/corrupt lock is NOT "zero sessions"
    print("cannot read %s: %s" % (lock, e), file=sys.stderr)
    raise SystemExit(3)
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
  if ($LASTEXITCODE -ne 0) { Warn "could not read $lock (probe exited $LASTEXITCODE)"; return -1 }
  $val = 0
  if (-not [int]::TryParse("$out".Trim(), [ref]$val)) {
    Warn "unreadable session count from ${lock}: $out"
    return -1
  }
  return $val
}

function Test-NoLive {
  $wts  = Get-LinkedWorktrees
  $sess = Get-LiveSessions
  $probeFailed = ($null -eq $wts) -or ($sess -lt 0)
  $wtCount = if ($null -eq $wts) { 0 } else { @($wts).Count }
  $sessCount = if ($sess -lt 0) { 0 } else { $sess }

  if ($wtCount -gt 0 -or $sessCount -gt 0 -or $probeFailed) {
    if ($Force) {
      Warn "live worktrees/sessions present or unprobeable - proceeding under -Force (separate-git-dir will orphan live worktrees)"
      return $true
    }
    Err "refusing: relocation must run in a proven no-live-worktree window."
    if ($probeFailed) {
      Err "  a safety probe could not run (see the WARN above), so I cannot prove nothing is live."
      Err "  a probe that errors and a vault that is idle look identical - this refuses rather than guess."
      Err "  common cause: git 'dubious ownership' on a restored/migrated/external/network vault. Fix with:"
      Err "    git config --global --add safe.directory `"$VaultAbs`""
    }
    if ($wtCount -gt 0) { Err "  linked worktrees still registered:"; $wts | ForEach-Object { [Console]::Error.WriteLine("    $_") } }
    if ($sessCount -gt 0) { Err "  $sessCount live Claude session(s) in $VaultAbs/.claude/.session-lock.json" }
    Err "  close all sessions + 'git worktree remove' scratch trees, then re-run. Override: -Force"
    return $false
  }
  return $true
}

# ---- sidecar destination guard ----------------------------------------------
function Test-SidecarLocation {
  if (Test-PathInside $Sidecar $VaultAbs) {
    Err "refusing: the sidecar ($Sidecar) is INSIDE the vault ($VaultAbs)."
    Err "  the machinery would never leave the synced tree. Pass -Sidecar <a path outside the vault>."
    return $false
  }
  if (Test-PathInside $VaultAbs $Sidecar) {
    Err "refusing: the vault ($VaultAbs) is INSIDE the sidecar ($Sidecar)."
    Err "  pass -Sidecar <a path outside the vault>."
    return $false
  }
  $ccs = Join-Path $ScriptDir "check-cloud-sync.py"
  if (-not (Test-Path -LiteralPath $ccs)) {
    Warn "could not verify the sidecar destination: $ccs is missing (cloud-sync detector unavailable)"
    return $true
  }
  $out = ""
  try {
    $py = Get-PyExe
    $out = "$(& $py $ccs --porcelain $Sidecar 2>$null)".Trim()
  } catch { $out = "" }
  if ($out -like 'CLOUD_SYNC_RISK*') {
    $svc = $out -replace '^CLOUD_SYNC_RISK:?\s*', ''
    if ($Force) {
      Warn "sidecar destination is inside $svc - proceeding under -Force (the sync melt is NOT fixed by this run)"
      return $true
    }
    Err "refusing: the sidecar destination is inside $svc, a cloud-sync folder."
    Err "  sidecar: $Sidecar"
    Err "  moving the machinery from one synced folder into another does not stop the melt;"
    Err "  it only splits your notes from their repo. This usually means the profile folder"
    Err "  itself is synced (roaming / OneDrive-managed / network), so the default"
    Err "  ~/.brain-sidecar is too. Re-run with a local path, e.g. -Sidecar C:\brain-sidecar"
    Err "  Override (NOT recommended): -Force"
    return $false
  }
  if ($out -eq 'OK_LOCAL') { return $true }
  Warn "could not verify the sidecar destination is outside a cloud-sync folder (check-cloud-sync.py said: '$out') - proceeding"
  return $true
}

# =============================================================================
# RELOCATE one cache/worktree dir (relocate + link)
# =============================================================================
function Move-CacheDir { param([string]$Rel, [string]$Dst, [string]$RType)
  $src = Get-VaultPath $Rel
  if (Test-ReparsePoint $src) { Say "  . $Rel already a link - skip"; return $true }
  if (-not (Test-Path -LiteralPath $src)) { return $true }   # absent -> nothing to do

  # THE ONE RULE: never move anything git tracks, whatever the path is named.
  $n = Get-TrackedCount $Rel
  if ($n -lt 0) {
    Warn "  . $Rel - KEPT: could not read the git index, so I cannot prove this is a cache"
    return $true
  }
  if ($n -gt 0) {
    Say "  . $Rel - KEPT: git tracks $n file(s) here. Those are notes, not a cache;"
    Say "        relocating them would show every one as DELETED and sync that deletion."
    return $true
  }

  if (Test-Path -LiteralPath $Dst) {
    $bak = "$Dst.bak-$((Get-Date).ToString('yyyyMMddHHmmss'))"
    Invoke-OrDry "move existing $Dst -> $bak" { Move-Item -LiteralPath $Dst -Destination $bak }
    Warn "  . prior sidecar copy at $Dst backed up"
  }
  if (-not (Add-JournalRecord $RType $Rel $Dst "relocate")) {
    Err "  . $Rel NOT moved: an unrecordable move cannot be reversed"
    return $false
  }
  Invoke-MaybeDie "post-journal:$Rel"
  Invoke-OrDry "move $src -> $Dst" {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Dst) | Out-Null
    Move-Item -LiteralPath $src -Destination $Dst
  }
  Invoke-MaybeDie "post-mv:$Rel"
  Invoke-OrDry "link $src -> $Dst" { New-DirLink -Link $src -Target $Dst }
  Invoke-MaybeDie "post-link:$Rel"
  Say "  . $Rel -> $Dst + link"
  return $true
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
    if (-not (Add-JournalRecord "git" ".git" $SideGit "separated")) { return $false }
    Invoke-MaybeDie "post-journal:.git"
    Invoke-OrDry "git init --separate-git-dir $SideGit" {
      New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SideGit) | Out-Null
      & git -C $VaultAbs init --separate-git-dir $SideGit | Out-Null
    }
    Invoke-MaybeDie "post-git"
    if (-not $DryRun -and -not (Test-Path -LiteralPath $gitpath -PathType Leaf)) {
      Err "  separate-git-dir did not produce a .git pointer file"; return $false
    }
    Invoke-OrDry "git worktree repair" { & git -C $VaultAbs worktree repair *> $null }
    Say "  . .git -> $SideGit (pointer file left in vault)"
    return $true
  }
  # no .git at all -> fresh repo with a separated gitdir
  if (-not (Add-JournalRecord "git" ".git" $SideGit "fresh-init")) { return $false }
  Invoke-OrDry "git init --separate-git-dir $SideGit (fresh)" {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SideGit) | Out-Null
    & git -C $VaultAbs init --separate-git-dir $SideGit | Out-Null
  }
  Say "  . initialized fresh repo with gitdir at $SideGit"
  return $true
}

# =============================================================================
# ROLLBACK
# =============================================================================
# Each leg returns 0 restored / 3 nothing to do / 1 failed.
function Restore-GitDir { param([string]$Target)
  $ptr = Join-Path $VaultAbs ".git"
  if ((Test-Path -LiteralPath $ptr -PathType Container) -and -not (Test-ReparsePoint $ptr)) {
    if (Test-Path -LiteralPath $Target -PathType Container) {
      Err "  . .git NOT restored: BOTH $ptr (a real dir) and $Target exist."
      Err "    refusing to overwrite either - inspect them and keep the one you want."
      return 1
    }
    Say "  . .git already a real directory - nothing to restore"
    return 3
  }
  if (-not (Test-Path -LiteralPath $Target -PathType Container)) {
    Err "  . .git NOT restored: the sidecar gitdir is missing ($Target)"
    return 1
  }
  try {
    Invoke-OrDry "restore .git dir from $Target" {
      if (Test-Path -LiteralPath $ptr) { Remove-Item -LiteralPath $ptr -Force }
      Move-Item -LiteralPath $Target -Destination $ptr
      & git -C $VaultAbs config --unset core.worktree 2>$null | Out-Null
      & git -C $VaultAbs worktree repair *> $null
    }
  } catch {
    Err "  . .git NOT restored: $_"
    return 1
  }
  Say "  . restored .git (directory) from $Target"
  return 0
}

function Restore-CacheDir { param([string]$Rel, [string]$Target)
  $link = Get-VaultPath $Rel
  $isLink = Test-ReparsePoint $link
  if ((Test-Path -LiteralPath $link) -and -not $isLink) {
    if (Test-Path -LiteralPath $Target) {
      Err "  . $Rel NOT restored: BOTH the vault path and $Target exist - refusing to overwrite either"
      return 1
    }
    Say "  . $Rel already in place - nothing to restore"
    return 3
  }
  if (-not (Test-Path -LiteralPath $Target)) {
    if ($isLink) {
      Err "  . $Rel NOT restored: $Target is missing and the vault path is a dangling link"
      return 1
    }
    Say "  . ${Rel}: nothing to restore (the move never happened)"
    return 3
  }
  try {
    Invoke-OrDry "restore $Rel from $Target" {
      if (Test-ReparsePoint $link) { Remove-DirLink $link }
      New-Item -ItemType Directory -Force -Path (Split-Path -Parent $link) | Out-Null
      Move-Item -LiteralPath $Target -Destination $link
    }
  } catch {
    Err "  . $Rel NOT restored: $_"
    return 1
  }
  Say "  . restored $Rel"
  return 0
}

function Invoke-Rollback {
  $src = ""
  if ((Test-Path -LiteralPath $Journal) -and ((Get-Item -LiteralPath $Journal).Length -gt 0)) { $src = $Journal }
  elseif (Test-Path -LiteralPath $Manifest) { $src = $Manifest }
  else {
    Err "no relocation record at $Journal or $Manifest - nothing to roll back"
    return 2
  }
  Say "Rolling back machinery sidecar for: $VaultAbs"
  Say "  record: $src"
  $rows = Get-RecordRows -Src $src -Reverse
  $restored = 0; $already = 0; $failed = 0
  foreach ($row in @($rows)) {
    if (-not "$row") { continue }
    $parts = "$row" -split "`t"
    while ($parts.Count -lt 4) { $parts += "" }
    $typ = $parts[0]; $rel = $parts[1]; $target = $parts[2]
    if (-not $typ) { continue }
    $r = 1
    if ($typ -eq "git") { $r = Restore-GitDir $target }
    elseif ($typ -eq "cache" -or $typ -eq "worktrees") { $r = Restore-CacheDir $rel $target }
    else { Warn "  . unknown record type '$typ' for '$rel' - skipped"; $r = 1 }
    if ($r -eq 0) { $restored++ } elseif ($r -eq 3) { $already++ } else { $failed++ }
  }
  if ($failed -eq 0) {
    if (-not $DryRun) {
      Remove-Item -LiteralPath $Journal  -Force -ErrorAction SilentlyContinue
      Remove-Item -LiteralPath $Manifest -Force -ErrorAction SilentlyContinue
    }
    Say "Rollback complete: $restored restored, $already already in place, 0 failed."
    return 0
  }
  # The record is the only map back. It is NEVER deleted on a partial rollback.
  Err "Rollback INCOMPLETE: $restored restored, $already already in place, $failed FAILED."
  Err "  the relocation record was kept so you can fix the cause and re-run:"
  Err "    relocate-machinery-sidecar.ps1 `"$VaultAbs`" -Sidecar `"$Sidecar`" -Rollback"
  return 1
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
# PHYSICALLY resolve it (realpath, not abspath): the containment checks below
# have to compare real locations, and the sidecar usually does not exist yet.
$Sidecar = "$(Get-PhysicalPath $Sidecar)".Trim()

$Slug     = Get-Slug $VaultAbs
$SideGit  = Join-Path (Join-Path $Sidecar "git") $Slug
$SideCache = Join-Path (Join-Path $Sidecar "cache") $Slug
$SideWt   = Join-Path (Join-Path $Sidecar "worktrees") $Slug
$Manifest = Join-Path (Join-Path $Sidecar "manifests") "$Slug.json"
# Append-only write-ahead record, kept across runs (see Add-JournalRecord).
$Journal  = Join-Path (Join-Path $Sidecar "manifests") "$Slug.journal.jsonl"

if ($Rollback) { $rbrc = @(Invoke-Rollback)[-1]; exit ([int]$rbrc) }

# informational cloud detect for the VAULT (the whole point is the cloud is ALLOWED)
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

# the destination is NOT informational: a synced sidecar fixes nothing
if (-not (Test-SidecarLocation)) { exit 1 }

$script:GitState = Get-GitState
if (-not (Test-NoLive)) { exit 1 }

$failedPaths = 0
Say "Relocating .git ..."
if (-not (Move-GitDir)) { exit 4 }
$script:GitState = Get-GitState   # a fresh-init just created the repo

Say "Relocating worktrees ..."
if (-not (Move-CacheDir -Rel $WorktreesRel -Dst $SideWt -RType "worktrees")) { $failedPaths++ }

Say "Relocating caches ..."
foreach ($rel in $CacheDirs) {
  $dst = Join-Path $SideCache ($rel -replace '/', [System.IO.Path]::DirectorySeparatorChar)
  if (-not (Move-CacheDir -Rel $rel -Dst $dst -RType "cache")) { $failedPaths++ }
}

Write-Manifest

if ($DryRun) {
  Say "DRY-RUN complete - no changes made. Manifest would be: $Manifest"
  exit 0
}
if ($failedPaths -gt 0) {
  Err "$failedPaths path(s) could not be relocated (see the errors above). Record: $Journal"
  Err "Reverse what DID move: relocate-machinery-sidecar.ps1 `"$VaultAbs`" -Sidecar `"$Sidecar`" -Rollback"
  exit 4
}
Say "Done. Manifest: $Manifest"
Say "Reverse anytime: relocate-machinery-sidecar.ps1 `"$VaultAbs`" -Rollback"
exit 0
