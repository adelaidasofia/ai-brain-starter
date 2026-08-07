#!/usr/bin/env python3
"""
monthly-baseline.py — compute structured baseline data for /monthly insights.

Walks journal entries in <VAULT_ROOT>/📓 Journals/{Month YYYY}/, extracts frontmatter
(floor, floor_level, sleep_time, gym, meditation, deep_work, rt_pulse,
rt_productive_h, rt_distracting_h, health_steps, health_calories, health_resting_hr,
people_mentioned, word_count), aggregates the target month and the prior 3 months,
computes deltas + anomalies + word-frequency comparisons, outputs structured JSON
or human-readable markdown.

The insights skill consumes this output to produce anomaly-led monthly reports
instead of summary-shaped ones.

Why: Without a baseline, "you had 6 Courage entries this month" is a number,
not insight. With a baseline ("Courage was 30%/49%/46% in the prior three months,
dropped to under 10% this month"), it becomes a signal worth a panel pass.

Usage:
  VAULT_ROOT=/path/to/vault python3 scripts/monthly-baseline.py --month 2026-04
  VAULT_ROOT=/path/to/vault python3 scripts/monthly-baseline.py --month 2026-04 --pretty
  VAULT_ROOT=/path/to/vault python3 scripts/monthly-baseline.py --month 2026-04 --output baseline.json

Configure TRACKED_WORDS for your own vocabulary anomalies. The default set
covers emotional-state words; add your project / person / topic names so the
report flags vocabulary spikes when a name or topic suddenly dominates.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
VAULT = Path(os.environ.get("VAULT_ROOT", str(_SCRIPT_DIR.parent)))
JOURNAL = VAULT / "📓 Journals"

MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]

# Keywords whose frequency we track (signals: emotional state, current focus, etc.)
# Add project / person / topic names to surface vocabulary spikes specific to your life.
# The baseline math is per-entry-normalized so entry-count differences don't pollute.
TRACKED_WORDS = [
    # Emotional-state register (universal)
    "tired", "stressed", "exhausted", "grateful", "excited", "frustrated",
    "angry", "calm", "overwhelmed", "peaceful", "anxious", "scared", "sad",
    "happy", "joy", "alive", "stuck", "free",
    # Add your own: project names, key people, recurring topics, themes
    # Example for a founder: "raise", "investor", "team", "product"
    # Example for a writer: "draft", "publish", "essay", "newsletter"
]

# Frontmatter fields we track + how to coerce them
NUMERIC_FIELDS = {
    "rt_pulse": float,
    "rt_productive_h": float,
    "rt_distracting_h": float,
    "deep_work": float,
    "health_steps": int,
    "health_calories": int,
    "health_resting_hr": int,
    "word_count": int,
}

BOOL_FIELDS = {"gym", "meditation"}  # truthy if value in {true, yes, gym, sí, si, 1}

TRUTHY = {"true", "yes", "gym", "sí", "si", "1", "y"}


def month_folder(month_iso: str) -> Path:
    """`2026-04` → `📓 Journals/April 2026`."""
    y, m = month_iso.split("-")
    return JOURNAL / f"{MONTH_NAMES[int(m) - 1]} {y}"


def prev_month(month_iso: str) -> str:
    y, m = map(int, month_iso.split("-"))
    if m == 1:
        return f"{y - 1}-12"
    return f"{y}-{m - 1:02d}"


def parse_frontmatter(path: Path) -> tuple[dict, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}, ""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    fm_text = text[4:end]
    body = text[end + 5:]
    fm: dict = {}
    for line in fm_text.split("\n"):
        m = re.match(r"^([\w_-]+):\s*(.*?)$", line)
        if m:
            fm[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return fm, body


def collect_month(month_iso: str) -> dict:
    folder = month_folder(month_iso)
    if not folder.exists():
        return {"month": month_iso, "exists": False, "entries": 0}

    entries_data = []
    floors = Counter()
    floor_levels = Counter()
    word_freq = Counter()
    numeric_acc: dict[str, list[float]] = defaultdict(list)
    bool_counts: dict[str, int] = defaultdict(int)
    people_mentions = Counter()
    body_lengths = []

    for path in folder.glob("*.md"):
        # Skip aggregation files
        if any(skip in path.name for skip in ("Summary", "Weekly", "Aggregated")):
            continue
        fm, body = parse_frontmatter(path)
        if not fm:
            continue
        if fm.get("type") not in (None, "journal", "ai-chat", "AI Extract"):
            # Only include actual journal entries
            if "journal" not in fm.get("type", "").lower():
                continue
        entries_data.append({"file": path.name, "fm": fm, "len": len(body.split())})

        # Floor
        if fm.get("floor"):
            floors[fm["floor"]] += 1
        if fm.get("floor_level"):
            floor_levels[fm["floor_level"]] += 1

        # Numeric fields
        for k, coerce in NUMERIC_FIELDS.items():
            v = fm.get(k)
            if v:
                # Strip units like "h" from "7.5h"
                v_clean = re.sub(r"[a-zA-Z%$,]", "", v)
                try:
                    numeric_acc[k].append(coerce(v_clean))
                except ValueError:
                    pass

        # Boolean fields (gym, meditation)
        for k in BOOL_FIELDS:
            v = fm.get(k, "").lower()
            if v in TRUTHY or (v.isdigit() and int(v) > 0):
                bool_counts[k] += 1

        # sleep_time may be like "23:00" or "01:45" — derive sleep duration if both sleep_time + wake_time exist
        # For now, just track presence
        if fm.get("sleep_time"):
            bool_counts["has_sleep_data"] += 1

        # People mentions
        if fm.get("people_mentioned"):
            for p in re.split(r"[,;\[\]]", fm["people_mentioned"]):
                p = p.strip().strip('"').strip("'")
                if p and len(p) > 1:
                    people_mentions[p] += 1

        # Body word freq
        body_lower = body.lower()
        for word in TRACKED_WORDS:
            n = len(re.findall(r"\b" + re.escape(word) + r"\b", body_lower))
            if n:
                word_freq[word] += n

        body_lengths.append(len(body.split()))

    out: dict = {
        "month": month_iso,
        "exists": True,
        "entries": len(entries_data),
        "floors": dict(floors),
        "floor_levels": dict(floor_levels),
        "word_freq": dict(word_freq),
        "people_mentions": dict(people_mentions.most_common(20)),
        "avg_entry_length": (sum(body_lengths) / len(body_lengths)) if body_lengths else 0,
    }
    # Numeric averages
    for k, vals in numeric_acc.items():
        if vals:
            out[f"{k}_avg"] = sum(vals) / len(vals)
            out[f"{k}_n"] = len(vals)
    # Bool counts
    for k, n in bool_counts.items():
        out[k] = n

    return out


def compute_deltas(target: dict, baselines: list[dict]) -> dict:
    """Compute deltas + anomalies vs baseline months.

    Returns:
      floor_deltas: each floor's % share in target vs avg-of-baselines, sorted by abs delta
      word_deltas: word-frequency ratios target/avg-baseline (>2× or <0.5× = anomaly)
      numeric_deltas: each numeric field's change vs baseline avg
      missing_data_flags: list of fields that are absent in target
    """
    if not target.get("exists") or target["entries"] == 0:
        return {"error": "no entries in target month"}

    deltas: dict = {}

    # Floor deltas (as % share)
    target_floors = target.get("floors", {})
    target_total = sum(target_floors.values()) or 1
    target_pct = {k: v / target_total * 100 for k, v in target_floors.items()}

    baseline_pcts: dict[str, list[float]] = defaultdict(list)
    for b in baselines:
        if not b.get("exists"):
            continue
        b_floors = b.get("floors", {})
        b_total = sum(b_floors.values()) or 1
        for k, v in b_floors.items():
            baseline_pcts[k].append(v / b_total * 100)

    floor_deltas = []
    all_floors = set(target_pct) | set(baseline_pcts)
    for f in all_floors:
        t = target_pct.get(f, 0)
        b_avg = sum(baseline_pcts[f]) / max(len(baseline_pcts[f]), 1) if baseline_pcts.get(f) else 0
        delta = t - b_avg
        if abs(delta) >= 3:  # 3pp threshold for surfacing
            floor_deltas.append({
                "floor": f, "target_pct": round(t, 1), "baseline_pct": round(b_avg, 1),
                "delta_pp": round(delta, 1),
                "ratio": round(t / b_avg, 2) if b_avg > 0.5 else None,
            })
    floor_deltas.sort(key=lambda x: -abs(x["delta_pp"]))
    deltas["floor_deltas"] = floor_deltas[:15]

    # Word frequency deltas (per-entry-normalized)
    target_wf = target.get("word_freq", {})
    target_n = target["entries"]
    word_deltas = []
    all_words = set(target_wf)
    for b in baselines:
        if b.get("exists"):
            all_words |= set(b.get("word_freq", {}))
    for w in all_words:
        t_per = target_wf.get(w, 0) / max(target_n, 1)
        b_pers = []
        for b in baselines:
            if not b.get("exists"):
                continue
            b_n = b["entries"] or 1
            b_pers.append(b.get("word_freq", {}).get(w, 0) / b_n)
        if not b_pers:
            continue
        b_avg = sum(b_pers) / len(b_pers)
        if t_per < 0.1 and b_avg < 0.1:
            continue  # both negligible
        ratio = t_per / b_avg if b_avg > 0.05 else (float("inf") if t_per > 0.5 else 1.0)
        if ratio >= 2.0 or (ratio <= 0.5 and b_avg > 0.5):
            word_deltas.append({
                "word": w,
                "target_per_entry": round(t_per, 2),
                "baseline_per_entry": round(b_avg, 2),
                "ratio": round(ratio, 2) if ratio != float("inf") else "new",
            })
    word_deltas.sort(key=lambda x: -(x["target_per_entry"] if isinstance(x["ratio"], str) else x["target_per_entry"] - x["baseline_per_entry"]))
    deltas["word_deltas"] = word_deltas[:20]

    # Numeric deltas (sleep, gym, RT, etc.)
    numeric_deltas = {}
    numeric_keys = [k for k in target if k.endswith("_avg")]
    for k in numeric_keys:
        t_val = target[k]
        b_vals = [b[k] for b in baselines if b.get(k) is not None]
        if not b_vals:
            continue
        b_avg = sum(b_vals) / len(b_vals)
        delta = t_val - b_avg
        delta_pct = (delta / b_avg * 100) if b_avg > 0 else 0
        if abs(delta_pct) >= 10:  # 10% threshold
            numeric_deltas[k.replace("_avg", "")] = {
                "target": round(t_val, 2),
                "baseline_avg": round(b_avg, 2),
                "delta_pct": round(delta_pct, 0),
            }
    deltas["numeric_deltas"] = numeric_deltas

    # Activity deltas (gym, meditation)
    activity_deltas = {}
    for k in ("gym", "meditation"):
        t_val = target.get(k, 0)
        b_vals = [b.get(k, 0) for b in baselines if b.get("exists")]
        b_avg = sum(b_vals) / max(len(b_vals), 1)
        delta = t_val - b_avg
        if abs(delta) >= 2:  # ≥2 entries off baseline
            activity_deltas[k] = {
                "target": t_val,
                "baseline_avg": round(b_avg, 1),
                "delta": round(delta, 1),
            }
    deltas["activity_deltas"] = activity_deltas

    # Entry-count delta (signal: did journaling slow?)
    target_count = target["entries"]
    baseline_counts = [b["entries"] for b in baselines if b.get("exists")]
    if baseline_counts:
        b_avg = sum(baseline_counts) / len(baseline_counts)
        deltas["entry_count_delta"] = {
            "target": target_count,
            "baseline_avg": round(b_avg, 1),
            "delta_pct": round((target_count - b_avg) / b_avg * 100, 0) if b_avg > 0 else 0,
        }

    # Missing-data flags
    missing = []
    for field in ("sleep_time", "rt_pulse_avg", "health_steps_avg", "gym"):
        looks_at = field.replace("_avg", "") if field.endswith("_avg") else field
        if not target.get(field) and not target.get(looks_at):
            missing.append(field)
    deltas["missing_data_flags"] = missing

    return deltas


def render_human(target_iso: str, target: dict, baselines: list[dict], deltas: dict) -> str:
    out = [f"# Monthly baseline — {target_iso}", ""]
    out.append(f"**Target**: {target['entries']} entries · avg length {target.get('avg_entry_length', 0):.0f} words")
    baseline_labels = ", ".join(b["month"] for b in baselines if b.get("exists"))
    out.append(f"**Baseline**: {baseline_labels} ({sum(b['entries'] for b in baselines if b.get('exists'))} entries)")
    out.append("")

    # Entry count
    if deltas.get("entry_count_delta"):
        d = deltas["entry_count_delta"]
        arrow = "↓" if d["delta_pct"] < 0 else "↑"
        out.append(f"## Entry count")
        out.append(f"- {target['entries']} entries this month vs {d['baseline_avg']:.0f} baseline avg ({arrow} {d['delta_pct']:+.0f}%)")
        out.append("")

    # Floor deltas
    if deltas.get("floor_deltas"):
        out.append("## Floor distribution shifts (≥3pp from baseline)")
        out.append("| Floor | Target % | Baseline % | Δpp | Ratio |")
        out.append("|---|---:|---:|---:|---:|")
        for d in deltas["floor_deltas"]:
            ratio_str = f"{d['ratio']}×" if d.get("ratio") else "—"
            out.append(f"| {d['floor']} | {d['target_pct']}% | {d['baseline_pct']}% | {d['delta_pp']:+.1f} | {ratio_str} |")
        out.append("")

    # Word freq anomalies
    if deltas.get("word_deltas"):
        out.append("## Word-frequency anomalies (≥2× or ≤0.5× baseline, per-entry-normalized)")
        out.append("| Word | This month/entry | Baseline/entry | Ratio |")
        out.append("|---|---:|---:|---:|")
        for d in deltas["word_deltas"]:
            ratio = d["ratio"] if d["ratio"] != "new" else "(new)"
            out.append(f"| {d['word']} | {d['target_per_entry']} | {d['baseline_per_entry']} | {ratio} |")
        out.append("")

    # Numeric deltas (RT, health)
    if deltas.get("numeric_deltas"):
        out.append("## Numeric metric deltas (≥10% from baseline)")
        out.append("| Metric | Target | Baseline avg | Δ% |")
        out.append("|---|---:|---:|---:|")
        for k, v in deltas["numeric_deltas"].items():
            out.append(f"| {k} | {v['target']} | {v['baseline_avg']} | {v['delta_pct']:+.0f}% |")
        out.append("")

    # Activity deltas (gym, meditation)
    if deltas.get("activity_deltas"):
        out.append("## Activity deltas (≥2 entries off baseline)")
        for k, v in deltas["activity_deltas"].items():
            out.append(f"- **{k}**: {v['target']} entries this month vs {v['baseline_avg']} baseline ({v['delta']:+.1f})")
        out.append("")

    # People top mentions (target month only — useful context)
    if target.get("people_mentions"):
        out.append("## Top people mentioned this month")
        out.append("(For panel context — who was the journaler thinking/writing about?)")
        for p, n in list(target["people_mentions"].items())[:10]:
            out.append(f"- **{p}**: {n} entries")
        out.append("")

    # Missing data flags
    if deltas.get("missing_data_flags"):
        out.append("## Missing data this month")
        out.append("Fields the insights skill cannot derive from frontmatter:")
        for f in deltas["missing_data_flags"]:
            out.append(f"- `{f}`")
        out.append("")

    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1] if __doc__ else "")
    ap.add_argument("--month", required=True, help="Target month, format YYYY-MM (e.g. 2026-04)")
    ap.add_argument("--baseline-months", type=int, default=3, help="Number of prior months for baseline (default 3)")
    ap.add_argument("--output", help="Write JSON to this path. Otherwise stdout.")
    ap.add_argument("--pretty", action="store_true", help="Output human-readable markdown instead of JSON")
    args = ap.parse_args()

    target = collect_month(args.month)
    baselines = []
    cursor = args.month
    for _ in range(args.baseline_months):
        cursor = prev_month(cursor)
        baselines.append(collect_month(cursor))

    deltas = compute_deltas(target, baselines)

    if args.pretty:
        out_text = render_human(args.month, target, baselines, deltas)
    else:
        out_text = json.dumps(
            {"target": target, "baselines": baselines, "deltas": deltas},
            indent=2,
            ensure_ascii=False,
        )

    if args.output:
        Path(args.output).write_text(out_text, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(out_text)


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
