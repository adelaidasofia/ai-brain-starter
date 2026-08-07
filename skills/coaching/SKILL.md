---
name: coaching
description: 'Use when the user wants to process a hard conversation, a decision they''re second-guessing, or accumulated friction with a person (too big for a daily journal), wants panel takes that update with their corrections, or wants growth on a theme tracked over weeks. Triggers: /coaching, "coach me through this", "help me process this". NOT fitness coaching (/coach), daily entries (/journal), weekly/monthly reviews (/weekly, /monthly), or cross-session pattern detection (/patterns).'
---

# Coaching Session — Multi-Pass Panel + Accountability Tracking

A skill that turns a one-off hard moment into a tracked accountability arc. Runs panel passes that update with the user's corrections, files a synthesized record with a re-eval date, and updates the vault's rolling pattern tracker so growth can be measured over time.

## Why this exists

Daily journals capture one moment per day. Panel reactions inside `/journal` are one-shot. Most real coaching, real therapy, real advisor relationships are NOT one-shot — they're multi-turn, they update when new evidence comes in, and they track whether the same blind spot keeps surfacing across months.

The vault has all the pieces (panel rules in `⚙️ Meta/rules/advisory-panel.md`, daily journals, decision logs) but no skill that orchestrates the multi-pass coaching arc and files it for tracking. This skill fills that gap.

## Three-tier architecture

This skill produces files at three different timescales:

1. **Verbatim raw (immediate)** — `📋 Strategy/Coaching Sessions/Processing Notes - YYYY-MM-DD - <topic>.md`. The user's exact words during the session. Per the "save exact words" rule, no annotation, no synthesis. Available for re-read forever.

2. **Synthesized accountability record (per session)** — `🏠 Home/Coaching Sessions/YYYY-MM-DD - <topic>.md`. What surfaced, commitments named, re-eval date one month out. This is the file `/weekly` and `/monthly` look at to ask "did the pattern repeat? did the commitments land?"

3. **Rolling pattern aggregator (across sessions)** — `🏠 Home/Panel Feedback Log.md`. Patterns table at the top tracks mention counts. Single mention = watch. 2+ mentions across different contexts = promote to acute action item. The aggregator is what tells you "this is a real recurring pattern" vs "this was a one-off."

## When to use vs. /journal

**Use `/coaching` when:**
- The triggering event won't fit in a daily journal entry (a 2-hour call, a multi-day arc with one person, a decision that needs panel feedback over multiple iterations)
- The user wants the panel to UPDATE its takes when they provide corrections (not just hear them once)
- The user wants this tracked across future weeks/months, not just logged today
- Stakes are high enough that a re-eval one month out matters

**Stay in `/journal` when:**
- Daily check-in
- One-off reflection
- Floor work for the day
- Single-pass panel reaction is enough

## How it runs

### Step 1: Triggering event

Ask the user, in their language:
- What's the situation you want the panel to look at?
- When did it happen (date or rough range)?
- Who was involved?
- Is there a transcript / journal entry / message thread already in the vault, or does the user need to talk through it now?

If a transcript already exists in the vault, ask for the path. If not, the user talks through what happened. Either way, capture is the next step.

### Step 2: Capture verbatim

Decide where the raw goes:
- **Cofounder / business relational sync** — `📋 Strategy/Co-founder Syncs/Processing Notes - YYYY-MM-DD - <topic>.md`
- **Decision review** — `📋 Strategy/Decision Reviews/Processing Notes - YYYY-MM-DD - <topic>.md`
- **Personal relationship** — `📋 Strategy/Personal Coaching/Processing Notes - YYYY-MM-DD - <topic>.md`
- **Generic** — `📋 Strategy/Coaching Sessions/Processing Notes - YYYY-MM-DD - <topic>.md`

If the user has a transcript already, link to it. If not, capture what they say in their exact words, organized by chronological order or topic. The verbatim file gets numbered sections (1, 2, 3...) with section headers naming the topic, and quoted bodies that are the user's exact words.

**Critical rule (carry from the daily-journal skill):** Verbatim section bodies are the user's voice only. Panel takes do NOT live in this file. Annotations, synthesis, and commentary live in the synthesized record (Step 6) or the aggregator (Step 7), never inline in the verbatim file.

### Step 3: Initial panel pass

Read `⚙️ Meta/rules/advisory-panel.md`. Convene 3-5 voices most relevant to the triggering event. Apply the rules:

- 3-5 voices, each 1-2 sentences in their authentic voice
- At least one MUST dissent
- Cite sources for any factual claims
- Use only named panelists from the file (no archetypes, no invented experts)
- Subject-match weighting (rule 10): the panelist whose expertise matches the actual subject carries more weight than headcount
- Output-confidence filter (rule 11): flag any recommendation below 80% conviction as low-conviction (keep for texture, not for decisions)
- Peer-review pass (rule 9): after the 3-5 voices speak, run one chairman pass with the 5-bucket taxonomy — consensus (where they converged), contradictions (where they clashed), partial coverage (a dimension only one voice addressed), unique insights (a point only one voice raised), blind spots (what they all missed). The chairman analyzes, does not merge: surface the disagreement, never collapse it into a balanced-view paragraph
- Effort-aware sizing (rule 15): if `${CLAUDE_EFFORT}` is set, scale the panel accordingly

Deliver the panel's read with a clear lead sentence answering the triggering question. Don't bury the answer.

### Step 4: Wait for the user's response

The user will do one or more of:
- Confirm a take that landed
- Push back with a correction (new factual info, different context, a missed nuance)
- Add autobiographical context that recalibrates the panel's read
- Ask follow-up questions

This is the move that distinguishes coaching from journal. Whatever the user provides, the next pass UPDATES the takes transparently, not silently.

### Step 5: Iterate panel passes (the corrections-update-takes loop)

For each new piece of info the user provides:

1. Acknowledge what changes from prior takes. Be explicit: "Pattern 3 (X) is now demoted because Y." Or: "Patricia Engel's earlier read assumed Z; the autobiographical context just shared inverts that."
2. Re-run the relevant subset of voices with the new info. Don't re-run the whole panel for small corrections; do re-run when the correction is substantive (changes the framing).
3. Note clashes between prior reads and current reads explicitly. The user should be able to see how their corrections moved the panel.

Keep iterating until the user signals they're ready to synthesize. Common signals: "OK that's enough," "let's wrap this up," "save this," or just shifting to operational mode ("let's track this").

### Step 6: Synthesize and file the Coaching Session record

Create `🏠 Home/Coaching Sessions/YYYY-MM-DD - <topic>.md` with this structure:

```markdown
---
creationDate: YYYY-MM-DD
type: coaching-session
session_format: panel-with-claude
duration_approx: <one-pass | multi-pass | multi-pass-Nhr>
triggering_event: "<one-line description>"
themes: [theme1, theme2, ...]
panelists_seated:
  - <Name 1>
  - <Name 2>
  - ...
related: [<wikilinks to verbatim file, source transcripts, decision logs>]
status: open
re_eval_date: YYYY-MM-DD (one month from session)
---

*Brief one-paragraph framing of what this session covered.*

## Triggering event

<2-3 sentences describing what triggered the session, with wikilinks to source material.>

## What surfaced (synthesized across all passes)

### Pattern 1: <Theme name>

**The observation**: <what the panel saw, with citations>

**User's correction (if any)**: <what they pushed back with, what shifted>

**Status**: <1 mention in tracker / demoted / promoted / contextual fact (not a watch pattern)>

### Pattern 2: <next theme>
...

## User's commitments coming out of the session

1. <Concrete behavior commitment, not vague>
2. <Another concrete behavior commitment>
...

## Codified outcomes (rules / files changed this session)

- <Any vault rules added>
- <Any files moved or scrubbed>
- <Any structural changes>

## Re-eval signals (check by <re_eval_date>)

- Did <Pattern 1> show up at least once in real time?
- Did the user act on <commitment 1>?
- Did <Pattern 2> surface in any other context, or stay <person/situation>-specific?
...

## Cross-references

- Verbatim source: <wikilink>
- Source transcripts (if any): <wikilinks>
- Decisions: <wikilinks>
- Pattern tracking: [[Panel Feedback Log]]
```

### Step 7: Update the rolling Panel Feedback Log

Open `🏠 Home/Panel Feedback Log.md`. Two updates:

1. **Patterns table at the top** — for each NEW pattern this session, add a row with:
   - Pattern name
   - Mention count (1, since this is the first)
   - Action: "Watch for repeat. Re-eval YYYY-MM-DD."

   For patterns that surfaced this session AND already existed in the table, increment the mention count. If count hits 2+ across different contexts, change the Action to a concrete commitment (promote to acute).

2. **Synthetic Panel Reactions section** — append a new entry:

```markdown
### YYYY-MM-DD — <topic> coaching session

⚠️ **Synthetic panel reactions across one Claude session, NOT real investor feedback. Real-human content lives in "By Meeting" section above.**

**Context:** <one-paragraph framing>
**Panelists:** <list>
**Pass 1:** <what surfaced>
**Pass 2 (if multi-pass):** <how takes updated with user corrections>
...
**Convergence (across all passes):** <high-confidence signals>
**Clash:** <where panelists disagreed>
**Partial coverage:** <a dimension only one voice addressed; others silent>
**Unique insight:** <a point only one voice raised>
**Subject-match weighting:** <which panelist's voice carries most weight>
**Collective blind spot (chairman):** <what all panelists missed>
**User's commitments:** <numbered list>
**Re-eval:** YYYY-MM-DD (one month). <Re-eval signals.>
**Entry:** <wikilinks to coaching session record + verbatim>
```

### Step 8: Confirm with the user

Tell the user, in plain language:
- Verbatim saved at <path>
- Synthesized record saved at <path>
- Patterns added to tracker: <list>
- Re-eval date: <date>

Then close. Do not pile on additional panel takes. The session is logged. The system now exists to remember.

## Integration with other skills

- **`/journal`** — one-shot daily check-in with inline panel reaction. If the user starts a journal entry that turns into a multi-pass conversation, suggest switching to `/coaching` so the work gets tracked.
- **`/weekly` and `/monthly`** — natural surface for re-eval. The weekly review skill should read open Coaching Sessions, surface ones whose re_eval_date has passed, and ask "did the pattern repeat? did the commitments land?"
- **`/patterns`** — pattern detection across MANY sessions / journals. The `/patterns` skill reads the Panel Feedback Log Patterns table to confirm whether a pattern hits 2+ mentions in different contexts (the promote threshold).
- **`/deconstruct`** — if a coaching session surfaces a `stakes: high` decision, auto-offer `/deconstruct` for first-principles analysis before the user commits.

## Failure modes to avoid

1. **Yes-machine panels.** If every voice agrees, you've picked the wrong voices. Force at least one dissent. The dissent is where the value is.
2. **Synthesis without verbatim.** Always file the verbatim raw FIRST. The synthesized record is downstream; if you only file the synthesis, you've stripped the user's voice from the archive.
3. **Inflating mention counts.** A single coaching session that touches three themes adds ONE mention to each theme, not three to one theme. Mentions cumulate across DIFFERENT sessions / contexts.
4. **Skipping the corrections loop.** If the user pushes back and you don't update the takes transparently, you're roleplaying a panel, not running one. The corrections loop is what makes this honest.
5. **Forgetting re-eval.** A Coaching Session without a re_eval_date is just a fancy journal entry. The accountability comes from the calendar return.

## Output expectation

After running this skill end-to-end, the vault has:
- Verbatim raw file (the user's voice preserved)
- Synthesized Coaching Session record (with re-eval date)
- Panel Feedback Log entry (rolling aggregator updated)
- Pattern table updated (mention counts adjusted)

The user should be able to re-read these in any combination and reconstruct what happened, what the panel said, what corrections updated the takes, what they committed to, and when to check back.
