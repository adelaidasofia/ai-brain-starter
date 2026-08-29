#!/usr/bin/env bash
# exit-contract: ENFORCING

# preflight.sh — verify every prerequisite BEFORE bootstrap touches the machine.
#
# Bilingual (English / Español, locale-detected via $LANG and macOS AppleLocale).
# Returns 0 GREEN / 1 YELLOW / 2 RED.
#
# Usage:
#   bash scripts/preflight.sh              # human-readable terminal report
#   bash scripts/preflight.sh --json       # machine-readable JSON for bootstrap
#   bash scripts/preflight.sh --quiet      # only print the final status line
#
# bootstrap.sh runs this first and refuses to proceed on RED.
# Bypass for development: PREFLIGHT_BYPASS=1 bash bootstrap.sh

set -uo pipefail

GREEN_COUNT=0
YELLOW_COUNT=0
RED_COUNT=0
# Set to 1 by any check whose fix needs an admin/IT actor, not the participant
# (missing git capability, no admin group). Drives print_it_request below,
# independent of RED/YELLOW: git is deliberately non-blocking (the install
# fetches without it — see bootstrap.sh's archive-entry path), but a
# participant who hits it still needs the SAME copy-pasteable ask.
IT_HELP_NEEDED=0
GREEN_LINES=()
YELLOW_LINES=()
RED_LINES=()
INFO_LINES=()
JSON_MODE=0
QUIET_MODE=0

for arg in "$@"; do
  case "$arg" in
    --json) JSON_MODE=1 ;;
    --quiet|-q) QUIET_MODE=1 ;;
  esac
done

# ─── Locale detection ─────────────────────────────────────────────────────────
# Override via PREFLIGHT_LANG=es|en for testing or explicit caller intent.
# Otherwise: $LC_ALL > $LANG > macOS AppleLocale > en.
detect_lang() {
  local raw="${PREFLIGHT_LANG:-${LC_ALL:-${LANG:-}}}"
  if [[ -z "$raw" && "$(uname -s)" == "Darwin" ]]; then
    raw="$(defaults read -g AppleLocale 2>/dev/null || true)"
  fi
  [[ -z "$raw" ]] && raw="en_US"
  [[ "${raw:0:2}" == "es" ]] && echo "es" || echo "en"
}
LANG_CODE="$(detect_lang)"

# Translation helper — t "english text" "spanish text"
t() { [[ "$LANG_CODE" == "es" ]] && echo "$2" || echo "$1"; }

# ─── Color helpers (suppressed in JSON / quiet mode) ──────────────────────────
if [[ $JSON_MODE -eq 1 || $QUIET_MODE -eq 1 ]]; then
  C_RED=""; C_YEL=""; C_GRN=""; C_DIM=""; C_BLD=""; C_RST=""
else
  C_RED=$'\033[31m'; C_YEL=$'\033[33m'; C_GRN=$'\033[32m'
  C_DIM=$'\033[2m'; C_BLD=$'\033[1m'; C_RST=$'\033[0m'
fi

red()    { RED_LINES+=("$1"); RED_COUNT=$((RED_COUNT+1)); [[ $JSON_MODE -eq 0 && $QUIET_MODE -eq 0 ]] && printf "  ${C_RED}✗${C_RST} %s\n" "$1"; }
yellow() { YELLOW_LINES+=("$1"); YELLOW_COUNT=$((YELLOW_COUNT+1)); [[ $JSON_MODE -eq 0 && $QUIET_MODE -eq 0 ]] && printf "  ${C_YEL}!${C_RST} %s\n" "$1"; }
green()  { GREEN_LINES+=("$1"); GREEN_COUNT=$((GREEN_COUNT+1)); [[ $JSON_MODE -eq 0 && $QUIET_MODE -eq 0 ]] && printf "  ${C_GRN}✓${C_RST} %s\n" "$1"; }
info()   { INFO_LINES+=("$1"); [[ $JSON_MODE -eq 0 && $QUIET_MODE -eq 0 ]] && printf "  ${C_DIM}·${C_RST} %s\n" "$1"; }
section() { [[ $JSON_MODE -eq 0 && $QUIET_MODE -eq 0 ]] && printf "\n${C_BLD}%s${C_RST}\n" "$1"; }

have() { command -v "$1" >/dev/null 2>&1; }

# run_with_timeout SECS CMD [ARGS...] — portable timeout wrapper, mirrored
# from bootstrap.sh's helper of the same name (this script runs standalone,
# before bootstrap.sh is even fetched, so it cannot source it).
run_with_timeout() {
  local secs="$1"; shift
  if have timeout; then timeout "$secs" "$@"; return $?; fi
  if have gtimeout; then gtimeout "$secs" "$@"; return $?; fi
  "$@" & local pid=$! elapsed=0
  while [[ $elapsed -lt $secs ]] && kill -0 "$pid" 2>/dev/null; do sleep 1; elapsed=$((elapsed + 1)); done
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null; sleep 1; kill -KILL "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null || true; return 124
  fi
  wait "$pid"
}

# have_working_git — PRESENCE IS NOT CAPABILITY on macOS: with the Command
# Line Tools absent, /usr/bin/git exists and is on PATH (`have git` is true),
# but running it raises the CLT install GUI dialog and hangs. Probe by
# EXECUTION, bounded, so a stub that opens a GUI cannot hang this report.
# Mirrors bootstrap.sh's identically-named probe.
have_working_git() { have git || return 1; run_with_timeout 15 git --version >/dev/null 2>&1; }

# print_it_request — one copy-pasteable, bilingual block naming exactly what
# to ask IT for: the packages, the repo URL, and why. EN + ES TOGETHER
# (not routed through t()) because the cohort is bilingual and the person
# reading it may not be the person who ran this check. Voice matches
# bootstrap.sh's own "exact request for IT" line (~1050).
print_it_request() {
  cat <<EOF

━━━ $(t "Copy-paste request for your IT team" "Pedido para copiar y enviar a tu equipo de IT") ━━━

EN — please install, for the AI Brain Starter workshop:
  - Git (macOS: Xcode Command Line Tools, "xcode-select --install"; Linux: the distro's git package)
  - Homebrew (macOS package manager, https://brew.sh) — or local admin rights to run its installer
  - Python 3.10 or newer
  - Node.js 18 or newer
  Repo: https://github.com/mycelium-hq/ai-brain-starter
  Why: standard open-source developer tools to run a local AI assistant (Claude Code) against a
  private notes vault. Nothing installs system-wide beyond Git/Homebrew; everything else installs
  into this user's own home directory.

ES — por favor instalar, para el taller de AI Brain Starter:
  - Git (macOS: Xcode Command Line Tools, "xcode-select --install"; Linux: el paquete git de la distro)
  - Homebrew (gestor de paquetes de macOS, https://brew.sh) — o permisos de administrador para correr su instalador
  - Python 3.10 o más reciente
  - Node.js 18 o más reciente
  Repo: https://github.com/mycelium-hq/ai-brain-starter
  Por qué: herramientas estándar de desarrollo open-source para correr un asistente de IA local
  (Claude Code) sobre un vault de notas privado. Nada se instala a nivel de sistema más allá de
  Git/Homebrew; el resto se instala en la carpeta de este usuario.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF
}

# ─── Header ───────────────────────────────────────────────────────────────────
if [[ $JSON_MODE -eq 0 && $QUIET_MODE -eq 0 ]]; then
  printf "\n${C_BLD}%s${C_RST}\n" "$(t \
    "AI Brain Starter — pre-flight check" \
    "AI Brain Starter — verificación previa")"
  printf "${C_DIM}%s${C_RST}\n" "$(t \
    "Verifying every prerequisite before any tool gets installed." \
    "Verificando cada requisito antes de instalar nada.")"
fi

# ─── 1. Operating system ──────────────────────────────────────────────────────
section "$(t "Operating system" "Sistema operativo")"

OS_KIND="$(uname -s)"
case "$OS_KIND" in
  Darwin)
    MACOS_VER="$(sw_vers -productVersion 2>/dev/null || echo unknown)"
    MACOS_MAJOR="${MACOS_VER%%.*}"
    if [[ "$MACOS_MAJOR" =~ ^[0-9]+$ && "$MACOS_MAJOR" -ge 11 ]]; then
      green "$(t "macOS $MACOS_VER (supported, ≥11 Big Sur)" "macOS $MACOS_VER (compatible, ≥11 Big Sur)")"
    elif [[ "$MACOS_MAJOR" =~ ^[0-9]+$ && "$MACOS_MAJOR" -ge 10 ]]; then
      yellow "$(t \
        "macOS $MACOS_VER is older than Big Sur. Most things install but Homebrew may warn." \
        "macOS $MACOS_VER es anterior a Big Sur. La mayoría se instala, pero Homebrew puede advertir.")"
    else
      red "$(t \
        "macOS $MACOS_VER is too old. Upgrade to Big Sur (11) or newer. https://support.apple.com/macos/upgrade" \
        "macOS $MACOS_VER es muy antiguo. Actualizá a Big Sur (11) o más reciente. https://support.apple.com/es-co/macos/upgrade")"
    fi
    ARCH="$(uname -m)"
    info "$(t "Architecture: $ARCH" "Arquitectura: $ARCH")"
    ;;
  Linux)
    DISTRO=""
    [[ -f /etc/os-release ]] && DISTRO="$(. /etc/os-release && echo "${PRETTY_NAME:-$NAME}")"
    green "$(t "Linux: ${DISTRO:-detected}" "Linux: ${DISTRO:-detectado}")"
    ;;
  *)
    red "$(t \
      "Unknown OS: $OS_KIND. Mac, Windows, and Linux are supported." \
      "Sistema operativo desconocido: $OS_KIND. Compatibles: Mac, Windows, Linux.")"
    ;;
esac

# ─── 2. Disk space ────────────────────────────────────────────────────────────
section "$(t "Disk space" "Espacio en disco")"

if [[ "$OS_KIND" == "Darwin" || "$OS_KIND" == "Linux" ]]; then
  FREE_KB="$(df -k "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')"
  if [[ -n "$FREE_KB" && "$FREE_KB" =~ ^[0-9]+$ ]]; then
    FREE_GB=$((FREE_KB / 1024 / 1024))
    if [[ $FREE_GB -ge 2 ]]; then
      green "$(t "${FREE_GB} GB free in \$HOME (≥2 GB needed)" "${FREE_GB} GB libres en \$HOME (mínimo 2 GB)")"
    else
      red "$(t \
        "Only ${FREE_GB} GB free in \$HOME. Free at least 2 GB before installing." \
        "Solo ${FREE_GB} GB libres en \$HOME. Liberá al menos 2 GB antes de instalar.")"
    fi
  else
    yellow "$(t "Could not measure free disk space" "No se pudo medir el espacio libre")"
  fi
fi

# ─── 3. Internet connectivity ─────────────────────────────────────────────────
section "$(t "Network connectivity" "Conexión a internet")"

reachable() {
  # Any HTTP response (incl. 403 / 404) counts as "network reached the host."
  # Only DNS failure, connect refused, or timeout count as not reachable.
  local url="$1" timeout="${2:-6}"
  local code
  code="$(curl -sS -m "$timeout" -o /dev/null -w "%{http_code}" "$url" 2>/dev/null)"
  [[ "$code" =~ ^[1-5][0-9][0-9]$ ]]
}

declare -a HOSTS=(
  "https://github.com|GitHub"
  "https://raw.githubusercontent.com|GitHub raw (Homebrew, scripts)"
  "https://registry.npmjs.org|npm registry (Claude Code)"
  "https://claude.ai|claude.ai (Claude Code sign-in)"
)

for entry in "${HOSTS[@]}"; do
  url="${entry%%|*}"; name="${entry##*|}"
  if reachable "$url"; then
    green "$(t "$name reachable" "$name accesible")"
  else
    red "$(t \
      "$name NOT reachable at $url — check VPN / firewall / corporate proxy" \
      "$name NO accesible en $url — revisá VPN / firewall / proxy corporativo")"
  fi
done

# ─── 4. Claude Code (the desktop app + CLI) ───────────────────────────────────
section "$(t "Claude Code" "Claude Code")"

CC_OK=0
if [[ "$OS_KIND" == "Darwin" && -d "/Applications/Claude.app" ]]; then
  green "$(t "Claude Code desktop app found at /Applications/Claude.app" "Claude Code (app de escritorio) encontrado en /Applications/Claude.app")"
  CC_OK=1
fi
if have claude; then
  CC_VER="$(claude --version 2>/dev/null | head -1 | awk '{print $1}')"
  green "$(t "Claude Code CLI on PATH: ${CC_VER:-installed}" "Claude Code CLI en PATH: ${CC_VER:-instalado}")"
  CC_OK=1
fi
if [[ $CC_OK -eq 0 ]]; then
  red "$(t \
    "Claude Code is not installed. Install from https://claude.ai/download then re-run this check." \
    "Claude Code no está instalado. Instalalo desde https://claude.ai/download y volvé a correr esta verificación.")"
else
  yellow "$(t \
    "Make sure you are signed in to Claude Code with a paid plan (Pro, Max, or Team) before pasting the install prompt." \
    "Asegurate de estar logueado en Claude Code con un plan pago (Pro, Max o Team) antes de pegar el prompt de instalación.")"
fi

# ─── 5. Admin / sudo capability (Mac/Linux) ───────────────────────────────────
section "$(t "Admin permissions" "Permisos de administrador")"

if [[ "$OS_KIND" == "Darwin" ]]; then
  if [[ "$EUID" -eq 0 ]]; then
    yellow "$(t \
      "Running as root. Install will work, but the recommended path is to run as your normal user; sudo is requested only when needed." \
      "Corriendo como root. Funciona, pero la ruta recomendada es como tu usuario normal; se pide sudo solo cuando hace falta.")"
  elif sudo -n true 2>/dev/null; then
    green "$(t "Sudo available (cached)" "Sudo disponible (en caché)")"
  elif id -Gn "$USER" 2>/dev/null | tr ' ' '\n' | grep -qE '^(admin|wheel)$'; then
    green "$(t "User is in the admin group — sudo will prompt for your Mac password" "Tu usuario está en el grupo admin — sudo te va a pedir tu contraseña de Mac")"
    info "$(t \
      "Heads up: when sudo prompts, you won't see characters as you type. That's normal Mac security." \
      "Aviso: cuando sudo te pida la contraseña, NO vas a ver los caracteres mientras tipiás. Es normal en Mac.")"
  else
    red "$(t \
      "Your user is not in the admin group. Homebrew + Obsidian install needs admin. Use a personal Mac or ask IT to add you." \
      "Tu usuario no está en el grupo admin. Homebrew y Obsidian necesitan admin. Usá una Mac personal o pedile a IT que te agregue.")"
  fi

  # MDM / device-management heads-up (informational only)
  if profiles -P 2>/dev/null | grep -q "There are.*configuration profiles installed" \
    || profiles status -type enrollment 2>/dev/null | grep -qi "enrolled"; then
    yellow "$(t \
      "This Mac is enrolled in Mobile Device Management (MDM). Some installs may be blocked by IT policy." \
      "Esta Mac está inscrita en Mobile Device Management (MDM). IT puede bloquear algunas instalaciones por política.")"
  fi
fi

# ─── 6. Optional pre-existing tools (ahead of the install) ────────────────────
section "$(t "Existing tools (informational)" "Herramientas ya instaladas (informativo)")"

if have_working_git; then
  info "$(t "git $(git --version 2>/dev/null | awk '{print $3}') (OK)" "git $(git --version 2>/dev/null | awk '{print $3}') (OK)")"
else
  IT_HELP_NEEDED=1
  yellow "$(t \
    "git is missing or not runnable yet (macOS: Xcode Command Line Tools not installed — a git binary can exist on PATH and still fail this way). Not required to start — the install fetches without git — but auto-update and any git workflow need it. Run: xcode-select --install, or ask IT (see the request below)." \
    "git falta o todavía no funciona (macOS: no están instaladas las Xcode Command Line Tools — puede haber un binario de git en el PATH y aun así fallar así). No hace falta para empezar — la instalación funciona sin git — pero la auto-actualización y cualquier flujo con git lo necesitan. Corré: xcode-select --install, o pedile a IT (ver el pedido más abajo).")"
fi

if have brew; then
  info "$(t "Homebrew already installed: $(brew --version 2>/dev/null | head -1)" "Homebrew ya instalado: $(brew --version 2>/dev/null | head -1)")"
fi

if have node; then
  NODE_V="$(node --version 2>/dev/null | tr -d v)"
  NODE_MAJOR="${NODE_V%%.*}"
  if [[ "$NODE_MAJOR" =~ ^[0-9]+$ && "$NODE_MAJOR" -ge 18 ]]; then
    info "$(t "Node $NODE_V (OK)" "Node $NODE_V (OK)")"
  else
    yellow "$(t \
      "Node $NODE_V is older than recommended (≥18). Bootstrap will keep your version; upgrade later if anything fails." \
      "Node $NODE_V es anterior al recomendado (≥18). Bootstrap mantiene tu versión; actualizá después si algo falla.")"
  fi
fi

if have python3; then
  PY_V="$(python3 --version 2>&1 | awk '{print $2}')"
  PY_MAJ="${PY_V%%.*}"
  PY_MIN="${PY_V#*.}"; PY_MIN="${PY_MIN%%.*}"
  if [[ "$PY_MAJ" =~ ^[0-9]+$ && "$PY_MIN" =~ ^[0-9]+$ ]] \
     && { [[ $PY_MAJ -gt 3 ]] || { [[ $PY_MAJ -eq 3 ]] && [[ $PY_MIN -ge 10 ]]; }; }; then
    info "$(t "Python $PY_V (OK)" "Python $PY_V (OK)")"
  else
    yellow "$(t \
      "Python $PY_V is older than 3.10. Bootstrap will install 3.12 alongside; not blocking." \
      "Python $PY_V es anterior a 3.10. Bootstrap instalará 3.12 al lado; no es bloqueante.")"
  fi
fi

if [[ "$OS_KIND" == "Darwin" && -d "/Applications/Obsidian.app" ]]; then
  info "$(t "Obsidian already installed at /Applications/Obsidian.app" "Obsidian ya instalado en /Applications/Obsidian.app")"
fi

# ─── 7. Existing ai-brain-starter clone ───────────────────────────────────────
section "$(t "Existing AI Brain Starter install" "Instalación previa de AI Brain Starter")"

ABS_DIR="$HOME/.claude/skills/ai-brain-starter"
if [[ -d "$ABS_DIR/.git" ]]; then
  info "$(t "Existing clone found at $ABS_DIR — bootstrap will fast-forward if behind, skip if a fork." \
            "Clon existente en $ABS_DIR — bootstrap hará fast-forward si está atrás, saltará si es un fork.")"
elif [[ -d "$ABS_DIR" ]]; then
  yellow "$(t \
    "Folder $ABS_DIR exists but is not a git clone. Bootstrap may overwrite. Move it aside if you have local changes." \
    "La carpeta $ABS_DIR existe pero no es un clon git. Bootstrap puede sobrescribir. Movela a un lado si tenés cambios locales.")"
else
  green "$(t "Clean slate — no previous install detected" "Comenzando de cero — sin instalación previa")"
fi

# ─── 8. JSON output ───────────────────────────────────────────────────────────
if [[ $JSON_MODE -eq 1 ]]; then
  python3 - "$GREEN_COUNT" "$YELLOW_COUNT" "$RED_COUNT" "$LANG_CODE" <<'PYEOF' \
    "${GREEN_LINES[@]+"${GREEN_LINES[@]}"}" "<SEP>" \
    "${YELLOW_LINES[@]+"${YELLOW_LINES[@]}"}" "<SEP>" \
    "${RED_LINES[@]+"${RED_LINES[@]}"}" "<SEP>" \
    "${INFO_LINES[@]+"${INFO_LINES[@]}"}"
import json, sys
green_n, yellow_n, red_n, lang = sys.argv[1:5]
remaining = sys.argv[5:]
buckets = []
current = []
for x in remaining:
    if x == "<SEP>":
        buckets.append(current); current = []
    else:
        current.append(x)
buckets.append(current)
out = {
    "lang": lang,
    "green": int(green_n),
    "yellow": int(yellow_n),
    "red": int(red_n),
    "lines": {"green": buckets[0], "yellow": buckets[1], "red": buckets[2], "info": buckets[3]},
    "status": "red" if int(red_n) > 0 else ("yellow" if int(yellow_n) > 0 else "green"),
}
print(json.dumps(out, indent=2, ensure_ascii=False))
PYEOF
fi

# ─── 9. Summary ───────────────────────────────────────────────────────────────
if [[ $JSON_MODE -eq 0 ]]; then
  printf "\n${C_BLD}%s${C_RST}\n" "$(t "Summary" "Resumen")"
  printf "  ${C_GRN}%d${C_RST} %s · ${C_YEL}%d${C_RST} %s · ${C_RED}%d${C_RST} %s\n\n" \
    "$GREEN_COUNT" "$(t "passed" "OK")" \
    "$YELLOW_COUNT" "$(t "warnings" "advertencias")" \
    "$RED_COUNT" "$(t "blockers" "bloqueantes")"
fi

if [[ $RED_COUNT -gt 0 ]]; then
  if [[ $JSON_MODE -eq 0 ]]; then
    printf "${C_RED}%s${C_RST}\n" "$(t \
      "Pre-flight FAILED. Fix the items marked ✗ above, then run:" \
      "Verificación FALLÓ. Arreglá los ítems marcados con ✗ y volvé a correr:")"
    printf "  bash scripts/preflight.sh\n"
    # A red blocker on a locked-down machine usually means "I can't fix this
    # myself" — hand over the composed ask so the very next step is pasting
    # it to IT, not re-deriving which packages to name.
    [[ $IT_HELP_NEEDED -eq 1 ]] && print_it_request
  fi
  exit 2
elif [[ $YELLOW_COUNT -gt 0 ]]; then
  if [[ $JSON_MODE -eq 0 ]]; then
    printf "${C_YEL}%s${C_RST}\n\n" "$(t \
      "Pre-flight PASSED with warnings. Bootstrap will continue; see warnings above." \
      "Verificación OK con advertencias. Bootstrap va a continuar; revisá las advertencias arriba.")"
    # git non-working is exactly this case: a warning, not a blocker — but
    # still something only IT can fix on a locked-down machine.
    [[ $IT_HELP_NEEDED -eq 1 ]] && print_it_request
  fi
  exit 1
else
  if [[ $JSON_MODE -eq 0 ]]; then
    printf "${C_GRN}%s${C_RST}\n\n" "$(t \
      "Pre-flight PASSED. You're ready — paste the install prompt into Claude Code." \
      "Verificación OK. Listo — pegá el prompt de instalación en Claude Code.")"
  fi
  exit 0
fi
