#!/bin/bash
# Rotate hook logs when they exceed a size threshold.
# Keeps last N rotated copies, gzipped. Safe to call every SessionStart.
# Runs silently and fast (~10ms for small logs); exits 0 always.
#
# Policy: rotate at >500KB, keep 30 generations (.1.gz ... .30.gz).
# Total cap per log ~16MB on disk after compression.
#
# KEEP default raised from 3 → 30 so substrate-audit-style analyses
# over a 90-day window have enough rotated history to count rule fires.
# Override per-host via env var (KEEP=N bash rotate-logs.sh).
#
# Usage:
#   LOG_DIR=~/.claude/hooks bash rotate-logs.sh
#   KEEP=30 LOG_DIR=~/.claude/hooks bash rotate-logs.sh
#
# Or add specific log paths to LOGS array below.

set +e

LOG_DIR="${LOG_DIR:-$HOME/.claude/hooks}"
MAX_BYTES="${MAX_BYTES:-512000}"
KEEP="${KEEP:-30}"

# Auto-collect all .log files in LOG_DIR, or override by setting LOGS explicitly.
#
# NUL-delimited read loop, not `mapfile`: this script's shebang is /bin/bash,
# which on macOS is bash 3.2, where `mapfile` does not exist. It used to be one,
# and the builtin's absence (under `set +e`, and with `exit 0` at the bottom)
# left LOGS empty on every Mac -- so this hook ran at every SessionStart, rotated
# nothing, and reported success. Logs grew without bound and nothing said so.
# See scripts/PORTABILITY.md section 4; enforced by
# tests/integration/test_bash32_portability.sh.
if [ ${#LOGS[@]:-0} -eq 0 ]; then
  LOGS=()
  while IFS= read -r -d '' f; do
    LOGS+=("$f")
  done < <(find "$LOG_DIR" -maxdepth 1 -name "*.log" -print0 2>/dev/null)
fi

rotate() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  local size
  size=$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null || echo 0)
  (( size > MAX_BYTES )) || return 0

  local i
  for (( i = KEEP - 1; i >= 1; i-- )); do
    [[ -f "${f}.${i}.gz" ]] && mv "${f}.${i}.gz" "${f}.$((i + 1)).gz" 2>/dev/null
  done
  if gzip -c "$f" > "${f}.1.gz" 2>/dev/null; then
    : > "$f"
  else
    rm -f "${f}.1.gz"
  fi
}

for log in "${LOGS[@]}"; do
  rotate "$log"
done

# Evict generations beyond KEEP
for log in "${LOGS[@]}"; do
  ls "${log}."*.gz 2>/dev/null | sort -rn -t. -k2 | tail -n +$((KEEP + 1)) | while read -r old; do
    rm -f "$old"
  done
done

exit 0
