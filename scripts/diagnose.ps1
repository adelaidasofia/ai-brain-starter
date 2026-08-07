# diagnose.ps1 - Self-check for an installed AI Brain Starter vault.
# Windows port of diagnose.sh. Runs ~10 checks, prints green/yellow/red.
#
# Usage:
#   pwsh diagnose.ps1                      # use $env:VAULT_PATH or current dir
#   pwsh diagnose.ps1 -Vault C:\path\vault
#
# Exit codes: 0 = all green, 1 = at least one FAIL, 2 = only WARNs.
#
# This file MUST start with a UTF-8 BOM (EF BB BF). Windows PowerShell 5.1
# reads BOM-less .ps1 as Windows-1252 and crashes on non-ASCII bytes.
# Verify with: Get-Content -Encoding Byte -TotalCount 3 diagnose.ps1
# Should print: 239 187 191

param(
  [string]$Vault = $env:VAULT_PATH
)

$script:Green  = 0
$script:Yellow = 0
$script:Red    = 0

function Section($title) { Write-Host ""; Write-Host $title -ForegroundColor Cyan }
function Ok($msg)        { Write-Host "  OK    $msg" -ForegroundColor Green;  $script:Green++ }
function Warn($msg, $hint = $null) {
  Write-Host "  WARN  $msg" -ForegroundColor Yellow
  if ($hint) { Write-Host "        -> $hint" -ForegroundColor DarkYellow }
  $script:Yellow++
}
function Bad($msg, $hint = $null) {
  Write-Host "  FAIL  $msg" -ForegroundColor Red
  if ($hint) { Write-Host "        -> $hint" -ForegroundColor DarkRed }
  $script:Red++
}

# ----- locate vault -----
if (-not $Vault) { $Vault = (Get-Location).Path }
if (-not (Test-Path -LiteralPath $Vault -PathType Container)) {
  Write-Host "Vault path not a directory: $Vault" -ForegroundColor Red
  Write-Host "Pass the vault path: pwsh diagnose.ps1 -Vault C:\path\to\vault"
  exit 1
}
$Vault = (Resolve-Path -LiteralPath $Vault).Path

Write-Host "AI Brain Starter diagnostics" -ForegroundColor White
Write-Host "Vault: $Vault"

# ----- 1. CLAUDE.md -----
Section "1. Vault memory (CLAUDE.md)"
$claudeMd = Join-Path $Vault "CLAUDE.md"
if (Test-Path -LiteralPath $claudeMd) {
  $size = (Get-Item -LiteralPath $claudeMd).Length
  if ($size -lt 200) {
    Warn "CLAUDE.md exists but is tiny ($size bytes)" "Re-run /setup-brain Phase 4."
  } else {
    Ok "CLAUDE.md present ($size bytes)"
  }
  if (Select-String -LiteralPath $claudeMd -Pattern "## Vault Map" -Quiet) {
    Ok "Vault Map section present"
  } else {
    Warn "No '## Vault Map' section in CLAUDE.md" "Claude will create duplicate folders without it."
  }
} else {
  Bad "CLAUDE.md missing" "Run /setup-brain Phase 4."
}

# ----- 2. Meta folder -----
Section "2. Meta folder"
$meta = Join-Path $Vault "$([char]0x2699)$([char]0xFE0F) Meta"
if (Test-Path -LiteralPath $meta -PathType Container) {
  Ok "Meta/ folder present"
  foreach ($sub in @("scripts", "rules")) {
    if (Test-Path -LiteralPath (Join-Path $meta $sub) -PathType Container) {
      Ok "Meta/$sub/ present"
    } else {
      Warn "Meta/$sub/ missing"
    }
  }
} else {
  Bad "Meta/ folder missing" "Run /setup-brain Phase 3."
}

# ----- 3. Skills installed -----
Section "3. Claude Code skills"
$skillsDir = Join-Path $env:USERPROFILE ".claude\skills"
if (Test-Path -LiteralPath $skillsDir -PathType Container) {
  Ok "~/.claude/skills/ exists"
  if (Test-Path -LiteralPath (Join-Path $skillsDir "ai-brain-starter") -PathType Container) {
    Ok "ai-brain-starter skill installed"
  } else {
    Warn "ai-brain-starter skill not in ~/.claude/skills/" "Re-run bootstrap to clone it."
  }
  if (Test-Path -LiteralPath (Join-Path $skillsDir "daily-journal") -PathType Container) {
    Ok "daily-journal skill installed"
  } else {
    Warn "daily-journal skill not installed" "/setup-brain Phase 10a creates it."
  }
} else {
  Bad "~/.claude/skills/ missing" "Claude Code may not be installed."
}

# ----- 4. Hooks -----
Section "4. Claude Code hooks"
$settings      = Join-Path $env:USERPROFILE ".claude\settings.json"
$localSettings = Join-Path $Vault ".claude\settings.local.json"
$hookFound = $false
foreach ($f in @($settings, $localSettings)) {
  if ((Test-Path -LiteralPath $f) -and (Select-String -LiteralPath $f -Pattern '"hooks"' -Quiet)) {
    $hookFound = $true
    Ok "hooks registered in $f"
  }
}
if (-not $hookFound) {
  Warn "No hooks registered" "/setup-brain Phase 5 wires them. Without them, no auto context-loading."
}

# Wired-but-absent: a hook command naming ~/.claude/hooks/<file> that is not on
# disk. Every such command carries a `[ -f ]` guard (or hook_runner's
# --fallback silent), so the miss is a SILENT no-op -- the hook never fires and
# nothing says so. Checking that settings.json merely CONTAINS "hooks" (above)
# passes even when every referenced file is gone.
#
# Observed on a real install: vault-context.py wired 7 times, present 0 times,
# alongside retry-budget.py and validate-mcp-json.py -- all three are
# phase-05 "install by default" copies that never ran. scripts/check-home-hook-deploy.py
# does NOT cover this: it is a static REPO lint proving each hook has a
# documented deploy route, and it cannot see whether that route ever executed here.
#
# Reported as a WARNING, not an error, because phase-05 documents
# `rm ~/.claude/hooks/<name>.py` as the supported uninstall -- an absent file is
# genuinely ambiguous between never-installed and deliberately-removed. Naming
# the file lets the reader tell which; silence does not.
$absHooksDir = Join-Path $env:USERPROFILE ".claude\hooks"
$wiredNames  = New-Object System.Collections.Generic.HashSet[string]
foreach ($f in @($settings, $localSettings)) {
  if (-not (Test-Path -LiteralPath $f)) { continue }
  $raw = Get-Content -LiteralPath $f -Raw -ErrorAction SilentlyContinue
  if (-not $raw) { continue }
  # Anchor on .claude/hooks/ so the skill's own hooks dir
  # (.claude/skills/ai-brain-starter/hooks/) is not swept in. Separator class
  # covers POSIX "/" and JSON-escaped Windows "\\".
  foreach ($m in [regex]::Matches($raw, '\.claude[\\/]+hooks[\\/]+([A-Za-z0-9_.-]+\.(?:py|sh))')) {
    [void]$wiredNames.Add($m.Groups[1].Value)
  }
}
if ($wiredNames.Count -gt 0) {
  $missingHooks = @($wiredNames | Where-Object { -not (Test-Path -LiteralPath (Join-Path $absHooksDir $_)) } | Sort-Object)
  if ($missingHooks.Count -eq 0) {
    Ok ("all {0} wired ~/.claude/hooks/ file(s) present on disk" -f $wiredNames.Count)
  } else {
    $src = Join-Path $env:USERPROFILE ".claude\skills\ai-brain-starter\hooks"
    $restorable = @($missingHooks | Where-Object { Test-Path -LiteralPath (Join-Path $src $_) })
    $hint = "These never fire and report no error. "
    if ($restorable.Count -gt 0) {
      $hint += ("Reinstall: Copy-Item '{0}\{1}' -Destination '{2}\'" -f $src, ($restorable -join "','$src\"), $absHooksDir)
      $hint += " -- or, if you removed them on purpose, this is expected."
    } else {
      $hint += "No source copy found in the starter's hooks/ dir either."
    }
    $msg = "{0} of {1} wired ~/.claude/hooks/ file(s) MISSING: {2}" -f $missingHooks.Count, $wiredNames.Count, ($missingHooks -join ", ")
    Warn $msg $hint
  }
}

$gch = Join-Path $meta "scripts\graph-context-hook.sh"
if (Test-Path -LiteralPath $gch) {
  Ok "graph-context-hook.sh present (Bash, runs in WSL/Git Bash on Windows)"
} else {
  Warn "graph-context-hook.sh not in Meta/scripts/" "Phase 5 installs it."
}

# ----- 5. Insights pipeline -----
Section "5. Insights pipeline"
$index = Join-Path $meta "journal-index.json"
if (Test-Path -LiteralPath $index) {
  $ageDays = [int]((Get-Date) - (Get-Item -LiteralPath $index).LastWriteTime).TotalDays
  if ($ageDays -gt 14) {
    Warn "journal-index.json is $ageDays days old" "Re-run build-journal-index.py or /weekly to refresh."
  } else {
    Ok "journal-index.json is fresh ($ageDays days old)"
  }
  try {
    Get-Content -LiteralPath $index -Raw | ConvertFrom-Json | Out-Null
    Ok "journal-index.json is valid JSON"
  } catch {
    Bad "journal-index.json is malformed" "Delete it and re-run build-journal-index.py."
  }
} else {
  Warn "No journal-index.json yet" "/weekly or /monthly will build it on first run."
}

# ----- 6. Required CLI tools -----
Section "6. Required CLI tools"
foreach ($tool in @("git", "python", "python3")) {
  if (Get-Command $tool -ErrorAction SilentlyContinue) {
    Ok "$tool installed"
  } elseif ($tool -eq "python3") {
    # On Windows the binary is usually 'python', so this is fine if python is present
    if (Get-Command python -ErrorAction SilentlyContinue) {
      Ok "python3 available (as 'python')"
    } else {
      Bad "python missing" "Install from python.org or 'winget install Python.Python.3.12'"
    }
  } elseif ($tool -eq "python") {
    # handled above
  } else {
    Bad "$tool missing" "Install $tool. 'winget install Git.Git' for git."
  }
}

# ----- 7. Vault git -----
Section "7. Vault git status"
if (Test-Path -LiteralPath (Join-Path $Vault ".git") -PathType Container) {
  Ok "Vault is a git repo (snapshot history available)"
  Push-Location -LiteralPath $Vault
  $remote = git remote -v 2>$null | Select-Object -First 1
  Pop-Location
  if ($remote) {
    Warn "Vault has a git remote: $remote" "Vaults are usually local-only."
  } else {
    Ok "No git remote (correct for a private vault)"
  }
} else {
  Warn "Vault is not a git repo" "You won't have rollback history. Optional but recommended."
}

# ----- 8. .ps1 sanity -----
Section "8. PowerShell files (Windows compat)"
$searchRoots = @($Vault, (Join-Path $env:USERPROFILE ".claude\skills\ai-brain-starter")) | Where-Object { Test-Path -LiteralPath $_ }
$ps1Files = @()
foreach ($root in $searchRoots) {
  $ps1Files += Get-ChildItem -LiteralPath $root -Recurse -Filter '*.ps1' -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '\\\.git\\' }
}
if ($ps1Files.Count -eq 0) {
  Ok "No .ps1 files to check"
} else {
  $bomFail = 0; $emFail = 0; $parseFail = 0
  foreach ($f in $ps1Files) {
    $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
    if ($bytes.Length -lt 3 -or $bytes[0] -ne 0xEF -or $bytes[1] -ne 0xBB -or $bytes[2] -ne 0xBF) {
      $bomFail++
    }
    $text = [System.Text.Encoding]::UTF8.GetString($bytes)
    if ($text.Contains([char]0x2014)) { $emFail++ }
    $tokens = $null; $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
      $f.FullName, [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors -and $errors.Count -gt 0) { $parseFail++ }
  }
  if ($bomFail -eq 0) { Ok "All .ps1 files have UTF-8 BOM" }
  else { Warn "$bomFail .ps1 file(s) missing UTF-8 BOM" "PS 5.1 will crash on non-ASCII." }
  if ($emFail -eq 0) { Ok "No em dashes in .ps1 files" }
  else { Warn "$emFail .ps1 file(s) contain em dashes" "Replace with ASCII hyphens." }
  if ($parseFail -eq 0) { Ok "All .ps1 files parse cleanly" }
  else { Bad "$parseFail .ps1 file(s) have parser errors" "Run pwsh on each to see the error." }
}

# ----- 9. MCP config -----
Section "9. MCP servers"
$mcpLocal  = Join-Path $Vault ".mcp.json"
$mcpGlobal = Join-Path $env:USERPROFILE ".claude.json"
$mcpFound = $false
foreach ($f in @($mcpLocal, $mcpGlobal)) {
  if ((Test-Path -LiteralPath $f) -and (Select-String -LiteralPath $f -Pattern '"mcpServers"' -Quiet)) {
    $mcpFound = $true
    try {
      Get-Content -LiteralPath $f -Raw | ConvertFrom-Json | Out-Null
      Ok "MCP config valid: $f"
    } catch {
      Bad "MCP config malformed: $f" "Invalid JSON. Fix the file."
    }
  }
}
if (-not $mcpFound) { Warn "No MCP config found" "MCPs are optional." }

# ----- 10b. Scheduled-task naming hygiene -----
Section "10b. Scheduled-task naming hygiene"
$schedDir = Join-Path $env:USERPROFILE ".claude\scheduled-tasks"
$skillsDir = Join-Path $env:USERPROFILE ".claude\skills"
if (Test-Path -LiteralPath $schedDir -PathType Container) {
  $collideNames = @()
  $noprefixNames = @()
  Get-ChildItem -LiteralPath $schedDir -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    $taskName = $_.Name
    $skillMd = Join-Path $_.FullName "SKILL.md"
    if (-not (Test-Path -LiteralPath $skillMd)) { return }
    # Collision: scheduled-task name matches an installed skill name.
    $skillCandidate = Join-Path $skillsDir $taskName
    if (Test-Path -LiteralPath $skillCandidate -PathType Container) {
      $collideNames += $taskName
    }
    # Convention: cron-only tasks should prefix with _ to read as cron-only.
    if (-not $taskName.StartsWith("_")) {
      $noprefixNames += $taskName
    }
  }
  if ($collideNames.Count -eq 0 -and $noprefixNames.Count -eq 0) {
    Ok "Scheduled-task names are clean (no skill collisions, all underscore-prefixed)"
  } else {
    if ($collideNames.Count -gt 0) {
      Warn "$($collideNames.Count) scheduled-task name(s) collide with installed skills: $($collideNames -join ', ')" `
        "Rename: e.g. 'daily-journal' -> '_daily-journal-cron'. See docs/MAINTENANCE.md."
    }
    if ($noprefixNames.Count -gt 0) {
      Warn "$($noprefixNames.Count) scheduled-task name(s) lack '_' prefix: $($noprefixNames -join ', ')" `
        "Cron-only tasks should start with _ to read as cron-only in autocomplete. See docs/MAINTENANCE.md."
    }
  }
} else {
  Ok "No ~/.claude/scheduled-tasks/ directory (skipped)"
}

# ----- 10. ai-brain-starter freshness -----
Section "10. ai-brain-starter freshness"
$absDir = Join-Path $env:USERPROFILE ".claude\skills\ai-brain-starter"
if (Test-Path -LiteralPath (Join-Path $absDir ".git") -PathType Container) {
  Push-Location -LiteralPath $absDir
  $localSha = (git rev-parse HEAD 2>$null)
  if ($localSha) { $localSha = $localSha.Substring(0, 7) }
  $fetchOk = $false
  try { git fetch origin main --quiet 2>$null; $fetchOk = $true } catch {}
  if ($fetchOk) {
    $remoteSha = (git rev-parse origin/main 2>$null)
    if ($remoteSha) { $remoteSha = $remoteSha.Substring(0, 7) }
    if ($localSha -eq $remoteSha) {
      Ok "ai-brain-starter is up to date ($localSha)"
    } else {
      $behind = (git rev-list --count "$localSha..$remoteSha" 2>$null)
      Warn "ai-brain-starter is $behind commit(s) behind origin/main" "cd $absDir; git pull"
    }
  } else {
    Warn "Could not fetch from origin (offline?)"
  }
  Pop-Location
} else {
  Warn "ai-brain-starter is not a git repo" "Re-run bootstrap.ps1 to clone it."
}

# ----- 11. cloud-sync location (the freeze class) -----
Section "11. Cloud-sync location"
$checkCloud = $null
$cloudCands = @(
  (Join-Path $PSScriptRoot "check-cloud-sync.py"),
  (Join-Path $env:USERPROFILE ".claude\skills\ai-brain-starter\scripts\check-cloud-sync.py")
)
foreach ($c in $cloudCands) { if (Test-Path -LiteralPath $c) { $checkCloud = $c; break } }
if ($checkCloud) {
  $py = Get-Command python -ErrorAction SilentlyContinue
  if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
  if ($py) {
    $verdict = "$(& $py.Source $checkCloud --porcelain $Vault 2>$null)".Trim()
    if ($verdict -like "OK_LOCAL*") {
      Ok "Vault is on a local disk (not a consumer cloud-sync root)"
    } elseif ($verdict -like "CLOUD_SYNC_RISK:*") {
      $svc = ($verdict -replace "^CLOUD_SYNC_RISK:", "")
      Bad "Vault is inside $svc (a consumer cloud-sync folder)" "A git-backed vault here melts the sync engine (pegged CPU / frozen machine). Move it local. See docs/CLOUD_SYNC.md."
    } else {
      Warn "Could not evaluate cloud-sync location" "check-cloud-sync.py returned: $verdict"
    }
  } else {
    Warn "python not found" "Cannot verify the vault is outside a cloud-sync root."
  }
} else {
  Warn "check-cloud-sync.py not found" "Cannot verify the vault is outside a cloud-sync root."
}

# ----- 12. off-machine backup (the one-disk-failure class) -----
Section "12. Off-machine backup"
$checkBackup = $null
$backupCands = @(
  (Join-Path $PSScriptRoot "check-vault-backup.py"),
  (Join-Path $env:USERPROFILE ".claude\skills\ai-brain-starter\scripts\check-vault-backup.py")
)
foreach ($c in $backupCands) { if (Test-Path -LiteralPath $c) { $checkBackup = $c; break } }
if ($checkBackup) {
  $py = Get-Command python -ErrorAction SilentlyContinue
  if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
  if ($py) {
    $bverdict = "$(& $py.Source $checkBackup --porcelain $Vault 2>$null)".Trim()
    if ($bverdict -like "BACKED_UP:vault-backup:*") {
      $age = ($bverdict -split ":")[-1]
      # An archive that EXISTS is not the same as a backup that RUNS. Mirror the
      # staleness contract of hooks/surface-backup-status.py so the two surfaces
      # cannot disagree: past the threshold, a snapshot is evidence the schedule
      # stopped firing, not evidence of a healthy backup.
      $staleDays = 3.0
      $envStale = 0.0
      if ($env:VAULT_BACKUP_STALE_DAYS -and [double]::TryParse(
            $env:VAULT_BACKUP_STALE_DAYS, [Globalization.NumberStyles]::Float,
            [Globalization.CultureInfo]::InvariantCulture, [ref]$envStale)) {
        $staleDays = $envStale
      }
      # InvariantCulture on purpose: check-vault-backup.py always emits a '.'
      # decimal, which a comma-decimal locale (es-ES, de-DE, fr-FR) would fail
      # to parse, silently collapsing every age to 0 and re-greening the check.
      $ageNum = 0.0
      $ageOk = [double]::TryParse($age, [Globalization.NumberStyles]::Float,
                                  [Globalization.CultureInfo]::InvariantCulture, [ref]$ageNum)
      if ($ageOk -and $ageNum -gt $staleDays) {
        Warn "Last vault snapshot is ~$age days old (> ${staleDays}d) - the backup is not running on schedule" "Run it now: pwsh scripts/vault-backup.ps1 run -Vault '$Vault'. Then find out why the schedule stopped: on Windows a task with DisallowStartIfOnBatteries never fires on a laptop running on battery (last result 0x800710E0)."
      } else {
        Ok "Off-machine backup present (vault-backup, ~$age days old)"
      }
    } elseif ($bverdict -eq "BACKED_UP:timemachine") {
      Ok "Off-machine backup present (Time Machine destination configured)"
    } elseif ($bverdict -like "BACKED_UP:cloud:*") {
      $svc = ($bverdict -replace "^BACKED_UP:cloud:", "")
      Ok "Off-machine copy present ($svc - a cloud copy; single-file snapshots are safer, see docs/BACKUP.md)"
    } elseif ($bverdict -eq "BACKED_UP:git-remote") {
      Ok "Off-machine backup present (git HEAD pushed to a remote)"
    } elseif ($bverdict -eq "NO_BACKUP:configured-not-run") {
      Warn "Backup configured but no snapshot exists yet (or destination unreachable)" "Run: pwsh scripts/vault-backup.ps1 run -Vault '$Vault'"
    } elseif ($bverdict -eq "NO_BACKUP") {
      Bad "Vault has NO off-machine backup - one disk failure loses everything" "Set one up (one command): pwsh scripts/vault-backup.ps1 setup -Vault '$Vault'. See docs/BACKUP.md."
    } else {
      Warn "Could not evaluate backup status" "check-vault-backup.py returned: $bverdict"
    }
  } else {
    Warn "python not found" "Cannot verify the vault has an off-machine backup."
  }
} else {
  Warn "check-vault-backup.py not found" "Cannot verify the vault has an off-machine backup."
}

# ----- 13. Obsidian renderer crashes (the large-vault OOM class) -----
Section "13. Obsidian renderer crashes"
$checkRenderer = $null
$rendererCands = @(
  (Join-Path $PSScriptRoot "check-renderer-crashes.py"),
  (Join-Path $env:USERPROFILE ".claude\skills\ai-brain-starter\scripts\check-renderer-crashes.py")
)
foreach ($c in $rendererCands) { if (Test-Path -LiteralPath $c) { $checkRenderer = $c; break } }
if ($checkRenderer) {
  $py = Get-Command python -ErrorAction SilentlyContinue
  if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
  if ($py) {
    $rverdict = "$(& $py.Source $checkRenderer --porcelain 2>$null)".Trim()
    if ($rverdict -eq "OK_NO_CRASHES") {
      Ok "No repeated Obsidian renderer crashes"
    } elseif ($rverdict -eq "SKIP_NOT_MACOS") {
      Ok "Renderer-crash check skipped (macOS-only crash reports)"
    } elseif ($rverdict -like "RENDERER_CRASHES:*") {
      $cnt = ($rverdict -replace "^RENDERER_CRASHES:", "")
      Warn "Repeated Obsidian renderer crashes ($cnt in ~14 days, EXC_BREAKPOINT / renderer OOM)" "A heavy indexer plugin is likely exhausting the renderer on a large vault. Quit Obsidian, set .obsidian/community-plugins.json to [] (restricted mode), reopen, enable Dataview only, then add others one at a time. Scope or drop Smart Connections / Tasks. See templates/rules/obsidian-plugins.md 'Large-vault plugin posture'."
    } else {
      Warn "Could not evaluate renderer-crash history" "check-renderer-crashes.py returned: $rverdict"
    }
  } else {
    Warn "python not found" "Cannot check for repeated Obsidian renderer crashes."
  }
} else {
  Warn "check-renderer-crashes.py not found" "Cannot check for repeated Obsidian renderer crashes."
}

# ----- summary -----
Write-Host ""
Write-Host "Summary" -ForegroundColor White
Write-Host "  OK:   $script:Green"  -ForegroundColor Green
Write-Host "  WARN: $script:Yellow" -ForegroundColor Yellow
Write-Host "  FAIL: $script:Red"    -ForegroundColor Red
Write-Host ""

if ($script:Red -gt 0) {
  Write-Host "Something is broken. Fix the FAILs above, then re-run." -ForegroundColor Red
  exit 1
} elseif ($script:Yellow -gt 0) {
  Write-Host "Working, with caveats. Address WARNs when convenient." -ForegroundColor Yellow
  exit 2
} else {
  Write-Host "All green. Your second brain is healthy." -ForegroundColor Green
  exit 0
}
