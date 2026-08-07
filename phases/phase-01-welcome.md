## Phase 1: Language & Welcome

### Step 1.−2 — Read the install recap if present (run FIRST, before any user-facing question)

The bootstrap caches the user's metadata at `~/.claude/.ai-brain-starter-recap.json` only when the user installed through the web form (most installs do not — they go straight to the interview). If the file is present, it looks like:

```json
{
  "ok": true,
  "name": "...",
  "email": "...",
  "lang": "en" | "es",
  "branch": "cohort" | "tier" | "comp" | "open",
  "cohortCode": "..." | null,
  "tier": "..." | null,
  "os": "mac-arm" | "mac-intel" | "windows" | "linux"
}
```

If the file exists and parses cleanly, capture these into your working memory:

- `RECAP_NAME` from `name`
- `RECAP_EMAIL` from `email`
- `RECAP_LANG` from `lang` — **a HINT, never authoritative**. Do NOT pre-set `PRIMARY_LANGUAGE` from this. Step 1.0 still runs and the user still picks.
- `RECAP_BRANCH` (informational, used to tune Phase 11 wiring)
- `RECAP_OS` (informational, used to skip the OS-detection step in Phase 5)

`name` + `email` are facts the user typed themselves on the install page. Reuse them. `lang` is a server-side default from a one-field form (the install page defaults to `"en"` if no language is selected — an early signup hit this default and the recap.lang was `"en"` even though no one ever asked the user). Treat it as background context only.

If the file is missing or malformed, fall back to the standard interview flow below (ask everything from scratch).

**No personalized welcome before Step 1.0 runs.** The recap'd name lands in the Step 1.1 welcome AFTER the user has picked a language. Pre-emptive welcomes in English or Spanish would lock the conversation into a language the user never confirmed. Wait.

After reading the recap, continue with Step 1.−1 (mode detection) below. **Step 1.0 always runs. No exceptions.**

### Step 1.−1 — Detect mode: NEW PERSONAL VAULT vs JOINING EXISTING TEAM VAULT (run BEFORE the language question)

Before anything else, figure out what the user is trying to do. There are three modes:

**A. New personal vault** — fresh start, no existing vault. Walk through every phase.
**B. Joining an existing team vault** — someone else already set up a team vault and they're a new member joining. Skip structure-creation entirely; just verify Phase 0 is installed, fix any cwd-mismatch (see below), wire their meeting tool, and confirm. Should take <5 minutes.
**C. Upgrading their own existing vault** — they already ran setup once and want to add features or sync the latest CLAUDE.md template. Use the "Already Set Up?" branch at the top of this file.

**Auto-detection logic:**

```bash
# Look for an existing CLAUDE.md in the cwd, parent directories (up to 4 levels),
# and one level deep (subdirectories). The walk-down case catches the cwd-mismatch
# bug: a team member launches Claude from the wrapper folder (e.g. a Google Drive
# shared folder root) but the real CLAUDE.md is one level deeper, in the actual
# vault content subfolder.
# CLAUDE.md inside a subfolder of the launch directory.
CWD="$(pwd)"
FOUND_CLAUDE=""
DETECTED_PARENT_PATH=""

# Walk up
DIR="$CWD"
for _ in 1 2 3 4; do
  if [[ -f "$DIR/CLAUDE.md" ]]; then FOUND_CLAUDE="$DIR/CLAUDE.md"; break; fi
  PARENT="$(dirname "$DIR")"; [[ "$PARENT" == "$DIR" ]] && break; DIR="$PARENT"
done

# Walk one level down (catch cwd-mismatch)
if [[ -z "$FOUND_CLAUDE" ]]; then
  for sub in "$CWD"/*/; do
    if [[ -f "$sub/CLAUDE.md" ]]; then
      FOUND_CLAUDE="$sub/CLAUDE.md"
      DETECTED_PARENT_PATH="$sub"
      break
    fi
  done
fi
```

**Decision tree:**

- **Argument hint:** if the user invoked `/setup-brain join-team`, jump straight to mode B and skip the question below.
- **No CLAUDE.md found anywhere:** → mode A (new personal vault). Continue to Step 1.0.
- **CLAUDE.md found in cwd:** → mode C (upgrade their own vault). Use the "Already Set Up?" branch at the top of this file.
- **CLAUDE.md found in a parent directory** (and they're working in a subfolder of an existing vault): → ask "It looks like you're inside an existing vault at `<parent>`. Are you (1) joining this as a team member, (2) just adding work in a subfolder, or (3) starting fresh somewhere else?" → answer 1 routes to mode B; answer 3 routes them to a fresh directory and mode A.
- **CLAUDE.md found in a SUBFOLDER of the cwd** (the cwd-mismatch case): → this is a team-vault folder structure where the actual content lives one level deep — common with Google Drive / OneDrive / Dropbox shared folders that wrap a single content subfolder. Auto-fix it (next section). Then proceed to mode B.

### Step 1.−1a — Cwd-mismatch auto-fix (joining a team vault where the content is one level deep)

If `CLAUDE.md` was found in a subfolder of the cwd (not in the cwd itself), the user is in a team vault where the actual content folder is one level deep. This pattern is common for:

- Google Drive shared folders that contain a single subfolder of vault content
- OneDrive / Dropbox shared workspaces
- Manually-organized team folders where a top-level wrapper folder contains the actual vault

**The bug:** Claude Code only auto-loads CLAUDE.md from the cwd and walks UP, not DOWN. If the user launches Claude from the wrapper folder, the team CLAUDE.md is never loaded. Their session has no project context, the meeting workflow rule doesn't fire, the graph never gets read, and every answer is generic. The user can go DAYS without realizing.

**The auto-fix:** write a thin pointer CLAUDE.md at the cwd (the wrapper folder) that says "the real CLAUDE.md is in the subfolder — please load that file." Claude Code reads this pointer at session start, reads the pointed-at file, and the user gets full project context regardless of which folder they launched from.

```bash
# Only run if a subfolder CLAUDE.md was found AND the cwd doesn't already have one
if [[ -n "$DETECTED_PARENT_PATH" && ! -f "$CWD/CLAUDE.md" ]]; then
  REL_PATH="${DETECTED_PARENT_PATH%/}"
  REL_PATH="${REL_PATH##*/}"
  cat > "$CWD/CLAUDE.md" <<EOF
# Pointer to the real team vault CLAUDE.md

The actual project CLAUDE.md lives at \`$REL_PATH/CLAUDE.md\`. **Read that file at session start.** All project rules, the team context, the Knowledge Graph rule, and the meeting workflow rule live there.

This pointer file exists because Claude Code only auto-loads CLAUDE.md from the current working directory (and walks UP through parent directories). It does not walk DOWN into subfolders. The team vault's actual content lives in the \`$REL_PATH/\` subfolder, so without this pointer, every Claude session launched from this directory would miss the real CLAUDE.md and operate with no project context.

**Files of note (all inside \`$REL_PATH/\`):**
- \`CLAUDE.md\` — the real one
- \`⚙️ Meta/graphify-out/GRAPH_REPORT.md\` — read this first for any strategy question
- \`⚙️ Meta/graphify-out/graph.json\` — queryable knowledge graph

If you're a team member joining this vault, you can leave this pointer file in place — it's part of the team setup. Don't delete it.
EOF
  echo "✓ Wrote pointer CLAUDE.md at $CWD/CLAUDE.md → real one at $REL_PATH/CLAUDE.md"
fi
```

**Tell the user out loud:** "Heads up — I noticed your team vault has the real CLAUDE.md one folder deep (inside `<subfolder>`). Claude Code only loads CLAUDE.md from where you launch it, so I just wrote a tiny pointer file at the top level so every team member who runs Claude from this folder picks up the real one automatically. You won't have to think about it again."

### Step 1.−1b — Mode B: Joining an existing team vault (minimal setup)

If we routed to mode B (joining an existing team vault), do NOT run Phases 2, 3, 4, 5, 14, 15, 16, 19. The vault already exists; you'd just be duplicating work. Run only:

1. **Phase 0** — silent install of the dependencies (graphify, humanizer, etc.). The user may already have some of these from earlier conversations; the install commands are idempotent.
2. **Step 1.−1a** — write the cwd pointer if needed (above).
3. **Phase 11 — Meeting tool selection** (the new adaptive section, see below). Ask which tool they use and wire it up.
4. **A short verification block** — confirm the team CLAUDE.md is loadable, graphify is callable, and the meeting MCP is registered if applicable.
5. **Hand off** — say "You're ready. The team vault is at `<path>`. Open it in Claude Code from here and the real CLAUDE.md will load automatically. Try asking a question that uses the graph (e.g. *'what does our graph say about pricing?'*) to confirm the context is loading."

Don't ask the language question (1.0) or any of the personal-setup questions (1.1) — those are for new personal vaults only. The team vault already has its own CLAUDE.md with its own conventions.

### Step 1.−1c — Scope is fixed: the personal brain always installs (modes A and C)

**Don't ask what the brain is "for," and don't volunteer a work-only or operational-only install.** For a new personal vault (mode A) or an upgrade of the user's own vault (mode C), there is no scope-selection step. The personal brain — daily journaling, the floor framework, the advisory panel, insights, life reflection — installs by default, every time. It is the gift, not an upsell.

This guard exists because the most natural-looking improvisation breaks it: the user describes a heavily operational life ("I run a company," "this vault is all work / clients / CRM"), or you're upgrading a vault that is currently 100% operational, and it feels helpful to ask *"What do you want this brain for? That decides which phases I run."* **Do not.** An all-business answer, or an all-operational existing vault, is not a signal to narrow scope — it is the exact person who is missing the personal half. Run every phase regardless of how work-focused they sound, and let the personal phases arrive as the normal design, never announced as optional or as something you considered skipping.

If a user *explicitly* asks for an operational-only / work-only setup, make the case once, warmly: *"Happy to focus it however you want — one thing first: the journaling and reflection side is the part most people end up valuing most, and it's zero extra effort to include since it sits quietly until used. Want me to include it, or keep this strictly operational?"* Then do exactly what they answer. If they still say work-only, install work-only, tell them the personal phases can be added any time with one sentence ("add the personal brain"), and move on — their explicit choice always wins over the default. The per-feature asks in later phases (do you own a wearable, do you read books, do you write publicly) are about where your *data* goes — they are NOT a scope gate, and only the user can skip the personal core, never you on their behalf.

### Step 1.0 — Languages (ASK FIRST, BEFORE ANYTHING ELSE — NO EXCEPTIONS)

**This step always runs.** Not "unless the recap has a lang field." Not "unless the user's first message was in English." Not "unless Phase -2 already set something." Always. The user has to pick.

**Open polyglot.** The very first message you send must show the user their language is one of the options — not buried, not after a paragraph of English. Open with this exact greeting (verbatim, including the line breaks and the emoji-free wave), translated into the most common languages the substrate expects, with the call-to-action at the end:

> 👋
>
> **What language do you want to do this in?**
> ¿En qué idioma quieres hacer esto?
> Em qual idioma você quer fazer isso?
> Dans quelle langue veux-tu faire ça?
> In welcher Sprache möchtest du das machen?
> 你想用哪种语言来做这个？
> どの言語でやりたいですか？
> בְּאֵיזוֹ שָׂפָה אַתָּה רוֹצֶה לַעֲשׂוֹת אֶת זֶה?
> أَيُّ لُغَةٍ تُرِيدُ أَنْ تَفْعَلَ هَذَا بِهَا؟
>
> *Just answer in the language you want me to use. I'll match you.*
> *Responde en el idioma que quieras usar. Te sigo.*

The user typing back "español" / "français" / "Português" / "中文" / a sentence in any language is the signal. Match whatever they wrote in. If they answer in a language not in the list above, that's fine too — match their language and run the whole rest of setup in it.

**After they answer, ask the follow-up in their chosen language:**

> "Got it. Do you also take notes / journal in any other languages? Some people slip into a second language for emotional content, or use one language for work and another for personal stuff — that's normal, tell me all of them. Or 'just this one' if it's only one language."

Then store:
- `PRIMARY_LANGUAGE` — the language they chose to set up in (whatever they answered in / wrote)
- `SECONDARY_LANGUAGES` — list (possibly empty) of every other language they mentioned. These drive the alias generation later.

**Why the recap.lang cannot drive this:** the install signup form defaults to `lang: "en"` when no field is provided (which is most of the time, because the form prefills it from URL params or browser locale). A user who never picked English ends up with `recap.lang: "en"` and a setup interview that silently runs in English. The first install where this surfaced recap-defaulted to English without the user ever being asked. Don't trust recap.lang as authoritative. Open polyglot. Let the user pick by responding.

Wait for their answer. Store it as:
- `PRIMARY_LANGUAGE` — the one language the whole bot runs in
- `SECONDARY_LANGUAGES` — list (possibly empty) of every other language they mentioned. These drive the alias generation later.

**CRITICAL: from this point forward, conduct EVERYTHING in their primary language.** This is not "translate the questions" — it's "be a native speaker of that language for the rest of the conversation." That includes:

- Every spoken/written prompt and explanation you give the user
- All folder names where idiomatic (e.g. Spanish: `📓 Diarios/`, `🏠 Casa/`, `📚 Libros/`, `📝 Notas/`, `👤 CRM/`, `⚙️ Meta/` — keep emojis, translate words; check with the user if they prefer English folder names for tooling reasons)
- The CLAUDE.md file content (rules, vault map, preferences — written in their language, not English)
- Journal interview questions
- Concept note descriptions, headings, and the floor framework labels
- Insight reports (`/weekly`, `/monthly`)
- Error messages and confirmations
- The names of canonical concept notes (e.g. Spanish primary → `Miedo.md` is the canonical file, `fear` is an alias; English primary with Spanish journaling → `Fear.md` is canonical, `miedo` is an alias)

If they pick a non-English primary language, do NOT default back to English mid-setup just because the SKILL.md is written in English. Translate every prompt as you go. If you don't know the idiomatic translation for a phrase, ask the user.

**Substack link override (Spanish only):** the SKILL.md links to the framework article at `https://adelaidadiazroa.substack.com/s/internal-design` (English) in several places. **Only swap it if the user picks Spanish** — in that case, replace every occurrence with `https://perspectivasblog.substack.com/s/el-rascacielos` (and use the Spanish title "El Rascacielos — el modelo del diseño interno"). For every other language (including English), leave the existing English URL as-is.

### Step 1.1 — Welcome (in their language)

Now translate the welcome into their primary language and continue:

"Hey! I'm going to help you set up an AI-powered second brain. By the end of this conversation, you'll have a personal knowledge vault that I can read, search, and build on every time we talk. No more re-explaining yourself.

If you want the full story behind this system — why it was built, what it does, and what surprised the creator most — check out: https://adelaidadiazroa.substack.com/p/how-i-built-a-second-brain-that-actually

First — a few questions so I know what we're working with:"

**Do NOT mention how long the setup will take.** No "2-3 hours," no "30 minutes for the basics," no "we can go as deep as you want." The full version is the only version (workshop/basic-vs-full bifurcation was killed in PR #62 — `feedback_install_skill_banned_patterns.md`). Time estimates are time-gates in disguise per the best-of-best lockout and create dropout points where users self-select out of the full install. Just open the interview.

Then ask these ONE AT A TIME. Wait for each answer before moving on:

1. "What's your name?"
2. "What do you do? (job, projects, passions — whatever matters to you)"
3. "Do you already have notes somewhere? (Apple Notes, Google Docs, Notion, Evernote, paper journals, voice memos, scattered files, or nothing yet?)"
4. "Have you ever journaled? (daily, occasionally, used to, never — doesn't matter either way, we're setting one up for you regardless)"
5. "**Do you write publicly?** (Blog, book, newsletter, Substack, Medium, LinkedIn posts — anything you write *for readers*, beyond private notes.) If not, that's totally fine — just say no, and I won't create a Writing folder for you."

**Store the answer as `WRITES_PUBLICLY` — true or false.** This gates whether a `✍️ Writing/` folder gets created in Phase 3, whether writing-related rules get added in Phase 4, and whether the humanizer rule fires in later sessions. Journaling does NOT count — journaling is for the user's own eyes and lives in `📓 Journals/`. Writing means content with an intended audience. If the answer is ambiguous ("kind of," "sometimes"), ask one follow-up: "Is anyone besides you reading it?" Only a clear yes creates a Writing folder.

6. **Obsidian check — DETECT FIRST, don't ask if it's already there.** The bootstrap auto-installs Obsidian, so by the time the user reaches /setup-brain it should already be present. Run the appropriate detection check FIRST and only fall through to asking if it's actually missing:

```bash
# Mac
[[ -d "/Applications/Obsidian.app" ]] && echo "OBSIDIAN_PRESENT" || echo "OBSIDIAN_MISSING"

# Linux
(command -v obsidian || [ -f "/var/lib/flatpak/exports/bin/md.obsidian.Obsidian" ] || [ -f "$HOME/.local/share/flatpak/exports/bin/md.obsidian.Obsidian" ] || [ -x "$HOME/.local/bin/obsidian" ]) && echo "OBSIDIAN_PRESENT" || echo "OBSIDIAN_MISSING"
```

```powershell
# Windows
$paths = @("$env:LOCALAPPDATA\Obsidian\Obsidian.exe", "$env:ProgramFiles\Obsidian\Obsidian.exe", "${env:ProgramFiles(x86)}\Obsidian\Obsidian.exe")
if ($paths | Where-Object { Test-Path -LiteralPath $_ }) { "OBSIDIAN_PRESENT" } else { "OBSIDIAN_MISSING" }
```

**If `OBSIDIAN_PRESENT`:** Skip the question entirely. Go straight to step 7 with: *"Obsidian is already installed (the bootstrap took care of that for you). Let's create your vault now."*

**If `OBSIDIAN_MISSING`** (the bootstrap was skipped or failed): re-run the bootstrap, which handles Obsidian install on Mac/Linux/Windows with all the platform fallbacks (brew, snap, flatpak, AppImage, winget):

```bash
# Mac/Linux
bash ~/.claude/skills/ai-brain-starter/bootstrap.sh
```

```powershell
# Windows
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\.claude\skills\ai-brain-starter\bootstrap.ps1"
```

Then say: *"I just installed Obsidian for you. Let's create your vault now."*

**Never** ask the user to "go download Obsidian" — that breaks the one-command promise and assumes they know what Obsidian is and how to install a desktop app.

7. "Now open Obsidian and choose 'Create new vault.' Name it whatever feels right — your name, 'Brain,' 'Notes,' whatever. The simplest spot is your home folder (like `~/Brain` or `~/Notes`) — **not** Desktop or Documents, because those are cloud-synced (iCloud on Mac, OneDrive on Windows) and a *raw* vault there melts the sync daemon. **If you want these notes on your iPhone**, that's supported too — pick Documents/Desktop and tell me, and I'll set up the safe sync mode that keeps the notes in iCloud while moving the machinery out (see `docs/CLOUD_SYNC.md`). Let me know when it's created."

**Wait for confirmation before continuing.**

8. "Perfect. Now I need you to tell me the path to your vault. In Obsidian, go to Settings (gear icon) → About → look for 'Vault path.' Paste it here."

Save the vault path — you'll use it for all file operations.

8.5. **GUARD — before saving the path, check whether it is inside a cloud-sync folder.** A *raw* git-backed vault inside iCloud / OneDrive / Dropbox / Google Drive / Box melts the OS sync daemon (pegged CPU, frozen machine). Run:

```bash
python3 ~/.claude/skills/ai-brain-starter/scripts/check-cloud-sync.py --porcelain "<VAULT_PATH>"
```

- Output starts with `OK_LOCAL` → good, continue.
- Output starts with `CLOUD_SYNC_RISK:` → the path is inside `<service>`. **Do NOT make them choose between options or explain the internals, and do NOT ask "do you want these notes on your iPhone?"** — a non-technical user won't know what to answer. Apply the one safe default yourself: keep their notes exactly where they are (still synced, still on their phone) and move only the churning machinery (git + caches) out to a local sidecar. With all other Claude sessions closed (separating the git dir orphans live worktrees — the script refuses otherwise), run:

    ```bash
    bash ~/.claude/skills/ai-brain-starter/scripts/relocate-machinery-sidecar.sh "<VAULT_PATH>" --dry-run   # preview
    bash ~/.claude/skills/ai-brain-starter/scripts/relocate-machinery-sidecar.sh "<VAULT_PATH>"             # apply
    ```

  Say it simply, in their language: *"That folder syncs to the cloud, which can freeze your computer. I'm moving the noisy hidden part out so it's safe — your notes don't move and nothing gets deleted."* Then confirm `<VAULT_PATH>/.git` is now a one-line pointer (`cat "<VAULT_PATH>/.git"` shows `gitdir: ...`) and continue. Idempotent + safe to re-run (sweep new caches after the first session), fully reversible with `--rollback`. Details: `docs/CLOUD_SYNC.md` Shape B.

  - **Fallback — ONLY if the user explicitly says they don't want the vault in `<service>` at all:** move it fully local instead. *"No problem — paste a local path like `~/Brain` and I'll re-check."* Re-run the check until it returns `OK_LOCAL`, then continue. (Large vault already there → `docs/CLOUD_SYNC.md` for how to move it out safely.)

### Step 8.6 — Establish an off-machine backup (or explicitly defer, with a warning) — BEFORE declaring setup complete

The vault is the one irreplaceable thing here. Local-disk-only is the silent killer: it works perfectly right up until the drive dies, and then everything is gone — every note, every journal entry. The hourly git auto-snapshot is **local-only** (it refuses to run with a remote), so it is rollback history, not a backup. A real person hit exactly this: ~1,100 notes, no iCloud, no Time Machine, no remote, one disk. One failure from total loss.

**So a brand-new vault does not count as "set up" until it has an off-machine backup, or the user has explicitly chosen to defer with eyes open.** Do not skip this silently.

First, check what they already have:

```bash
python3 ~/.claude/skills/ai-brain-starter/scripts/check-vault-backup.py "<VAULT_PATH>"
```

- Verdict `OK ...` (Time Machine, a cloud copy, or a pushed git remote already exists) → say so warmly: *"Good — you already have an off-machine copy via <source>, so you're covered. I'll move on."* Done.
- Verdict `FAIL  ... NO off-machine backup` → set one up now. It is one command and picks a destination the user already has:

```bash
bash ~/.claude/skills/ai-brain-starter/scripts/vault-backup.sh setup --vault "<VAULT_PATH>"
```

Walk them through it in their language: it asks for a destination folder — **an external drive, or a Google Drive / Dropbox / OneDrive folder** (a single daily archive syncs fine; it is the churning vault that must never live in cloud sync, not one compressed file). It writes one compressed snapshot immediately, installs a daily schedule, and never touches the machine-exhaust dirs. **For a vault that will hold journals, health data, or client/CRM notes, add `--encrypt`** — it stores the passphrase in the OS keychain, never in plaintext.

Then have them prove it actually restores (a backup nobody has restored is a hope, not a backup):

```bash
bash ~/.claude/skills/ai-brain-starter/scripts/vault-backup.sh verify --vault "<VAULT_PATH>"
```

**If the user genuinely wants to defer** (no external disk handy, wants to decide on a cloud folder later): do not fight them, but do not let it pass silently either. Say plainly: *"Okay — just so you know, your brain currently has no off-machine backup, so right now one disk failure would lose everything. I've left it un-set-up at your call. You'll see a reminder at the start of every session until a backup exists, and the one command to fix it is `bash ~/.claude/skills/ai-brain-starter/scripts/vault-backup.sh setup`."* The SessionStart signal (`surface-backup-status.py`) then keeps it visible until it's done — it is advisory and never blocks, but it does not go quiet. See `docs/BACKUP.md`.

