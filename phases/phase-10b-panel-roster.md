# Phase 10b: Advisory Panel Roster & Journal Completion

This file is a continuation of Phase 10a (journaling setup). Read this when generating Step 5 of the journal skill.

Run the full panel setup as written below. Every user gets the advisory panel — it's the feature that makes journaling generative instead of confessional.

---

#### Step 6: Confirm the enrichment (the entry is already saved)

By now the entry was already written to disk at Step 1.5 (capture-first — see Phase 10a's Capture-First Contract). This step only confirms the enrichment and shows the panel before it lands in the file.

If the panel ran, show the full panel section inline, then: "Okay, here's the panel, and I'd tag this [Floor]. Approve as-is, or swap/edit a voice?" On confirmation, UPDATE the saved file (Step 7).

If the user opted out of the panel or disengaged, the captured entry stands as-is. Confirm what was saved and let them go. Never hold the entry until after the panel — the capture already happened.

#### Step 7: Finalize the entry (in-place update of the file from Step 1.5)

**This is an UPDATE of the file already written at capture time, not a fresh create.** Rewrite it in place with the finalized floor, the enriched body, the accountability line, and — only if the panel ran — the `## Panel dialogue` section. Never create a second file for the same session. If you refined the filename to match the day's theme, rename in place rather than leaving a stale duplicate.

**File location:** `[VAULT_PATH]/📓 Journals/` — use the vault path from setup. This MUST be the user's actual vault path, verified during Phase 3. The folder is created with the 📓 emoji prefix in Phase 3 — do not save to a plain `Journals/` folder.

**Filename format:** Descriptive title from the content (5-8 words, Title Case):
- "Great Meeting Feeling Momentum.md"
- "Hard Conversation Stayed Calm.md"
- "Low Energy But Got Through It.md"

**Entry format — NOTE the strict separation between the user's original voice and synthetic panel commentary:**

```markdown
---
creationDate: YYYY-MM-DDTHH:MM
floor: [Floor name]
floor_level: [low/middle/high]
[any habit fields they requested, e.g. exercise_count: 3, sleep_time: 11pm, reading_mins: 30]
---

## Journal — [User's first name]'s voice
[The journal entry — written in FIRST PERSON as the user, in their voice. Stream of consciousness, casual, honest. Include the details they shared. Don't clean it up too much — journals should be raw and real. But DO capture insights that surfaced during the conversation that they wouldn't have written on their own. **This section contains the user's original thought only. Panel voices, advisor names, and synthetic dialogue NEVER appear here.** If a panel insight shifted their thinking during the interview and they said so out loud, write THEIR reaction here in THEIR voice — put the panelist's actual line in the panel section below.]

[If they opted into the gratitude check-in, include the abundance/gratitude note naturally woven in.]

[Accountability tracking line, e.g.:]
[Whatever they chose to track, e.g.:]
**Sleep:** [time to bed] · **Reading:** [X] min · **Exercise:** [X]/[target] this week

---

## Panel dialogue

[Short staged exchange among the 3–5 selected panelists — actual dialogue, not parallel bullets. Panelists talk to each other and to the user. At least one dissent must be clearly visible. Keep it tight.]

**Dissent:** [One line naming who pushed back and what they challenged. Always present — this is not optional.]
**Omission flagged:** [One line, only if the omission pass surfaced something — otherwise remove this line entirely]

---

*Floor: [[{Floor}]] · [[{Level} Floors]]*

## Concepts
[[Tag1]] | [[Tag2]] | [[Tag3]]
```

**Why the separation matters:** The single biggest long-term failure mode of AI-assisted journaling is **voice blending** — where users cannot tell, on reread 6 months later, which sentences were their own thinking and which were AI synthesis. The journal archive loses its value as a record of how the user actually thinks. The horizontal rules, the explicit `## Journal — [name]'s voice` header, the `⚠️` disclaimer, and the separate panel section together make voice-blending structurally impossible. Do not let the generated skill collapse these sections into a single narrative. Do not let the panel's lines appear in the body paragraph. This rule is non-negotiable.

**CRITICAL — Post-save verification:**
After writing the file, VERIFY it exists and is not empty. Use the Read tool to confirm the file was saved. If the save fails for any reason (wrong path, missing folder, permissions), TELL THE USER IMMEDIATELY. Say what failed and offer to retry. **Never let a journal entry be lost.**

#### Step 7.5: To-Do Extraction

After saving the journal entry, scan the full conversation for **action items, follow-ups, or things the user said they need to do**. Look for:
- "Remind me to..." / "I need to..." / "I should..." / "I have to..."
- Follow-ups promised to people
- Conversations they flagged as needed ("I need to have that hard talk with X")
- Events or deadlines mentioned that need a task attached

If you find any:
1. Read the user's to-do file (check CLAUDE.md for path — typically `Home/✅ Get to-do.md` or similar)
2. Check for duplicates before adding
3. Add a new dated section near the top (after any urgent section):

```markdown
## 📋 From Journal — [YYYY-MM-DD]

- [ ] [task 1 — specific, include context so future-you knows why]
- [ ] [task 2]
```

4. Update `updated:` in frontmatter to today
5. Tell the user: "I also pulled [X] to-dos from the journal and added them to your list."

If no clear action items came up, skip silently — don't force it.

#### Step 8: After saving
Tell them the file name and floor. Connect to patterns when possible:
- "This is your 3rd Courage entry this month — you're on a streak."
- "Last time that person came up, you were on Anger. Today it's Acceptance. That's movement."
- "You mentioned money stress + a new idea in the same breath. Classic escape pattern. Just flagging it."
- If an idea was quarantined: "Parked [idea] in Idea Quarantine. Main project first. But it's saved."
- Habit count: "You're at [X]/[target] this week. [Encouragement or push as appropriate.]"

#### Step 9: Auto-log panel dissents and omissions to the panel feedback log

If Step 5 surfaced a **dissent** or an **omission flag**, automatically append it to a panel feedback log at `[VAULT_PATH]/Home/Panel Feedback Log.md` (or whichever path the user set up during Phase 4 for this log — check CLAUDE.md). If the file doesn't exist, create it with a short header explaining it's a cross-context log of every real and synthetic panel reaction.

Append format:

```markdown
### YYYY-MM-DD — Daily journal dissent / omission

⚠️ **Synthetic panel reaction from /journal, not real investor or advisor feedback.**

**Context:** [1 line — what came up in the entry that triggered the dissent/omission]
**Panelists:** [names of selected voices]
**Dissent:** [verbatim from the entry's Dissent line — attribute to the panelist who said it]
**Omission flagged:** [verbatim from the entry's Omission line, if any]
**Entry:** [[{filename without .md}]]
```

**This is automatic — never ask the user to approve the log append.** The point is to close the loop between daily journal pushback and the broader panel feedback record so patterns become visible over time (if three different daily entries all got the same dissent, that's a real pattern to act on, not a random note). If there's no dissent or omission from the entry (which shouldn't happen if Step 5 was followed correctly — dissent is required), skip the log append silently.

**Important principles for the generated skill:**
- Write the entry AS the user, not about them
- Keep their voice — people write journals in long flowing paragraphs, thinking out loud
- Include specific details (names, places, what happened)
- If they surfaced something new in the conversation that surprised them, make sure it lands in the entry
- Don't over-polish. The best entries are messy and real.
- **Strict voice separation.** The `## Journal — [name]'s voice` section contains ONLY their original thought. Panel voices, advisor names, and synthetic dialogue NEVER appear there. Panel commentary lives exclusively in the `## Panel dialogue` section below the horizontal rule. Never blend the two.
- The floor tag goes before ## Concepts
- Use `[[wikilinks]]` for all concept references
- **Good days matter.** Most people only journal in detail when things are bad. Push for detail on good days too — these are the entries they'll want to read later.

### Add /journal routing to CLAUDE.md

After creating the journal skill, also add this block to the user's CLAUDE.md so `/journal` works as a slash command:

```markdown
# daily journal
- **daily-journal** (`~/.claude/skills/daily-journal/SKILL.md`) — daily journal interview. Trigger: `/journal`
When the user types `/journal`, invoke the Skill tool with `skill: "daily-journal"` before doing anything else.
```

Tell them: "I added /journal to your memory file. From now on, just type /journal and we'll start."

### Install the daily journal trigger

Now install the scheduled trigger that fires a journal conversation at `JOURNAL_TRIGGER_TIME` every day — but only if the user hasn't already journaled that day.

**Scheduling mechanism:**

Use whichever scheduling system is available in this Claude Code install. Try them in this order:

1. **`schedule` skill** (preferred — built-in Anthropic skill). Invoke the Skill tool with `skill: "schedule"` and ask it to create a new scheduled task with:
   - **Name:** `daily-journal-reminder`
   - **Schedule:** daily at `[JOURNAL_TRIGGER_TIME]` in timezone `[JOURNAL_TRIGGER_TZ]`
   - **Prompt:** the task body below

2. **`mcp__scheduled-tasks__create_scheduled_task`** (fallback — scheduled-tasks MCP). Call with equivalent parameters (`name`, `cron` or `schedule`, `prompt`).

3. **Cron fallback** (if neither is available): write a bash wrapper at `[VAULT_PATH]/⚙️ Meta/scripts/run-daily-journal.sh` that checks for today's entry and invokes `claude --print` headlessly with the task body as the prompt. Use the same pattern as `run-insights.sh` in Phase 18. Install it with a `crontab -e` line: `30 19 * * * /bin/bash "/path/to/vault/⚙️ Meta/scripts/run-daily-journal.sh"` (adjusting the `30 19` to match the user's chosen time in their local timezone, and converting to UTC if the system uses UTC cron). Note: cron can only run headless — it will create the entry directly instead of having a back-and-forth conversation. Tell the user this tradeoff explicitly if you have to fall back to cron.

**Task body (the prompt the scheduler runs at the chosen time):**

```
Daily journal check-in for [TODAY'S DATE in YYYY-MM-DD].

STEP 1 — Did they already journal today?

Check if the user already has a journal entry for today before doing anything else:

1. First try the index at `[VAULT_PATH]/⚙️ Meta/journal-index.json`. If it exists, read it and look for any entry where `date == today` (YYYY-MM-DD). If found, EXIT SILENTLY — do not prompt the user, do not send any message, just end the task. They already journaled today. Do NOT create a duplicate.

2. If the index doesn't exist (user skipped Phase 18), fall back to scanning the Journals folder directly. Use Grep on `[VAULT_PATH]/📓 Journals/*.md` for the frontmatter line `creationDate: [TODAY]` (match on the YYYY-MM-DD prefix only, ignore time). If ANY file matches, EXIT SILENTLY.

3. Only if BOTH checks find nothing, continue to Step 2.

STEP 2 — Start the journal conversation.

Invoke the Skill tool with `skill: "daily-journal"` and begin the interview as normal. The skill will greet the user, ask the opening question, and save the entry.

The user is expecting this prompt at [HH:MM their local time], so lead with a warm opening from the daily-journal skill — don't start with meta-talk like "the scheduled task ran."

STEP 3 — If the user doesn't respond within a reasonable window.

If they don't engage within 30 minutes, drop it silently. Do not re-prompt. Tomorrow's trigger will try again.
```

Substitute `[VAULT_PATH]` and the time placeholders with the actual values saved from Phase 1 (vault path) and Phase 10 (`JOURNAL_TRIGGER_TIME` and `JOURNAL_TRIGGER_TZ`).

**After installing the trigger, verify it was registered.** Call `mcp__scheduled-tasks__list_scheduled_tasks` (or the `schedule` skill's list action) and confirm `daily-journal-reminder` appears with the expected schedule. If it doesn't, TELL THE USER IMMEDIATELY — don't let them walk away thinking the trigger is set when it isn't.

**Tell the user:**
> "Daily journal trigger is installed. Every day at [TIME] I'll check if you've already journaled — if you haven't, I'll start a conversation. If you have, I stay out of your way. You can still run `/journal` manually anytime, or change the time by saying 'change my journal trigger time.'"

**If the user wants to change the time later:** they can say "change my journal trigger time to [new time]" and you'll update the scheduled task in place using the same mechanism (schedule skill → `update_scheduled_task` on the `daily-journal-reminder` task, or edit the cron line). Don't make them re-run setup.

---

## → Next phase (the install is not finished)

You have just completed **Phase 10b**. Do not stop here, do not summarise the
install as complete, and do not wait to be asked to continue.

1. **Record progress.** Write `~/.claude/.ai-brain-starter-progress.json`:
   `{"last_completed_phase": "10b", "ts": "<ISO-8601 timestamp for now>", "version": 1}`
   This one line is what lets a resumed install pick up here instead of starting over.
2. **Run the next phase.** Read `phases/phase-11-external-tools.md` from this skill's own directory
   and execute **Phase 11**.

If you are running low on context, say so out loud and tell the user to open a
fresh session and ask to resume the ai-brain-starter install; the progress file
above is what makes that work. Ending here silently leaves them with a
half-built brain that looks finished.

<!-- phase-chain: next=phase-11-external-tools.md -->
