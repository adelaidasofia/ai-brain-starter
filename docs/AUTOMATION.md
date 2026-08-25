---
name: automation
description: How the health stack auto-triggers without the user remembering commands
---

# Automation — health stack v0.5

The substrate's health stack has 41 tools, 5 skills, and 2 auto-trigger hooks. The point of the hooks is so the user doesn't need to remember the tools or the skills. Whenever they're in Claude Code at all, the chain fires.

This doc explains the auto-trigger chain, what fires when, and how to verify it's working.

## The chain

Single entry point: the user's existing daily `/journal` habit. The chain runs once per day, gated to the moment the user actually engages with the substrate.

```
User opens Claude Code at some point in the day (any number of sessions)
    │   (SessionStart fires no health hooks — pure overhead reduction
    │    for users with ~20 sessions/day. Codified 2026-05-10.)
    │
    ▼
User types /journal (existing daily habit)
    │
    ▼
Stop hook fires when /journal completes
    │
    ▼
hooks/coach-auto-prescribe-on-journal.py runs three steps in order:
    │
    │   1. Wearable sync — checks last Oura import age, last Fitbit
    │      import age. For each vendor with credentials set AND data > 24h
    │      stale: pulls yesterday → today catch-up window. Apple Health
    │      skipped (requires iOS interaction; hookify nudges on stale).
    │
    │   2. Journal backfill — runs scripts/backfill-journal-body-context.py
    │      for yesterday only. Appends Body track section below yesterday's
    │      verbatim content (substrate journal-verbatim rule preserved).
    │
    │   3. Coach prescription — reads <vault>/Meta/coach-profile.yaml.
    │      Checks if today's prescription exists. If not, fires the
    │      coach.decide_workout_type decision tree (recovery + sleep +
    │      cycle phase + somatic state + body_says_slow_down + Floor
    │      qualifier from today's journal).
    │
    ▼
If profile.calendar_drop=true AND google-workspace MCP connected:
    │   Today's workout drops to Google Calendar at preferred_workout_clock.
    │
    ▼
Done. Body context in yesterday's journal. Workout in calendar.
Single trigger. Zero remembering.
```

## Why /journal is the gate (not SessionStart)

The v0.5.0 build initially wired the wearable sync on SessionStart. The panel correction (Karpathy + Patrick Collison + Naval, 2026-05-10): a user with ~20 sessions/day was paying 20× the python-process overhead for a logic that only needs to run once daily.

The fix (v0.5.1): tie the chain to `/journal` — the user's existing once-daily habit. Three benefits:

- **Once-daily firing.** No 20× overhead.
- **Substrate philosophy preserved.** "If you don't journal, you miss out" — the substrate doesn't work harder than the user. If the user skips /journal Sunday, Monday's `/coach today` reads from Saturday's data and `/health doctor` shows the staleness.
- **Single failure surface.** One hook to verify, one trigger to debug, one cadence to remember.

Howard Marks's dissent on the panel was load-bearing: name the cost. The cost is "if you skip /journal for 2 days, your wearable data is 2 days stale and your prescription is off." That's tolerable, and `/health doctor` surfaces it loudly.

## The opt-in SessionStart sync (power-user only)

`hooks/health-auto-sync.py` ships in the repo and is fully tested but is NOT wired in the default `hooks.json` template. Users who want per-session sync (e.g., never journals but wants the data fresh) can manually add it to their `~/.claude/settings.json` SessionStart block. Documented at the bottom of this file.

> Why it stays out by default is the general SessionStart resource-governance rule: a SessionStart hook fires once per session, so a machine running many concurrent sessions runs it many times at once. Heavy or network work (a corpus walk, a wearable sync) belongs on a scheduled job or once-daily Stop hook, never the cold-start path. The full invariant, the shipped-set audit, and the regression guards that enforce it are in [HOOK_FLEET_RESOURCE_GOVERNANCE.md](HOOK_FLEET_RESOURCE_GOVERNANCE.md).

## What auto-fires vs what stays manual

**Auto:**
- Wearable sync (Oura, Fitbit) — on every SessionStart, if data is > 24h stale
- Daily prescription — when user types /journal (or equivalent)
- Yesterday's body-track backfill — when user types /journal
- Calendar drop of today's workout — same trigger, if configured
- /weekly + /monthly insights body track — already auto-fires via section 0d in the insights skill
- health-context auto-fires inside /journal, /coaching, /panel, /patterns (existing wiring)

**Manual (by design — Bainbridge panel dissent integrated):**
- `/coach log` — logging RPE + lift actuals after a session. The body stays in the loop; the substrate never auto-logs completion.
- Apple Health re-export — requires iOS interaction, can't be triggered from desktop. The hookify nudge surfaces a reminder every 28 days.
- Lab import — manual CSV upload from patient portal. Hookify reminds when an out-of-range marker is > 90 days without a re-test.
- One-time backfill of the full year (`/backfill-journal-body-context`) — runs once per year-window, then daily backfill takes over.
- `/health-setup` for first-time install (vendor + OS picker) — manual because the user is doing OAuth flows + setting env vars.

## Failure modes + recovery

**1. Wearable API down at session start.** Hook catches exception, exits silently. Next session retries. No noise, no broken state.

**2. User skips /journal one day.** Tomorrow's /journal triggers the chain for two days at once (yesterday backfill + today prescription). No missed days.

**3. User doesn't open Claude Code for a week.** Next session pulls 7 days of wearable backfill in one shot. SessionStart hook handles ranges, not just single days.

**4. health-mcp not yet installed.** Hooks exit silently. Once `/health-setup` is run, hooks start firing.

**5. Coach profile not yet set.** Stop hook exits silently. Once `/coach profile` is run, hooks start firing.

**6. Calendar drop fails (google-workspace MCP not connected).** The prescription is still created in the DB and surfaces on next /coach today. The hook only TRIES the calendar drop; failure doesn't block.

## How to verify

Run `/health doctor` to see the freshness of every source, last prescription + completion, which hooks are installed, and any out-of-range labs needing attention. Color-coded green / yellow / red so you can scan in 30 seconds.

## How to install

The default wired set lives in `hooks.json` at the repo root — that's the file `scripts/install-hooks-user-level.py` reads to write entries into `~/.claude/settings.json` during bootstrap, and it's what `services/health-mcp/tests/test_v05_hooks.py` tests against. By default it wires one entry:

- Stop (matcher: `mcp__.*journal|daily-journal|journal`) → `coach-auto-prescribe-on-journal.py`

`health-auto-sync.py` (SessionStart) is opt-in, not part of this default wiring — see "The opt-in SessionStart sync" above. Add it to your own `~/.claude/settings.json` SessionStart block if you want per-session wearable refresh.

To remove the default auto-trigger: edit `~/.claude/settings.json` and drop the Stop entry above. Skills and tools keep working manually.

## Opt-in: scheduled tasks instead of hooks

If you prefer cron-style scheduling (machine on, network up, fires at fixed times regardless of whether you open Claude Code), you can wire scheduled tasks via the `/schedule` skill. Suggested cadence:

- `_health-daily-sync` — 06:00 daily, runs `health_import_oura(yesterday, yesterday)` + `health_import_fitbit(yesterday, yesterday)`
- `_coach-daily-prescription` — preferred_workout_clock - 30min, runs `/coach today`
- `_coach-weekly-plan` — Sundays at 18:00, runs `/coach week`

The hook path is preferred because it doesn't depend on the machine being on at a fixed time, but both can coexist (the substrate is idempotent on duplicate runs).

## Why this matters

A user who has to remember `/coach today` every morning will forget by day 4. A user whose workout appears in their calendar at 7am without them doing anything will train. Capability without trigger isn't deployed; it's a museum.

The Bainbridge dissent at the original panel (2026-05-10) was load-bearing: auto-trigger the ANALYSIS, never auto-trigger the ACTION. The substrate prepares the workout and surfaces it; you decide whether to do it. That's the line.
