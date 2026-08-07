## Phase 5: Build the Context Layer

"Now I'm creating three small notes that let me orient myself in 10 seconds every session."

> **Platform note — read before running any shell command in this phase.** The
> file and folder operations below are written as Mac/Linux bash (`cp`, `mkdir -p`,
> `chmod`). **On Windows those error** — `cp`/`chmod` don't exist and `mkdir -p`
> isn't valid — and the vault silently ends up missing `⚙️ Meta/scripts`,
> `⚙️ Meta/Sessions`, `⚙️ Meta/Decisions`, and the hook scripts. So on Windows,
> perform the SAME operations with your native file tools instead: create each
> folder, and write/copy each file, directly (no bash). The `.sh` scripts are
> still written to disk — they run under Git Bash/WSL if present — and `chmod +x`
> is a Mac/Linux-only no-op you skip on Windows. The end state must be identical
> on every OS: the folders exist and every file is in place. Verify with a
> directory listing before moving on.

Create these files in the Meta/ folder:

**00 Start Here.md:**
```markdown
---
creationDate: [today]
type: meta
---
# Start Here

Read these in order at the start of every session:
1. [[CLAUDE]] — who I am, how to behave
2. [[Current Priorities]] — what matters right now
3. [[Open Loops]] — what's unresolved
4. [[Last Session]] — what happened last time
```

**Current Priorities.md** — Ask them: "What are your top 5 priorities right now? Across work, life, everything." Build the note from their answer with headlines and bullet points.

**Open Loops.md** — Ask them: "What are you waiting on from other people? What do you need to do but haven't? What decisions are you sitting on?" Organize into three sections: Waiting On Others, Needs Action, Decisions Pending.

**⚙️ Meta/topic-map.json** — Copy `templates/topic-map.json` from this repo to `⚙️ Meta/topic-map.json` in their vault, then personalize it with them.

This file is what makes the vault-context hook routes *their* important files (not just generic examples) into context when they ask about the topics that matter to them. Without it, the hook only injects Current Priorities and Open Loops.

Ask them: "When you ask Claude about certain topics, which vault files should auto-load? For example, when you say 'raise' or 'investor,' we can pull your raise dashboard. When you say 'client' or 'pipeline,' we can pull your sales tracker."

For each of their top 4-6 focus areas, capture:
- A short name (e.g. `fundraising`, `writing`, `sales-pipeline`)
- 4-8 trigger keywords they'd actually type
- The 1-3 vault files that matter most for that topic

Replace the example entries in `topic-map.json` with their answers. Remove anything they don't need. If they want to redefine what counts as a "strategic" question, edit the `_signals` array at the top — otherwise leave the defaults.

**Last Session.md:**
```markdown
---
creationDate: [today]
type: meta
---
# Last Session

## [today's date] — Initial Setup
- Created vault structure
- Built CLAUDE.md
- Set up context layer
- [add what else was done]

## Still Pending
- [anything not finished]
```

### Link your memory into the vault (so your brain actually lives in your vault)

Claude Code keeps its persistent memory (what it learns about you) in a hidden, per-machine tool folder at `~/.claude/projects/<your-vault>/memory/`. For a second brain, that is the wrong place: it is invisible in Obsidian, it is not in your vault's history, and it does not follow you to another machine or tool. This step makes that memory physically live inside your vault, at `⚙️ Meta/Agent Memory/`, so everything Claude remembers shows up in your notes.

Run this once (idempotent and loss-free — any existing memory is migrated into the vault, the old folder is backed up, never deleted):

```bash
python3 ~/.claude/skills/ai-brain-starter/scripts/link-agent-memory.py --vault "[VAULT_PATH]"
```

Then tell the user plainly: "Done — from now on, everything I remember about you is saved inside your vault (you'll see it in `⚙️ Meta/Agent Memory/`), not hidden in a system folder." If the command prints a refusal (an existing symlink points somewhere else), surface that to the user rather than forcing it.

### Install the Session Protocol Hook

"One more critical thing — I'm going to install a hook that makes sure I always read your files before responding. Without this, I might greet you before loading context. With it, every session starts with full context automatically."

Check if `.claude/settings.local.json` exists in the vault. If it does, merge the hook into the existing file. If not, create it. Add this hook:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "echo '{\"hookSpecificOutput\":{\"hookEventName\":\"UserPromptSubmit\",\"additionalContext\":\"MANDATORY SESSION PROTOCOL: Before responding to the user, you MUST first read these files in order: 1) The project CLAUDE.md at the vault root 2) ⚙️ Meta/Last Session.md 3) ⚙️ Meta/Current Priorities.md — Do NOT greet the user or respond until all three files have been read. This is non-negotiable.\"}}'",
            "once": true,
            "statusMessage": "Loading session context..."
          }
        ]
      }
    ]
  }
}
```

Also add an auto-update hook that checks GitHub for updates at most once per week (gated by a timestamp file at `~/.claude/.ai-brain-starter-last-update`). Create or update `.claude/settings.local.json` to include a second hook:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "echo '{\"hookSpecificOutput\":{\"hookEventName\":\"UserPromptSubmit\",\"additionalContext\":\"MANDATORY SESSION PROTOCOL: Before responding to the user, you MUST first read these files in order: 1) The project CLAUDE.md at the vault root 2) ⚙️ Meta/Last Session.md 3) ⚙️ Meta/Current Priorities.md — Do NOT greet the user or respond until all three files have been read. This is non-negotiable.\"}}'",
            "once": true,
            "statusMessage": "Loading session context..."
          },
          {
            "type": "command",
            "command": "LAST=~/.claude/.ai-brain-starter-last-update; if [ ! -f \"$LAST\" ] || [ -n \"$(find \"$LAST\" -mtime +6 2>/dev/null)\" ]; then touch \"$LAST\" && cd ~/.claude/skills/ai-brain-starter 2>/dev/null && git fetch origin main --quiet 2>/dev/null && if [ \"$(git rev-parse HEAD 2>/dev/null)\" != \"$(git rev-parse origin/main 2>/dev/null)\" ]; then git pull --quiet origin main 2>/dev/null && CHANGES=$(git log --oneline HEAD@{1}..HEAD 2>/dev/null) && SYNC_OUTPUT=$(bash ~/.claude/skills/ai-brain-starter/scripts/sync-skills.sh 2>&1) && echo \"{\\\"hookSpecificOutput\\\":{\\\"hookEventName\\\":\\\"UserPromptSubmit\\\",\\\"additionalContext\\\":\\\"AI Brain Starter was auto-updated. Commits: $CHANGES. Skill sync result: $SYNC_OUTPUT. Any file that changed was backed up to <file>.bak-YYYY-MM-DD-HHMM before being overwritten — preserving local customizations. Now: 1) Read docs/CHANGELOG.md and tell the user in 1-2 plain sentences what changed and why. 2) If the sync output lists backed-up files, mention it casually so they know their customizations are recoverable. 3) Check if hooks.json differs from .claude/settings.local.json — if so, update settings.local.json to match. Keep it casual, not a changelog dump.\\\"}}\"; else echo '{\"continue\":true,\"suppressOutput\":true}'; fi; else echo '{\"continue\":true,\"suppressOutput\":true}'; fi",
            "once": true,
            "statusMessage": "Checking for skill updates..."
          }
        ]
      }
    ]
  }
}
```

Also add the **vault-context hook** — this one actually reads and injects file contents before responding to strategic questions, instead of just instructing Claude to read them. The difference matters: an instruction can be skipped or deferred; injected content is always there.

Copy the hook file:
```bash
cp ~/.claude/skills/ai-brain-starter/hooks/vault-context.py ~/.claude/hooks/vault-context.py
```

Then add it as an additional UserPromptSubmit hook in `.claude/settings.local.json`:
```json
{
  "type": "command",
  "command": "python3 ~/.claude/hooks/vault-context.py",
  "statusMessage": "Loading vault context..."
}
```

The hook auto-detects the vault root by walking up from the working directory. It fires only when the prompt contains strategic keywords (plan, priorities, decision, strategy, revenue, client, product, etc.) and injects `⚙️ Meta/Current Priorities.md` and `⚙️ Meta/Open Loops.md` as `additionalContext`. Silent on trivial queries.

To add your own project-specific files, edit `~/.claude/hooks/vault-context.py` and uncomment the `TOPIC_MAP` example entries — each entry maps a list of keywords to a list of vault-relative file paths.

### Additional PreToolUse hooks

The starter ships five PreToolUse hooks under `hooks/`. Two install by default (they help every user with no behavior-change cost). Three are conditional — install only if the specific risk applies.

**Install by default — run these two `cp` commands now:**

```bash
mkdir -p ~/.claude/hooks
cp ~/.claude/skills/ai-brain-starter/hooks/retry-budget.py ~/.claude/hooks/
cp ~/.claude/skills/ai-brain-starter/hooks/validate-mcp-json.py ~/.claude/hooks/
```

| Hook | What it does | Why it's default |
|---|---|---|
| `retry-budget.py` | Blocks the 4th identical Bash command within 30 minutes | Caps Claude's tendency to loop on failing commands and burn context. Bypass: `RETRY_BUDGET_BYPASS=1 <cmd>` |
| `validate-mcp-json.py` | Blocks Write/Edit on `.mcp.json` if the result fails JSON parsing | Claude Code silently drops malformed `.mcp.json`, disabling every registered MCP. Catches a single misplaced brace before it takes the stack dark. |

Both are already wired into `hooks.json` with file-exists guards — if the user removes the file, the hook chain stays clean (protection just goes away, no errors). To uninstall either: `rm ~/.claude/hooks/<name>.py`.

**Conditional opt-ins — install only if the risk applies (read each file first):**

| Hook | What it blocks | Install when |
|---|---|---|
| `block-raw-vault-git.py` | Raw `git add/commit/checkout/reset/merge/rebase` inside a git-tracked vault | Your vault has `.git/` and you run multiple Claude sessions against it (prevents cross-session lock races) |
| `block-vault-git-fullwalk.py` | Unscoped `git add -A`, `git add .`, full-tree `git status` | Same vaults with >10K files — prevents 10+ minute walks and token burn |
| `permission-denied.py` | (Hook event handler, informational) | Improves error surfacing on permission denials |

```bash
# Install the conditional ones that apply to your setup:
cp ~/.claude/skills/ai-brain-starter/hooks/block-raw-vault-git.py ~/.claude/hooks/
cp ~/.claude/skills/ai-brain-starter/hooks/block-vault-git-fullwalk.py ~/.claude/hooks/
cp ~/.claude/skills/ai-brain-starter/hooks/permission-denied.py ~/.claude/hooks/
```

The two git-guard hooks read their vault path from the `VAULT_ROOT` environment variable; if unset, they no-op safely. Set it in `~/.zshrc` or `~/.claude/settings.json` pointing at your vault root. Emergency bypass for blocked git commands: `GIT_VAULT_BYPASS=1 git ...`.

Tell them: "Done. From now on, the first thing I do every session is read your files — automatically, before I say anything. And whenever you ask something strategic, I'll pull your current priorities and open items into context before I respond. If there's an update to the skill, I'll pull it and apply it automatically — you'll just see a quick note about what changed."

Also create the **session-end-hook.sh** script. This script writes a per-worktree session stub (never to the shared `Last Session.md`) and then runs the aggregator. This design is race-safe against concurrent worktrees — see the "Why per-worktree writes" note below the script for the full explanation.

```bash
#!/bin/bash
# Save to: [VAULT_PATH]/⚙️ Meta/scripts/session-end-hook.sh
# chmod +x this file after creating it
#
# NO STUBS: Claude writes session files directly during session close
# (see ⚙️ Meta/rules/session-end-cascade.md Phase 2). This hook only:
#   1. Appends a timestamp to Session Log
#   2. Cleans up: deletes stubs >7d old, archives substantive files >7d old
#   3. Runs the aggregator to refresh Last Session.md
#   4. Emits the session-close prompt for Claude
#
# PER-WORKTREE META WRITES: each session gets its own file in
# ⚙️ Meta/Sessions/ named by timestamp + worktree. The aggregator
# rebuilds Last Session.md deterministically — concurrent runs are safe
# (same sorted input → same bytes). See issue #5.
#
# Prior versions wrote a "stub" file every hook invocation, expecting
# Claude to fill it in. In practice most sessions end without running
# the full protocol, and stubs piled up (one user had 966 of 1,046
# files as empty stubs). This version trusts Claude to write the real
# file during session close and never creates stubs.

VAULT="[VAULT_PATH]"
META_DIR="$VAULT/⚙️ Meta"
SESSIONS_DIR="$META_DIR/Sessions"
ARCHIVE_DIR="$SESSIONS_DIR/Archive"
SESSION_LOG="$META_DIR/Session Log.md"
ERROR_LOG="$META_DIR/hook-errors.log"
AGGREGATE_SESSIONS="$META_DIR/scripts/aggregate-sessions.py"
DATE=$(date +%Y-%m-%d)
TIME=$(date +%H:%M)
TIMESTAMP_FILE=$(date +%Y-%m-%dT%H-%M)

# Cutoff for retention (7 days back). BSD date (macOS) uses -v, GNU uses -d.
if date -v-7d +%Y-%m-%d >/dev/null 2>&1; then
  CUTOFF=$(date -v-7d +%Y-%m-%d)
else
  CUTOFF=$(date -d '7 days ago' +%Y-%m-%d)
fi

# GUARD: fail loudly, never silently. If the Meta dir doesn't exist, bubble an error
# into the Claude hook context so the user sees it. This honors the NEVER fail silently rule.
if [ ! -d "$META_DIR" ]; then
  MSG="session-end-hook: Meta directory not found at '$META_DIR'. Vault may use a different folder name than '⚙️ Meta' — update this script's META_DIR. No session context saved."
  mkdir -p "$VAULT" 2>/dev/null && echo "$DATE $TIME — $MSG" >> "$VAULT/hook-errors.log"
  echo "{\"continue\":true,\"stopReason\":\"session-end-error\",\"systemMessage\":\"HOOK ERROR: $MSG Tell the user immediately and help fix the path.\"}"
  exit 0
fi

# Derive worktree name. Try three methods in order:
#   1. pwd matches .../.claude/worktrees/{name}/... → use {name}
#   2. Read the .git file if we're inside a git worktree
#   3. Fall back to "main-$$" (PID) so two concurrent fallback sessions
#      never collide on the same filename
WORKTREE_NAME=""
PWD_PATH="$(pwd)"
case "$PWD_PATH" in
  *"/.claude/worktrees/"*)
    WORKTREE_NAME=$(echo "$PWD_PATH" | sed -n 's|.*/\.claude/worktrees/\([^/]*\).*|\1|p')
    ;;
esac
if [ -z "$WORKTREE_NAME" ] && [ -f "$PWD_PATH/.git" ]; then
  GITDIR=$(grep -o 'worktrees/[^ ]*' "$PWD_PATH/.git" 2>/dev/null | head -1)
  if [ -n "$GITDIR" ]; then
    WORKTREE_NAME=$(echo "$GITDIR" | sed 's|worktrees/||' | tr -d '[:space:]')
  fi
fi
[ -z "$WORKTREE_NAME" ] && WORKTREE_NAME="main-$$"

SESSION_FILE="$SESSIONS_DIR/${TIMESTAMP_FILE}-${WORKTREE_NAME}.md"

mkdir -p "$SESSIONS_DIR" 2>>"$ERROR_LOG"
mkdir -p "$ARCHIVE_DIR" 2>>"$ERROR_LOG"

# Step 1: Always write a timestamp entry to Session Log (guaranteed, no Claude involvement).
# Append-only, small writes are atomic on local filesystems so this is safe under concurrency.
if ! echo "- $DATE $TIME — session ended ($WORKTREE_NAME)" >> "$SESSION_LOG" 2>>"$ERROR_LOG"; then
  echo "{\"continue\":true,\"stopReason\":\"session-end-error\",\"systemMessage\":\"HOOK ERROR: Could not append to Session Log at '$SESSION_LOG'. Check '$ERROR_LOG' for details and tell the user.\"}"
  exit 0
fi

# Step 2: Retention cleanup — delete stubs >7d old, archive substantive >7d old.
# Runs every hook invocation but only touches files past the cutoff (fast + idempotent).
# Keeps the Sessions folder from growing unbounded while preserving the last week
# of context for /weekly reviews and the aggregator's Last Session.md rebuild.
for f in "$SESSIONS_DIR"/*.md; do
  [ -f "$f" ] || continue
  fname=$(basename "$f")
  fdate="${fname:0:10}"
  # Skip files that don't start with a date pattern
  [[ "$fdate" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || \
  [[ "$fdate" =~ ^[0-9]{8} ]] || continue
  # Normalize YYYYMMDD to YYYY-MM-DD for comparison
  if [[ "$fdate" =~ ^[0-9]{8}$ ]]; then
    fdate="${fdate:0:4}-${fdate:4:2}-${fdate:6:2}"
  fi
  # Skip if within retention window
  [[ "$fdate" > "$CUTOFF" || "$fdate" == "$CUTOFF" ]] && continue
  # Old file: delete if legacy stub, archive if substantive
  if grep -q 'session_label: "update pending"' "$f" 2>/dev/null; then
    rm "$f"
  else
    mv "$f" "$ARCHIVE_DIR/"
  fi
done

# Step 3: Run the aggregator to refresh Last Session.md from Sessions/.
# Deterministic output → safe even if another worktree's hook is running
# the same aggregator at the same moment (both write identical bytes).
if [ -f "$AGGREGATE_SESSIONS" ]; then
  VAULT_ROOT="$VAULT" python3 "$AGGREGATE_SESSIONS" >/dev/null 2>>"$ERROR_LOG" || true
fi

# Step 4: Ask Claude to run session close protocol and log any decisions.
cat <<EOF
{"continue":true,"stopReason":"session-end-cascade","systemMessage":"SESSION ENDING (${DATE} ${TIME}, worktree: ${WORKTREE_NAME}): Run session close protocol (⚙️ Meta/rules/session-end-cascade.md). Write session file to '${SESSION_FILE}' — do NOT write to Last Session.md directly (auto-generated by aggregate-sessions.py). VERBATIM RULE: for commitments made during this session, capture the EXACT words used. For any decisions, create per-decision files at '${META_DIR}/Decisions/${TIMESTAMP_FILE}-{slug}.md' with frontmatter (type: decision, worktree, decision_date). After writing, run: VAULT_ROOT='${VAULT}' python3 '${AGGREGATE_SESSIONS}' && VAULT_ROOT='${VAULT}' python3 '${META_DIR}/scripts/aggregate-decisions.py'. Also save any non-obvious technical discoveries as memory files (type: discovery)."}
EOF
```

**Why per-worktree writes (the failure mode this design prevents):** if a user runs multiple Claude Code sessions in parallel worktrees, and each session follows the session-end cascade rule to write to the shared `Last Session.md` and `Decision Log.md`, the writes will race. Each session reads the file, constructs a new version with its entry, writes it back. Last write wins. Earlier sessions' entries are silently clobbered. The per-worktree split eliminates the race: unique filenames in `Sessions/` and `Decisions/` prevent contention at the write layer, and the aggregator scripts produce deterministic output from sorted input — so even concurrent aggregator runs can clobber each other without data loss, because they write the same bytes. Reported and fixed at [mycelium-hq/ai-brain-starter#5](https://github.com/mycelium-hq/ai-brain-starter/issues/5).

**Companion scripts** (Phase 5 also installs these — see `scripts/aggregate-sessions.py` and `scripts/aggregate-decisions.py` in this repo):

```bash
# Copy the two aggregator scripts into the vault's Meta folder
cp ~/.claude/skills/ai-brain-starter/scripts/aggregate-sessions.py "[VAULT_PATH]/⚙️ Meta/scripts/"
cp ~/.claude/skills/ai-brain-starter/scripts/aggregate-decisions.py "[VAULT_PATH]/⚙️ Meta/scripts/"
chmod +x "[VAULT_PATH]/⚙️ Meta/scripts/aggregate-sessions.py" "[VAULT_PATH]/⚙️ Meta/scripts/aggregate-decisions.py"

# Create the source-of-truth folders
mkdir -p "[VAULT_PATH]/⚙️ Meta/Sessions" "[VAULT_PATH]/⚙️ Meta/Decisions"
```

Also create the **write-hook.sh** script that fires after every Write tool call. It auto-triggers meeting-todos extraction when a meeting note is saved:

```bash
#!/bin/bash
# Save to: [VAULT_PATH]/⚙️ Meta/scripts/write-hook.sh
# chmod +x this file after creating it

INPUT=$(cat)

# GUARD: if python3 is missing, fail loudly. Honors NEVER fail silently rule.
if ! command -v python3 >/dev/null 2>&1; then
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PostToolUse\",\"additionalContext\":\"HOOK ERROR: write-hook.sh needs python3 but it's not on PATH. Tell the user and help them install it.\"}}"
  exit 0
fi

FILE_PATH=$(echo "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    path = d.get('tool_input', {}).get('file_path', '')
    print(path)
except Exception as e:
    sys.stderr.write(f'write-hook.sh JSON parse error: {e}\n')
    print('')
")
PARSE_EXIT=$?

# If python parsing itself errored, surface it — don't pretend nothing happened
if [ $PARSE_EXIT -ne 0 ]; then
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PostToolUse\",\"additionalContext\":\"HOOK ERROR: write-hook.sh could not parse the tool input JSON. Check your Claude Code version and tell the user.\"}}"
  exit 0
fi

if echo "$FILE_PATH" | grep -qi "Meeting Notes/\|Meeting-Notes/"; then
  BASENAME=$(basename "$FILE_PATH" .md)
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PostToolUse\",\"additionalContext\":\"Meeting note saved: '$BASENAME'. Run /meeting-todos on this file now — extract action items, show the user a preview, and add confirmed tasks to the to-do file. Do this automatically without waiting to be asked.\"}}"
else
  echo "{}"
fi
```

Replace the Stop hook path in `.claude/settings.local.json` to point to this script:
```json
"Stop": [{"hooks": [{"type": "command", "command": "bash '[VAULT_PATH]/⚙️ Meta/scripts/session-end-hook.sh'", "statusMessage": "Saving session context..."}]}]
```

And add the PostToolUse hook:
```json
"PostToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "bash '[VAULT_PATH]/⚙️ Meta/scripts/write-hook.sh'", "statusMessage": "Checking write triggers..."}]}]
```

After creating both scripts, run: `chmod +x "[VAULT_PATH]/⚙️ Meta/scripts/session-end-hook.sh" "[VAULT_PATH]/⚙️ Meta/scripts/write-hook.sh"`

**Note:** If the user was already set up with `originals-hook.sh`, migrate by copying its contents into `write-hook.sh` and updating the hook path in `.claude/settings.local.json`.

### Context-Routing Hooks

Install everything below as written. These are the hooks that fire on every prompt to route the right graph context and panel voices into the conversation — the connective tissue that makes the vault feel like a second brain instead of a notes folder.

**graph-context-hook.sh (install if the user has graphify installed).**

If the vault uses `/graphify` to build a knowledge graph, install the **graph-context-hook.sh** companion. It's a `UserPromptSubmit` hook that fires on every prompt, regex-matches the prompt against routing keywords, and (on match) injects `additionalContext` pointing the assistant at the right `GRAPH_REPORT.md` with a freshness note. Silent passthrough on no match.

Why: telling Claude in CLAUDE.md to "always read the graph first" works some of the time. Injecting a routing reminder AT the moment of the matching prompt — with the file's mtime so staleness is visible — is more reliable, especially in long sessions where the static reminder fires only once.

Copy `scripts/graph-context-hook.sh` from this repo into `[VAULT_PATH]/⚙️ Meta/scripts/`, then **edit the CONFIG block at the top of the file**: set `VAULT_ROOT`, set `PRIMARY_GRAPH` and `PRIMARY_PATTERN` (regex of keywords for the main graph), and either configure `SECONDARY_GRAPH`/`SECONDARY_PATTERN` for a sub-folder graph (e.g. a separate work/team graph) or set `SECONDARY_GRAPH=""` if you only have one. Test with:

```bash
echo '{"hook_event_name":"UserPromptSubmit","prompt":"<your test phrase>"}' | bash "[VAULT_PATH]/⚙️ Meta/scripts/graph-context-hook.sh"
```

A matching prompt should print a `hookSpecificOutput` JSON; a non-matching prompt should print `{"continue":true}`. Then register it as a second `UserPromptSubmit` hook entry alongside the static MANDATORY SESSION PROTOCOL hook (see `hooks.json` for the entry shape).

**Design rule:** the hook does NOT pin specific god-node names in its message text. God-node names go stale every graphify run. The stable signal is the path + freshness date — let the model open the report to see the actual current top nodes. If you need a hand-curated snapshot, put it in CLAUDE.md (with an "as of YYYY-MM-DD" tag), not in the hook.

The full hook template (UserPromptSubmit + Stop + PreCompact + PostToolUse) is in `hooks.json` at the repo root. After any `git pull`, compare it to your `.claude/settings.local.json` to see if hooks have been updated.

**Hook performance note for large vaults (5,000+ files):** PostToolUse hooks fire on every tool call. In code repos this is fine. In large Obsidian vaults (5,000+ files), a PostToolUse hook that scans files or runs scripts can become overwhelming, firing hundreds of times in a session. If you notice slowdowns or excessive hook output, consider moving the hook logic to a cron-based approach (check every N minutes) instead of per-tool-call. The Write-matcher pattern above is scoped narrowly (only fires on Write, not Read/Grep/etc.) which keeps it manageable.

**Sync philosophy for auto-updates:** when auto-updating files (skills, scripts, templates), always back up the existing file before overwriting. Never skip an update because the user might have customized the file. The right pattern is: copy the existing file to `<name>.bak-YYYY-MM-DD-HHMM`, then overwrite with the new version. This way the update always lands AND local customizations are recoverable from the backup. Missing an update is invisible; a backup is always recoverable.

**Decision Log.md:**
```markdown
---
creationDate: [today]
type: meta
---
# Decision Log

| Date | Decision | Why | Outcome |
|------|----------|-----|---------|
| [today] | Set up AI-powered vault | Want a connected second brain | In progress |
```

**Vault Changelog.md:**
```markdown
---
creationDate: [today]
type: meta
---
# Vault Changelog

*Everything we've built, improved, or automated — in order. Check here before building something new.*

## [today's date] — Initial Setup
- Created vault structure with [X] folders
- Built CLAUDE.md with personal context
- Set up context layer (priorities, open loops, session tracking)
- Installed session protocol hook
- **Impact:** AI orients itself in 10 seconds instead of 15 minutes
```

**Content Drafts.md** (for auto-capture of sharp insights during conversations):
```markdown
---
creationDate: [today]
type: meta
---
# Content Drafts

*Sharp insights, standalone observations, and ideas that surface during conversations. Batch-captured at end of sessions.*

## Ready to Use
```

**Idea Quarantine.md** (only create if the user has a business/project):
```markdown
---
creationDate: [today]
type: meta
---
# Idea Quarantine

*New ideas go here to cool off before getting attention. Main project first. Ideas are welcome — but they go in quarantine, not into action.*

## Ideas
```
