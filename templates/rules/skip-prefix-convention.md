---
name: skip-prefix-convention
type: rule
status: active
bug_class: SKIP-PREFIX-LEAKED-TO-VAULT-PERSISTENCE
---

# Skip-prefix convention: say it without saving it

## The rule

A line whose first non-whitespace token is `__SKIP` is something you said to the
assistant but do **not** want written down. It must be stripped before any write
to a life-record path, and the assistant must say that it dropped it.

```
Today went well. Shipped the migration.
__SKIP the real reason I was upset was the call with my sister
Tomorrow: finish the runbook.
```

The journal entry gets the first and third lines. The second never lands.

## Why this exists

A brain that saves everything is only useful if you can speak freely to it. The
moment you start self-censoring because anything you type might end up in a file
you will re-read, share, or index, you stop telling it the truth, and a brain you
lie to is worth less than no brain at all.

`__SKIP` is the pressure valve: a way to think out loud, mid-entry, without that
thought becoming a record.

The convention borrows from a habit people already have. In most shells a command
typed with a leading space stays out of history. Same instinct, one character more
deliberate, because a journal is easier to write into by accident than a shell is.

## Protocol

When the assistant sees a `__SKIP` line in content bound for a life-record path:

1. **Strip** every such line before the write.
2. **Confirm** one line per dropped item, so the drop is never silent:
   `Dropped __SKIP line 2 (token preview: the real reason I was...)`
3. **Do not paraphrase** the dropped content back into the entry. The whole point
   is that the thought does not land.
4. **Do not log it** anywhere else either: no session capture, no hook log, no
   "just in case" copy.

Step 2 is not decoration. Silent stripping is its own failure: you cannot tell a
line that was dropped from a line the assistant never noticed, so a silent skip
turns a privacy primitive into a trust hole.

## The hard guard

`hooks/block-skip-prefix-in-vault-write.py` (PreToolUse, `Write|Edit|MultiEdit`
and `Bash`) **blocks** the write when a `__SKIP` line survives to the tool call.

It blocks rather than warns because a persisted line cannot be un-persisted. Once
it is in the file it is also in git history and in every index that reads the
vault. Privacy primitives fail closed.

It fires on:

- `Write` — content carries the token
- `Edit` / `MultiEdit` — **new** content carries it (a `__SKIP` in `old_string` is
  the assistant *removing* the marker, which must never be blocked)
- `Bash` — a redirect or heredoc writing into a life-record path

## Scope

Life-record surfaces, the places a person narrates their own life. By default:

| Surface | Path |
|---|---|
| Journals | `Journals/<Month YYYY>/*.md` |
| Coaching records | `Coaching Sessions/` |
| Verbatim capture | `Processing Notes - *.md` (any folder) |
| Relational syncs | `Co-founder Syncs/`, `Personal Coaching/`, `Decision Reviews/` |
| Panel aggregator | `Panel Feedback Log.md` |

Deliberately **not** in scope: rule files, runbooks, code, and imported
third-party transcripts (Granola, WhatsApp, email). You never had a `__SKIP`
opportunity in someone else's transcript, so the token there is a false positive.

### Extending it to your own vault

Different vault, different folder names. Add your own without editing the hook:

```bash
export SKIP_PREFIX_EXTRA_PATHS='Diario/:Therapy Notes/:Morning Pages/'
```

Colon-separated regexes, matched against the write path. An unparseable pattern is
reported on stderr and skipped, never silently dropped, so a typo cannot quietly
shrink your coverage.

## Bypass

`SKIP_PREFIX_BYPASS=1`, honored from the session env and as an inline
`SKIP_PREFIX_BYPASS=1 <cmd>` prefix on the Bash path.

Legitimate uses: editing this rule, the hook, or a skill file that quotes the
literal token. Never for real journal or coaching content. Files whose names mark
them as self-referential (`SKILL.md`, `CLAUDE.md`, `AGENTS.md`, this rule, the
hook itself) are exempt automatically.

## Bug class

`SKIP-PREFIX-LEAKED-TO-VAULT-PERSISTENCE`. Family:
`ARTIFACT-WITHOUT-USER-CONSENT` — content the user did not authorize for
persistence reaches a persisted file.

Test: `tests/integration/test_skip_prefix_guard.sh` (20 assertions, negative
control on every claim, including that `__SKIPPED` is not the token and that a
removal edit is never blocked).
