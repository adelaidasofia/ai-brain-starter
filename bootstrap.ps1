# ai-brain-starter, one-command bootstrap (Windows)
#
# This script installs everything Phase 0 of the setup-brain skill installs.
# It runs in two modes:
#   - Inside Claude Code (from the README paste-flow): Claude invokes this
#     as part of end-to-end setup and continues into the interview after.
#   - Standalone (run directly from PowerShell after cloning the repo): tools
#     get installed, then the Next-Steps block tells the user to open Claude
#     Code and paste the setup prompt. Detection is via $env:CLAUDE_CODE_ENTRYPOINT.
#
# Usage (clone the repo first, then run the local script; do not curl-pipe).
# Run from PowerShell, NOT cmd.exe:
#     git clone https://github.com/mycelium-hq/ai-brain-starter "$env:USERPROFILE\.claude\skills\ai-brain-starter"
#     powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\.claude\skills\ai-brain-starter\bootstrap.ps1"
#
# Dry run (preview changes without making them):
#     powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\.claude\skills\ai-brain-starter\bootstrap.ps1" -DryRun
#
# SAFETY GUARANTEES, same as bootstrap.sh:
#   - Existing settings.json/.mcp.json keys preserved (setdefault never overwrites)
#   - Local uncommitted changes to ai-brain-starter clone are stashed before pull
#   - DIVERGENT forks of ai-brain-starter (commits on both sides) are skipped
#   - Sub-skill folders with their own .git/ are detected as YOUR FORK and skipped
#   - Symlinked sub-skill folders are detected and skipped (warns)
#   - Custom skills outside the bundled set (humanizer, anything you
#     installed yourself) are NEVER touched
#   - Your vault CLAUDE.md is NEVER touched
#   - Every file modification creates a .bak-YYYY-MM-DD-HHMM backup
#   - Final summary lists every change made
#
# Safe to re-run. Skips anything already installed.

param([switch]$DryRun, [string]$Profile = "")

$ErrorActionPreference = "Stop"
$RepoUrl = "https://github.com/mycelium-hq/ai-brain-starter.git"
$SkillDir = "$env:USERPROFILE\.claude\skills\ai-brain-starter"
$Failed    = @()
$Installed = @()
$Updated   = @()
$Skipped   = @()
$Backups   = @()
$Cleaned   = @()

function Hdr($msg)  { Write-Host ""; Write-Host $msg -ForegroundColor White -BackgroundColor DarkBlue }
function Log($msg)  { Write-Host "  · $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  + $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }
function Err($msg)  { Write-Host "  X $msg" -ForegroundColor Red; $script:Failed += $msg }
function Dry($msg)  { Write-Host "  [dry-run] $msg" -ForegroundColor Magenta }
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# Is this process running elevated (Administrator)?
# Decides per-MACHINE vs per-USER for every runtime we install. A per-machine
# MSI run from a normal shell needs a UAC consent it can never obtain under
# /quiet: msiexec aborts with 1925 ("insufficient privileges ... for all users
# of the machine") or 1603, and Start-Process -Wait without -PassThru THREW
# THAT AWAY. Net effect on a real Windows install: Node "installed" silently,
# `Have node` was false, and the only surviving signal was a bare
# "node install failed" with no reason. Cached once - elevation cannot change
# mid-run.
$script:IsElevated = $null
function Test-Elevated {
    if ($null -eq $script:IsElevated) {
        try {
            $id = [Security.Principal.WindowsIdentity]::GetCurrent()
            $script:IsElevated = ([Security.Principal.WindowsPrincipal]$id).IsInRole(
                [Security.Principal.WindowsBuiltInRole]::Administrator)
        } catch {
            # Non-Windows PowerShell (pwsh on macOS/Linux) has no WindowsIdentity.
            # Treat as unelevated: the user-scoped path works everywhere.
            $script:IsElevated = $false
        }
    }
    return $script:IsElevated
}

# Run an installer and return its REAL exit code (0 when it never launched is a
# lie, so a launch failure returns a sentinel). Start-Process -Wait alone
# discards the exit status entirely - the bug this closes.
# -PassThru WITHOUT -Wait, then WaitForExit() on the object we hold. `-Wait
# -PassThru` also reports exit codes correctly (both forms verified against a
# script exiting 0/1/3/42), but holding the object and waiting on it ourselves
# keeps the handle open explicitly, and the $null guard below is what actually
# matters: an exit code we could not read must never come back as 0. Returning
# a false 0 here would rebuild, one layer up, the exact silent-success bug this
# function exists to remove.
function Invoke-Installer([string]$FilePath, [string[]]$ArgumentList) {
    try {
        $p = Start-Process -PassThru -FilePath $FilePath -ArgumentList $ArgumentList
        if ($null -eq $p) { return -1 }
        $p.WaitForExit()
        # A null ExitCode means we do not know what happened. That is the exact
        # state this function exists to stop reading as success.
        if ($null -eq $p.ExitCode) { return -1 }
        return $p.ExitCode
    } catch {
        Warn "could not launch ${FilePath}: $_"
        return -1
    }
}

# Prepend a directory to the PERSISTED user PATH (and this process's PATH).
# Used by the user-scoped runtime installs, which unpack somewhere the machine
# PATH will never mention. Idempotent: re-running never duplicates the entry.
#
# Known tradeoff: SetEnvironmentVariable rewrites the USER Path as a plain
# string, so a `%VAR%` inside it is stored expanded. Accepted deliberately -
# `setx` truncates at 1024 characters, which silently DESTROYS a long PATH, and
# that is the worse failure. The machine PATH, where expandable entries
# actually live, is never touched here.
function Add-UserPathEntry([string]$Dir) {
    $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    if ($null -eq $userPath) { $userPath = "" }
    $already = $userPath -split ';' | Where-Object { $_.TrimEnd('\') -ieq $Dir.TrimEnd('\') }
    if (-not $already) {
        $newPath = if ($userPath.Trim()) { "$Dir;$userPath" } else { $Dir }
        [System.Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    }
    if (-not (($env:Path -split ';') | Where-Object { $_.TrimEnd('\') -ieq $Dir.TrimEnd('\') })) {
        $env:Path = "$Dir;$env:Path"
    }
}

# Run a native command without letting benign stderr kill the install.
# Under $ErrorActionPreference = "Stop", a native command that writes ANY
# warning to stderr (pip's routine "not on PATH" note, npm progress, git
# hints) can surface as a terminating NativeCommandError even though the
# command SUCCEEDED - a real Windows install died mid-run on exactly that,
# at `pip install pipx`. Success is the exit code, never stderr.
# Returns $true when the command exited 0.
function Run-Native([scriptblock]$Block) {
    $eapSaved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $global:LASTEXITCODE = 0
    try {
        & $Block 2>&1 | ForEach-Object { Write-Host "    $_" }
    } catch {
        # e.g. CommandNotFound: the native command never ran, so LASTEXITCODE
        # would still hold the initial 0 - force failure so callers see it.
        Write-Host "    $_" -ForegroundColor Yellow
        $global:LASTEXITCODE = 1
    } finally {
        $ErrorActionPreference = $eapSaved
    }
    return ($LASTEXITCODE -eq 0)
}

function Backup-File($path) {
    if (-not (Test-Path $path)) { return }
    $bak = "$path.bak-$(Get-Date -Format 'yyyy-MM-dd-HHmm')"
    if ($DryRun) { Dry "would back up: $path -> $bak" }
    else { Copy-Item $path $bak; $script:Backups += $bak }
}

# ─── Locale detection + bilingual helper ──────────────────────────────────────
# Override via $env:BOOTSTRAP_LANG = "es"|"en". Otherwise: LC_ALL > LANG > Get-Culture > en.
function Detect-Lang {
    $raw = $env:BOOTSTRAP_LANG
    if (-not $raw) { $raw = $env:LC_ALL }
    if (-not $raw) { $raw = $env:LANG }
    if (-not $raw) { try { $raw = (Get-Culture).Name } catch { $raw = "en-US" } }
    if ($raw.Substring(0, [Math]::Min(2, $raw.Length)) -eq "es") { return "es" }
    return "en"
}
$script:LangCode = Detect-Lang
function T([string]$en, [string]$es) {
    if ($script:LangCode -eq "es") { return $es } else { return $en }
}

# ─── Corporate / hardened install profile ─────────────────────────────────────
# Mirrors bootstrap.sh --profile corporate. Enabled via `-Profile corporate` or
# $env:CORPORATE_PROFILE = "1": minimal named plugin set, no external-egress MCPs,
# telemetry OFF, versions pinned, user-space only. Full spec + manifest:
# docs/CORPORATE_PROFILE.md.
$CorporateProfile = ($Profile -eq "corporate") -or ($env:CORPORATE_PROFILE -eq "1")
if ($Profile -and $Profile -ne "corporate") {
    Warn "Unknown -Profile '$Profile' (expected: corporate). Ignoring."
}
if ($CorporateProfile) {
    $env:CORPORATE_PROFILE = "1"   # so embedded `python -` children can read it
    $env:EMAIL_GATE_BYPASS = "1"   # no email mint / no quick-mint network call
    $env:MYCELIUM_NO_PING  = "1"   # no install-ping to myceliumai.co
    Write-Host ""
    Write-Host "=== CORPORATE / HARDENED PROFILE ACTIVE ===" -ForegroundColor Yellow
    Write-Host "  No external-egress MCPs | telemetry OFF | versions pinned | user-space only." -ForegroundColor Yellow
    Write-Host "  Reviewable component manifest emitted at the end. Full spec: docs/CORPORATE_PROFILE.md" -ForegroundColor Yellow
}

# ─── Optional signup (the install never blocks on it) ──────────────────────
$emailMarker = "$env:USERPROFILE\.claude\.ai-brain-starter-email-on-file"
# Canonical host: alternate domains 308-redirect and POST bodies are not
# reliably re-sent on redirect by older PowerShell. Always call canonical.
$installApiBase = if ($env:MYCELIUM_INSTALL_API) { $env:MYCELIUM_INSTALL_API } else { "https://mycelium-ai.co" }

# Optional signup. This block only runs when the user already provided an
# email - a web-form token or EMAIL/NAME env vars. With nothing provided it
# is skipped and the install proceeds; the setup interview makes one optional
# ask at the end. The install never blocks on signup.
if ($env:EMAIL_GATE_BYPASS -ne "1" -and -not $DryRun -and -not (Test-Path $emailMarker) -and ($env:TOKEN -or ($env:EMAIL -and $env:NAME))) {
    Hdr (T "Signup" "Registro")

    # Inline path: EMAIL+NAME passed as env vars (typically by Claude Code
    # after asking the user inline). POST to quick-mint to get a token
    # without making the user leave the chat.
    if (-not $env:TOKEN -and $env:EMAIL -and $env:NAME) {
        $qmLang = if ($env:LANG_HINT) { $env:LANG_HINT } else { "en" }
        if ($qmLang -ne "en" -and $qmLang -ne "es") { $qmLang = "en" }
        $qmOs = "windows"
        Log (T "Minting install token for $($env:EMAIL) via $installApiBase..." `
              "Generando token para $($env:EMAIL) en $installApiBase...")
        try {
            $qmBody = @{
                email = $env:EMAIL
                name = $env:NAME
                lang = $qmLang
                os = $qmOs
                consentRequired = $true
            } | ConvertTo-Json -Compress
            $qmResp = Invoke-RestMethod -Uri "$installApiBase/api/install/quick-mint" `
                -Method Post -ContentType "application/json" -Body $qmBody -TimeoutSec 12 -ErrorAction Stop
            if ($qmResp.ok -and $qmResp.token -match '^[a-f0-9]{32}$') {
                Ok (T "Token minted inline. No browser needed." `
                      "Token generado en línea. Sin navegador.")
                $env:TOKEN = $qmResp.token
            } else {
                Err (T "Inline mint returned no token. Falling back." `
                      "Mint inline no devolvió token. Caemos al formulario.")
            }
        } catch {
            Err (T "Inline mint failed: $_. Falling back." `
                  "Falló mint inline: $_. Caemos al formulario.")
        }
    }

    # If a token was provided (web-form path) or minted inline above,
    # validate it. On ANY failure here, warn and continue tokenless. The
    # install must never abort over an optional signup.
    $tk = if ($env:TOKEN) { $env:TOKEN.ToLower().Trim() } else { "" }
    if ($tk -and $tk -notmatch '^[a-f0-9]{32}$') {
        Warn (T "Token shape invalid - continuing without it." `
               "Formato de token inválido - seguimos sin él.")
        $tk = ""
    }
    if ($tk) {
        Log (T "Validating token against $installApiBase..." `
              "Validando token contra $installApiBase...")
        try {
            $verifyUri = "$installApiBase/api/install/verify?token=$tk"
            $resp = Invoke-RestMethod -Uri $verifyUri -Method Get -TimeoutSec 10 -ErrorAction Stop
            if ($resp.valid -ne $true) {
                Warn (T "Token did not validate - continuing without it." `
                       "El token no validó - seguimos sin él.")
                $tk = ""
            }
        } catch {
            Warn (T "Token validation request failed - continuing without it." `
                   "Falló la solicitud de validación - seguimos sin él.")
            $tk = ""
        }
    }
    if ($tk) {
        Ok (T "Token valid. Recording email-on-file marker." `
              "Token válido. Guardando marca de email-en-archivo.")
        New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.claude" | Out-Null
        Set-Content -Path $emailMarker -Value $tk -Encoding ASCII

        # Fetch recap for setup-brain Phase 1 pre-population
        try {
            $recapUri = "$installApiBase/api/install/recap?token=$tk"
            $recap = Invoke-WebRequest -Uri $recapUri -Method Get -TimeoutSec 8 -UseBasicParsing -ErrorAction Stop
            if ($recap.StatusCode -eq 200) {
                $recapPath = "$env:USERPROFILE\.claude\.ai-brain-starter-recap.json"
                Set-Content -Path $recapPath -Value $recap.Content -Encoding UTF8
                Ok (T "Recap cached for setup-brain Phase 1." `
                      "Recap guardado para Phase 1 de setup-brain.")
            }
        } catch {
            # Best-effort. Setup-brain Phase 1 falls back to asking the questions.
        }

        # Fire install_bootstrap_started event (best-effort)
        try {
            $os = "$([System.Environment]::OSVersion.VersionString) $env:PROCESSOR_ARCHITECTURE"
            $body = @{ token = $tk; os = $os } | ConvertTo-Json -Compress
            Invoke-RestMethod -Uri "$installApiBase/api/install/started" -Method Post `
                -Body $body -ContentType "application/json" -TimeoutSec 6 -ErrorAction SilentlyContinue | Out-Null
        } catch {
            # Best-effort. Funnel telemetry, not a hard dependency.
        }
    }
}

# ─── Pre-flight gate (skip with $env:PREFLIGHT_BYPASS = "1") ──────────────────
$scriptRoot = Split-Path -Parent $PSCommandPath
$preflightLocal = Join-Path $scriptRoot "scripts\preflight.ps1"
$preflightInstalled = "$env:USERPROFILE\.claude\skills\ai-brain-starter\scripts\preflight.ps1"
$preflightToRun = ""
if (Test-Path $preflightLocal) { $preflightToRun = $preflightLocal }
elseif (Test-Path $preflightInstalled) { $preflightToRun = $preflightInstalled }

if ($env:PREFLIGHT_BYPASS -ne "1" -and $preflightToRun -and -not $DryRun) {
    Hdr (T "Pre-flight check" "Verificación previa")
    Log (T "Verifying every prerequisite before any tool is installed." `
          "Verificando cada requisito antes de instalar nada.")
    & $preflightToRun
    $preflightRc = $LASTEXITCODE
    if ($preflightRc -eq 2) {
        Write-Host ""
        Write-Host (T "Bootstrap aborted: pre-flight found blockers. Fix them and re-run." `
                       "Bootstrap detenido: la verificación previa encontró bloqueantes. Arreglalos y volvé a correr.") `
                       -ForegroundColor Red
        Write-Host (T "To bypass during development: `$env:PREFLIGHT_BYPASS = '1'; pwsh bootstrap.ps1" `
                       "Para saltarla en desarrollo: `$env:PREFLIGHT_BYPASS = '1'; pwsh bootstrap.ps1")
        exit 2
    }
}

Hdr "Cleaning up deprecated tools"

# claude-mem: unauthenticated local HTTP API, arbitrary file-read surface,
# API keys in plaintext, hook that injected content into every session.
$settingsPath = "$env:USERPROFILE\.claude\settings.json"
$claudeMemPresent = $false
if (Test-Path $settingsPath) {
    try {
        $s = Get-Content $settingsPath -Raw | ConvertFrom-Json
        $hasMkt  = $s.extraKnownMarketplaces -and $s.extraKnownMarketplaces.PSObject.Properties.Name -contains "thedotmack"
        $hasPlug = $s.enabledPlugins -and $s.enabledPlugins.PSObject.Properties.Name -contains "claude-mem@thedotmack"
        $claudeMemPresent = $hasMkt -or $hasPlug
    } catch {}
}
if ($claudeMemPresent) {
    if ($DryRun) { Dry "would remove claude-mem from settings.json (marketplace + plugin entry)" }
    else {
        Backup-File $settingsPath
        $s = Get-Content $settingsPath -Raw | ConvertFrom-Json
        if ($s.extraKnownMarketplaces) { $s.extraKnownMarketplaces.PSObject.Properties.Remove("thedotmack") }
        if ($s.enabledPlugins)         { $s.enabledPlugins.PSObject.Properties.Remove("claude-mem@thedotmack") }
        $s | ConvertTo-Json -Depth 10 | Set-Content $settingsPath
        Ok "Removed claude-mem, had security issues (open local HTTP port, file-read surface). Built-in memory covers everything it did."
        $script:Cleaned += "claude-mem"
    }
} else { Ok "claude-mem not present, nothing to clean" }

# notebooklm: browser automation + Google login dance wasn't worth it for most users.
$notebooklmDir = "$env:USERPROFILE\.claude\skills\notebooklm"
if (Test-Path $notebooklmDir) {
    if ($DryRun) { Dry "would remove $notebooklmDir (notebooklm skill)" }
    else {
        Remove-Item -Recurse -Force $notebooklmDir
        Ok "Removed notebooklm, rarely used, required browser automation + Google login on every session. To restore: git clone https://github.com/PleasePrompto/notebooklm-skill.git `$env:USERPROFILE\.claude\skills\notebooklm"
        $script:Cleaned += "notebooklm"
    }
} else { Ok "notebooklm not present, nothing to clean" }

Hdr (T "ai-brain-starter, one-command install" "ai-brain-starter, instalación de un solo comando")
Write-Host ""
if ($DryRun) {
    Write-Host ("  " + (T "DRY RUN MODE - showing what would be installed without making any changes." `
                            "MODO DE PRUEBA - mostrando lo que se instalaría sin hacer cambios reales.")) -ForegroundColor Magenta
    Write-Host ""
}
Write-Host ("  " + (T "This installs the full AI brain stack: graphify, humanizer," `
                       "Esto instala el stack completo de AI brain: graphify, humanizer,"))
Write-Host ("  " + (T "meeting-todos, patterns, insights, deconstruct, daily-journal, rise," `
                       "meeting-todos, patterns, insights, deconstruct, daily-journal, rise,"))
Write-Host ("  " + (T "repurpose-talk, nano-banana (skill docs), Granola + ChatPRD MCPs," `
                       "repurpose-talk, nano-banana (docs de skill), MCPs de Granola + ChatPRD,"))
Write-Host ("  " + (T "the obsidian-skills marketplace, plus the ai-brain-starter skill" `
                       "el marketplace obsidian-skills, y la skill ai-brain-starter"))
Write-Host ("  " + (T "itself. Takes ~5 minutes the first time." `
                       "misma. Tarda ~5 minutos la primera vez."))
Write-Host ""
Write-Host ("  " + (T "When it's done, Claude continues with the setup interview automatically." `
                       "Cuando termine, Claude continúa con la entrevista de setup automáticamente."))
Write-Host ("  " + (T "You don't need to type anything." "No necesitás tipear nada."))
Write-Host ""
Start-Sleep -Seconds 1

# ─── winget bootstrap ─────────────────────────────────────────────────────────
# winget ships with Windows 11 and recent Windows 10. On older Windows 10 it's
# missing, auto-install App Installer (which provides winget) before doing
# anything else. Never abort with "go install something from the Microsoft
# Store yourself", that defeats the one-command promise.
if (-not (Have winget) -and $DryRun) {
    Dry "would: install winget (App Installer) from the Microsoft Store package"
} elseif (-not (Have winget)) {
    Hdr (T "Installing winget (App Installer)" "Instalando winget (App Installer)")
    Log (T "winget is the Windows package manager we use to install everything else." `
          "winget es el gestor de paquetes de Windows que usamos para instalar todo lo demás.")
    Log (T "Your Windows version is missing it, we'll install it for you now." `
          "Tu versión de Windows no lo tiene, lo instalamos por vos ahora.")

    # Method 1: Microsoft's official MSIX bundle. URL aka.ms/getwinget always
    # resolves to the latest stable release on GitHub.
    $tempInstaller = "$env:TEMP\AppInstaller.msixbundle"
    try {
        Log (T "Downloading App Installer from Microsoft..." "Descargando App Installer de Microsoft...")
        Invoke-WebRequest -Uri "https://aka.ms/getwinget" -OutFile $tempInstaller -UseBasicParsing
        Log (T "Installing... (a Windows install dialog may appear briefly)" `
              "Instalando... (puede aparecer brevemente un diálogo de instalación de Windows)")
        Add-AppxPackage -Path $tempInstaller -ErrorAction Stop
        Remove-Item $tempInstaller -Force -ErrorAction SilentlyContinue
    } catch {
        Warn (T "Auto-install of winget failed: $_" "Falló la auto-instalación de winget: $_")
        Warn (T "Falling back to direct MSI installs for Python/Node/Obsidian." `
               "Volviendo a instalaciones MSI directas para Python/Node/Obsidian.")
    }

    # Refresh PATH so winget is callable in this session
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
}

if (Have winget) {
    Ok "winget available"
    $UseWinget = $true
} else {
    # Final fallback, winget could not be installed. Use direct downloads for
    # the things we need. The user is on a very old Windows; mark $UseWinget so
    # later sections can branch.
    Warn "Continuing without winget, using direct installer downloads."
    $UseWinget = $false
}

# ─── Python 3.10+ ─────────────────────────────────────────────────────────────
$pythonOk = $false
try {
    $v = (python --version 2>&1) -replace 'Python ',''
    if ([version]$v -ge [version]"3.10") { $pythonOk = $true }
} catch {}
if (-not $pythonOk -and $CorporateProfile) {
    Warn (T "Corporate profile: Python 3.10+ not found - NOT auto-installing (user-space, pinned-version policy)." `
            "Perfil corporativo: no se encontro Python 3.10+ - NO se instala automaticamente (espacio de usuario, version fija).")
    Warn (T "Provision Python via your IT-approved channel, then re-run. Steps that need python will be skipped." `
            "Instala Python por tu canal aprobado de IT y volve a correr. Los pasos que necesitan python se omitiran.")
} elseif (-not $pythonOk -and $DryRun) {
    Dry "would: winget install Python.Python.3.12 (or direct download fallback)"
} elseif (-not $pythonOk) {
    Hdr "Installing Python 3.12"
    if ($UseWinget) {
        winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    } else {
        Log "winget unavailable, downloading Python installer directly."
        $pyInstaller = "$env:TEMP\python-installer.exe"
        try {
            Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" -OutFile $pyInstaller -UseBasicParsing
            # InstallAllUsers=1 is a per-machine install: it needs elevation,
            # and under /quiet there is no UAC prompt to grant it, so it just
            # fails. python.org's installer supports a per-user install with
            # InstallAllUsers=0 and no privileges at all - use it when this
            # shell is not already elevated.
            $perUser = -not (Test-Elevated)
            if ($perUser) {
                Log (T "Not running as Administrator - installing Python for this user only." `
                       "No estas como Administrador - instalando Python solo para este usuario.")
            }
            $pyArgs = @("/quiet", "PrependPath=1", "Include_pip=1",
                        $(if ($perUser) { "InstallAllUsers=0" } else { "InstallAllUsers=1" }))
            $code = Invoke-Installer $pyInstaller $pyArgs
            Remove-Item $pyInstaller -Force -ErrorAction SilentlyContinue
            # 0 = installed, 3010 = installed, reboot pending. Anything else is
            # a failure that used to be discarded silently.
            if ($code -ne 0 -and $code -ne 3010) {
                Err (T "Python installer exited $code (nothing was installed)." `
                       "El instalador de Python salio con $code (no se instalo nada).")
                if ($code -eq 1602 -or $code -eq 1603 -or $code -eq -1) {
                    Warn (T "That is usually a denied elevation prompt. Re-run this script from an Administrator PowerShell, or install Python yourself from python.org." `
                            "Normalmente es un prompt de elevacion denegado. Volve a correr este script desde un PowerShell como Administrador, o instala Python vos mismo desde python.org.")
                }
            }
        } catch {
            Err "Python install failed: $_"
        }
    }
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
}
if (Have python) { Ok "python $(python --version)" } else { Err "python install failed" }

# ─── Node.js ──────────────────────────────────────────────────────────────────
if (-not (Have node) -and $CorporateProfile) {
    Warn (T "Corporate profile: Node.js not found - NOT auto-installing (user-space, pinned-version policy)." `
            "Perfil corporativo: no se encontro Node.js - NO se instala automaticamente (espacio de usuario, version fija).")
    Warn (T "Provision Node.js via your IT-approved channel, then re-run. Steps that need node will be skipped." `
            "Instala Node.js por tu canal aprobado de IT y volve a correr. Los pasos que necesitan node se omitiran.")
    Warn (T "The exact command for IT to approve/run: winget install -e --id OpenJS.NodeJS.LTS" `
            "El comando exacto para que IT apruebe/corra: winget install -e --id OpenJS.NodeJS.LTS")
} elseif (-not (Have node) -and $DryRun) {
    Dry "would: winget install OpenJS.NodeJS.LTS (or direct download fallback)"
} elseif (-not (Have node)) {
    Hdr "Installing Node.js"
    $nodeVersion = "v20.18.0"
    $wingetTried = $false
    $userScopedNode = $false
    if ($UseWinget) {
        $wingetTried = $true
        winget install -e --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
        # winget's Node package is per-machine too, so an unelevated run hits
        # the same denied-elevation wall. Before this, a winget failure ended
        # the attempt: no fallback ran, and the only trace was "node install
        # failed" three lines later. Now it falls through to the direct path.
        if ($LASTEXITCODE -ne 0) {
            Warn (T "winget could not install Node.js (exit $LASTEXITCODE) - falling back to a direct download." `
                    "winget no pudo instalar Node.js (salio $LASTEXITCODE) - probando con descarga directa.")
        }
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    }
    if (-not (Have node)) {
        if ($wingetTried) { Log "Downloading Node.js LTS directly." }
        else { Log "winget unavailable, downloading Node.js LTS directly." }
        try {
            if (Test-Elevated) {
                # Elevated: the per-machine MSI is the right install, and the
                # UAC consent this needs is already granted.
                $nodeInstaller = "$env:TEMP\node-installer.msi"
                Invoke-WebRequest -Uri "https://nodejs.org/dist/$nodeVersion/node-$nodeVersion-x64.msi" -OutFile $nodeInstaller -UseBasicParsing
                $code = Invoke-Installer "msiexec" @("/i", "`"$nodeInstaller`"", "/quiet", "/norestart")
                Remove-Item $nodeInstaller -Force -ErrorAction SilentlyContinue
                if ($code -ne 0 -and $code -ne 3010) {
                    Err (T "Node.js MSI exited $code (nothing was installed)." `
                           "El MSI de Node.js salio con $code (no se instalo nada).")
                }
            } else {
                # NOT elevated. The MSI is per-machine and CANNOT be granted
                # elevation under /quiet - it aborts with 1925/1603, and that
                # exit code used to be thrown away, so the install looked like
                # it worked. The official ZIP build needs no privileges at all:
                # unpack it under LOCALAPPDATA and put it on the USER PATH.
                Log (T "Not running as Administrator - installing Node.js for this user only (no UAC needed)." `
                       "No estas como Administrador - instalando Node.js solo para este usuario (sin UAC).")
                $nodeZip     = "$env:TEMP\node-$nodeVersion-win-x64.zip"
                $stagingDir  = "$env:TEMP\node-unpack-$([System.Guid]::NewGuid().ToString('N'))"
                $nodeHome    = "$env:LOCALAPPDATA\nodejs"
                Invoke-WebRequest -Uri "https://nodejs.org/dist/$nodeVersion/node-$nodeVersion-win-x64.zip" -OutFile $nodeZip -UseBasicParsing
                Expand-Archive -LiteralPath $nodeZip -DestinationPath $stagingDir -Force
                $unpacked = Join-Path $stagingDir "node-$nodeVersion-win-x64"
                if (-not (Test-Path (Join-Path $unpacked "node.exe"))) {
                    throw "unpacked archive has no node.exe at $unpacked"
                }
                # Move an existing folder aside rather than deleting it. We only
                # get here because node is not on PATH, so whatever is there is
                # broken or stale - but it is still the user's, and a silent
                # recursive delete of a folder under LOCALAPPDATA is not ours to
                # make. Same .bak-<stamp> convention the rest of the install uses.
                if (Test-Path $nodeHome) {
                    $shelved = "$nodeHome.bak-$(Get-Date -Format 'yyyy-MM-dd-HHmm')"
                    Move-Item -LiteralPath $nodeHome -Destination $shelved -ErrorAction SilentlyContinue
                    if (Test-Path $nodeHome) { throw "could not move the existing $nodeHome aside" }
                    Warn (T "Moved a previous $nodeHome aside to $shelved." `
                            "Se movio el $nodeHome anterior a $shelved.")
                }
                Move-Item -LiteralPath $unpacked -Destination $nodeHome
                $userScopedNode = $true
                Remove-Item $nodeZip -Force -ErrorAction SilentlyContinue
                Remove-Item $stagingDir -Recurse -Force -ErrorAction SilentlyContinue
                Add-UserPathEntry $nodeHome
                # npm's global prefix on Windows is %APPDATA%\npm. The MSI adds
                # that to PATH; a ZIP install does not, so `npm install -g
                # @anthropic-ai/claude-code` (the very next step) would succeed
                # and still leave `claude` unrunnable. Put it on PATH too.
                $npmGlobal = "$env:APPDATA\npm"
                if (-not (Test-Path $npmGlobal)) { New-Item -ItemType Directory -Path $npmGlobal -Force | Out-Null }
                Add-UserPathEntry $npmGlobal
                Ok (T "Node.js installed at $nodeHome (user-scoped)." `
                       "Node.js instalado en $nodeHome (solo para este usuario).")
            }
        } catch {
            Err "Node install failed: $_"
        }
    }
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    # A user-scoped install lives on the USER PATH, which the line above does
    # pick up - but only after Add-UserPathEntry persisted it. Re-assert the
    # in-process entries so this same run can call node/npm immediately.
    #
    # Gated on $userScopedNode, NOT on the folder existing: a LEFTOVER
    # %LOCALAPPDATA%\nodejs from an earlier run would otherwise be prepended
    # ahead of a per-machine install we just made, and the stale copy would win
    # every `node` call from here on.
    if ($userScopedNode) {
        Add-UserPathEntry "$env:LOCALAPPDATA\nodejs"
        Add-UserPathEntry "$env:APPDATA\npm"
    }
}
if (Have node) { Ok "node $(node --version)" } else { Err "node install failed" }

# ─── Claude Code (Anthropic's CLI/desktop app, REQUIRED) ─────────────────────
# Without this, the user has no way to actually run /setup-brain after the
# bootstrap finishes. Distributed via npm so the install path is identical
# across Mac, Linux, and Windows once Node is present.
if (-not (Have claude) -and $DryRun) {
    Dry "would: npm install -g @anthropic-ai/claude-code"
} elseif (-not (Have claude)) {
    Hdr (T "Installing Claude Code" "Instalando Claude Code")
    Log (T "Claude Code is Anthropic's developer tool that runs the AI brain skill." `
          "Claude Code es la herramienta de Anthropic para developers que corre la skill del AI brain.")
    Log (T "It's different from claude.ai (the chat website), this one lives in your" `
          "Es diferente de claude.ai (el sitio de chat); este vive en tu")
    Log (T "terminal and can read and write files in your vault. Installing via npm." `
          "terminal y puede leer y escribir archivos en tu vault. Instalando vía npm.")
    # EAP guard: npm routinely writes warnings to stderr, which PowerShell 5.1
    # turns into a terminating error under Stop even with 2>$null (verified).
    # Relax it just here so the $LASTEXITCODE check below decides success, not a
    # stray warning line. $LASTEXITCODE survives the restore (it's a var assign).
    $eapSaved = $ErrorActionPreference; $ErrorActionPreference = "SilentlyContinue"
    npm install -g @anthropic-ai/claude-code 2>$null
    $ErrorActionPreference = $eapSaved
    if ($LASTEXITCODE -ne 0) {
        Err (T "Claude Code install failed, install manually with: npm install -g @anthropic-ai/claude-code" `
              "Falló la instalación de Claude Code. Instalalo manual con: npm install -g @anthropic-ai/claude-code")
    }
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
}
if (Have claude) { Ok (T "Claude Code installed" "Claude Code instalado") }

# ─── pipx ─────────────────────────────────────────────────────────────────────
if (-not (Have pipx) -and $DryRun) {
    Dry "would: python -m pip install --user pipx"
} elseif (-not (Have pipx)) {
    Hdr "Installing pipx"
    # Run-Native: pip's routine "scripts not on PATH" stderr warning aborted a
    # real install here under EAP=Stop. Exit code decides success, not stderr.
    if (-not (Run-Native { python -m pip install --user pipx })) { Err "pip install pipx failed" }
    [void](Run-Native { python -m pipx ensurepath })
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (Have pipx) { Ok "pipx" } else { Err "pipx install failed" }

# ─── fastmcp ──────────────────────────────────────────────────────────────────
# Framework for building custom MCP servers in minimal Python. Needed when
# wiring custom connectors (CRM bridges, vault sync, investor relations, etc.)
if (-not (Have fastmcp) -and $DryRun) {
    Dry "would: pipx install fastmcp"
} elseif (-not (Have fastmcp)) {
    Hdr "Installing fastmcp"
    # EAP guard: pipx writes progress/warnings to stderr -> terminating error
    # under Stop in PS 5.1 even with 2>$null. Relaxed here; the Have-check below
    # is what decides success.
    $eapSaved = $ErrorActionPreference; $ErrorActionPreference = "SilentlyContinue"
    pipx install fastmcp 2>$null
    $ErrorActionPreference = $eapSaved
}
if (Have fastmcp) { Ok "fastmcp" } else { Warn "fastmcp not installed (non-blocking, install later with: pipx install fastmcp)" }

# ─── gh (GitHub CLI) ──────────────────────────────────────────────────────────
if (-not (Have gh) -and $DryRun) {
    Dry "would: winget install GitHub.cli"
} elseif (-not (Have gh)) {
    Hdr "Installing gh (GitHub CLI)"
    Log "gh lets the session-end capture cascade file improvement ideas as GitHub issues automatically."
    winget install -e --id GitHub.cli --accept-source-agreements --accept-package-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
}
if (Have gh) { Ok "gh installed" } else { Warn "gh not installed, install manually if needed" }

# gh authentication, required for the session-end capture cascade to file
# improvement ideas as GitHub issues automatically. Walk the user through it
# the first time only.
if (Have gh) {
    # Detect gh auth WITHOUT crashing. Under $ErrorActionPreference='Stop',
    # PowerShell 5.1 turns a native command's stderr into a terminating
    # NativeCommandError -- even with 2>$null -- so an unauthenticated
    # `gh auth status` (it writes "not logged in" to stderr and exits non-zero)
    # would kill the whole bootstrap on any fresh machine, interactive or not.
    # Guard every gh call with SilentlyContinue and read $LASTEXITCODE instead.
    $eapSaved = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    gh auth status 1>$null 2>$null
    $ghAuthed = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $eapSaved

    if (-not $ghAuthed) {
        # The browser login below needs a real terminal. Read-Host throws or
        # blocks when stdin isn't interactive (piped install, CI, or when Claude
        # Code runs this bootstrap as a subprocess), so only prompt when we can.
        if ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
            Hdr "GitHub login (OPTIONAL, skip with Ctrl+C)"
            Write-Host "  This step is OPTIONAL. You only need it if you want your AI brain to"
            Write-Host "  automatically file improvement ideas as GitHub issues for the maintainer."
            Write-Host ""
            Write-Host "  Do you have a GitHub account?"
            Write-Host "     YES      -> press Enter, a browser window opens, log in, done."
            Write-Host "     NO       -> press Ctrl+C right now to skip. Everything else still works."
            Write-Host "     NOT SURE -> press Ctrl+C to skip. You can come back later with: gh auth login"
            Write-Host ""
            Write-Host "  (If you press Enter, you'll see options like 'GitHub.com -> HTTPS ->"
            Write-Host "   Login with web browser.' Just pick those defaults, they're fine.)"
            Write-Host ""
            [void](Read-Host "  Press Enter to log in, or Ctrl+C to skip")
            $eapSaved = $ErrorActionPreference
            $ErrorActionPreference = "SilentlyContinue"
            gh auth login
            $ErrorActionPreference = $eapSaved
            if ($LASTEXITCODE -ne 0) { Warn "gh auth skipped or failed, run 'gh auth login' later if you want issue filing" }
        } else {
            Warn "Non-interactive shell: skipping optional GitHub login (run 'gh auth login' later to enable issue filing)"
        }

        # Re-check after a possible login attempt (still crash-guarded).
        $eapSaved = $ErrorActionPreference
        $ErrorActionPreference = "SilentlyContinue"
        gh auth status 1>$null 2>$null
        $ghAuthed = ($LASTEXITCODE -eq 0)
        $ErrorActionPreference = $eapSaved
    }

    if ($ghAuthed) { Ok "gh authenticated" }
    else { Warn "gh not authenticated (issue filing disabled until you run: gh auth login)" }
}

# ─── Obsidian, REQUIRED, the entire setup writes notes into an Obsidian vault.
# Auto-install via winget. Never ask the user to "go download" anything, that
# breaks the one-command promise and assumes they know what Obsidian is and
# how to install a desktop app on Windows.
$ObsidianInstalled = $false
$ObsidianPaths = @(
    "$env:LOCALAPPDATA\Programs\obsidian\Obsidian.exe",
    "$env:LOCALAPPDATA\Obsidian\Obsidian.exe",
    "$env:ProgramFiles\Obsidian\Obsidian.exe",
    "${env:ProgramFiles(x86)}\Obsidian\Obsidian.exe"
)
foreach ($p in $ObsidianPaths) {
    if (Test-Path -LiteralPath $p) { $ObsidianInstalled = $true; break }
}

if (-not $ObsidianInstalled -and $CorporateProfile) {
    Warn (T "Corporate profile: Obsidian not found - NOT auto-installing. Deploy your IT-approved, version-pinned build." `
            "Perfil corporativo: no se encontro Obsidian - NO se instala. Desplega tu build aprobado y con version fija por IT.")
} elseif (-not $ObsidianInstalled) {
    Hdr (T "Installing Obsidian" "Instalando Obsidian")
    Log (T "Obsidian is the note-taking app this whole setup writes into. Free, runs locally, no account." `
          "Obsidian es la app de notas en la que todo este setup escribe. Gratis, corre local, sin cuenta.")
    if ($DryRun) {
        Dry "would: install Obsidian via winget or direct download"
    } else {
        if ($UseWinget) {
            Log (T "Installing via winget so you don't have to download anything yourself." `
                  "Instalando vía winget para que no tengas que descargar nada manual.")
            winget install -e --id Obsidian.Obsidian --accept-source-agreements --accept-package-agreements
        } else {
            Log (T "winget unavailable, downloading Obsidian installer directly from obsidian.md." `
                  "winget no disponible, descargando instalador de Obsidian directamente desde obsidian.md.")
            $obsInstaller = "$env:TEMP\Obsidian-Installer.exe"
            try {
                # Resolve latest Windows installer from Obsidian's GitHub releases.
                $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/obsidianmd/obsidian-releases/releases/latest" -UseBasicParsing
                $url = ($rel.assets | Where-Object { $_.name -match 'Obsidian.*\.exe$' -and $_.name -notmatch 'arm64' } | Select-Object -First 1).browser_download_url
                if ($url) {
                    Invoke-WebRequest -Uri $url -OutFile $obsInstaller -UseBasicParsing
                    Start-Process -Wait -FilePath $obsInstaller -ArgumentList "/S"
                    Remove-Item $obsInstaller -Force -ErrorAction SilentlyContinue
                } else {
                    Err "Could not resolve latest Obsidian installer URL, download manually from https://obsidian.md/download"
                }
            } catch {
                Err "Obsidian install failed: $_, download manually from https://obsidian.md/download and re-run this script"
            }
        }
        if ($LASTEXITCODE -eq 0 -or -not $UseWinget) {
            $script:Installed += "Obsidian"
            foreach ($p in $ObsidianPaths) {
                if (Test-Path -LiteralPath $p) { $ObsidianInstalled = $true; break }
            }
        }
    }
}
if ($ObsidianInstalled) { Ok "Obsidian installed" }

# ─── graphify ─────────────────────────────────────────────────────────────────
if (-not (Have graphify) -and $DryRun) {
    Dry "would: pipx install graphifyy + graphify install --platform windows"
} elseif (-not (Have graphify)) {
    Hdr "Installing graphify (knowledge graph builder)"
    # Run-Native: pipx writes progress + PATH notes to stderr (EAP=Stop hazard).
    if (-not (Run-Native { pipx install graphifyy })) { Err "pipx install graphifyy failed" }
    if (Have graphify) { [void](Run-Native { graphify install --platform windows }) }
}
if (Have graphify) { Ok "graphify" } else { Err "graphify install failed" }

# ─── Clone or update ai-brain-starter ─────────────────────────────────────────
# SAFETY:
#   - Stashes local uncommitted changes before pulling
#   - Detects DIVERGENT history (your fork has commits not on origin/main)
#     and refuses to pull, so your fork is never silently overwritten
# Adopt-ArchiveInstall DIR REPOURL - turn a directory that holds this repo's
# files but no .git (the archive entry path, for a machine with no git) into a
# real clone, so the self-update path below works on every later run. Returns
# $true on success. A $false is NON-FATAL to the caller: archive content is
# complete and functional, and only auto-update depends on this.
#
# NEVER destroys local edits. A bare `reset --hard` would silently discard a
# hand-patched archive install, so the tree is staged and diffed against
# origin/main first; anything that differs is copied aside under the same
# .bak-<stamp> convention this installer already uses for Node.
function Adopt-ArchiveInstall {
    param([string]$Dir, [string]$RepoUrl)
    # git writes progress/notices to stderr, which PowerShell 5.1 turns into a
    # terminating error under Stop even with 2>$null. Same guard the self-update
    # section below uses; $LASTEXITCODE checks are unaffected.
    $eapAdopt = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    Push-Location $Dir
    try {
        Remove-Item -LiteralPath "$Dir\.adopt-backup-path" -Force -ErrorAction SilentlyContinue
        git init -q 2>$null;                       if ($LASTEXITCODE -ne 0) { return $false }
        git remote remove origin 2>$null | Out-Null
        git remote add origin $RepoUrl 2>$null;    if ($LASTEXITCODE -ne 0) { return $false }
        git fetch --quiet origin main 2>$null;     if ($LASTEXITCODE -ne 0) { return $false }
        git add -A 2>$null | Out-Null
        git diff --cached --quiet origin/main 2>$null
        if ($LASTEXITCODE -ne 0) {
            $shelved = "$Dir.bak-$(Get-Date -Format 'yyyy-MM-dd-HHmm')"
            Copy-Item -LiteralPath $Dir -Destination $shelved -Recurse -Force
            if (-not (Test-Path -LiteralPath $shelved)) { return $false }
            Set-Content -LiteralPath "$Dir\.adopt-backup-path" -Value $shelved
        }
        git reset --hard --quiet origin/main 2>$null; if ($LASTEXITCODE -ne 0) { return $false }
        git branch -q -M main 2>$null | Out-Null
        git branch -q --set-upstream-to=origin/main main 2>$null | Out-Null
        return $true
    } finally {
        Pop-Location
        $ErrorActionPreference = $eapAdopt
    }
}

Hdr "Installing the ai-brain-starter skill"
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.claude\skills" | Out-Null
if (Test-Path "$SkillDir\.git") {
    Log "Already installed - checking for updates..."
    Push-Location $SkillDir
    # EAP guard for this self-update section: git writes progress/notices to
    # stderr, which PowerShell 5.1 turns into a terminating error under Stop
    # (even with 2>$null). Relax it here; the $LASTEXITCODE checks below (e.g.
    # git diff --quiet) are unaffected. Restored right after Pop-Location.
    $eapSelfUpdate = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"

    if ($DryRun) { Dry "would: git fetch --quiet origin" }
    else { git fetch --quiet origin 2>$null }

    $ahead = (git rev-list --count "@{u}..HEAD" 2>$null)
    $behind = (git rev-list --count "HEAD..@{u}" 2>$null)
    if (-not $ahead) { $ahead = 0 }
    if (-not $behind) { $behind = 0 }
    $ahead = [int]$ahead
    $behind = [int]$behind

    if ($ahead -gt 0 -and $behind -gt 0) {
        Warn "DIVERGENT FORK DETECTED at $SkillDir"
        Warn "  Your local clone has $ahead commit(s) NOT on origin/main"
        Warn "  AND origin/main has $behind commit(s) NOT on your clone"
        Warn "  Refusing to pull. Your fork is preserved unchanged."
        Warn "  To merge manually: cd $SkillDir; git pull --rebase"
        $script:Skipped += "ai-brain-starter clone (divergent fork - manual merge required)"
    }
    elseif ($ahead -gt 0 -and $behind -eq 0) {
        Log "Your clone has $ahead local commit(s) and is otherwise current. Leaving as-is."
        $script:Skipped += "ai-brain-starter clone (local commits, up to date)"
    }
    elseif ($behind -gt 0) {
        git diff --quiet --ignore-submodules HEAD 2>$null
        if ($LASTEXITCODE -ne 0) {
            $stashMsg = "bootstrap auto-stash $(Get-Date -Format 'yyyy-MM-dd-HHmm')"
            Log "Detected local uncommitted changes - stashing as: $stashMsg"
            Log "Recover later with: cd $SkillDir; git stash list; git stash pop"
            if (-not $DryRun) {
                git stash push -u -m $stashMsg 2>$null | Out-Null
                if ($LASTEXITCODE -eq 0) {
                    # The checkout is now pristine upstream with the user's patches
                    # removed. sync-vault-scripts.ps1 refuses to run while this is
                    # set, so the vault keeps its patched scripts instead of having
                    # the UNPATCHED upstream copies written over them (the reported
                    # session-close-runner.sh / vault-safe-commit.sh regression).
                    $env:ABS_CLONE_PATCHES_STASHED = $stashMsg
                    Warn "Your vault's <meta>/scripts/ will NOT be refreshed this run - this"
                    Warn "  clone no longer holds your patches, and syncing would overwrite the"
                    Warn "  patched copies in your vault. Restore and sync when ready:"
                    Warn "    cd $SkillDir; git stash pop; pwsh scripts/sync-vault-scripts.ps1"
                }
            }
        }
        if ($DryRun) { Dry "would: git pull --quiet (fast-forward $behind commit(s))" }
        else { git pull --quiet 2>$null }
        $script:Updated += "ai-brain-starter clone (pulled $behind commit(s))"
    }
    else {
        Log "ai-brain-starter clone is up to date"
    }
    Pop-Location
    $ErrorActionPreference = $eapSelfUpdate
} elseif (Test-Path "$SkillDir\bootstrap.ps1") {
    # The directory holds this repo's files but has no .git: the entry command
    # fetched a zip because git was unavailable. `git clone` into a non-empty
    # directory FAILS, and that failure used to land in the red actionable list -
    # trading a missing-git block for a scary-looking one. Adopt instead.
    if (-not (Have git)) {
        Warn (T "Installed from an archive, and git isn't available yet - automatic updates stay off until git is installed." `
                "Instalado desde un archivo, y git todavia no esta disponible - las actualizaciones automaticas quedan apagadas hasta instalar git.")
        $script:Skipped += "ai-brain-starter clone (archive install, git unavailable)"
    }
    elseif ($DryRun) { Dry "would: adopt $SkillDir as a git clone of $RepoUrl" }
    elseif (Adopt-ArchiveInstall -Dir $SkillDir -RepoUrl $RepoUrl) {
        if (Test-Path "$SkillDir\.adopt-backup-path") {
            Warn (T "Local changes were found in the archive install. A copy is at:" `
                    "Se encontraron cambios locales en la instalacion por archivo. Hay una copia en:")
            Warn ("  " + (Get-Content -LiteralPath "$SkillDir\.adopt-backup-path" -ErrorAction SilentlyContinue))
        }
        $script:Installed += "ai-brain-starter clone (adopted from archive install)"
    }
    else {
        # Non-fatal: the archive content is complete and functional. Only
        # auto-update needs the git wiring, so this is a Warn, never an Err.
        Warn (T "Couldn't reconnect the archive install to the repository - automatic updates stay off. Everything else works." `
                "No se pudo reconectar la instalacion por archivo al repositorio - las actualizaciones automaticas quedan apagadas. Todo lo demas funciona.")
        $script:Skipped += "ai-brain-starter clone (archive install, adopt failed)"
    }
} else {
    if ($DryRun) { Dry "would: git clone $RepoUrl -> $SkillDir" }
    elseif (-not (Run-Native { git clone --quiet $RepoUrl $SkillDir })) { Err "ai-brain-starter clone failed (git exit $LASTEXITCODE)" }
    $script:Installed += "ai-brain-starter clone"
}
if ((Test-Path "$SkillDir\SKILL.md") -or $DryRun) { Ok "ai-brain-starter at $SkillDir" } else { Err "ai-brain-starter clone failed" }

# ─── Sub-skills (with comprehensive safety checks) ───────────────────────────
# SAFETY:
#   - If the destination has its own .git/, treat it as YOUR FORK and skip
#   - If the destination is a SYMLINK, warn and skip
#   - Otherwise: file-by-file sync with backup-before-overwrite
#   - Custom skill folders outside the bundled set (humanizer, daily-journal,
#     anything the user installed themselves) are NEVER touched
Hdr "Installing bundled sub-skills (with safety checks)"
$stamp = Get-Date -Format "yyyy-MM-dd-HHmm"

foreach ($sub in @("graphify", "cierre-de-llamada", "meeting-todos", "patterns", "insights", "deconstruct", "daily-journal", "rise", "repurpose-talk", "nano-banana", "second-brain-mapping", "setup-vault-types", "diagnose", "note-todos", "sunday-review", "coach", "coaching", "backfill-journal-body-context", "longitudinal", "resolver-query", "for-my-team", "health-context", "health-doctor", "health-setup", "ingest-github", "ingest-health", "ingest-youtube", "evolve", "instinct-export", "instinct-import", "interview-me", "doubt-driven-development", "secret-warn")) {
    $src = "$SkillDir\skills\$sub"
    $dst = "$env:USERPROFILE\.claude\skills\$sub"

    if (-not (Test-Path $src)) {
        Err "$sub skill source missing in repo"
        continue
    }

    # Symlink detection (PowerShell: ReparsePoint attribute)
    if ((Test-Path $dst) -and ((Get-Item $dst -ErrorAction SilentlyContinue).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        $target = (Get-Item $dst).Target
        Warn "$sub is a SYMLINK to $target - bootstrap will NOT write through it"
        Warn "  If you want bootstrap to update this skill, replace the symlink with a regular folder."
        $script:Skipped += "$sub skill (symlink to $target)"
        continue
    }

    # Fork detection (.git inside the destination)
    if (Test-Path "$dst\.git") {
        Log "$sub has its own .git directory - detected as YOUR FORK, skipping entirely"
        Log "  Your fork is preserved untouched. You manage updates to it yourself."
        $script:Skipped += "$sub skill (your own fork - has .git)"
        continue
    }

    # Regular folder or missing, eligible for sync
    if ($DryRun) {
        Dry "would sync $sub skill from $src to $dst (with backup-before-overwrite)"
        continue
    }

    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    $createdCount = 0
    $updatedCount = 0
    $backedUpCount = 0
    Get-ChildItem -Recurse -File $src | ForEach-Object {
        $rel = $_.FullName.Substring($src.Length + 1)
        $dstFile = Join-Path $dst $rel
        $dstParent = Split-Path $dstFile -Parent
        if (-not (Test-Path $dstParent)) { New-Item -ItemType Directory -Force -Path $dstParent | Out-Null }
        if (Test-Path $dstFile) {
            if ((Get-FileHash $_.FullName).Hash -ne (Get-FileHash $dstFile).Hash) {
                Copy-Item $dstFile "$dstFile.bak-$stamp"
                $script:Backups += "$dstFile.bak-$stamp"
                Copy-Item -Force $_.FullName $dstFile
                $backedUpCount++
                $updatedCount++
            }
        } else {
            Copy-Item -Force $_.FullName $dstFile
            $createdCount++
        }
    }
    if ($createdCount -gt 0 -or $updatedCount -gt 0) {
        Ok "${sub}: $createdCount new, $updatedCount updated, $backedUpCount backed up"
        $script:Updated += "$sub skill ($createdCount new, $updatedCount updated, $backedUpCount backed up)"
    } else {
        Ok "${sub}: already current"
    }
}

# ─── Humanizer ────────────────────────────────────────────────────────────────
$humDir = "$env:USERPROFILE\.claude\skills\humanizer"
if (-not (Test-Path $humDir) -and $DryRun) {
    Dry "would: git clone humanizer -> $humDir"
} elseif (-not (Test-Path $humDir)) {
    Hdr "Installing humanizer"
    [void](Run-Native { git clone --quiet https://github.com/adelaidasofia/humanizer.git $humDir })
}
if (Test-Path $humDir) { Ok "humanizer skill installed" } else { Err "humanizer clone failed" }

# Trust-prompt heads-up: registering MCP servers + a plugin marketplace triggers
# Claude Code's built-in trust prompt. Pre-frame it so non-technical installers
# don't panic. Pairs with phase-00-install.md Step 0.0b.
Write-Host ""
Write-Host ("  " + (T "HEADS UP: Claude Code may pause to ask you to approve these tools." "AVISO: Claude Code puede frenar en un momento para pedirte que apruebes estas herramientas."))
Write-Host ("  " + (T "That prompt is its normal safety check for anything not from Anthropic." "Ese aviso es su chequeo de seguridad normal para cualquier cosa que no viene de Anthropic."))
Write-Host ("  " + (T "It is expected for third-party tools. What gets added is itemized in the README - the choice is yours." "Es esperado con herramientas de terceros. El README detalla todo lo que se agrega - la decision es tuya."))
Write-Host ""

# ─── Granola MCP ─────────────────────────────────────────────────────────────
# SAFETY: backup .mcp.json before editing. Existing MCP servers (custom
# integrations, other URL or stdio MCPs the user wired themselves) are
# preserved, setdefault() only adds the granola entry if missing.
Hdr "Registering MCPs (Granola + ChatPRD)"
if ($CorporateProfile) {
    Warn (T "Corporate profile: skipping external-egress MCPs (granola -> granola.ai, chatprd -> chatprd.ai)." `
            "Perfil corporativo: se omiten MCPs con egreso externo (granola -> granola.ai, chatprd -> chatprd.ai).")
    Log (T "  Enable opt-in after security review - see docs/CORPORATE_PROFILE.md." `
          "  Habilitalos opt-in tras revision de seguridad - ver docs/CORPORATE_PROFILE.md.")
} else {
$mcpPath = "$env:USERPROFILE\.claude\.mcp.json"
Backup-File $mcpPath

if ($DryRun) {
    Dry "would: register granola + chatprd MCPs (existing entries preserved)"
} else {
    $pyMcp = @"
import json, os
p = os.path.expanduser('~/.claude/.mcp.json')
try:
    with open(p) as f: m = json.load(f)
except FileNotFoundError:
    m = {'mcpServers': {}}
m.setdefault('mcpServers', {})
if 'granola' not in m['mcpServers']:
    m['mcpServers']['granola'] = {'type': 'url', 'url': 'https://mcp.granola.ai/mcp'}
if 'chatprd' not in m['mcpServers']:
    m['mcpServers']['chatprd'] = {'type': 'url', 'url': 'https://app.chatprd.ai/mcp'}
with open(p, 'w') as f: json.dump(m, f, indent=2)
"@
    $pyMcp | python -
    if ($LASTEXITCODE -eq 0) { Ok "MCPs registered: granola, chatprd (Granola needs an account to use)" } else { Err "MCP registration failed" }
}
}  # end corporate-profile MCP gate

# ─── Marketplaces + enabled plugins (settings.json) ──────────────────────────
# SAFETY: backup settings.json first. setdefault() never clobbers existing
# marketplaces, plugins, permissions, env vars, or any other keys.
Hdr "Registering marketplace + enabling plugins"
$settingsPath = "$env:USERPROFILE\.claude\settings.json"
Backup-File $settingsPath

if ($DryRun) {
    if ($CorporateProfile) {
        Dry "would register obsidian-skills marketplace and enable: obsidian, context7 (playwright EXCLUDED - browser automation)"
        Dry "would ENFORCE telemetry-off + pin env in settings.json (DISABLE_TELEMETRY, DISABLE_ERROR_REPORTING, DISABLE_FEEDBACK_COMMAND, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC, DISABLE_AUTOUPDATER, MYCELIUM_NO_PING)"
    } else {
        Dry "would register obsidian-skills marketplace (kepano/obsidian-skills) and enable: obsidian, context7, playwright"
    }
} else {
    $pyPlugins = @"
import json, os
p = os.path.expanduser('~/.claude/settings.json')
corporate = os.environ.get('CORPORATE_PROFILE') == '1'
try:
    with open(p) as f: s = json.load(f)
except FileNotFoundError:
    s = {}
s.setdefault('extraKnownMarketplaces', {})
if 'obsidian-skills' not in s['extraKnownMarketplaces']:
    s['extraKnownMarketplaces']['obsidian-skills'] = {
        'source': {'source': 'github', 'repo': 'kepano/obsidian-skills'}
    }
s.setdefault('enabledPlugins', {})
plugins = ['obsidian@obsidian-skills', 'context7']
if not corporate:
    plugins.append('playwright')  # browser automation - out of the hardened minimal set
for plug in plugins:
    s['enabledPlugins'].setdefault(plug, True)
if corporate:
    env = s.setdefault('env', {})
    for k, v in (('DISABLE_TELEMETRY','1'),('DISABLE_ERROR_REPORTING','1'),('DISABLE_FEEDBACK_COMMAND','1'),('CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC','1'),('DISABLE_AUTOUPDATER','1'),('MYCELIUM_NO_PING','1')):
        env[k] = v
with open(p, 'w') as f: json.dump(s, f, indent=2)
"@
    $pyPlugins | python -
    if ($LASTEXITCODE -eq 0) { Ok "Marketplace + plugins registered (settings.json backed up)" } else { Err "settings.json plugin registration failed" }
}

# ─── Verification ────────────────────────────────────────────────────────────
Hdr "Verifying installation"
if ($DryRun) {
    Log "skipping verification under -DryRun (nothing was actually installed)"
} else {
foreach ($pair in @(@("graphify","graphify"), @("node","node"), @("npm","npm"), @("pipx","pipx"), @("gh","gh"))) {
    if (Have $pair[1]) { Ok $pair[0] } else { Err "$($pair[0]) not callable" }
}
foreach ($sub in @("graphify","cierre-de-llamada","meeting-todos","patterns","insights","deconstruct","daily-journal","rise","repurpose-talk","nano-banana","humanizer","ai-brain-starter","diagnose","second-brain-mapping","setup-vault-types","note-todos","sunday-review","coach","coaching","backfill-journal-body-context","longitudinal","resolver-query","for-my-team","health-context","health-doctor","health-setup","ingest-github","ingest-health","ingest-youtube","evolve","instinct-export","instinct-import","interview-me","doubt-driven-development","secret-warn")) {
    if (Test-Path "$env:USERPROFILE\.claude\skills\$sub") { Ok "skill: $sub" } else { Err "skill missing: $sub" }
}
if (Test-Path "$env:USERPROFILE\.claude\skills\graphify\scripts") { Ok "graphify scripts" } else { Err "graphify scripts missing" }

$mcpContent = Get-Content "$env:USERPROFILE\.claude\.mcp.json" -ErrorAction SilentlyContinue
if ($mcpContent -match "granola") { Ok "granola MCP in .mcp.json" } else { Err "granola not in .mcp.json" }
if ($mcpContent -match "chatprd") { Ok "chatprd MCP in .mcp.json" } else { Err "chatprd not in .mcp.json" }

if ((Get-Content "$env:USERPROFILE\.claude\settings.json" -ErrorAction SilentlyContinue) -match "obsidian-skills") {
    Ok "obsidian-skills marketplace in settings.json"
} else { Warn "obsidian-skills marketplace not in settings.json (non-blocking)" }
}  # end: if (-not $DryRun)

Write-Host ""
if ($Failed.Count -eq 0) {
    Write-Host ("━━━ " + (T "All checks passed." "Todas las verificaciones pasaron.") + " ━━━") -ForegroundColor Green
} else {
    Write-Host ("━━━ $($Failed.Count) " + (T "check(s) failed:" "verificación(es) fallaron:") + " ━━━") -ForegroundColor Red
    foreach ($f in $Failed) { Write-Host "  - $f" }
    Write-Host ""
    Write-Host (T "Don't proceed silently - fix these before continuing the setup interview." `
                  "No sigas en silencio: arreglá esto antes de continuar con la entrevista de setup.")
}

# ─── Change summary ──────────────────────────────────────────────────────────
Hdr "Change summary"
if ($DryRun) { Write-Host "DRY RUN - no actual changes made." -ForegroundColor Magenta }

if ($Installed.Count -eq 0 -and $Updated.Count -eq 0 -and $Skipped.Count -eq 0 -and $Backups.Count -eq 0 -and $Cleaned.Count -eq 0) {
    Write-Host "  Nothing to report - your setup was already current."
} else {
    if ($Installed.Count -gt 0) {
        Write-Host ""
        Write-Host "  Installed (new):" -ForegroundColor Green
        foreach ($x in $Installed) { Write-Host "    + $x" }
    }
    if ($Updated.Count -gt 0) {
        Write-Host ""
        Write-Host "  Updated:" -ForegroundColor Cyan
        foreach ($x in $Updated) { Write-Host "    ^ $x" }
    }
    if ($Skipped.Count -gt 0) {
        Write-Host ""
        Write-Host "  Skipped (your customizations preserved):" -ForegroundColor Yellow
        foreach ($x in $Skipped) { Write-Host "    o $x" }
    }
    if ($Backups.Count -gt 0) {
        Write-Host ""
        Write-Host "  Backups created (recoverable):" -ForegroundColor Yellow
        foreach ($x in $Backups) { Write-Host "    > $x" }
        Write-Host ""
        Write-Host "  To restore any backup: Move-Item <file>.bak-YYYY-MM-DD-HHMM <file>"
    }
    if ($Cleaned.Count -gt 0) {
        Write-Host ""
        Write-Host "  Removed (deprecated):" -ForegroundColor Red
        foreach ($x in $Cleaned) { Write-Host "    X $x" }
    }
}
Write-Host ""

# ─── User-level hook install (closes #6 - fires universally inside worktrees) ────────
# Parity with bootstrap.sh:1593-1605. Without this block, every hook in
# hooks.json (incl. inject-meeting-workflow-on-trigger.py) ships to disk
# but nothing ever wires it into $env:USERPROFILE\.claude\settings.json.
# The "I just had a meeting" trigger silently produces nothing for Windows
# users. Caught by adversarial audit 2026-05-27 before the 30x-team install.

$userHookInstaller = "$SkillDir\scripts\install-hooks-user-level.py"
if ((Test-Path $userHookInstaller) -and -not $DryRun) {
    Write-Host ""
    Write-Host ("━━━ " + (T "Installing hooks at user level (so they fire inside worktrees)" `
                              "Instalando hooks a nivel de usuario (para que disparen dentro de worktrees)") + " ━━━") -ForegroundColor Cyan

    # py first: the launcher is always real. A bare `python3`/`python` on PATH
    # can be the Microsoft Store alias STUB (opens the Store instead of running),
    # so every candidate is validated by actually executing it.
    $pythonCmd = $null
    foreach ($candidate in @("py", "python", "python3")) {
        $resolved = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $resolved) { continue }
        & $resolved.Source -c "import sys" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $pythonCmd = $resolved.Source; break }
    }

    if (-not $pythonCmd) {
        Write-Host ("  ! " + (T "Python 3 not on PATH - skipping user-level hook install." `
                                "Python 3 no esta en el PATH - saltando la instalacion de hooks.")) -ForegroundColor Yellow
        Write-Host ("  ! " + (T "Install Python 3 (microsoft.com/python or python.org), then re-run this bootstrap." `
                                "Instalá Python 3 (microsoft.com/python o python.org), luego volvé a correr este bootstrap.")) -ForegroundColor Yellow
        $Failed += "user-level hook install (missing python3) - meeting trigger + 6 other hooks WILL NOT FIRE until resolved"
    } else {
        try {
            # --fail-on-missing (parity with bootstrap.sh): verifies every wired
            # hook script exists on disk and escalates divergent-fork strands.
            & $pythonCmd $userHookInstaller --quiet --fail-on-missing
            if ($LASTEXITCODE -eq 0) {
                Write-Host ("  OK " + (T "User-level hooks installed (~/.claude/settings.json)" `
                                          "Hooks a nivel de usuario instalados (~/.claude/settings.json)")) -ForegroundColor Green
            } else {
                Write-Host ("  X " + (T "User-level hook install FAILED (exit $LASTEXITCODE)." `
                                         "La instalación de hooks a nivel de usuario FALLÓ (exit $LASTEXITCODE).")) -ForegroundColor Red
                Write-Host ("  X " + (T "The meeting trigger + 6 other UserPromptSubmit hooks will not fire." `
                                         "El trigger de meetings + 6 hooks UserPromptSubmit no van a disparar.")) -ForegroundColor Red
                Write-Host ("  X " + (T "Re-run manually: & `"$pythonCmd`" `"$userHookInstaller`" --fail-on-missing" `
                                         "Volvé a correr manualmente: & `"$pythonCmd`" `"$userHookInstaller`" --fail-on-missing")) -ForegroundColor Red
                $Failed += "user-level hook install (exit $LASTEXITCODE) - meeting trigger + 6 other hooks WILL NOT FIRE"
            }
        } catch {
            Write-Host ("  X " + (T "User-level hook install threw: $_" `
                                     "La instalación de hooks tiró: $_")) -ForegroundColor Red
            $Failed += "user-level hook install (exception) - meeting trigger + 6 other hooks WILL NOT FIRE"
        }
    }
    Write-Host ""
} elseif ($DryRun) {
    Write-Host ("  > [dry-run] would run: python $userHookInstaller --quiet --fail-on-missing  " + `
                  "# wires 7 UserPromptSubmit hooks incl. meeting-workflow trigger") -ForegroundColor DarkGray
}

# Deploy vault-side scripts (journal-preflight.py, aggregators, etc.) into the
# vault's Meta/scripts. Windows parity with the Mac update flow
# (sync-skills.py -> sync-vault-scripts.sh). Self-resolves the vault from the
# settings.json hooks just installed. Non-fatal: never blocks the install.
if (-not $DryRun) {
    try {
        $vaultSync = Join-Path $PSScriptRoot "scripts/sync-vault-scripts.ps1"
        if (Test-Path $vaultSync) { & $vaultSync -Quiet }
    } catch {
        Write-Host ("  ! " + (T "vault-script sync skipped: $_" `
                                 "sincronizacion de scripts del vault omitida: $_")) -ForegroundColor DarkYellow
    }
}

Write-Host ""
Write-Host ("━━━ " + (T "Install complete" "Instalación completa") + " ━━━") -ForegroundColor Cyan
Write-Host ""

# ─── Report install completion to Mycelium (best-effort, fail-open) ──────────
if ((Test-Path $emailMarker) -and -not $DryRun -and -not $CorporateProfile) {
    try {
        $recordedToken = (Get-Content -Path $emailMarker -TotalCount 1).Trim()
        # Only a real 32-char hex token is a funnel token. The marker may
        # instead hold "declined" or "recorded"; in those cases send nothing
        # (a declined user's machine must not ping the server).
        if ($recordedToken -match '^[a-f0-9]{32}$') {
            $os = "$([System.Environment]::OSVersion.VersionString) $env:PROCESSOR_ARCHITECTURE"
            $sigPath = "$env:USERPROFILE\.claude\.ai-brain-starter-hmac-secret"
            $signature = $null
            if (Test-Path $sigPath) {
                try {
                    $secret = (Get-Content -Path $sigPath -TotalCount 1).Trim()
                    if ($secret) {
                        $hmac = [System.Security.Cryptography.HMACSHA256]::new(
                            [System.Text.Encoding]::UTF8.GetBytes($secret))
                        $hash = $hmac.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($recordedToken))
                        $signature = ($hash | ForEach-Object { $_.ToString("x2") }) -join ""
                        $hmac.Dispose()
                    }
                } catch {
                    # Skip signing on any error; server still accepts unsigned during rollout.
                }
            }
            $payload = @{ token = $recordedToken; os = $os; completed = $true }
            if ($signature) { $payload.signature = $signature }
            $body = $payload | ConvertTo-Json -Compress
            Invoke-RestMethod -Uri "$installApiBase/api/install/complete" -Method Post `
                -Body $body -ContentType "application/json" -TimeoutSec 8 -ErrorAction SilentlyContinue | Out-Null
        }
    } catch {
        # Best-effort. Failing to report shouldn't block the user.
    }
}

if ($env:CLAUDE_CODE_ENTRYPOINT) {
    # Running inside Claude Code (the paste-flow from the README). Claude will
    # continue with the setup interview automatically; no user action needed.
    Write-Host ("  " + (T "Tools are ready. Your setup interview starts now: the first question" `
                            "Las herramientas están listas. Tu entrevista de setup arranca ahora: la primera"))
    Write-Host ("  " + (T "is the next thing you'll see, no commands to type and no folders to open." `
                            "pregunta es lo próximo que vas a ver, sin comandos que tipear ni carpetas que abrir."))
    Write-Host ""
} else {
    # Running standalone (irm-to-iex from PowerShell). Guide the user into the
    # paste-flow inside the Claude Code desktop app.
    Write-Host ("  " + (T "Tools are ready. Now open the Claude Code desktop app and paste this" `
                            "Las herramientas están listas. Ahora abrí la app de escritorio de Claude Code y pegá esto"))
    Write-Host ("  " + (T "into the chat to run the setup interview:" `
                            "en el chat para correr la entrevista de setup:"))
    Write-Host ""
    if ($script:LangCode -eq "es") {
        Write-Host "      Por favor configurá mi AI Brain Starter completo en esta sesión."
        Write-Host "      La skill ai-brain-starter ya está instalada en"
        Write-Host "      ~/.claude/skills/ai-brain-starter. Empezá la entrevista de setup"
        Write-Host "      corriendo la skill setup-brain y guiame por cada fase sin parar."
    } else {
        Write-Host "      Please set up my AI Brain Starter end-to-end in this session. The"
        Write-Host "      ai-brain-starter skill is already installed at"
        Write-Host "      ~/.claude/skills/ai-brain-starter. Start the setup interview by"
        Write-Host "      running the setup-brain skill and walk me through every phase"
        Write-Host "      without stopping."
    }
    Write-Host ""
    Write-Host ("  " + (T "Claude will ask where your vault should live and build everything" `
                            "Claude te va a preguntar dónde vivirá tu vault y va a construir todo"))
    Write-Host ("  " + (T "around your answers. You don't need to type any other commands." `
                            "alrededor de tus respuestas. No necesitás tipear ningún otro comando."))
    Write-Host ""
}

# ─── Corporate / hardened profile: version-pin sentinels + reviewable manifest ──
# Emitted LAST so it is the final thing a security reviewer sees. Prints under
# -DryRun too (so `-Profile corporate -DryRun` is a no-change review).
if ($CorporateProfile) {
    try { $absRev = (git -C $SkillDir rev-parse --short HEAD 2>$null) } catch { $absRev = $null }
    if (-not $absRev) { $absRev = "unknown" }
    try { $ccVer = ((claude --version 2>$null) -split ' ')[0] } catch { $ccVer = $null }
    if (-not $ccVer) { $ccVer = "not-detected" }
    $manifestPath = "$env:USERPROFILE\.claude\.ai-brain-starter-corporate-manifest.md"

    if (-not $DryRun) {
        # Pin: short-circuit the self-update hook + pre-create the no-ping sentinel.
        New-Item -ItemType File -Force -Path "$env:USERPROFILE\.claude\.ai-brain-starter-pinned" -ErrorAction SilentlyContinue | Out-Null
        New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.mycelium" -ErrorAction SilentlyContinue | Out-Null
        New-Item -ItemType File -Force -Path "$env:USERPROFILE\.mycelium\onboarded-ai-brain-starter" -ErrorAction SilentlyContinue | Out-Null
    }

    $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $manifest = @"
# AI Brain Starter - Corporate Install Manifest
Generated: $stamp | Profile: corporate (hardened) | Host: Windows $([System.Environment]::OSVersion.Version) $env:PROCESSOR_ARCHITECTURE

## Pinned versions (no auto-update)
- ai-brain-starter skill : rev $absRev - https://github.com/mycelium-hq/ai-brain-starter
  Self-update hook DISABLED via sentinel ~/.claude/.ai-brain-starter-pinned (delete it to re-enable updates).
- Claude Code CLI        : $ccVer - autoupdater off (DISABLE_AUTOUPDATER=1 + CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1)
- Obsidian               : pin to your IT-approved build + disable in-app auto-update (see docs/CORPORATE_PROFILE.md)

## Installed Claude Code plugins (minimal named set)
- obsidian@obsidian-skills - source: github kepano/obsidian-skills
- context7                 - documentation lookup MCP

## First-party skills
- Bundled IN the pinned ai-brain-starter revision above (graphify, daily-journal, insights, patterns,
  meeting-todos, second-brain-mapping, ...). Ship in-repo - no per-skill network fetch.

## EXCLUDED by this profile (reason)
- Third-party marketplaces : sentry, stripe, cloudflare, claude-seo, superpowers, marketingskills (dev/marketing)
- playwright plugin        : browser automation - out of the hardened minimal set
- granola / chatprd MCPs   : external URL MCPs that egress conversation context off-machine
- Shell-execution Obsidian plugins (e.g. "Shell Commands", "Hider") : never installed/recommended -
  the abuse vector in the REF6598 / PHANTOMPULSE RAT campaign (Elastic Security Labs, Apr 2026)

## Telemetry / network (all OFF)
- EMAIL_GATE_BYPASS=1 (no email mint) | MYCELIUM_NO_PING=1 (no install ping)
- settings.json env enforced: DISABLE_TELEMETRY, DISABLE_ERROR_REPORTING, DISABLE_FEEDBACK_COMMAND,
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC, DISABLE_AUTOUPDATER, MYCELIUM_NO_PING

## Operator recommendations (manual - see docs/CORPORATE_PROFILE.md)
- Keep the vault OUTSIDE any cloud-synced folder (OneDrive / iCloud / Dropbox / Google Drive).
- Enable Obsidian Restricted Mode (no community plugins) for sensitive vaults.
- Review + approve this manifest before rollout. Re-run with -Profile corporate after each approved update.
"@

    Hdr "Corporate component manifest"
    Write-Host $manifest
    if (-not $DryRun) {
        try {
            Set-Content -Path $manifestPath -Value $manifest -Encoding UTF8
            Ok "manifest written: $manifestPath"
        } catch {
            Warn "could not write manifest to $manifestPath"
        }
    } else {
        Dry "would write manifest to $manifestPath"
    }
}
