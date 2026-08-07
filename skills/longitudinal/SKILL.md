---
name: longitudinal
description: 'Use when the user wants multi-year health patterns from HealthKit + journal data: "patterns in my health data," "what does my body tell me," "correlations between mood and HRV," "Floor x body fingerprint," long-term trends in sleep, HRV, VO2max, cycle, or symptoms, "how has my body changed over the years," or /longitudinal. NOT for single-day analysis (use /health-doctor), this-week patterns (use /weekly), or journal-only pattern extraction (use /patterns).'
argument-hint: "[scope -- e.g. 'all', '5y', '1y', leave blank for 1y default]"
---

When the user types /longitudinal, run the multi-year correlation pass and surface only the strongest signals across years of health-mcp + journal data.

## Language

Generate the report in the language the user writes in. If Spanish, all sections including the panel commentary are in Spanish.

## Scope resolution

Parse the argument for window:
- `all` -> earliest record in DB to today
- `Ny` -> last N years (e.g. 5y, 3y)
- `Nm` -> last N months (e.g. 18m)
- blank -> 365 days

If `all`, query the DB for the earliest record date first:
```python
SELECT MIN(start_date) FROM records WHERE value IS NOT NULL
```

## Step 1: top_signals first (the noise filter)

Always call `health_top_signals(vault_root=..., lookback_days=N, min_strength="moderate")` first. This is Lara Briden's dissent codified: most correlations are noise. The substrate has already filtered. Start with what's left.

If `signal_count == 0`, report "no signals above noise threshold for this window" and stop — do not invent. Surface what IS there: the deltas and r-values that didn't quite clear the threshold, in case the user wants to relax it.

## Step 2: Floor x body fingerprints for the user's top 3 Floors

Load the journal index, count Floors in the window, take the top 3 by occurrence.

For each Floor, call:
```python
health_floor_body_fingerprint(floor=<name>, vault_root=..., lookback_days=N)
```

Report the body fingerprint deltas (HRV, RHR, sleep efficiency, cycle phase distribution). If `delta_pct` exceeds ±10% AND `n_on_floor >= 10`, this is a real fingerprint. Below that, mention it but flag as "weak."

## Step 3: Sleep architecture trend

Call `health_sleep_architecture(start, end)` for the window AND for the prior matching window (e.g. 1y now vs 1y prior). Compare REM%, Deep%, Core%, efficiency. Flag drift > 5 percentage points.

## Step 4: Longitudinal markers

Call `health_longitudinal_summary(start, end, granularity="quarter")`. Pull HRV baseline, VO2max, lean body mass, walking steadiness, sleep efficiency by quarter. Compute trend slope per marker (rough linear regression: (last - first) / first * 100).

Surface only markers with > 5% drift across the window.

## Step 5: Symptom correlate (if symptom data present)

Call `health_symptom_correlate(symptom_type=None, vault_root=..., lookback_days=N)`. If any symptom has > 15% delta_pct on a body metric AND n >= 5 days with the symptom, surface it.

## Step 6: Named loops (optional, if the user has /patterns output recently)

If `[VAULT_PATH]/Meta/Patterns/loops-detected.json` exists (created by /patterns), read it. For each named loop with date list, call `health_loop_signature(loop_dates_iso=[...], vault_root=...)`. Surface the loop body fingerprint.

If the file doesn't exist, skip this step silently — no error.

## Report format

```markdown
## Longitudinal pattern review -- <window>

### What survived the noise filter
<bullet list of top_signals results, strongest first>

### Floor x body fingerprint
<for each top 3 Floor: name, occurrence count, body deltas with > 10% threshold>

### Sleep architecture
<current vs prior window comparison, flag drift>

### Longevity markers (per quarter)
<table or bullets of HRV baseline, VO2max, lean mass trend>

### Symptoms
<symptom correlates with > 15% delta>

### Named loops (if /patterns ran recently)
<loop body fingerprints>

### Health panel commentary
<3-5 voices from the Health & Body section of advisory-panel.md commenting on the strongest signals. Required: Peter Attia (longevity), Stacy Sims (cycle phase if any female-physiology pattern is present), Bessel van der Kolk (HRV-vagal-tone if Floor x body signal is strong), Lara Briden (dissent: did we pick signal or noise). Each voice: 1-2 sentences referencing a SPECIFIC number from the report.>

### What to do next
<3-5 concrete, action-shaped items based on the strongest signals. NOT "consider X" -- "do X this week.">
```

## Critical: zero fabrication

Every number in the report comes from a health-mcp tool call. If a tool returns `error`, surface the error verbatim and skip that section. Never invent a metric. Never round so heavily that a 38.2 ms HRV becomes "around 40."

If the lookback window includes a period before the user started recording (e.g. they ran /longitudinal all but the DB only has 3 years), report the actual span you found: "earliest record: 2023-04-12, so the window is 2023-04-12 to 2026-05-10 (3.1 years)."

## When to escalate

If a single marker shows > 25% drift in the wrong direction (HRV baseline dropping > 25%, VO2max dropping > 25%, lean mass dropping > 10%), surface it AT THE TOP of the report with the marker name in bold and a one-line note. These are health-significant changes worth flagging directly. Do not bury them in section 4.
