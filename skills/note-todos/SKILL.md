---
name: note-todos
description: 'Use when the user says /note-todos, asks to pull, extract, or file action items or to-dos from a non-meeting note (class notes, book notes, podcast notes, transcripts, panel writeups, strategy docs, action plans), or mentions tasks buried in a note that never reached a to-do list: unfiled checkboxes, orphan action items, ''what tasks are hiding in my notes''. For meeting notes prefer meeting-todos, which carries the full meeting workflow cascade.'
argument-hint: "[filename, path, or keyword — omit to scan recent unprocessed notes]"
---

## What this skill does

Action items hide in notes you don't think of as task lists. A class note, a book chapter, a podcast transcript, a panel writeup — each one accumulates "I should do X" between the substantive content. Without a routing pass, those items stay invisible until weeks later when you wonder why nothing got done.

This skill does the routing pass. It reads a note in full, extracts every actionable item, decides which scope each task belongs to (personal / team / consulting / etc), wraps entities in wikilinks, tags owners inline, writes to the correct canonical to-do file, and annotates the source note so future reads know the items have been ported.

It is the non-meeting counterpart to `/meeting-todos`, which carries the 8-step meeting workflow cascade (transcript fetch, speaker attribution, decision capture, etc) on top of the same extraction logic. If the input is a meeting, route to `/meeting-todos` instead.

## Trigger

`/note-todos` — optionally followed by a path, filename, or keyword:
- `/note-todos` → finds candidate notes in recent activity (modified within 14 days, contain 3+ open `- [ ]` items, no "filed-on" annotation at the section head)
- `/note-todos {keyword}` → globs for notes matching keyword in filename or first 200 chars of body
- `/note-todos {full-path-to-note.md}` → opens the exact file

Tell the user which file you found before proceeding.

## Step 1 — Read the note in full

Always read the entire note. Action items can appear:
- Inline in prose ("I should email her", "next week I want to...")
- In dedicated sections labeled "Action items," "Next steps," "TODO," "Para mí," "To do" (any case)
- In tables where one column is "Action" or "Owner"
- In footnotes or appendix lists

Don't skim. Skipping prose-buried items is the failure mode this skill exists to prevent.

## Step 2 — Detect scope

A vault commonly has multiple to-do files for different areas of life or work. The skill needs to route each task to the right file. Determine scope from a combination of: (a) the source note's path, (b) entities mentioned in the task, (c) verbs in the task ("ship the deck for X" vs "memorize 10 facts").

Configure your scopes in your vault's CLAUDE.md or in a sibling `note-todos.config.md` file with this shape:

```yaml
scopes:
  - name: personal
    to_do_file: "To-dos/Get to-do.md"
    detection_signals:
      paths: ["Notes/", "Books/", "Journals/"]
      keywords: []  # default scope when no team/consulting signals fire
    inline_fields_template: "[impact:: 1-5] [urgency:: 1-5] [effort:: S|M|L] [commit:: Y|N]"
  - name: team
    to_do_file: "Team/To-dos/Team To-dos.md"
    detection_signals:
      paths: ["Team/"]
      keywords: ["{client name}", "{product name}", "{cofounder names}"]
    inline_fields_template: "[owner:: NAME] [area:: AREA] [priority:: 1|2|3]"
  - name: consulting
    to_do_file: "Consulting/To-dos/Get to-do.md"
    detection_signals:
      paths: ["Consulting/"]
      keywords: ["{your consulting brand}", "{client list}"]
    inline_fields_template: "[impact:: 1-5] [urgency:: 1-5] [effort:: S|M|L] [commit:: Y|N]"
```

If your vault only has one scope, the config simplifies to one entry and Step 2 is a no-op.

If a single note crosses scopes (e.g. a class note has both team-marketing tasks and personal-life tasks), split routing per-task. If unclear, ask the user once: "This note has tasks for {scope A} and {scope B}. Route accordingly?"

## Step 3 — Extract action items

For each `- [ ]` item or imperative line:

1. **Cleanup.** Strip stale arrow-notation (`-> owner: NAME`), parenthetical class meta, trailing fluff like "{teacher} expects this for retention." Keep the verb-first action core.

2. **Identify owner.** Two cases:
   - **Self tasks:** anything where the user is directly named or implied as the doer ("audit my deck," "memorize 10 facts," "review X").
   - **Others' tasks (team scope only):** anything a team member could execute. Route to the team's canonical to-do file with `[owner:: TEAMMATE_NAME]`. Do NOT write to a per-person file directly — Dataview views render per-person lists from the canonical file.
   - **Co-founder / peer tasks:** use a collaborative tone, not directive. Frame as support ("Review X" rather than "Read X"), include the WHY in the task body so the receiver has context.

3. **Wikilink entities.** Wrap people, projects, companies, frameworks, and concepts in `[[wikilinks]]`. Check existing notes for the canonical wikilink form before guessing — if "[[Jane Smith]]" exists in the vault, don't write "[[Jane]]" or "[[J. Smith]]." Don't auto-wikilink generic verbs (audit, email, review).

4. **Add inline fields per scope.** Use the `inline_fields_template` from the scope config. Common templates:
   - Personal scoring: `[impact:: 1-5] [urgency:: 1-5] [effort:: S|M|L] [commit:: Y|N] [score:: COMPUTED] [priority:: P1|P2|P3]`
   - Team assignment: `[owner:: NAME] [area:: AREA] [priority:: 1|2|3]`

5. **Self-contained-task rule.** Every task MUST be readable out of session context. If a task fails the self-contained test, enrich from the source note before filing. Apply the 4-anchor minimum: at least one of (a) bracketed context prefix like `[Class Day 5]`, (b) wikilink, (c) URL, (d) file path. Tasks like "Verify PDF" or "Review the doc" without anchors FAIL — add `[{source-note-name}]` or similar before writing.

## Step 4 — Confirm before writing

Show a routing preview grouped by destination scope:

```
Routing preview from {Note Name}.md:

→ Personal (To-dos/Get to-do.md):
  • [Class Day 5] Memorize 60-second scripts for 3 conviction lines [impact:: 4] [urgency:: 3] [effort:: M] [commit:: Y]

→ Team (Team/To-dos/Team To-dos.md):
  • [[Pitch Deck v3]] Re-frame the opening to lead with reward, not pain [owner:: SELF] [area:: marketing] [priority:: 2]

3 tasks total. Proceed?
```

Wait for confirmation before writing.

## Step 5 — Write

Append each task to its destination file under an appropriate section header:
- Use the existing convention in the destination file. Most to-do files group items by source ("From Meetings," "From Class Notes," etc) or by priority.
- If a fitting section exists, append there.
- If novel, create `## From {Note Name} — YYYY-MM-DD` matching the file's existing section style.

For team scope: write to the canonical team file with the right `[owner::]`. Do not write to per-person files; let Dataview views render them.

## Step 6 — Annotate the source note

At the head of the action-items section in the source note, prepend:

```
> Filed to {to-do-file} on YYYY-MM-DD by /note-todos. {N} tasks routed ({A} personal, {B} team, ...).
```

This signals to future readers that the items are tracked and avoids the "list lives here, no one ever sees it" failure mode. The annotation is also a guard against this skill re-routing the same items twice — Step 0's candidate scan skips notes that already have a "Filed to" annotation.

## Step 7 — Surface conflicts

- **Duplicates.** If a task appears in BOTH the source note AND already exists in the target to-do file (deduped by lemma match), skip the duplicate, name it in the preview ("Skipping: '...' already in line N").
- **Broken wikilinks.** If a task references a wikilink that doesn't resolve to any vault file, warn at write time and suggest creating the stub.

## Step 8 — Run the orphan-list guard

After writing, optionally invoke a vault-wide orphan-task-list scanner if your vault has one configured. The guard's job is to confirm the source note no longer triggers as a violator (because action items have been ported AND the section is annotated). If the source note is still flagged, surface the residual unowned items for review.

## Backward compatibility

`/meeting-todos` continues to fire on `/meeting-todos` and runs the meeting workflow cascade (transcript fetch, speaker attribution, decision capture, doc generation), which is more than just to-do extraction. `/note-todos` does the to-do extraction subset for non-meeting notes.

If a user runs `/note-todos` on a meeting note: warn that `/meeting-todos` is the better skill for meetings, but proceed if confirmed.

## Test surfaces

- A class or book note with an "Action items for me" section
- A panel writeup with 10+ unowned items in prose
- A relationship-action plan or strategy doc with mixed personal + team items
- A podcast transcript with sidebar action items between content blocks

## Why this skill exists

Action items in source notes that aren't ported to canonical to-do files are invisible work. Notes accumulate, the action items bloat, and weeks later you find a list that nobody surfaced. The orphan-action-items pattern is the failure mode this skill closes for non-meeting notes.

Generalizing the meeting-todos extraction logic to handle any note type lets the same routing + wikilink + owner-tagging pattern flow through the whole reading pipeline, not just meetings.
