---
name: sunday-review
description: 'Use when the user asks for the weekly meta-review or Sunday wrap-up of their week and vault: says /sunday, /sunday-review, ''let''s do the weekly review,'' ''Sunday review,'' ''review my week,'' ''weekly retro,'' ''week in review,'' ''end-of-week review,'' or wants journals, patterns, vault health, and stale decisions reviewed together at week''s end. Not for a journal-pattern read alone (use /weekly) or mid-week single checks.'
---

# /sunday-review — weekly meta-orchestrator

> **`{SKILL_DIR}`** = this skill's own folder (locally: the directory this SKILL.md lives in; a served brain substitutes the real absolute path before you read this). Shared starter files live at the repo root two levels up: `{SKILL_DIR}/../..`. If a path does not resolve, name the missing file and stop — never guess another location.

You are running the Sunday meta-review. This skill doesn't reinvent the existing weekly skills — it orchestrates them in the right sequence, surfaces the cross-cutting signal, and produces ONE clean note instead of N independent reports.

## Order of operations

Run each step in order. After each step, capture the headline finding (1-2 sentences) into the running synthesis. Do NOT dump full reports inline — link to them.

### Step 1 — Pattern recognition (`/weekly`)

Invoke the existing `/weekly` insights skill. It produces the panel-driven journal pattern recognition for the past 7 days. Capture: which floor was dominant, what pattern repeated, what was avoided.

### Step 2 — Instinct Engine (`/patterns`)

Invoke `/patterns`. It scans recent sessions, journals, and decisions for hardening patterns and turns them into concrete captures. Capture: any new patterns ready to codify into a CLAUDE.md rule, concept note, or skill improvement.

### Step 3 — Vault hygiene scan

Run:
```bash
python3 "{SKILL_DIR}/../../scripts/vault-hygiene.py" --quiet
```

It writes a fresh report to `⚙️ Meta/Vault Hygiene.md`. Capture: how many broken wikilinks, empty notes, stale notes, duplicate concepts.

### Step 4 — CLAUDE.md drift

Run:
```bash
python3 "{SKILL_DIR}/../../scripts/check-claude-md-drift.py" --quiet
```

It writes a report to `⚙️ Meta/CLAUDE-md drift.md`. Capture: any dormant people, archived projects, broken links, or old codifications that need review.

### Step 4b — Multi-edit semantic drift (if the script exists in this vault)

Run:
```bash
python3 "$VAULT_ROOT/⚙️ Meta/scripts/drift-detection.py"
```

It writes a report to `⚙️ Meta/Drift Audit.md`. Lists vault files edited 5+ times in the last 30 days (configurable via `--days`, `--min-edits`, `--top`, `--include`). Codified-rule files at the top are highest-leverage drift candidates. Capture: which rule file shows highest churn, and whether any of the diffs softened a guard or shifted a number without a Decisions/ entry. Skip silently if the script is missing (vault hasn't installed it yet). Inspired by Microsoft DELEGATE-52 (arxiv.org/abs/2604.15597) finding that frontier LLMs corrupt ~25% of professional content over 20 edits.

### Step 4c — Cross-document rule conflicts (if the script exists in this vault)

Run:
```bash
python3 "$VAULT_ROOT/⚙️ Meta/scripts/check-rule-conflicts.py" --scan-all
```

It writes a report to `⚙️ Meta/Rule Conflicts.md`. Engram-inspired (github.com/Gentleman-Programming/engram) keyword-anchor detector — catches `always X` vs `never X` contradictions across the rules corpus. Add `--semantic` if `ANTHROPIC_API_KEY` is set for vocabulary-different contradiction detection via claude-haiku. Capture: any candidate conflicts at confidence ≥0.5 that need reconciliation. Pair signal with drift detection: drift = single-document shift over time; conflicts = cross-document clash at write time. Skip silently if the script is missing.

### Step 4c.5 — Storage tier audit (if the script exists in this vault)

Run:
```bash
python3 "$VAULT_ROOT/⚙️ Meta/scripts/storage-tier-audit.py"
```

Walks every vault subdirectory, reads its `.tier` declaration (GIT / DB / OS), flags directories missing the declaration or holding GIT-tier files >1MB. Output uses Compiled-Truth + Timeline format at `⚙️ Meta/Storage Tier Audit.md`. Pattern source: garrytan/gbrain (cherry-picked). Capture: how many directories missing tier declarations, any oversized GIT-tier files. Add `.tier` files to the highest-traffic missing directories before next week. Skip silently if the script is missing.

### Step 4c.6 — Zero-LLM typed-relationship refresh (if the script exists in this vault)

Run:
```bash
python3 "$VAULT_ROOT/⚙️ Meta/scripts/wire_typed_relationships.py" \
  --output "$VAULT_ROOT/⚙️ Meta/typed-edges.jsonl"
```

Re-extracts typed edges (frontmatter + wikilinks) across the full vault using regex + entity-type rules. Zero LLM calls. Pattern source: garrytan/gbrain (cherry-picked). The output JSONL feeds `/graphify` Part A.5 so the next graphify run skips the structural extraction LLM cost. Capture: total edges by type (works_at / journaled_about / attended / etc.), runtime in ms, any extraction failures logged to stderr. Skip silently if the script is missing.

### Step 4c.7 — Public repo standards audit (if the script exists in this vault)

Run:
```bash
python3 "$VAULT_ROOT/⚙️ Meta/scripts/audit-public-repo-standards.py"
```

Audits every public non-archived non-fork repo under your GitHub org for baseline standards: LICENSE, README, real CI workflow (Dependabot auto-merge alone does not count), last-push freshness (>60d = stale). Classifies repo type (`code-python`, `code-node`, `code-go`, `code-rust`, `skill`, `docs`, `meta`) so CI requirements only apply to actual code repos. Output uses Compiled-Truth + Timeline format at `⚙️ Meta/Public Repo Standards Audit.md`. Capture: any repos with gaps, especially `missing LICENSE` (MIT add via `gh api -X PUT contents/LICENSE`) or `no CI workflow` (extend the harden script's Layer 6 to handle the missing case). Skip silently if the script is missing.

### Step 4c.8 — Hallucination tracking (if the scripts exist in this vault)

Run the aggregator first (fast, no LLM calls):
```bash
python3 "$VAULT_ROOT/⚙️ Meta/scripts/hallucination-watch.py" --quiet
```

Then run the sample audit (slower, requires Claude auth — `ANTHROPIC_API_KEY` in env OR a `claude` CLI subprocess can reach Max OAuth):
```bash
python3 "$VAULT_ROOT/⚙️ Meta/scripts/hallucination-sample-audit.py" --turns 6 --quiet
```

The aggregator tallies pre-ship catches from the hookify-blocks log, post-ship corrections from the Critical Failure Inventory, and fabrication-class feedback memories. Output: `⚙️ Meta/Hallucination Watch.md`. The sample audit random-samples recent assistant turns, extracts verifiable factual claims, deterministic vault-greps the keywords, and Claude-classifies each claim as supported / contradicted / unverifiable. Output: `⚙️ Meta/Hallucination Sample Audit.md` (overwrites) + appended history to `⚙️ Meta/Hallucination Sample Audit History.jsonl`. Together they form a per-vault verification harness in the family DELEGATE-52 (arxiv 2604.15597, Apr 2026) identifies as the only consistent mitigation for multi-turn LLM corruption. Capture: this week's fabrication hook fires, any new Critical Failure Inventory rows, this run's `verified_fraction`, and the 4-week trend on verified_fraction (if 4+ history rows exist). Skip silently if either script is missing.

### Step 4c.9 — Agent Memory orphan-debt trend (if the script exists in this vault)

Run:
```bash
python3 "$VAULT_ROOT/⚙️ Meta/scripts/orphan-memo-trend.py"
```

Reports how many Agent Memory memos are NOT referenced in the `MEMORY.md` index, split into total (mostly expected cold mass) and **non-audit** (`feedback_`/`project_`/`performance_`/`user_` — the actionable hot tier the discoverability close-gate flags), with the delta since last run persisted to `⚙️ Meta/logs/orphan-memo-trend.json`. Policy: `MEMORY.md` is a curated, capped index (~200 lines / 24.4 KB Read-tool cliff), NOT a complete catalog — do NOT drive the count to zero; "orphan" is the correct resting state for cold / superseded memos. Only a RISING non-audit count is a regression (new hot memos written without an index entry — index them with the atomic `memo` / `memo-finalize` helper). Capture: this week's non-audit orphan count + delta. Skip silently if the script is missing.

### Step 4d — Passive captures triage (if the script exists in this vault)

Run:
```bash
SINCE=$(date -v-7d +%Y-%m-%d 2>/dev/null || date -d "7 days ago" +%Y-%m-%d)
python3 "$VAULT_ROOT/⚙️ Meta/scripts/passive-capture.py" --scan-since "$SINCE"
python3 "$VAULT_ROOT/⚙️ Meta/scripts/passive-capture.py" --triage
```

Engram-inspired (`mem_capture_passive`) — scans the past week's session transcripts for utterances pattern-matched as rules, decisions, or lessons that were NOT explicitly filed via `/journal` or `/decision`. Writes triageable stubs to `⚙️ Meta/Passive Captures/{date}-{slug}.md`. Idempotent via state file. Capture: how many pending captures, broken down by type (rule/decision/lesson). Adopt the load-bearing ones into CLAUDE.md or rules/ files; reject and archive the rest. Skip silently if the script is missing.

### Step 4f — Closed-loop week report (if the script exists in this vault)

Run:
```bash
python3 "{SKILL_DIR}/../../scripts/closed-loop-week-report.py" \
  --vault-root "$VAULT_ROOT" --days 7
```

Reports the previous week's automatic activity in the episodic→procedural memory loop: how many promotion candidates landed in `Meta/Promotion-Candidates/` awaiting human ratification, how many got auto-promoted into `Meta/Workflows/` / `Meta/Exceptions/` / `Meta/Facts/`, how many rules were demoted to `status: superseded`, and the current resolver conflict count. Inline the script's output into the synthesis under "Closed-loop activity." If candidates are pending, the user's "One thing to do this week" should be reviewing them. The closed-loop runs hourly (promote) and weekly (demote/conflicts), but the human ratification gate only fires once a week, here. Skip silently if the script is missing.

### Step 4g — Security cadence check (if the script exists in this vault)

Run:
```bash
python3 "$VAULT_ROOT/⚙️ Meta/scripts/security-cadence-report.py"
```

Reads telemetry from every autonomous security layer (gh-harden daily fleet, workflow-injection scanner, endpoint-inventory daily NDJSON, secret-detection log, permission-denied log, Dependabot backlog per repo, hookify auto-commit volume) and writes `⚙️ Meta/Security Cadence Report.md`. Designed as the weekly meta-audit of automation coverage — the question Layer-1 cron substrate doesn't answer: *"is the automation still catching what it should?"*

After the script lands the report, inline a 5-minute Claude pass answering the 6 drift questions at the report tail:

1. Did any cron-layer fail silently this week? (Baselines missing days, gh-harden log gap > 24h, scanner returning identical lines for multiple days)
2. Did any hookify rule fire zero times in 30d? (Rule earning its slot, or drifted off the actual risk surface — candidate for deletion)
3. Did secret-detection or permission-denied counts spike? (Spike = new risk surface OR false-positive class needing carve-out; investigate)
4. Any production-tier repo with >5 open Dependabot PRs? (Tier-protected prod repos cannot accumulate unreviewed vuln PRs)
5. Has the threat landscape shifted since last check? (Read 2-3 CISA + OWASP + vendor advisories — does any layer need a new pattern?)
6. Any new public repo / endpoint / agentic surface shipped this week not classified into the 4-layer detection model? (CI-publish workflow-injection / post-publish Dependabot / outbound SSRF / endpoint-installed-state inventory)

The Claude pass surfaces signals INTO the /sunday-review synthesis under a "Security cadence" subsection. If any question's answer is non-trivial → escalate to `/code-security` as event-driven, NOT calendar-driven. The three-layer cadence model:

- **Layer 1** — daily cron (gh-harden + workflow-injection scan + endpoint inventory + Dependabot + hookify family). Auto, no skill invocation.
- **Layer 2** — weekly meta-audit (this Step 4g via security-cadence-report.py).
- **Layer 3** — event-driven `/code-security` (new endpoint, new agentic surface, new CVE class, drift escalation from Layer 2). Not calendar.

Source: panel synthesis (Schneier + Charity Majors + Patrick Collison + DHH-dissent, 2026-05-27) on cadence-vs-event-driven security. Bug class prevented: `ARTIFACT-WITH-OVER-STRICT-VERIFICATION` applied to time — over-scheduled verification trains a bypass habit; real fires get ignored. Skip silently if the script is missing.

### Step 5 — Decision retrospective

Run:
```bash
python3 "{SKILL_DIR}/../../scripts/decision-retrospective.py" --apply-prompt
```

This surfaces decisions older than 90 days with empty Outcome and appends review-ready prompts to `⚙️ Meta/Decision Retrospective.md`. Capture: how many stale decisions need their Outcome filled in.

### Step 6 — Skill usage curatorial pass (if telemetry enabled)

Run:
```bash
python3 "{SKILL_DIR}/../../scripts/skill-usage-report.py"
python3 "{SKILL_DIR}/../../scripts/curate-skills-surface.py" --top 5 --days 7
```

Capture: the 3 most-used skills this week + any skills that haven't been used in 30+ days (dormant — candidates for pruning or re-promoting).

### Step 7 — Synthesize into one note

Write a single markdown note to `📓 Journals/Reviews/Sunday Review {YYYY-MM-DD}.md` with this structure:

```markdown
---
creationDate: {ISO timestamp}
type: review
category: sunday-meta
week_of: {YYYY-MM-DD}
---

# Sunday Review — {YYYY-MM-DD}

## Highlights this week
[1-2 sentences from /weekly + /patterns: what was the dominant emotional pattern, what shipped, what stalled]

## What's hardening (from /patterns)
[any new patterns that appeared 3+ times → ready to codify]

## Vault state
- Hygiene: [link to Meta/Vault Hygiene.md] · {broken wikilinks count}, {empty notes count}, {stale notes count}
- CLAUDE.md drift: [link to Meta/CLAUDE-md drift.md] · {N signals flagged}
- Stale decisions: [link to Meta/Decision Retrospective.md] · {N candidates for outcome backfill}

## Skill usage
- Top this week: {skill 1}, {skill 2}, {skill 3}
- Dormant: {names of skills not used in 30 days}

## One thing to do this week
[Pick the highest-leverage action from the captures above. Just one.]
```

### Step 8 — Surface to the user

Reply with a 2-3 sentence summary. Link to the Sunday Review note. Name the one thing to do this week. End there. Do NOT inline the whole review.

## Why this exists

Five Sunday-relevant skills exist already (`/weekly`, `/patterns`, `vault-hygiene`, `claude-md-drift`, `decision-retrospective`). Running them sequentially by hand is friction. Running them via this orchestrator produces one synthesized output that respects the user's attention budget.

The Matuschak panel critique was that more skills don't deepen thinking unless they compound. This is the compounding layer: it forces the existing tools to interlock once a week.

## When NOT to use this

- During the week (these are weekly-cadence checks; daily use is overkill).
- When the user explicitly only wants `/weekly` (the journal pattern read), not the full meta-review.
- When the user says they want to skip the system and just journal — respect that.

## Configuration

In CLAUDE.md frontmatter, you can disable specific steps:

```yaml
sundayReview:
  skipHygiene: false
  skipDrift: false
  skipRetro: false
  skipTelemetry: true   # if telemetry not opted-in
```

If a step's underlying script is missing, skip silently and note it in the synthesis under "Pending steps."
