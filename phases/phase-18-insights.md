## Phase 18: Weekly & Monthly Insights

Run the full setup as written below, including monthly reports, pattern analysis, panel commentary, and all sections.

---

"One more thing — and this might be the most powerful part. I can generate a weekly and monthly reflection from your journal entries. Not just a summary of what happened, but pattern recognition: what floors you've been on, what's shifting, what a life coach would push you on, what a therapist would want you to sit with."

Ask: "Want me to set up weekly and monthly insight reports? You type /weekly or /monthly anytime and I'll analyze your entries for that calendar period and give you a reflection."

If yes, first create a journal index builder script at `[VAULT_PATH]/⚙️ Meta/scripts/build-journal-index.py`:

```python
#!/usr/bin/env python3
"""Build a date index of all journal entries for fast lookup.

Honors the NEVER fail silently rule:
- Missing folders raise FileNotFoundError with a clear message.
- Per-file parse errors are logged to stderr AND to a sidecar log, never swallowed.
- Non-zero exit code if ANY file failed, so cron / callers can detect partial success.
"""
import os, sys, json, traceback
from datetime import datetime

VAULT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JOURNAL_DIR = os.path.join(VAULT, "\U0001f4d3 Journals")           # 📓 Journals
META_DIR = os.path.join(VAULT, "\u2699\ufe0f Meta")                # ⚙️ Meta
OUTPUT = os.path.join(META_DIR, "journal-index.json")
ERROR_LOG = os.path.join(META_DIR, "journal-index-errors.log")

# Guard: fail loudly if expected folders don't exist
if not os.path.isdir(JOURNAL_DIR):
    sys.stderr.write(
        f"ERROR: Journals folder not found at '{JOURNAL_DIR}'.\n"
        f"Check that your vault uses the '📓 Journals' folder name (Phase 3 default).\n"
        f"If your folder is named differently, update JOURNAL_DIR in this script.\n"
    )
    sys.exit(1)
if not os.path.isdir(META_DIR):
    sys.stderr.write(
        f"ERROR: Meta folder not found at '{META_DIR}'.\n"
        f"Check that your vault uses the '⚙️ Meta' folder name (Phase 3 default).\n"
    )
    sys.exit(1)

entries = []
errors = []
for fname in os.listdir(JOURNAL_DIR):
    fpath = os.path.join(JOURNAL_DIR, fname)
    if not fname.endswith(".md") or os.path.isdir(fpath):
        continue
    try:
        with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
            in_fm, meta = False, {}
            for i, line in enumerate(f):
                if i == 0 and line.strip() == '---':
                    in_fm = True; continue
                if in_fm:
                    if line.strip() == '---': break
                    if ': ' in line:
                        k, v = line.split(': ', 1)
                        meta[k.strip()] = v.strip().strip("'\"")
                if i > 15: break
            if 'creationDate' in meta:
                entry = {"file": fname, "date": meta['creationDate'][:10]}
                if 'floor' in meta: entry["floor"] = meta["floor"]
                if 'floor_level' in meta: entry["floor_level"] = meta["floor_level"]
                entries.append(entry)
    except Exception as e:
        errors.append((fname, f"{type(e).__name__}: {e}"))

entries.sort(key=lambda x: x["date"])
with open(OUTPUT, 'w') as f:
    json.dump({"total": len(entries), "last_updated": datetime.now().strftime("%Y-%m-%dT%H:%M"), "entries": entries}, f, indent=2, ensure_ascii=False)

# Surface any per-file errors loudly — never swallow
if errors:
    with open(ERROR_LOG, 'w') as f:
        f.write(f"# Journal index build errors — {datetime.now().isoformat()}\n")
        f.write(f"# {len(errors)} file(s) could not be parsed\n\n")
        for fname, err in errors:
            f.write(f"{fname}\t{err}\n")
    sys.stderr.write(
        f"WARNING: Indexed {len(entries)} entries but {len(errors)} file(s) failed to parse.\n"
        f"See '{ERROR_LOG}' for details.\n"
    )
    print(f"Indexed {len(entries)} entries ({len(errors)} errors — see {ERROR_LOG})")
    sys.exit(2)  # non-zero so cron/wrappers know it was a partial success

print(f"Indexed {len(entries)} entries")
```

Run it: `python3 "[VAULT_PATH]/⚙️ Meta/scripts/build-journal-index.py"`

Then create the skill file at `~/.claude/skills/insights/SKILL.md`:

```
**Insights skill template:** Read the full template from `templates/generated/insights-skill-template.md` and save it to `~/.claude/skills/insights/SKILL.md`. Replace `[VAULT_PATH]` with the user's actual vault path.
```

Then add routing to the user's CLAUDE.md so `/weekly` and `/monthly` work as slash commands:

```markdown
# insights (weekly / monthly)
- **insights** (`~/.claude/skills/insights/SKILL.md`) - journal pattern recognition. Triggers: `/weekly`, `/monthly`, `/insights`
When the user types `/weekly` or `/monthly`, invoke the Skill tool with `skill: "insights"` before doing anything else.
```

Now set up automatic generation. Weekly insights run every Monday morning, monthly insights on the 2nd of each month. Don't ask, just install them:

### Mac / Linux

Create the script at `[vault]/⚙️ Meta/scripts/run-insights.sh`:

```bash
#!/bin/bash
# run-insights.sh — Generate weekly or monthly journal insight reports via Claude Code CLI
# Usage: ./run-insights.sh weekly   (Monday mornings via cron)
#        ./run-insights.sh monthly  (2nd of each month via cron)

PERIOD="${1:-weekly}"
# IMPORTANT: replace [VAULT_PATH] with the user's actual vault path before
# deploying. Phase 11 should prompt for this and inject it automatically; if
# you're hand-editing, substitute it here. The script fails loud below if the
# placeholder wasn't replaced.
VAULT_DIR="[VAULT_PATH]"
LOG_FILE="$VAULT_DIR/⚙️ Meta/scripts/.insights-cron.log"

if [ "$VAULT_DIR" = "[VAULT_PATH]" ] || [ ! -d "$VAULT_DIR" ]; then
  echo "ERROR: VAULT_DIR is not set or does not exist: $VAULT_DIR" >&2
  echo "Edit run-insights.sh and replace [VAULT_PATH] with your actual vault path." >&2
  exit 1
fi

# Find the Claude CLI (path changes with version updates)
CLAUDE_BASE="$HOME/Library/Application Support/Claude/claude-code"
CLAUDE_BIN=$(find "$CLAUDE_BASE" -name "claude" -path "*/MacOS/claude" 2>/dev/null | sort -V | tail -1)

# Linux fallback
if [ -z "$CLAUDE_BIN" ]; then
  CLAUDE_BIN=$(command -v claude 2>/dev/null)
fi

if [ -z "$CLAUDE_BIN" ]; then
  echo "$(date): ERROR — Claude CLI not found" >> "$LOG_FILE"
  exit 1
fi

echo "$(date): Starting $PERIOD insights generation..." >> "$LOG_FILE"

cd "$VAULT_DIR" || exit 1

"$CLAUDE_BIN" --print \
  --model claude-sonnet-4-6 \
  --allowedTools "Read,Write,Edit,Glob,Grep,Bash" \
  --permission-mode acceptEdits \
  "Run the /insights skill for a $PERIOD report. Read the skill at ~/.claude/skills/insights/SKILL.md first, then follow its instructions exactly. Read all journal entries for the $PERIOD calendar period and generate the full report. Save it to the correct folder. After the report is saved, run /patterns in auto mode: read ~/.claude/skills/patterns/SKILL.md, scan for patterns, then automatically capture all findings without asking for confirmation — this is a headless cron run with no user present. Save pattern captures as concept notes, CLAUDE.md rules, or writing seeds — wherever they fit best." \
  >> "$LOG_FILE" 2>&1

EXIT_CODE=$?
echo "$(date): Finished $PERIOD insights + patterns (exit code: $EXIT_CODE)" >> "$LOG_FILE"
```

Make it executable: `chmod +x "[vault]/⚙️ Meta/scripts/run-insights.sh"`

Then add cron jobs. Ask the user their timezone and convert to UTC:

```bash
# Example for America/Bogota (UTC-5): 9am local = 14:00 UTC
crontab -e
# Add these lines:
# Weekly insights — every Monday at 9am local
0 14 * * 1 /bin/bash "/path/to/vault/⚙️ Meta/scripts/run-insights.sh" weekly
# Monthly insights — 2nd of each month at 9am local
0 14 2 * * /bin/bash "/path/to/vault/⚙️ Meta/scripts/run-insights.sh" monthly
```

### Windows

Create `run-insights.ps1` in the vault's `⚙️ Meta/scripts/` folder.

**CRITICAL: save the file as UTF-8 with BOM.** Windows PowerShell 5.1 (the default on Windows 10/11) reads BOM-less .ps1 files as Windows-1252 and chokes on any non-ASCII byte (em dashes, the ⚙️ in the vault path, box-drawing chars). The script below contains the ⚙️ emoji in `⚙️ Meta\scripts\`, so a no-BOM save will produce a parser error on the first run. When using the Write tool, prepend `\ufeff` to the content; when piping from Python, write `b'\xef\xbb\xbf' + content.encode('utf-8')`. After writing, verify with `file run-insights.ps1` (should report "UTF-8 (with BOM)") or PowerShell `(Get-Content run-insights.ps1 -Encoding Byte -TotalCount 3) -join ','` (should output `239,187,191`).

```powershell
# run-insights.ps1 - Generate weekly or monthly journal insight reports via Claude Code CLI
# Usage: .\run-insights.ps1 -Period weekly
#        .\run-insights.ps1 -Period monthly
param([string]$Period = "weekly")

# IMPORTANT: replace [VAULT_PATH] with the user's actual vault path before
# deploying. Phase 11 should prompt for this and inject it automatically; if
# you're hand-editing, substitute it here. The script fails loud below if the
# placeholder wasn't replaced.
$VaultDir = "[VAULT_PATH]"
$LogFile = "$VaultDir\⚙️ Meta\scripts\.insights-cron.log"

if ($VaultDir -eq "[VAULT_PATH]" -or -not (Test-Path $VaultDir -PathType Container)) {
    Write-Error "VAULT_DIR is not set or does not exist: $VaultDir. Edit run-insights.ps1 and replace [VAULT_PATH] with your actual vault path."
    exit 1
}

# Find Claude CLI (Windows)
$ClaudeBin = Get-ChildItem "$env:LOCALAPPDATA\AnthropicClaude\claude-code" -Recurse -Filter "claude.exe" -ErrorAction SilentlyContinue |
  Sort-Object FullName | Select-Object -Last 1

if (-not $ClaudeBin) {
  $ClaudeBin = Get-Command claude -ErrorAction SilentlyContinue
}

if (-not $ClaudeBin) {
  Add-Content $LogFile "$(Get-Date): ERROR - Claude CLI not found"
  exit 1
}

Add-Content $LogFile "$(Get-Date): Starting $Period insights generation..."
Set-Location $VaultDir

& $ClaudeBin.FullName --print `
  --model claude-sonnet-4-6 `
  --allowedTools "Read,Write,Edit,Glob,Grep,Bash" `
  --permission-mode acceptEdits `
  "Run the /insights skill for a $Period report. Read the skill at ~/.claude/skills/insights/SKILL.md first, then follow its instructions exactly. Read all journal entries for the $Period calendar period and generate the full report. Save it to the correct folder. After the report is saved, run /patterns in auto mode: read ~/.claude/skills/patterns/SKILL.md, scan for patterns, then automatically capture all findings without asking for confirmation, this is a headless cron run with no user present. Save pattern captures as concept notes, CLAUDE.md rules, or writing seeds, wherever they fit best." `
  2>&1 | Add-Content $LogFile

Add-Content $LogFile "$(Get-Date): Finished $Period insights (exit code: $LASTEXITCODE)"
```

Then set up Windows Task Scheduler:

```powershell
# Weekly - every Monday at 9am
$WeeklyAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-File `"C:\path\to\vault\⚙️ Meta\scripts\run-insights.ps1`" -Period weekly"
$WeeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 9am
Register-ScheduledTask -TaskName "AI Brain Weekly Insights" -Action $WeeklyAction -Trigger $WeeklyTrigger -Description "Generate weekly journal insights"

# Monthly - 2nd of each month at 9am
$MonthlyAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-File `"C:\path\to\vault\⚙️ Meta\scripts\run-insights.ps1`" -Period monthly"
$MonthlyTrigger = New-ScheduledTaskTrigger -Once -At 9am -RepetitionInterval (New-TimeSpan -Days 30)
# Note: For exact "2nd of month" scheduling, use Task Scheduler GUI or schtasks:
# schtasks /create /tn "AI Brain Monthly Insights" /tr "powershell -File \"C:\path\to\vault\run-insights.ps1\" -Period monthly" /sc monthly /d 2 /st 09:00
Register-ScheduledTask -TaskName "AI Brain Monthly Insights" -Action $MonthlyAction -Trigger $MonthlyTrigger -Description "Generate monthly journal insights"
```

Tell the user which option was set up and confirm the schedule: "Your weekly insight will generate automatically every Monday at [time] and your monthly on the 2nd at [time]. You can also run /weekly or /monthly manually anytime. Check the log at `⚙️ Meta/scripts/.insights-cron.log` if you ever want to verify it ran."

---

## → Next phase (the install is not finished)

You have just completed **Phase 18**. Do not stop here, do not summarise the
install as complete, and do not wait to be asked to continue.

1. **Record progress.** Write `~/.claude/.ai-brain-starter-progress.json`:
   `{"last_completed_phase": "18", "ts": "<ISO-8601 timestamp for now>", "version": 1}`
   This one line is what lets a resumed install pick up here instead of starting over.
2. **Run the next phase.** Read `phases/phase-19-23-finish.md` from this skill's own directory
   and execute **Phase 19-24.5**.

If you are running low on context, say so out loud and tell the user to open a
fresh session and ask to resume the ai-brain-starter install; the progress file
above is what makes that work. Ending here silently leaves them with a
half-built brain that looks finished.

<!-- phase-chain: next=phase-19-23-finish.md -->
