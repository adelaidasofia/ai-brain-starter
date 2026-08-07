---
type: rule
purpose: Lifecycle of cross-session handoff files. Single source of truth for create-time enforcement, location convention, and close-time cleanup.
trigger: Creating, editing, or scanning handoff files at session close
---

# Handoff file lifecycle

Handoff files are cross-session context bridges. One session writes them so the next session can pick up cold. Once the bridged work ships, they become clutter and create ambiguity about what's still active. This rule defines a closed-loop lifecycle so they never accumulate again.

## Identification

A file is a handoff if EITHER:
- frontmatter `type:` is `handoff`, `session-handoff`, `session-starter`, or `prompt`, OR
- filename matches `*Handoff*.md`, `*handoff*.md`, `*-handoff-*.md`, or `next-session-*.md`

The frontmatter form is authoritative, a non-handoff filename with `type: handoff` is still a handoff. The filename form is a backstop for files written without frontmatter.

## Location convention

Active handoffs live in `⚙️ Meta/Handoffs/`. Consumed (shipped) handoffs that you want to keep as history move to `⚙️ Meta/Handoffs/Archive/`. Never put handoffs at `⚙️ Meta/` top-level, that's reserved for permanent reference docs.

Team or shared vaults follow the same convention under their own `⚙️ Meta/` root.

## Required frontmatter (enforced at write time)

Every handoff file MUST include a non-empty `consumes_when:` field naming the completion signal. Examples:

```yaml
consumes_when: graph reaches >8000 nodes via Stage 5D Option B
consumes_when: claude-meeting-todos repo published on github.com/<your-handle>
consumes_when: production launch complete (calendar bookings, message relays, scope rejections all shipping live)
```

If you cannot name a concrete completion signal, the bridged work is not concrete enough to ship, write a clearer plan first.

**Enforcement.** PreToolUse hook `~/.claude/skills/ai-brain-starter/hooks/validate-handoff-frontmatter.py` blocks any Write, Edit or MultiEdit that produces a handoff file with missing/empty `consumes_when:`. Scope: markdown files inside a vault, where a vault is the nearest ancestor folder holding a `Meta` / `⚙️ Meta` subfolder (`$VAULT_ROOT` applies only when that lookup finds nothing). Writes outside a vault, and any internal error, fail OPEN. Bypass: `HANDOFF_FRONTMATTER_BYPASS=1` (rare).

## Close-time scan (Phase 0c of session-close.md)

```bash
find "⚙️ Meta/Handoffs" -maxdepth 1 -type f -name "*.md" -print
```

Also scan top-level `⚙️ Meta/*.md` as a backstop for handoffs that escaped the convention (filename match or frontmatter `type:`).

For each match, classify into one bucket:

1. **Read or referenced this session AND `consumes_when` signal reached** — propose `git mv` to `Handoffs/Archive/` (or `rm` if not worth keeping). Confirm reached evidence in the proposal (graph stats, files created, repo published). On confirm, move/delete.
2. **Read this session AND work still in flight** — keep. Append `last_consumed: YYYY-MM-DD` to frontmatter so next close re-evaluates with fresh context.
3. **Not touched this session AND mtime > 14 days** — audit before reporting. Compare `consumes_when` signal against current vault/repo state. If reached, propose archive/delete. If not, name the specific blocker.
4. **Not touched AND mtime <= 14 days** — leave alone.

**Never delete or move without explicit user confirm.** File deletion is destructive, auto mode does not authorize it. Always present: filename, mtime, `consumes_when` signal, current state evidence, then ask.

## Generalization (any consumable artifact)

The `consumes_when:` field is a general-purpose signal: any `*.md` with that frontmatter participates in the same lifecycle, regardless of filename or type. PRD drafts, one-off prompt scratchpads, journal seeds, contribution drafts, "Next X Experiments" scratch files, all use the same closed loop.

**Two archive locations:**
- `⚙️ Meta/Handoffs/Archive/` — for handoff-typed files specifically (frontmatter `type:` matches `handoff|session-handoff|session-starter|prompt`).
- `⚙️ Meta/Archive/` — for all other consumed artifacts with `consumes_when:`. Examples: journal seeds whose entries were folded in, contribution drafts whose issues were posted upstream, one-off PRDs whose deliverables shipped.

**Close-time scan extends to both:**
```bash
find "⚙️ Meta/Handoffs" "⚙️ Meta/Archive" -maxdepth 1 -type f -name "*.md"
find "⚙️ Meta" -maxdepth 1 -type f \( -iname "*handoff*.md" -o -iname "next-session-*.md" \)
grep -lE "^consumes_when:" "⚙️ Meta"/*.md 2>/dev/null
```

The `consumes_when:` grep catches non-handoff transients that escaped the filename heuristic. Active artifacts stay at top-level until consumed; once consumed, they move to the appropriate `Archive/` for posterity (preserves the proposal text, the seed quotes, the original framing) without polluting the active workspace.

**The hook is scoped to handoffs only.** Auto-blocking every `*.md` with `consumes_when:` would surface too many false positives. The hook covers the high-leverage handoff case where forgotten metadata reliably causes accumulation; for non-handoff `consumes_when:` artifacts, the close-time scan is the enforcement layer.

## Why this exists

After enough sessions, vaults accumulate stale handoff files at the top of `⚙️ Meta/` from work that shipped weeks earlier. They create drift between recorded state and current state, and a future session reads them as still-active context. The metadata-at-create + folder-convention + close-time-scan combination closes the loop end-to-end.
