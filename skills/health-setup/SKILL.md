---
name: health-setup
description: Use when the user says /health-setup or /setup-health, asks to set up or connect a wearable (Apple Watch, Apple Health, Oura Ring, Fitbit, Garmin, Whoop), asks how to import Oura / Fitbit / Apple Health data, asks which wearable to use, hits a failing health_vendor_healthcheck, a 429 rate limit, or an expired Fitbit token during setup, or has health-mcp installed with an empty body track. Not for querying already-imported data or building new connectors.
---

# health-setup, interactive wearable connector wizard

Walks the user from "I have an Apple Watch / Oura / Fitbit" to "data is in my DuckDB and I can run /weekly with body track populated."

The wizard branches by:
1. Which wearable(s) they have
2. Which OS they're on (macOS / Windows / Linux)
3. Whether they're starting fresh or adding a second device

It never installs anything they don't want. Each branch has its own setup path — most of them are 3-5 manual steps and a paste-into-shell to set env vars.

## When to use

- User says `/health-setup` or `/setup-health` or `setup my health connector`
- User asks "how do I import Oura / Fitbit / my Apple Watch data"
- User says "which wearable should I use" — pick + walk through setup
- After a fresh install of the ai-brain-starter substrate
- After health-mcp v0.3+ is registered but no data is imported yet

Do NOT use for:
- Querying already-imported data (use `health_status`, `health_recovery_score`, etc.)
- Building new wearable connectors (that's a substrate dev task)
- Onboarding non-health skills

## Wizard flow

### Step 1: Detect the OS

Look for `darwin` / `linux` / a Windows path separator in the environment. Confirm with the user if uncertain. Map to one of: `macos`, `linux`, `windows`.

### Step 2: Ask which wearable(s) they have

Multiple-select. The substrate currently supports first-class:

- **Apple Watch / iPhone (Apple Health)** — most common, free, works without iPhone-paired apps
- **Oura Ring** — free Personal Access Token, no app review needed
- **Fitbit** — free Personal app, slightly more setup (OAuth2)
- **Garmin** — sync to Apple Health on iPhone, then ingest via Apple Health path
- **Whoop** — deferred to v0.4

If they say "multiple" — that's fine, the substrate's shared DuckDB schema accepts data from all vendors. Run each vendor's setup in sequence.

If they say "I don't have one" — close the wizard. Recommend they journal manually + add labs (`health_import_labs`) for the parts of the substrate that don't need wearables.

### Step 3: Run `health_vendor_setup_guide` for each chosen vendor

Call the tool with the vendor + OS:

```
health_vendor_setup_guide(vendor="oura", os_kind="macos")
```

The returned dict contains: display_name, summary, common_steps, transfer_steps (OS-specific), env_vars (with explanations), tool_to_run, ongoing_cadence, notes.

Render it to the user as numbered steps. Do NOT paraphrase the env-var commands — copy them verbatim, the user will paste them into their shell.

### Step 4: Verify before importing

Once env vars are set and Claude Code restarted, run:

```
health_vendor_healthcheck(vendor="oura")   # for Oura
health_vendor_healthcheck(vendor="fitbit") # for Fitbit
health_status()                            # for Apple Health
```

Each returns either `{ok: true, ...account-info}` or `{ok: false, error: "..."}`. If `ok: false`, surface the error and walk back to the env-var step.

### Step 5: First import

Once the healthcheck passes, run the vendor's import for a reasonable initial window. For backfill, propose Jan 1 of the current year to today:

```
health_import_apple_health("/path/to/export.zip")        # Apple Health
health_import_oura(start="2026-01-01", end="2026-05-10") # Oura
health_import_fitbit(start="2026-01-01", end="2026-05-10") # Fitbit (may take 2-5min due to per-day API calls)
```

Surface the row counts at the end. Confirm with a sample query:

```
health_recovery_score("2026-05-09")
health_cycle_context("2026-05-09")
health_longevity_panel("2026-05-09")
```

### Step 6: Suggest the ongoing cadence

For Apple Health: re-export from iOS every 1-4 weeks.

For Oura + Fitbit: a daily scheduled task. Suggest creating one via the `/schedule` skill — pull yesterday's data every morning at 6am. The scheduled task call is:

```
health_import_oura(start="<yesterday>", end="<yesterday>")
health_import_fitbit(start="<yesterday>", end="<yesterday>")
```

Each runs in <30 seconds for a single day.

### Step 7: Suggest the backfill

If the user wants their existing daily journals enriched with body context retroactively, point them to `/backfill-journal-body-context`. That skill walks every journal entry this year and appends a body-track section below the original content (verbatim preserved per the journal voice rule).

## Voice rules

- Direct, warm, no fluff
- One step at a time — never dump the full wizard in one message
- Copy-paste blocks: indent and code-fence them so the user can copy without losing whitespace
- Match the user's language (Spanish if they Spanish, English if they English)
- If the user is on Windows, never give them macOS commands as the "default"

## Multi-vendor merging

If the user has both Apple Watch AND an Oura Ring, the DuckDB schema accepts both. The recovery_score formula will use whichever metric has data for a given day. When both vendors record HRV on the same day, the LAST writer wins (no smart merging in v0.3). v0.4 will add per-source priority preferences.

## Graceful failure modes

- **Vendor API rate-limited:** Fitbit has 150 req/hour. The importer respects the rate by serial single-day calls. If 429 surfaces, suggest a 1-hour wait or chunking the backfill into smaller windows.
- **Token expired:** Fitbit access tokens expire in 8 hours. If FITBIT_REFRESH_TOKEN + FITBIT_CLIENT_ID + FITBIT_CLIENT_SECRET are set, the client refreshes automatically. If only the access token is set, walk the user back through the OAuth flow.
- **No iPhone available:** Apple Health requires iOS export. If user has no iPhone, they cannot use the Apple Health path. Route them to Oura (free) or Fitbit.
- **Vendor not supported yet:** Whoop and direct Garmin support are v0.4. Today's substitute: sync Garmin to Apple Health on iPhone, then export via Apple Health.

## Output contract

The wizard does not write any files. It only:
- Calls `health_vendor_setup_guide` to render instructions
- Asks the user to paste env vars into their shell themselves
- Calls `health_vendor_healthcheck` and the import tools when they're ready
- Surfaces the row counts + sample queries at the end

No vault writes, no global state changes. The substrate stays clean.
