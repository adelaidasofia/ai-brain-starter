## Phase 11: Connect External Tools

**MANDATORY ASK — never skip, regardless of what the user mentioned earlier.** If the user listed Gmail / Google Calendar / Outlook / Slack / any other tool back in Phase 4 question 3, that's INFORMATION; this phase is where Claude actually wires the MCP. The known failure mode: model captures "Gmail" in CLAUDE.md `## Tools I Use` and then SKIPS this phase thinking the question is already answered. Phase 11 must fire even when prior phases already surfaced the tool name. The question changes from "do you use it?" to "you mentioned Gmail earlier — installing google-workspace-mcp now."

"Let's connect Claude to the tools you actually use. This is where the vault becomes an operating system, not just a notebook."

**Universal capture write-through.** Anything the user reveals while answering this phase's questions — preferred email account, calendar timezone, work tools that reveal industry/role, contacts at companies they care about, geographic context (work hours, regional accounts) — appends to `🏠 Home/About Me.md` in the right section (`## Work`, `## Identity`, `## Habits, Routines, Rhythm`) per Phase 3c. Don't pause to confirm — write the bullet and continue.

### Email & Calendar

**First check what they said in Phase 4 question 3 (the tools answer).** If they named Gmail, Google Calendar, Google Drive, Google Docs, Google Sheets, or Google Meet anywhere in that answer, DO NOT re-ask. Skip directly to install:

> "You mentioned [Gmail / Google Calendar / etc.] back in the tools section, so I'm installing `google-workspace-mcp` now. One MCP that covers Gmail, Calendar, Drive, Docs, Sheets, and Meet. Supports multiple accounts (work + personal) and is token-efficient. I'll walk you through the Google Cloud OAuth setup."

**Otherwise ask:** "Do you use Gmail / Google Calendar / Google Drive / Google Docs? Or Outlook / Microsoft 365?"

**If they use ANY Google Workspace surface (Gmail, Calendar, Drive, Docs, Sheets, Meet) — install `google-workspace-mcp` as the default.** Do NOT recommend Settings → Connectors first. The MCP covers all 5 surfaces in one install, supports multiple accounts, and is token-efficient.

Tell them: "I'm going to install `google-workspace-mcp` — one MCP that covers Gmail, Calendar, Drive, Docs, and Sheets. It supports multiple Google accounts (work + personal) and uses fewer tokens than the official connectors. I'll walk you through the Google Cloud OAuth setup."

Then install it directly (don't ask "want to install it?" — they already said they use Google):

1. Clone: `git clone https://github.com/adelaidasofia/google-workspace-mcp ~/.claude/google-workspace-mcp`
2. Install deps: `cd ~/.claude/google-workspace-mcp && pip install -r requirements.txt`
3. Walk them through `SETUP.md` for the OAuth consent screen. This is the one slow part — GCP console UI, creating an OAuth client, downloading `client_secret.json`, dropping it in the repo folder. Stay with them through every screen; this is where non-tech users give up. Use the Visual Reassurance Protocol throughout.
4. Register in `~/.claude.json` mcpServers block:
   ```json
   "google-workspace": {
     "command": "python3",
     "args": ["/Users/<user>/.claude/google-workspace-mcp/server.py"]
   }
   ```
5. Authorize each account: `python3 -c "from accounts import add_account; add_account()"` — browser opens, they grant consent, refresh token lands in macOS Keychain. Ask them upfront which accounts (work + personal + any others) and run this once per account.
6. Verify: restart Claude Code, check Settings → MCP, confirm `google-workspace` is listed with 61 tools across Gmail/Calendar/Drive/Docs/Sheets.

**Non-mac users:** the MCP stores tokens in macOS Keychain on Mac. On Windows/Linux, tokens fall back to an encrypted file (the `keyring` library picks the right backend). Flag this if they're not on macOS so they know tokens aren't in Keychain-level security.

**If they use Outlook / Microsoft 365 instead:** "Go to Settings → Connectors and connect Microsoft 365. Once connected, I can search your email, draft replies with full context, check your schedule, and create events."

**If they use both:** install `google-workspace-mcp` for Google and add the Microsoft 365 connector for Outlook.

### Communication
Ask: "Do you use Slack?"
- "Connect Slack from Settings → Connectors. I'll be able to search messages, read channels, and draft messages with your vault context."

### CRM & Sales
Ask: "Do you use HubSpot, Apollo, or any CRM tool?"
- "Connect it. Then your Obsidian CRM and your actual sales CRM stay in sync. I can look up contacts, check deal status, and draft outreach from your vault."

### Meeting Notes — adaptive setup based on the user's tool

The whole "I just had a meeting" workflow rule needs to know where the transcript lives. **Ask the user which tool they use, then generate the right rule + sync wiring.** Don't assume Granola — most teams don't use it.

Ask:

> "Do you record / transcribe your meetings? Which tool? Pick the closest:
>
>   1. **Granola** — Mac app, AI-generated summaries, has an API/MCP
>   2. **Google Meet + Gemini** — verbatim transcripts auto-generated as Google Docs in your Drive
>   3. **Otter.ai** — transcripts in their web app, can export to Drive/Slack
>   4. **Fireflies.ai** — same idea, transcripts in their app
>   5. **Zoom recordings + Zoom AI Companion** — transcripts saved to Zoom cloud
>   6. **Microsoft Teams + Copilot** — transcripts in OneDrive
>   7. **Notion AI Notetaker** — transcripts saved as Notion pages
>   8. **Manual notes** — I take my own notes during/after the meeting, no AI
>   9. **Multiple tools** — different tools for different meetings
>   10. **None / I don't record meetings** — skip this whole thing
>
> Tell me the number(s) and any specifics (which Drive folder, which channel, etc.)."

Store the answer as `MEETING_TOOLS` (a list — could be multiple). Then for each tool, wire it up specifically:

#### 1. Granola

- **Heads-up: Granola's API needs a paid Granola plan.** Have them check Granola → Settings → Connectors for an **API keys** section. If it is not there, their plan lacks API access — route them to Google Meet + Gemini, Otter, or manual notes (the options below) instead of this one.
- **No MCP needed.** `scripts/granola_sync.py` pulls full transcripts from Granola's official Public API and writes them to the meeting notes folder.
- **Tell them:** "Granola syncs via its Public API. Generate a key in Granola (Settings > Connectors > API keys), save it to `~/.config/granola/api-key`, then run `python3 scripts/granola_sync.py --health` to verify it and `--dry-run` to preview. For auto-export every 2 hours, install the LaunchAgent in `scripts/com.granola-export.plist` (set the script + log paths, then `launchctl load` it)."
- **Discovery rule for the meeting workflow CLAUDE.md section:**
  > Glob the meeting-notes folder for files matching `*- Transcript.md` modified in the last 24 hours. The LaunchAgent re-syncs every 2 hours; if a just-ended meeting is missing, run `python3 scripts/granola_sync.py` manually (Granola can take a few minutes to finalize a transcript). Read the file fully — it's the source of truth.

#### 2. Google Meet + Gemini

- **No MCP install needed** — Gemini transcripts live as Google Docs in Drive, accessed via the Google Drive MCP if they have it (Phase 11 Email/Calendar section covers Google Drive).
- **Tell them:** "Gemini auto-creates a Google Doc transcript named like 'Meeting with [Name] - YYYY/MM/DD - Transcript' and saves it to a folder called 'Meet Recordings' in your Drive. Make sure that folder is shared/accessible from Claude. If you have the Google Drive MCP installed, you're set."
- **Ask:** "What's the name of the Drive folder where Gemini saves transcripts? (Default is 'Meet Recordings')"
- **Store** as `MEETING_DRIVE_FOLDER`.
- **Discovery rule for the meeting workflow CLAUDE.md section:**
  > Use the Google Drive MCP (`mcp__google_drive__search` or equivalent) to search for transcripts by meeting title + attendee name + today's date. The folder is **`<MEETING_DRIVE_FOLDER>`**. Gemini transcripts are verbatim and timestamped — they're the source of truth. Read the full doc before answering, never skim.

#### 3. Otter.ai

- **Ask:** "Do you have Otter set to auto-export transcripts somewhere? (Slack, Google Drive, Dropbox, email?)"
- **Store** the destination as `OTTER_EXPORT_PATH`.
- **Tell them:** "Otter doesn't have an MCP yet, so we'll rely on its export. Set up auto-export in Otter Settings → Integrations to drop transcripts into a folder I can read."
- **Discovery rule:**
  > Check **`<OTTER_EXPORT_PATH>`** for the most recent file matching today's date. Read the full transcript (Otter exports the verbatim text) before answering.

#### 4. Fireflies.ai

- Same pattern as Otter — ask for the export destination and store as `FIREFLIES_EXPORT_PATH`.
- **Discovery rule:** check `FIREFLIES_EXPORT_PATH` for the most recent file matching today's date.

#### 5. Zoom recordings + Zoom AI Companion

- **Ask:** "Are you using Zoom Cloud Recording (transcripts in Zoom's web portal) or Local Recording (transcripts on disk)?"
- **If cloud:** "Set up Zoom auto-export to a Drive/Dropbox folder. Then I can read them like Otter/Fireflies. Without auto-export, I have to ask you to download each one manually."
- **If local:** "What folder does Zoom save recordings to?" → store as `ZOOM_RECORDING_PATH`.
- **Discovery rule:** check `ZOOM_RECORDING_PATH` for `*.vtt` or `*.txt` files matching today's date.

#### 6. Microsoft Teams + Copilot

- **Ask:** "What OneDrive folder do Teams transcripts land in? (Usually `<your>/Recordings/`)"
- **Store** as `TEAMS_RECORDING_PATH`. Same discovery pattern as Zoom.

#### 7. Notion AI Notetaker

- **Tell them:** "Notion's AI Notetaker writes transcripts as Notion pages. We need the Notion MCP to access them. Set up the Notion MCP in Phase 11 (Project Management section) — or skip and use Notion's API directly later."
- **Discovery rule:** use the Notion MCP to query the meetings database.

#### 8. Manual notes

- **No tool wiring** — the meeting-todos skill still works on hand-typed notes, just trigger it manually with `/meeting-todos` after you save the note.
- **Discovery rule:** the user will name the meeting note file in chat — read that file directly.

#### 9. Multiple tools

- Walk through each one separately. The CLAUDE.md meeting workflow rule will list ALL discovery sources and tell Claude to **search them all in parallel** before reading anything. When multiple sources exist for the same meeting (e.g. Granola summary + verbatim Gemini transcript), the rule should prefer the verbatim source — see the "Source hierarchy" step below.

#### 10. None

- Skip the meeting workflow rule entirely. Just install the meeting-todos skill (in case they later start typing meeting notes manually) and move on.

---

**After collecting the answer**, generate the meeting workflow rule **adapted to their tools** and write it to `<vault>/⚙️ Meta/rules/meeting-workflow.md`, **overwriting** the generic Granola-default template that Phase 4 copied there. The customized rule file is the canonical source of truth — the `inject-meeting-workflow-on-trigger.py` hook reads it when the user says "I just had a meeting" and injects it as `additionalContext`. Phase 4 placed a generic copy so the cascade isn't empty for users who skip Phase 11; once Phase 11 runs, that generic copy must be replaced with the tool-specific version.

A short pointer in CLAUDE.md is added separately (see the routing block below) — do NOT duplicate the rule body into CLAUDE.md, that would force the model to reconcile two near-identical bodies and drift over time as one or the other is edited.

The template to write to `<vault>/⚙️ Meta/rules/meeting-workflow.md` (substitute the variables based on their answers):

```markdown
# Meeting workflow — "I just had a meeting" trigger

When the user says any variation of "I just had a meeting", "pull meeting notes", "pull the transcript", "[name] meeting is done", or similar, run the full meeting workflow automatically. Do NOT ask for clarification.

## Step 1 — Discovery: find ALL sources before reading anything

This user uses: **<list of MEETING_TOOLS from the answer>**.

Search each source IN PARALLEL before reading anything:

<for each tool the user picked, insert the discovery rule from above>

Surface every candidate file you find. Do not pick one and ignore the others.

## Step 2 — Source hierarchy

When multiple sources exist for the same meeting (e.g. Granola + Gemini for a Google Meet call), prefer the source with the **most verbatim** transcript:

  1. **Verbatim timestamped transcripts** (Gemini Docs, Otter, Fireflies, Zoom AI, Teams Copilot) → source of truth, read 100%
  2. **Post-processed AI summaries** (Granola summaries, Notion AI Notetaker condensed view) → useful as a backup, but NEVER skim. If a verbatim source exists, read THAT and skip the summary to save tokens.
  3. **Hand-typed notes** → read fully

If the source doc is too large for your context window, dispatch a subagent with explicit "read 100% in chunks" instructions. Never skim.

## Step 3 — Full cascade (run all of it, in order)

After reading the source(s) fully:

1. Enrich the meeting note in the vault: TL;DR → decisions table → action items → verbatim quotes → meta-observations
2. Cascade decisions to canonical strategy/pitch docs (with a rule-consistency scan to catch contradictions)
3. Append high-stakes decisions to `Decision Log.md` (what / why / floor / stakes / speed; outcome and pattern blank for later)
4. Update the CRM contact file for every attendee (read 2 adjacent CRM files first to confirm the pattern; preserve dataview blocks)
5. Update to-dos: business items → team to-do; personal items → personal to-do. Never duplicate. Default to personal when ambiguous.
6. Run `/humanizer` on any external-facing prose written
7. Verify with backlinks — open the CRM file and confirm the meeting note shows up; open the personal to-do and confirm the team embed renders
8. Report every file changed. Flag what the user should eyeball. State which sources were read with byte counts as evidence of completeness.
```

Save the variables (MEETING_TOOLS, MEETING_DRIVE_FOLDER, etc.) so future maintenance/upgrade runs can re-generate the rule if the user switches tools.

---

Then offer the meeting-todos skill (always, regardless of which tool they picked):

"After any meeting note is saved to your Meeting Notes folder, I'll automatically pull out your action items (separate from others') and show you a preview before adding anything to your to-do. You don't have to type anything — it fires the moment the note lands in the vault. You can also trigger it manually with `/meeting-todos` anytime."

The skill itself was already installed in Phase 0 at `~/.claude/skills/meeting-todos/`. If it's missing, re-run the bootstrap (`bash ~/.claude/skills/ai-brain-starter/bootstrap.sh` or `.ps1` on Windows).

Add routing to the user's CLAUDE.md:

```markdown
# meeting todos
- **meeting-todos** (`~/.claude/skills/meeting-todos/SKILL.md`) — extract action items from a meeting note and add them to to-do. Trigger: `/meeting-todos`
When the user types `/meeting-todos`, invoke the Skill tool with `skill: "meeting-todos"` before doing anything else.
```

### Design & Creative
Ask: "Do you use Canva, Figma, or any design tools?"
- "Connect Canva or Figma from connectors. I can search your designs, generate new ones from vault context, and pull brand assets."

### Project Management
Ask: "Do you use Linear, Notion, Asana, or any project tracker?"
- "If it's in the connectors list, connect it. If not, we can set up periodic imports."

Tell them: "You don't have to connect everything now. Start with email and calendar — those give the biggest boost. You can add more anytime."

---

## → Next phase (the install is not finished)

You have just completed **Phase 11**. Do not stop here, do not summarise the
install as complete, and do not wait to be asked to continue.

1. **Record progress.** Write `~/.claude/.ai-brain-starter-progress.json`:
   `{"last_completed_phase": "11", "ts": "<ISO-8601 timestamp for now>", "version": 1}`
   This one line is what lets a resumed install pick up here instead of starting over.
2. **Run the next phase.** Read `phases/phase-12-17-imports-rules.md` from this skill's own directory
   and execute **Phase 12-17**.

If you are running low on context, say so out loud and tell the user to open a
fresh session and ask to resume the ai-brain-starter install; the progress file
above is what makes that work. Ending here silently leaves them with a
half-built brain that looks finished.

<!-- phase-chain: next=phase-12-17-imports-rules.md -->
