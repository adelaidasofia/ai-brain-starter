# Your vault can live in iCloud. Its machinery can't.

Your brain is a living git repository. Claude Code works inside it with
per-session git **worktrees** — fast, throwaway checkouts under
`.claude/worktrees/`. That design is what makes parallel sessions safe. It
also means the directory churns constantly: thousands of files created and
deleted as you work, a `.git` that a single `git gc` rewrites wholesale, and
search-index caches that rebuild on the fly.

Point a consumer cloud-sync daemon — iCloud Drive, OneDrive, Dropbox, Google
Drive, Box — at that churn and it tries to upload every worktree file, every
`.git` object, every cache, in real time. On an active machine that compounds
into hundreds of thousands of file events and a sync daemon pinned at full CPU
for hours. It is the single most common way to make a healthy brain feel broken.

The storm hides one fact: your *notes* are not the problem. Markdown is tiny
and barely changes. The freeze comes entirely from the **machinery** — `.git`,
worktrees, caches — living inside the synced tree. So the rule is not "never
sync your brain." The rule is:

> **The vault may be synced. The machinery never is.**

That gives you three supported shapes, all first-class. Shapes A and B are for
your own single-user brain — pick by whether you want your notes on your phone.
Shape C is for a team sharing one vault. The footprint signal at session start
will warn you if it sees machinery inside a sync folder; here is how to set up
each shape.

---

## Three supported shapes

### Shape A — vault fully local (simplest)

Put the vault on a normal local path — `~/brain`, `~/vaults/<name>`, anywhere
**not** inside a sync root. On macOS that means **not** `~/Desktop` or
`~/Documents` when iCloud "Desktop & Documents" is on (both are synced); on
Windows, not inside `OneDrive\`. The index lives server-side; backups are a
deliberate off-machine choice (below). Nothing to configure — this is the
default.

Already inside a sync folder and you don't need phone access? Move it out onto a
local disk with the helper — **not** a raw `mv`. A bare move relocates the files
but silently orphans your **Claude Code session history**: Claude keys
per-project state on the vault's absolute path, so moving the vault makes every
prior transcript read *"Session history unavailable"* and leaves the agent-memory
symlink dangling. The helper does the move, leaves a symlink at the old path, AND
re-homes that Claude state (copying it, so the old location stays a backup):

```bash
# preview first (changes nothing)
bash scripts/relocate-vault.sh --ensure-backup ~/Desktop/MyVault ~/MyVault --dry-run
# do it — quit Obsidian + close Claude sessions first. --ensure-backup stands up AND
# verifies an off-machine backup, THEN moves, in one step. Fail-closed: if the backup
# can't be verified it refuses and leaves the vault untouched.
bash scripts/relocate-vault.sh --ensure-backup ~/Desktop/MyVault ~/MyVault
# already have a verified backup? the plain form moves once it confirms one exists
# (it still REFUSES a backup-less vault unless you pass --force):
bash scripts/relocate-vault.sh ~/Desktop/MyVault ~/MyVault
```

`--ensure-backup` writes the archive to `<parent-of-your-vault>/ai-brain-backups` by
default — for a cloud vault that sibling folder is off-machine, and a single daily
archive syncs without the storm the churning `.git` tree causes. Send it to an
external disk instead with `--backup-dest /Volumes/YourDrive`. This is exactly what
the SessionStart cloud-sync offer runs for you, so you usually never type it by hand.

**On Windows (PowerShell)** use the `.ps1` parity script — same behavior, same
Claude-history migration, but it leaves a **junction** at the old path (needs no
admin or Developer Mode, and OneDrive does not sync through it):

```powershell
# preview first (changes nothing)
powershell -ExecutionPolicy Bypass -File scripts\relocate-vault.ps1 "$env:USERPROFILE\OneDrive\MyVault" "$env:USERPROFILE\MyVault" -DryRun
# do it (quit Obsidian + close Claude sessions first; -Force overrides the soft gates)
powershell -ExecutionPolicy Bypass -File scripts\relocate-vault.ps1 "$env:USERPROFILE\OneDrive\MyVault" "$env:USERPROFILE\MyVault"
```

The SessionStart cloud-sync offer auto-detects your OS and prints whichever of
these you can actually run, so you usually do not type this by hand.

Already moved it with a plain `mv` and lost your session picker? Re-home just the
Claude Code state, no second move:

```bash
bash scripts/relocate-vault.sh --migrate-claude-state ~/Desktop/MyVault ~/MyVault
```

Sync daemons follow the *symlink file* (a few bytes), not the target's
contents — so the churn leaves the sync scope entirely.

**Retiring the old-path symlink (optional, later).** That symlink is a safety net:
while it is there, anything that still hardcodes the old path keeps working — which
also *hides* which references you have actually migrated. The day the symlink dies,
every un-migrated reference breaks (a scheduled job re-creates a phantom folder at
the old path; a hook command points at nothing). When you want to retire the old
path for good, sweep for what still resolves it first. The sweep reaches every
surface a vault touches — code repos (including docstrings and comments), JSON
config, MCP configs, docs, shell rc — greps each git repo at its **canonical**
`origin/main` (a stale local branch can hide a reference the repo still ships), and
classifies every hit:

- **executed** — a real command or path (a code line, a fenced shell block, a JSON
  string *value*). These break when the symlink dies. Repoint them first.
- **doc-pointer** — a comment, a docstring, prose. Cosmetic; fix at leisure.
- **keep** — intentional (a migration source, a dead JSON config *key*). Left alone.

```bash
# what still resolves the old path? (classified, with a go/no-go)
bash scripts/relocate-vault.sh --sweep ~/Desktop/MyVault ~/MyVault
# retire the symlink — refuses unless ZERO executed references remain
bash scripts/relocate-vault.sh --drop-symlink ~/Desktop/MyVault
```

`--drop-symlink` runs the sweep and removes the symlink only when nothing
executable still points at the old path; until then it tells you exactly what to
repoint. It also reports the `~/.claude.json` blast radius separately (dead project
*keys* are cosmetic; string *values* are load-bearing), and never edits anything
itself — the repoint is yours.

> **One failure, two shapes.** Whether a bare `.git/` sits inside a sync mirror
> or a whole git-backed vault sits inside a sync root, the cause is identical:
> high-churn git machinery + a real-time sync daemon. Shape A (above) moves the
> vault out; Shape B (below) keeps the vault synced and moves only the machinery.
> Either removes the churn from the sync scope — that is the whole policy.

### Shape B — vault synced, machinery in a local sidecar (notes on every device)

This is the mode that lets you **keep your notes in iCloud and edit them on your
iPhone**, two-way, without the storm. It relocates every churning machinery dir
OUT of the synced tree into a local sidecar and leaves only tiny static pointers
behind:

- `.git` → a real git directory outside the tree, via
  `git init --separate-git-dir`, leaving a one-line `.git` **pointer file** in
  the vault (static, safe to sync).
- `.claude/worktrees/` and the caches (`.smart-env`, `.codegraph`, graph output,
  session logs, snapshots) → relocated to the sidecar with a symlink back. The
  sync daemon follows the symlink (a few bytes), never the target's churn.

> **This relocation fixes the *sync* daemon, not the Obsidian *renderer*.** A
> cloud-sync daemon follows the symlink *file* and stays out of the churn — but
> Obsidian's own file watcher follows the symlink *back in* and indexes the
> target anyway. So the sidecar is the right move for sync, yet it does **not**
> cure the separate worktree-on-vault renderer melt (a Desktop per-session
> worktree checkout doubling the vault inside the watched tree). That one has no
> relocation fix; the only cure is to launch the vault PLAIN with the worktree
> box unchecked. See [VAULT_WORKTREE_MELT.md](VAULT_WORKTREE_MELT.md).

One command sets it up. Run it with **all Claude sessions closed and no scratch
worktrees live** — separating the git directory orphans live worktrees, so the
script refuses unless the window is clean (`--force` to override):

```bash
# preview first (changes nothing)
bash scripts/relocate-machinery-sidecar.sh "/path/to/vault" --dry-run
# do it (default sidecar: ~/.brain-sidecar; override with --sidecar or $BRAIN_SIDECAR)
bash scripts/relocate-machinery-sidecar.sh "/path/to/vault"
# fully reversible — restores a normal local repo
bash scripts/relocate-machinery-sidecar.sh "/path/to/vault" --rollback
```

**On Windows (PowerShell)** the `.ps1` parity script does the same — `.git` via
`git init --separate-git-dir` (a static pointer file) and each cache/worktree dir
as a **junction** to the sidecar:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\relocate-machinery-sidecar.ps1 "C:\path\to\vault" -DryRun
powershell -ExecutionPolicy Bypass -File scripts\relocate-machinery-sidecar.ps1 "C:\path\to\vault"
powershell -ExecutionPolicy Bypass -File scripts\relocate-machinery-sidecar.ps1 "C:\path\to\vault" -Rollback
```

(The bash `--nosync` flag is intentionally not ported: the `.nosync` suffix is an
iCloud-only convention OneDrive ignores, so on Windows the sidecar relocation is
the supported path.)

Then turn on iCloud Drive (or Desktop & Documents) for that folder. The docs
sync to every device; the machinery never leaves your Mac. Verify the calm
yourself: run a `git gc` plus a full session and watch Activity Monitor —
`fileproviderd`/`bird` stay idle.

> **Second Mac?** The `.git` pointer syncs as static text but points at *this*
> Mac's sidecar. On another Mac, re-run the helper there to stand up its own
> sidecar. On iPhone there is no git, so the pointer is harmless — you just see
> your notes.

> **`.gitignore` is not enough.** It stops you *committing* the machinery; it
> does **not** stop a cloud daemon from *syncing* it. The bytes must physically
> leave the synced tree (Shape B) or be flagged `.nosync` (the helper's
> `--nosync` mode renames caches to `<name>.nosync`, which iCloud ignores by
> name). Keep the machinery out of git too — your `.gitignore` should contain at
> least:
>
> ```gitignore
> .claude/worktrees/
> .smart-env/
> .codegraph/
> ⚙️ Meta/Worktree Snapshots/
> ⚙️ Meta/logs/
> ```

### Shape C — one vault shared across a team (notes only, no machinery anywhere)

Shapes A and B are single-user: one person, one brain. Shape C is the team case
— several people (often mixed Mac/Windows, often non-technical) collaborating in
**one** vault through a shared cloud folder: a Google Drive *shared folder*, a
Shared Drive, a shared Dropbox. Here the cloud folder is not a convenience bolted
onto a git vault; it **is** the sync, the collaboration, and the version history.
So the move is not "relocate the machinery" — it is "have no machinery in the
folder at all."

> **A shared team vault holds notes only. No `.git`, no per-session worktrees, no
> snapshot/commit automation pointed at it — ever.** The cloud folder is the
> version layer. Git on top earns both failures at once: the high-churn melt *and*
> repo corruption when two machines write `.git` at the same instant. The notes
> sync fine; nothing else belongs there.

Concretely, for a shared team vault:

- **Never run the brain's git-snapshot / session-close machinery against it.**
  Those tools assume a *local git* vault. Pointed at a cloud-mirror folder they
  find no `.git`, fail mid-run, and litter a stray log into the synced tree. Keep
  each person's machinery on *their own local* brain (Shape A or B), never on the
  shared one.
- **Expect — and ignore — editor-config conflict copies.** When two people have
  the vault open, the cloud daemon writes a `… 2` duplicate of whatever
  editor-config file both machines touched (`app 2.json`, `workspace 2.json`,
  `graph 2.json`). They are cosmetic — they never touch your actual notes. Delete
  them when they pile up. To minimize: one person "owns" the editor config
  (plugins, theme), everyone else leaves it alone, and close the editor when not
  actively in it.
- **Each teammate still gets a personal brain for deep work.** The shared vault is
  for shared *notes*. Real code, repos, and agent-driven dev work live in each
  person's *local* repos and *local* brain — never in the shared folder. Shape C
  is additive: run Shape A or B for your own brain and join the shared one too.

> **Google Drive gotcha — a shared folder is not on disk until you shortcut it.**
> When someone shares a folder with you it appears under *Shared with me* in the
> web UI but **not** as a folder the desktop app syncs to disk. Open Drive on the
> web, *Shared with me* → the folder → **Add shortcut to Drive** → place it in
> *My Drive*. Only then does the desktop app materialize it at
> `…/My Drive/<folder>` for local editing. (Dropbox and Box are similar: a share
> must be added to your own tree before the daemon syncs it.)

A cloud folder is *sync*, not backup — and a shared one that any member can
mass-delete is a single point of failure for the whole team. The folder's owner
should keep a versioned export or off-folder backup, exactly as in the section
below.

---

## What the brain does for sync safety

- **Worktree hygiene is automatic.** Each session's worktree is removed when
  the session ends; a session-start cap reclaims any that a crash left behind;
  unsaved work is snapshotted first and committed work is preserved on its
  branch. You never accumulate a pile. (See `docs/HOOKS_INSTALL.md`.)
- **Backups are a deliberate, off-machine choice** — one compressed daily
  snapshot to a destination you pick, with a restore you actually verify — not a
  side effect of a sync daemon that also happens to hold your secrets in
  plaintext. One command sets it up: `bash scripts/vault-backup.sh setup`
  (encrypted with `--encrypt`). (See `docs/BACKUP.md`.)
- **The index is server-side.** For Mycelium runtime users, the searchable
  index lives in the runtime, not in a synced local folder. Your laptop holds
  the source notes on a local disk; the heavy index never touches your
  machine's sync scope.

## The guardrails, concretely

These ship as session hooks (install via `docs/HOOKS_INSTALL.md`). They are
**non-destructive by design**: they reclaim only what is provably
reconstructible, and they *surface* (never auto-delete) anything that needs
judgment.

| When | Hook | What it does |
|---|---|---|
| SessionEnd | `remove-ended-worktree.py` | removes that session's scratch worktree (committed work stays on its branch, unsaved work is snapshotted first) |
| SessionStart | `enforce-worktree-cap.py` | caps scratch worktrees (default 12), reclaiming the oldest idle ones a crash left behind |
| SessionStart | `worktree-footprint-signal.py` | warns early on worktree count, orphan dirs, low free disk, and the dangerous vault-in-a-cloud-sync-folder combo |
| SessionStart | `remediate-runaway-procs.py` | reaps orphaned runaway processes (the `yes`-pileup class), pure waste with zero recoverable output |
| weekly cron | `scripts/worktree-prune.sh` | backstop: safe reclaim of orphan dirs + merged-branch cleanup + snapshot retention |
| on-demand / weekly | `hooks/check-sync-folder-machinery.py` | audits **every** cloud-synced root (iCloud Drive, iCloud Desktop & Documents, OneDrive, Dropbox, Box, Google Drive) for machinery — `.git`, `node_modules`, build dirs, any 5k+-file dir — *anywhere on the machine*, not just the vault. Broader than the per-session `worktree-footprint-signal.py` (which only checks the vault's own location). Advisory, never blocks; `--self-test` proves it fires. |

**The non-destructive contract.** Auto-remediation fixes only reconstructible
things: a scratch worktree directory (recreatable from its branch), an orphaned
runaway process (no output to lose), git's stale worktree refs. It NEVER
auto-deletes the judgment calls (unpushed commits, stashes, or a directory
whose git metadata it cannot reason about); those are *surfaced* for you to
decide. The discriminator for worktree removal is **location**
(`.claude/worktrees/` scratch), not branch name: a deliberate
`~/dev/<repo>-<slug>` sibling worktree is never touched, even when idle and on a
`claude/*` branch.

**Force a reclaim now** (snapshot-first; classifies each dir, never
blind-deletes; a dir with genuinely-unsaved work or dangling git metadata is
kept and reported):

```bash
# preview what would be reclaimed
python3 ~/.claude/skills/ai-brain-starter/scripts/worktree-reclaim.py --dry-run
# do it
python3 ~/.claude/skills/ai-brain-starter/scripts/worktree-reclaim.py
```

**Don't write secrets into notes.** A live API key in a note gets committed,
synced, and indexed. The `block-secret-in-note.py` write guard refuses a
Write/Edit that would put a high-confidence credential (AWS / GitHub PAT /
provider keys / database-URL passwords) into a `.md`/`.txt` note. Store it in
the keychain or a gitignored secrets file and reference it by name instead.

## Already melting? Rebuild the sync DB

If a sync daemon is *already* pinned — on macOS, iCloud's `fileproviderd` and
`bird` at 70-130% CPU for hours, a Finder that beachballs, files that won't
download — the damage is usually a **corrupted local sync database**: it
references hundreds of thousands of items that no longer exist and retries them
forever. Removing the churn (above) stops it getting *worse*; it does **not**
drain a backlog that already exists. You have to rebuild the database.

Do it in this order, and **measure before and after each step — don't guess.**
Stop at whichever step drops the CPU.

**0. Confirm the diagnosis** (macOS):

```bash
# how many phantom entries is the File Provider DB retrying?
fileproviderctl dump 2>/dev/null | grep -c itemNotFound
# is fileproviderd actually pegged? (watch several samples, not one)
top -l 4 -s 12 -o cpu -stats command,cpu | grep -E 'fileproviderd|bird'
```

A six-figure `itemNotFound` count plus sustained high CPU is the signature.

**1. Remove the cause first.** Run the machinery audit and move anything it flags
out of every sync folder. A rebuild only stays clean if nothing is still feeding
it:

```bash
python3 ~/.claude/skills/ai-brain-starter/hooks/check-sync-folder-machinery.py
```

**2. Try the in-place repair (non-destructive).** macOS ships a consistency
checker/repairer for the File Provider DB:

```bash
fileproviderctl check  -P -o /tmp/fpck-check.txt    # read-only: see the breakage
fileproviderctl repair -P -o /tmp/fpck-repair.txt   # attempt an in-place reconcile
```

Re-measure. If the `itemNotFound` count and CPU drop, you're done. If `repair`
finishes with `FPCKDomain Code=65` and the counts **don't** move, the domain is
too corrupted for in-place repair — go to step 3.

**3. Rebuild from the server (the supported reset).** System Settings → your
Apple Account → iCloud → **iCloud Drive** → turn it **off**, choosing **"Keep a
Copy"** of files on this Mac. Reboot. Turn iCloud Drive back **on**. Leave
"Desktop & Documents folders" off *unless* you have set up Shape B above (the
machinery sidecar) — once the machinery is out of the synced tree, re-enabling
D&D is safe. This discards the corrupt local DB and re-fetches a clean one from
Apple's servers.

- Expect a CPU spike and a long re-sync afterward — that part is normal; let it
  run (hours, on a large drive).
- **Back up first** (next section). Your local files are kept, but a tested backup
  is the only safe way into an irreversible iCloud operation.

> Heads-up: some users report Calendar/Contacts duplication after toggling iCloud,
> and especially after signing out of the **whole** Apple Account. Toggle **only
> iCloud Drive** — it's the narrower, safer move — and avoid a full account
> sign-out unless step 3 alone doesn't take.

Verify success the way you diagnosed it: the `itemNotFound` count collapses and
`fileproviderd` settles to near-zero at idle.

## Order matters: back up before you move

Relocating the vault out of a sync folder is the right fix, but the vault is
often the one irreplaceable asset, so **stand up an off-machine backup and pull a
real restore from it FIRST, then relocate.** A backup you have never restored is
a hope, not a backup. One command gets you protected immediately —
`bash scripts/vault-backup.sh setup` (add `--encrypt` for a sensitive vault),
then `bash scripts/vault-backup.sh verify` to prove the restore. Full guide,
including the restic option for an offsite tier: `docs/BACKUP.md`.

A brain that melts the machine it runs on isn't a brain you'll keep. Whichever
shape you pick — fully local, synced with the machinery in a sidecar, or shared
across a team with no machinery at all — the invariant is the same: notes can
sync, machinery never does, the index lives server-side, and the backup is
encrypted-and-verified. That's the whole policy.
