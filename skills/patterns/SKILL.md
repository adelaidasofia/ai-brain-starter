---
name: patterns
description: Use when the user says /patterns or mentions the Instinct Engine, says "I keep noticing" or "this keeps coming up", after a weekly review, when a pattern is hardening, or at session end after repeated corrections, 5+ tool-call friction on a routine task, dead-end backtracking, or an undocumented discovery. Also as a silent pre-flight before fix/debug/build/refactor on a named surface. Not for weekly/monthly journal reviews, daily journaling, or one-off decisions.
trigger: /patterns
---

# /patterns — Instinct Engine

> **`{SKILL_DIR}`** = this skill's own folder (locally: the directory this SKILL.md lives in; a served brain substitutes the real absolute path before you read this). Shared starter files live at the repo root two levels up: `{SKILL_DIR}/../..`. If a path does not resolve, name the missing file and stop — never guess another location.

You are extracting signal before it evaporates. This skill scans recent sessions and surfaces what's hardening into real insight, then proposes concrete captures.

Run this after a weekly review, after a heavy journaling session, or whenever the user says "I keep noticing..." or "this keeps coming up."

**Headless / auto mode:** If you are running in a cron job, background script, or `--print` session with no interactive user, skip Step 3's confirmation. Auto-capture all findings. Add `(auto-captured — review and edit)` as a note in any new files created. Proceed directly to Step 4 → Step 5.

---

## First-run config

On first invocation, look for `[VAULT]/.patterns-prefs.md`. If it doesn't exist, ask:

1. **Last-session file path** (where the most recent session summary lives, e.g. `Meta/Last Session.md`, or `none`)
2. **Decision log path** (e.g. `Meta/Decision Log.md`, or `none`)
3. **Journal folder** (e.g. `Journal/`, or `none`)
4. **Drafts folder** (where writing seeds get created, e.g. `Writing/Drafts/`)
5. **Concept folder** (where new concept notes go, e.g. `Notes/Concepts/`)
6. **Changelog file** (where pattern runs get logged, e.g. `Vault Changelog.md`, or `none`)
7. **CLAUDE.md path** (where rules get written, default `CLAUDE.md` at vault root)

Save preferences. Don't ask again.

---

## Pre-flight: institutional-knowledge check

**Use BEFORE starting a non-trivial feature, bug-diagnosis, or refactor — not after.** This is the proactive sibling of the reactive auto-detection below. Pattern source: `EveryInc/compound-engineering-plugin` `ce-learnings-researcher` (MIT, reimplemented clean).

**Trigger surface:** any session that opens with verbs like "fix", "debug", "diagnose", "implement", "build", "refactor", "extract", "consolidate" plus a named surface (a service, an MCP server, a runbook, a hook) — work where a similar problem might already have been solved.

**What to do (silently, before the user's first task):**
1. Search the prior-work sources from your prefs — the decision log, any `feedback_*` / memory files, prior `/patterns` captures, and a failure-inventory note if one exists (last ~60 days) — for keywords matching the surface the user just named.
2. If 1+ match: surface a single 1-2 line nudge before the user starts — *"There's prior work on [X]: [link or path]. Worth reading before you start?"*
3. If nothing found: stay silent. Pre-flight is value when it surfaces something, dead weight when it doesn't.

**Why this matters:** the reactive auto-detection (below) catches drift AFTER the work is done. The pre-flight check catches it BEFORE you re-solve a problem you already solved — so you don't spend Tuesday rediscovering what you codified on Thursday.

**Anti-pattern:** dumping a 5-link wall of "you might want to read these." ONE link, ONE sentence, the closest match. The user can ask for more.

---

## Auto-Detection (session-end triggers)

At the end of every session (before the session-close checklist), Claude should silently evaluate whether the current session hit any of these four triggers. If one or more fires, surface it as a suggestion before closing.

**Trigger 1: High tool-call friction**
The session required 5+ tool calls to accomplish something that should have been routine (e.g., finding a file, running a known workflow, looking up a fact that's in the vault). This suggests a missing shortcut, rule, or skill step.
→ *"I noticed it took [N] steps to [task]. Want me to run /patterns to capture a shortcut?"*

**Trigger 2: User correction**
The user corrected Claude's approach during this session ("no, not that way", "don't do X", "use Y instead"). A feedback memory was likely saved, but there may be a deeper pattern if this correction echoes prior ones.
→ *"You corrected me on [topic] this session. Want me to check if this is a recurring pattern worth capturing as a rule?"*

**Trigger 3: Dead-end recovery**
Claude hit errors or dead ends before finding the working path (wrong file, failed approach, had to backtrack). The successful path should be preserved so future sessions don't repeat the exploration.
→ *"I had to backtrack on [task] before finding the right approach. Want me to capture the working path as a pattern?"*

**Trigger 4: Non-trivial discovery**
Claude discovered something non-obvious about the vault, a tool, an API, or a workflow that isn't documented anywhere. A discovery memory may have been saved, but it might warrant a rule or skill update.
→ *"I discovered [finding] this session. Want me to check if it should become a permanent rule?"*

**How to evaluate:** This is a lightweight check, not a full /patterns scan. Claude reviews the conversation in memory for: corrections received, tool-call counts on repeated tasks, backtrack moments, and surprises. If nothing fires, say nothing. If one or more fires, offer a single concise prompt (not a wall of text). The user can say "yes" (run full /patterns), "just save it" (capture the specific finding without a full scan), or "no" (skip).

**Do NOT auto-run the full pattern scan.** Only suggest. The user decides whether to run it.

---

## Step 1: Gather recent signal (do this silently)

Read these sources in parallel (skip any set to `none` in prefs):
- The last-session file — what was just worked on
- The decision log — last 10–15 entries (look for repeating decision types)
- Any content drafts or writing drafts — recent drafts (look for recurring metaphors)
- Last 7 journal entries (if a `journal-index.json` exists in the vault's Meta folder (`⚙️ Meta/` or `Meta/`), use it; otherwise read directly)

If a weekly review was just run, its output is already in context — use that, don't re-scan journals.

---

## Step 1.5: Read the observation ledger (deterministic, not the transcript)

If the Instinct Engine is installed, a `PreToolUse` hook has logged EVERY tool call this session to `~/.claude/instinct/observations.jsonl`. Read that ledger instead of reconstructing the session from the transcript — it is the 100%-capture source, not a ~50-80% reconstruction.

- **Apply decay first:** `python3 "{SKILL_DIR}/../../scripts/instinct.py" decay` (erodes instincts unseen past the grace window so confidence stays honest).
- **Friction (Trigger 1) with evidence:** count repeated `action` values per `session` in the ledger. 5+ of the same `action` to reach one outcome = a friction pattern backed by hard counts, not a vibe.
- **Current standings:** `python3 "{SKILL_DIR}/../../scripts/instinct.py" report --limit 30` lists instincts by effective confidence and flags stale ones.

If the engine is NOT installed, fall back to the in-context conversation review below.

---

## Step 2: Extract patterns

Look across everything for:

**Recurring themes** — What topics, people, or tensions keep appearing? If something shows up 3+ times in different sessions, it's a pattern.

**Decision frameworks** — Are there repeating decision types where the same logic applies each time? (e.g., "When [X], I always choose [Y] because [Z]") These should become CLAUDE.md rules.

**Writing patterns** — Metaphors, phrases, or ideas that resurface across entries. Often the most original material — but invisible because it's spread across time.

**Behavioral loops** — What accountability items keep getting flagged? What does the user keep resolving and then re-encountering?

**Vault gaps** — What concepts keep appearing in notes that don't have their own concept note yet?

---

## Step 3: Surface findings

Present findings grouped by type. Be specific — quote the actual phrase or pattern. Maximum 5 proposals at a time:

> **Writing pattern:** "[exact quote]" has appeared in 3 entries and 2 drafts in the last 2 weeks.
> → Proposed: start a writing seed in your drafts folder

> **Decision framework:** Every time [situation], you [action]. This has happened [N] times.
> → Proposed: new CLAUDE.md rule — "[rule text]"

Ask: "Which of these do you want to capture?"

---

## Step 4: Execute confirmed captures

After the user confirms, execute all approved captures in one pass:

- **Writing seed** → create a draft in the drafts folder with the exact phrasing + context of where it appeared
- **CLAUDE.md rule** → add to the relevant section, sync to any other CLAUDE.md files
- **Concept note** → create in the concept folder, add wikilinks
- **Skill improvement** → note it clearly: "This should be baked into [skill name] — flag for next update"
- **Confidence update (self-improving memory)** → when this run confirms an existing instinct held (it fired again, uncorrected), run `python3 "{SKILL_DIR}/../../scripts/instinct.py" reinforce <slug>`. When the user corrected one, run `... correct <slug>`. That bidirectional update is what makes the library self-improving instead of append-only. See `docs/instinct-engine.md`. When a domain accumulates many high-confidence instincts, run `/evolve` to propose promoting the cluster into a dedicated skill.

---

## Step 5: Log and close

Append to the configured changelog file (if set):
```
- [DATE] /patterns run: [N] captures made — [brief list]
```

If any universal patterns emerged (not personal, applicable to any second-brain user), flag them: "This pattern could go in the universal patterns repo — want me to note it for next time?"

