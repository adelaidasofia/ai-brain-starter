# Vault Maintenance

Automated hygiene checks to keep your vault clean over time. These complement the one-time setup from `bootstrap.sh` with ongoing maintenance.

## Off-machine backup (read this first)

The single most important piece of ongoing maintenance is making sure the vault
is not your only copy. The hourly git auto-snapshot is *local-only* — rollback
history, not a backup. One disk failure with no off-machine copy loses
everything.

Set up a daily off-machine backup in one command (provider-agnostic destination,
optional encryption, with a real restore check):

```bash
bash scripts/vault-backup.sh setup     # then: ... verify   to prove the restore
```

The session-start signal (`surface-backup-status.py`) and `/diagnose` (section
12) both nag until a backup exists. Full guide: **[docs/BACKUP.md](BACKUP.md)**.

## Scripts

### vault-daily-maintenance.sh (resource-gated daily cron)

The home for heavy maintenance that should never run on the interactive session-close path. On a mature vault (10k-60k+ files) the close-time git snapshot is heavy, and the full-tree hygiene scans are heavier still; running them at close on an already-busy machine can pin or crash it right when you wrap up. This script moves that work to a once-a-day, low-priority, load-gated pass.

It does two jobs:

1. **Reconcile (data safety).** Re-runs the aggregators and commits any session / decision / captures files a session-close left uncommitted. The close hook (`scripts/session-end-hook.sh`) defers its git snapshot when the machine is saturated or a sibling close holds the cascade mutex; this is the catch-up that snapshots that work within a day, so a deferred close loses nothing.
2. **Heavy hygiene (best effort).** Runs the full-tree / git-log-walking scripts the substrate ships but that are too heavy for every close: `drift-detection.py`, `check-rule-conflicts.py --scan-all`, `passive-capture.py --scan-today`. Each runs only if present, never aborts the pass, and runs at `nice -n 10`.

```bash
# load-gated normal run (skips heavy work if 1-min loadavg/core >= 3.0)
bash scripts/vault-daily-maintenance.sh --vault-root /path/to/vault

# ignore the load gate (run now no matter what)
bash scripts/vault-daily-maintenance.sh --vault-root /path/to/vault --force

# only the deferred-close catch-up, skip heavy hygiene
bash scripts/vault-daily-maintenance.sh --vault-root /path/to/vault --reconcile-only
```

Resource-awareness (load gate + close-cascade mutex) is shared with the close hook via `scripts/_session_close_guard.sh`, so the two can never drift. Override the threshold with `CLOSE_MAX_LOAD_PER_CORE` (default `3.0`). Logs to `<vault>/Meta/logs/vault-daily-maintenance.log`. See the close-side documentation in `templates/rules/session-close.md` ("Resource-aware close").

**The schedule installs itself.** `scripts/install-hooks-user-level.py` calls
`install-vault-daily-maintenance.sh` whenever it knows the vault path — launchd
on macOS, a cron entry elsewhere, both idempotent. This is deliberate: every
reclaim tool below used to be opt-in, so on a default install none of them ever
ran and the machine only ever got fuller (MYC-2363). Nothing gates the VALUE
behind a flag; `ABS_NO_AUTO_GC=1` turns it off for someone who drives reclaim by
hand.

Re-point or re-install it by hand with:

```bash
# macOS (launchd) / Linux (cron) — same command, runs daily at 04:30 local
bash scripts/install-vault-daily-maintenance.sh /path/to/vault
```

**What the auto-GC tier reclaims** (each was previously opt-in, each runs behind
the same load + battery + user-idle gates):

| Leg | Reclaims |
|---|---|
| `worktree-prune.sh` | vault scratch worktrees, orphan dirs/branches, old snapshots |
| `graphify_prune_stale_cache.py` | graph-cache entries matching no current file |
| `dev-repo-reaper.py --apply` | merged branches / worktrees / checkpoint stashes |
| `dev-worktree-prune.py --apply` | orphaned `<dev-root>/<repo>-<slug>` session worktrees |
| `dev-hub-refresh.py --apply` | fast-forwards clean bare hubs so a recon read is never weeks stale |
| `dev-drift-report.py --write-state` | measures un-backed-up drift for the SessionStart surface |

Everything reaped is provably preserved elsewhere: a branch is reaped only when
its content is on a remote, a dirty worktree is snapshotted to
`~/.claude/logs/dev-worktree-snapshots/` before removal, and un-backed-up work is
surfaced for a human, never deleted.

**Idle-awareness.** Beyond the load gate, the pass defers when the machine is on
battery or when someone was at the keyboard in the last 5 minutes
(`MAINT_MIN_USER_IDLE_SEC`). Both are deferrals with catch-up, not skips.

### vault_maintenance.py

Monthly scan that checks 7 categories of vault hygiene issues:

- **Inbox overdue** - files sitting in Inbox/ longer than 7 days
- **Naming issues** - filenames over 60 chars or starting with lowercase (likely un-renamed imports)
- **Stray binaries** - images, PDFs, and docs outside designated folders
- **Backup accumulation** - .bak and .backup_ files scattered across the vault
- **Empty folders** - folders with 0 files
- **Large folders** - any folder with 500+ files (warning threshold)
- **Graphify backups** - graph.json.backup_* count (target: 3 or fewer)

```bash
python3 scripts/vault_maintenance.py --vault-root /path/to/vault
```

Output: a Markdown report at `Meta/Maintenance Report.md` (auto-detects emoji-prefixed Meta folder).

Options:
- `--binary-allowed Media Pics Archive` - override which folders are allowed to contain binary files

### rotate_graphify_backups.py

Keeps the N most recent graphify backups and deletes the rest. Also cleans .bak files older than N days.

```bash
python3 scripts/rotate_graphify_backups.py --vault-root /path/to/vault
python3 scripts/rotate_graphify_backups.py --vault-root /path/to/vault --keep 5 --bak-max-age 14
```

### rotate-last-session.py

Keeps `Last Session.md` lean by archiving older sessions to monthly files. `Last Session.md` is read on every UserPromptSubmit, so every stale session in it pays a token tax on every prompt.

```bash
python3 scripts/rotate-last-session.py --vault-root /path/to/vault          # keep last 3 (default)
python3 scripts/rotate-last-session.py --vault-root /path/to/vault --keep 1 # keep only the newest
python3 scripts/rotate-last-session.py --vault-root /path/to/vault --dry-run
```

### decision-outcome-check.py

Walks the Decision Log and surfaces decisions older than N days (default 30) with a blank `Outcome:` field. A decision log without outcomes can't teach you about your own patterns. Run weekly or wire into a scheduled task.

```bash
python3 scripts/decision-outcome-check.py --vault-root /path/to/vault           # default 30 days
python3 scripts/decision-outcome-check.py --vault-root /path/to/vault --days 14 # more aggressive
python3 scripts/decision-outcome-check.py --vault-root /path/to/vault --dry-run
```

## Scheduled Tasks

Set these up as Claude Code scheduled tasks for hands-off maintenance:

### Naming convention: prefix cron-only tasks with `_`

Scheduled tasks live in `~/.claude/scheduled-tasks/<name>/SKILL.md`. Claude Code's slash autocomplete registers these alongside `~/.claude/skills/` entries, so a user typing `/` sees both real skills and cron-triggered tasks in the same list. That is confusing when a task shares a stem with a real skill (e.g., `daily-journal` the cron-style task vs `journal` the conversational skill).

Workaround until upstream supports a `cron_only: true` frontmatter flag (tracked in [anthropics/claude-code#57508](https://github.com/anthropics/claude-code/issues/57508)): name your scheduled tasks with a leading underscore so they sort to the bottom of autocomplete and read as cron-only at a glance.

| Bad | Good |
|---|---|
| `daily-journal` | `_daily-journal-cron` |
| `graphify-weekly-check` | `_graphify-weekly-cron` |
| `monthly-token-optimization` | `_monthly-token-cron` |

Edit the existing task by renaming the directory and updating the `name:` field in its `SKILL.md` frontmatter to match. `/diagnose` will warn on any scheduled task that does not follow this convention or that collides with an installed skill name.

### Daily Vault Maintenance + auto-GC (installed by default)
- **What:** the resource-gated daily cron above (`vault-daily-maintenance.sh`): deferred-close reconciliation + heavy hygiene, off the interactive close path.
- **Install:** automatic — the hook installer schedules it (launchd on macOS, cron elsewhere). Re-run by hand with `bash scripts/install-vault-daily-maintenance.sh /path/to/vault`; opt out with `ABS_NO_AUTO_GC=1`.
- **Why a launchd/cron job and not a Claude Code scheduled task:** this is a deterministic shell pass with no model judgment, and it must run at low IO/CPU priority even when no Claude session is open, exactly what launchd/cron does and a scheduled task does not.

### Monthly Vault Maintenance (recommended)
- **Schedule:** 1st of every month, 9am
- **Cron:** `0 9 1 * *`
- **Prompt:** "Run the vault maintenance scan. Execute: python3 '{vault}/Meta/scripts/vault_maintenance.py' --vault-root '{vault}'. Read the generated Maintenance Report and summarize findings."

### Quarterly Vault Audit (optional)
- **Schedule:** 1st of Jan/Apr/Jul/Oct, 10am
- **Cron:** `0 10 1 1,4,7,10 *`
- **Prompt:** See [QUARTERLY-RUNBOOK.md](QUARTERLY-RUNBOOK.md) — a 10-step runbook that refreshes About Me, vault map counts, domain summaries, CRM spot checks, wikilink reference, and trend files. Goes deeper than the monthly scan. Paste the runbook body into your scheduled task prompt.

### Weekly Graphify Backup Rotation (recommended if using graphify)
- **Schedule:** Sundays, 3am
- **Cron:** `0 3 * * 0`
- **Prompt:** "Run graphify backup rotation. Execute: python3 '{vault}/Meta/scripts/rotate_graphify_backups.py' --vault-root '{vault}'. Report results."

## The Inbox Pattern

Create an `Inbox/` folder at your vault root as a quick-capture landing zone. The rule: nothing stays there longer than 7 days. The monthly maintenance scan flags overdue items.

This prevents the "junk drawer" problem where notes pile up in random folders because you didn't have time to file them properly. Everything enters through Inbox, then gets filed to its permanent home within a week.

Add this to your vault's obsidian rules:
> **Inbox zero.** Inbox/ is the quick-capture landing zone. Nothing stays there longer than 7 days. The monthly maintenance scan flags overdue items. When filing out, move in Obsidian UI so wikilinks auto-update.
