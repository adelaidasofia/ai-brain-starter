---
creationDate: {{DATE}}
type: rule
purpose: Full protocol for daily update-check and drift-check at session start
trigger: Every session start, after reading CLAUDE.md + Last Session.md + Current Priorities.md
---

# Session start — daily AI brain setup update check

At the start of every session, after loading the usual context (CLAUDE.md, Last Session.md, Current Priorities.md), do a quick **once-per-day** check for updates to the AI brain setup itself. The user is non-technical and doesn't run terminal commands — you do this for them, and you translate the result into plain English.

<!-- NONDEV-REGISTER:START — canonical block. Byte-identical in every file listed in scripts/check-nondev-register-sync.py. Edit one copy, run that script, propagate. -->
## Plain-language register — NON-NEGOTIABLE

Most people running this vault are not developers. They journal, plan, think, run a business. Governs every phase, every skill, every session — never close-only.

1. **Never narrate machinery.** No "git snapshot", "Bash task", "mutex", "worktree", "another session running in parallel", no raw revision id. Machinery runs automatically and silently. Maintainer reference, never something the user reads or hears.
2. **Never end a turn on a technical either/or.** Commit or not, push or not, which merge strategy, keep or delete a branch, save these files or leave them — not theirs to answer blind. Do the safe thing and say what you did in one plain sentence, or ask something anyone could answer without knowing what git is.
3. **Never make them paste a credential.** No API key into a shell config, no dotfile edit, no terminal. Set it for them, or route the feature through a path that needs no key. A key pasted into a dotfile is a key they cannot find, rotate, or remove.
<!-- NONDEV-REGISTER:END -->

## How to run the check

Run this command quietly (don't show output to the user yet):

**Mac / Linux:**
```bash
bash ~/.claude/skills/ai-brain-starter/scripts/update-check.sh
```

**Windows (PowerShell):**
```powershell
powershell -File "$env:USERPROFILE\.claude\skills\ai-brain-starter\scripts\update-check.ps1"
```

The script outputs one of four statuses: `UP_TO_DATE`, `SKIPPED_TODAY`, `BEHIND`, or `ERROR`.

## How to handle each status

### `SKIPPED_TODAY`

Already checked today. Do nothing. Don't mention it to the user.

### `UP_TO_DATE`

Their setup is current. Do nothing. Don't mention it to the user — they don't need a "you're up to date" message every day.

### `ERROR`

Something went wrong (network down, repo missing, etc.). The script's `REASON:` line tells you what. **Do not surface this to the user unless it's recurring.** A one-time fetch failure is normal (their wifi might be flaky); a persistent failure means their setup is broken and you should help them fix it.

### `BEHIND`

This is the interesting case. The script outputs:
- `COMMITS_BEHIND: <number>` — how many updates they're missing
- `---CHANGELOG_NEW---` block — the raw CHANGELOG entries since their current version, in markdown

**Translate the CHANGELOG entries into plain English.** The CHANGELOG is written for humans but uses some technical phrasing — "Phase 0", "MCP", "skills", "scripts", etc. Strip the jargon. Focus on what the user gets:

- **Skip:** version numbers, file paths, git commits, code snippets, install commands
- **Keep:** user-facing benefits (new tools, faster workflows, fixed bugs that affected them, new features)
- **Tone:** warm and conversational, not a release notes dump

**Then say to the user, in their primary language:**

> "Hey, just a heads up — your AI brain setup has an update available. Here's what's new:
>
> [1–4 plain-English bullets summarizing the new features and fixes from the CHANGELOG slice. Pick the 4 most user-relevant. Skip the technical ones.]
>
> Want me to install it? It takes about a minute and it's safe — I just run a script that updates everything automatically. Or I can hold off until later if you're in the middle of something."

**If they say yes:**

Run the bootstrap, which is idempotent and only installs what's missing:

**Mac / Linux:**
```bash
bash ~/.claude/skills/ai-brain-starter/bootstrap.sh
```

**Windows (PowerShell):**
```powershell
powershell -File "$env:USERPROFILE\.claude\skills\ai-brain-starter\bootstrap.ps1"
```

When the bootstrap finishes, parse its verification block. If everything passed, tell the user:

> "Done. Your setup is now up to date. [If anything visible changed, mention it: 'You should see the new /[skill-name] command available in this session.']"

If the verification block reported failures (red ✗ markers), tell the user EXACTLY which items failed and offer to retry. **Never silently ignore failures** — that's how broken setups get baked in for weeks.

**If they say no / not now:**

Don't run the bootstrap. Just say: "No problem. I'll check again tomorrow." The script already wrote today's date to the cooldown file, so you won't ask again until tomorrow.

## What to do if the check command itself isn't installed

If `~/.claude/skills/ai-brain-starter/scripts/update-check.sh` doesn't exist, the user is on a very old version. Tell them: "Heads up — your AI brain setup is from before automatic updates were added. Want me to do a one-time refresh so future updates work automatically?" If yes, run the bootstrap (which will pull the latest and create the update-check script).

---

# File drift check — runs every session, separately from CHANGELOG check

The CHANGELOG check above only tells you whether the user is BEHIND on commits. It does NOT tell you whether files that were already installed in a prior release have since drifted from the repo's version. Drift can happen because: a previous sync only partially landed, the user hand-edited a script in their vault, a `git stash` recovery left files mixed, or a manual cherry-pick from upstream missed something.

**After handling the CHANGELOG check above (regardless of whether it returned UP_TO_DATE, BEHIND, or SKIPPED_TODAY), run the drift check.** It honors its own once-per-day cooldown so it won't double-prompt during the same day.

### How to run the drift check

You need the user's vault path to check vault-scope drift. The vault path is the directory that contains the `CLAUDE.md` file you loaded at session start — walk up from the current working directory until you find a `CLAUDE.md`, that ancestor directory is `$VAULT_PATH`. (If you can't find one, run drift-check without `--vault` and it'll only check the installed-skills scope.)

**Mac / Linux:**
```bash
bash ~/.claude/skills/ai-brain-starter/scripts/drift-check.sh --vault "[VAULT_PATH]"
```

**Windows (PowerShell):**
```powershell
powershell -File "$env:USERPROFILE\.claude\skills\ai-brain-starter\scripts\drift-check.ps1" -Vault "[VAULT_PATH]"
```

The script outputs:

```
STATUS: <OK | DRIFT | SKIPPED_TODAY | ERROR>
DRIFT_COUNT: <integer>
---DRIFT_FILES---
<scope>|<installed_path>|<repo_source_path>|<note>
...
---END---
```

Scopes:
- `skill` — file under `~/.claude/skills/<skill>/` differs from `<starter>/skills/<skill>/<rel-path>`
- `vault-script` — file under `$VAULT/⚙️ Meta/scripts/<basename>` differs from `<starter>/scripts/<basename>`
- `vault-rule` — block in `$VAULT/CLAUDE.md` (identified by an H1 heading) differs from `<starter>/templates/rules/<file>.md`
- `vault-rule-file` — standalone file under `$VAULT/⚙️ Meta/rules/<basename>` differs from `<starter>/templates/rules/<basename>`

### How to handle each status

**`SKIPPED_TODAY`** — already checked today. Do nothing. Don't mention it.

**`OK` with `DRIFT_COUNT: 0`** — everything is in sync. Do nothing. Don't mention it.

**`ERROR`** — same rule as update-check: a one-time error is normal, surface it only on recurring failures.

**`DRIFT` (or, from an older installed copy, `OK` with `DRIFT_COUNT > 0`)** — this is the interesting case. Treat the two identically: `DRIFT` is the current token, and the `OK`-plus-count form is what a copy predating the token change still emits. Walk the user through it interactively. NEVER batch-overwrite. NEVER skip the diff display. NEVER skip the backup.

### How to walk the user through drift

Open with a warm, non-technical lead-in:

> "Hey, I noticed [N] files in your setup have drifted from the repo version. That can happen when a previous update only partially landed, or if a file was hand-edited. I'll walk you through them one by one — for each one I'll show you what's different, you decide whether to update it, and I always make a backup first so nothing is ever lost. Sound good?"

If they say no / not now: "No problem — I'll check again tomorrow." Stop. Don't ask again until the next day's cooldown clears.

If they say yes, for **each drift entry**, do this exact sequence:

1. **Read both files** (Read tool on `installed_path` and `repo_source_path`).

2. **Compute and show a compact diff.** Use the Bash tool: `diff -u "<installed_path>" "<repo_source_path>" | head -80`. If the diff is longer than 80 lines, show the first 80 and tell the user "[truncated — N lines total]". Frame the diff naturally: "Here's what would change in [basename]:"

3. **For `vault-rule` drift**, the diff is between two markdown blocks. Be extra clear that the change is to a *block inside* their CLAUDE.md, not the whole file. Say: "This is a block inside your vault's CLAUDE.md, identified by the heading `[heading]`. I'd replace just that block, not the whole file."

4. **If the entry has a `note` field**, surface it before asking. Especially for `graph-context-hook.sh` (note: `hand-edited CONFIG block at top of file — cherry-pick changes, do NOT overwrite wholesale`) — tell the user: "This file has a hand-edited config block at the top with paths and settings specific to your vault. I won't replace it wholesale. Instead, I can show you what's new in the repo version and let you decide if any of those changes are worth merging by hand."

5. **Ask the user, one of five actions:**
   - **update** — apply this one
   - **skip** — leave this file alone for now, move to the next (will ask again next time drift-check runs)
   - **skip permanently** — never ask about this file again. Appends the installed path to `~/.claude/.ai-brain-starter-drift-check-ignore` so future drift-check runs silently filter it out.
   - **update all remaining** — apply every remaining drift without further asking (still backs up each one, still respects `note` warnings)
   - **stop** — quit the drift walkthrough entirely

6. **If the user says update** (or "update all"), do this in order — backup ALWAYS happens first, no exceptions:
   - `cp "<installed_path>" "<installed_path>.bak-$(date +%Y-%m-%d-%H%M)"` — back up the file
   - **For `skill` and `vault-script` scopes**: `cp "<repo_source_path>" "<installed_path>"` — overwrite with repo version
   - **For `vault-rule` scope**: use the Edit tool with `old_string` = the entire installed block and `new_string` = the entire repo template content. Do NOT overwrite the whole CLAUDE.md file — only replace the block.
   - Confirm to the user: "Updated. Backup at `<installed_path>.bak-...`."

7. **If the user says skip**, log it: "Skipped [basename]." Move on.

7a. **If the user says skip permanently**, append the installed path to `~/.claude/.ai-brain-starter-drift-check-ignore`.

8. **After the walkthrough**, summarize: "Done. Updated [N], skipped [M]. Every updated file has a timestamped backup next to it."

### Safety rules — non-negotiable

- **Backup before every change.** No silent overwrites, ever.
- **Never overwrite a file flagged with a `note`.** Cherry-pick or ask.
- **For `vault-rule` drift, only replace the block — never the whole file.**
- **Show the diff before asking.** The user cannot consent to a replacement they haven't seen.

### What to do if drift-check itself isn't installed

If `~/.claude/skills/ai-brain-starter/scripts/drift-check.sh` doesn't exist, the user is on a version that predates drift detection. Don't surface this — the next bootstrap will install it. Just skip the drift block silently and continue.

## Why these checks matter

Users on this setup are non-technical. They will never run `git pull` themselves. Without these checks, they would stay on whatever version they first installed forever — missing every bug fix, every new tool, every workflow improvement.
