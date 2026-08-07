---
name: ingest-health
description: Use when the user says /ingest-health, asks to import, sync, ingest, or load Apple Health / Apple Watch / HealthKit data, has a fresh export.zip from the iPhone Health app, a Simple Health Export CSV folder, or a Health Auto Export TCP live feed, or when health-mcp queries return empty because no data was ever imported. Not for querying existing health data (health-mcp tools do that) or non-Apple wearables (Garmin, Whoop, Oura).
---

# ingest-health, Apple Health to DuckDB connector

Loads Apple Health data into `~/.claude/health-mcp/health.duckdb` so the rest of the substrate (daily-journal, coaching, advisory-panel, patterns, insights) can read biometrics through health-mcp tools.

This is one of the substrate's ingestion connectors (slack, gmail, linear, github, notion, whatsapp, health). Each owns a single source.

## When to use

- User says `/ingest-health` (with or without `--mode xml|csv|tcp` and a path)
- User asks to import, sync, capture, ingest, or pull Apple Health data
- User wants to set up the health connector for the first time
- After a fresh Apple Health export from iPhone (mode A) or after refreshing the Simple Health Export folder (mode B)

Do NOT use for:
- Querying existing health data (use health-mcp's `health_query`, `health_metric_series`, `health_recovery_score`, etc.)
- Writing to the vault (this skill is read-only with respect to the vault)
- Non-Apple-Health sources (Garmin / Whoop / Oura get different connectors; for multi-wearable see the open-wearables project)

## Privacy notes

Apple Health data is among the most sensitive content the user owns. Read this before running:

1. **Storage stays local.** The DuckDB file lives at `~/.claude/health-mcp/health.duckdb`. Treat it as a medical record.
2. **Export files are sensitive too.** Apple's `export.zip` contains every metric ever recorded on the device. Don't commit it to git, don't sync it through cloud storage you don't trust, and delete the staging copy after import if you don't need to re-import.
3. **Live mode is local Wi-Fi only.** The Health Auto Export TCP server is unauthenticated. Run it only on a trusted network.
4. **The MCP never writes the vault.** This skill imports into DuckDB. Vault-aware tools READ frontmatter; they never write.

If any of these is unclear, do not run the skill. Ask first.

## How it works

Three modes. Pick by argument or ask the user.

### Mode A: XML export.zip (default; free, manual, universal)

1. User has exported from iOS Health app: Profile → Export All Health Data → `export.zip`
2. They've moved the zip to disk (AirDrop / Files / share)
3. Skill calls `health_import_xml(zip_path)`
4. The MCP streams-parses the XML, inserts into DuckDB, returns row counts
5. Idempotent: re-running on the same SHA returns `skipped: true` unless `force=True`

### Mode B: Simple Health Export CSV folder (free, manual)

1. User has the folder of `HKQuantityTypeIdentifier*.csv` files from the Simple Health Export iOS app
2. Skill calls `health_import_csv(folder_path)`
3. Same idempotency as XML

### Mode C: Health Auto Export TCP-live (paid iOS app, real-time)

1. User has Health Auto Export installed on iPhone with TCP server enabled
2. Skill calls `health_live_query(metric, host, port, start, end)` for the metric the user asks about
3. v1 returns the raw response. v0.2 will normalize into the same DuckDB schema as A and B.

## Voice rules

- No em dashes in any user-facing prose
- No exclamation marks
- Direct, no fluff
- Voice rules apply to the summary the skill returns, NOT to docstrings inside health-mcp source

## Invocation

The skill is a thin orchestrator. Actual import runs in Python at `${SKILL_ROOT}/ingest.py`. The skill assembles the health-mcp tool calls and hands the work to the script.

When invoked:

1. Parse arguments. Accept any of:
   - `<path>` (positional): infer mode from path (`.zip` or `.xml` -> A, directory -> B)
   - `--mode xml|csv|tcp`
   - `--force` (re-import even if SHA matches)
   - `--host HOST --port PORT --metric METRIC --start DATE --end DATE` (TCP mode)

2. Resolve mode + paths. If ambiguous, ask the user.

3. Call the appropriate health-mcp tool:
   - Mode A: `health_import_xml(zip_or_xml_path, force=...)`
   - Mode B: `health_import_csv(folder_path, force=...)`
   - Mode C: `health_live_query(metric, host=..., port=..., start=..., end=...)`

4. Surface the response: row counts, skipped/imported flag, top metric types.

5. After successful import (A or B), suggest a sanity check call:
   - `health_status()` for stats
   - `health_recovery_score("<recent date>")` for end-to-end smoke

## Output

The skill returns a brief summary. No file writes. Example:

```
Imported 482,193 records / 18 workouts / 1,247 sleep segments from
/Users/<you>/Downloads/export.zip in 6.4s.

Top types:
  HKQuantityTypeIdentifierStepCount: 358,201 records (2018-09-01 to 2026-05-09)
  HKQuantityTypeIdentifierHeartRate: 92,047 records (2019-12-15 to 2026-05-09)
  HKQuantityTypeIdentifierHeartRateVariabilitySDNN: 4,182 records
  ...

Try: health_recovery_score("2026-05-09") for an end-to-end smoke check.
```

## Composition

After ingestion, these skills can use the data:

- **daily-journal**: calls `health_journal_context(date)` to prompt with last-night sleep + HRV
- **patterns**: calls `health_floor_correlation(metric, days, vault_root)` to surface biometric-vs-Floor patterns
- **coaching**: calls `health_coaching_context(start, end, vault_root)` to surface recovery markers
- **advisory-panel**: calls `health_panel_context(date, vault_root)` to inform deferral suggestions
- **insights**: calls `health_weekly_rollup(week_start)` for the weekly review

The wrapper skill `health-context` auto-fires these when the relevant trigger keywords appear.
