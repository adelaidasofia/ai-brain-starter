# Phases 12-17: Imports, Taxonomy, Backup, and Obsidian Rules

**Run every phase in this file. Ask each phase's question explicitly.** Do NOT skip a phase because the user didn't mention the topic earlier — most users have no idea these features exist until you ask. Banned framings: "Skip — you didn't mention books / wearables / a framework / a backup tool." Always ask. Then skip only if their explicit answer is no.

---

## Phase 12: Import Book Notes & Highlights

**MANDATORY ASK — do NOT skip because they didn't bring up books earlier.** Ask now:

"Do you read books and highlight? (Kindle, Apple Books, Readwise, physical books with margin notes?)"

If yes, explain: "Your book highlights are some of the most valuable notes you have — they're the ideas that resonated enough to mark. Let's get them in."

Walk through each source:
- **Kindle:** "Go to read.amazon.com → Notes & Highlights → export. Or if you use Readwise, it's even easier."
- **Readwise:** "Export as markdown — Readwise has an Obsidian plugin that syncs automatically. Install it from Community Plugins."
- **Apple Books:** "This one's harder. You can copy highlights manually, or use a tool like Bookfusion to export."
- **Physical books:** "Take photos of your margin notes. I can transcribe them."
- **PDF annotations:** "Drop the PDFs in the vault. I can extract highlighted text and annotations."

After import:
- Create a `Books/` folder if it doesn't exist
- One note per book with: title, author, key highlights, personal reflections
- Add wikilinks to concepts that match existing vault notes
- "Your reading and your thinking are now connected. When you write about a topic, your book highlights surface as context."

## Phase 13: Health Data Import (devices + labs)

**MANDATORY ASK — never assume.** Do NOT skip this phase because the user didn't mention health earlier. Most people who own a wearable or have had recent lab work don't think to bring it up during a vault setup. Ask BOTH halves explicitly.

**Universal capture write-through.** Anything the user reveals about their health while answering this phase — conditions, diagnoses, cycle context, recent medical events, medications, therapists, fitness habits — appends to `🏠 Home/About Me.md` under `## Health` per Phase 3c. Health context is high-value for every future session (panel reviews, journal entries, advisory work). Capture is lossless.

### 13a. Wearables

"Do you wear or use any of these? Apple Watch / Apple Health, Oura Ring, Fitbit, Garmin, Whoop, or any other health-tracking device or app?"

Wait for their answer. If they say yes (even to one), continue. If they say "no, none of these," move to 13b.

If yes: "We can import your health data and cross-reference it with your journal entries. Imagine asking 'what do my best weeks have in common?' and getting back: gym 4x, sleep before midnight, HRV above 40, no social media after 9pm. The habit tracking from your journal gives you the subjective data — this gives you the objective data."

Then walk through their specific source. If they have the `health-setup` skill installed (from the ai-brain-starter bundle), invoke it via the Skill tool to handle wearable wiring end-to-end. Otherwise:

- **Apple Watch / Apple Health:** Export via Apple Health app → Share → Export All Health Data. Creates a zip with XML. The `ingest-health` skill (also bundled) imports it into a local DuckDB. Three modes: XML export.zip, Simple Health Export CSV folder, Health Auto Export TCP-live.
- **Oura Ring:** Personal Access Token from cloud.ouraring.com → set `OURA_TOKEN` env var. The `health-setup` skill wires this end-to-end.
- **Fitbit / Garmin / Whoop:** each has its own auth dance. `health-setup` skill handles each one.
- **Apple Watch without export hassle:** Health Auto Export iOS app pushes live data via TCP. Walk them through it.

After import, the `health-context` skill (auto-fires on journal entries) pairs body data with Floor tags — so a "Floor: Joy" entry with "HRV 22, RHR 78, sleep 5h 12m" gets the parasympathetic-state caveat automatically.

### 13b. Lab tests + health reports

ASK regardless of the wearables answer — devices and labs are different surfaces, and someone with no wearable may still have rich annual bloodwork. Phrasing matters: don't say "medical records" (intimidating); say "lab tests or any health reports."

"Have you had any recent lab tests, bloodwork, hormone panels, or other health reports in the last year or two? PDFs, images of paper results, anything? Even one set of bloodwork is enough to start."

If they say yes: "Save the PDFs or photos in your vault under `🏠 Home/Health/Labs/` (create the folder if it doesn't exist). When you mention symptoms, energy, mood, or anything body-related in journals, I'll cross-reference. For PDF lab panels, the `parse-mcp interpret` tool extracts the structured values and writes a markdown summary next to the original. You can also just drop them in and ask me 'what's in here?' when we're back in a session."

Walk through one if they have it handy right now: "Want to drop one in now? I'll show you the flow once so you know how it works."

If they say no: "Cool. If you do bloodwork later — annual physical, perimenopause panel, fertility screen, whatever — drop the PDFs in `🏠 Home/Health/Labs/` and I'll know what to do."

If they explicitly said no to BOTH wearables and labs, skip the rest of Phase 13 silently — Phase 10's habit tracking covers the subjective side. Otherwise, the body track in journals is now wired.

## Phase 14: Build Your Concept Taxonomy

**MANDATORY ASK** — concept notes are how the vault stops being a filing cabinet. Always run this phase.

Ask: "Do you have a framework you think about life through? (Values, principles, categories, a personal philosophy?) Or do you want to build one from the themes already in your writing?"

Not everyone has a framework like the High-Rise. But everyone has recurring themes. Help them identify theirs:

"Let me scan what you've already written — journals, notes, whatever's in the vault — and pull out the themes that keep coming up."

Scan for recurring concepts across their notes. Report the top 15-20 themes.

Then ask: "These are the ideas your brain keeps returning to. Want me to create a concept note for each one? Each note becomes a hub — everything you've ever written about that topic links through it."

For each concept note:
```markdown
---
creationDate: [today]
type: concept
---

[Brief description of what this concept means to them]

## Connected
[[Related Concept 1]] | [[Related Concept 2]] | [[Related Concept 3]]

## All entries mentioning this concept
[Dataview query pulling all files that link to this note]
```

This is what turns a vault from a filing system into a thinking system. The concepts are the nodes. The links are the edges. The graph becomes navigable.

## Phase 15: Backup confirmation (verify before declaring setup done)

This is the final backup checkpoint. The off-machine backup was established in **Phase 1, step 8.6** (right after the vault path was confirmed local). Here you confirm it actually took — and if step 8.6 was deferred or skipped, this is the last chance to set it up before you call setup complete.

**Do NOT wave backup off.** The old version of this phase said "whatever you normally use already covers it — no special setup needed." That assumption is exactly what fails people: a real user had ~1,100 notes, no Time Machine, no cloud copy, no remote, one disk — one failure from losing everything, because everyone assumed their "normal habits" had it covered. A markdown folder on a single disk with no off-machine copy is one hardware fault from total loss. Confirm, don't assume.

Run the detector (single source of truth) against their vault path:

```bash
python3 ~/.claude/skills/ai-brain-starter/scripts/check-vault-backup.py "<VAULT_PATH>"
```

- **`OK ...`** (vault-backup archive present, or Time Machine / a cloud copy / a pushed git remote already exists) → confirm it warmly in their PRIMARY_LANGUAGE, then nudge the one-time restore check if they haven't done it:
  - *EN:* "Your brain is backed up off-machine (via <source>) — good. Run `bash ~/.claude/skills/ai-brain-starter/scripts/vault-backup.sh verify` once to prove the restore actually works, and you're set."
- **`FAIL ... NO off-machine backup`** → do NOT move on. Set it up now (this is the same one command from step 8.6):

```bash
bash ~/.claude/skills/ai-brain-starter/scripts/vault-backup.sh setup --vault "<VAULT_PATH>"
```

  It asks for a destination they already have (an external drive, or a Google Drive / Dropbox / OneDrive folder), writes one compressed daily snapshot (no sync-storm), and schedules it. Add `--encrypt` for a vault holding journals / health / client notes. Then have them `verify` the restore.

  If they still choose to defer, say it plainly (do not soften it away): *"Okay — just know your brain has no off-machine backup right now, so one disk failure would lose it. You'll see a reminder at the start of every session until a backup exists. The one command to fix it is `bash ~/.claude/skills/ai-brain-starter/scripts/vault-backup.sh setup`."* The SessionStart signal then keeps it visible until it's done.

Full guide: `docs/BACKUP.md`. Never push the live vault *into* iCloud / Drive / Dropbox as the "backup" — that is the sync-storm failure mode (`docs/CLOUD_SYNC.md`); the backup is a single daily archive *sent to* such a folder, not the churning vault living in one.

Team-vault sharing is handled separately in Phase 20.

## Phase 16: Add Obsidian Power Rules to CLAUDE.md

"Last thing — let me add some rules to your memory file that make every future session smarter."

Add these to their CLAUDE.md under a new section:


**Obsidian Rules template:** Read the full template from `templates/generated/obsidian-rules-template.md` and append it to the user's CLAUDE.md.


Create the Content Drafts, Decision Log, and Vault Changelog files if they don't exist.

### Build the Wikilink Reference

After all rules are added, build a Wikilink Reference file that lists every linkable note in the vault. This helps Claude (and the user) know what can be wikilinked when writing new content.

Create `[VAULT_PATH]/⚙️ Meta/Wikilink Reference.md`:

```markdown
---
creationDate: [today]
type: meta
---
# Wikilink Reference

*All linkable notes and their aliases. Check this before adding wikilinks to new content. Update when new concept notes are created.*

Total: [count] notes

## By Folder
[For each folder, list all .md files with their aliases from frontmatter]
```

To build it, scan every .md file in the vault, extract the filename and any `aliases:` from YAML frontmatter, and list them organized by folder. This becomes the reference Claude checks before wikilinking new content — ensuring links go to real notes, not broken references.

## Phase 17: Connect External Tools Check

After all the installs and imports, quickly verify: "Let's make sure everything is connected. What can you see?"
- Test email: "Search your email for [recent term]"
- Test calendar: "What's on your calendar this week?"
- Test journal: "Let's do a quick /journal test"
- Test vault search: "Ask me something about your notes"

---

## → Next phase (the install is not finished)

You have just completed **Phase 17**. Do not stop here, do not summarise the
install as complete, and do not wait to be asked to continue.

1. **Record progress.** Write `~/.claude/.ai-brain-starter-progress.json`:
   `{"last_completed_phase": "17", "ts": "<ISO-8601 timestamp for now>", "version": 1}`
   This one line is what lets a resumed install pick up here instead of starting over.
2. **Run the next phase.** Read `phases/phase-18-insights.md` from this skill's own directory
   and execute **Phase 18**.

If you are running low on context, say so out loud and tell the user to open a
fresh session and ask to resume the ai-brain-starter install; the progress file
above is what makes that work. Ending here silently leaves them with a
half-built brain that looks finished.

<!-- phase-chain: next=phase-18-insights.md -->
