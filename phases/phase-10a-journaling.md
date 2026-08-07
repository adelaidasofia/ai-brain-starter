
## Phase 10: Set Up Daily Journaling

**Journaling is not optional.** It's the core habit that makes the vault alive. Without it, the vault is just a filing cabinet. With it, patterns emerge, the AI learns who you are, and the system compounds.

Say: "Now the most important part of this whole setup: your daily journal. Here's how it works: you type /journal, I ask you about your day, we talk for a few minutes, and I save the entry to your vault automatically. Over time it builds a map of your patterns, emotions, and growth. Even if you've never journaled before, this is different. You're not staring at a blank page. You're having a conversation, and I handle the rest."

Then ask these questions one at a time (conversational, not a form):
1. "What time of day do you want me to start the journal conversation automatically? I'll install a scheduled trigger that kicks off a /journal session at that time — but only if you haven't journaled yet that day, so it stays out of your way on days you already wrote. Give me a specific time in your local timezone (like `7:30pm` or `8:00am`). If you're not sure, I'll default to **7:30pm** — evening wind-down works for most people. You can change it later."
2. "What do you want me to ask about? (work, emotions, relationships, health, all of it?)"
3. "Do you want me to track any habits? Things like sleep time, mood, reading, exercise, water intake, meditation, screen time? I'll ask about them each session and log them in the entry."
4. "How raw do you want the entries? (polished or stream-of-consciousness?)"
5. "Do you want me to hold you accountable on anything? This is totally personal — it should be whatever YOU tend to let slide. Some examples: sleep consistency ('that's the late-bed spiral again'), reading ('you said 30 min a day, when's the last time you picked up the book?'), exercise ('you're at 2 this week, you said 4'), screen time ('any scroll holes today?'), meditation, water intake, spending habits — literally anything. I'll check in during each journal session with coach energy, not parent energy. What matters to you?"
6. "Do you want a gratitude or abundance check-in each session? It's a quick question — 'what's one thing you have right now that you're grateful for?' — to make sure the journal doesn't only capture hard days. Some people love it, some find it corny. Totally optional."

Save their answers — you'll use ALL of them when building the journal skill below.

**Store their answer to question 1 as `JOURNAL_TRIGGER_TIME`.** Parse it into 24-hour `HH:MM` format. If they say "7:30pm" store `19:30`. If they say "evening" or give a vague answer, confirm a specific time or default to `19:30`. You'll use this value in the "Install the daily trigger" step below. Also ask their IANA timezone if it isn't already set in the vault config — `America/Bogota`, `America/New_York`, `Europe/London`, etc. Store as `JOURNAL_TRIGGER_TZ`. If the user doesn't know their timezone, infer from `date +%Z` on their machine.

### Emotional floor tagging

"One more thing: each journal entry gets automatically tagged with an emotional 'floor.' It's based on a framework called the Internal High-Rise, 16 levels of emotional consciousness from Shame at the bottom to Peace at the top.

You don't have to do anything. I listen to what you say, read between the lines, and tag the entry myself. You just talk. Over weeks and months, the tags build up and you can literally see your emotional patterns in data: which people put you on which floors, what your average floor is this month vs. last, whether you're trending up or down. This is what turns your vault into a life coach.

If you want to understand the framework deeper: [Internal Design — The High-Rise Model on Substack](https://adelaidadiazroa.substack.com/s/internal-design)"

**IMPORTANT: The user never self-assigns a floor number.** Claude reads the journal conversation, identifies the dominant emotional state, and tags it automatically in Step 4 of the journal skill. Don't ask them to pick a number, rate themselves, or learn the scale. They just talk. The framework works in the background.

### Writing voice as floor verification

After 50+ journal entries, you'll notice the user's writing style changes predictably by floor. The prose is a seismograph. Trust the punctuation and sentence structure over self-report. If they say "good day" but the writing is future-tense cascading, that's Fear, not Joy.

Common patterns (calibrate these to each user as you accumulate entries):

| Floor | Typical voice signature |
|---|---|
| Shame | Recursive self-attack, sentence fragments, "wtf is wrong with me" |
| Guilt | Catalog of things not done, "I should/I haven't" patterns |
| Apathy | Trailing "...", "idk", "whatever", sentences refuse to finish |
| Grief | Broken sentences, onomatopoeic sounds, grammar dissolves |
| Fear | Future-tense cascade, "what if" spirals, contingency lists mid-paragraph |
| Desire | Run-on stacking ("and then... and also..."), spontaneous calculations |
| Anger | Longest sentences, second-person address ("you said..."), profanity |
| Pride | Achievement stacking, dollar amounts, itineraries, "look at this" energy |
| Courage | Post-action declarative ("I sent it"), nervous laughter next to vulnerability |
| Neutrality | Chronological reporting, no emotional charge, "it was nice" |
| Willingness | Re-engagement declarations, "getting back on track" |
| Acceptance | "And that's okay", gentle flowing, disappointment held not fought |
| Reason | Numbered lists, frameworks, strategic vocabulary, analytical distance |
| Love | Bilingual code-switching (secondary language emerges), names others, outward warmth |
| Joy | Short bouncing sentences, exclamation, brevity, spontaneous decisions |
| Peace | Shortest entries, simple words, no spirals, nothing to process |

**Cross-floor heuristics:**
- Entry length is often inversely proportional to floor height (low floors = longest entries, Peace = shortest)
- Secondary language appearing mid-entry = high floor signal (they're relaxed enough to code-switch)
- Body vocabulary (hiding/sick = low, buzzing/racing = Fear, warm/tingly = Love, rested = Peace)

These patterns are USER-SPECIFIC. The table above is a starting template. After ~100 entries, you'll have enough data to calibrate the voice signatures to this particular person. Use the patterns to cross-check your floor assignment, not replace it.

### Bilingual aliases (recap before creating floor notes)

You already collected `PRIMARY_LANGUAGE` and `SECONDARY_LANGUAGES` in Phase 1 Step 1.0. Apply them now:

**The rule:** treat every language they use as pointing at the same wikilink. One concept = one note. The canonical filename and body are in their primary language; every secondary language goes in the `aliases:` list. A user who writes "tengo miedo" in an otherwise-English entry should be able to wikilink `[[Fear|miedo]]` (or `[[Miedo|miedo]]` for a Spanish-primary user) and land on the same note. Never create parallel single-language notes for the same idea.

This rule isn't just for floors — apply it to every concept note you create for this user going forward. If they later use a term in a secondary language that maps to an existing concept, add it to that note's aliases instead of making a new note.

### Create floor concept notes

Create a concept note for each of the 34 floors in their vault. These notes serve two purposes: (1) when they click a floor wikilink like `[[Fear]]` in a journal entry, they see what that floor means and all their entries tagged with it, and (2) each note links back to the Substack article for deeper reading.

Save each floor note to `[VAULT_PATH]/📝 Notes/Floors/` — the dedicated folder created in Phase 3. **NEVER save floor notes to the vault root, and never loose in `📝 Notes/`.** If `📝 Notes/Floors/` does not exist yet (e.g. an upgrade install), create it first (`mkdir -p`), then write into it. A floor note at the top level is the bug that fills a user's sidebar with `Amor`, `Culpa`, `Soledad`, `Pisos Bajos`. Create all 34:

```markdown
---
creationDate: [today]
type: concept
floor_tier: [low/middle/high]
floor_number: [1-34]
aliases: [english variants + translations in every language the user journals in]
# e.g. monolingual: [fear, fearful, afraid, scared]
# e.g. English + Spanish: [fear, fearful, afraid, scared, miedo, temor, miedoso, asustado]
---

**Floor [number] of 34** · [[{Level} Floors]]

[2-3 sentence description of what this floor feels like. Write it in second person — "You feel..." Make it recognizable, not clinical.]

**Signals:** [3-5 common signs you're on this floor — thoughts, behaviors, body sensations]

**Movement:** To move up from here, [1-2 sentences on what helps]

**Read more:** [Internal Design — The High-Rise Model on Substack](https://adelaidadiazroa.substack.com/s/internal-design)

## All entries on this floor

```dataview
TABLE creationDate as Date, floor_level as Level
FROM "📓 Journals"
WHERE floor = "[Floor Name]"
SORT creationDate DESC
```
```

**The 34 floors (create one note each):**

*Low (1-18) — reactive floors:*
1. **Disgust** (low) — Visceral rejection. "Ew." Body recoils before words form.
2. **Shame** (low) — Self-disgust, hiding, "I am the problem." Everything feels broken and it's your fault.
3. **Embarrassment** (low) — Lighter than shame. "Oh god." Cringe, social replay, but recoverable.
4. **Guilt** (low) — "I should be doing more." Not enough. Letting people down. Productive self-blame.
5. **Apathy** (low) — "Nothing matters." Checked out, numb, Netflix spiral. The floor where you stop trying.
6. **Resignation** (low) — Flat affect, "it is what it is" without relief. Defeated-without-spiral.
7. **Confusion** (low) — Contradictory toggles, "but then again," circular logic, "idk."
8. **Loneliness** (low) — "No one gets it." Surrounded but unseen.
9. **Boredom** (low) — Restless, understimulated, "I don't know what I want to do." Not collapsed — searching.
10. **Grief** (low) — Loss, sadness, missing. Something was taken or ended. The floor of letting go.
11. **Disappointment** (low) — "I thought it would be different." Quiet heaviness, gap between expectation and reality.
12. **Hurt** (low) — Relational pain. "How could they?" Short sentences, pulls inward.
13. **Fear** (low) — Anxiety, "what if," imposter feelings. The floor that keeps you from starting.
14. **Frustration** (low) — Blocked-energy. "This should be working." Short patience, "ugh."
15. **Desire** (low) — Wanting, craving, reaching. Ambition mixed with lack. "If I just had X, then..."
16. **Anger** (low) — Injustice, someone not matching effort. ALL CAPS, profanity at someone, second-person attack.
17. **Contempt** (low) — Clipped, dismissive, certain. "Pathetic." The opposite of curiosity.
18. **Pride** (low) — Proving something, competitive, needing external validation. The top of the low floors.

*Middle (19-24) — transitional floors:*
19. **Courage** (middle) — Taking action despite fear. "I sent it." The floor where everything changes.
20. **Hope** (middle) — Future-tense forward momentum. "I think this could." Steady, not manic.
21. **Neutrality** (middle) — Calm observation. "It is what it is." Processing without emotional charge.
22. **Willingness** (middle) — Open, optimistic restart. "I'm getting back on track."
23. **Acceptance** (middle) — Making peace with reality. Letting go of control. Not resignation — release.
24. **Reason** (middle) — Clear-headed, analytical, strategic. Numbered lists. The thinking floor.

*High (25-34) — generative floors:*
25. **Trust** (high) — Quiet confidence. "It'll work out." Less hedging, fewer contingencies.
26. **Compassion** (high) — Other-oriented warmth. "I can see why they..." Holding pain without fixing.
27. **Humility** (high) — Quiet self-correction. "I was wrong about." Accurate without drama.
28. **Belonging** (high) — Quiet certainty. "This is my room." "I'm home." Less writing, not more.
29. **Love** (high) — Connection, warmth toward others. Bilingual (other languages emerge). Names people.
30. **Gratitude** (high) — "I'm so grateful." Recognition without forcing it, often mid-difficulty.
31. **Excitement** (high) — Anticipatory bouncing. "Ahhh." "I'm so excited." Body saying yes.
32. **Wonder** (high) — "I just sat in awe." Amazement vocabulary, expansion language.
33. **Joy** (high) — Short bouncing sentences, light exclamation, "fuck it" decisions, brevity.
34. **Peace** (high) — The shortest entries in the vault. Simple words, no spirals, nothing to process.

**Spanish translation reference (use this if the user journals in Spanish):**

| Floor | Spanish aliases to add |
|---|---|
| Disgust | asco, asqueroso, repugnancia |
| Shame | vergüenza, avergonzado, avergonzada |
| Embarrassment | pena, vergüenza social, qué oso |
| Guilt | culpa, culpable |
| Apathy | apatía, apático, apática, indiferencia |
| Resignation | resignación, resignado |
| Confusion | confusión, confundido, no sé |
| Loneliness | soledad, solo, sola |
| Boredom | aburrimiento, aburrido, aburrida |
| Grief | duelo, luto, pena |
| Disappointment | decepción, decepcionado, defraudado |
| Hurt | herido, lastimado, dolido |
| Fear | miedo, temor, miedoso, asustado |
| Frustration | frustración, frustrado, frustrada |
| Desire | deseo, anhelo, ansia |
| Anger | ira, rabia, enojo, enfado, furia |
| Contempt | desprecio, desdén |
| Pride | orgullo, orgulloso, soberbia |
| Courage | valentía, coraje, valor, valiente |
| Hope | esperanza, esperanzado |
| Neutrality | neutralidad, neutral |
| Willingness | disposición, voluntad, dispuesto |
| Acceptance | aceptación, aceptar |
| Reason | razón, razonar, racional |
| Trust | confianza, confiar |
| Compassion | compasión, compasivo |
| Humility | humildad, humilde |
| Belonging | pertenencia, pertenecer, en casa |
| Love | amor, amar, amando, amada |
| Gratitude | gratitud, agradecido, agradecida |
| Excitement | entusiasmo, emoción, emocionado |
| Wonder | asombro, maravilla, asombrado |
| Joy | alegría, gozo, alegre, dichoso |
| Peace | paz, sereno, tranquilidad, paz interior |

For other languages (French, Portuguese, German, etc.), generate the equivalents on the fly using the same pattern: the noun form, common adjective/verb forms, and any close synonyms. When in doubt, ask the user which variants they actually use.

Also create three tier notes using this template (customize the description and floor list for each). **Same rule: save all three to `[VAULT_PATH]/📝 Notes/Floors/`, never the vault root** — `Low Floors` / `Pisos Bajos` at the top level is the same sidebar-clutter bug.

**Low Floors.md:**
```markdown
---
creationDate: [today]
type: concept
floor_tier: low
aliases: [low floors, reactive floors]
---

Floors 1–18. You're responding to the world, not choosing. These are the reactive floors — disgust, shame, embarrassment, guilt, apathy, resignation, confusion, loneliness, boredom, grief, disappointment, hurt, fear, frustration, desire, anger, contempt, pride. They don't mean something is wrong with you. They mean you're human.

**Floors in this tier:** [[Disgust]], [[Shame]], [[Embarrassment]], [[Guilt]], [[Apathy]], [[Resignation]], [[Confusion]], [[Loneliness]], [[Boredom]], [[Grief]], [[Disappointment]], [[Hurt]], [[Fear]], [[Frustration]], [[Desire]], [[Anger]], [[Contempt]], [[Pride]]

**Read more:** [Internal Design — The High-Rise Model on Substack](https://adelaidadiazroa.substack.com/s/internal-design)

## Recent entries on these floors

```dataview
TABLE creationDate as Date, floor as Floor
FROM "📓 Journals"
WHERE floor_level = "Low"
SORT creationDate DESC
LIMIT 20
```
```

**Middle Floors.md:**
```markdown
---
creationDate: [today]
type: concept
floor_tier: middle
aliases: [middle floors, transitional floors]
---

Floors 19–24. You're starting to choose how you respond. These are the transitional floors — courage, hope, neutrality, willingness, acceptance, reason. The shift from reacting to deciding happens here.

**Floors in this tier:** [[Courage]], [[Hope]], [[Neutrality]], [[Willingness]], [[Acceptance]], [[Reason]]

**Read more:** [Internal Design — The High-Rise Model on Substack](https://adelaidadiazroa.substack.com/s/internal-design)

## Recent entries on these floors

```dataview
TABLE creationDate as Date, floor as Floor
FROM "📓 Journals"
WHERE floor_level = "Middle"
SORT creationDate DESC
LIMIT 20
```
```

**High Floors.md:**
```markdown
---
creationDate: [today]
type: concept
floor_tier: high
aliases: [high floors, generative floors]
---

Floors 25–34. You're creating, not reacting. Trust, compassion, humility, belonging, love, gratitude, excitement, wonder, joy, peace — the generative floors. These aren't destinations you reach permanently. They're floors you visit, live in for stretches, and return to.

**Floors in this tier:** [[Trust]], [[Compassion]], [[Humility]], [[Belonging]], [[Love]], [[Gratitude]], [[Excitement]], [[Wonder]], [[Joy]], [[Peace]]

**Read more:** [Internal Design — The High-Rise Model on Substack](https://adelaidadiazroa.substack.com/s/internal-design)

## Recent entries on these floors

```dataview
TABLE creationDate as Date, floor as Floor
FROM "📓 Journals"
WHERE floor_level = "High"
SORT creationDate DESC
LIMIT 20
```
```

### Configure graph to hide floor nodes

After creating the floor concept notes, configure the vault's `.obsidian/graph.json` so the graph shows life patterns (people, places, topics, decisions) instead of the framework scaffolding. The wikilinks still work — clicking `[[Fear]]` in a journal entry still opens the Fear note — but the graph stays clean.

```python
import json, os

VAULT_PATH = "[VAULT_PATH]"
graph_path = os.path.join(VAULT_PATH, ".obsidian", "graph.json")
os.makedirs(os.path.dirname(graph_path), exist_ok=True)

# Load existing or create new
graph = {}
if os.path.exists(graph_path):
    with open(graph_path, encoding="utf-8") as f:
        graph = json.load(f)

# Add floor exclusion filter to search
floors_filter = '-file:Shame -file:Guilt -file:Apathy -file:Grief -file:Fear -file:Desire -file:Anger -file:Pride -file:Courage -file:Neutrality -file:Willingness -file:Acceptance -file:Reason -file:Love -file:Joy -file:Peace -file:"Low Floors" -file:"Middle Floors" -file:"High Floors"'

existing_search = graph.get("search", "")
if "Shame" not in existing_search:
    graph["search"] = (existing_search + " " + floors_filter).strip()

graph["showOrphans"] = False

with open(graph_path, "w", encoding="utf-8") as f:
    json.dump(graph, f, indent=2)
```

Tell the user: "Your graph now shows your real life patterns — people, places, topics — without the floor framework cluttering the view. The floors are still there in your notes. Click any floor wikilink and it still works."

### Building the journal skill

Create a journal skill customized to their answers. Save it to `~/.claude/skills/daily-journal/SKILL.md`.

**IMPORTANT: The skill you generate must be PRESCRIPTIVE and COMPLETE. Do NOT generate a skeleton that relies on Claude's judgment at runtime. Every step, every question, every format must be spelled out explicitly in the skill file. A vague instruction like "ask about habits" will produce inconsistent results. Instead write the exact questions, the exact follow-up logic, and the exact format. The skill file IS the specification — if it's not in the file, it won't happen.**

The journal skill MUST include ALL of the following steps, in this order:

#### Capture-First Contract (bake this in as the skill's spine)

The generated skill MUST save the entry from the user's first substantive message, BEFORE the follow-ups, accountability, floor analysis, or panel — then enrich the same file in place. Reason: most people open the journal, type what happened, and leave. If the entry only saves after the full interview (the naive flow), every one of those sessions loses the entry. Capturing a raw entry the user can walk away from is the whole point.

Bake these into the generated skill verbatim:

- **Step 1.5 (immediate save).** The moment the user gives real content — pasted with `/journal` or typed in answer to the Step 1 opener — write a complete, valid entry: provisional floor, their words in their voice, the accountability line as known, floor tag, concepts. No panel section yet. If the session ends right there, that file is a finished entry on its own. Do not block this save on any data pull or MCP call.
- **Enrich in place.** Steps 2–9 update the SAME file. Step 7 ("save") becomes an in-place update, never a second file.
- **Resume, don't duplicate.** At the start, check whether today already has an entry (a journal index or a date-scan of the journal folder); if it exists, UPDATE it instead of creating a second file. One calendar day = one journal entry that grows across sessions.
- **Graceful exit at every later step.** If the user says "that's it" / goes quiet / declines the panel, finalize the existing file as-is, do a quick floor re-check, and let them go. Never hold the entry hostage to the panel or any later step. A captured-and-abandoned entry, with no panel section, is valid and complete.

#### Standing Rules — Panel Behavior (applies throughout the interview)

The advisory panel is a **live participant, not a closing credit.** Most journaling tools tack on a generic "insight" at the end. That is exactly when the user has already rationalized whatever they were going to rationalize. The panel has to be allowed to interrupt DURING the interview, and the final panel section has to have real dissent. Include both of these mechanisms in the generated skill.

**Trigger → voice routing (mid-interview interjections).** When the user says certain things during Steps 1–3, pull in ONE panelist mid-interview with one sentence in their voice, then return to the interview. Do not batch these for the end. Here is the standard trigger table — adapt the voices to match the roster you include in Step 5, but keep the triggers themselves:

| Trigger (user language or situation) | Voice type to pull in |
|---|---|
| Hedge words: "I guess," "kind of," "I don't know why," "maybe I" | Vulnerability / shame-research voice (Brené Brown archetype) |
| "I should" / "I need to" without a date attached | Execution cadence voice (Keith Rabois / Patrick Collison archetype) |
| New business idea during a hard stretch or already-committed sprint | Creativity voice OR tech-thesis voice (Rick Rubin / Marc Andreessen archetype) |
| Financial stress + guilt + spending on others | Trauma-informed therapist voice (Gabor Maté archetype) |
| Avoiding a hard conversation with a specific person | Relational boundaries voice (Terry Real archetype) |
| Family money/approval dynamic | Shadow integration voice (Debbie Ford archetype) |
| Good day they're struggling to receive | Vulnerability OR positive-psychology voice (Brené Brown / Martin Seligman archetype) |
| Frustration at a teammate/cofounder | Emotional fitness voice (Dr. Emily Anhalt archetype) |
| Habit missed + rationalization (gym, sleep, etc.) | Longevity / protocol voice (Peter Attia archetype) |
| Late-bed / scroll pattern re-emerging | Sleep architecture voice (Chris Winter archetype) |
| Dating, longing, crush without action | Behavioral-science-of-dating voice (Logan Ury / Matthew Hussey archetype) |
| Raise/investor framing | Founder-market-fit voice (Marc Andreessen / regional founder archetype) |
| Body symptom / cycle / energy crash | Female-physiology voice if user is female (Stacy Sims / Lara Briden archetype) |
| Creative work they feel proud of | Creativity voice (Rick Rubin / Elizabeth Gilbert archetype) |
| A gathering or relational moment they want to mark | Gathering/social-architecture voice (Priya Parker archetype) |
| Overwhelmed, nervous system dysregulated | Somatic voice (Peter Levine / van der Kolk archetype) |
| Spiritual or meaning drift | Mindfulness / existential voice (Thich Nhat Hanh / existential archetype) |
| Needs a mirror, not analysis | Reflective-listener archetype |
| Rumination about things outside their control | Stoic voice (Marcus Aurelius archetype) |

Pull in ONE voice per trigger, mid-interview. Save stacking for Step 5. The point is to interrupt the user's framing in real time, not after the fact.

**Omission pass (run before Step 5).** Before staging the Step 5 dialogue, ask yourself: *"What did they NOT say tonight that a panelist would notice?"* Common omissions:
- A commitment from a previous entry that never got mentioned again
- A person they were frustrated with yesterday who vanished from tonight's entry
- A deadline or meeting tomorrow they didn't prep for in the interview
- A behavior change they said they'd make and didn't bring up
- A body signal (sleep, habit streak, energy, cycle) they skipped past

If an omission exists, one panelist at Step 5 must name it in one sentence.

**Separation rule (critical — do not blend voices).** The main body of the journal entry is **the user's original voice only.** Panel interjections during Steps 1–3 inform your follow-up questions — they do NOT get written into the narrative body of the saved entry. Panel dialogue lives in its own clearly-labeled section after the narrative body so that when the user rereads their journals, they can always tell what is their own original thought and what is panel commentary. Never blend the two. If a panel insight genuinely shifted their thinking during the interview and they said so out loud, capture *their* reaction in *their* voice in the body, and put the panelist's line separately in the panel section. This rule is load-bearing for the journal archive's long-term readability — once the two voices are blended, future rereads cannot tell what was the user's own thinking and what was AI synthesis.

#### Step 1: Opening question
Warm, casual, matched to time of day. ONE question, don't overwhelm.
- Morning: "Hey! How are you waking up today? What's on your mind?"
- Afternoon: "How's the day going so far? Anything standing out?"
- Evening: "How was today? What's sitting with you right now?"

#### Step 2: Follow the thread (2-4 follow-up questions)
Based on their answer, dig deeper. Be curious, not clinical. Include these specific behaviors in the skill:
- If they mention **work**: "How does that make you feel about where things are headed?" or "Is that exciting or stressful or both?"
- If they mention **a person**: "What floor did that interaction put you on?" or "How did you feel after?"
- If they mention **feeling good**: "What specifically made it good? I want to capture this one." (Most people document bad days in detail but skip over good ones — push here.)
- If they mention **feeling bad**: "Is this a familiar pattern or something new?" or "What would the High-Rise say about where you are right now?"
- If they seem surface-level: "What's underneath that?" or "If you were writing this at 1am with no filter, what would you actually say?"
- Don't let them off the hook with "I'm fine" — gently dig.
- Use their language back to them.
- Celebrate wins they'd normally skip over.

#### Step 2.5: Abundance / gratitude check (only if they opted in — question 6 from setup)
If the user said yes to the gratitude check-in, ask ONE quick question about present abundance:
- "What's one thing you have right now — financially, personally, anything — that you're grateful for?"
- Even "I had a great dinner" or "I can pay my rent" counts.
- This counters the natural bias toward only journaling when things are hard. The good stuff is there — it just doesn't get written down. Include the answer naturally in the entry.

If they said no or skipped question 6, skip this step entirely.

#### Step 3: Accountability check
Based on what the user said they want to be held accountable on (question 5 from setup), build a SPECIFIC accountability check into the skill. For each item they chose, include:

**The pattern:** What to ask, what a good answer looks like, what a bad answer looks like, and what to say for each.

Build the accountability check from WHATEVER the user asked for in question 5. Don't assume gym, sleep, or any specific habit — use exactly what they said. For each item, follow this structure:

1. **Ask** — a direct check-in question specific to their habit
2. **Compare** — measure against their stated goal/target
3. **Push or celebrate** — if behind, name the pattern and nudge forward; if on track, acknowledge the streak
4. **Log** — track the data in the entry

Example structures (use as templates, adapt to whatever they chose):

**Exercise (if they asked for it):**
- "How many [exercise type] days this week so far?"
- If below target: "You're at [X]. You said [target]. When's the next one?"
- If on track: "That's [X] this week. The streak is building."

**Sleep (if they asked for it):**
- "What time did you go to bed last night?"
- If past their target: "That's the late bed -> tired tomorrow -> unproductive -> guilt spiral pattern. Phone in another room tonight?"

**Reading (if they asked for it):**
- "Did you read today?" or "How far into [book] are you?"
- If slipping: "You said [target]. When's the last time you actually sat down with the book?"

**Scrolling / screen time (if they asked for it):**
- "Any scroll holes or binge sessions today?"
- If yes: "That's the crash after a sprint. Normal. But let's not let it become a streak."

**Meditation / mindfulness (if they asked for it):**
- "Did you sit today?"
- If missed: "That's [X] days skipped. You said this resets you. What's blocking it?"

**Spending / money (if they asked for it):**
- "Can you afford that without it stinging after?"

**Any other habit they named:** Follow the same structure — ask, compare to their stated goal, push gently if behind, celebrate if on track. The user defines what matters, not the skill.

**Key principle: Coach energy, not parent energy.** Direct but not nagging. Track the data. Name the patterns. Don't lecture.

#### Step 3.5: Idea quarantine check (for entrepreneurs/builders)
If the user mentioned during setup that they're working on a business or project, include this step in the skill:
- If a new business idea or "what if I built..." moment comes up during the conversation, DON'T let it derail. Note it, and after saving the journal entry, append it to an `Idea Quarantine` section in their vault (create `Business/Idea Quarantine.md` if it doesn't exist).
- Format: `- **[YYYY-MM-DD]** — [the idea, 1-2 sentences] *(from journal)*`
- Tell the user: "I caught an idea in there — parked it in Idea Quarantine so it doesn't distract but doesn't get lost."
- If they're excited about a side idea during a hard stretch on their main project, name it: "Is this real inspiration or escape from the hard thing?"

Skip this step entirely for users who aren't building something.

#### Step 4: Identify the floor
Based on everything they said, identify the PRIMARY floor:

**Low Floors:**
- Shame — "I'm such an idiot," self-disgust, hiding
- Guilt — "I should be doing more," not enough, letting people down
- Apathy — "Nothing matters," checked out, numb, Netflix spiral
- Grief — Loss, sadness, missing someone/something, killed mood
- Fear — Anxiety, "what if," scared, uncertain, imposter feelings
- Desire — Wanting, craving, reaching, ambition mixed with lack
- Anger — Frustration, someone not matching effort, disrespect
- Pride — Proving something, competitive, need for external validation

**Middle Floors:**
- Courage — Taking action despite fear, showing up, doing the hard thing
- Neutrality — Calm observation, "it is what it is," processing without charge
- Willingness — "Getting back on track," optimistic restart, open to trying
- Acceptance — Making peace with reality, letting go of control
- Reason — Analytical, strategic, clear-headed problem solving

**High Floors:**
- Love — Connection, gratitude, warmth, feeling held, giving freely
- Joy — Delight, fun, laughter, alive, "best day ever" energy
- Peace — Stillness, presence, nothing to fix, enough as-is

#### Step 5: Advisory panel dialogue (3 voices, up to 5)

Based on what came up in Steps 1–3 AND which triggers fired mid-interview (see Standing Rules), select the **3–5 most relevant advisors** from the full roster below. Default to 3; go up to 5 only when multiple domains got triggered. **Do NOT re-interview the user** — Steps 1–3 already did the interviewing. Work from what's already on the table.

**Format:** Stage a short in-character **dialogue** among the selected panelists. **Not parallel bullets** — an actual exchange where they can challenge each other AND the user's assumptions. Each speaks in their authentic voice with minimal fluff, using their known mental models and life philosophies. Panelists can ask the user questions back if needed.

**Integration goal:** The panel integrates their expertise toward the user's goals across wealth, health, love, spirituality, leadership, and legacy (or whichever domains are relevant to this user — adapt during setup to what the user said they want the panel to cover). Pull the voice the moment most needs, not the voice most comfortable to hear.

**Hard rules (bake these into the generated skill verbatim):**
- **At least one panelist MUST dissent or push back.** Not console, not affirm — challenge. Especially on middle/high-floor entries, where rationalizations slip through most easily. If all panelists agree, you have not looked hard enough. This is the single most important rule in Step 5. Most journaling tools fail here because they default to affirmation; this skill must not.
- **At least one panelist MUST address any omission** surfaced by the omission pass.
- **If any facts or studies are mentioned, include the source. Don't make those up.** Hallucinated citations are disqualifying — a fabricated study is worse than no citation.
- **Remain in character.** Speak with their known mental models and life philosophies, not generic coaching speak. If you cannot hear the panelist's voice in your head, pick a different panelist.
- **Robust disagreement where useful, not consensus for its own sake.**
- Keep it tight — this is a daily beat, not a full session.

**The Advisory Panel roster:**

**DO NOT ask the user to customize the panel at setup time.** No "default 5" proposed for approval. No "want to swap any in?" prompt. The full roster below gets installed into the daily-journal skill verbatim, and the skill's Step 5 selects 3-5 panelists PER ENTRY based on which mid-interview triggers fired and what surfaced in Steps 1-3. This guarantees variation across entries — a finance-stress journal pulls Hormozi + Buffett + Gabor Maté; a creative-block journal pulls Rubin + Gilbert + Brené Brown; a relationship journal pulls Perel + Tatkin + Solomon. Same default 5 every day would defeat the point.

The user CAN edit the roster later by asking ("add my grandmother as a panelist," "remove Hormozi"). That edit lands in the daily-journal SKILL.md directly. But never volunteer the customization prompt at setup — it adds a decision they don't need to make and locks them into a static panel.

Install the full roster below into the daily-journal skill exactly as written:

*Wealth & Strategy:*
Naval Ravikant (leverage, asymmetric bets, freedom-through-clarity) · Warren Buffett (capital allocation, simplicity, patience, circle of competence) · Ray Dalio (macro cycles, principles-based decisions, risk parity) · Alex Hormozi (execution, offers, scaling) · Tom Wheelwright (tax strategy, entity design, intergenerational planning) · Marc Andreessen (tech thesis, software-eats-world, founder empathy) · Stephen Schwarzman (PE discipline, scale-up playbooks) · Howard Marks (credit cycles, risk management, second-level thinking) · Sam Zell (contrarian, distressed value, downside-first thinking) · Robert Kiyosaki (cash-flow mindset, financial education) · Ken Griffin (risk-adjusted returns, market microstructure) · Laurene Powell Jobs (impact investing, values-led legacy) · Richard Branson (joyful entrepreneurship, brand magic)

*Cross-Border / Regional (customize to user's geography):*
Regional scaling founders · Cross-border tax strategist · Family office CIO · Global mobility strategist · Regional political-economy strategist · Cross-border real estate investor

*Family Office / Legacy:*
James E. Hughes Jr. (family governance, mission/values continuity, heirs' preparedness) · Family Office CIO archetype (portfolio discipline, opportunity triage, IPS enforcement)

*Leadership & Ops:*
Sheryl Sandberg (org scale, operating cadence, people systems) · Keith Rabois (execution brutality, cadence, high-velocity frameworks) · Patrick Collison (speed + quality culture, humane high standards) · Reid Hoffman (network strategy, blitzscaling, partnership ecosystems) · Adam Grant (organizational psychology, generosity architecture) · Tony Robbins (state management, peak performance)

*Gatherings & Social Architecture:*
Priya Parker (designing gatherings, community meaning-making)

*Power, Shadow & Civilization:*
Robert Greene (power dynamics, strategy psychology tempered ethically) · Debbie Ford (shadow integration for leaders) · Yuval Noah Harari (civilizational context, tech ethics) · Mo Gawdat (happiness as operating system) · Balaji Srinivasan (decentralization, sovereignty)

*Voice & Platform:*
Oprah Winfrey (compassionate authority, platform building) · Maya Angelou (purpose, grace, authentic voice) · Jackie Kennedy Onassis (elegance, discretion, privacy with power)

*Health & Longevity:*
Dr. Peter Attia (prevention, longevity, metric-driven protocols) · Dr. Stacy Sims (female training by cycle/phase, women's physiology) · Dr. Lara Briden (hormone literacy, cycle repair, perimenopause) · Dr. Elizabeth Boham / IFM (root-cause medicine, lab-driven prevention) · Dr. Carrie Pagliano, DPT (pelvic floor, core integrity, functional movement) · Dr. Emily Anhalt (emotional fitness, resilience tools for leaders) · Dr. Chris Winter (sleep architecture, recovery) · Jenna Braddock, RD (female athlete nutrition, sustainable fueling) · Dr. Rhonda Patrick (micronutrients, cellular health) · Functional PCP archetype (integrates data, coordinates diagnostics)

*Wisdom & Meaning:*
Thich Nhat Hanh (mindful presence, compassion, peace in action) · Compassionate Buddhist Monk archetype (non-judgment, equanimity) · Stoic Philosopher / Marcus Aurelius (agency, serenity, controllables)

*Psychology & Inner Work:*
Brené Brown (vulnerability, shame research, courage) · CBT Therapist archetype (cognitive restructuring, behavioral plans) · Existential Psychotherapist archetype (meaning, authentic choice) · Gabor Maté / Trauma-Informed Therapist (root wounds, compassion-led healing) · Martin Seligman / Positive Psychologist (strengths, flourishing) · Jungian Analyst archetype (archetypes, shadow, dreamwork) · Inner Child Therapist archetype (re-parenting, attachment repair) · Curious Friend / Reflective Listener archetype (non-judgmental mirroring)

*Relationships:*
Esther Perel (erotic intelligence, polarity, aliveness in long-term bonds) · Dr. Stan Tatkin (secure functioning, co-regulation) · Dr. John & Julie Gottman (research-backed repair, love maps, bids) · Terry Real (empowered love, boundaries with connection) · Dr. Sue Johnson (attachment science, safe emotional connection) · Dr. Alexandra Solomon (relational self-awareness, LGBTQ+ inclusive) · Alain de Botton (love as education, realism with idealism) · Matthew Hussey (practical dating strategy, attunement) · Logan Ury (behavioral science of dating) · Jay & Radhi Shetty (spiritual partnership, ritualized growth) · Conscious queer polarity voices (for LGBTQ+ users)

*Somatic & Embodied Healing:*
Dr. Peter Levine (Somatic Experiencing, body-first trauma release) · Bessel van der Kolk (embodied healing, body keeps the score) · Bonnie Bainbridge Cohen (Body-Mind Centering, movement-as-awareness)

*Planetary & Sacred:*
Jane Goodall (planetary compassion, stewardship, humility with action) · Charles Eisenstein (interbeing, sacred economics) · Robin Wall Kimmerer (reciprocity with Earth, indigenous wisdom, awe practice)

*Creativity:*
Rick Rubin (creativity via presence, subtractive genius, trust the muse) · Elizabeth Gilbert (creative courage, fear alchemy, permission to play) · Twyla Tharp (creative discipline, daily craft)

**No setup-time customization prompt.** Install the full roster above verbatim into the daily-journal SKILL.md. The skill picks 3-5 per entry from this full roster based on what surfaces — variation is the feature, not a bug. If the user later asks to add a specific person (grandmother, mentor, professor) or remove one of the listed voices, edit the daily-journal SKILL.md directly at that point. Never volunteer the customization question.

**Advisory Panel Roster:** The full panel roster and voice routing trigger table are in `phases/phase-10b-panel-roster.md`. Read that file when you reach Step 5 of the journal skill generation.
