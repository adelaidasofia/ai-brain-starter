---
creationDate: 2026-04-21
type: person
relationship: maintainer
status: active
last_interaction: 2026-04-21
next_step: Send follow-up after AI Brain workshop
priority: high
person_relationship_type: "maintainer"
person_is_public_figure: true
person_journal_mention_count: 0
person_priority: "high"
example: true
---

> **Example card.** Sample showing how to structure a CRM entry for an Author or Maintainer relationship. Replace with a real person in your life when you set up your vault, or delete this file. The `example: true` flag in the frontmatter lets your dataview queries skip it. In your own vault, you would also wikilink names, places, and concepts to other notes (skipped here so this card renders cleanly on first install).

# Author / Maintainer (example)

*Maintainer of [ai-brain-starter](https://github.com/mycelium-hq/ai-brain-starter). The actual person behind this card runs an event-planning company in Latin America and an AI consulting practice on the side.*

**Role:** Founder and AI operating systems consultant
**Based:** Latin America (with regular travel to NYC)

**Public profiles:**
- Website: https://diazroa.com
- Substack (English): https://adelaidadiazroa.substack.com
- Substack (Spanish): https://perspectivasblog.substack.com
- LinkedIn: /adelaidadiazroa
- GitHub: @adelaidasofia

## How We Met
Cloned the ai-brain-starter repo, found this example card in `templates/CRM-examples/`.

## Area of work
AI operating systems, knowledge graphs, founder workflows. Event planning for LATAM corporates.

## Writing
Two book series in progress on Substack. Weekly publishing in English and Spanish.

## Good At
Building AI systems that compound over time. Translating between technical and non-technical audiences. Asking the question that surfaces the real blocker.

## Notes
- Open-sourced the AI Brain system this repo ships
- Available for paid Brain Setup, Implementation Sprint, or Fractional engagements (see for-teams/working-with-me.md)
- Free 20-minute AI diagnostic at https://diazroa.com

## 5 Love Languages

If you have asked someone in your life to take the [5 Love Languages quiz](https://5lovelanguages.com/quizzes), drop their result into this section using the format below. The `inject-love-language-context.py` hook reads this section automatically whenever the person's name appears in your prompt, and uses it to shape how the assistant drafts messages, plans gifts, or designs apologies for them.

- Quality time — 35%
- Words of affirmation — 25%
- Physical touch — 20%
- Acts of service — 15%
- Receiving gifts — 5%
*Source: replace with where this came from — quiz date, a transcribed screenshot, a conversation, etc.*

The hook matches the person's filename, any frontmatter alias, and (if globally unique) their first name. Case- and accent-insensitive. Caches in `~/.claude/.love-language-index.json` and refreshes when any CRM file changes or the cache turns one day old. Delete this section if you do not want to use the hook — it silently no-ops for cards that omit it.

## How this card auto-populates

You fill this out once. The system keeps it alive after that:

- **Every journal entry** that mentions this person updates `person_journal_mention_count` and `person_last_journal_iso`. Runs nightly via the metadata extractor, or on demand with `/second-brain-mapping`.
- **Every meeting note** with this person gets auto-linked here under a `## Meeting Notes` section by the meeting workflow.
- **Floor co-occurrence** (`person_floor_cooccurrence`) tracks which emotional floors you tend to be on when you interact with this person. Surfaces patterns over months that you cannot see day-to-day.
- **Auto-wikilinks** connect this card back to any other vault file that mentions the person's name. You do not maintain the connections by hand.
- **Dataview queries** in your dashboards read these fields and surface views like "people I have not contacted in 30 days," "people on my low floors," "high-priority contacts with stale next steps."

The card you see today shows `mention_count: 0` and `last_journal: blank`. After 30 days of journaling and adding more people, this same card becomes a queryable index of your relationship with the person, not a static profile. Run `/second-brain-mapping` whenever you want to refresh the auto-populated fields across your whole vault. Once everything is interconnected, the system is at its most powerful.
