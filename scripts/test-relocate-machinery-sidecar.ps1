#Requires -Version 5.1
<#
  Behavioral test for relocate-machinery-sidecar.ps1 (MYC-2383 - Windows parity
  of relocate-machinery-sidecar.sh). Runs under pwsh on any OS. On Windows the
  machinery links are junctions, elsewhere symbolic links; both carry the
  ReparsePoint attribute, so the link assertions hold cross-platform. Proves:
  .git -> pointer file via --separate-git-dir, caches -> links, manifest names
  the vault (the offer-suppression key), idempotency, full rollback, dry-run
  inertness, and the live-worktree refusal gate (paired negative control).

  Run: pwsh -File scripts/test-relocate-machinery-sidecar.ps1
#>
$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot
$MS   = Join-Path $Here "relocate-machinery-sidecar.ps1"
$script:fails = 0
$gear = [string][char]0x2699 + [string][char]0xFE0F   # keep this file ASCII-clean

function Check { param([string]$Label, [bool]$Cond)
  if ($Cond) { Write-Host "PASS  $Label" } else { Write-Host "FAIL  $Label"; $script:fails++ }
}
function Get-Py {
  foreach ($n in @("python3", "python")) {
    $c = Get-Command $n -ErrorAction SilentlyContinue
    if ($c) { $v = (& $c.Source -c "import sys;print(sys.version_info[0])" 2>$null); if ("$v".Trim() -eq "3") { return $c.Source } }
  }
  throw "python 3 required for the test"
}
$Py = Get-Py
function RealPath { param([string]$p) return ("$(& $Py -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' $p)").Trim() }
function Is-Reparse { param([string]$p)
  $it = Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
  return ($it -and (($it.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0))
}
function Run-MS { param([string[]]$MsArgs)
  $out = & pwsh -NoProfile -File $MS @MsArgs 2>&1
  return [pscustomobject]@{ rc = $LASTEXITCODE; out = ("$out" -join "`n") }
}
function New-Vault { param([string]$Path)
  New-Item -ItemType Directory -Force -Path $Path | Out-Null
  Set-Content -LiteralPath (Join-Path $Path "CLAUDE.md") -Value "# brain"
  & git -C $Path init -q | Out-Null
  & git -C $Path config user.email "t@example.com" | Out-Null
  & git -C $Path config user.name "t" | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $Path ".smart-env") | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $Path ".codegraph") | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $Path "$gear Meta/Sessions") | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $Path ".claude/worktrees") | Out-Null
  Set-Content -LiteralPath (Join-Path $Path ".smart-env/cache.bin") -Value "x"
}

function New-VaultTracked { param([string]$Path)
  # A vault whose "<gear> Meta/Sessions" notes are COMMITTED - i.e. a real one.
  # The caches are gitignored, so one fixture exercises both sides of the rule:
  # tracked content is never moved, untracked caches still are.
  New-Item -ItemType Directory -Force -Path $Path | Out-Null
  & git -C $Path init -q | Out-Null
  & git -C $Path config user.email "t@example.com" | Out-Null
  & git -C $Path config user.name "t" | Out-Null
  & git -C $Path config commit.gpgsign false | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $Path ".smart-env") | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $Path ".codegraph") | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $Path "$gear Meta/Sessions") | Out-Null
  Set-Content -LiteralPath (Join-Path $Path ".gitignore") -Value ".smart-env/`n.codegraph/`n"
  Set-Content -LiteralPath (Join-Path $Path ".smart-env/cache.bin") -Value "x"
  Set-Content -LiteralPath (Join-Path $Path "$gear Meta/Sessions/2026-08-01-work.md") -Value "note one"
  Set-Content -LiteralPath (Join-Path $Path "$gear Meta/Sessions/2026-08-02-work.md") -Value "note two"
  & git -C $Path add -A | Out-Null
  & git -C $Path commit -qm init | Out-Null
}
# How many paths git reports as DELETED: the number the user sees, the number
# auto-snapshot commits, and the number multi-machine sync pushes.
function Get-DeletedCount { param([string]$v)
  $out = & git -C $v status --porcelain 2>$null
  return @($out | Where-Object { "$_" -match '^.D|^D' }).Count
}
# -like, never -Filter: the Win32 filter can match *.json against *.jsonl (the
# 8.3 short-name rule), which would count the journal as a manifest.
function Get-ManifestFiles { param([string]$Side)
  return @(Get-ChildItem -LiteralPath (Join-Path $Side "manifests") -File -ErrorAction SilentlyContinue |
           Where-Object { $_.Name -like "*.json" })
}
function Get-JournalFiles { param([string]$Side)
  return @(Get-ChildItem -LiteralPath (Join-Path $Side "manifests") -File -ErrorAction SilentlyContinue |
           Where-Object { $_.Name -like "*.journal.jsonl" })
}

$TMP = Join-Path ([System.IO.Path]::GetTempPath()) ("ms-test-" + [Guid]::NewGuid().ToString("N").Substring(0, 10))
New-Item -ItemType Directory -Force -Path $TMP | Out-Null
try {
  # ---- 1. REAL relocate: .git pointer + cache links + manifest --------------
  $v1   = Join-Path $TMP "vault1"; New-Vault $v1
  $side = Join-Path $TMP "sidecar"
  $r = Run-MS -MsArgs @($v1, "-Sidecar", $side)
  Check "relocate: rc 0"                       ($r.rc -eq 0)
  $gitptr = Join-Path $v1 ".git"
  $gitIsFile = (Test-Path -LiteralPath $gitptr -PathType Leaf)
  Check "relocate: .git is now a pointer FILE" $gitIsFile
  if ($gitIsFile) {
    $first = Get-Content -LiteralPath $gitptr -TotalCount 1
    Check "relocate: .git pointer says 'gitdir:'" ("$first" -match '^gitdir:')
  }
  Check "relocate: .smart-env is a link"       (Is-Reparse (Join-Path $v1 ".smart-env"))
  Check "relocate: .codegraph is a link"       (Is-Reparse (Join-Path $v1 ".codegraph"))
  Check "relocate: emoji-Meta/Sessions is a link" (Is-Reparse (Join-Path $v1 "$gear Meta/Sessions"))
  Check "relocate: .claude/worktrees is a link" (Is-Reparse (Join-Path $v1 ".claude/worktrees"))
  Check "relocate: cached file survived the move" (Test-Path -LiteralPath (Join-Path $v1 ".smart-env/cache.bin"))
  $man = Get-ManifestFiles $side
  Check "relocate: manifest written"           ($man.Count -eq 1)
  if ($man.Count -eq 1) {
    $doc = Get-Content -Raw -LiteralPath $man[0].FullName | ConvertFrom-Json
    Check "relocate: manifest 'vault' = resolved vault (suppression key)" ($doc.vault -eq (RealPath $v1))
    Check "relocate: manifest schema tag"      ($doc.schema -eq "machinery-sidecar/1")
  }

  # ---- 2. idempotency: re-run is a no-op report -----------------------------
  $r = Run-MS -MsArgs @($v1, "-Sidecar", $side)
  Check "idempotent: rc 0"                     ($r.rc -eq 0)
  Check "idempotent: .git already a pointer"   ($r.out -match "already a pointer")
  Check "idempotent: a cache already a link"   ($r.out -match "already a link")

  # ---- 3. rollback restores .git dir + caches, removes manifest -------------
  $r = Run-MS -MsArgs @($v1, "-Sidecar", $side, "-Rollback")
  Check "rollback: rc 0"                       ($r.rc -eq 0)
  Check "rollback: .git is a real dir again"   (Test-Path -LiteralPath (Join-Path $v1 ".git") -PathType Container)
  Check "rollback: .smart-env is a real dir again" ((Test-Path -LiteralPath (Join-Path $v1 ".smart-env") -PathType Container) -and -not (Is-Reparse (Join-Path $v1 ".smart-env")))
  Check "rollback: cached file still present"  (Test-Path -LiteralPath (Join-Path $v1 ".smart-env/cache.bin"))
  Check "rollback: manifest removed"           (-not (Test-Path -LiteralPath $man[0].FullName))

  # ---- 4. dry-run inertness -------------------------------------------------
  $v4 = Join-Path $TMP "vault4"; New-Vault $v4
  $r = Run-MS -MsArgs @($v4, "-Sidecar", (Join-Path $TMP "sidecar4"), "-DryRun")
  Check "dry-run: rc 0"                        ($r.rc -eq 0)
  Check "dry-run: .git still a real dir"       (Test-Path -LiteralPath (Join-Path $v4 ".git") -PathType Container)
  Check "dry-run: no sidecar manifest written" (-not (Test-Path -LiteralPath (Join-Path $TMP "sidecar4/manifests")))

  # ---- 5. GUARD: a live linked worktree REFUSES (separate-git-dir orphans) --
  $v5 = Join-Path $TMP "vault5"; New-Vault $v5
  Set-Content -LiteralPath (Join-Path $v5 "f.txt") -Value "x"
  & git -C $v5 add -A | Out-Null
  & git -C $v5 commit -qm init | Out-Null
  & git -C $v5 worktree add -q (Join-Path $TMP "wt5") -b scratch5 | Out-Null
  $r = Run-MS -MsArgs @($v5, "-Sidecar", (Join-Path $TMP "sidecar5"))
  Check "guard: linked worktree -> rc 1 (refuse)" ($r.rc -eq 1)
  Check "guard: explains the refusal"          ($r.out -match "refusing")
  Check "guard: .git untouched (still a dir)"   (Test-Path -LiteralPath (Join-Path $v5 ".git") -PathType Container)
  # negative control: -Force proceeds past the same guard
  $r = Run-MS -MsArgs @($v5, "-Sidecar", (Join-Path $TMP "sidecar5"), "-Force")
  Check "guard neg-control: -Force proceeds (rc 0)" ($r.rc -eq 0)

  # ---- 6. TRACKED NOTES ARE NEVER RELOCATED (the data-loss regression) ------
  # "<gear> Meta/Sessions" is BOTH a CacheDirs name AND, in a real vault, over a
  # thousand tracked markdown notes. Relocating it turns the directory into a
  # junction/symlink, `git status` reports every note as deleted, and the next
  # auto-commit + push propagates that deletion to every machine. The rule is the
  # git INDEX, never the name. Negative control: the ignored cache still moves.
  $v6 = Join-Path $TMP "vault6"; New-VaultTracked $v6
  $side6 = Join-Path $TMP "sidecar6"
  $r = Run-MS -MsArgs @($v6, "-Sidecar", $side6)
  Check "tracked: rc 0"                        ($r.rc -eq 0)
  Check "tracked: ZERO deletions in git status" ((Get-DeletedCount $v6) -eq 0)
  Check "tracked: Sessions left as a real directory" `
        ((Test-Path -LiteralPath (Join-Path $v6 "$gear Meta/Sessions") -PathType Container) -and `
         -not (Is-Reparse (Join-Path $v6 "$gear Meta/Sessions")))
  Check "tracked: the notes are still on disk" `
        (Test-Path -LiteralPath (Join-Path $v6 "$gear Meta/Sessions/2026-08-01-work.md"))
  Check "tracked: reports WHY it kept the path" ($r.out -match "KEPT: git tracks")
  Check "tracked neg-control: the ignored cache still relocated" (Is-Reparse (Join-Path $v6 ".smart-env"))

  # ---- 7. INTERRUPT: -Rollback works after a kill between move and link -----
  # The manifest used to be written on the LAST line, so any interrupt left no
  # record and -Rollback hard-refused. A kill between Move-Item and the link left
  # the directory ABSENT with its content stranded in the sidecar. Every move is
  # now journalled (append + fsync) BEFORE it happens.
  $v7 = Join-Path $TMP "vault7"; New-VaultTracked $v7
  $side7 = Join-Path $TMP "sidecar7"
  $env:BRAIN_SIDECAR_TEST_KILL_AT = "post-mv:.smart-env"
  $r = Run-MS -MsArgs @($v7, "-Sidecar", $side7)
  $env:BRAIN_SIDECAR_TEST_KILL_AT = ""
  Check "interrupt: the run was killed (rc non-zero)" ($r.rc -ne 0)
  Check "interrupt: .smart-env is ABSENT mid-move (the window that used to be unrecoverable)" `
        (-not (Test-Path -LiteralPath (Join-Path $v7 ".smart-env")))
  Check "interrupt: no manifest was written" ((Get-ManifestFiles $side7).Count -eq 0)
  Check "interrupt: the write-ahead journal survived" ((Get-JournalFiles $side7).Count -eq 1)
  $r = Run-MS -MsArgs @($v7, "-Sidecar", $side7, "-Rollback")
  Check "interrupt: rollback after the kill -> rc 0" ($r.rc -eq 0)
  Check "interrupt: .smart-env restored as a real dir" `
        ((Test-Path -LiteralPath (Join-Path $v7 ".smart-env") -PathType Container) -and -not (Is-Reparse (Join-Path $v7 ".smart-env")))
  Check "interrupt: the cached file came back"  (Test-Path -LiteralPath (Join-Path $v7 ".smart-env/cache.bin"))
  Check "interrupt: .git restored as a real dir" (Test-Path -LiteralPath (Join-Path $v7 ".git") -PathType Container)
  Check "interrupt: ZERO deletions after rollback" ((Get-DeletedCount $v7) -eq 0)

  # ---- 8. A FAILED ROLLBACK SAYS SO (and keeps the evidence) ----------------
  # The old rollback reported "Rollback complete." and exited 0 no matter what,
  # then deleted the manifest - the only map back - on the way out.
  $v8 = Join-Path $TMP "vault8"; New-Vault $v8
  $side8 = Join-Path $TMP "sidecar8"
  $r = Run-MS -MsArgs @($v8, "-Sidecar", $side8)
  Remove-Item -LiteralPath (Join-Path $side8 "git") -Recurse -Force
  $r = Run-MS -MsArgs @($v8, "-Sidecar", $side8, "-Rollback")
  Check "failed rollback: rc non-zero"          ($r.rc -ne 0)
  Check "failed rollback: says INCOMPLETE"      ($r.out -match "INCOMPLETE")
  Check "failed rollback: the record was KEPT" ((Get-JournalFiles $side8).Count -eq 1)

  # ---- 9. A PROBE THAT CANNOT RUN COUNTS AS LIVE (fail closed) --------------
  # The live-session probe printed nothing when the lock could not be parsed and
  # the caller read that as ZERO sessions, so an unreadable lock silently
  # disarmed the refusal that keeps separate-git-dir from orphaning live work.
  $v9 = Join-Path $TMP "vault9"; New-Vault $v9
  New-Item -ItemType Directory -Force -Path (Join-Path $v9 ".claude") | Out-Null
  $lock9 = Join-Path $v9 ".claude/.session-lock.json"
  Set-Content -LiteralPath $lock9 -Value "{ this is not json"
  $r = Run-MS -MsArgs @($v9, "-Sidecar", (Join-Path $TMP "sidecar9"))
  Check "fail-closed: an unreadable session lock -> rc 1 (refuse)" ($r.rc -eq 1)
  Check "fail-closed: explains that a probe could not run" ($r.out -match "could not run")
  Check "fail-closed: .git untouched"           (Test-Path -LiteralPath (Join-Path $v9 ".git") -PathType Container)
  Set-Content -LiteralPath $lock9 -Value '{"sessions": {}}'
  $r = Run-MS -MsArgs @($v9, "-Sidecar", (Join-Path $TMP "sidecar9"))
  Check "fail-closed neg-control: a readable empty lock -> rc 0" ($r.rc -eq 0)

  # ---- 10. THE SIDECAR DESTINATION IS CHECKED TOO ---------------------------
  # On a roaming / OneDrive-managed / network profile the profile folder is
  # itself synced, so the default ~/.brain-sidecar lands INSIDE a sync root:
  # .git moves from one synced tree to another, every success check passes, and
  # the melt is unchanged - now with the notes split off as well.
  $v10 = Join-Path $TMP "vault10"; New-Vault $v10
  $r = Run-MS -MsArgs @($v10, "-Sidecar", (Join-Path (Join-Path $TMP "OneDrive") "brain-sidecar"))
  Check "sidecar guard: a cloud-synced destination -> rc 1" ($r.rc -eq 1)
  Check "sidecar guard: names the service"      ($r.out -match "refusing: the sidecar destination is inside OneDrive")
  Check "sidecar guard: vault untouched"        (Test-Path -LiteralPath (Join-Path $v10 ".git") -PathType Container)
  $r = Run-MS -MsArgs @($v10, "-Sidecar", (Join-Path $v10 ".sidecar"))
  Check "sidecar guard: a destination inside the vault -> rc 1" ($r.rc -eq 1)
  $r = Run-MS -MsArgs @($v10, "-Sidecar", (Join-Path $TMP "sidecar10"))
  Check "sidecar guard neg-control: a local destination -> rc 0" ($r.rc -eq 0)
}
finally {
  # detach any worktree gitdir lock before cleanup
  Remove-Item -LiteralPath $TMP -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
if ($script:fails -gt 0) { Write-Host "FAILED: $($script:fails)"; exit 1 }
Write-Host "ALL TESTS PASSED"
exit 0
