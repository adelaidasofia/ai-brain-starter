#!/usr/bin/env bash
# Test: metadata extraction + the insight engine on a SPANISH vault, end to end.
#
# Bug class: three silent zeros, all found on one Spanish vault where every
# extractor ran green and every floor-based finding was empty:
#
#   1. person.py scanned a hardcoded "📓 Journals" folder. A Spanish install
#      has "📓 Diarios" (that is what Phase 1 tells the installer to create), so
#      the scan found nothing: every person got mention_count 0 and an empty
#      floor co-occurrence, and the insight engine's lucky-charm / drag-people
#      sections — which are built on those — never fired.
#   2. The journal writes the floor as a NAME (`floor: Entusiasmo`, `floor:
#      Hope`); person.py and the insight engine only read `floor_num`, and the
#      journal extractor's own name→number map was the pre-expansion 17-level
#      English list. Spanish names scored nothing; 16 of the 34 English floors
#      scored nothing; the rest scored on the wrong scale.
#   3. Spanish type names (`reunion`, `nota`, `estrategia`, …) and a few this
#      repo's own skills write (`rise`, `profile`) had no extractor, so those
#      notes dropped out of the index with no message.
#
# Fix under test: _floors.py (one canonical en+es map, name beats stored
# number), person.py journal-folder detection, TYPE_ALIASES for es + repo-own
# types (an explicit <type>.py still wins over an alias), and the engine
# deriving floor_num from the name.
#
# Self-contained: tmpdir fake vault + a private copy of scripts/ (so a custom
# extractor can be added without touching the repo). Every path the scripts
# touch is pinned by VAULT_ROOT / INSIGHTS_OUTPUT; nothing here reads or writes
# the home directory, so HOME is deliberately NOT redirected — the extractors
# import PyYAML, and a redirected HOME would hide a user-site install of it.
# Exit 0 = pass, 1 = fail. If no python3 on this machine can import yaml the
# suite says so and exits 0 (the CI job installs PyYAML for exactly this test).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
for f in scripts/vault-metadata-extract.py scripts/vault-insight-engine.py \
         scripts/extractors/_dispatcher.py scripts/extractors/_floors.py; do
  if [ ! -f "$REPO_ROOT/$f" ]; then
    echo "ERROR: $REPO_ROOT/$f not found" >&2
    exit 1
  fi
done

# The extractors and the engine `import yaml`. Prefer the python3 on PATH; fall
# back to any other interpreter that has it (a pipx venv from /graphify counts).
PY=""
for cand in python3 python3.13 python3.12 python3.11 python3.10 python3.9 "$HOME"/.local/pipx/venvs/*/bin/python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import yaml" >/dev/null 2>&1; then
    PY="$cand"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "SKIP: no python3 with PyYAML found on this machine (pip install pyyaml); the extractor end-to-end assertions did not run."
  exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Private copy of the scripts tree: lets us add a custom extractor (plan.py)
# and proves the tree works when it is a plain copy, not the repo checkout.
STARTER="$TMP/starter"
mkdir -p "$STARTER/scripts"
cp -R "$REPO_ROOT/scripts/extractors" "$STARTER/scripts/extractors"
rm -rf "$STARTER/scripts/extractors/__pycache__"
cp "$REPO_ROOT/scripts/vault-metadata-extract.py" "$REPO_ROOT/scripts/vault-insight-engine.py" "$STARTER/scripts/"
cat > "$STARTER/scripts/extractors/plan.py" <<'PY'
# A user's own extractor for `type: plan`. It must keep winning over the
# built-in `plan -> strategy` alias.
from _base import count_words, ExtractionResult
AUTO_FIELDS = ("plan_marker", "word_count")
def extract(filepath, body, fm, context):
    return ExtractionResult({"plan_marker": "custom-extractor", "word_count": count_words(body)}, AUTO_FIELDS, auto_fields=AUTO_FIELDS)
PY

# The Spanish vault, laid out the way the setup interview lays it out.
V="$TMP/vault"
mkdir -p "$V/📓 Diarios/2026-08" "$V/👤 CRM" "$V/📝 Notas" "$V/⚙️ Meta"

cat > "$V/📓 Diarios/2026-08/2026-08-01.md" <<'MD'
---
creationDate: 2026-08-01T21:10
type: journal
floor: Entusiasmo
floor_level: Alto
---

Hoy hablé con [[Ana Pérez]] sobre el proyecto y salió mejor de lo que esperaba.
MD
cat > "$V/📓 Diarios/2026-08/2026-08-02.md" <<'MD'
---
creationDate: 2026-08-02T21:10
type: journal
floor: Miedo
floor_level: Bajo
---

Día pesado. La reunión con [[Ana Pérez]] me dejó pensando en los plazos del banco.
MD
cat > "$V/📓 Diarios/2026-08/2026-08-03.md" <<'MD'
---
creationDate: 2026-08-03T21:10
type: journal
floor: Hope
floor_num: 9
---

An English-tagged day, with a stale floor_num from an older extractor still in the frontmatter.
MD
cat > "$V/📓 Diarios/2026-08/2026-08-04 Rise.md" <<'MD'
---
creationDate: 2026-08-04T07:30
type: rise
floor: Alegría
floor_level: Alto
priorities:
  - "cerrar el presupuesto"
---

Amanecí con energía y ganas de cerrar el presupuesto de una vez.
MD
cat > "$V/👤 CRM/Ana Pérez.md" <<'MD'
---
type: person
relationship: colega
company: Ejemplo S.A.
---

Ana lidera el equipo comercial y es la persona con la que más coordino.
MD
cat > "$V/📝 Notas/Comité de gerencia.md" <<'MD'
---
type: reunion
date: 2026-08-02
---

Comité semanal. Asistió [[Ana Pérez]] y repasamos el flujo de caja del mes.

## Decisiones
- Cerrar el presupuesto esta semana
MD
cat > "$V/📝 Notas/Plan comercial.md" <<'MD'
---
type: plan
---

Plan comercial del segundo semestre, con metas por canal y responsables por línea.
MD
cat > "$V/📝 Notas/Sistema de archivo.md" <<'MD'
---
type: sistema
---

Cómo se organizan las carpetas del vault y qué va en cada una, con ejemplos.
MD

# ── Run extraction, then the engine ─────────────────────────────────────
if ! VAULT_ROOT="$V" "$PY" "$STARTER/scripts/vault-metadata-extract.py" --progress-every 0 >"$TMP/extract.log" 2>&1; then
  echo "FAIL: vault-metadata-extract.py exited non-zero" >&2
  cat "$TMP/extract.log" >&2
  exit 1
fi
if ! VAULT_ROOT="$V" INSIGHTS_OUTPUT="$TMP/insights.md" \
     "$PY" "$STARTER/scripts/vault-insight-engine.py" --quiet >"$TMP/engine.log" 2>&1; then
  echo "FAIL: vault-insight-engine.py exited non-zero" >&2
  cat "$TMP/engine.log" >&2
  exit 1
fi

# ── Assertions ──────────────────────────────────────────────────────────
"$PY" - "$V" "$TMP/insights.md" "$TMP/extract.log" <<'PY'
import os, re, sys, yaml
V, report, extract_log = sys.argv[1], sys.argv[2], sys.argv[3]

def fm_of(rel):
    text = open(os.path.join(V, rel), encoding="utf-8").read()
    end = text.find("\n---", 3)
    return yaml.safe_load(text[3:end]) or {}

failed = 0
def check(cond, label):
    global failed
    if not cond:
        failed += 1
        print(f"FAIL: {label}", file=sys.stderr)

j1 = fm_of("📓 Diarios/2026-08/2026-08-01.md")
j2 = fm_of("📓 Diarios/2026-08/2026-08-02.md")
j3 = fm_of("📓 Diarios/2026-08/2026-08-03.md")
rise = fm_of("📓 Diarios/2026-08/2026-08-04 Rise.md")
person = fm_of("👤 CRM/Ana Pérez.md")
meeting = fm_of("📝 Notas/Comité de gerencia.md")
plan = fm_of("📝 Notas/Plan comercial.md")
sistema = fm_of("📝 Notas/Sistema de archivo.md")

# 1. journal extractor: floor NAME -> 34-floor number, Spanish and English
check(j1.get("floor_num") == 31, f"journal 'Entusiasmo' -> floor_num 31 (got {j1.get('floor_num')!r})")
check(j2.get("floor_num") == 13, f"journal 'Miedo' -> floor_num 13 (got {j2.get('floor_num')!r})")
check(j3.get("floor_num") == 20 or j3.get("floor_num") == 9,
      f"journal 'Hope' keeps a floor_num (got {j3.get('floor_num')!r})")
# (the stale 9 survives in the FILE without --force — the idempotency contract —
#  but must NOT survive into the person index or the engine, checked below)

# 2. rise -> journal alias: the morning entry is in the index with its floor
check("smart_excerpt" in rise or "word_count" in rise, "rise entry was routed to the journal extractor")
check(rise.get("floor_num") == 33, f"rise 'Alegría' -> floor_num 33 (got {rise.get('floor_num')!r})")

# 3. person.py found 📓 Diarios and translated the names (2 mentions, floors 31 + 13)
check(person.get("person_journal_mention_count") == 2,
      f"person mention count from 📓 Diarios == 2 (got {person.get('person_journal_mention_count')!r})")
co = [str(x) for x in (person.get("person_floor_cooccurrence") or [])]
check(sorted(co) == ["13", "31"], f"person floor co-occurrence == [31, 13] (got {co!r})")
check(person.get("person_last_journal_iso") == "2026-08-02",
      f"person last journal iso == 2026-08-02 (got {person.get('person_last_journal_iso')!r})")

# 4. Spanish type aliases routed to real extractors
check(meeting.get("meeting_date_iso") == "2026-08-02" and "Ana Pérez" in (meeting.get("meeting_attendees") or []),
      f"type: reunion -> meeting extractor (got {meeting!r})")
check("Cerrar el presupuesto esta semana" in (meeting.get("meeting_decisions") or []),
      "meeting extractor read the Spanish '## Decisiones' section")
check("reference_topic" in sistema or "word_count" in sistema, "type: sistema -> reference extractor")

# 5. an explicit extractor beats an alias
check(plan.get("plan_marker") == "custom-extractor", f"type: plan -> the user's plan.py, not the strategy alias (got {plan!r})")
check("strategy_counterpart" not in plan and "strategy_stakes" not in plan, "strategy fields were NOT written to the plan note")

# 6. nothing fell out of the index without an extractor
log = open(extract_log, encoding="utf-8").read()
for t in ("reunion", "rise", "sistema", "plan"):
    check(f"NO_EXTRACTOR_FOR:{t}" not in log, f"no NO_EXTRACTOR_FOR:{t} in the extraction log")

# 7. the insight engine has a floor baseline and used the canonical scale for
#    the stale-number entry (mean of 31, 13, 20 = 21.3; with the stale 9 it
#    would be 17.7)
rep = open(report, encoding="utf-8").read()
m = re.search(r"\| Journal floor: mean / p25 / p75 \| ([\d.]+) / ([\d.]+) / ([\d.]+) \|", rep)
check(m is not None, "engine report has a 'Journal floor' baseline row (floor_num derived from names)")
if m:
    mean = float(m.group(1))
    check(abs(mean - 21.3) < 0.15, f"engine floor mean == 21.3 from names (31,13,20), not from the stale stored 9 (got {mean})")

if failed:
    print(f"FAIL: {failed} assertion(s) failed", file=sys.stderr)
    sys.exit(1)
print("PASS: Spanish vault — journals in 📓 Diarios are found, floor names (es+en) score on the 34-floor scale, "
      "es/rise types reach real extractors, a custom extractor beats an alias, and the engine has a floor baseline")
PY
