---
name: health-article-prompt
description: Self-contained prompt for drafting a Substack article about the health stack (Apple Health / Oura / Fitbit + cycle-aware coaching + journal pairing + auto-trigger chain). Paste into a fresh Claude Code session. Output: two Substack drafts (English + Spanish) saved to your two publications, NOT published.
---

# Substack article prompt — the health package, for healthy people who are not tech people

Copy everything below the `---PROMPT---` marker into a fresh Claude Code session. The session must have:
- The user's vault loaded (CLAUDE.md auto-loads)
- The substack MCP connected (`mcp__substack__create_draft` etc. available)
- The user's voice rules in their vault at `⚙️ Meta/rules/voice-style.md` + `voice-firewall.md` + `life-history-prose.md`
- The advisory panel roster in `⚙️ Meta/rules/advisory-panel.md`

The prompt assumes the writer has used the health stack at least once and wants to share the experience. If the writer has not yet imported their own health data and run `/coach today`, the article will read as a product spec instead of a lived story — the writer will need to fill in concrete moments from their own data and mark them with the `[FILL IN]` convention this prompt establishes.

---PROMPT---

I want to draft a Substack article about the health package my AI Brain Starter substrate now ships. The audience is healthy people who care about longevity and self-knowledge but are not tech people. No jargon. No "MCP server" or "DuckDB" — translate everything to human concepts.

Two outputs: one English draft for my main publication, one Spanish (Colombian, tú form) draft for my secondary publication. Both saved as drafts, NEVER published — I review them first.

## Before drafting (mandatory)

Read these files in full, in this order:

1. `⚙️ Meta/rules/voice-style.md` — what my voice IS (warm, direct, no fluff, no preaching, the specific register established in the voice rules file)
2. `⚙️ Meta/rules/voice-firewall.md` — binary DON'Ts (no em dashes in external prose, no exclamation marks, no LinkedIn-influencer tone, banned framings)
3. `⚙️ Meta/rules/life-history-prose.md` — every fact (age, year, location, sensory detail, dollar amount, named person, direct quote) MUST trace to a vault source OR be marked `[FILL IN]`. Never invent.
4. `⚙️ Meta/rules/advisory-panel.md` — the panel rules, especially Rule 9 (Jackie Kennedy required on every public-facing review) and Rule 2 (named panelists only, no archetypes)
5. The CHANGELOG entries for health-mcp v0.1 through v0.5.1 at `~/dev/ai-brain-starter/docs/CHANGELOG.md` — these tell you exactly what shipped, in order, with the panel reasoning for each decision. The article should be told FROM the lived experience side, not the engineering side, but you need this for accuracy.
6. `~/dev/ai-brain-starter/docs/AUTOMATION.md` — the why-/journal-is-the-gate explanation. Useful for the philosophy section.

Do NOT read individual SKILL.md files. They are dense and will leak engineering jargon into your draft. The CHANGELOG and AUTOMATION.md are the right altitude.

## What the article should cover

A reader who has never heard of any of this should finish the article understanding:

1. **What problem this solves.** They have an Apple Watch / Oura Ring / Fitbit / phone health app. The data sits in separate apps. None of it pairs with their journal, their weekly review, their actual mood that week. The "smart coaching" promises elsewhere are templates dressed up as personalization.

2. **What the system actually does, in lived terms.** Each morning, after they journal, their phone has tomorrow's workout in the calendar, sized to last night's sleep, this week's recovery, where they are in their cycle if they have one, what their journal said about how they felt yesterday. Their lab results from last month sit in the same place as their sleep data. When their `mood is low for a stretch, the system can show them whether it's because their Vitamin D came back low in last month's labs, or whether they were chronically under-fueling, or whether it's an anniversary pattern they've been carrying for three years without naming.

3. **The seven kinds of insight it can surface.** Use these as concrete "and here's an example of what this looks like" sections. Each one should land with a specific scenario, not a feature list. The scenarios are illustrative; the writer can mark them `[FILL IN with my own example]` if they want to use their actual data later.

   - **Cycle-phase correction.** "Your HRV was 18% below baseline mid-week. But you were in mid-luteal phase. That dip is physiology, not a recovery deficit." Most coaches and apps would tell you to rest more. The system tells you the truer thing.
   - **Floor pairing.** "On the days you wrote 'Fear' in your journal frontmatter this month, your sleep latency averaged 23 minutes vs 11 on the others. Your body registers the worry before you name it. For the next two weeks, when sleep latency runs long, write before bed instead of in the morning."
   - **Under-fuel detection.** "Five of the last fourteen days, you ate less than 70% of what you burned. The recovery score kept telling you to rest more. The actual prescription was eat enough." Most fitness apps cannot tell the difference.
   - **Anniversary patterns.** "Your HRV this month is 14% below the same month last year. It's also 14% below the same month the year before. Three years of the same dip in the same month. That's not random. That's worth a conversation."
   - **Lab status flagging.** "Your Vitamin D came back at 28 ng/mL last month, below range. The persistent low-mood pattern this month may have a metabolic floor under it. Worth a re-test in 90 days after supplementation."
   - **Sleep regularity.** "Your bed-time variance was 87 minutes over the last 14 days. Your wake-time was 72 minutes. The body is in chronic low-grade jet lag. Pick a wake time within a 30-minute window for the next two weeks."
   - **Longevity panel.** "VO2Max trended down 4% over 90 days. Zone 2 minutes ran 124 per week against a 180 target. Cardio capacity is the priority lever this quarter."

4. **The philosophical line.** Auto-trigger the analysis, never auto-trigger the action. The system prepares the workout, appends body context to yesterday's journal, drops the workout in the calendar — but logging completion stays manual. The body stays in the loop on what actually happened. Frame this as the difference between coaching and surveillance.

5. **What's automatic.** The reader should understand that they do not need to remember a single command. They journal (their existing habit) and the system runs the chain. If they skip journaling for a few days, the system quietly waits. It never works harder than they do.

6. **What's still manual, and why.** Apple Health requires a one-time export from the iPhone (the system can pull it weekly after that). Labs require uploading the CSV from their patient portal once a quarter. These are the only friction points and the reader should know about them upfront.

7. **The case for labs.** Apple Health captures roughly the visible 20% of health — heart rate, sleep, steps, exercise. The chemistry that drives chronic disease (ApoB, fasting insulin, hs-CRP, full thyroid panel, Vitamin D, ferritin) is invisible to it. Quarterly labs run roughly $200-400 direct-pay at LabCorp or Quest in the US. The labs change the prescription. Frame this as a self-knowledge investment, not a cost.

8. **The privacy line.** Local-only. Their health data never leaves their machine. The system does not send anything to OpenAI or Anthropic or any third party beyond what their existing wearable apps already do. This is one paragraph, near the top, not buried.

## Article structure

```
Title: [a line, not a headline — something like "The body has a journal too" or "What my Oura Ring told my journal" or "A longevity coach that actually reads my mood"]

Subtitle: one line, the promise. No exclamation marks.

Opening (2 paragraphs):
  A scene. The writer was juggling data across [N] apps, never paired with their journal,
  and noticed [a specific pattern] that none of the apps surfaced. Use [FILL IN] if the
  writer needs to add their own scene later. This is the hook.

## What I built (1-2 paragraphs)
  Plain English description of the system. Not feature list. The before/after.

## The seven kinds of insight
  Seven sub-sections, one per insight type from the list above. Each: a one-line setup,
  then a scenario in italics or quotes, then 1-2 sentences on what this changed for
  the writer. Use [FILL IN] for personal moments the writer wants to add.

## The line between coaching and surveillance
  The auto-trigger-analysis / never-auto-trigger-action philosophy.
  Bainbridge dissent (panel 2026-05-10) is the load-bearing principle here.
  Two paragraphs. This is the part that distinguishes the article from a fitness-app review.

## What's automatic, what stays manual
  Practical. One short paragraph each. The reader should be able to imagine themselves
  doing it.

## Why labs matter
  One or two paragraphs. The 20% / 80% framing. The labs that move the prescription.
  Direct-pay range. Not a sales pitch, an invitation.

## A privacy note
  Local-only. Two sentences. Earlier in the article would also work — Jackie's vote.

## Closing
  Friction is not suffering. The point of the system isn't to optimize a body into a
  spreadsheet, it's to inhabit a body better. Land on a sentence the reader can carry
  with them.

(Bottom of post, one line below the close, separated by a horizontal rule:)
Built it as open-source for anyone running an AI Brain Starter. github.com/mycelium-hq/ai-brain-starter
```

Length target: 1,500 to 2,500 words. Not shorter (it would lose the lived register). Not longer (it would lose readers).

## Voice rules (mandatory)

- **No em dashes.** Use commas, colons, periods, parentheses. Substack is external prose; the no-em-dash rule applies.
- **No exclamation marks.**
- **No LinkedIn-influencer tone.** No "Here's what most people get wrong" or "I learned 5 things." No listicles. The voice is first-person, lived, slightly philosophical, warm.
- **No preaching.** The writer noticed something true. They are not selling longevity.
- **Friction is not suffering.** Surface this thesis (or whichever canonical thesis the writer's voice rules establish) if relevant.
- **No "let me share" / "let me tell you" / "I want to talk about"** openers. Start in the scene.
- **First occurrence of Floor names → wikilinks.** `[[Fear]]`, `[[Courage]]`, etc. on first occurrence. Spanish version uses Spanish aliases (`[[Miedo]]`, `[[Valentía]]`).
- **No bullet lists in body prose** except in the "seven insights" section. Otherwise sentences and paragraphs.

## Life-history claims

Every concrete claim ("I noticed X on Y date", "my HRV ran Z%", "I journaled on a Thursday and saw...") must either:

1. Trace to a vault source the writer can cite, OR
2. Be marked `[FILL IN]` so the writer can replace with their actual experience before publishing

Do NOT invent specific numbers, dates, or sensory details. The scenarios under "The seven kinds of insight" are illustrative — they are written as anonymous "your" examples, not "I" claims. If the writer wants to make any of them first-person, they will swap [FILL IN] for their actual data before publish.

## After the English draft is composed: panel review

Per advisory-panel.md Rule 9 (Jackie Kennedy required on every public-facing review), convene a 5-voice panel on the EN draft:

1. **Jackie Kennedy Onassis** — public image, what does this look like to the reader who does not know me, is the privacy commitment visible enough, does the close land
2. **Elizabeth Gilbert** — creative voice check, does the opening pull a reader in, is the language alive
3. **Maya Angelou** — does the writing have weight, is the closing line worth carrying
4. **Brené Brown** — does the article respect the reader, does it preach, does it land without making the reader feel diminished
5. **One dissenter from the Wisdom & Meaning section** — Thich Nhat Hanh OR Marcus Aurelius. The dissent role is "is the writer making the body more legible without making it more lived" (the Bainbridge axis from the 2026-05-10 panel)

Each voice speaks 1-2 sentences in their authentic register. At least one MUST dissent or push back. Apply the notes that score 80+ conviction (Rule 11). Reject low-conviction notes politely.

After applying panel notes, run the humanizer pass:

```
/humanizer
```

This is non-negotiable. The Substack publisher rule requires it BEFORE the draft is saved.

After /humanizer returns, run the verification script:

```
/usr/bin/python3 "<VAULT_ROOT>/⚙️ Meta/scripts/verify_humanizer_pass.py"
```

If that script flags anything, fix it and re-run /humanizer. Loop until clean.

## After the EN draft is clean: Spanish translation

Translate to Colombian Spanish, tú form (NOT usted, NOT vosotros). Match the same warm + direct register. Do NOT word-for-word translate — re-write the article in the same voice. Spanish has its own rhythm and the article should sound like it was written in Spanish, not translated.

Floor wikilinks use Spanish aliases on first occurrence: `[[Miedo]]`, `[[Valentía]]`, `[[Aceptación]]`, etc.

Run /humanizer on the Spanish draft too. Some AI-Spanish telltales are different from AI-English telltales; the humanizer skill knows both.

Re-run `verify_humanizer_pass.py` on the Spanish draft. Loop until clean.

## After both drafts are clean: save to Substack

Use the substack MCP. The user has two configured publications (check via `mcp__substack__list_publications`):

- **Primary (English):** publish to the writer's main Substack publication
- **Secondary (Spanish):** publish to the writer's Spanish secondary publication

For each: call `mcp__substack__create_draft(title, body, subtitle, audience="everyone")`. Do NOT publish. The writer will read both drafts and decide.

Surface the two draft IDs and URLs to me at the end so I can open them and review.

## Final acceptance criteria

Before declaring done:

- [ ] Both EN and ES drafts saved (NOT published) in their respective Substack publications
- [ ] EN draft is 1,500-2,500 words
- [ ] ES draft is 1,500-2,500 words (Spanish runs ~15% longer than English, so the ES draft may be slightly larger)
- [ ] Zero em dashes in either body (commas, colons, periods, parentheses only)
- [ ] Zero exclamation marks in either body
- [ ] Floor wikilinks present on first occurrence in each language
- [ ] Bottom-of-post one-line link to github.com/mycelium-hq/ai-brain-starter present in both
- [ ] Privacy commitment visible in the first third of the article (Jackie's vote)
- [ ] At least one dissent voice in the panel pass actually changed something in the draft
- [ ] /humanizer pass clean on both
- [ ] `verify_humanizer_pass.py` returns 0 errors on both
- [ ] At least 3 of the seven insight scenarios are marked `[FILL IN]` so the writer can replace with their lived experience before publishing
- [ ] No invented dates, dollar amounts, or named-person quotes (life-history-prose rule)

## What to surface to me at the end

- Both Substack draft URLs (so I can open and review)
- Word counts
- Panel synthesis: which voice changed what
- Any [FILL IN] markers I need to address before publishing
- /humanizer summary: how many AI-signal corrections were applied

Confirm understanding, then start with the voice-rule reads.

---END PROMPT---

## Notes for the writer using this prompt

This prompt is intentionally heavy on guardrails. The article is about a system the writer built. The temptation to over-claim, to slip into "I built this revolutionary tool" register, is real. The voice-style + voice-firewall + humanizer chain catches that. Jackie's panel seat catches public-image risks. The `[FILL IN]` convention prevents fabricated life-history.

The single most important sentence to internalize before writing: **the article is about inhabiting a body better, not optimizing one**. If a paragraph slips toward "biohacker" or "optimize", cut it.

The single most important panel note is Bainbridge's dissent from the 2026-05-10 health-mcp build panel: *"Auto-trigger the analysis. Never auto-trigger the action."* That principle is the article's spine. Without it, the article reads as another fitness app pitch.

## When to use this prompt

- After the writer has imported their own health data via `/health-setup` and `/ingest-health` and run `/coach today` at least once
- After at least one `/journal` session has fired the auto-chain
- Optionally, after a few weeks of accumulated body-track sections so the writer has lived examples to fill in

The prompt works without those prerequisites (the seven scenarios are illustrative), but the article will feel more grounded if the writer has used the system themselves first.

## Bilingual reminder

Substack drafts in this stack ALWAYS ship as EN + ES pairs. The Spanish version is not optional, not a "later" — both drafts get created in the same session. The Spanish audience for this kind of substrate is hungry and underserved.
