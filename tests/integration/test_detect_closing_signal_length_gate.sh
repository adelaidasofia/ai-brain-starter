#!/usr/bin/env bash
# Test: a sign-off is short and ends the message; pasted work is neither.
#
# Bug class: three false positives in nine days on one Spanish vault, all from
# the same two causes in detect-closing-signal.py:
#
#   1. The shared pack tiers were matched with re.MULTILINE, so every
#      $-anchored sign-off pattern (`\blisto[,.!\s]*$`, `\bya (está|estuvo|fue)…$`,
#      `\bbye\b…$`) matched the end of ANY line. "Borrador listo" as line 3 of a
#      60-line handoff fired the full close cascade.
#   2. Message length was never considered. A pasted brief, spec, or handoff is
#      work, not a wave — but nothing distinguished it from "listo, gracias".
#
# Fix under test:
#   1. The shared en/es/pt pack tiers match WITHOUT MULTILINE — against the
#      whole message (`$` = its true end) and against its LAST LINE alone (so a
#      `^bye`-shaped pattern still recognizes "All good.\nbye"). An inner line
#      ending in a sign-off word satisfies neither.
#   2. The natural-language tiers (high_confidence, ambiguous, emoji_only) only
#      look at prompts of at most SHORT_PROMPT_MAX_CHARS (300). The `explicit`
#      slash-command tier still fires at any length — typing /close is deliberate.
#   3. es.json strict_guards: "estoy listo/a" is readiness, never a goodbye.
#
# Every "should NOT fire" case has a "should fire" twin so a change that mutes
# the detector outright cannot pass this suite.
#
# Self-contained: tmpdir fake vault, HOME redirected. Exit 0 = pass, 1 = fail.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/detect-closing-signal.py"
if [ ! -f "$HOOK" ]; then
  echo "ERROR: $HOOK not found" >&2
  exit 1
fi

# The threshold under test must exist as a named constant — otherwise the gate
# regressed to a magic number (or to no gate at all) and the long-prompt
# assertions below would be testing an accident.
if ! grep -qE '^SHORT_PROMPT_MAX_CHARS *= *300' "$HOOK"; then
  echo "ERROR: SHORT_PROMPT_MAX_CHARS = 300 not found in $HOOK (length gate regressed)" >&2
  exit 1
fi

# es.json must carry the 'estoy listo' strict guard exactly once. Python's
# json.loads keeps the LAST duplicate key, so a second "strict_guards" block
# would silently discard whichever came first — the guard would exist in the
# file and be dead at runtime.
PACK="$REPO_ROOT/templates/closing-signals/es.json"
python3 - "$PACK" <<'PY' || exit 1
import json, re, sys
raw = open(sys.argv[1], encoding="utf-8").read()
n_keys = len(re.findall(r'"strict_guards"\s*:', raw))
if n_keys != 1:
    print(f"ERROR: es.json has {n_keys} 'strict_guards' keys (must be exactly 1; duplicates are silently dropped by json.loads)", file=sys.stderr)
    sys.exit(1)
guards = json.loads(raw).get("strict_guards", [])
pats = [g["pattern"] if isinstance(g, dict) else g for g in guards]
if not any("estoy" in p for p in pats):
    print("ERROR: es.json strict_guards is missing the 'estoy listo' guard", file=sys.stderr)
    sys.exit(1)
PY

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export HOME="$TMP/fake-home"
export USERPROFILE="$TMP/fake-home"
mkdir -p "$HOME/.claude"

VAULT="$TMP/vault"
META="$VAULT/Meta"
mkdir -p "$META/Sessions" "$META/Decisions"

run_hook() {
  local prompt="$1"
  printf '{"prompt":%s,"session_id":"test-sid","cwd":%s}' \
    "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$prompt")" \
    "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$VAULT")" \
    | VAULT_ROOT="$VAULT" CLOSING_SIGNAL_LANGS="en,es,pt" python3 "$HOOK"
}

assert_no_fire() {
  local label="$1" prompt="$2"
  local output
  output="$(run_hook "$prompt")"
  if echo "$output" | grep -qE "SESSION CLOSE|POSSIBLE SESSION CLOSE"; then
    echo "FAIL [should NOT fire] $label" >&2
    return 1
  fi
  return 0
}

assert_fires() {
  local label="$1" prompt="$2"
  local output
  output="$(run_hook "$prompt")"
  if ! echo "$output" | grep -qE "SESSION CLOSE|POSSIBLE SESSION CLOSE"; then
    echo "FAIL [should fire] $label" >&2
    return 1
  fi
  return 0
}

failed=0

# A filler paragraph with no sign-off vocabulary. ~110 chars, ASCII only so the
# byte/char count is unambiguous on every platform.
FILLER="Revisa el presupuesto de la torre norte, compara los tres escenarios de flujo y deja las cifras en la hoja de calculo."
LONG="$FILLER $FILLER $FILLER"          # > 300 chars
SHORT="$FILLER"                          # < 300 chars

# ── 1. MULTILINE removed: a sign-off word ending an INNER line is not a close ──
# (each phrase below is a real pack pattern anchored with `$` or `^`, so under
# MULTILINE it matched the inner line and fired the cascade)
assert_no_fire "es: 'Borrador listo' as an inner line of a handoff" \
  "$(printf 'Te paso el handoff de hoy.\nBorrador listo\nFalta la seccion de riesgos y las cifras del banco.')" \
  || failed=$((failed+1))
assert_no_fire "es: 'ya está' as an inner line" \
  "$(printf 'Lo del contrato ya está\nAhora falta cerrar los numeros con el banco y revisar el cronograma.')" \
  || failed=$((failed+1))
assert_no_fire "es: 'chao' as an inner line" \
  "$(printf 'Le escribi:\nchao\ny me quede pensando en la propuesta. Sigamos con el presupuesto.')" \
  || failed=$((failed+1))
assert_no_fire "en: \"i'm done\" as an inner line of a status update" \
  "$(printf "Status update:\ni'm done\nStill open: the retry path and the docs.")" \
  || failed=$((failed+1))
assert_no_fire "en: 'done for today' as an inner line" \
  "$(printf 'Notes from the call:\ndone for today\nNext: draft the migration plan and list the open risks.')" \
  || failed=$((failed+1))
assert_no_fire "en: 'bye' as an inner line" \
  "$(printf 'She wrote:\nbye\nand left the meeting. Next: rewrite the summary.')" \
  || failed=$((failed+1))

# …but the SAME words on the LAST line of a short message still close — a
# goodbye on the last line is exactly where a goodbye belongs.
assert_fires "es: 'listo, gracias' at the true end" \
  "$(printf 'Queda claro.\nlisto, gracias')" \
  || failed=$((failed+1))
assert_fires "es: 'ya está' at the true end" \
  "$(printf 'Perfecto.\nya está')" \
  || failed=$((failed+1))
assert_fires "es: 'chao' on the last line (^-anchored pattern)" \
  "$(printf 'Perfecto.\nchao')" \
  || failed=$((failed+1))
assert_fires "en: 'bye' on the last line (^-anchored pattern)" \
  "$(printf 'All good.\nbye')" \
  || failed=$((failed+1))
assert_fires "en: 'thanks' on the last line (^-anchored pattern)" \
  "$(printf 'All good.\nthanks!')" \
  || failed=$((failed+1))
assert_fires "en: \"i'm done\" at the true end" \
  "$(printf "All good.\ni'm done")" \
  || failed=$((failed+1))

# ── 2. Length gate: long prompts are work, even if they END in a sign-off ──
assert_no_fire "es: >300 chars ending in 'listo, gracias'" \
  "$LONG listo, gracias" \
  || failed=$((failed+1))
assert_no_fire "es: >300 chars ending in 'chao'" \
  "$(printf '%s\nchao' "$LONG")" \
  || failed=$((failed+1))
assert_no_fire "en: >300 chars ending in \"i'm done\"" \
  "$LONG i'm done" \
  || failed=$((failed+1))
assert_no_fire "en: >300 chars ending in 'bye' on its own line" \
  "$(printf '%s\nbye' "$LONG")" \
  || failed=$((failed+1))
assert_no_fire "en: >300 chars ending in a wave emoji (weak tier)" \
  "$(printf '%s\n👋' "$LONG")" \
  || failed=$((failed+1))

# …the same endings on a SHORT prompt still close (the gate is about length,
# not about the words).
assert_fires "es: <300 chars ending in 'listo, gracias'" \
  "$SHORT listo, gracias" \
  || failed=$((failed+1))
assert_fires "es: <300 chars ending in 'chao' on its own line" \
  "$(printf '%s\nchao' "$SHORT")" \
  || failed=$((failed+1))
assert_fires "en: <300 chars ending in \"i'm done\"" \
  "$SHORT i'm done" \
  || failed=$((failed+1))
assert_fires "en: <300 chars ending in a wave emoji" \
  "$(printf '%s\n👋' "$SHORT")" \
  || failed=$((failed+1))
assert_fires "es: bare 'chao'" "chao" || failed=$((failed+1))
assert_fires "en: bare 'bye'" "bye" || failed=$((failed+1))
assert_fires "es: bare 'listo'" "listo" || failed=$((failed+1))

# ── 3. The explicit slash-command tier ignores the length gate ──
assert_fires "es: /cerrar after >300 chars" \
  "$LONG /cerrar" \
  || failed=$((failed+1))
assert_fires "en: /close after >300 chars" \
  "$LONG /close" \
  || failed=$((failed+1))
assert_fires "en: /wrap-up as the whole prompt" "/wrap-up" || failed=$((failed+1))

# ── 4. 'estoy listo/a' is readiness, never a goodbye (strict guard) ──
assert_no_fire "es: 'estoy listo'" "estoy listo" || failed=$((failed+1))
assert_no_fire "es: 'estoy listo para el día'" "estoy listo para el día" || failed=$((failed+1))
assert_no_fire "es: 'estoy lista, arranquemos'" "estoy lista, arranquemos" || failed=$((failed+1))
assert_no_fire "es: 'ok, estoy listo'" "ok, estoy listo" || failed=$((failed+1))
# …while 'listo' without 'estoy' keeps its meaning.
assert_fires "es: 'listo por hoy'" "listo por hoy" || failed=$((failed+1))
assert_fires "es: 'ya está, listo'" "ya está, listo" || failed=$((failed+1))

if [ "$failed" -gt 0 ]; then
  echo "FAIL: $failed assertion(s) failed" >&2
  exit 1
fi
echo "PASS: pack tiers anchor to the whole message, natural-language tiers only fire on short prompts, /close ignores length, 'estoy listo' never closes"
