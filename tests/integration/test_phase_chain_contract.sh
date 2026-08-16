#!/usr/bin/env bash
# Test: scripts/check-phase-chain.py holds the install phase files to one
# complete, walkable chain.
#
# History this locks (real, observed 2026-08-15):
#   The install is 12 files under phases/, ~3,700 lines, phases 0-24. Ten of the
#   twelve contained no reference to any other phase file anywhere in their body.
#   The only thing sequencing them was the Phase Routing Table in SKILL.md.
#   A real install stopped dead at the phase-02-03 -> phase-04 file boundary and
#   reported itself complete: the user was left with folders and a skeleton
#   CLAUDE.md while 15 phases had never run. Nothing errored. Nothing was missing
#   from disk. The file simply ended and the model concluded it was done.
#
# So each phase file now carries a footer naming its successor, in two coupled
# halves: model-facing prose (`phases/<next>.md`) and a machine marker
# (<!-- phase-chain: next=<file> -->). This test proves the checker actually
# rejects each way that contract can break — not merely that it agrees with the
# current, already-correct tree.
#
# Assertions:
#   1. POSITIVE  — the shipped phases/ passes.
#   2. POSITIVE CONTROL — a hand-built well-formed chain in a temp dir passes.
#      Without this, a malformed harness would make every negative below "pass"
#      for the wrong reason (everything exits 1 regardless of the mutation).
#   3. NEGATIVE  — the 2026-08-15 defect: a middle file with no marker.
#   4. NEGATIVE  — an orphan file nothing points to (the future regression:
#      someone adds phase-25 and wires nothing to it).
#   5. NEGATIVE  — a marker pointing at a file that does not exist.
#   6. NEGATIVE  — marker present but the model-facing prose missing (a green
#      checker over an install that still stops).
#   7. NEGATIVE  — a cycle.
#   8. NEGATIVE  — no terminal at all.
#   9. NEGATIVE  — two terminals.
#  10. FAIL LOUD — an empty or absent phases dir exits 2, never 0.
#
# Exit 0 = pass, 1 = fail.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CHECKER="$REPO_ROOT/scripts/check-phase-chain.py"
PHASES="$REPO_ROOT/phases"

for f in "$CHECKER" "$PHASES"; do
  [ -e "$f" ] || { echo "ERROR: $f not found" >&2; exit 1; }
done

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

failed=0
fail() { echo "FAIL: $*" >&2; failed=1; }
ok() { echo "ok   $*"; }

run_checker() {  # <phases-dir> -> echoes exit code
  python3 "$CHECKER" --phases-dir "$1" >/dev/null 2>&1
  echo $?
}

# Build a well-formed 3-file chain in $1 (fresh dir each time).
mk_chain() {
  local d="$1"
  rm -rf "$d"; mkdir -p "$d"
  cat > "$d/phase-00-a.md" <<'EOF'
# Phase 0
body
Read `phases/phase-01-b.md` next.
<!-- phase-chain: next=phase-01-b.md -->
EOF
  cat > "$d/phase-01-b.md" <<'EOF'
# Phase 1
body
Read `phases/phase-02-c.md` next.
<!-- phase-chain: next=phase-02-c.md -->
EOF
  cat > "$d/phase-02-c.md" <<'EOF'
# Phase 2
body
This is the last phase.
<!-- phase-chain: terminal -->
EOF
}

# --- 1. POSITIVE: the shipped tree ------------------------------------------
rc="$(run_checker "$PHASES")"
[ "$rc" = "0" ] && ok "shipped phases/ forms one complete chain" \
  || fail "shipped phases/ violates the chain contract (exit $rc) — run: python3 $CHECKER"

# --- 2. POSITIVE CONTROL on the harness itself ------------------------------
mk_chain "$TMP/good"
rc="$(run_checker "$TMP/good")"
[ "$rc" = "0" ] && ok "positive control: hand-built valid chain passes (harness is sound)" \
  || fail "positive control FAILED (exit $rc) — the harness is broken, so every negative below is meaningless"

# --- 3. NEGATIVE: the 2026-08-15 defect, a middle file that just ends --------
mk_chain "$TMP/nomarker"
cat > "$TMP/nomarker/phase-01-b.md" <<'EOF'
# Phase 1
body, and then the file simply ends with no pointer to anything
EOF
rc="$(run_checker "$TMP/nomarker")"
[ "$rc" = "1" ] && ok "a phase file with no chain marker is rejected (the original defect)" \
  || fail "missing chain marker NOT caught (exit $rc)"

# --- 4. NEGATIVE: an orphan nothing points to -------------------------------
mk_chain "$TMP/orphan"
cat > "$TMP/orphan/phase-03-d.md" <<'EOF'
# Phase 3
a phase nobody chains to, so it never runs for anybody
<!-- phase-chain: terminal -->
EOF
rc="$(run_checker "$TMP/orphan")"
[ "$rc" = "1" ] && ok "an unreachable phase file is rejected" \
  || fail "orphan phase NOT caught (exit $rc)"

# --- 5. NEGATIVE: pointer to a file that does not exist ---------------------
mk_chain "$TMP/dangling"
cat > "$TMP/dangling/phase-01-b.md" <<'EOF'
# Phase 1
Read `phases/phase-99-gone.md` next.
<!-- phase-chain: next=phase-99-gone.md -->
EOF
rc="$(run_checker "$TMP/dangling")"
[ "$rc" = "1" ] && ok "a pointer to a nonexistent phase file is rejected" \
  || fail "dangling pointer NOT caught (exit $rc)"

# --- 6. NEGATIVE: marker without the model-facing prose ---------------------
# The half that makes the checker green while the install still stops.
mk_chain "$TMP/noprose"
cat > "$TMP/noprose/phase-01-b.md" <<'EOF'
# Phase 1
body with no reference to the next file anywhere in the text
<!-- phase-chain: next=phase-02-c.md -->
EOF
rc="$(run_checker "$TMP/noprose")"
[ "$rc" = "1" ] && ok "a marker with no model-facing prose is rejected" \
  || fail "marker/prose drift NOT caught (exit $rc)"

# --- 7. NEGATIVE: a cycle ---------------------------------------------------
mk_chain "$TMP/cycle"
cat > "$TMP/cycle/phase-02-c.md" <<'EOF'
# Phase 2
Read `phases/phase-00-a.md` next.
<!-- phase-chain: next=phase-00-a.md -->
EOF
rc="$(run_checker "$TMP/cycle")"
[ "$rc" = "1" ] && ok "a cyclic chain is rejected" \
  || fail "cycle NOT caught (exit $rc)"

# --- 8. NEGATIVE: no terminal ----------------------------------------------
mk_chain "$TMP/noterm"
cat > "$TMP/noterm/phase-02-c.md" <<'EOF'
# Phase 2
Read `phases/phase-03-d.md` next.
<!-- phase-chain: next=phase-03-d.md -->
EOF
cat > "$TMP/noterm/phase-03-d.md" <<'EOF'
# Phase 3
the chain runs off the end with nothing declaring itself last
EOF
rc="$(run_checker "$TMP/noterm")"
[ "$rc" = "1" ] && ok "a chain with no terminal is rejected" \
  || fail "missing terminal NOT caught (exit $rc)"

# --- 9. NEGATIVE: two terminals --------------------------------------------
mk_chain "$TMP/twoterm"
cat > "$TMP/twoterm/phase-01-b.md" <<'EOF'
# Phase 1
also claims to be last
<!-- phase-chain: terminal -->
EOF
rc="$(run_checker "$TMP/twoterm")"
[ "$rc" = "1" ] && ok "two terminals are rejected" \
  || fail "duplicate terminal NOT caught (exit $rc)"

# --- 10. FAIL LOUD: cannot check -> exit 2, never 0 -------------------------
mkdir -p "$TMP/empty"
rc="$(run_checker "$TMP/empty")"
[ "$rc" = "2" ] && ok "an empty phases dir exits 2 (fail loud)" \
  || fail "empty phases dir should exit 2, got $rc"

rc="$(run_checker "$TMP/does-not-exist")"
[ "$rc" = "2" ] && ok "a missing phases dir exits 2 (fail loud)" \
  || fail "missing phases dir should exit 2, got $rc"

# --- 11. NEGATIVE: the chain and SKILL.md's routing table disagree ----------
# Two sources of truth for one order. The footers drive execution now, so a
# table that drifts is documentation that lies, and every check above passes.
mk_chain "$TMP/tabledrift"
cat > "$TMP/tabledrift/SKILL.md" <<'EOF'
# Routing table
| 0 | `phases/phase-00-a.md` |
| 2 | `phases/phase-02-c.md` |
| 1 | `phases/phase-01-b.md` |
EOF
rc="$(python3 "$CHECKER" --phases-dir "$TMP/tabledrift" --skill-md "$TMP/tabledrift/SKILL.md" >/dev/null 2>&1; echo $?)"
[ "$rc" = "1" ] && ok "a routing table that disagrees with the chain is rejected" \
  || fail "chain/table order drift NOT caught (exit $rc)"

# --- 12. POSITIVE: a table that agrees passes -------------------------------
cat > "$TMP/tabledrift/SKILL.md" <<'EOF'
# Routing table
| 0 | `phases/phase-00-a.md` |
| 1 | `phases/phase-01-b.md` |
| 2 | `phases/phase-02-c.md` |
EOF
rc="$(python3 "$CHECKER" --phases-dir "$TMP/tabledrift" --skill-md "$TMP/tabledrift/SKILL.md" >/dev/null 2>&1; echo $?)"
[ "$rc" = "0" ] && ok "a routing table that agrees passes (not just always-red)" \
  || fail "agreeing table wrongly rejected (exit $rc)"

if [ "$failed" -eq 0 ]; then
  echo "PASS: phase-chain contract holds, and the checker rejects all 8 break modes."
  exit 0
fi
echo "FAILED" >&2
exit 1
