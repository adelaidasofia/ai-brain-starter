# Back up your brain (off-machine, in one command)

Your vault is the one irreplaceable thing here. Everything else — the skills, the
hooks, this repo — is reinstallable. Your notes and journals are not.

Local-disk-only is the silent killer. The vault works perfectly right up until
the disk dies, and then it is all gone at once: no warning, no degraded mode. A
real person hit exactly this — about 1,100 notes, no Time Machine, no cloud copy,
no git remote, a single drive. One hardware failure away from losing everything,
and nothing ever said so out loud.

This page is how the brain makes sure that can't happen to you quietly.

> **The rule: a brain in daily use has at least one off-machine copy, and you
> have restored from it at least once.** Anything less is a hope, not a backup.

---

## What "backup" is NOT

- **The hourly git auto-snapshot is not a backup.** It is *local-only* by design
  (it refuses to run if a remote exists) — brilliant rollback history, zero
  protection against the disk failing. See `scripts/auto-snapshot.sh`.
- **A cloud-synced vault is a backup, but the wrong kind.** A live copy in
  iCloud / OneDrive / Dropbox helps if the disk dies — but pointing a sync daemon
  at the churning vault (worktrees, `.git` objects) is the machine-melting
  failure `docs/CLOUD_SYNC.md` exists to prevent. The fix below gives you the
  off-machine copy *without* the churn: one compressed file per day, not a live
  mirror of a million tiny objects.

---

## The one command

```bash
bash ~/.claude/skills/ai-brain-starter/scripts/vault-backup.sh setup
```

(Windows: `pwsh ~/.claude/skills/ai-brain-starter/scripts/vault-backup.ps1 setup`)

It asks you one thing: **where the backup should go.** Pick a destination you
already have, off this machine:

- an external drive (`/Volumes/Backup`, `D:\Backup`), or
- a cloud folder you already sync (Google Drive / Dropbox / OneDrive / Box).

A cloud folder is fine *as a destination* — the backup is **one compressed file
that gets replaced once a day**, so there is no sync storm. It is the live vault
that must never live in a sync folder, not a single daily archive.

Then `setup`:

1. writes the **first snapshot immediately** (so you are protected right away),
2. installs a **daily schedule** (launchd on macOS, cron on Linux, a Scheduled
   Task on Windows) at 03:00 local,
3. **excludes the regenerable machine-exhaust** (`.claude/worktrees`,
   `.smart-env`, `.codegraph`, caches) — your notes and `.git` history are kept,
   the bloat is not.

Provider-agnostic: the destination is just a folder path. Nothing is hard-wired
to one cloud.

### Sensitive vault? Encrypt it.

If your vault holds journals, health data, or client/CRM notes, add `--encrypt`:

```bash
bash ~/.claude/skills/ai-brain-starter/scripts/vault-backup.sh setup --encrypt
```

It encrypts each archive with AES-256 (via `gpg`, or `openssl` as a fallback) and
stores the passphrase in your **OS keychain** (macOS Keychain / libsecret /
Windows DPAPI), never in plaintext on disk. The daily run reads it from there
with no prompt.

---

## Prove it restores (do this once)

A backup you have never restored is a hope, not a backup. This actually extracts
the newest archive to a temp directory and confirms your notes come back:

```bash
bash ~/.claude/skills/ai-brain-starter/scripts/vault-backup.sh verify
```

It records the verification date. The session-start signal will nudge you to
re-verify periodically — restoring is the only thing that proves the chain works
end to end.

---

## Check status any time

```bash
bash ~/.claude/skills/ai-brain-starter/scripts/vault-backup.sh status
```

Shows the destination, whether it is reachable, how fresh the snapshots are, when
you last verified a restore, and the canonical verdict from the detector.

---

## How the brain keeps you honest

You do not have to remember any of this. Two surfaces keep it visible:

- **At session start**, `surface-backup-status.py` checks for *any* off-machine
  copy — our `vault-backup`, a configured Time Machine destination, a cloud copy,
  or a pushed git remote. If there is **none**, it prints a loud line *every
  session* until one exists. It is advisory and never blocks; it just does not go
  quiet. (Bypass for a session with `VAULT_BACKUP_BYPASS=1`.)
- **`/diagnose`** (section 12) reports the same verdict in the health check, and
  the onboarding interview (`phases/phase-01-welcome.md`, step 8.6) establishes a
  backup — or makes you decline it on purpose — before setup is called done.
- **On Windows**, re-running `setup` self-heals the daily scheduled task: it reads
  the existing task back, checks the script path, the interpreter, and the
  battery-power setting, and re-registers it automatically if any of them is
  wrong — no prompt, and a healthy task is left untouched.

The single source of truth for all of these is
`scripts/check-vault-backup.py` — run it directly any time:

```bash
python3 ~/.claude/skills/ai-brain-starter/scripts/check-vault-backup.py "<vault-path>"
```

---

## If you'd rather use restic (offsite, incremental, advanced)

`vault-backup.sh` is the zero-config default: one file, one command, any folder.
If you want incremental dedup + an offsite repo (S3, B2, an SFTP box) with
point-in-time history, [restic](https://restic.net) is the heavier-duty tool:

```bash
# one-time
restic init --repo /Volumes/Backup/brain-restic        # or s3:..., b2:..., sftp:...
# each run (cron it): keep the notes, skip the machine-exhaust
restic backup "<vault-path>" \
  --exclude .claude/worktrees --exclude .smart-env --exclude .codegraph \
  --repo /Volumes/Backup/brain-restic
restic restore latest --target /tmp/restore-check --repo /Volumes/Backup/brain-restic  # verify!
```

restic encrypts the whole repo by default and is the right call for an offsite,
versioned copy. The two compose: `vault-backup.sh` for the always-on local-disk
snapshot, restic for the offsite tier when you want it.

---

## See also

- **`docs/CLOUD_SYNC.md`** — why the live vault must stay out of cloud-sync
  folders (the sync-storm failure), and how to move it out safely. Back up *with
  this page* before you relocate.
- **`docs/MAINTENANCE.md`** — the ongoing hygiene scans (worktrees, naming,
  graphify rotation) that keep the vault healthy over time.
