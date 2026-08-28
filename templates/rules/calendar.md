---
type: rule
tags: [calendar, timezone, google-workspace, mcp, hook]
---

# Calendar Rule

One hard rule, enforced by a PreToolUse hook.

## The rule

Every `start` and `end` field passed to `cal_create_event` or `cal_update_event` MUST end with an explicit timezone marker:

- `-05:00` for Lima / Quito / EST in winter
- `-04:00` for NYC EDT (Apr-Oct) / Santiago / La Paz
- `-07:00` / `-08:00` for LA depending on DST
- `+00:00` or `Z` for UTC

**Not enough:** the separate `time_zone: "America/Lima"` parameter. The MCP silently accepts naive times and Google treats them as UTC. A "10 AM local" request without the offset lands as 10:00 UTC, which can be five hours off in the Americas, eight hours off in East Asia.

## Why this matters

Naive datetimes in calendar APIs are a known footgun. The MCP layer accepts them, the calendar layer silently reinterprets them as UTC, and the event lands at the wrong hour. The error compounds: invitees get the wrong slot, reminders fire at the wrong time, and the original requester usually only catches it after the meeting was missed. The fix is a one-line discipline (always include the offset) plus a hook that enforces it.

## Enforcement

Hook script: `~/.claude/hooks/validate-calendar-timezone.py`
Registered in: `~/.claude/settings.json` as PreToolUse with matcher `mcp__.*`. The hook self-filters to the calendar create/update tools, so the broad matcher costs one cold start per MCP call and changes no behaviour. It is deliberate: the footprint gate probes `mcp__server__tool`, so a narrower matcher would be invisible to the SLA rather than cheaper.
Action: blocks any call whose start/end is a datetime without TZ marker. Date-only strings (all-day events) are exempt.

## Default timezone

Configure your default in CLAUDE.md (e.g. "Default timezone: -05:00 year-round"). Override only when the user explicitly says they're in another timezone (e.g. travel).

**Travel override checklist:**
- Before creating any event during a travel window, state the target timezone in chat: "Creating this in NYC local time (-04:00 during EDT). Confirm?"
- Do not guess DST boundaries. Check a reliable source or ask.
- For NYC: EDT (UTC-4) runs roughly second Sunday in March through first Sunday in November. Outside that window = EST (UTC-5).

## Verification step (always)

After every `cal_create_event` or `cal_update_event`, immediately call `cal_list_events` with a narrow window around the event and confirm the returned `start` matches what was requested. Bugs in the MCP return path have masked failures before, list-after-write is the only reliable check.

## Bypass

Only use `CAL_TZ_BYPASS=1` if you are calling the MCP with a genuinely non-standard payload (e.g. raw RFC3339 coming from another system that already validated). Never use it to work around the rule.
