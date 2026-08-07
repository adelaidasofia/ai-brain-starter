---
type: skill
name: diagnose
description: 'Use when an AI Brain vault needs a health check or something feels off: Claude ignores CLAUDE.md, journal entries missing from /weekly, hooks not firing, something broke after a git pull, a friend''s install misbehaves, Obsidian crashes or pegs the CPU, a .claude/worktrees folder appeared, Granola/WhatsApp/Slack/Gmail notes stopped arriving, or Windows .ps1 scripts error. Also after major /setup-brain upgrades. Not for debugging code; audits the vault install only.'
trigger: /diagnose
argument-hint: "[vault path, defaults to $VAULT_PATH or current directory]"
tool_access:
  - Bash
  - Read
  - Glob
  - Grep
policy_constraints:
  - rule: Never modify vault files; this is a read-only self-check
    exception_handling: Surface the issue to the user with a remediation hint, do not auto-fix
  - rule: Never write findings outside the user's terminal report
    exception_handling: Print to stdout only; no log files or vault writes
  - rule: Treat missing CLAUDE.md, missing Meta folder, or stale journal index as red findings, not silent skips
    exception_handling: Emit a red status row naming the missing artifact
required_inputs:
  - name: vault_path
    type: path
    required: false
    description: Path to the vault root. Defaults to $VAULT_PATH env var or current working directory.
output_shape:
  format: terminal-report
  fields:
    status_rows: list of {check_name, color (green/yellow/red), detail}
    summary_line: one-line overall health status
    exit_code: 0 if all green, 1 if any yellow, 2 if any red
---

# /diagnose

> **`{SKILL_DIR}`** = this skill's own folder (locally: the directory this SKILL.md lives in; a served brain substitutes the real absolute path before you read this). Shared starter files live at the repo root two levels up: `{SKILL_DIR}/../..`. If a path does not resolve, name the missing file and stop — never guess another location.

Tells you whether your second brain is healthy.

## Why

Most "Claude is broken" reports trace to one of:

- CLAUDE.md missing or has no Vault Map
- Hooks not registered, so no auto context-loading
- `journal-index.json` stale or malformed, so insights find nothing
- `.ps1` files lost their UTF-8 BOM, so Windows PowerShell crashes
- ai-brain-starter is many commits behind, so the user is on stale logic

`/diagnose` checks all of these in ~5 seconds. It writes nothing, sends no network requests beyond a single `git fetch`, and exits with a status code so it can be wired into CI or cron.

## What to do

1. Find the script: `{SKILL_DIR}/../../scripts/diagnose.sh` (the starter repo root sits two levels above this skill's folder on every install shape).

2. Run it. Pick the right one for the platform:

   **Mac / Linux:**
   ```bash
   bash "{SKILL_DIR}/../../scripts/diagnose.sh"
   # or pass an explicit vault:
   bash "{SKILL_DIR}/../../scripts/diagnose.sh" "/path/to/vault"
   ```

   **Windows:**
   ```powershell
   pwsh "{SKILL_DIR}/../../scripts/diagnose.ps1"
   # or:
   pwsh "{SKILL_DIR}/../../scripts/diagnose.ps1" -Vault "C:\path\to\vault"
   ```

   By default it uses `$VAULT_PATH` if set, else the current directory.

3. Read the output to the user in plain language. Don't dump the raw report unless they ask. Translate:

   - **All green:** "Your vault is healthy. Nothing to do."
   - **Only WARNs:** "Working, but $N things to clean up. Want me to fix them?" Then offer to fix the specific WARNs (re-build journal index, add Vault Map to CLAUDE.md, pull latest ai-brain-starter, etc).
   - **At least one FAIL:** "Something is broken: [name the FAIL in one sentence]. I can fix it: [propose the fix]." Wait for confirmation, then fix.

## What each check means

| # | Check | If FAIL | If WARN |
|---|---|---|---|
| 1 | CLAUDE.md present + has Vault Map | Re-run /setup-brain Phase 4 | Add the Vault Map section by hand |
| 2 | ⚙️ Meta/ + scripts/ + rules/ present | Re-run /setup-brain Phase 3 | Create the missing subfolder |
| 3 | ai-brain-starter + daily-journal skills installed | Re-run bootstrap | Install the missing skill from its phase |
| 4 | Hooks registered + graph-context-hook parses | Phase 5 wires hooks; bash -n the script | - |
| 5 | journal-index.json valid + fresh | Delete and rebuild via build-journal-index.py | Re-run /weekly to refresh |
| 6 | git, python3 (jq optional) on PATH | brew install / winget install | Optional tools missing |
| 7 | Vault is a git repo | - | Recommend `git init` for snapshot history |
| 8 | All .ps1 have BOM, no em dashes, parse clean | Fix the parser error | Add BOM and strip em dashes (see SKILL.md notes) |
| 9 | MCP config valid JSON | Fix the JSON | No MCPs registered (fine if intentional) |
| 10 | ai-brain-starter up to date with origin/main | Re-clone | git pull in the starter root (`{SKILL_DIR}/../..`) |
| 10b | No scheduled-task name collides with a skill; cron-only tasks `_`-prefixed | - | Rename per docs/MAINTENANCE.md |
| 11 | Vault on a local disk, not a consumer cloud-sync root | Move it local (docs/CLOUD_SYNC.md) | Could not evaluate the path |
| 12 | Vault has an off-machine backup | Set one up: `bash scripts/vault-backup.sh setup` (docs/BACKUP.md) | Backup configured but no snapshot yet |
| 13 | No repeated Obsidian renderer crashes (macOS; skips elsewhere) | - | Heavy indexer likely OOM-ing the renderer on a large vault: restricted mode -> Dataview only -> add others one at a time (see the obsidian-plugins rule, "Large-vault plugin posture") |
| 14b | Ingest connectors still producing data (the silent-empty 0-vs-0 gap) | - | A connector exited 0 but returned 0 items (a vendor changed a surface): check its auth/permissions, re-run its ingest skill, confirm it pulls >0 items |
| 17 | No git worktree living inside the Obsidian-watched vault tree | - | The Desktop per-session worktree checkbox dropped a checkout under `.claude/worktrees/` inside the vault -> renderer OOM/crash. Relocation is dead; the flag does NOT gate it. Relaunch the vault PLAIN with the worktree box UNCHECKED (`cd <vault> && claude`). See docs/VAULT_WORKTREE_MELT.md |

## When to suggest /diagnose proactively

- User says "Claude isn't reading my CLAUDE.md anymore"
- User says "the journal entries aren't showing up in /weekly"
- User says "I just did a git pull and something feels off"
- User says "my friend installed it but it's broken"
- User says "Obsidian keeps crashing when I open it" / "Obsidian won't open" (likely a heavy-indexer renderer OOM on a large vault - check 13)
- User says "my brain feels stale" / "I haven't seen any new Granola/WhatsApp/Slack/Gmail notes in a while" / "<source> stopped showing up" (likely a silently-empty connector - check 14b)
- User says "Obsidian melted / pegged the CPU after I opened a Claude Desktop session" / "the vault doubled itself" / "there's a `.claude/worktrees` folder full of copies of my vault" (likely a worktree-on-vault checkout - check 17; relaunch PLAIN with the worktree box unchecked)
- After every major upgrade prompt during /setup-brain

Do NOT use /diagnose as an email-capture surface. If `~/.claude/.ai-brain-starter-email-on-file` is missing, that is fine and never a finding — the email is optional, and the only places it is ever asked are the setup interview (Phase 24.4) and the once-per-update post-pull nudge. Never tell the user to fetch or paste a token.

## Don't

- Don't run any of the FAIL fixes without confirming first.
- Don't pipe `/diagnose` output into a `gh issue` or anywhere public — it includes home directory paths.
- Don't treat a WARN about ai-brain-starter being behind as urgent. Default cadence is fine.
