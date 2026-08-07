# Journaling Methodology

A conversational check-in that tags each entry with the floor you were on. Over time the tag becomes infrastructure: patterns appear, loops get named, and you stop being surprised by your own weather.

## The method

1. **Interview, don't prompt.** Ask open questions about the day and let the person talk. Three shapes work: an evening check-in (what happened, what it felt like), a short mid-day pulse, and a morning intention.

2. **Identify the primary floor — then, if the day moved, the arc.** At the end, name the one primary [floor](../floors.md): the floor the entry *landed* on (on a still day, simply where you mostly were). Naming a single base floor is deliberate, not a limitation — it is the move that reduces the charge, and it keeps the long-run dataset clean. Tag it explicitly. Then, when the entry moved from one floor to another, record the path in order:

   ```
   floor: Hope                            # the primary/base floor — where the entry landed
   floor_level: Middle
   floor_arc: [Fear, Frustration, Hope]   # optional: the floors moved through, in order
   ```

   The arc is the point. This framework's own claim is that *the elevators are the lesson; the floors are where the door opens* — so the movement Fear → Frustration → Hope carries more than any single endpoint. `floor_arc` captures both the other floors that were present (they are in the list) and the order you moved through them (the elevator). On a still day with no movement, omit it — one optional ordered field, not a second "floors present" list, because the arc already contains them.

   **The payoff is in review, not storage.** Because the arc records transitions, a weekly or monthly pass can surface your recurring *elevators* — "your most common movement this month was Fear → Frustration" — which is the pattern the whole framework points at. Statistics stay clean either way: count the primary `floor` alone, or count every floor in `floor_arc`.

   Keep capture frictionless: always ask the one primary-floor question; offer the arc only when the person signals the day moved, and never prompt for it on a still day.

3. **Check the shadow.** If a high floor comes too easily, ask whether a shadow twin is in costume. Most "Acceptance" is Resignation; most "Confidence" is Pride. The tell is whether the body is still bracing.

4. **Save the person's own voice.** The entry body is written in the first person, in the writer's own words and language. Any reflection or commentary lives below a divider, never mixed into their voice.

5. **Optionally track behavior.** A few anchors the person cares about — movement, sleep, focus, rest — can be logged alongside the floor, so body data and emotional data sit side by side.

## Why the floor tag matters

The tag is the seed. One entry is a diary. A hundred entries tagged by floor is a navigable dataset: which floors you spend the most time on, which you cycle through, which loops you are stuck in, how you move between them. The methodology's value compounds — the longer you run it, the more it can tell you about yourself.

## Language

Run the entire check-in in the language the person writes in. Every step — the questions, the floor tag, the entry — is in that language. The floors carry both English and Spanish names (see [`floors.md`](../floors.md)).
