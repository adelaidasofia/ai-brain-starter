---
name: backfill-journal-body-context
description: Use when the user says /backfill-journal-body-context, wants existing daily journal entries enriched with body data retroactively (HRV, sleep, recovery, cycle phase, labs), says 'backfill my journals with health', wants Apple Health / Oura / Fitbit / Whoop history paired with past journals, or just ran /health-setup with a historical import. Not for brand-new entries (daily-journal covers those) or for editing journal text.
---

# backfill-journal-body-context

Reads existing daily journals, pulls health-mcp data for each entry's date, and appends a "Body track" section BELOW the original verbatim content. The original entry text is NEVER modified — the rule from `feedback_journal_verbatim_words.md` is non-negotiable.

## When to use

- User says `/backfill-journal-body-context`
- User wants their existing journals enriched with body data (HRV, sleep, recovery, cycle phase) retroactively
- User wants to see what `/weekly` and `/monthly` would surface if they had been pulling body data all along
- After running `/health-setup` for the first time and importing a backfill window of biometric data

Do NOT use for:
- Brand-new journal entries (daily-journal already pulls body context via health-context skill)
- Editing journal content beyond appending the body section
- Anything that touches the original verbatim journal body

## How it works

1. Determine the date range. Default: `--year <current-year>` (Jan 1 to today).
2. Find journal entries in that range using `[VAULT_PATH]/⚙️ Meta/journal-index.json` (or `Meta/journal-index.json` on non-emoji vaults; rebuild if stale).
3. For each entry:
   - Read the file
   - Check if it already has a `## Body track (health-mcp, backfilled YYYY-MM-DD)` section — if yes, skip (idempotent)
   - Call `health_journal_context(date, voice_profile="warm")` for the data + rendered prose
   - Call `health_cycle_context(date)` if cycle data exists (for women's cycle awareness)
   - Call `health_recovery_score(date)` + `health_sleep_score(date)` for the scores
   - Call `health_lab_panel(date, lookback_days=180)` for any out-of-range markers active that period
   - Pair the body data with the entry's floor tag (from frontmatter `floor_level` + `floor`)
   - Render the body-track section using the template below
   - Append BELOW the original content with a clear divider
4. Print summary: N entries processed, M backfilled, K skipped.

## Body track template

Append below the original journal content, after a blank line + horizontal rule:

```markdown

---

## Body track (health-mcp, backfilled {{today_iso}})

*Auto-generated context. Original journal entry above is preserved verbatim.*

**Floor that day:** {{floor_name}} ({{floor_level}})

**Cycle phase** (if cycle data exists): {{phase}}, cycle day {{cycle_day}}{{ — irregularity flag if any}}

**The body that day:**
- HRV: {{hrv_ms}} ms ({{hrv_delta_pct}}% vs 30-day baseline)
- RHR: {{rhr_bpm}} bpm
- Sleep: {{sleep_asleep_min}} min ({{sleep_efficiency}}% efficiency, REM {{rem_min}}min, deep {{deep_min}}min)
- Steps: {{steps_total}}
- Workouts: {{workout_count}} ({{workout_min}}min)
- Mindful: {{mindful_min}}min

**Scores:**
- Recovery: {{recovery_score}}/100 ({{confidence}} confidence)
- Sleep: {{sleep_score}}/100

**Floor-paired interpretation** (1-3 sentences, see synthesis rule below): {{interpretation}}

**Lab markers** (if any out-of-range result from the prior 180 days, paired with today): {{lab_flags}}
```

## Synthesis rule (the helpful-not-just-cool part)

The `interpretation` line must follow the same shape as the `/weekly` section 0d synthesis. Each line:
- Names the pattern (cycle phase explaining HRV, under-fueling masking recovery, anniversary coupling, etc.)
- Hypothesizes what it might mean
- Suggests a specific next-action when applicable

Examples (template SHAPES, not for verbatim use):

> Floor was Fear and HRV ran 22% below baseline on a luteal day. The body and the mind both registered the threat. Without the journal entry the recovery score would have called this "rest more"; pairing with Fear shows the actual signal was "the worry is doing work the rest can't fix."

> Floor was Joy after a strong gym week. HRV at baseline, sleep efficiency 94%. This is the high-water mark — note what conditions produced it.

> Floor was Apathy and Vitamin D 25-OH came back at 26 ng/mL (below range) the month before. The mood floor may have had a metabolic floor under it. Worth a re-test after 3 months of supplementation.

Banned shapes (from `/weekly` section 0d, same rules apply):
- Listing body numbers without an interpretation
- Generic "rest more / eat better / hydrate" advice
- Treating recovery score as ground truth when cycle phase or under-fuel explains the dip
- Pretending in-range labs cause symptoms

## Model routing

The interpretation line is grunt-work prose. Use the cheapest model that produces a helpful sentence. Order of preference:

1. **Python template** (zero cost) — for high-confidence cases (HRV in normal range during follicular, no out-of-range labs, no anniversary signal). The script `scripts/backfill-journal-body-context.py` covers this.
2. **MiniMax** (~$0.06/M tokens, very cheap) — for cases where the data shows a real pattern that deserves a synthesized sentence. Invoke via `"⚙️ Meta/scripts/minimax.sh"` if the vault has it, otherwise skip the LLM step and use a fallback template.
3. **Haiku** (cheap, fast, reliable) — for cases where MiniMax is unavailable.
4. **Sonnet** — only if explicit `--high-quality` flag passed and the user accepts the cost.

The default is Python template + MiniMax fallback. Do NOT default to Sonnet for hundreds of journal entries.

## Invocation

The actual work runs in `scripts/backfill-journal-body-context.py`. The skill assembles arguments and hands off to the script.

When invoked:

1. Parse arguments:
   - `--year YYYY` (default: current year)
   - `--start YYYY-MM-DD` and `--end YYYY-MM-DD` (override year)
   - `--vault-root PATH` (default: $VAULT_ROOT or autodetect from cwd)
   - `--llm-model {python,minimax,haiku,sonnet}` (default: `python` with `minimax` fallback)
   - `--dry-run` (print what would change without writing)
   - `--force` (overwrite an existing body-track section)

2. Sanity checks:
   - health-mcp must be registered. If not, abort with setup instructions.
   - Run `health_status()` to confirm there's biometric data in the DuckDB. If the count is zero, abort and suggest `/health-setup` first.
   - The vault must have a `⚙️ Meta/journal-index.json` (or `Meta/journal-index.json` on non-emoji vaults) and a journal folder. Rebuild the index if stale.

3. Run the script:
   ```
   /usr/bin/python3 "[REPO_ROOT]/scripts/backfill-journal-body-context.py" --year 2026 --vault-root "$VAULT_ROOT"
   ```

4. Surface the summary: N entries processed, M backfilled, K skipped, plus the date range covered.

## Output

The skill does NOT write to the vault itself — the Python script does the file mutations. The skill only:
- Validates inputs
- Calls health-mcp tools to verify data exists
- Invokes the Python script
- Reports the result

## Idempotency

The Python script checks each journal file for an existing `## Body track (health-mcp, backfilled` line. If present, skip unless `--force`. Re-running the skill on the same range is safe.

## Ongoing daily run

After the initial backfill, the same script runs daily for yesterday's entry via a scheduled task (use the `/schedule` skill). Suggested cadence: 7am local, after Apple Watch has uploaded the previous night's sleep data.

Scheduled-task body:
```
/usr/bin/python3 "[REPO_ROOT]/scripts/backfill-journal-body-context.py" --start "$(date -v-1d +%F)" --end "$(date -v-1d +%F)" --vault-root "$VAULT_ROOT"
```

That's "backfill yesterday, every morning, forever." Set it up after the initial backfill completes successfully.

## Voice rules

- The auto-generated body-track section is in `warm` register (narrative sentences, not clinical exact-number dumps)
- For Spanish journals, render the section in Spanish (the script detects via the journal's frontmatter `language:` field if present)
- Floor names use the appropriate language alias (`[[Joy]]` in English / `[[Alegría]]` in Spanish)
- Never modify the original entry's content. Only append.

## Privacy

The script reads journal files + the local DuckDB. It writes only to journal files (appending body-track sections). No data leaves the machine. The interpretation step (if LLM-backed) sends ONLY the structured body data + floor tag to the chosen model — never the journal body content.
