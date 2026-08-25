# Session coordination for external mutations (GitHub, Linear, Slack)

On a machine running many concurrent agent sessions, two kinds of shared-state
damage show up that no local-git guard can see: a PR appears that no session's
transcript created, and a tracker ticket changes state with zero work behind
it. Both are EXTERNAL mutations - they live in GitHub, Linear, or Slack, not in
any working tree - so they slip past every repo-scoped primitive:

| Primitive | Covers | Stops at |
|---|---|---|
| `hooks/session-lock.py` | git verbs vs a live sibling session | the repo boundary |
| `hooks/block-git-mutation-mid-operation.py` | mutations into an in-flight rebase/merge | the repo boundary |
| `scripts/_session_close_guard.sh` | concurrent close-cascade git snapshots | the repo boundary |

This document adds the two missing pieces for the world OUTSIDE the repo:
an **attribution trailer** so every external mutation is traceable to the
session that made it from the artifact alone, and a **coordination wrapper**
(`scripts/gh-safe.py`) so two sessions cannot silently mint competing
mutations against the same branch or PR.

## The attribution trailer

One convention, adoptable by any tool that writes to a shared surface:

```
session: <id>
```

- The trailer is the **last non-empty line** of the artifact's body text,
  preceded by a blank line.
- `<id>` is the acting session's id - Claude Code exposes it as
  `$CLAUDE_SESSION_ID` (and as `session_id` in hook payloads). Charset:
  `[A-Za-z0-9._:-]`.
- Detection regex (for auditors and dedup tooling): `(?mi)^session:\s*(\S+)\s*$`
- A missing id is a refusal, never a silent omission. An artifact with no
  trailer is, by convention, human-made - which is exactly what makes an
  unattributed agent mutation detectable.

Where the trailer goes, per surface:

| Mutation | Trailer location |
|---|---|
| PR create | PR body (embedded by `gh-safe`) |
| PR merge | claim comment on the PR (posted by `gh-safe` before merging); also appended to the merge-commit body when `--body` is used |
| Linear status change | a claim comment posted in the same motion as the move |
| Slack post | last line of the post |

## `scripts/gh-safe.py` - the wrapper for `gh pr create` / `gh pr merge`

```
python3 scripts/gh-safe.py pr create --title "..." --body "..." [gh args]
python3 scripts/gh-safe.py pr merge <pr> --squash [gh args]
```

Alias it once (`alias gh-safe='python3 <repo>/scripts/gh-safe.py'`) and use it
everywhere a session mutates GitHub PR state. Reads (`gh pr list|view|checks`)
need no wrapper.

Three guarantees, in order:

1. **Attribution, fail loud.** The session id comes from `--session <id>` or
   `$CLAUDE_SESSION_ID`. Neither present: exit 2 before any gh call. Creates
   require `--body`/`--body-file` so the trailer can be embedded (`--fill`
   alone cannot carry it). Merges post the claim comment first; if the comment
   fails, the merge is refused rather than performed unattributed.
2. **Read-before-write, fail closed, inside the lock.** Create runs
   `gh pr list --head <branch> --state open` first and refuses (exit 4) if a
   PR exists, printing it. Merge re-reads the PR inside the lock and refuses
   (exit 4) unless it is OPEN. A failed read refuses to mutate blind (exit 1).
   Because the read happens inside the critical section, the second of two
   racing sessions sees the first one's PR instead of racing it.
3. **Advisory lock, one yields.** A mkdir-based lock keyed by repo+branch
   under the user temp dir - atomic on POSIX and Windows, no `flock(1)`
   (absent on stock macOS), no `fcntl` (absent on Windows). The loser does not
   wait: it exits 3 immediately, naming the holder session, so the calling
   agent can decide what to do. Stale locks (owner older than
   `GH_SAFE_LOCK_STALE_SEC`, default 600s, or a dead same-host pid on POSIX)
   are reclaimed automatically.

| Exit | Meaning |
|---|---|
| 0 | wrapped gh command succeeded |
| 2 | usage / missing session id / body cannot carry the trailer |
| 3 | yielded: another session holds the repo+branch lock |
| 4 | refused by read-before-write (PR exists / PR not OPEN) |
| 1 | a pre-write read failed (refusing to mutate blind) |
| other | the wrapped gh command's own exit code |

Env: `CLAUDE_SESSION_ID` (id source), `GH_SAFE_LOCK_DIR` (lock root, default
system temp dir), `GH_SAFE_LOCK_STALE_SEC` (stale reclaim age, default 600).

There is deliberately **no bypass env**: the escape hatch for a wedged lock is
the stale reclaim, and a "just this once" bypass is how two sessions mint
competing PRs again.

Negative controls: `tests/integration/test_gh_safe_session_coordination.sh`
races two concurrent creates against a PATH-shimmed fake gh (the suite never
talks to real GitHub) and asserts exactly one proceeds, one yields, and the
artifact carries the trailer.

## Warning on bare `gh pr create|merge`

`templates/hookify-rules/hookify.warn-bare-gh-pr-mutation.local.md` is an
opt-in hookify rule that warns when a bare `gh pr create` / `gh pr merge` is
about to run outside the wrapper. Copy it to your `.claude/` directory to
activate (see `templates/hookify-rules/README.md`).

## Policy: should a session ever move ticket status autonomously?

Recommendation, deliberately unenforced for now: **a session may move a
tracker ticket's status only when the move is accompanied, in the same
motion, by a claim comment carrying the session trailer** - and a move into a
review/done state must also link the artifact (the PR) that justifies it.
Status moves with no claim comment are reserved for humans.

Why this shape: the incident's second half was a ticket moved In Progress to
In Review with zero work behind it. Requiring an artifact-visible claim makes
a phantom move either attributable (the trailer names the session) or
impossible (no comment, no move - so an unaccompanied move is immediately
recognizable as human or rogue). Enforcement is not built on purpose: adopt
the convention first; build an auditor on the tracker's webhook only if
violations recur after adoption.
