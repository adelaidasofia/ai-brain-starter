# The Instinct Engine

A self-improving memory layer for your second brain. It turns flat-file agent
memories (`feedback_*.md` / `discovery_*.md`) into a **confidence-weighted,
decaying, project-scoped, portable** instinct library — and captures 100% of
tool calls deterministically so pattern extraction stops being guesswork.

Before this engine, `/patterns` reconstructed "what happened this session" by
re-reading the transcript in context — probabilistic (~50-80%) and lossy — and
memories had only a categorical `strength:` (explicit / correction / implicit),
no number, no decay, no portability. The Instinct Engine adds the six things
that were missing.

> **Provenance.** The patterns here were derived from an audit of
> [affaan-m/ECC](https://github.com/affaan-m/ECC) (`continuous-learning-v2`,
> `/evolve`, `/instinct-import`) and **reimplemented clean** per the
> license-hygiene rule — pattern adopted, code original.

---

## 1. Confidence + decay

Every instinct carries four managed frontmatter keys (added by `backfill`,
never clobbering your existing keys or body):

```yaml
confidence: 0.90         # 0.0–1.0, effective belief in this instinct
observations: 4          # times reinforced
last_seen: 2026-05-29    # last reinforce/correct/decay date
project_id: global       # scope (see §3)
exposures: 11            # sessions this was actually injected into (§7)
last_exercised: 2026-08-27  # last such session (absent if never)
evidence: reinforced     # what `confidence` is BASED ON (§7)
```

`evidence` is the field that keeps the number honest. A `0.82` that came out of
the seed table because the prose contained "codified" and a `0.82` that climbed
there across exposures are indistinguishable once written — and a pack that
exports both as "confidence" is claiming evidence it does not have. Values:
`seed` (never exercised — the number is the prior), `exercised` (injected into
real sessions, not yet promoted), `reinforced` (crossed the gate at least
once), `corrected` (marked wrong; outranks the others).

**Seeding** maps the existing `strength:` taxonomy onto a number:

| signal | seed confidence |
|---|---|
| `strength: explicit` (user stated it verbatim) | 0.90 |
| `strength: correction` (user corrected an action) | 0.75 |
| `strength: implicit` (inferred, unconfirmed) | 0.50 |
| no strength · `feedback_*` with hard-rule language (never / always / banned / codified / must) | 0.82 |
| no strength · `feedback_*` (a codified preference) | 0.72 |
| no strength · `discovery_*` (an audit / finding) | 0.60 |
| no strength · other | 0.60 |

Most memories never carried a `strength:` label, so the type/content seed is
what gives the engine real signal on day one. `instinct.py reseed` recomputes
this seed for instincts that have no `strength:` and have never been reinforced
(it never resets a strengthened or reinforced instinct).

**Bidirectional update** (the rule ECC states as "increases when repeatedly
observed / decreases when corrected / decreases when unseen"):

- **reinforce** — `c' = c + 0.15·(1 − c)` (climbs with diminishing returns; ceiling 0.99).
- **correct** — `c' = max(0.05, c · 0.5)` (sharp, recoverable halving).
- **decay** — flat for a 30-day grace window, then a 180-day half-life curve
  on time since `last_seen`. Non-compounding: decay applies the true elapsed
  staleness once and advances `last_seen`, so running it daily never
  double-erodes.

The CLI does the math; `/patterns` decides WHICH instinct to reinforce or
correct based on the observation ledger + the conversation.

---

## 2. The 100% observe loop

`hooks/observe-tool-calls.py` is a `PreToolUse` hook that fires on **every**
matched tool call and appends one scrubbed JSON line to
`~/.claude/instinct/observations.jsonl`:

```json
{"ts":"2026-05-29T23:40:00Z","session":"a1b2c3d4","project":"repo:my-app","tool":"Bash","action":"bash:git","detail":"git status"}
```

It is built to three hard contracts:

1. **Never blocks** — always emits the neutral passthrough, even on its own
   internal error. A ledger must never degrade a real tool call.
2. **Fast** — no subprocess on the hot path; the project key comes from a cheap
   filesystem walk.
3. **Sensitive-path-safe** — logs the tool + a COARSE action + a short detail,
   never file CONTENT; suppresses detail for secret-bearing paths
   (`.env`, `admin.env`, `*.key`, `.ssh/`, …); runs every captured string
   through a secret scrubber (AWS/GitHub/Stripe/Anthropic/OpenAI/npm patterns +
   `key=`/`token=`/`Bearer` forms).

This is **complementary to** `post-tool-use-learnings.py`, which captures only
failures + explicit `<learning>` annotations as episodic notes. The observe
ledger is the full, scrubbed tool-call stream that `/patterns` reads instead of
re-scanning the transcript.

---

## 3. Project scoping

`project_id` isolates instincts so a repo-specific convention does not bleed
into unrelated work:

- `global` — applies everywhere (the default; all existing memories backfill to this).
- `personal-vault` — the vault's own instincts.
- `repo:<name>` / a remote-url hash — a specific code repo.

A context loader (and `export`) surfaces `project_id == current OR global`, so
project isolation is **opt-in for future instincts** and **never hides**
anything that is currently global.

---

## 4. /evolve — promote a cluster into a structure

When a domain accumulates several high-confidence instincts, `/evolve` proposes
promoting them into ONE structure:

```bash
python3 scripts/instinct.py evolve
```

Clusters instincts by inferred domain; any cluster with **≥ 2 instincts and
median confidence ≥ 0.80** gets a proposed-skill scaffold written to
`⚙️ Meta/Instinct Proposals/`. Promotion to a real Command/Skill/Agent is a
human judgment call — the scaffold is a starting point, not an auto-created skill.

---

## 5. Portable export / import

```bash
python3 scripts/instinct.py export --min-confidence 0.70 --out pack.yaml
python3 scripts/instinct.py import pack.yaml --dry-run   # review
python3 scripts/instinct.py import pack.yaml             # apply
```

The pack unit is one instinct: `id / trigger / confidence / domain /
source_repo` + `action` + `evidence`. Import is **confidence-gated**: a
higher-confidence incoming instinct updates the local one, an equal-or-lower
one is skipped, and a brand-new one lands in `inherited/` (tagged
`inherited: true`, `observations: 0`).

---

## 6. CLI reference

```
python3 scripts/instinct.py backfill [--dry-run] [--no-backup]
python3 scripts/instinct.py reseed   [--dry-run] [--no-backup]
python3 scripts/instinct.py reinforce <slug>
python3 scripts/instinct.py correct   <slug>
python3 scripts/instinct.py decay     [--dry-run]
python3 scripts/instinct.py promote   [--dry-run] [--every N] [--min-session-calls N] [--no-decay] [--reset-state]
python3 scripts/instinct.py recompute [--limit N]      # decay + report
python3 scripts/instinct.py report    [--project P] [--min-confidence F] [--stale] [--json] [--limit N]
python3 scripts/instinct.py export    [--project P] [--min-confidence F] [--all] [--out FILE]
python3 scripts/instinct.py import    FILE [--dry-run]
python3 scripts/instinct.py evolve    [--out DIR]
```

Memory dir resolves from `--memory-dir` → `$INSTINCT_MEMORY_DIR` → an upward
walk for `*Meta/Agent Memory` → the default vault path.

---

## 7. Promotion — closing the loop automatically

§1 describes a bidirectional update, but both directions are **manual**:
`/patterns` Step 4 decides what to reinforce, and a human has to invoke
`/patterns`. Measured on a real 665-instinct store after three months: **12
instincts above the 0.80 injection floor, 11 of them still at
`observations: 1`; 342 with no stored confidence at all; 248 of the managed
files stamped with a single backfill date.** The observe ledger held 20,678
lines. `OBSERVATIONS_PATH` was read by nothing. Every number in the store was
the seed it was born with — which means the top-N that gets injected every
session was decided by *whether a memory's prose happened to contain the word
"never"*, and never changed after.

`promote` is the scheduled pass that closes it.

```bash
python3 scripts/instinct.py promote --dry-run   # see what it would credit
./scripts/install-instinct-promote-daemon.sh /abs/path/to/vault   # daily, 04:20
```

**The signal.** The paid runtime solved this first: its learning loop treats a
retrieval **citation** — the memory was actually pulled into an answer — as the
observation, gates promotion on a count of them, and climbs asymptotically so a
feed of positives cannot run away. The substrate's exact equivalent is an
**injection**: the SessionStart hook selected this instinct and put it in front
of the agent. That selection used to be computed and thrown away; it is now
appended to `~/.claude/instinct/injections.jsonl`, which is the join key the
engine never had.

**What it will not do, stated exactly.** An exposure records that the instinct
was PUT IN FRONT OF THE AGENT — nothing more. No correction signal exists
anywhere on disk, so this does not and cannot mean "it was used and nothing
contradicted it"; that predicate is unevaluated. Confidence here measures
**exercise, not correctness**, which is exactly why `evidence` exists as a
separate field. It never auto-`correct`s: automating only the *upward*
direction would manufacture 0.99s across the whole library — a different
fiction, and a worse one, because it would look measured. Downward pressure
comes from decay, which is real and observable.

**The gates**, each of which exists because the naive version is dishonest:

| gate | why |
|---|---|
| 1 reinforce step per **3** exposures (`--every`) | promotion is intentional, not runaway |
| session must have made **5+** tool calls (`--min-session-calls`) | a session that started and died proves nothing about what it loaded |
| one exposure per instinct **per session** | multi-segment sessions (resume, post-compact) cannot triple-count |
| records younger than **60 min** are skipped | a still-running session would be credited for what it has loaded so far and never revisited |
| already-credited sessions tracked in `promote-state.json` | re-runs are idempotent — the same ledger twice credits nothing |
| a `cursor_ts` high-water mark alongside that set | the set is bounded, so a long-lived install drops its oldest ids; without the cursor, a ledger record that outlived its own id would be credited again |
| a **corrupt** state file raises; only an **absent** one is a fresh start | reading "unreadable" as "nothing credited yet" would re-credit every session still in the ledger. Absent and failed are different answers — pass `--reset-state` to start over deliberately |
| an `O_EXCL` lock around the pass, with an atomic rename-aside stale break | the daily job and a hand-run overlapping would both credit the same records. `stat` → `unlink` → `create` is not enough: a loser can delete the winner's fresh lock |
| the state is written **before** the instinct files, and a failed write **raises** | the state write is what makes a re-run idempotent, so it must land before the changes it accounts for. Written last and fail-open, any crash after the first file leaves those files bumped and the sessions uncredited — measured at +0.015 confidence per repeat, climbing to the ceiling while every log line reads healthy |
| an existing-but-unreadable ledger raises | "cannot read" is not "no evidence yet"; a scheduled caller reads the second as healthy forever |
| a record with no parseable `ts` is skipped once a cursor exists | it cannot be checked against the cursor, so it is unverifiable rather than exempt — otherwise this class re-credits forever after the session set truncates |
| ledger slugs matching no file are named and NOT counted | otherwise the headline asserts exposures that landed nowhere |
| `--every` must be >= 1 | a gate of 0 accumulates exposures, promotes nothing, and reports a clean run |

**Exploration.** Ranking purely by confidence is a closed loop: only injected
instincts earn exposures, only exposed instincts get promoted, so the top-N
freezes and the rest of the library can never acquire evidence no matter how
good it is. The injection hook therefore spends a minority of its budget
(`INSTINCT_INJECT_EXPLORE`, default 3 of 12) on in-scope instincts *below* the
floor with the fewest exposures, rotated by session id so different sessions
sample different candidates. That rotation is bounded to a pool of the
least-exercised (`INSTINCT_INJECT_EXPLORE_POOL`, default 24) — rotating the
*whole* sorted list makes the sort inert: measured over 4000 sessions against
650 below-floor candidates, only 3.5% of picks landed on the 24 least-exercised
and rank 649 was sampled as often as rank 0. Bounded, that figure is 100%. Explore picks are **labelled as unproven** in the
injected block — an instinct under evaluation must not read like a confirmed
one.

**Liveness.** Each run stamps `last_run` in
`~/.claude/instinct/promote-state.json`. If that is more than two days old the
job has stopped: a scheduled maintenance pass that dies and one with nothing to
do both print nothing.

---

## 8. Safety + tests

- Every managed-field write keeps a one-time `<file>.bak-instinct` snapshot.
- Edits are **surgical**: only the four managed keys change; all other
  frontmatter lines and the entire body are byte-preserved.
- Runs are idempotent — a second identical run writes nothing.
- `python3 tests/test_instinct.py` covers the math, surgical editing,
  backfill/correct/reinforce, export/import round-trip, evolve, and project
  scoping. `python3 hooks/observe-tool-calls.py --self-test` covers capture +
  redaction.
