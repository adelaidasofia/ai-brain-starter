---
creationDate: {{DATE}}
type: rule
purpose: Session close protocol — save what the session produced to the vault, then hand off to the automatic hooks. Run before goodbye or context compaction.
trigger: User signals session end OR context compaction imminent
supersedes: session-end-cascade.md (deprecated; this file is the canonical source)
---

# Session close protocol

**What a close is for: nothing the session produced gets lost.** What the user learned, decided, felt, and committed to gets written to their vault — their second brain — so the next session, and the next month, build on it instead of starting cold. Everything else in this file is plumbing that serves that one goal.

<!-- NONDEV-REGISTER:START — canonical block. Byte-identical in every file listed in scripts/check-nondev-register-sync.py. Edit one copy, run that script, propagate. -->
## Plain-language register — NON-NEGOTIABLE

Most people running this vault are not developers. They journal, plan, think, run a business. Governs every phase, every skill, every session — never close-only.

1. **Never narrate machinery.** No "git snapshot", "Bash task", "mutex", "worktree", "another session running in parallel", no raw revision id. Machinery runs automatically and silently. Maintainer reference, never something the user reads or hears.
2. **Never end a turn on a technical either/or.** Commit or not, push or not, which merge strategy, keep or delete a branch, save these files or leave them — not theirs to answer blind. Do the safe thing and say what you did in one plain sentence, or ask something anyone could answer without knowing what git is.
3. **Never make them paste a credential.** No API key into a shell config, no dotfile edit, no terminal. Set it for them, or route the feature through a path that needs no key. A key pasted into a dotfile is a key they cannot find, rotate, or remove.
<!-- NONDEV-REGISTER:END -->

The git/resource detail in the final step of this protocol runs automatically and silently; it is maintainer reference, not something the user reads or hears.

**You don't need to read this file when the cascade fires** — `detect-closing-signal.py` injects all paths and instructions into your context automatically. This file is documentation, debugging, and the rare manual run. Full architecture + internals: [docs/SESSION_CLOSE.md](../../docs/SESSION_CLOSE.md).

## How it runs (three layers, automatic)

| Layer | Mechanism | Does |
|---|---|---|
| 1 | `detect-closing-signal.py` (UserPromptSubmit) | Detect the goodbye; resolve paths; pre-build the session-file shell; inject everything you need |
| 2 | Your turn | Surface unfinished work, scan the conversation, write captures to the vault in one batched block |
| 3 | `session-end-hook.sh` (Stop) | Backstop + aggregate + (only if the vault is backed up by git) snapshot — automatic, silent |

**Phase numbers match the injected cascade.** The phase names and numbers below are the same ones `detect-closing-signal.py` puts in your context at close, so the two can be read together. Numbering that drifts between the two is a bug in whichever one moved: they describe one process, and a number that means different things in each is worse than no number.

**Skip condition.** Fewer than 5 user messages AND no captures = trivial. Say a warm goodbye, skip the protocol. The hook still logs a timestamp.

**Closing signals + custom signals.** EN/ES/PT packs at `templates/closing-signals/*.json`; explicit slash forms (`/close`, `/bye`, `/cerrar`, `/tchau`); false-positive guards (quoted "bye", "done with X" transitions, meta-questions). Extend via `closingSignals.custom: [...]` in CLAUDE.md.

## Phase 0b — Unfinished business (you, FIRST)

Before writing anything, surface — in PLAIN language — anything the user started this session but didn't finish: a draft half-written, a decision they were weighing, a task they meant to do, a document still open. For each: "Want to finish this now, or leave it for next time?" Wait for their call before continuing.

*Coding sessions only:* if and only if this was a build session, also surface background jobs still running, anything killed mid-run, or a pipeline phase that errored. Skip this entirely for non-dev sessions — they have none of these.

If nothing is unfinished: say so and continue.

## Phase 1 — Scan the conversation (you, one pass)

One read of the conversation. Compose everything in memory, then write in Phase 2. **Lead with what compounds the user's thinking; the optional buckets come last and only when they apply.**

**Belief shifts.** Does the user end believing something different than they started? That is the most valuable capture — the first journal seed.

**Journal seeds.** VERBATIM quotes where the user revealed a belief, an observation, a change of mind. Tag emotional ones `[emotional]`. Never reword. → `Session Captures.md`.

**Decisions.** New decisions: one file per decision in `Decisions/{timestamp}-{slug}.md` with frontmatter (`type: decision`, date, stakes, outcome placeholder). Capture the REASONING, not just the outcome. Backfill the Outcome on any prior decision (the hook pre-lists the open ones) this session resolved.

**To-dos.** File to the user's canonical to-do destination(s). Self-contained rule: every task carries a `[context prefix]` OR wikilink OR URL OR file path so it stands alone out of session. Reconcile: check off (`- [x]`) anything completed this session, matched by substance.

**Learnings — what to do better.** What did this session teach about how the user works, or how their system should work? A cleaner way to do a recurring thing, a friction point worth removing, a process that should change, an optimization worth applying. Capture it to the vault (Captures under "Learnings", or the user's improvements file if one is configured) so the brain compounds instead of just logging. **If an optimization is safe to apply right now, apply it — don't just note it.**

**Delegations.** Items for other people: add to the team to-do with `@Name`, and draft the message the user sends in one click.

**Writing notes** *(only if a Substack/blog is configured in CLAUDE.md).* Kill conditions before drafting: no "I + happened-today" diary, no LinkedIn-thought-leader tone, no ego framing; must read as a universal observation that stands alone. Bilingual setup → draft both languages. → Content Drafts file or Captures.

**Time tracking** *(only if the vault uses it).* `- HH:MM - HH:MM | Category | Brief`. Verify start < end.

**GitHub issues** *(only if this was a coding session that filed any).* Log to `Open GitHub Issues.md`. Skip otherwise.

## Phase 2 — Write to the vault (you, one batched block)

All edits in parallel. No read-write ping-pong. No tool-call narration.

**Session file.** Fill the pre-built shell the hook created (headers already there — fill the bodies). Capture commitments in the user's exact words.

**Decision files.** Create at the pre-resolved `Decisions/` path with frontmatter.

**Append, never overwrite.** Wikilink people, projects, concepts. Leave enough context to make sense in 6 months.

**Vault firewall.** Personal content → personal vault. Team/business → team vault. Ambiguous → personal. Never leak personal content into a shared vault.

## Phase 2b — Commit what you wrote (you, before the goodbye)

Save the files this close produced: the session file, any decision files, the captures, plus anything else you touched. Use the vault's safe-commit wrapper with **explicit paths** — never stage the whole tree, which on a large vault is slow and sweeps up work that is not yours.

```bash
bash "<vault>/scripts/vault-safe-commit.sh" "session: <slug> — <summary>" "<path>" "<path>"
```

This is yours to do, not the Stop hook's. `verify-session-close-cascade.py` runs BEFORE the Stop hook and blocks the close while session-close artifacts are still uncommitted, so waiting for the automatic snapshot in the final step costs you a hard block and a forced re-run.

*Skip if the vault is not git-tracked* — the captures are already on disk.

## Phase 3 — Functional audit (you, code sessions only)

Only when the session shipped code or docs to a repo other people install. Syntax-check what changed, resolve every path referenced in docs, smoke-test new scripts, and reread shipped copy for claims that overstate what is actually wired. Non-dev sessions have none of this: skip it and say nothing.

## Phase 4 — Goodbye (you, one line)

Tell the user plainly what you saved: "Saved to your vault: N decisions, M to-dos, the belief shift about Y. Anything I missed?" Then a warm goodbye in their language. No machinery, no phase names, no file paths unless they ask.

**If this session set a `/goal`, add one line: ask them to type `/goal clear`.** `/goal` installs a session-scoped Stop hook that blocks stopping until its condition holds, so at a deliberate close it blocks the close and re-invokes you with nothing left to do. This is the one part of the goodbye that is **not** automatic and cannot be: `/goal` is a client-side command with no tool behind it, so only the user can run it. You do not have to go looking — when the detector sees a `/goal` that was never cleared, it adds the reminder (and quotes the condition) to the close instructions it injects.

**The one exception:** a goal whose condition was actually met auto-cleared already. Say nothing then. Telling someone to clear a goal that succeeded is noise.

## After your turn — Automatic finalization (the Stop hook, you do nothing)

Runs after your turn with zero involvement from you, and silently from the user's view:

1. **Backstop.** If the session file is still empty (you bailed), a Haiku fallback fills it from the transcript (needs `ANTHROPIC_API_KEY`; without it, leaves a recovery flag for next time).
2. **Aggregate.** Rebuilds Last Session.md + Decision Log.md.
3. **Backup snapshot.** If — and only if — the vault is backed up by git, the hook saves a snapshot of just the files this close touched. The promise to the user is plain: *their work is saved automatically.* The mechanics (explicit-path staging, worktree safety, resource-gating on large vaults) live in [docs/SESSION_CLOSE.md](../../docs/SESSION_CLOSE.md) — not something the user or you think about. A vault that is not git-tracked simply skips this; the captures are already on disk.
4. **Cleanup.** Retention sweep + marker removal.

**Nothing is lost, ever.** The session file is on disk before this phase runs. If the snapshot is deferred (busy machine) the daily maintenance job commits it within a day. Heavy hygiene (drift scans, full-tree walks) never runs on the close path — it lives in that daily job.

## Recovery, manual run, config, opt-out

- **Recover a bailed close:** `python3 scripts/recover-last-close.py` (`--list` to see flags). **Undo:** `python3 scripts/undo-last-close.py` (interactive).
- **Trigger manually:** type `/close`, `/bye`, `/done`, `/cerrar`, `/tchau`, etc. — detector keywords, not registered commands.
- **Config (CLAUDE.md frontmatter):** `closingSignals.custom: [...]`, `closeDetection: regex|hybrid`, `sessionCloseFeedback: silent|minimal|verbose` (default silent).
- **Skip / off:** trivial sessions auto-skip; `export CLOSING_SIGNAL_DETECTION=off` for one shell; remove the hook entries from `hooks.json` to uninstall.
- **Errors** fail open (never block the user), logged to `~/.claude/logs/session-close-errors.log`. Full internals, schema, resource-gating, worktree invariant: [docs/SESSION_CLOSE.md](../../docs/SESSION_CLOSE.md).

## Why this rule exists

The job is simple and human: when a session ends, what the user thought, decided, and committed to is saved to their second brain — automatically, in plain language, with nothing lost. Earlier versions buried that behind developer machinery (git internals, background-task checks, issue logging) that most users — who journal and plan, not code — never needed to see. This version leads with the capture and keeps the plumbing backstage. Detection is deterministic (Layer 1), the capture is yours (Phases 0b-3), and a Haiku backstop (Layer 3) guarantees no silent loss even if you bail.
