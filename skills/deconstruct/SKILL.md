---
name: deconstruct
description: 'Use when a decision or strategy rests on unexamined convention: the user says ''that''s how it''s done'', ''best practice'', or ''everyone does it this way'', copies a competitor, or feels stuck in a pricing model, business model, roadmap, hiring process, or career path they can''t justify from scratch. Also use on any high-stakes decision (stakes: high) before commit, or when the user types /deconstruct or asks to question assumptions or reason from first principles.'
trigger: /deconstruct
---

# Deconstruct -- First Principles Analysis

A structured thinking skill modeled on Aristotle's method: find the foundational truths that cannot be derived from anything more basic, then reason upward from those truths alone.

## When This Fires

- **Manual:** User types `/deconstruct` and describes a problem, decision, or situation
- **Auto-triggered (fast mode, optional):** When logging a decision with `stakes: high` in any decision template, Claude can auto-offer: "This is high-stakes. Want me to deconstruct it before you commit?" If yes, run fast mode (Phase 1 + Phase 4 only). To enable, add `deconstruct_auto: true` to your decision template frontmatter.
- **Auto-triggered (panel integration, optional):** If your vault has an advisory panel skill (like the daily-journal plugin), add a trigger row for convention-following language. When the user says "that's how it's done," "best practice," "everyone does it this way," or copies a competitor's approach without questioning why, one panel voice surfaces 1-2 hidden assumptions in one sentence, then asks: "Is that actually true for YOU, or is it convention?"

## Two Modes

### Fast Mode (default for auto-triggers)
Phase 1 (surface assumptions) + Phase 4 (high-leverage move). Use for daily decisions and anything where the full run would be overkill.

### Full Mode (default for `/deconstruct`)
All four phases in sequence. Use for pricing models, business models, career decisions, hiring processes, strategy pivots, and anything where "that's how it's done" is load-bearing in the current approach.

## How It Works

When invoked manually via `/deconstruct`, start by asking:

> "Describe the problem, decision, or situation you want me to deconstruct. Include enough context that I can distinguish your actual constraints from your assumptions. Tell me what you know is true and what you believe is true."

If the problem is too vague to deconstruct meaningfully, ask 1-2 clarifying questions. Do not guess.

Then execute the phases in order. Complete each phase fully before moving to the next.

---

## Phase 1: Surface the Assumptions

Read the user's description carefully. Identify the assumptions embedded in how they framed the problem.

For each assumption:
- State it explicitly in one sentence
- Classify its origin:
  - **Convention** ("this is how the industry does it")
  - **Imitation** ("competitors do it this way")
  - **Precedent** ("it worked before")
  - **Fear** ("we'd lose X if we changed")
  - **Unexamined default** ("nobody questioned this")
- Rate how load-bearing it is: if this assumption were false, would the problem change shape significantly? (High / Medium / Low)

**Focus on assumptions the user is most likely unaware of.** The obvious ones aren't useful.

**Do not invent assumptions to fill space.** If the user's framing is mostly sound, say so and identify only the genuine blind spots.

**Fear flag:** When an assumption is classified as **fear**, call it out explicitly. Fear-origin assumptions are rarely intellectual problems. They're emotional ones dressed as strategy ("I can't charge more because clients will leave," "I can't let this person go because they've been loyal," "I can't skip this meeting because what if it's the one"). When you find one, add a line after it: *"This one isn't an analysis problem. It's a journal entry. What are you actually afraid of?"* This bridges deconstruction to emotional processing, where fear-origin assumptions can be addressed honestly.

**Constraint check:** Separately list the actual constraints (money, time, people, physics, legal) vs. perceived constraints (things that feel fixed but could change). This distinction is where most hidden assumptions live.

Present assumptions in a numbered list, ordered by load-bearing rating (High first).

---

## Phase 2: Establish First Principles

Strip away everything identified in Phase 1. What remains that is verifiably true independent of convention, opinion, or prior strategy?

Apply these three tests to each candidate truth:

1. **Is it true even if every competitor disappeared tomorrow?**
2. **Is it true even if the user had never tried any prior approach?**
3. **Can it be stated without referencing any industry norm or "best practice"?**

If a statement passes all three tests, it qualifies as a first principle.

Present them as a numbered list. Aim for 3 to 7 principles. If you can only find 1 or 2, that is fine. Do not pad the list.

---

## Phase 3: Rebuild from the Foundation

Using ONLY the first principles from Phase 2, construct 3 distinct solution approaches as if no prior approach to this problem existed. Differentiate them clearly:

**Approach A: Optimized for speed.** What could be built or decided fastest?

**Approach B: Optimized for impact.** What would create the largest long-term result?

**Approach C: Optimized for simplicity.** What is the minimum viable version?

For each approach, explain the reasoning chain from first principles to proposed action. Do not reference what competitors do or what is "standard."

---

## Phase 4: The High-Leverage Move

From the three approaches above, identify the single action or decision that:
- Is enabled by first-principles thinking but would be invisible under conventional analysis
- Has disproportionate impact relative to its cost or effort
- The user could begin executing within the next 1 to 2 weeks

Present it as a specific, concrete recommendation (not a vague principle). Include:
- **What to do** (one sentence)
- **Why conventional thinking obscures it** (one sentence)
- **The first concrete step to take** (actionable, this week)

If no single action clearly dominates, present the top 2 candidates and explain the trade-off between them honestly.

---

## Saving the Analysis

After the user confirms they're satisfied with the analysis:

1. **If the analysis led to a decision:** Create a decision file per your vault's decision template. Add `deconstruct: true` to the YAML frontmatter so these can be queried later. In the "Why" field, reference the first principles that drove the decision.

2. **If the analysis surfaced a new concept:** Create a concept note in the right vault folder.

3. **If the analysis is exploratory (no decision yet):** Save to your notes folder with this format:

```markdown
---
creationDate: YYYY-MM-DDTHH:MM
type: deconstruction
topic: {brief topic}
mode: full | fast
---

# Deconstruction: {Topic}

## Assumptions Surfaced
{Phase 1 output}

## First Principles
{Phase 2 output}

## Approaches
{Phase 3 output}

## High-Leverage Move
{Phase 4 output}
```

If your vault doesn't have a notes folder convention, the skill will ask once and remember.

---

## Weekly Retrospective Integration

If your vault uses a weekly insights or review skill (like the insights plugin), add a "first-principles audit" step:

> Scan your decisions folder for any decisions logged this period with `stakes: high`. Check whether they have `deconstruct: true` in their YAML frontmatter.
>
> - If any high-stakes decisions were made WITHOUT a deconstruct pass, flag them: "You made [X] high-stakes decision(s) this week without running a first-principles check. Not every decision needs one, but if any of these feel like you followed a playbook instead of thinking it through, it's not too late to run `/deconstruct` on them."
> - If ALL high-stakes decisions were deconstructed, note it as a win.
> - If there were no high-stakes decisions this period, skip silently.

This closes the accountability loop. The skill catches decisions in real time. The weekly audit catches what slipped through.

---

## Formatting Rules

- Write in direct, clear prose. No filler phrases, no hedging, no "it depends" without specifying what it depends on.
- Use plain language. Avoid jargon unless the user introduced it.
- If quoting facts or studies, include the source. Don't fabricate.

## What to Run It On

This skill works best when you suspect you're stuck inside assumptions you can't see:
- A pricing model copied from competitors without questioning why
- A product roadmap built on what users say they want vs. what they actually need
- A career path followed because "that's how it's done in this industry"
- A business model that feels stuck but can't figure out why
- A hiring process, marketing strategy, or workflow everyone told you was "best practice"
- An identification and analysis of your competitors
- Any decision where "we've always done it this way" is the real answer

## Notes

- The full run is heavy. Don't suggest it for trivial decisions. Fast mode exists for a reason.
- The 3-question test in Phase 2 is the core of the skill. If you rush it, the rest falls apart.
- The constraint vs. perceived-constraint split in Phase 1 is where most value lives. Real constraints are physics. Perceived constraints are stories.
- If the user's framing is actually sound and first-principles-aligned already, say so. Don't manufacture problems to justify the framework.

