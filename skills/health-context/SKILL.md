---
name: health-context
description: 'Use when a host skill that benefits from biometric context runs: daily-journal (/journal), coaching, advisory-panel (/panel), patterns, or insights (/weekly, /monthly). Also use when the user asks how HRV, sleep, recovery, resting heart rate, steps, or workouts relate to a journal entry, coaching window, decision, pattern, or weekly review. Companion layer, never standalone. Setup: health-setup. Data import: ingest-health. Sync diagnostics: health-doctor.'
---

# health-context, biometric context for substrate skills

Wraps the vault-aware tools in `health-mcp` so other skills can pull biometric context without each one re-implementing the connection logic.

This skill is invoked WITH another skill, never standalone. It's the "look up what the body was doing during this moment" layer.

## When to use

- User invokes `/journal` or `/daily-journal`: pull `health_journal_context(today)` and prompt with HRV, sleep duration, sleep efficiency, steps, workouts
- User invokes `/coaching`: pull `health_coaching_context(start, end, vault_root)` for the coaching window
- User invokes `/panel` or strategy/decision question fires the advisory panel: pull `health_panel_context(today, vault_root)` for delta-vs-7d
- User invokes `/patterns`: pull `health_floor_correlation(metric, days, vault_root)` for HRV-vs-Floor and other biometric-vs-Floor patterns
- User invokes `/weekly` or `/monthly` (insights): pull `health_weekly_rollup(week_start)` and fold into the review

Do NOT use for:
- Querying live data without a prior import (suggest `/ingest-health` first)
- Writing to the vault (this skill never writes)
- Replacing medical advice (the scores are directional, not diagnostic)

## Graceful degradation

If health-mcp is not registered, has no data, or the tool call errors:

- Fail silently. Continue the host skill (journal, coaching, etc.) without health context.
- Surface a one-line note in the host skill's output: "(health context not available; run /ingest-health to set up)"
- Never block the host skill on missing health data.

## Composition by host skill

### Inside daily-journal

When the journal skill assembles its prompt, call `health_journal_context(date)` for today. Add to the prompt:

```
Body context (today, from health-mcp):
  HRV: 28ms (vs 42ms 30-day baseline -- 33% below)
  RHR: 65 bpm
  Sleep: 5h 12m asleep (efficiency 87%, REM 38m, deep 22m)
  Steps: 4,820
  Workouts: 0
  Mindful: 0 min
```

This becomes a journal-prompt input: "Your body had a tough night. How does that map to what you noticed?" The journal skill owns the prompt; health-context owns the data fetch.

### Inside coaching

When the coaching skill spans a multi-day window (e.g. last 14 days of accumulated tension), call `health_coaching_context(start, end, vault_root)`. Surface:

- Average recovery score over the window
- Days with low recovery (< 50)
- Days with low restorative sleep (REM + deep < 60 min)
- Floor distribution from journals in the window

This gives the coaching skill a "body track" alongside the emotional track.

### Inside advisory-panel

When a panel fires on a strategy/decision moment, call `health_panel_context(today, vault_root)`. Surface:

- Today's recovery score
- 7-day average
- Delta (today minus 7-day avg)
- Today's Floor tag (if a journal exists for today)

If `delta < -10` AND `today's floor_level` is in the lower third, the panel can fold "your body and floor are both low; consider deferring high-stakes calls" into its synthesis. Never override the user's decision; only inform.

### Inside patterns

When the patterns skill runs, call `health_floor_correlation(metric, days, vault_root)` for each of: HRV, RHR, sleep duration, steps, recovery score. Surface any correlation with `n >= 10` and `|r| >= 0.25`. Examples the patterns skill can render:

- "Low-Floor days correlate with low HRV (r=-0.42, n=38)"
- "Steps below 5k correlate with floor level <= 6 (r=-0.31, n=42)"

### Inside insights (weekly / monthly)

When the insights skill runs `/weekly` or `/monthly`, call `health_weekly_rollup(week_start)` for each week in the review. Add to the rendered review:

- HRV avg / min / max
- RHR avg / min / max
- Steps total / daily-avg
- Workout count + total minutes
- Recovery trend (positive / flat / negative)

## vault_root

Every vault-aware tool needs `vault_root`. Resolve it from:

1. The host skill's environment, if it sets `VAULT_ROOT`
2. The CLAUDE.md path's parent directory (search up from cwd)
3. Ask the user only if neither is available

Never hardcode a path. Pass as a tool argument every call.

## Invocation pattern

This skill does not have its own `/` slash command. It piggybacks on the host skill via the host skill's invocation. The host skill MUST call health-context at the start of its prompt-assembly phase, before LLM synthesis, so the body data is available throughout.

Example (inside daily-journal's orchestrator):

```python
# Inside daily-journal
try:
    body = await call_tool("health_journal_context", {"date_str": today.isoformat()})
    prompt += f"\nBody today: HRV {body['hrv_ms']}ms, RHR {body['rhr_bpm']} bpm, sleep {body['sleep_asleep_min']}min..."
except Exception:
    # health-mcp not available; continue without
    prompt += "\n(health context not available; run /ingest-health to set up)"
```

## Privacy

Reads only. Never writes the vault, never writes the health DB, never sends data over the network. Health Auto Export TCP-live is the one exception, and that's local-Wi-Fi only.
