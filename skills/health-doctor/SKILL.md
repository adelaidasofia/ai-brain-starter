---
name: health-doctor
description: Use when the user runs /health doctor or /health status, asks whether the health auto-chain is working, when the last Oura, Fitbit, Apple Health, or labs sync happened, whether the coach prescribed today, why a workout or prescription didn't appear in the calendar, why wearable data looks stale or missing, whether an out-of-range lab marker needs a re-test, or wants to confirm health-mcp hook wiring after first-time setup.
---

# health-doctor

The substrate has 41 tools + 5 skills + 2 auto-trigger hooks. None of it matters if the chain breaks silently. This skill is the surface for verifying the system is actually running.

Run `/health doctor` whenever:
- It's been a few days since you saw a workout in your calendar
- You want to know if yesterday's wearable data actually made it in
- You want to see which lab markers need a re-test
- You're setting up the auto-chain for the first time and want to confirm the wiring

## What it reports

Six sections, each with a green / yellow / red flag:

### 1. Data freshness

For each enabled source:
- **Apple Health**: hours since last `health_import_xml` (yellow if > 14 days, red if > 28 days)
- **Oura**: hours since last `health_import_oura` (yellow if > 36h, red if > 7d)
- **Fitbit**: hours since last `health_import_fitbit` (yellow if > 36h, red if > 7d)
- **Labs**: days since last `health_import_labs` (yellow if any marker is > 180 days old or any out-of-range marker is > 90d without a re-test)

Pulled from the `imports` table via `health_status()`.

### 2. Last prescription + completion

- Most recent prescription via `health_coach_recent_prescriptions(days=7)`
- Was it completed? (RPE + lift actuals logged via `/coach log`)
- Streak: how many days in a row had both a prescription AND a completion
- Missed days: prescriptions with no completion in the last 14 days

Yellow if completion rate < 60%. Red if completion rate < 40% or no prescriptions in 7 days.

### 3. Auto-trigger hooks installed

Check whether the two automation hooks are wired in `~/.claude/settings.json`:
- `health-auto-sync.py` on SessionStart (silently refreshes Oura/Fitbit if stale)
- `coach-auto-prescribe-on-journal.py` on Stop (prescribes + backfills after /journal)

Red if expected hooks not in settings.json. Yellow if the hook script files exist but aren't registered. Green if both registered AND firing recently (check `~/.claude/hookify-blocks.log` for the hook name in the last 48h).

### 4. Coach profile status

Reads `<VAULT_ROOT>/Meta/coach-profile.yaml`:
- Profile exists? Last updated?
- `calendar_drop: true` AND google-workspace MCP connected? (calendar drop won't fire otherwise)
- `preferred_workout_clock` set?
- `days_per_week` reasonable for `level`?
- `started_iso` set? (used by deload-week computation)

Yellow if any field missing. Red if no profile at all.

### 5. Lab status flags

Run `health_lab_panel(today, lookback_days=180)` and surface ANY marker with `status: low` or `status: high`:
- Marker, value, range, status
- Days since last test for that marker
- WHY this marker matters (pull from `health_recommended_labs()`)
- Suggested re-test cadence (e.g. "re-test in 90 days after supplementation")

Yellow if any marker is out-of-range and last tested > 90 days ago. Red if any out-of-range marker is critical (low ferritin in menstruating users, elevated hs-CRP > 3.0, fasting insulin > 10, Vitamin D < 20).

### 6. Cycle phase + sleep regularity (women's substrate qualifier)

If menstrual flow records exist:
- Current phase + cycle day + irregularity flag from `health_cycle_context(today)`
- Cycle length variance over last 6 cycles
- Yellow if irregularity = "mild_irregular". Red if "irregular".

Plus `health_sleep_regularity(last_14_days)`:
- Regularity score
- Bed-time stdev, wake-time stdev, mean sleep latency
- Yellow if regularity < 70. Red if < 50.

## Output format

A markdown report with the six sections, each summarized to 3-5 lines with the flag, the data, and a one-line "what to do" if yellow or red.

Example:

```markdown
# Health doctor — 2026-05-10

## 🟢 Data freshness
- Apple Health: 12 days ago (yellow threshold: 14d) — re-export soon
- Oura: 4 hours ago — fresh
- Fitbit: not configured
- Labs: ApoB tested 2026-05-01 (9 days), Vitamin D last 2026-05-01 (9 days)

## 🟡 Last prescription + completion
- Last prescription: 2026-05-09 lower_body_strength (diff 7/10)
- Completed: no
- 7-day streak: 0 (missed yesterday's log)
- Action: /coach log yesterday's session to keep the progression chain accurate

## 🟢 Auto-trigger hooks
- SessionStart: health-auto-sync.py ✓ registered, last fired 4h ago
- Stop: coach-auto-prescribe-on-journal.py ✓ registered, last fired 14h ago

## 🟢 Coach profile
- /vault/Meta/coach-profile.yaml updated 2026-05-09
- calendar_drop: true (google-workspace MCP connected ✓)
- preferred_workout_clock: 07:00
- days_per_week: 4, level: intermediate, started_iso: 2026-05-09

## 🔴 Lab status flags
- Vitamin D 25-OH: 26 ng/mL (low, ref 30-100). Last tested 9 days ago.
  - Why: drives mood, immunity, recovery. Linked to chronic fatigue.
  - Suggested: 5000 IU/day; re-test in 90 days (target 2026-08-09)

## 🟡 Cycle + sleep regularity
- Cycle: luteal, day 22 (regular over last 6 cycles)
- Sleep regularity: 64/100 (yellow). Bed-time stdev 78min over last 14 days.
- Action: pick a wake time within a 30-min window for the next 14 days.
```

## Tools called

- `health_status()` — top-level table counts + imports table
- `health_coach_recent_prescriptions(days=7)` — prescriptions + completion status
- `health_coach_summary(days=28)` — completion rate
- `health_lab_panel(today, lookback_days=180)` — most recent labs per marker
- `health_recommended_labs()` — the WHY for any flagged marker
- `health_cycle_context(today)` — current phase + irregularity
- `health_sleep_regularity(today-14, today)` — bed/wake variance

Plus:
- Read `~/.claude/settings.json` to verify hooks are registered
- Read `~/.claude/hookify-blocks.log` to verify hooks have fired recently
- Read `<VAULT_ROOT>/Meta/coach-profile.yaml` to verify profile state

## When to surface unprompted

The hook system can surface specific flags WITHOUT the user running `/health doctor` explicitly:

- **PostToolUse on any health-mcp call**: if Apple Health is > 28 days stale, surface a one-line nudge ("Re-export Apple Health — last import 31 days ago")
- **SessionStart**: if any lab is out-of-range AND > 90 days old, surface re-test reminder
- **Stop on coach-auto-prescribe**: if today's prescription was created, surface the why_today line

These are hookify nudges, configured separately. The skill is the comprehensive surface; the nudges are the targeted catches.

## Graceful failure

- health-mcp not registered → report "/health-setup first"
- DuckDB empty → report "/ingest-health first"
- No profile → report "/coach profile first"
- No journal entries → skip cycle / Floor sections silently

The doctor never blocks. It always returns a report, even if it's "this is what's missing to get started."

## Voice

Direct. Color-coded flags (🟢 / 🟡 / 🔴) for fast scan. Each yellow / red has a specific "what to do" line, not a vague "consider reviewing." Reader should know exactly what to fix in 30 seconds of reading.
