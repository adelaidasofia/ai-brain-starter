#!/usr/bin/env bash
# exit-contract: NOT-A-CHECKER -- orchestrates the extract, wikilink and
#   insight phases

# second-brain-mapping.sh
# Unified vault-mapping pipeline. Runs three steps:
#   1. journal-metadata-extract   fast, no LLM, always runs
#   2. graphify                   expensive LLM, skipped here (needs Claude)
#   3. graphify_wikilink_gaps + graphify_apply_wikilinks  fast, interactive approval
#
# Graphify (Phase 2) cannot run from bash alone because it's LLM-orchestrated.
# Invoke the /second-brain-mapping slash command from Claude Code to drive all three,
# or run this script standalone to do Phase 1 + Phase 3 against the existing graph.
#
# Usage:
#   ./second-brain-mapping.sh                      # full: metadata + wikilinks (no graphify)
#   ./second-brain-mapping.sh --metadata-only      # only Phase 1
#   ./second-brain-mapping.sh --wikilinks-only     # only Phase 3
#   ./second-brain-mapping.sh --dry-run            # preview metadata + wikilink changes
#   ./second-brain-mapping.sh --force              # re-process already-tagged journals
#   ./second-brain-mapping.sh --year=2026          # scope metadata to one year
#
set -euo pipefail

# Self-locate: this script lives at <vault-root>/scripts/, so VAULT = parent dir.
# Override with VAULT_ROOT env var for non-standard installs.
SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAULT="${VAULT_ROOT:-$(cd "$SCRIPTS/.." && pwd)}"
GRAPH_OUT="$VAULT/graphify-out"

# ── Concurrency guard (POSIX-portable, no flock dependency) ───────────
# Uses atomic mkdir as the lock primitive. Works on macOS, Linux, WSL.
# Second invocation sees the directory exists and exits immediately.
LOCK_DIR="/tmp/second-brain-mapping.$(echo "$VAULT" | shasum -a 256 | cut -c1-12).lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  # Check if stale lock (owner PID no longer exists)
  if [[ -f "$LOCK_DIR/pid" ]]; then
    OWNER_PID=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
    if [[ -n "$OWNER_PID" ]] && ! kill -0 "$OWNER_PID" 2>/dev/null; then
      echo "Stale lock detected (PID $OWNER_PID gone). Clearing."
      rm -rf "$LOCK_DIR"
      mkdir "$LOCK_DIR" || { echo "Still locked after cleanup. Exiting."; exit 3; }
    else
      echo "Another /second-brain-mapping run is active (PID $OWNER_PID, lock: $LOCK_DIR)."
      echo "Exit that session or wait for it to finish."
      exit 3
    fi
  else
    echo "Lock exists without PID marker: $LOCK_DIR. Manual cleanup needed."
    exit 3
  fi
fi
echo $$ > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

METADATA_ONLY=0
WIKILINKS_ONLY=0
DRY_RUN=0
FORCE_META=0
YEAR_ARG=""

for arg in "$@"; do
  case $arg in
    --metadata-only) METADATA_ONLY=1 ;;
    --wikilinks-only) WIKILINKS_ONLY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --force) FORCE_META=1 ;;
    --year=*) YEAR_ARG="$arg" ;;
    --help|-h)
      sed -n '2,19p' "$0"
      exit 0
      ;;
    *) echo "Unknown flag: $arg (use --help)"; exit 2 ;;
  esac
done

ts() { date '+%H:%M:%S'; }
hr() { printf '%.0s─' {1..60}; echo; }

hr
echo "Second Brain Mapping   $(date '+%Y-%m-%d %H:%M %Z')"
hr

# ── Precheck: was /setup-vault-types run? ────────────────────────────
# The dispatcher discovers extractors by scanning scripts/extractors/ for
# non-underscore .py files. If none exist in the vault's extractor dir, the
# user never ran /setup-vault-types and Phase 1 would silently report
# "no extractor" for every file. Bounce early with a clear message.
EXTRACTOR_DIR="$SCRIPTS/extractors"
if [[ -d "$EXTRACTOR_DIR" ]]; then
  EXTRACTOR_COUNT=$(find "$EXTRACTOR_DIR" -maxdepth 1 -name '*.py' -not -name '_*' 2>/dev/null | wc -l | tr -d ' ')
else
  EXTRACTOR_COUNT=0
fi
if [[ "$EXTRACTOR_COUNT" -eq 0 && $WIKILINKS_ONLY -eq 0 ]]; then
  echo ""
  echo "No document-type extractors are configured in $EXTRACTOR_DIR."
  echo "Run /setup-vault-types first to choose which kinds of notes you take"
  echo "(journal, book, meeting, person, etc.). Then re-run /second-brain-mapping."
  echo ""
  echo "If you already ran /setup-vault-types and see this message, the symlinks"
  echo "may be pointing to a moved starter repo — re-run /setup-vault-types to refresh."
  exit 4
fi

# ── Phase 1: vault-wide metadata extraction (all types) ──────────────
if [[ $WIKILINKS_ONLY -eq 0 ]]; then
  echo ""
  echo "[$(ts)] Phase 1: Vault metadata extraction ($EXTRACTOR_COUNT registered types)"
  FLAGS=""
  [[ $DRY_RUN -eq 1 ]] && FLAGS="$FLAGS --dry-run"
  [[ $FORCE_META -eq 1 ]] && FLAGS="$FLAGS --force"
  [[ -n "$YEAR_ARG" ]] && FLAGS="$FLAGS $YEAR_ARG"

  python3 "$SCRIPTS/vault-metadata-extract.py" $FLAGS
  echo "[$(ts)] Phase 1 done"
fi

[[ $METADATA_ONLY -eq 1 ]] && { echo ""; echo "Metadata-only run complete."; exit 0; }

# ── Phase 2: graphify (NOTICE — not run from bash) ────────────────────
if [[ $WIKILINKS_ONLY -eq 0 ]]; then
  echo ""
  echo "[$(ts)] Phase 2: Graphify"
  echo "  Skipped in bash (graphify is LLM-orchestrated, needs Claude)."
  echo "  To run: invoke /second-brain-mapping in Claude Code,"
  echo "          or run /graphify separately before re-running this script."
fi

# ── Phase 3: wikilink gaps + apply ────────────────────────────────────
echo ""
echo "[$(ts)] Phase 3: Wikilink gaps + apply"

if [[ ! -f "$GRAPH_OUT/graph.json" ]]; then
  echo "  No graph.json at $GRAPH_OUT."
  echo "  Run /graphify first; rerun this script with --wikilinks-only to finish."
  exit 0
fi

cd "$VAULT"

# Always regenerate the gaps report — fast, deterministic, non-interactive.
if [[ $DRY_RUN -eq 1 ]]; then
  echo "  [DRY RUN] Regenerating WIKILINK_GAPS.md"
else
  echo "  → Regenerating WIKILINK_GAPS.md from graph.json"
fi
python3 "$SCRIPTS/graphify_wikilink_gaps.py"

# apply_wikilinks is interactive. Skip gracefully when stdin isn't a TTY
# (hook invocation, piped run, scheduled task) — never silently abort mid-file.
if [[ ! -t 0 ]]; then
  echo ""
  echo "  → Non-interactive shell detected. Skipping apply_wikilinks."
  echo "    Review WIKILINK_GAPS.md, then run interactively:"
  echo "    python3 \"\$SCRIPTS/graphify_apply_wikilinks.py\""
elif [[ $DRY_RUN -eq 1 ]]; then
  echo ""
  echo "  [DRY RUN] apply_wikilinks preview (first prompts only):"
  python3 "$SCRIPTS/graphify_apply_wikilinks.py" --dry-run
else
  echo ""
  echo "  → Starting interactive apply (approve each candidate):"
  python3 "$SCRIPTS/graphify_apply_wikilinks.py"
fi


# ── Phase 4: Insight engine ───────────────────────────────────────────
# Zero-LLM cross-type surprise finder. Runs every time — it's fast and the
# output changes whenever any metadata changes.
echo ""
echo "[$(ts)] Phase 4: Insight engine"
if [[ $DRY_RUN -eq 1 ]]; then
  echo "  [DRY RUN] Would scan typed files and write ⚙️ Meta/Second-Brain Insights.md"
else
  python3 "$SCRIPTS/vault-insight-engine.py" --top 5
fi

echo ""
hr
echo "Second Brain Mapping complete."
echo "Insights report: ⚙️ Meta/Second-Brain Insights.md"
hr
