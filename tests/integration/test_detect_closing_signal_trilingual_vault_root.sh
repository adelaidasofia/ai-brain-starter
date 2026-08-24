#!/usr/bin/env bash
# Test: session-close vault-root detection must work in every language the
# close cascade already supports, and must offer a prose-independent opt-in.
#
# Bug (MYC-2457): the "does this folder own its own close cascade" check matched
# an ENGLISH-ONLY heading (`^#+\s*Session\s+(?:End|Close)\b`) while the rest of
# the cascade shipped trilingual en/es/pt closing-signal packs. A Spanish- or
# Portuguese-authored CLAUDE.md therefore never declared itself, so
# find_repo_vault_root() returned None and resolution fell back to the global
# VAULT_ROOT. For an operator running several client brains on one machine with
# a global VAULT_ROOT exported — the LatAm consultant delivery shape — a
# non-English vault silently wrote its session artifacts into the WRONG vault.
# Silent, and it looks exactly like a healthy close.
#
# A heading is also only translation-proof until someone rewords it, so this
# adds a declarative marker that does not depend on prose at all.
#
# Assertions:
#   1. Spanish heading ("## Cierre de sesión") resolves to ITSELF under a
#      competing global VAULT_ROOT.  <- the reported bug
#   2. Portuguese heading ("## Fim de sessão") likewise.
#   3. Accent-less spellings ("Cierre de sesion") likewise — people type them.
#   4. `.session-close-root` sentinel file declares the root with NO heading at
#      all, in any language, however worded.
#   5. `sessionCloseRoot: true` in CLAUDE.md frontmatter does the same.
#   6. NEGATIVE CONTROL — English behavior byte-unchanged: an English-heading
#      vault still resolves to itself.
#   7. NEGATIVE CONTROL — FALLBACK PRESERVED: "## Session Protocol" (the
#      default vault's own real heading) must still NOT declare a root, or
#      every default vault would start false-winning the walk-up.
#   8. NEGATIVE CONTROL — a Spanish heading about something else
#      ("## Cierre de trimestre") must NOT declare a root.
#   9. A marker with NO Meta dir does NOT declare a root — it falls back.
#  10. Same for the frontmatter key with no Meta dir.
#
# 9-10 pin the 2026-08-22 reversal: the marker originally skipped the Meta
# requirement the heading path carries, on the reasoning that an explicit
# declaration should be honored immediately and then fail loudly about the
# missing dir. That premise did not hold — the fallback is not silent either
# (the offsite warning announces it), so loudness could not break the tie.
# Accidental-capture risk did: `.session-close-root` is a DOTFILE and dotfiles
# propagate by accident, so a stray one would capture a folder and write the
# operator's session notes into it — inside a cloned client repo, that is
# private content landing in someone else's tree. Requiring Meta makes a stray
# marker inert until a human creates somewhere to write.
#
# Controls 6-8 carry the weight: widening a regex is easy, and a widened regex
# that matches everything would pass 1-5 while silently breaking the fallback
# that keeps unrelated folders resolving to the operator's default vault.
#
# Self-contained: tmpdir fake vaults, HOME sandboxed. Exit 0 = pass.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=tests/integration/lib/sandbox_home.sh
. "$REPO_ROOT/tests/integration/lib/sandbox_home.sh"
HOOK="$REPO_ROOT/hooks/detect-closing-signal.py"
if [ ! -f "$HOOK" ]; then
  echo "ERROR: $HOOK not found" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
sandbox_home "$TMP/home"

DEFAULT="$TMP/default"
mkdir -p "$DEFAULT/⚙️ Meta"
printf '# Default vault\n\n## Session Protocol\n\nnot a close-cascade heading\n' > "$DEFAULT/CLAUDE.md"

fails=0

# Build a candidate vault: $1=name, $2=CLAUDE.md body ("" for none), $3=sentinel?(yes/no)
mkvault() {
  local d="$TMP/$1"
  mkdir -p "$d/⚙️ Meta"
  [ -n "$2" ] && printf '%s\n' "$2" > "$d/CLAUDE.md"
  [ "${3:-no}" = "yes" ] && : > "$d/.session-close-root"
  echo "$d"
}

# Same, but with NO Meta dir — a folder that declares itself and has nowhere
# to write. Reachable in production: a `.session-close-root` committed to a
# repo and cloned, swept up by `cp -r`, or baked into a scaffold.
mkvault_no_meta() {
  local d="$TMP/$1"
  mkdir -p "$d"
  [ -n "$2" ] && printf '%s\n' "$2" > "$d/CLAUDE.md"
  [ "${3:-no}" = "yes" ] && : > "$d/.session-close-root"
  echo "$d"
}

# Which vault root does the hook resolve to for a close fired inside $1?
resolved() {
  printf '{"prompt":"ok bye","session_id":"t","cwd":"%s"}' "$1" \
   | VAULT_ROOT="$DEFAULT" python3 "$HOOK" \
   | python3 -c 'import json,sys,re
c=json.load(sys.stdin).get("hookSpecificOutput",{}).get("additionalContext","")
m=re.search(r"^  Vault root:\s+(.*)$", c, re.M)
print(m.group(1).strip() if m else "")'
}

expect() { # $1=label $2=dir $3=self|default
  local got want
  got="$(resolved "$2")"
  if [ "$3" = "self" ]; then want="$2"; else want="$DEFAULT"; fi
  if [ "$got" != "$want" ]; then
    echo "FAIL: $1 — resolved to '$got', expected '$want'" >&2
    fails=$((fails + 1))
  fi
}

# 1-3. Non-English headings must declare the folder its own root.
expect "es heading"          "$(mkvault es      '# Bóveda

## Cierre de sesión

cascada aquí')"                        self
expect "pt heading"          "$(mkvault pt      '# Cofre

## Fim de sessão

cascata aqui')"                        self
expect "es heading no accent" "$(mkvault esna   '# Boveda

## Cierre de sesion

cascada aqui')"                        self

# 4-5. Declarative markers, prose-independent.
expect "sentinel file, no heading at all" "$(mkvault sentinel '# Anything

## Notas sueltas

no close heading here' yes)"           self
expect "frontmatter key" "$(mkvault fm '---
title: Client brain
sessionCloseRoot: true
---

# Whatever

## Notas')"                            self

# 6. NEGATIVE CONTROL — English unchanged.
expect "en heading (unchanged)" "$(mkvault en '# Vault

## Session End

cascade here')"                        self

# 7. NEGATIVE CONTROL — fallback preserved for a session-adjacent heading.
expect "Session Protocol still falls back" "$(mkvault proto '# Vault

## Session Protocol

not a close cascade')"                 default

# 8. NEGATIVE CONTROL — unrelated Spanish heading must not win.
expect "es unrelated heading falls back" "$(mkvault esother '# Bóveda

## Cierre de trimestre

reporte financiero')"                  default

# 9-10. A declared-but-unwritable folder must NOT capture the cascade.
expect "marker with NO Meta dir falls back" "$(mkvault_no_meta straymarker '' yes)" default
expect "frontmatter key with NO Meta dir falls back" "$(mkvault_no_meta strayfm '---
sessionCloseRoot: true
---

# Whatever')" default

if [ "$fails" -gt 0 ]; then
  echo "FAILED: $fails assertion(s)" >&2
  exit 1
fi
echo "PASS: vault-root detection is trilingual (en/es/pt) + declarative, Meta required on both paths, fallback preserved"
