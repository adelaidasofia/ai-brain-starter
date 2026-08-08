#!/usr/bin/env bash
# Test: the Spanish pack (es.json) discriminates meta-discussion from real closes.
#
# Bug class: es.json shipped WITHOUT a strict_guards tier — only en.json had one
# (codified 2026-05-25). Because detect-closing-signal.py lets the strong tiers
# (explicit, high_confidence) OVERRIDE false_positive_guards, every FP guard in
# es.json was dead code against a high_confidence hit. Spanish users therefore
# had no way to suppress meta-discussion of the cascade. Two real failures from
# one user's vault, four sessions running:
#
#   FALSE POSITIVE: "¿Ya está en el prompt o no está creado?" fired the full
#   cascade mid-explanation. The high_confidence pattern `\bya (está|estuvo|fue)\b`
#   was unanchored, so it matched inside any question; the "question-form" FP
#   guard could not suppress it because FP guards lose to high_confidence.
#
#   FALSE NEGATIVE: "cierra esta sesión" fired NOTHING. The high_confidence
#   pattern `\bcerr(emos|ar|amos)...` is built on the stem `cerr`, which covers
#   cerrar/cerremos/cerramos but NOT the imperative "cierra" — Spanish "cerrar"
#   is an e→ie stem-changing verb, so the imperative stem is "cierr", not "cerr".
#
# Fix under test:
#   1. strict_guards tier added to es.json (Spanish counterparts of en.json's).
#   2. `ya (está|estuvo|fue)` anchored to end-of-message.
#   3. imperative "cierra/cierre/cierren" + Argentine "cerrá" added to the
#      close-session pattern.
#
# Self-contained: tmpdir fake vault, HOME redirected. Exit 0 = pass, 1 = fail.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/detect-closing-signal.py"
PACK="$REPO_ROOT/templates/closing-signals/es.json"
if [ ! -f "$HOOK" ]; then
  echo "ERROR: $HOOK not found" >&2
  exit 1
fi
if [ ! -f "$PACK" ]; then
  echo "ERROR: $PACK not found" >&2
  exit 1
fi

# Verify es.json has strict_guards — otherwise the tier regressed back out and
# every assertion below would be testing the wrong thing.
if ! python3 -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get('strict_guards') else 1)" "$PACK" 2>/dev/null; then
  echo "ERROR: $PACK missing strict_guards array (fix regressed)" >&2
  exit 1
fi

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
  local prompt="$1"
  local output
  output="$(run_hook "$prompt")"
  if echo "$output" | grep -qE "SESSION CLOSE|POSSIBLE SESSION CLOSE"; then
    echo "FAIL [should NOT fire]: $prompt" >&2
    return 1
  fi
  return 0
}

assert_fires() {
  local prompt="$1"
  local output
  output="$(run_hook "$prompt")"
  if ! echo "$output" | grep -qE "SESSION CLOSE|POSSIBLE SESSION CLOSE"; then
    echo "FAIL [should fire]: $prompt" >&2
    return 1
  fi
  return 0
}

failed=0

# META-DISCUSSION + the reported FALSE POSITIVE (should NOT fire)
for p in \
  "¿Ya está en el prompt o no está creado?" \
  "ya está en el prompt o todavía no?" \
  "por qué se cierra sola la sesión?" \
  "arregla el regex de cierre de sesión" \
  "el hook de cierre dispara mal, revísalo" \
  "las sesiones se archivan solas" \
  "la sesión se cierra sola cada rato" \
  "cierra la sesión de la base de datos" \
  "¿ya cerraste la sesión?" \
  "¿la sesión quedó cerrada?" \
  "qué significa chao?" \
; do
  assert_no_fire "$p" || failed=$((failed+1))
done

# LEGITIMATE CLOSES (should fire — the guards must not over-suppress)
for p in \
  "cierra esta sesión" \
  "cierra la sesión" \
  "cerremos la sesión" \
  "cerrar la sesión" \
  "chao" \
  "buenas noches" \
  "nos vemos" \
  "ya está" \
  "ya está, gracias" \
  "listo, gracias" \
  "eso es todo" \
  "ya estuvo por hoy" \
  "¿puedes cerrar la sesión?" \
; do
  assert_fires "$p" || failed=$((failed+1))
done

if [ "$failed" -gt 0 ]; then
  echo "FAIL: $failed assertion(s) failed" >&2
  exit 1
fi
echo "PASS: es.json strict_guards gate meta-discussion; imperative 'cierra' and anchored 'ya está' behave"
