---
name: warn-bare-gh-pr-mutation
enabled: true
event: bash
action: warn
conditions:
  - field: command
    operator: regex_match
    pattern: '\bgh\s+(?:-{1,2}[\w-]+(?:[= ]\S+)?\s+)*pr\s+(?:-{1,2}[\w-]+(?:[= ]\S+)?\s+)*(create|merge)\b'
---

**Bare `gh pr create` / `gh pr merge` outside the coordination wrapper.**

This command mutates SHARED EXTERNAL state (GitHub PRs). On a machine running
concurrent agent sessions, a bare mutation has three problems the local git
guards cannot see:

1. **No attribution.** Nothing on the resulting PR says which session made it.
2. **No read-before-write.** If a PR already exists for this branch, you mint
   a competing one instead of seeing it.
3. **No coordination.** Two sessions targeting the same branch race; nobody
   yields.

Use the wrapper instead - same arguments, plus the session trailer:

```
python3 scripts/gh-safe.py pr create --title "..." --body "..."
python3 scripts/gh-safe.py pr merge <pr> --squash
```

It requires a session id (`$CLAUDE_SESSION_ID` or `--session`), embeds the
`session: <id>` trailer in the PR body (create) or a claim comment (merge),
lists existing PRs first and refuses duplicates, and takes a repo+branch
advisory lock so a concurrent session yields instead of colliding.

Convention + exit codes: `docs/SESSION_COORDINATION.md`.

This is a WARN, not a block: reads that merely mention the words (or a
human intentionally driving gh by hand) proceed after the nudge. If you are
seeing this on your own deliberate one-off, carry on - but agent-driven
sessions should treat it as a wrong-tool signal.

## Why this rule exists

Observed incident class: with many concurrent sessions live, a PR appeared
that no session's transcript created, and a ticket moved state with no work
behind it. The unattributed-mutation half is structurally preventable at the
tool boundary: warn at the moment a bare external mutation is about to run,
point at the wrapper that makes it attributable and race-safe.

Bug class: **UNATTRIBUTED-EXTERNAL-MUTATION** - shared state changed by an
agent with no artifact-visible actor and no coordination, indistinguishable
from a rogue actor after the fact.
