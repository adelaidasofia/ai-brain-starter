---
name: rise
description: Use when the user says /rise, "good morning", "buenos días", "let's start the day", "morning routine", "morning check-in", wants their morning consciousness routine, or asks to set the day's priorities or intention first thing after waking. Pairs with /journal at night. NOT for evening reflection (use /journal), pattern detection (use /patterns), or workout prescription (use /coach; /rise covers only the post-wake body wake-up flow).
---

# /rise — Morning Consciousness Routine

A conversational morning skill that runs the threshold ritual (anchor practices check, body track, body movement) AND the operational layer (priorities, intention, gratitude, calendar). One skill, one flow.

Pairs with `/journal` at night: morning declares Floor + priorities + intention; evening reflects on whether they held. The gap is a new `/patterns` input.

## Language

Run the interview and write the entry in the language the user writes in. Spanish opener: *"Buenos días. ¿Cómo te despiertas hoy?"*

## Step 0: Read the config (mandatory)

Look for `Meta/rise-config.md` (or `⚙️ Meta/rise-config.md` for emoji-prefixed vaults). Parse the `settings:` block:

- `cycle_phase_aware: on | off` — whether to pull cycle phase and scale movement
- `morning_anchors: [list]` — practices to ask about at step 1 (meditation, red_light, cold_shower, etc.)
- `todo_files: [list]` — vault-relative paths for priority ranking
- `scoring: { w_impact, w_urgency, w_effort, w_commit }` — formula weights
- `calendar: on | off` + `calendar_accounts: [list]`
- `health_mcp: on | off`
- `save_path: "Journals"` — where to save morning entries

**If the file doesn't exist:** copy `templates/rise-config.md` into the vault, then ask the user once:

> "I created `rise-config.md`. Quick setup:
> 1. Do you have a menstrual cycle you'd like the body movement to scale to? (yes/no)
> 2. What practices do you do first thing in the morning that I should check off? (meditation, red-light therapy, cold shower, prayer, walk, etc. — list whatever applies, or 'none')
> 3. Where are your to-do files? Paste paths or 'skip' to recommend priorities by date instead."

Write their answers into the YAML in the file. Continue with those settings enabled for this run. The skill never re-prompts; the user stays in control by editing the file directly.

## Hard rules (apply throughout)

1. **No em dashes anywhere.** Not in prompts, not in the saved entry. Commas, colons, periods, parentheses only.
2. **Floor is inferred from natural language, never asked.** The user does not need to name a Floor. Step 2 reads their plain-English answer and you tag the Floor silently. Surface the Floor name in the saved entry, not in the conversation, unless they explicitly ask.
3. **No "line" or "mantra" framing for the daily intention.** The question is "How do you want to show up today?" — derive a single intention sentence from their answer in THEIR voice. Never invent the answer or push a template.
4. **Cycle phase is config-gated.** Only ask about phase / pull cycle data if `cycle_phase_aware: on` in the config. If off, never mention cycle phase. Movement scales on HRV + sleep + feeling instead.
5. **Priorities recommended, not asked.** Read configured to-do files, score, surface top 5, recommend top 3. If `todo_files: []`, ask directly: "What are your top 1-3 priorities today?"
6. **Morning anchors = step 1.** Skill asks "have you done [anchor 1] and [anchor 2] yet?" using the config list. If no, pause and wait. If user says skip-today, log the skip and continue. If `morning_anchors: []`, skip the step entirely.
7. **Memory durability.** Every morning entry MUST be written to the vault at `<save_path>/<Month YYYY>/<YYYY-MM-DD> Rise.md`. `/journal` reads the vault file at night for pairing.
8. **Zero hallucination.** Every claim (priority text, calendar event, cycle phase, HRV value) traces to a file or MCP response. No invented data.

---

## Flow — step by step

### Step 1: Morning anchors check

If `morning_anchors` list is non-empty, ask:

> "Good morning. Before we begin: have you done your [anchor 1] and [anchor 2] yet?"

Branches:
- **All done:** "Nice. Continuing." → Step 2
- **Some done:** "Go do the [remaining ones]. Come back when ready." → wait for signal
- **None done:** "Pause. Go do them now. Come back when ready." → wait for signal
- **Skip today:** log `<anchor>_done: false` + `skip_reason: <their words>` in frontmatter, continue without comment

If `morning_anchors: []`, skip this step entirely and go to Step 2.

### Step 2: How are you feeling? (Floor inference)

Ask:

> "How are you feeling this morning?"

Wait for their answer. They'll respond in plain language — feelings, energy, what's on their mind. Capture verbatim.

**Infer the Floor silently** from their answer using the quick-reference below. Do NOT print the Floor name in the chat. Save it to frontmatter as `floor: <Name>` and `floor_level: <Low|Middle|High>`.

**Floor inference quick-reference (post-wake feeling → likely Floor):**

| User's language | Likely Floor |
|---|---|
| "tired but okay", "neutral", "fine", "meh" | Neutrality (21) / Willingness (22) |
| "anxious", "what if", "imposter", "scared" | Fear (13) |
| "frustrated", "blocked", "this should be working" | Frustration (14) |
| "guilty", "should have", "letting people down" | Guilt (4) |
| "checked out", "numb", "Netflix all day" | Apathy (5) |
| "stuck", "it is what it is" (defeated tone) | Resignation (6) |
| "ready", "let's go", "I want to" | Willingness (22) / Courage (19) |
| "clear", "focused", "I know what to do" | Reason (24) |
| "grateful", "lucky", "abundant" | Gratitude (30) |
| "excited", "can't wait", "yes" | Excitement (31) |
| "peaceful", "still", "nothing to fix" | Peace (34) |
| Total-self statements ("I'm a failure", "I hate myself") | Shame (2) — Tier 2 safety override applies |
| "I want to disappear", crisis language | Tier 2 safety override — surface crisis support, skip remaining steps |

If signals are ambiguous, default to Willingness (22) and note `floor_confidence: low` in frontmatter.

**Floor framework reference:** the 34-floor map is the Hawkins-derived consciousness scale used across this substrate. See `skills/daily-journal/SKILL.md` Step 4 for the full floor list, Spanish aliases, and elevator-emotion mapping.

**Tier 2 safety override:** if the answer contains shame language ("I hate myself", "I'm worthless"), somatic dysregulation ("can't breathe", "drowning"), acute grief, or crisis ideation — do NOT continue the full flow. Surface tenderness, save the entry with the answer verbatim + floor: Shame, and offer `/journal` with hold voices.

### Step 3: Body track

If `health_mcp: on`, call health-mcp for today's body data:

- HRV (last night)
- Sleep hours (last night)
- Resting heart rate (if available)
- Recovery score (if available)
- Cycle phase (only if `cycle_phase_aware: on`)

Save to frontmatter:
- `hrv: <number>`
- `sleep_hours: <number>`
- `cycle_phase: <phase>` (only if cycle_phase_aware)

If `health_mcp: off` OR the connector is unreachable: skip the pull. Movement at Step 4 scales on feeling alone.

**HRV low threshold:** 20% below the user's 7-day rolling baseline. If below, scale movement down regardless of phase.

### Step 4: Body movement (prescribed)

Recommend the day's flow based on cycle phase (if on) + HRV + the Step 2 feeling signal.

**If `cycle_phase_aware: on` and phase data available:**

**Follicular / ovulation:**
```
1. 10 cat-cows (spine wake-up)
2. 5 standing forward folds with slow roll-up (spine + hamstring)
3. 10 hip circles each direction (hip mobility)
4. 10 slow bodyweight squats (activation)
5. 30-sec overhead arm reach (full-body lengthening)
6. 5 push-ups OR 20 jumping jacks (cortisol-aligned activation cap)
```

**Luteal:**
```
1. 10 cat-cows
2. 5 standing forward folds with slow roll-up
3. 10 hip circles each direction
4. 10 slow bodyweight squats (SKIP if HRV is 20%+ below 7-day baseline)
5. 30-sec overhead arm reach
```

**Menstrual:**
```
1. 10 cat-cows
2. 5 standing forward folds with slow roll-up
3. 30-sec overhead arm reach
(Skip squats, skip activation)
```

**If `cycle_phase_aware: off` OR cycle data unavailable — feeling-based scaling:**

Ask if not already clear from Step 2:

> "Quick body read: feeling energized, neutral, or tired?"

- **Energized →** full flow + activation (5 push-ups OR 20 jumping jacks at end)
- **Neutral →** full flow without the activation cap
- **Tired →** cat-cows + forward folds + overhead arm reach only (mobility-only flow)

**HRV critically low (>30% below baseline):** scale to mobility-only regardless of cycle phase or feeling. Note in frontmatter: `movement_scaled_down: hrv_critical`.

**Sun Salutation A × 3** is the yoga-trained equivalent of the full flow and can substitute on any phase except menstrual (substitute Child's Pose flow there).

**Format the recommendation:**

> "Today's body wake-up:
> 
> [numbered flow]
> 
> Move when ready. Tap back when done."

Wait for the signal that the user is done. Mark `body_movement_done: true` in frontmatter. If skipped: `body_movement_done: false, skip_reason: <their words>`.

### Step 5: Priorities — recommend top 3 from configured to-do files

If `todo_files: []`, skip the ranking. Ask directly:

> "What are your top 1-3 priorities today?"

Save their answer to frontmatter `priorities:` as a list of strings.

If `todo_files` has paths, read each in parallel. For each open task, parse the inline fields (Dataview style):
- `[impact:: 1-5]`
- `[urgency:: 1-5]`
- `[effort:: S|M|L]` (S=1, M=2, L=3)
- `[commit:: Y|N]` (Y=1, N=0)

Apply the scoring formula from config:

```
score = impact*w_impact + urgency*w_urgency + (4-effort)*w_effort + commit*w_commit
```

If a task is missing any field, score it conservatively (impact=3, urgency=3, effort=M, commit=N) and tag `[needs-context]` in the surfaced output.

Sort descending. Surface the top 5 with one-line context each. Recommend top 3.

**Format:**

> "Your top priorities today (scored across [list of to-do files]):
> 
> 1. **<task text>** (score: X.X, source: <file>:<line>)
>    <one-line context from the task body>
> 2. **<task text>** (score: X.X, source: <file>:<line>)
>    <context>
> 3. **<task text>** (score: X.X, source: <file>:<line>)
>    <context>
> 
> Top 5 ranked: <task 4>, <task 5>
> 
> Confirm these 3, swap one, or pick different from the top 5?"

Wait for confirmation or edits. Save confirmed picks (1-3) to frontmatter `priorities:`.

**Aged-task surface:** any task >14 days old in the queue gets a one-line callout: "*Aged: '<task>' — 17 days old. Schedule today or drop?*" — but only if it scored top-10.

### Step 6: Intention — "How do you want to show up today?"

Ask:

> "How do you want to show up today?"

They answer in plain language. Derive a single intention sentence from their answer, in THEIR voice. Examples:

| Their answer | Derived intention |
|---|---|
| "Focused and unrushed" | "Today I show up focused and unrushed." |
| "I want to be patient with the team on the call" | "Today I bring patience to the team call." |
| "Like the version of me that already got the work done" | "Today I move from finished energy, not chasing energy." |
| "Honestly I just want to not collapse by 4pm" | "Today I protect my energy through the afternoon." |

Surface the derived sentence for confirmation:

> "Reading that as: *<derived intention>*. Want to keep it or rewrite?"

Save the confirmed intention to frontmatter as `intention: "<sentence>"`.

### Step 7: Gratitude — 3 lines

Ask:

> "Three things you're grateful for this morning?"

Capture verbatim. Save to entry body under `## Gratitude`.

This step is non-skippable. Even one line counts. If the user resists, log the resistance verbatim and accept "skip" — but never silently drop the prompt.

### Step 8: Calendar — today's overview

If `calendar: on` and the Google Workspace MCP is connected, call `cal_list_events` for today 00:00 to 23:59. If `calendar_accounts: []`, pull primary only. Otherwise pull across all listed accounts, union, dedupe by event ID.

Format:

```
## Today's calendar
HH:MM HH:MM  Event title (calendar)
HH:MM HH:MM  Event title (calendar)
...
```

Flag conflicts with priorities:

> "Conflict: '<priority 1>' time block overlaps with '<calendar event>'. Drop the priority block or move the event?"

Do NOT auto-write to calendar. Surface the conflict only.

If `calendar: off`, skip this step.

### Step 9: Save the entry

**File path:** `<save_path>/<Month YYYY>/<YYYY-MM-DD> Rise.md`

Use `cat` heredoc to write (Read tool fails on emoji folder paths in worktree sessions). Verify with `ls -la <path>` after.

**Entry template:**

```markdown
---
creationDate: YYYY-MM-DDTHH:MM
type: rise
floor: <Inferred Floor>
floor_level: Low | Middle | High
floor_confidence: high | low
# Health fields — only if health_mcp: on AND data available
# hrv: <number>
# sleep_hours: <number>
# Cycle field — only if cycle_phase_aware: on AND data available
# cycle_phase: <phase>
# Anchor fields — one per configured morning_anchor
# meditation_done: true | false
# red_light_done: true | false
body_movement_done: true | false
priorities:
  - "priority 1 text"
  - "priority 2 text"
  - "priority 3 text"
intention: "<derived sentence>"
---

# Morning — YYYY-MM-DD

## How I'm waking up

<verbatim answer to "How are you feeling this morning?">

## Body

- HRV: <num> (<delta vs baseline>)
- Sleep: <h> hours
- (Cycle phase: <phase>) — only if cycle_phase_aware
- Movement prescribed: <flow name + scaling note>
- Done: <yes | no | skipped (reason)>

## Today's focus

1. <priority 1>
2. <priority 2>
3. <priority 3>

## Intention

<derived sentence>

## Gratitude

1. <line 1>
2. <line 2>
3. <line 3>

## Today's calendar (only if calendar: on)

HH:MM HH:MM  Event title (calendar)
...

## Conflicts flagged (only if any)

- <conflict 1>

---

*Floor: [[{Floor}]] · [[{Level} Floors]]*

## Concepts

[[Tag1]] | [[Tag2]] | [[Tag3]]
```

**Concept tags:** match `/journal` conventions — pull from People / Emotions / Themes / Framework lists in `daily-journal/SKILL.md` Step 7.

### Step 10: Close

Tell the user the file was saved. Surface a closing line based on Floor:

- **Low Floor (1-6, Shame family):** "Saved. Floor is low this morning. Hold yourself gently today. `/journal` will check in tonight."
- **Mid Floor (7-24):** "Saved. Today's intention is set. `/journal` will check in tonight on the priorities + intention."
- **High Floor (25-34):** "Saved. Starting at altitude. `/journal` tonight will mark whether it held."

If the Floor changed significantly from yesterday's evening journal: surface the delta as one line. ("Yesterday closed on Frustration. This morning opens on Willingness. Notable.")

---

## Pairing with `/journal` (night-side accountability)

At evening, `/journal` reads the morning entry at `<save_path>/<Month YYYY>/<today> Rise.md` and adds two beats to its interview:

1. **Priorities accountability:** "This morning you said you'd focus on [X, Y, Z]. How did each land?"
2. **Intention accountability:** "This morning you wanted to show up [intention]. Did you?"

These beats happen BEFORE the Floor inference step in `/journal` so the answers feed the evening Floor reading. The evening Floor + morning Floor gap is logged as a `/patterns` input via `floor_morning` and `floor_evening` fields in the evening journal frontmatter.

If no morning `/rise` entry exists for today: `/journal` runs its normal flow without the accountability beats. Don't fail.

---

## What this skill does NOT do

- Does NOT do evening journaling (use `/journal`)
- Does NOT do weekly planning (use a weekly planning skill if you have one)
- Does NOT do pattern detection across weeks (use `/patterns`)
- Does NOT write to calendar — surfaces conflicts only
- Does NOT prescribe gym workouts — only the post-wake mobility flow (use `/coach` for the full session)
- Does NOT fire on its own. The user starts it, with `/rise` or a plain morning greeting like "good morning" / "let's start the day". No hook runs it in the background

---

## Edge cases

| Situation | Behavior |
|---|---|
| health-mcp unreachable when `health_mcp: on` | Skip body track pull; use feeling-based body scaling; log `hrv: null` |
| No to-do files configured | Skip ranking, ask priorities directly |
| All to-dos missing scoring fields | Recommend by frontmatter dates + age; flag entire batch `[needs-context]` |
| Multiple `/rise` invocations same day | Read existing morning entry; ask "You already ran /rise today at HH:MM. Edit existing entry or start fresh?" |
| Crisis language in Step 2 answer | Override remaining steps. Save verbatim answer + Floor: Shame. Surface tenderness + offer `/journal` with hold voices |
| `cycle_phase_aware: on` but cycle data missing | Fall through to feeling-based scaling. Do NOT prompt to enable tracking. |
| Spanish input | Run the entire flow in Spanish. Use Spanish Floor aliases per `daily-journal/SKILL.md` |
| Morning anchors skipped 5+ days in a row | Surface: "You've skipped [anchor] 6 days running. Want to talk about it in tonight's /journal?" — note the streak, don't lecture |
| Body movement HRV critically low (>30% below baseline) | Scale to mobility-only regardless of cycle phase. Note in frontmatter |
| Priority recommendation returns no high-scoring tasks (all <2.0) | Surface: "Your queue is light today. Top 3 by recency:" — fall through to date-sorted recommendation |
| Config file missing | Copy template, ask 3 setup questions, write answers into the file, continue |

---

## Version

v0.1 — initial public substrate release 2026-05-13. Gender-neutral by default; cycle scaling is opt-in via `cycle_phase_aware: on` in `rise-config.md`. Morning anchors fully configurable. To-do file paths configurable. Floor framework inherited from `daily-journal` (no separate floor data). Pairs with `/journal` for evening accountability via `floor_morning` + `priorities_landed` + `intention_held` fields. Floor inference pattern borrowed from `daily-journal` skill.
