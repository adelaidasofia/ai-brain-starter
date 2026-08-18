#!/usr/bin/env python3
"""Compute period-vs-baseline anomalies for the insights skill (/weekly, /monthly).

The insights skill leads its report with anomalies and deltas, not with a summary
("Courage was 25% for three months, dropped to 3%" beats "you had 6 Courage
entries"). Deriving those by hand each run makes the numbers non-reproducible
between runs. This script computes them deterministically.

Usage:
    python3 monthly-baseline.py --month 2026-07 [--pretty]
    python3 monthly-baseline.py --week  2026-07-27 [--pretty]   # week START (Mon)

Output is JSON on stdout by default (for the skill to read), or a human-readable
report with --pretty. Nothing is written to disk.

WHAT IT REFUSES TO DO
---------------------
A common second-brain failure mode is confident output about things that were
never verified. So every comparison here carries
the sample size on BOTH sides, and when the baseline is too thin the script emits
`insufficient_baseline` instead of a number. A delta computed from two entries is
not a signal, and printing it as one is the bug this script must not become.

DESIGN NOTES
------------
* Text analysis runs on the JOURNAL VOICE SECTION ONLY (the `## Journal — voz de
  the journaler` block and its verbatim sub-block), never the `## Today` auto-pulled
  block. The auto-pull block names nearly every business every day, which makes
  every topic correlate with everything — a documented defect of earlier reports.
* `floor` may be a legacy inline list (`[Frustración, Gratitud]`) and entries may
  carry `floor_arc`. Both are EXPANDED for the floors-touched view and counted
  once for the landed view, per the insights skill's explicit instruction.
* Fields are sparsely populated (e.g. `meditation` exists on 4 of 16 entries).
  Coverage is reported for every field so absence is never read as a `false`.
"""
import argparse
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _floors import (  # noqa: E402
    Floors, landed_floor,
    parse_frontmatter, parse_inline_list, strip_accents,
)

# Reports live inside the journal folder and carry creationDate like entries do.
NON_JOURNAL_TYPES = {"insight", "insights", "monthly-insights", "weekly-insights",
                     "summary", "report"}
EXCLUDE_DIRS = {"Resúmenes", "Resumenes", "Summaries", "Reports"}

# Journal folder names this script recognises when --journal-dir is omitted.
# build-journal-index.py already solved this for itself (JOURNAL_DIR_CANDIDATES,
# added in #341); monthly-baseline.py never did, and kept the hardcoded English
# "📓 Journals", so /monthly and /weekly died on every localized vault the same
# way /weekly did before #341. This is a superset of that list, matched by
# SUFFIX so any emoji prefix works and the plural forms resolve too — a vault
# created as "📓 Diarios" matches "Diarios" here but matches nothing in
# build-journal-index.py's exact-name list. Kept as a local copy rather than a
# cross-import because the hyphenated filename isn't a valid Python module name
# and both scripts are meant to run standalone.
JOURNAL_DIR_NAMES = ("Journals", "Journal", "Diarios", "Diario",
                     "Diários", "Diário", "Journaux", "Tagebücher", "Tagebuch")


def find_journal_dir(vault_root):
    """Absolute path to the vault's journal folder, or None if there isn't one.

    Exact names win over emoji-prefixed ones so a vault carrying both resolves
    deterministically instead of depending on directory order.
    """
    try:
        children = sorted(os.listdir(vault_root))
    except OSError:
        return None
    dirs = [c for c in children if os.path.isdir(os.path.join(vault_root, c))]
    for name in JOURNAL_DIR_NAMES:
        for child in dirs:
            if child == name:
                return os.path.join(vault_root, child)
        for child in dirs:
            if child.endswith(" " + name):
                return os.path.join(vault_root, child)
    return None

# The voice section: "## Journal — voz de [name]", "## Diario — voz de [name]",
# "## Journal — la voz de [name]", "## Journal — [name]'s voice".
VOICE_HEADER = re.compile(r"^##\s+(Journal|Diario)\b", re.IGNORECASE | re.MULTILINE)
NEXT_H2 = re.compile(r"^##\s", re.MULTILINE)

# Inside the voice section the journaling skill inserts its own italic labels
# ("*Regla de captura verbatim.*", "*Sobre las gratitudes:*", "*Piso: [[Paz]]*").
# Those are the TOOL's words, not the journaler's, and they were drowning the word
# frequencies with template vocabulary ("captura", "barrido", "cerrando").
# A line whose entire content is wrapped in single asterisks is such a label.
ANNOTATION_LINE = re.compile(r"^\s*\*[^*].*\*\s*$", re.MULTILINE)
# Likewise the trailing habit tracker ("**Sueño:** 21:00 · **Gym:** no").
TRACKER_LINE = re.compile(r"^\s*\*\*[^*]+:\*\*.*$", re.MULTILINE)

STOPWORDS = set("""
a al algo algún alguna algunas alguno algunos ahí ahora ante antes aquel aquella
aquello aquí arriba así aun aunque bien cada casi como con contra cual cuales
cuando de del desde donde dos el él ella ellas ellos en entre era eran eres es
esa esas ese eso esos esta estaba estamos están estar estas este esto estos estoy
fue fuera fueron ha haber había han hasta hay hace hacer hacia he hemos hoy la
las le les lo los más me mi mis mucho muy nada ni no nos nosotros o os otra otro
otros para pero poco por porque puede pueden pues que qué quien quienes se sea
según ser si sí sin sobre solo son su sus tal también tanto te tiene tienen todo
todos tu tus un una uno unos usted ustedes va vamos van ver y ya yo lе
ese esa cosa cosas hizo dijo dice ahora luego después antes mismo misma
estar estoy está están sido siendo tener tengo tiene tenía haber hecho
día días semana mes año vez veces parte partes tema temas
""".split())

MIN_WORD_LEN = 4
DEFAULT_MIN_BASELINE = 5      # entries below which no anomaly is claimed
FLOOR_SHIFT_PP = 3.0          # skill threshold: report shifts >= 3 percentage points
WORD_RATIO_HI = 2.0           # skill threshold: >= 2x baseline rate
WORD_RATIO_LO = 0.5           # skill threshold: <= 0.5x baseline rate
METRIC_DELTA_PCT = 10.0       # skill threshold: >= 10% change
MIN_WORD_COUNT = 3            # ignore words too rare to mean anything

# A floor seen ONCE, on one side only, mechanically clears the 3pp threshold when
# the sample is small (1 of 9 entries = 11pp). That is arithmetic, not a shift.
# Require the busier side to have at least this many entries before calling it one.
MIN_FLOOR_N = 2


# ---------------------------------------------------------------- parsing

def voice_text(body):
    """Return the journal voice section only, or '' if the entry has no such header.

    Everything from the `## Journal|Diario ...` header to the next `## ` header.
    The `### Mis respuestas (verbatim)` sub-block is INSIDE this range on purpose:
    it is the journaler's own words too. The `## Today` auto-pull block is outside it.
    """
    m = VOICE_HEADER.search(body)
    if not m:
        return ""
    rest = body[m.end():]
    nxt = NEXT_H2.search(rest)
    return rest[:nxt.start()] if nxt else rest


def tokens(text):
    # Drop tool-authored scaffolding first, so template vocabulary cannot show up
    # as a "word anomaly" about the journaler's month.
    text = ANNOTATION_LINE.sub(" ", text)
    text = TRACKER_LINE.sub(" ", text)
    text = re.sub(r"\[\[([^\]|]+)(\|[^\]]+)?\]\]", r"\1", text)   # unwrap wikilinks
    text = re.sub(r"[`*_>#\-]", " ", text)
    out = []
    for w in re.findall(r"[a-záéíóúñü]+", text.lower()):
        if len(w) >= MIN_WORD_LEN and w not in STOPWORDS:
            out.append(w)
    return out


def parse_hhmm(v):
    """'21:00' / '22:30 (interrumpido...)' -> 21.0 / 22.5. Junk -> None."""
    if not v:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", str(v))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h + mi / 60.0


# Habit fields describe the day they are written in, so a morning entry's
# `gym: false` means "not yet", not "did not happen". Counting those as hard
# negatives is what made one 4-entry week report 0% gym and a -100% ANOMALY
# when the real number was 1 of 4. Below this capture hour a `false` is read as
# no-data instead.
GYM_CONFIRM_HOUR = 15


def truthy(v):
    """gym/meditation carry true/false/null/unknown/''. Returns True/False/None."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("true", "yes", "y", "sí", "si"):
        return True
    if s in ("false", "no", "n"):
        return False
    return None      # null, unknown, '' -> no data, NOT False


def capture_hour(meta):
    """Hour-of-day the entry was written, from creationDate. None if timeless."""
    return parse_hhmm(str(meta.get("creationDate", ""))[10:])


def gym_value(meta):
    """`gym`, but a morning `false` counts as no-data rather than a negative.

    Returns (True|False|None, premature) where premature flags a `false` that
    was demoted because the entry was captured before GYM_CONFIRM_HOUR.

    `gym_confirmed: true` opts an entry out of the demotion: it means the value
    was verified after the fact (the journaler said so in their own words, or a
    later entry's `gym_week` settles it), so an early-morning `false` is a real
    negative and must keep counting as one.
    """
    v = truthy(meta.get("gym"))
    if v is False and truthy(meta.get("gym_confirmed")) is not True:
        h = capture_hour(meta)
        if h is not None and h < GYM_CONFIRM_HOUR:
            return None, True
    return v, False


def gym_days_from_week_field(entries):
    """Gym days per the journaler's own running `gym_week` count.

    Preferred over counting `gym: true`: `gym_week` is cumulative and survives
    workouts on days with no entry, which the per-day boolean cannot see.
    Takes the highest value seen in each ISO week and sums those.
    """
    per_week = {}
    for e in entries:
        raw = str(e["meta"].get("gym_week", "")).strip()
        if not raw.isdigit():
            continue
        key = e["date"].isocalendar()[:2]
        per_week[key] = max(per_week.get(key, 0), int(raw))
    return (sum(per_week.values()), len(per_week)) if per_week else (None, 0)


# ---------------------------------------------------------------- loading

def load_entries(journal_dir):
    entries = []
    for root, dirs, files in os.walk(journal_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for fn in sorted(files):
            if not fn.endswith(".md"):
                continue
            p = os.path.join(root, fn)
            try:
                body = Path(p).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta = parse_frontmatter(body)
            if meta.get("type", "").strip().lower() in NON_JOURNAL_TYPES:
                continue
            if "creationDate" not in meta:
                continue
            try:
                d = datetime.strptime(meta["creationDate"][:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            v = voice_text(body)
            entries.append({
                "file": os.path.relpath(p, journal_dir),
                "date": d,
                "meta": meta,
                "voice": v,
                "voice_words": len(v.split()),
                "tokens": tokens(v),
            })
    entries.sort(key=lambda e: e["date"])
    return entries


def load_people(vault):
    """Roster from 👤 CRM/ filenames plus any `aliases:` in their frontmatter."""
    people = {}
    for crm in (Path(vault) / "👤 CRM", Path(vault) / "CRM"):
        if not crm.is_dir():
            continue
        for f in sorted(crm.glob("*.md")):
            name = f.stem
            names = {name}
            try:
                meta = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                meta = {}
            for a in parse_inline_list(meta.get("aliases")):
                if len(a) >= 4:
                    names.add(a)
            people[name] = names
    return people


# ---------------------------------------------------------------- analysis

def floor_views(entries, floors):
    """Landed distribution (one per entry) and touched distribution (arcs expanded)."""
    landed, touched, nums = Counter(), Counter(), []
    for e in entries:
        fl = parse_inline_list(e["meta"].get("floor"))
        arc = parse_inline_list(e["meta"].get("floor_arc"))
        if not fl:
            continue
        primary = fl[0]
        landed[primary] += 1
        n = floors.num(primary)
        if n:
            nums.append(n)
        for f in (arc or fl):
            touched[f] += 1
    return landed, touched, nums


# floor_level in entries is written in English; floor_tier in the notes, in Spanish.

def consistency_checks(entries, floors):
    """Frontmatter contradictions across a period. Delegates to the shared
    per-entry checker so this script and build-journal-index.py can never
    disagree about what counts as inconsistent."""
    issues = []
    for e in entries:
        for msg in floors.check(e["meta"], label=e["file"]):
            issues.append(f"{e['date']} — {msg}")
    return issues


def pct(c, total):
    return round(100.0 * c / total, 1) if total else 0.0


def mean(xs):
    return round(sum(xs) / len(xs), 2) if xs else None


def field_coverage(entries, field):
    have = sum(1 for e in entries
               if e["meta"].get(field, "").strip() not in ("", "null", "none"))
    return have, len(entries)


def numeric_metrics(entries, floors):
    _, _, nums = floor_views(entries, floors)
    sleeps = [h for h in (parse_hhmm(e["meta"].get("sleep_time")) for e in entries) if h]
    gym_read = [gym_value(e["meta"]) for e in entries]
    gym = [g for g, _ in gym_read]
    gym_known = [g for g in gym if g is not None]
    gym_premature = sum(1 for _, p in gym_read if p)
    gym_week_days, gym_week_n = gym_days_from_week_field(entries)
    doors = sum(1 for e in entries if e["meta"].get("door", "").strip() not in ("", "null"))
    verdicts = sum(1 for e in entries
                   if e["meta"].get("door_prev", "").strip() not in ("", "null"))
    return {
        "landed_floor_mean": mean(nums),
        "landed_floor_n": len(nums),
        "bedtime_mean_hour": mean(sleeps),
        "bedtime_n": len(sleeps),
        "voice_words_mean": mean([e["voice_words"] for e in entries]),
        "gym_rate_pct": (round(100.0 * sum(gym_known) / len(gym_known), 1)
                         if gym_known else None),
        "gym_known_n": len(gym_known),
        "gym_premature_n": gym_premature,
        "gym_days_week_field": gym_week_days,
        "gym_weeks_with_field": gym_week_n,
        "doors_set": doors,
        "door_verdicts": verdicts,
        "entries": len(entries),
    }


def compare_metrics(cur, base, sufficient):
    out = []
    labels = {
        "landed_floor_mean": "piso medio aterrizado",
        "bedtime_mean_hour": "hora media de dormir",
        "voice_words_mean": "palabras de voz propia por entrada",
        "gym_rate_pct": "% de días con gimnasio (sobre días con dato)",
    }
    for k, label in labels.items():
        a, b = cur.get(k), base.get(k)
        row = {"metric": k, "label": label, "period": a, "baseline": b}
        if not sufficient:
            row["status"] = "insufficient_baseline"
        elif a is None or b is None:
            row["status"] = "no_data"
        elif b == 0:
            row["status"] = "baseline_zero"
        else:
            d = 100.0 * (a - b) / abs(b)
            row["delta_pct"] = round(d, 1)
            # A rate built from n days moves in steps of 100/n points. When one
            # step is wider than the stable band (baseline +/- METRIC_DELTA_PCT),
            # no attainable value can ever read "stable" and the metric flags an
            # anomaly every period no matter what happened. That is resolution,
            # not signal -- say so instead of crying wolf.
            step = 100.0 / cur["gym_known_n"] if (
                k == "gym_rate_pct" and cur.get("gym_known_n")) else None
            if step is not None and step > abs(b) * METRIC_DELTA_PCT / 100.0:
                row["status"] = "insufficient_resolution"
                row["resolution_pp"] = round(step, 1)
            else:
                row["status"] = "anomaly" if abs(d) >= METRIC_DELTA_PCT else "stable"
        out.append(row)
    return out


def word_anomalies(cur, base, sufficient):
    if not sufficient:
        return []
    cw, bw = Counter(), Counter()
    for e in cur:
        cw.update(set(e["tokens"]))          # document frequency, not raw count:
    for e in base:                            # one entry ranting can't fake a trend
        bw.update(set(e["tokens"]))
    nc, nb = len(cur), len(base)
    if not nc or not nb:
        return []
    out = []
    for w in set(cw) | set(bw):
        c, b = cw[w], bw[w]
        if c + b < MIN_WORD_COUNT:
            continue
        rc, rb = c / nc, b / nb
        if rb == 0:
            if c >= MIN_WORD_COUNT:
                out.append({"word": w, "period_docfreq": c, "baseline_docfreq": 0,
                            "period_rate": round(rc, 3), "baseline_rate": 0.0,
                            "ratio": None, "kind": "nuevo"})
            continue
        ratio = rc / rb
        if ratio >= WORD_RATIO_HI or ratio <= WORD_RATIO_LO:
            kind = ("sube" if ratio >= WORD_RATIO_HI
                    else "desaparece" if c == 0 else "baja")
            out.append({"word": w, "period_docfreq": c, "baseline_docfreq": b,
                        "period_rate": round(rc, 3), "baseline_rate": round(rb, 3),
                        "ratio": round(ratio, 2), "kind": kind})
    out.sort(key=lambda r: (-(r["ratio"] if r["ratio"] is not None else 99),
                            -r["period_docfreq"]))
    return out[:20]


def people_mentions(entries, people, floors):
    rows = []
    for canon, names in people.items():
        total, days, floors_ = 0, [], []
        for e in entries:
            hits = sum(len(re.findall(re.escape(n), e["voice"], re.IGNORECASE))
                       for n in names)
            if hits:
                total += hits
                days.append(e["date"].isoformat())
                pr = landed_floor(e["meta"])
                n = floors.num(pr) if pr else None
                if n:
                    floors_.append(n)
        if total:
            rows.append({"person": canon, "mentions": total, "days": len(days),
                         "mean_floor_on_those_days": mean(floors_)})
    rows.sort(key=lambda r: -r["mentions"])
    return rows


def data_gaps(entries):
    tracked = ["floor", "floor_yesterday", "floor_arc", "moved_because", "body_check",
               "rope", "door", "door_prev", "gym", "sleep_time", "meditation",
               "gratitudes", "type"]
    gaps = []
    n = len(entries)
    for f in tracked:
        have, _ = field_coverage(entries, f)
        gaps.append({"field": f, "present": have, "of": n,
                     "coverage_pct": pct(have, n)})
    gaps.sort(key=lambda g: g["coverage_pct"])
    return gaps


# ---------------------------------------------------------------- periods

def month_range(s):
    y, m = int(s[:4]), int(s[5:7])
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    return start, end


def week_range(s):
    start = datetime.strptime(s, "%Y-%m-%d").date()
    start -= timedelta(days=start.weekday())        # snap back to Monday
    return start, start + timedelta(days=6)


# ---------------------------------------------------------------- render

def render(r):
    L = []
    p, b = r["period"], r["baseline"]
    L.append(f"BASELINE — {p['kind']} {p['label']}  ({p['start']} → {p['end']})")
    L.append(f"  periodo:  {p['n_entries']} entradas")
    L.append(f"  baseline: {b['n_entries']} entradas  ({b['start']} → {b['end']})"
             if b["n_entries"] else "  baseline: 0 entradas")
    if not b["sufficient"]:
        L.append(f"  ⚠️  {b['note']}")
    L.append("")

    L.append("PISOS — aterrizados")
    for f, c in r["floors"]["landed"].items():
        L.append(f"    {f:14} {c:3}  ({pct(c, p['n_entries'])}%)")
    if r["floors"]["touched"] != r["floors"]["landed"]:
        L.append("  tocados (arcos expandidos)")
        for f, c in r["floors"]["touched"].items():
            L.append(f"    {f:14} {c:3}")
    if r["floors"]["shifts"]:
        L.append(f"  cambios ≥{FLOOR_SHIFT_PP}pp vs baseline:")
        for s in r["floors"]["shifts"]:
            L.append(f"    {s['floor']:14} {s['baseline_pct']:5}% → {s['period_pct']:5}%"
                     f"  ({s['delta_pp']:+.1f}pp, n={s['n_period']}/{s['n_baseline']})")
    else:
        L.append("  cambios ≥3pp: ninguno computable" if not b["sufficient"]
                 else "  cambios ≥3pp: ninguno")
    L.append("")

    L.append("MÉTRICAS")
    for m in r["metrics"]["comparison"]:
        cur = m["period"] if m["period"] is not None else "—"
        base = m["baseline"] if m["baseline"] is not None else "—"
        tag = {"anomaly": "◀ ANOMALÍA", "stable": "estable",
               "insufficient_baseline": "sin baseline suficiente",
               "no_data": "sin dato", "baseline_zero": "baseline en cero",
               "insufficient_resolution": "resolución insuficiente"}[m["status"]]
        d = f"  {m['delta_pct']:+.1f}%" if "delta_pct" in m else ""
        L.append(f"    {m['label']:42} {str(cur):>8} vs {str(base):>8}{d}   {tag}")
    raw = r["metrics"]["period_raw"]
    L.append(f"    puertas puestas: {raw['doors_set']} · con veredicto: {raw['door_verdicts']}"
             f" · entradas: {raw['entries']}")
    L.append("")

    L.append("PALABRAS (solo sección de voz propia)")
    if not r["words"]:
        L.append("    sin baseline suficiente para anomalías léxicas")
    for w in r["words"][:12]:
        ratio = f"{w['ratio']}x" if w["ratio"] else "nuevo"
        L.append(f"    {w['word']:18} {w['period_docfreq']:2}/{p['n_entries']} entradas"
                 f"   vs baseline {w['baseline_docfreq']:2}   {ratio}  [{w['kind']}]")
    L.append("")

    L.append("PERSONAS (menciones en voz propia)")
    if not r["people"]:
        L.append("    ninguna ficha de CRM mencionada en la voz propia del periodo")
    for pr in r["people"][:10]:
        mf = pr["mean_floor_on_those_days"]
        L.append(f"    {pr['person']:22} {pr['mentions']:3} menciones · {pr['days']} días"
                 f" · piso medio {mf if mf else '—'}")
    L.append("")

    L.append("COBERTURA DE CAMPOS (periodo) — un campo vacío NO es un 'false'")
    for g in r["data_gaps"]:
        bar = "▓" * int(g["coverage_pct"] // 10) + "░" * (10 - int(g["coverage_pct"] // 10))
        L.append(f"    {g['field']:16} {bar} {g['present']:2}/{g['of']:<2} ({g['coverage_pct']}%)")

    if r.get("consistency"):
        L.append("")
        L.append("INCONSISTENCIAS DE FRONTMATTER (el dato se contradice a sí mismo)")
        for c in r["consistency"]:
            L.append(f"    ✗ {c}")
    if r["warnings"]:
        L.append("")
        L.append("AVISOS")
        for w in r["warnings"]:
            L.append(f"    ⚠️  {w}")
    return "\n".join(L)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="Period-vs-baseline anomalies for the insights skill.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--month", help="Target month, YYYY-MM")
    g.add_argument("--week", help="Any date in the target week, YYYY-MM-DD "
                                  "(snapped back to Monday)")
    # vault-root-ok: CLI default for an explicit --vault-root flag on a standalone
    # analysis tool that reads ANY vault by path, per the insights skill's
    # documented invocation (`VAULT_ROOT="<PATH>" python3 monthly-baseline.py ...`).
    # This script ships in the skill dir and is never itself inside a vault, so a
    # location-derived resolver would resolve to the skill rather than to the
    # vault under analysis. An explicit --vault-root always overrides this default.
    ap.add_argument("--vault-root", default=os.environ.get("VAULT_ROOT", "."))
    ap.add_argument("--journal-dir", default=None,
                    help="Journal subfolder relative to vault root. Default: auto-detect "
                         f"(any top-level folder named {', '.join(JOURNAL_DIR_NAMES)}, with or "
                         "without an emoji prefix).")
    ap.add_argument("--baseline-periods", type=int, default=0,
                    help="How many prior periods to use as baseline. "
                         "0 (default) = everything before the period.")
    ap.add_argument("--min-baseline", type=int, default=DEFAULT_MIN_BASELINE,
                    help=f"Baseline entries required before any anomaly is claimed "
                         f"(default {DEFAULT_MIN_BASELINE}). Below this the script "
                         f"reports insufficient_baseline instead of a number.")
    ap.add_argument("--pretty", action="store_true", help="Human-readable output")
    args = ap.parse_args()

    vault = os.path.abspath(args.vault_root)
    # Floor vocabulary comes from the vault's own floor notes, read once here
    # and threaded down. Nothing about the floors is declared in this script.
    floors = Floors(vault)
    if args.journal_dir is not None:
        jdir = os.path.join(vault, args.journal_dir)
        if not os.path.isdir(jdir):
            print(f"journal directory not found: {jdir}", file=sys.stderr)
            sys.exit(1)
    else:
        resolved = find_journal_dir(vault)
        if resolved is None:
            names = ", ".join(f"'{n}'" for n in JOURNAL_DIR_NAMES)
            print(f"no journal folder found under {vault}\n"
                  f"Looked for a top-level folder named {names} (an emoji prefix like "
                  f"'📓 Diarios' is fine). Pass --journal-dir <name> for a different layout.",
                  file=sys.stderr)
            sys.exit(1)
        jdir = resolved

    if args.month:
        kind, label = "month", args.month
        start, end = month_range(args.month)
        span = timedelta(days=30)
    else:
        kind, label = "week", args.week
        start, end = week_range(args.week)
        label = start.isoformat()
        span = timedelta(days=7)

    entries = load_entries(jdir)
    cur = [e for e in entries if start <= e["date"] <= end]

    b_start = None if args.baseline_periods == 0 else start - span * args.baseline_periods
    base = [e for e in entries
            if e["date"] < start and (b_start is None or e["date"] >= b_start)]

    warnings = []
    if not cur:
        warnings.append(f"No hay entradas en el periodo {label}.")
    sufficient = len(base) >= args.min_baseline
    if not sufficient:
        note = (f"baseline de {len(base)} entradas (<{args.min_baseline}): no se "
                f"reportan anomalías. Los números del periodo son válidos; las "
                f"COMPARACIONES no.")
        warnings.append(note)
    else:
        note = ""

    c_landed, c_touched, _ = floor_views(cur, floors)
    b_landed, _, _ = floor_views(base, floors)
    shifts = []
    if sufficient:
        for f in set(c_landed) | set(b_landed):
            np_, nb_ = c_landed[f], b_landed[f]
            if max(np_, nb_) < MIN_FLOOR_N:
                continue      # one lone entry is not a distribution shift
            cp, bp = pct(np_, len(cur)), pct(nb_, len(base))
            if abs(cp - bp) >= FLOOR_SHIFT_PP:
                shifts.append({"floor": f, "period_pct": cp, "baseline_pct": bp,
                               "delta_pp": round(cp - bp, 1),
                               "n_period": np_, "n_baseline": nb_})
        shifts.sort(key=lambda s: -abs(s["delta_pp"]))

        # Resolution guard. With N entries the distribution can only move in
        # 100/N steps, so a 3pp threshold on a 6-entry week is below the
        # instrument's resolution — every floor that moves at all "clears" it.
        step = 100.0 / len(cur) if cur else 0
        if step > FLOOR_SHIFT_PP:
            warnings.append(
                f"Con {len(cur)} entradas cada una vale {step:.1f}pp, así que el "
                f"umbral de {FLOOR_SHIFT_PP}pp está por debajo de la resolución: "
                f"los 'cambios' de pisos son granularidad, no tendencia. "
                f"Leelos como conteos (n=x/y), no como porcentajes.")

    cur_m, base_m = numeric_metrics(cur, floors), numeric_metrics(base, floors)

    if cur and all(not e["voice"] for e in cur):
        warnings.append("Ninguna entrada del periodo tiene sección de voz propia "
                        "reconocible; el análisis de palabras y personas queda vacío.")

    result = {
        "generated": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        "period": {"kind": kind, "label": label, "start": start.isoformat(),
                   "end": end.isoformat(), "n_entries": len(cur),
                   "files": [e["file"] for e in cur]},
        "baseline": {"start": base[0]["date"].isoformat() if base else None,
                     "end": base[-1]["date"].isoformat() if base else None,
                     "n_entries": len(base), "sufficient": sufficient,
                     "min_required": args.min_baseline, "note": note},
        "floors": {"landed": dict(c_landed.most_common()),
                   "touched": dict(c_touched.most_common()),
                   "baseline_landed": dict(b_landed.most_common()),
                   "shifts": shifts},
        "metrics": {"period_raw": cur_m, "baseline_raw": base_m,
                    "comparison": compare_metrics(cur_m, base_m, sufficient)},
        "words": word_anomalies(cur, base, sufficient),
        "people": people_mentions(cur, load_people(vault), floors),
        "data_gaps": data_gaps(cur),
        "consistency": consistency_checks(cur, floors),
        "warnings": warnings,
    }

    print(render(result) if args.pretty
          else json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    main()
