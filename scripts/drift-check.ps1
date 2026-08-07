# drift-check.ps1 - detect file drift between the ai-brain-starter repo and
# the user's installed copies. READ-ONLY: never modifies anything.
#
# See drift-check.sh for full docs and rationale. This is the PowerShell port
# for Windows users. Behavior, output format, and cooldown semantics are
# identical to the bash version.
#
# Usage:
#   powershell -File drift-check.ps1
#   powershell -File drift-check.ps1 -Vault "C:\path\to\vault"
#   powershell -File drift-check.ps1 -Vault "C:\path\to\vault" -Force
#
# Output format (stable, parseable - matches drift-check.sh):
#   STATUS: <OK | SKIPPED_TODAY | ERROR>
#   DRIFT_COUNT: <integer>
#   ---DRIFT_FILES---
#   <scope>|<installed_path>|<repo_source_path>|<note>
#   ...
#   ---END---
#
#   Scopes: skill | vault-script | vault-rule

param(
    [string]$Vault = "",
    [switch]$Force
)

# UTF-8 output for emoji-containing paths (⚙️ Meta/...) and consistent line endings
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# Self-locate the starter dir from the script's own location
# scripts\drift-check.ps1 → parent dir is the repo root
$ScriptDir   = Split-Path -Parent $PSCommandPath
$StarterDir  = Split-Path -Parent $ScriptDir
$InstallDir  = "$env:USERPROFILE\.claude\skills"
$CooldownFile = "$env:USERPROFILE\.claude\.ai-brain-starter-drift-check-last-run"
$IgnoreFile  = "$env:USERPROFILE\.claude\.ai-brain-starter-drift-check-ignore"
$Today = (Get-Date -Format "yyyy-MM-dd")

# ── Cooldown ──────────────────────────────────────────────────────────────
if (-not $Force -and (Test-Path -LiteralPath $CooldownFile)) {
    $last = (Get-Content -LiteralPath $CooldownFile -ErrorAction SilentlyContinue | Out-String).Trim()
    if ($last -eq $Today) {
        Write-Output "STATUS: SKIPPED_TODAY"
        exit 0
    }
}

# ── Repo guard ────────────────────────────────────────────────────────────
if (-not (Test-Path -LiteralPath $StarterDir)) {
    Write-Output "STATUS: ERROR"
    Write-Output "REASON: ai-brain-starter not installed at $StarterDir"
    exit 0
}

# ── Helpers ───────────────────────────────────────────────────────────────
$DriftLines = New-Object System.Collections.ArrayList

# Per-user ignore registry. Each line is either a literal installed path OR
# a wildcard pattern (PowerShell -like syntax). `#` starts a comment, blank
# lines are ignored. Trailing whitespace stripped. Mirrors drift-check.sh.
$IgnorePatterns = @()
if (Test-Path -LiteralPath $IgnoreFile) {
    $IgnorePatterns = @(Get-Content -LiteralPath $IgnoreFile -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#")) { $line }
    })
}

function Test-IsIgnored {
    param([string]$Path)
    if ($IgnorePatterns.Count -eq 0) { return $false }
    foreach ($pat in $IgnorePatterns) {
        if ($Path -like $pat) { return $true }
    }
    return $false
}

function Test-FilesIdentical {
    param([string]$PathA, [string]$PathB)
    if (-not (Test-Path -LiteralPath $PathA) -or -not (Test-Path -LiteralPath $PathB)) {
        return $false
    }
    $hashA = (Get-FileHash -LiteralPath $PathA -Algorithm SHA256).Hash
    $hashB = (Get-FileHash -LiteralPath $PathB -Algorithm SHA256).Hash
    return ($hashA -eq $hashB)
}

function Add-Drift {
    param(
        [string]$Scope,
        [string]$InstalledPath,
        [string]$RepoSourcePath,
        [string]$Note = ""
    )
    if (Test-IsIgnored $InstalledPath) { return }
    [void]$DriftLines.Add("$Scope|$InstalledPath|$RepoSourcePath|$Note")
}

function Get-MarkdownBlock {
    # Extract a top-level (#) markdown block from a file, starting at the
    # given heading line and ending at the next # heading or EOF.
    param([string]$FilePath, [string]$Heading)

    if (-not (Test-Path -LiteralPath $FilePath)) { return "" }
    $lines = Get-Content -LiteralPath $FilePath -Encoding UTF8
    $block = New-Object System.Collections.ArrayList
    $inBlock = $false
    foreach ($line in $lines) {
        if ($line -eq $Heading) {
            $inBlock = $true
            [void]$block.Add($line)
            continue
        }
        if ($inBlock -and $line.StartsWith("# ") -and $line -ne $Heading) {
            break
        }
        if ($inBlock) {
            [void]$block.Add($line)
        }
    }
    return ($block -join "`n")
}

function Normalize-ForCompare {
    # Defensive normalization to avoid false-positive drift on benign
    # formatting differences. Mirror of normalize_for_compare in drift-check.sh.
    #   1. Strip UTF-8 BOM
    #   2. Convert CRLF/CR to LF
    #   3. Strip trailing blank lines
    #   4. Strip trailing markdown thematic breaks (---, ***, ___)
    #   5. Re-add a single trailing newline
    param([string]$Text)

    if ($Text.Length -gt 0 -and [int]$Text[0] -eq 0xFEFF) {
        $Text = $Text.Substring(1)
    }
    $Text = $Text -replace "`r`n", "`n"
    $Text = $Text -replace "`r", "`n"

    $lines = [System.Collections.ArrayList]@($Text -split "`n")
    $thematicBreaks = @("---", "***", "___")
    while ($lines.Count -gt 0) {
        $last = $lines[$lines.Count - 1]
        if ([string]::IsNullOrWhiteSpace($last) -or $thematicBreaks -contains $last.Trim()) {
            $lines.RemoveAt($lines.Count - 1)
        } else {
            break
        }
    }
    return ($lines -join "`n") + "`n"
}

# ── Scope A - installed skills ────────────────────────────────────────────
$SkillsRepoDir = Join-Path $StarterDir "skills"
if (Test-Path -LiteralPath $SkillsRepoDir) {
    Get-ChildItem -LiteralPath $SkillsRepoDir -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        $skillName = $_.Name
        $installedSkillDir = Join-Path $InstallDir $skillName

        # If skill isn't installed, that's a missing-install (bootstrap's job),
        # not drift. Skip.
        if (-not (Test-Path -LiteralPath $installedSkillDir)) { return }

        Get-ChildItem -LiteralPath $_.FullName -File -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
            $srcFile = $_.FullName
            # Compute path relative to skills/<skill>/, then join with installedSkillDir
            $relPath = $srcFile.Substring($SkillsRepoDir.Length + 1)
            $relWithinSkill = $relPath.Substring($skillName.Length + 1)
            $destFile = Join-Path $installedSkillDir $relWithinSkill

            if ((Test-Path -LiteralPath $destFile) -and -not (Test-FilesIdentical $srcFile $destFile)) {
                Add-Drift -Scope "skill" -InstalledPath $destFile -RepoSourcePath $srcFile
            }
        }
    }
}

# ── Scope B - vault-installed scripts ─────────────────────────────────────
if ($Vault -and (Test-Path -LiteralPath $Vault)) {
    $VaultScriptsDir = Join-Path $Vault "⚙️ Meta\scripts"
    if (Test-Path -LiteralPath $VaultScriptsDir) {
        # Curated list - must match drift-check.sh
        $VaultScriptNames = @(
            "aggregate-sessions.py",
            "aggregate-decisions.py",
            "auto-wikilink.py",
            "build-journal-index.py",
            "graphify_dedupe_by_adjacency.py",
            "graph-context-hook.sh"
        )

        foreach ($name in $VaultScriptNames) {
            $src  = Join-Path $StarterDir "scripts\$name"
            $dest = Join-Path $VaultScriptsDir $name
            if (-not ((Test-Path -LiteralPath $src) -and (Test-Path -LiteralPath $dest))) { continue }
            if (-not (Test-FilesIdentical $src $dest)) {
                $note = ""
                if ($name -eq "graph-context-hook.sh") {
                    $note = "hand-edited CONFIG block at top of file - cherry-pick changes, do NOT overwrite wholesale"
                }
                Add-Drift -Scope "vault-script" -InstalledPath $dest -RepoSourcePath $src -Note $note
            }
        }
    }
}

# ── Scope C - vault CLAUDE.md rule blocks ─────────────────────────────────
if ($Vault) {
    $VaultClaudeMd = Join-Path $Vault "CLAUDE.md"
    $RulesDir = Join-Path $StarterDir "templates\rules"
    if ((Test-Path -LiteralPath $VaultClaudeMd) -and (Test-Path -LiteralPath $RulesDir)) {
        $vaultClaudeContent = Get-Content -LiteralPath $VaultClaudeMd -Encoding UTF8

        Get-ChildItem -LiteralPath $RulesDir -Filter "*.md" -File -ErrorAction SilentlyContinue | ForEach-Object {
            $ruleFile = $_.FullName

            # First H1 line of the template
            $heading = (Get-Content -LiteralPath $ruleFile -Encoding UTF8 | Where-Object { $_.StartsWith("# ") } | Select-Object -First 1)
            if (-not $heading) { return }

            # Heading must appear verbatim in the user's CLAUDE.md
            if (-not ($vaultClaudeContent -contains $heading)) { return }

            # Extract the installed block and compare to the template content,
            # normalizing both sides for BOM, line endings, trailing blanks,
            # and thematic-break separators.
            $installedBlock = Get-MarkdownBlock -FilePath $VaultClaudeMd -Heading $heading
            $templateRaw = Get-Content -LiteralPath $ruleFile -Encoding UTF8 -Raw

            $installedNormalized = Normalize-ForCompare $installedBlock
            $templateNormalized = Normalize-ForCompare $templateRaw

            if ($installedNormalized -ne $templateNormalized) {
                Add-Drift -Scope "vault-rule" -InstalledPath $VaultClaudeMd -RepoSourcePath $ruleFile -Note "block heading: $heading"
            }
        }
    }
}

# ── Output ─────────────────────────────────────────────────────────────────
Write-Output "STATUS: OK"
Write-Output "DRIFT_COUNT: $($DriftLines.Count)"
Write-Output "---DRIFT_FILES---"
foreach ($line in $DriftLines) {
    Write-Output $line
}
Write-Output "---END---"

# Record cooldown
Set-Content -LiteralPath $CooldownFile -Value $Today -Encoding UTF8

exit 0
