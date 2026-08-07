#!/usr/bin/env bash
#
# check-open-core-boundary.sh — allowlist-model guard for ADR-0001 (open-core boundary).
#
# MODEL (v2, MYC-1339):
#   An ALLOWLIST (.github/free-tier-allowlist.txt) encodes the DELIBERATE free-tier set.
#   Any skill dir under skills/ or any capability-pack-shaped top-level dir NOT in the
#   allowlist → FAIL. Default = blocked.
#
#   This replaces the v1 denylist (which was blind to gaps: it only flagged known names
#   and structural subtrees, and could not catch unknown paths or the top-level agentic-os
#   regression). The denylist patterns are kept as belt-and-suspenders: they catch
#   explicitly-named leaked paths even if the allowlist is mis-populated.
#
# FAIL CLOSED:
#   Missing or empty .github/free-tier-allowlist.txt → EXIT 1 (never silent-pass).
#
# Compatible with bash 3 (macOS default) — no mapfile / declare -A.
#
# Source of truth: docs/adr/0001-open-core-boundary.md
# Allowlist data:  .github/free-tier-allowlist.txt
# Runs in:         .github/workflows/open-core-boundary.yml (CI) and locally pre-push.
#
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

ALLOWLIST=".github/free-tier-allowlist.txt"
HAS_ERROR=0
VIOLATION_FILE="$(mktemp)"
trap 'rm -f "${VIOLATION_FILE}"' EXIT

# ── Helper: is_allowed <path> ────────────────────────────────────────────────
# Returns 0 if path appears in the allowlist (exact first-token match), 1 otherwise.
is_allowed() {
  local needle="$1"
  grep -v '^\s*#' "${ALLOWLIST}" | grep -v '^\s*$' | awk '{print $1}' \
    | grep -qxF "${needle}"
}

# ── 0. Fail closed: allowlist file must exist and be non-empty ───────────────

if [ ! -f "${ALLOWLIST}" ]; then
  echo "OPEN-CORE BOUNDARY ERROR: allowlist file '${ALLOWLIST}' is missing." >&2
  echo "This guard fails closed. Create the file before merging." >&2
  echo "See docs/adr/0001-open-core-boundary.md for the allowlist format." >&2
  exit 1
fi

allowed_count="$(grep -v '^\s*#' "${ALLOWLIST}" | grep -v '^\s*$' | grep -c . || echo 0)"
if [ "${allowed_count}" -eq 0 ]; then
  echo "OPEN-CORE BOUNDARY ERROR: allowlist file '${ALLOWLIST}' is empty (or all comments)." >&2
  echo "This guard fails closed. A non-empty allowlist is required." >&2
  exit 1
fi

# ── 1. Check every skills/ subdirectory against the allowlist ────────────────

while IFS= read -r skill_dir; do
  [[ "${skill_dir}" == "skills" ]] && continue
  if ! is_allowed "${skill_dir}"; then
    echo "ALLOWLIST_GAP: '${skill_dir}' is under skills/ but not in ${ALLOWLIST}  -> paid-tier content or unreviewed addition" >> "${VIOLATION_FILE}"
    HAS_ERROR=1
  fi
done < <(git ls-files skills/ | awk -F'/' '{print $1"/"$2}' | sort -u | grep -v '^skills/$')

# ── 2. Check capability-pack-shaped top-level dirs against the allowlist ─────
#
# Heuristic: a top-level dir (not skills/, not dot-dirs, not standard repo dirs) that
# contains:  agents/ + (kernel/ or contexts/ or rules/)   — agentic-OS shape
# OR:        a SKILL.md at its root                        — skills-pack shape
#
# Standard non-pack dirs (never need to be in the allowlist):
STANDARD_DIRS=".agents .claude-plugin .github .git commands docs floors for-teams git-hooks hooks meeting-todos para-equipos phases scripts services skills templates tests themes vendor"

while IFS= read -r top_dir; do
  # Skip dot-prefixed dirs
  case "${top_dir}" in .*) continue ;; esac
  # Skip standard dirs (word-boundary match)
  if echo " ${STANDARD_DIRS} " | grep -qF " ${top_dir} "; then
    continue
  fi

  # Detect capability-pack shape
  is_pack=0
  # Shape A: has agents/ subdir AND (kernel/ or contexts/ or rules/)
  if git ls-files "${top_dir}/agents/" 2>/dev/null | grep -q .; then
    if git ls-files "${top_dir}/kernel/" "${top_dir}/contexts/" "${top_dir}/rules/" 2>/dev/null | grep -q .; then
      is_pack=1
    fi
  fi
  # Shape B: has a SKILL.md at its root
  if git ls-files "${top_dir}/SKILL.md" 2>/dev/null | grep -q .; then
    is_pack=1
  fi

  if [ "${is_pack}" -eq 1 ]; then
    if ! is_allowed "${top_dir}"; then
      echo "ALLOWLIST_GAP: top-level capability pack '${top_dir}/' is not in ${ALLOWLIST}  -> paid-tier content or unreviewed addition" >> "${VIOLATION_FILE}"
      HAS_ERROR=1
    fi
  fi
done < <(git ls-files | awk -F'/' 'NF>1{print $1}' | sort -u)

# ── 3. Belt-and-suspenders denylist (v1 structural patterns, kept as backstop) ──
#
# These catch known paid-tier names/structures even if the allowlist is mis-populated.

while IFS= read -r path; do
  [ -z "${path}" ] && continue
  echo "DENYLIST_HIT: '${path}'  -> explicit paid-tier structural pattern (vertical-*, influencer-pack, connectors/, decision-audit/)" >> "${VIOLATION_FILE}"
  HAS_ERROR=1
done < <(git ls-files \
  | grep -E \
      -e '^skills/vertical-[^/]+/' \
      -e '^skills/influencer-pack/' \
      -e '^skills/[^/]+/connectors/' \
      -e '^skills/[^/]+/decision-audit/' \
  || true)

# ── 4. Report ────────────────────────────────────────────────────────────────

if [ "${HAS_ERROR}" -eq 1 ]; then
  {
    echo "OPEN-CORE BOUNDARY VIOLATION (ADR-0001)."
    echo
    echo "The public substrate must not ship unreviewed or paid-tier content."
    echo "Each violation below must be resolved before merge:"
    echo
    while IFS= read -r v; do
      echo "  - ${v}"
    done < "${VIOLATION_FILE}"
    echo
    echo "To add a legitimate free-tier path: add it to ${ALLOWLIST} with a"
    echo "rationale tag (teaching|personal|lead-magnet|template-MYC254|"
    echo "ingest-exemplar|safety) and reference the codified free-decision"
    echo "(Done ticket / self-label / ADR-0001 entry)."
    echo
    echo "See docs/adr/0001-open-core-boundary.md for full boundary definition."
  } >&2
  exit 1
fi

echo "open-core boundary OK: all skills/ and capability packs are in the allowlist."
echo "  Allowlist: ${ALLOWLIST} (${allowed_count} entries)"
