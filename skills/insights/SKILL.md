---
name: insights
description: Use when the user types /weekly or /monthly, asks for a weekly or monthly journal review or retrospective, asks 'how was my week' or 'how was my month', or wants trends, anomalies, or patterns across recent journal entries. Also fires when regenerating an insight report for a past week or month. NOT for writing today's entry (use daily-journal), cross-session pattern extraction (use patterns), or operational/team reviews.
argument-hint: "[week or month -- e.g. 'this week', 'last month', or leave blank for default]"
---

When the user types /weekly or /monthly, generate an insight report from their recent journal entries.

## Language

Generate the entire report in the language the user writes in. If Spanish, all sections — coach, therapist, panel, floor notes — are in Spanish.

**Spanish floor aliases:** Asco (1) · Vergüenza (2) · Bochorno (3) · Culpa (4) · Apatía (5) · Resignación (6) · Confusión (7) · Soledad (8) · Aburrimiento (9) · Duelo (10) · Decepción (11) · Herida (12) · Miedo (13) · Frustración (14) · Deseo (15) · Rabia (16) · Desprecio (17) · Orgullo (18) · Valentía (19) · Esperanza (20) · Neutralidad (21) · Disposición (22) · Aceptación (23) · Razón (24) · Confianza (25) · Compasión (26) · Humildad (27) · Pertenencia (28) · Amor (29) · Gratitud (30) · Entusiasmo (31) · Asombro (32) · Alegría (33) · Paz (34)

Floor wikilinks in the report use Spanish aliases in Spanish: `[[Miedo]]`, `[[Valentía]]` etc.

## For /weekly -- read all journal entries from the current calendar week (Monday-Sunday). If today is Monday or Tuesday, default to the previous week (since there's barely any data yet). The user can specify "this week" to override.
## For /monthly -- read all journal entries from the current calendar month (1st-last day). If today is the 1st-3rd, default to the previous month. The user can specify "this month" to override.

Journal entries are in: `[VAULT_PATH]/Journals/`

## CRITICAL: How to find entries by date

**DO NOT grep thousands of files.** Use the journal index instead.

### Step 0: Load the journal index
Read `[VAULT_PATH]/⚙️ Meta/journal-index.json` (or `[VAULT_PATH]/Meta/journal-index.json` on vaults without an emoji-prefixed Meta — the same folder `build-journal-index.py` writes to). Structure: `{"total": N, "last_updated": "YYYY-MM-DD", "entries": [{file, date, floor, floor_level}, ...]}`. Access entries via `idx["entries"]`, then filter by `entry["date"]`.

If the index doesn't exist or is more than 7 days old, rebuild it first:
```bash
/usr/bin/python3 "[VAULT_PATH]/Meta/scripts/build-journal-index.py"
```

### Step 1: Filter entries by date range
From `idx["entries"]`, filter where `entry["date"]` starts with the target YYYY-MM (monthly) or falls within the target Mon-Sun range (weekly).

**Floor counting — primary vs. arc.** Each entry has one primary `floor` (where it landed). Entries that moved also carry `floor_arc`, the ordered path of floors they passed through. Compute BOTH views and name which one a number is: (a) **landed-floor distribution** counts the primary `floor` once per entry; (b) **floors-touched distribution** expands `floor_arc` — count +1 for every floor in the arc, and an entry with no arc contributes just its primary floor. Do NOT collapse an arc to a single item, and treat a legacy list-form `floor: [A, B, C]` the same way (expand it, never count as one). Verify by running a Python script against the index, never hand-counting.

### Step 2: Read ONLY the matching files
Read the full content of each matching file. Do NOT read files outside the date range. With the index, you're reading 5-15 files instead of searching the entire vault.

### Step 2b: Pull RescueTime + session time data (if available)

**If the RescueTime MCP is connected:** Try calling `mcp__rescuetime__get_daily_summary` for each day. If the MCP is disconnected or returns errors, note "RescueTime unavailable for this period" in the report and skip all RescueTime-dependent sections. Do NOT silently omit. For weekly, also call `mcp__rescuetime__get_productivity_trend` with `days: 7`.

**If a Time Tracking file exists** (check CLAUDE.md for the path, typically `⚙️ Meta/Time Tracking.md`): Read it and filter entries for the period. This shows what categories were worked on during Claude Code sessions (Writing, Business, Vault, Personal, Admin). Merge with RescueTime app data for a combined picture: RescueTime shows which apps were used, session logs show what purpose they served.

Add to the report if data is available:
- Average daily Productivity Pulse for the period
- Total productive vs. distracting hours
- Top 3 apps by time
- Session time breakdown by category
- Notable gaps or mismatches (e.g., "12h in Obsidian but only 3h tagged as Writing in sessions")

### Step 2e: Token usage report (weekly + monthly, optional)

If the vault has `scripts/token-usage-report.py`, run it to surface real per-session token consumption + estimated USD cost from Claude Code session JSONLs:

```bash
# /weekly: 7-day window
VAULT_ROOT="<VAULT_PATH>" python3 "<VAULT_PATH>/scripts/token-usage-report.py" --days 7 --top 10

# /monthly: 30-day window
VAULT_ROOT="<VAULT_PATH>" python3 "<VAULT_PATH>/scripts/token-usage-report.py" --days 30 --top 20
```

The script writes `⚙️ Meta/Token Usage Report.md`. Read it and surface a compact block in the report. If `Opus cost share` >60% or `Sessions ≥60 turns` >3, surface as a coach line in Section 3 (model-routing or session-length drift). Single-session cost outliers (>$200) get one bullet in Section 2.

This replaces guess-work with real numbers from `~/.claude/projects/<vault-hash>/*.jsonl` (the only ground-truth source for token consumption — Claude Code does not surface this in any UI). Inspired by JuliusBrussee/caveman's /caveman-stats.

### Step 2f: Drift compression candidates (weekly + monthly, optional)

If the vault has `scripts/compress-vault-doc.py`, run the compression-candidate sweep on docs flagged by Drift Audit:

```bash
VAULT_ROOT="<VAULT_PATH>" python3 "<VAULT_PATH>/scripts/compress-vault-doc.py" --auto-from-drift --dry-run
```

If candidates show >2KB savings, surface as a compact block. If <2KB, skip silently.

### Step 2g: Baseline + anomaly computation (LEAD-WITH SIGNAL)

**The reason this skill exists is to surface insight, not to summarize.** A report that says "you had 6 Courage entries" is data. A report that says "Courage was 25% in the prior three months, dropped to 3% this month" is signal. That's the lead.

If the vault has `scripts/monthly-baseline.py`, run it for the target period:

```bash
VAULT_ROOT="<VAULT_PATH>" python3 "<VAULT_PATH>/scripts/monthly-baseline.py" --month YYYY-MM --pretty
```

The script outputs floor distribution shifts ≥3pp from baseline, word-frequency anomalies ≥2× or ≤0.5× baseline, numeric metric deltas ≥10%, activity deltas, top people mentions, and missing-data flags. **Use this output as the FIRST DATA SECTION** (sections 0a/0b/0c below). Don't bury it under "Month at a glance." Anomaly-led framing means the reader sees what's different before they see what's present.

### Step 2c: Data availability check
Before writing the report, check which data sources exist for the target period:
- Time Tracking file: does it have entries for this month?
- Deep Work Chain: does it have entries for this month?
- Decisions folder: do any decision files fall in this period?
- Skill usage log: any entries for this period?
- RescueTime MCP: is it connected?

If a data source has no entries for the period, skip that section silently. If 3+ data sources are missing, add a one-line note at the top: "Note: some tracking systems started after this period. Sections that depend on them are omitted."

## Report Structure

**REPORT OPTIMIZATION FUNCTION:** Optimize for INSIGHT (anomaly + delta + counterfactual + said-vs-did), NOT for COMPLETENESS (cover every section). A great report surfaces 3-5 things you didn't already know. Sections 0a, 0b, 0c are the load-bearing ones. Sections 1, 1b, 6 are appendix material. Don't lead with summary content. If a section has nothing genuinely surprising, write one short line and move on rather than padding.

The audit shaping every section: would this observation surprise the reader? If no, it's summary not insight. Cut it or compress it.

### 0a. Anomalies + deltas (LEAD WITH THIS)

From the Step 2g baseline output. Surface the floor shifts, word-frequency spikes, numeric metric changes, and activity deltas — but synthesize them, don't just paste the table. Format as 3-5 bullet points:

- "Courage was 25-49% in the prior three months. Dropped to 3% this month. Hope/Excitement/Gratitude filled the space. The center of gravity shifted from white-knuckle pushing to open-arm receiving."
- "[Person X] mentioned 32× more than baseline. Their arc dominated everything else."
- "Health steps dropped 42%. Body got quiet while the emotional/professional center got loud."

Each bullet names the data, the magnitude, AND the hypothesis the data invites. Don't stop at "X happened" — say "X happened, here's what it might mean."

### 0b. Said vs. did (counterfactual check)

Pull commitments from the period:
- Decision Log entries (`⚙️ Meta/Decisions/*.md`) where outcome is blank or stale
- Journal verbatim "I will" / "I'm going to" / "by [date]" statements
- Current Priorities deadlines that were active during the period

For each commitment: what was the outcome? Format as a 2-column comparison. Surface gaps explicitly. A missed deadline or stalled decision is signal, not an error to skip.

This is the section that converts "summary of what happened" into "report card on what was said." Without this, the skill produces narrative without accountability.

### 0c. Hidden connections (graph + co-occurrence)

For the period, surface:
- People → floor correlations (top 3 people from baseline output, mean floor of entries mentioning each)
- Topic clusters → floor (e.g., "raise" entries averaged Fear; "consulting" entries averaged Excitement)
- Day-of-week patterns if visible
- One non-obvious co-occurrence the GRAPH_REPORT.md flags (run /graphify query if needed)

Goal: surface 1-2 connections invisible from sequential reading. The graph sees aggregate patterns the diary doesn't.

### 0d. Body track (health-mcp)

The body has its own data and its own arc. When biometric / cycle / sleep / nutrition / lab data is available, it answers questions journals alone can't: which Floor-low days were physiology and which were content; whether the recovery score's "rest more" was the right prescription or whether the actual signal was "eat enough"; whether a Floor pattern this month is anniversary-coupled to the same month two years ago; whether a nutrient deficiency is masquerading as a low-mood pattern.

**If the `health` MCP is not registered or returns errors, skip this entire section silently.** Graceful degradation, never block the main report.

#### Tools to call (weekly)

- `health_weekly_rollup(week_start)` — HRV / RHR / sleep / steps / workout aggregates + recovery_trend
- `health_sleep_regularity(week_start, week_end)` — bed/wake variance, mean latency, nap count, regularity_score
- `health_nutrition_summary(week_start, week_end)` — daily macros + under_fuel_signal
- `health_cycle_context(today)` — current phase + cycle-day + irregularity
- `health_floor_correlation("HKQuantityTypeIdentifierHeartRateVariabilitySDNN", days=30, vault_root)` — HRV vs Floor over 30d
- `health_lab_panel(today, lookback_days=180)` — most recent lab values per marker with status (low / in_range / high)

#### Tools to call (monthly only — additional)

- `health_long_window("HKQuantityTypeIdentifierHeartRateVariabilitySDNN", years=2)` — YoY same-month + persistent_asymmetry signal
- `health_long_window_with_journal("HKQuantityTypeIdentifierStepCount", years=2, vault_root)` — Floor distribution by month + YoY metric
- `health_longevity_panel(today)` — VO2Max, walking speed, walking steadiness, lean mass, Zone 2 minutes, 6-min walk
- `health_phase_means("HKQuantityTypeIdentifierHeartRateVariabilitySDNN", days=90)` — cycle-phase segmented HRV
- `health_symptom_correlation(symptom_type, days=90, vault_root)` — for each symptom that appeared 3+ times this month, run the correlation

#### Synthesis instruction (HELPFUL, NOT JUST COOL)

The reason this section exists is to change what the user does tomorrow, not to display body numbers. Lead with patterns that pair body data with the Floor/journal track in ways neither could surface alone.

Surface 3-7 bullets, each shaped:
- **Pattern named** — concrete, with magnitudes from the tool calls
- **What it might mean** — the hypothesis the data invites (cycle phase, under-fueling, anniversary coupling, lab deficiency, sleep debt, etc.)
- **What to do about it** — one specific next-action. Not "be aware." Not "consider." A behavior or a check.

Worked examples (not for the report — the SHAPE):

> **HRV ran 18% below baseline mid-period — and you were in mid-luteal phase.** Cycle-phase context (Sims) says luteal HRV dips are physiology. The recovery score over-fired here. **Don't trust the "rest more" prompts during luteal week 3 unless RHR also climbed.**

> **Floor 4 (Fear) days correlated r=-0.42 with HRV across 12 paired observations.** On Fear days, sleep latency was 23min vs 11min average. The body is registering threat before the journal names it. **For the next two weeks, when sleep latency runs >20min, write before bed instead of in the morning.**

> **5 of 14 days you ate under 70% of energy burned. Recovery score recommended rest 4 of those days.** This is the under-fuel signal — recovery wants you to eat enough, not rest more. **Set a daily kcal floor of [basal+active*0.8] until HRV returns to baseline.**

> **HRV is 14% below same-month-last-year AND same-month-two-years-ago. Third-year asymmetry.** Persistent-asymmetry detector flagged it. Anniversary-pattern hypothesis (van der Kolk). **What was happening this time of year in [year-3]? `/deconstruct` on the period.**

> **Vitamin D 25-OH = 28 ng/mL (ref 30-100). Below range.** Vitamin D deficiency drives mood, immunity, and recovery. The persistent low-Floor pattern this month may have a metabolic floor under it. **Get tested again in 3 months after supplementing 5000 IU/day; flag with your physician.**

> **Sleep-regularity score 52/100. Bed-time stdev 87min, wake-time stdev 72min.** Body is in chronic low-grade jet lag. The recovery formula doesn't capture this — single-night scores look fine. **Pick a wake time within a 30-min window for 14 days; the score should climb 20+ points.**

> **VO2Max trended down 4% over 90 days. Zone 2 minutes 124/week (target 180+).** Cardio capacity is the priority longevity lever and it's drifting. **One Zone 2 walk (60-70% HRmax) per workday for the next month — measurable in the next monthly review.**

#### Banned shapes for this section

- Listing biometric numbers without an interpretation hypothesis ("HRV was 38ms; sleep was 6h 10m") — that's data, not insight
- Generic "stay hydrated" / "get more sleep" advice unconnected to the period's actual data
- Pretending labs cause symptoms when ranges are in-range
- Citing the recovery score as ground truth when cycle phase or under-fueling explains the dip
- "Talk to your doctor" as the entire prescription. Always pair with one specific behavioral next-action OR one specific data check

#### Cycle-phase qualifier (always apply if cycle data exists)

When `health_cycle_context(today)` returns a phase, ALL biometric interpretations in this section must be qualified by phase. Saying "your HRV dropped" without naming the phase is medically incomplete and the substrate gaslights its menstruating users without it. The phase tag is the first line of any biometric finding.

#### Lab status flags (always surface ANY out-of-range lab)

If `health_lab_panel(today)` returns ANY marker with `status: low` or `status: high`, surface it as its own bullet with the WHY for that marker (pull from `health_recommended_labs` if useful) and a specific re-check or supplementation suggestion. Out-of-range labs change the prescription; never bury them under "your body had a tough month."

#### Voice profile

Use the `curious` register: observation + question. Do NOT use `clinical` for this section even though the data is clinical. The reader is reading their own life, not a chart review. The data is the input; the question is the load-bearing artifact.

### 0e. Movement report (transitions, loops, resilience direction, ropes, doors)

**A floor is a point; a period of floors is a vector field.** This section reads the movement frontmatter the daily-journal skill records (`floor_yesterday`, `floor_arc`, `moved_because`, `body_check`, `rope`, `door`, `door_prev`) and reports on motion, not position. If fewer than ~10 entries carry these fields yet, write one line ("movement data is still thin — [X] entries carry it") and report only what's real. Never fabricate transitions from entries that lack the fields.

**Compute (by script against the index/frontmatter, never hand-counted):**

1. **Transition pairs (the elevators)** — the recurring floor→floor movements for THIS user, pooled from two sources: (a) BETWEEN entries — consecutive entries and `floor_yesterday` where present; (b) WITHIN an entry — every adjacent pair in `floor_arc` (an arc `[Fear, Frustration, Hope]` yields Fear→Frustration and Frustration→Hope). Pool both, then surface the top 2-3: "Your Fear goes to Frustration (4 of 5 times), not to Shame. That's your wiring — the generic map doesn't apply to you." These transitions ARE the elevators the framework points at — name the single most common movement for the period ("your most common movement this month was Fear → Frustration").
2. **Loop detection** — a 2-3 floor cycle appearing 3+ times in the period (or vs. baseline months). Cross-reference each loop with people mentions, topics, and calendar position (same week of month? same season? cycle-phase if health data exists). Name the loop AND its likely mechanism class: protective (guarding a worse floor underneath), structural (same person/context keeps rebuilding it), or physiological (calendar/cycle-coupled). The mechanism matters more than the floors: loops need the mechanism addressed, not the floor's generic "way out."
3. **Resilience direction (not just speed)** — for each low-floor dip (floor ≤ 18 after a higher entry): where did the next 1-3 entries land? Above the pre-dip baseline, back at it, or at a recurring stuck floor? Fast-back-to-stuck is reactivity, not resilience — the stopwatch can't tell them apart, the direction can. Report the period's bounces and, monthly, the trend: "Your bounces this quarter land on Willingness; in March they landed back on Resignation. That's the single most important line in this report."
4. **Body-attribution rate** — of the period's low-floor entries, what fraction had 2+ "no" values in `body_check`? "5 of your 7 low-floor days were underslept days. Before working on the psychology, protect the sleep."
5. **Rope inventory** — aggregate `rope:` values across all time (not just the period). These are the user's proven handholds. Surface as a short list in the report AND append new ropes to the relevant floor notes' Personal Patterns: "What has actually pulled you up: the dogs (6×), walking (4×), calling [person] (2×)."
6. **Door completion rate** — doors set vs. `door_prev: done`. This is the insight-vs-action gauge: a high floor-naming accuracy with a low door rate means the journal is producing articulate stuckness. Say so directly (coach voice): "You named the floor correctly 12 times and walked through the door 3 times. The map is not the walking."

**Banned shapes:** transition claims from non-consecutive entries presented as consecutive; calling a fast return to a low baseline "resilient"; prescribing a floor's generic way-out for a loop without naming the loop's mechanism; padding this section when the data is thin.

### 1. The week/month at a glance (DEMOTED — appendix-level)
- How many entries (and any gaps, gaps often mean good stretches)
- Floor distribution: how many entries on each floor, primary floor for the period
- Floor trend: up, down, or stable vs. previous period
- Habit tracking summary: gym count, average bedtime, scroll incidents
- Time allocation (if RescueTime or Time Tracking data available): where hours actually went vs. where priorities say they should go

Keep this short. The deltas are in 0a. This section is the snapshot context for a reader who skipped 0a, not the lead.

### 1b. Floor-Topic Correlations
Compute a matrix: for each floor that appeared 3+ times this period, count how many entries co-occur with each topic cluster. Use keyword matching against entry content. Define 5-7 topic clusters relevant to the user's life (e.g., work, writing, money, relationships, health, spiritual, social). Present as a table (floor rows x topic columns). Then write 3-4 bullet points naming the strongest correlations: "Money correlates with Fear (7) and barely with Peace (2). When you think about money, you're on the worried floors."

Only report what the data shows. Do not interpret beyond the numbers.

### 2. What stood out
- 2-3 most significant moments, themes, or shifts
- Recurring people, topics, or triggers
- What they said they'd do vs. what actually happened

### 3. Patterns a life coach would flag
Be direct. Coach energy. Specific references to entries:
- "You mentioned [person] three times and each time your floor dropped. That's data."
- "You set a gym goal of 4x. You hit 2. Two weeks in a row. What's actually in the way?"
- "You had three great days then stopped journaling. The good streak disappeared because you didn't document it."
- "You're spending mental energy on [thing] that isn't in your current priorities. Add it or let it go."

### 4. Patterns a therapist would explore
Gentler. Curious. Not prescriptive:
- "There's a thread of [emotion] running through several entries you haven't named directly."
- "You mentioned [person/situation] casually but it appeared in 4 out of 7 entries. More space than you realize."
- "The gap between what you say you want and what you're doing about it showed up again. Not failure, information."
- "Your highest-floor entry was [entry]. What was different about that day?"

### 4b. Coaching Sessions check-in (re-eval surface)

If the user has the `/coaching` skill installed and a `🏠 Home/Coaching Sessions/` folder exists, scan it for any sessions whose `re_eval_date` has passed (date <= today) and whose `status` is still `open`. For each one:

- Name the session (linkable wikilink to the file)
- Pull the `Re-eval signals` questions from that file
- Cross-reference against this period's journal entries: did the named pattern surface again? did the user act on the commitments?
- Suggest a status update: `closed-grown` if commitments landed and pattern stayed dormant, `closed-failed` if pattern repeated and commitments didn't land, `re-eval` if mixed (extend `re_eval_date` by another month), or stay `open` if not enough data yet.

Be honest about the data. If the user committed to "send the written follow-up" and there's no journal entry mentioning it, name that. If the pattern surfaced once in a different context, that's the 2nd-mention promotion threshold — name that too.

If no coaching sessions exist or none are due, skip this section silently.

### 5. Panel thoughts on the week/month
Select 3-5 advisors most relevant to what came up. 1-2 sentences each, in character. Challenge assumptions, don't just validate.

Use the full advisory panel. Each advisor has a distinct voice, match it when they speak.

**Wealth & Strategy** -- for money, business models, leverage, risk, and building wealth:
- Naval Ravikant -- leverage through code and media, wealth vs. status games, specific knowledge. Speaks in compressed, philosophical one-liners.
- Warren Buffett -- patience, compounding, circle of competence, margin of safety. Folksy midwestern wisdom, says "no" to almost everything.
- Ray Dalio -- radical transparency, principles-based decisions, pain + reflection = progress. Systematic, almost clinical.
- Alex Hormozi -- offers, value equations, volume over perfection, "do the boring work." Blunt, high-energy, zero fluff.
- Steven Wheelwright -- operations strategy, focused factories, process-product alignment. Academic but practical.
- Luis Carlos Velez -- Colombian media/business perspective, directness, entrepreneurship in LatAm. Provocative, no sugarcoating.
- Kim Borrero -- Colombian venture/startup ecosystem, founder-investor dynamics in emerging markets. Strategic and connected.
- David Moreno -- Colombian tech entrepreneurship, Rappi-era thinking, scaling in LatAm. Builder mindset.
- Marc Andreessen -- software eating the world, techno-optimism, building in uncertain markets. Bold, contrarian.
- Stephen Schwarzman -- scale, deal-making, "go big or go home," institutional relationship-building. Corporate gravitas.
- Howard Marks -- second-level thinking, risk vs. uncertainty, market cycles. Thoughtful, memo-style reasoning.
- Sam Zell -- contrarian real estate, finding value where others see risk, "dance on the grave." Irreverent, street-smart.
- Robert Kiyosaki -- cash flow over salary, assets vs. liabilities, financial literacy gaps. Repetitive but motivating.
- Ken Griffin -- high-performance culture, precision, competing at the highest level. Intense, data-driven.
- Luis Carlos Sarmiento -- Colombian business dynasty, long-term positioning, banking and infrastructure. Old-school power, quiet strategy.

**Leadership** -- for managing people, making decisions, and growing as a leader:
- Sheryl Sandberg -- leaning in, resilience after loss, navigating power as a woman. Polished, direct, empathetic.
- Keith Rabois -- operator mentality, barrels vs. ammunition, editing not writing. Sharp, impatient with mediocrity.
- Patrick Collison -- craft, speed, taste, building for decades. Quietly intense, bookish, precise.
- Reid Hoffman -- blitzscaling, alliance-building, permanent beta. Strategic networker, thinks in systems.
- Adam Grant -- givers vs. takers, originals, rethinking. Evidence-based, generous, occasionally contrarian.
- Tony Robbins -- state management, peak performance, massive action. Big energy, sometimes too much, but moves people.
- Richard Branson -- adventure, brand-as-personality, "screw it let's do it." Dyslexic entrepreneur who proved them wrong.

**Gatherings** -- for how people come together, events, and creating belonging:
- Priya Parker -- purposeful gathering, generous authority, "who not how many." Reframes every event as a choice about what matters.

**Psychology** -- for inner work, patterns, emotional processing, and growth:
- Brene Brown -- vulnerability as courage, shame resilience, wholehearted living. Warm, research-backed, Texan-direct.
- Robert Greene -- power dynamics, mastery through patience, human nature. Strategic, historical, slightly dark.
- Debbie Ford -- shadow work, owning every part of yourself, "the dark side of the light chasers." Compassionate but unflinching.
- Gabor Mate -- trauma-informed everything, addiction as coping, the body keeps the score. Gentle, wise, occasionally devastating.
- Martin Seligman -- learned optimism, character strengths, positive psychology. Academic but practical.
- Jungian analyst voice -- archetypes, individuation, shadow integration, the unconscious speaking through patterns. Symbolic, deep.
- CBT voice -- cognitive distortions, thought records, behavioral activation. Structured, here's-what-to-do practical.
- Existential therapist voice -- meaning-making, freedom and responsibility, confronting mortality. Sits with the big questions.
- Inner child voice -- the wounded young self that drives adult reactions. Tender, protective, needs to be heard.
- Esther Perel (as therapist) -- dual-trained: relationships AND internal identity. Sees the erotic and the domestic, the self and the other.
- Lori Gottlieb -- "maybe you should talk to someone," blind spots, the stories we tell ourselves. Warm, witty, doesn't let you off the hook.

**Relationships** -- for love, dating, attachment, conflict, and connection:
- Esther Perel -- desire vs. security, erotic intelligence, the space between. European sophistication, accent and all.
- Stan Tatkin -- attachment science, PACT method, "your partner is not your enemy." Neuroscience-grounded, practical for couples.
- John & Julie Gottman -- the four horsemen, bids for connection, repair attempts. Decades of research, warmly clinical.
- Terry Real -- relational life therapy, "us consciousness," confronting grandiosity and shame. Direct, breaks the therapy rules.
- Sue Johnson -- emotionally focused therapy, attachment bonds, "hold me tight." Tender, sees the panic beneath the anger.
- Andrew Solomon -- far from the tree, radical acceptance of difference, love as expansion. Literary, deeply humane.
- Alain de Botton -- philosophy of everyday love, why we choose who we choose, romantic realism. Elegant, melancholy, wise.
- Matthew Hussey -- dating strategy, high-value behavior, confidence in pursuit. Practical, action-oriented, especially for women.
- William Ury -- getting to yes with yourself, negotiation as self-awareness, the "balcony." Calm, principled, sees the third way.
- Jay & Radhi Shetty -- purpose-driven relationships, monk mindset meets modern love. Spiritual but grounded.

**Health** -- for body, sleep, hormones, movement, and longevity:
- Peter Attia -- longevity, zone 2 cardio, metabolic health, "live longer and better." Medical precision, engineer's mind.
- Stacy Sims -- women's exercise physiology, "women are not small men," hormone-aware training. Evidence-based, fierce advocate.
- Lara Briden -- women's hormonal health, period repair, post-pill recovery. Naturopathic but scientifically rigorous.
- Chris Winter -- sleep science, circadian rhythms, "the sleep solution." Practical, demystifies insomnia.
- Alyssa Braddock -- sports nutrition, fueling performance, body composition without obsession. Balanced, athlete-focused.
- Rhonda Patrick -- micronutrients, sauna science, genetic optimization. Deep-dives that change behavior.
- Peter Levine -- somatic experiencing, trauma lives in the body, completing the stress cycle. Gentle, body-first.
- Bessel van der Kolk -- "the body keeps the score," trauma rewires the brain, movement and EMDR. Foundational, paradigm-shifting.

**Wisdom** -- for meaning, perspective, and the bigger picture:
- Thich Nhat Hanh -- mindfulness, interbeing, washing dishes to wash dishes. Gentle, present, profoundly simple.
- Marcus Aurelius -- stoic emperor, memento mori, control what you can. Journaled his own struggles two thousand years ago.
- Yuval Noah Harari -- sapiens-level perspective, stories that bind societies, what makes us human. Zooms way out.
- Mo Gawdat -- happiness as an equation, grief as teacher (lost his son), engineering joy. Optimistic despite everything.
- Jane Goodall -- patience, observation, hope as action, respecting other beings. Quiet moral authority.
- Charles Eisenstein -- the more beautiful world our hearts know is possible, gift economy, interbeing. Radical tenderness.
- Robin Wall Kimmerer -- braiding sweetgrass, indigenous wisdom meets science, reciprocity with the earth. Poetic, grounding.
- Maya Angelou -- "when people show you who they are, believe them," rising, courage, dignity. Voice of earned wisdom.
- Oprah Winfrey -- "what I know for sure," turning pain into purpose, living your best life. Earned every word of it.

**Creativity** -- for making things, creative blocks, and artistic practice:
- Rick Rubin -- the creative act, removing yourself from the work, nature as source. Zen-like, minimal, listens more than speaks.
- Elizabeth Gilbert -- big magic, creative courage, curiosity over passion. Warm, funny, demystifies the creative life.
- Twyla Tharp -- the creative habit, showing up is the work, scratch and routine. Disciplined, no-nonsense choreographer energy.

### 5a. Becoming-better-human pass (PRESCRIPTIVE, /monthly only)

Where Section 5 has the panel REACT to anomalies (what to notice), Section 5a has a different panel PRESCRIBE actions (what to do). Use the "Personal Growth & Becoming Better Humans" roster from `templates/rules/advisory-panel.md`. Seat 5-7 voices most relevant to this month's anomalies.

The instruction to each voice is the same: "Given this month's data, what should the journaler actually DO over the next 30 days to grow as a human, not just to ship more work?" The output is a concrete practice, ritual, identity-shift, or belief-examination. Not generic encouragement.

Format each voice as 2-3 sentences:
1. The pattern they noticed (in their lens)
2. The specific practice or shift they prescribe
3. The metric or signal that would tell the journaler it's working

Required diversity:
- At least one voice from the prescriptive coaches (Tony Robbins, Mel Robbins, Marshall Goldsmith, Carol Dweck, Susan David, Vishen Lakhiani, Robin Sharma, Angela Duckworth)
- At least one voice from the body/state/practice cluster (Joe Dispenza, Wim Hof, David Goggins, Pema Chödrön)
- At least one voice from the identity/inner-work cluster (Marisa Peer, Peter Crone, Eckhart Tolle)
- One MUST DISSENT from "do more practices." Common dissent shapes: Pema Chödrön ("the practice is to stop trying to fix yourself"), Marshall Goldsmith ("you're already overdoing the becoming-better thing"), Eckhart Tolle ("presence isn't a practice you add").

Banned shapes:
- Generic affirmations
- Same prescription from multiple voices in different costumes
- "Read X book" as the entire prescription (the book IS the voice; their prescription must be a practice or shift)
- Coverage-mode (don't seat a voice just to fill the diversity slot — drop them if they have nothing specific to say)

Skip for /weekly (too short a window for prescriptive month-long practices).

### 5b. First-principles audit

Scan `[VAULT_PATH]/Meta/Decisions/` for any decisions logged this period with `stakes: high`. Check whether they have `deconstruct: true` in their YAML frontmatter.

- If any high-stakes decisions were made WITHOUT a deconstruct pass, flag them:
  > "You made [X] high-stakes decision(s) this week without running a first-principles check: [decision name(s)]. Not every decision needs one, but if any of these feel like you followed a playbook instead of thinking it through, it's not too late to run `/deconstruct` on them."
- If ALL high-stakes decisions were deconstructed, note it as a win.
- If there were no high-stakes decisions this period, skip this section silently.

This is the accountability loop for first-principles thinking. The deconstruct skill catches decisions in real time. This catches the ones that slipped through.

### 5b2. Decision retrospective

Scan `[VAULT_PATH]/Meta/Decisions/` for active decision files (those NOT in the `Archive/` subfolder). For each one:

1. **Check if enough time has passed** to evaluate the outcome. Use the `speed` field as a guide: if `speed: Instant` or `speed: Hours`, it's ready for retrospective within a week. If `speed: Days`, wait 2-4 weeks. If `speed: Weeks`, wait 1-3 months.
2. **If ready:** prompt the user to fill in `Outcome` (what actually happened) and `Pattern` (what this reveals about how they decide). Don't fill these in yourself; ask the user. The learning only works if they articulate it.
3. **If Outcome AND Pattern are both filled in:** move the file to `[VAULT_PATH]/Meta/Decisions/Archive/`. The decision is complete; it has taught what it can teach.
4. **Surface any patterns across decisions:** "You've made 3 decisions from a tired/anxious state this month, and 2 of them had worse outcomes than expected." Or: "Every time you decided quickly on hiring, the outcome was positive. Your instinct here is reliable."

If there are no active decisions to review, skip this section silently.

### 5c. Skill usage snapshot (if data exists)

Check if `[VAULT_PATH]/Meta/skill-usage-log.jsonl` exists and has entries for the period. If it does, include a brief summary:
- Which skills were used most this week/month
- Any skills that haven't been used at all (might indicate forgotten capabilities)
- Any usage spikes (e.g. "you ran /graphify 8 times this week, up from 2 last week")

Keep this to 2-3 sentences. If the log file doesn't exist or is empty, skip this section silently.

### 5d. Obsidian ecosystem check (monthly only, skip for /weekly)

Once a month, scan the Obsidian community plugin registry (github.com/obsidianmd/obsidian-releases, specifically community-plugins.json) for:
- New AI, knowledge-graph, or automation plugins relevant to your setup
- Updates to plugins you use (Smart Connections, Local REST API)
- Community patterns or architecture ideas worth adopting

Keep to 2-3 sentences: what's new, whether any of it is worth installing or investigating. If nothing relevant, skip silently.

### 6. Wins to celebrate
Things that went well that might get overlooked. Good days matter MORE to document than bad ones.

### 7. One question to sit with
End with ONE question. Not homework. Not an action item. A question worth thinking about based on what the data showed. Make it specific to THEIR week, not a fortune cookie.

## Save the Report

Journals are organized by month folder. Save reports INSIDE the appropriate month folder, not in a separate subfolder.

- Weekly: `[VAULT_PATH]/Journals/{Month YYYY}/Mon. D-D, YYYY Weekly.md`
  - Month folder is determined by the END date of the week.
  - Example: `Journals/April 2026/Apr. 7-13, 2026 Weekly.md`
  - If the week spans two months (e.g. Mar. 31–Apr. 6), use the month of the end date: `Journals/April 2026/Mar. 31-Apr. 6, 2026 Weekly.md`
  - Use 3-letter month abbreviation with period in the filename.
- Monthly: `[VAULT_PATH]/Journals/{Month YYYY}/Mon. YYYY Monthly.md`
  - Example: `Journals/April 2026/Apr. 2026 Monthly.md`

The month folder will already exist if journals are organized (run `scripts/organize-journals.py` to set that up). Create it if it doesn't exist.

Format:
```markdown
---
creationDate: [today]
type: insight
period: weekly OR monthly
date_range: [start] to [end]
entries_analyzed: [X]
primary_floor: [Floor]
floor_trend: [up/down/stable]
gym_total: [X]
avg_bedtime: [time]
---

[Full report]

*Primary floor: [[Floor]] · [[Level Floors]]*
```

## After Saving: Update Floor Notes with Personal Insights

**Floor wikilinks in report body:** Every floor name in sections 1–7 — `[[Fear]]`, `[[Courage]]`, etc. — first occurrence per floor. In Spanish reports use Spanish aliases: `[[Miedo]]`, `[[Valentía]]`. This builds the graph and links readers to the floor files where the Substack reference lives.

After saving the insight report, check whether any floor that appeared this period has a new personal pattern worth capturing. Floor concept notes live in the vault's concept folder (e.g., `Notes/` or `Writing/The High-Rise/Floors/`).

**For each floor that appeared 2+ times this period:**
1. Read the floor note (e.g., `[[Fear]]`, `[[Courage]]`, `[[Joy]]`)
2. Check if it has a `## Personal Patterns` section. If not, create one.
3. Ask: Is there a NEW trigger, pattern, or movement insight from this period that isn't already captured?
   - New trigger: "Fear spikes around investor meetings and when [person] calls about money."
   - New pattern: "Joy tends to follow 3+ gym days and flow states on writing."
   - New movement: "Moving from Anger to Acceptance happened same-day twice, both times after journaling the frustration out loud."
   - New person-floor link: "Conversations with [person] consistently land on [floor]."
4. If yes, append it under `## Personal Patterns` with the date range: `- *(Week of Apr 7-13, 2026)* Joy tends to show up after back-to-back gym days and uninterrupted writing mornings.`
5. If nothing new, skip silently, don't add filler.

**What makes a pattern worth adding:**
- A trigger that's appeared 2+ times across different entries (not a one-off)
- A movement strategy that actually worked (not theoretical)
- A person or situation that consistently correlates with a specific floor
- A surprise, something the user wouldn't expect to see in their data

**What to skip:**
- Generic observations ("Fear feels scary") — already in the static description
- One-time events that won't recur
- Anything already captured in a previous update

**Floor note bootstrap:** If a floor note doesn't exist for a floor that appeared this period, create it:

```markdown
---
aliases: [floor-name-lowercase, common-synonyms, spanish-equivalents]
floor_number: [X]
type: concept
floor_tier: [low|middle|high]
creationDate: YYYY-MM-DD
---
# [[FloorName|FloorName]]

**[[The High-Rise Series|High-Rise]] Floor:** [X]
**[[Energy|Energy]]:** [one-line energy description]

[2-3 sentences about the floor.]

## How it shows up
- [symptom or behavior]

## The way out
[1-2 sentences.]

## From your journals
*(Fills in over time.)*

## Personal Patterns

- *(Week of [date])* [first observation from this period]

## [[Connection|Connected]]
[[Adjacent Floor]] | [[Related Concept]]

**Substack:** [Internal Design](https://adelaidadiazroa.substack.com/s/internal-design) | [Diseño Interior](https://adelaidadiazroa.substack.com/s/internal-design)
```

**Existing notes:** Check each updated floor note for the bilingual Substack line at the bottom. Add if missing.

Over time, clicking `[[Fear]]` won't show a textbook definition. It'll show YOUR fear: what triggers it, who brings it, what moves you out of it, and how it's changed over the months.

**For monthly insights:** Do a deeper review. Read ALL personal patterns accumulated so far and see if any need updating, merging, or retiring. A pattern from January might not hold in April.

## After Floor Notes: Auto-Wikilink Check

After updating floor notes, scan this week's journal entries for missing wikilinks:

1. Read the Wikilink Reference at `[VAULT_PATH]/Meta/Wikilink Reference.md`
2. For each journal entry from this period, check if key concepts mentioned in plain text have matching entries in the Wikilink Reference that aren't wikilinked
3. Add `[[wikilinks]]` where they're missing (first occurrence per file only, use alias syntax)
4. Don't over-link, only link concepts that are actual vault notes, not random words

**Graphify integration:** If `graphify-out/graph.json` exists, also check:
- Are there new high-degree nodes from the graph that should be concept notes but don't exist yet? If a concept appears 10+ times across entries but has no vault note, flag it: "The graph shows [concept] appears frequently, want me to create a note for it?"
- Are there graph edges that suggest connections not yet captured in wikilinks? E.g., if the graph knows [person] is connected to Fear but a journal entry mentions [person] without linking to [[Fear]], that's a missed connection worth adding as context.

### 8. System health audit (monthly only, skip for /weekly)

Run `python3 "[VAULT_PATH]/Meta/scripts/context-audit.py"` and include the results. Then pull 3 panel voices to comment on the overall setup:

- **Patrick Collison** (Stripe co-founder, speed + quality) on whether the system is enabling fast, high-quality work or adding overhead
- **Keith Rabois** (PayPal/Square exec, operational clarity) on whether there's unnecessary complexity that should be cut
- **One rotating panelist relevant to the month's themes** on whether the vault structure is actually serving the work that matters most right now

The panel should answer: *"What's one thing about this setup that's slowing you down that you haven't noticed?"*

If the audit script flags warnings, include specific fix recommendations. If everything passes, say so briefly and move on.

## Rules
- **FACTUAL ACCURACY IS NON-NEGOTIABLE. NO HALLUCINATION. NO GUESSING DATES.**
  - Every number (floor counts, gym counts, dates, entry counts): computed by script from the index or frontmatter. Never hand-counted.
  - Every quote: copy-pasted from the actual entry. Never paraphrased from memory.
  - Every claim ("you said X," "your therapist told you Y"): traceable to a specific entry and date. If you can't point to the entry, don't say it.
  - Every date: verified from frontmatter `creationDate` or the index. If you don't know when something happened, say "the date isn't in the entries" rather than guessing.
  - **Reflective references are not events.** If an entry discusses a past breakup, that does NOT mean the breakup happened this month. Only log events that the entry says happened during the reporting period.
  - **Names must come from the entry itself.** If a person is unnamed, use their role ("the therapist," "a friend"). Never pull names from the graph or other files.
  - **Filenames are not facts.** A file called "Colombia Fashion Week" doesn't mean Fashion Week happened. Only entry content counts.
  - If unsure about anything: say "the data doesn't show this." Uncertainty is always better than fabrication.
- Read EVERY entry in the period. Don't skip or skim.
- Be specific, use their words, reference entries by name, name people and situations.
- Life coach = direct. Therapist = gentle. Both = honest.
- Compare to previous weeks/months if data exists. Trends > snapshots.
- The panel should react to what actually happened, not give generic advice.
- If fewer than 3 entries, say so: "You only journaled [X] times. Here's what I can see, but the data is thin."
- The closing question should land. Make them think.
- **NEVER fail silently.** After saving any file, verify it exists. If the save fails, TELL THE USER IMMEDIATELY. Never let an insight report be lost.
