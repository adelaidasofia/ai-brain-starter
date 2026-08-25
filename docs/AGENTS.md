# AGENTS.md

A memory runtime for AI agents and teams.

## What this is

A memory runtime for AI agents.

- Vault as substrate.
- Hooks as guardrails.
- Typed primitives.
- MCP plus REST.
- Owner-controlled, portable, version-controlled.

Markdown files on disk are the rows. YAML frontmatter is the contract. Python hooks are the gate. A knowledge graph and an HTTP service are the read surfaces. Bring your own agent.

## Why agents need this

Production AI agents fail in four ways.

- **Hallucination.** The model invents a procedure the company never wrote down.
- **Silent failure.** A violation ships because the validation step never ran.
- **Drift.** The recorded process and the actual process diverged months ago, and no one noticed.
- **Knowledge loss.** Lessons learned inside a session evaporate when the session closes. The next session pays the cost again.

Vector retrieval addresses none of these. Vector retrieval addresses recall over a corpus.

The substrate question is upstream of retrieval:

- Where does the corpus come from.
- Who can write to it.
- What shape entries take.
- When entries expire.
- How the agent knows which entry is fresh.

A markdown vault plus typed primitives plus deterministic guardrails is what survives the load when an agent moves from demo to production.

The five architectural pillars that prevent each failure mode live in [`RELIABILITY_MANIFESTO.md`](../RELIABILITY_MANIFESTO.md). This document positions the runtime. The manifesto defines the substrate. Read both.

Pillar names: vault as ground truth, hooks as deterministic guardrails, rule extraction from existing artifacts, decision-outcome trail, session-close cascade.

## Typed memory primitives

Six typed entries, each backed by a JSON Schema in [`templates/schemas/`](../templates/schemas/).

- Frontmatter is the row.
- The file body is prose for humans.
- The schema is the contract a downstream consumer reads.

| Type | Required fields | Purpose |
|---|---|---|
| `decision` | `type`, `decision_id`, `date` | A logged choice point. Captures stakes, rationale, alternatives. Outcome field starts blank and gets filled in later. |
| `fact` | `type`, `claim` | A verifiable claim about the world. Person role, market number, policy, threshold. The atom an agent cites when asserting something. |
| `workflow` | `type`, `name` | A named, repeatable process. Ordered steps, owners, approvals, handoffs, failure modes, edge cases. |
| `exception` | `type`, `exception_summary` | A documented deviation from a workflow, fact, or rule, plus the conditions under which the deviation applies. |
| `relationship` | `type`, `source_entity`, `target_entity` | A typed edge between two entities. Directed, weighted, labeled. The unit a graph build consumes. |
| `outcome` | `type`, `decision_id` | The observed result of a previously logged decision. Joins on `decision_id`. Carries `vs_expected` enum and `lessons_learned`. |

All six share the cross-type frontmatter contract: provenance, confidence, freshness_days, last_verified, source_count.

The schemas are deliberately permissive:

- `additionalProperties: true`.
- Only consumer-critical fields under `required`.
- Most fields nullable so legacy entries continue to validate.

Full schema documentation, with the silent-failure mode each schema's `_comment` names: [`templates/schemas/README.md`](../templates/schemas/README.md).

Two non-schema primitives operate alongside these six and have aggregator scripts in production:

- `journal`. A daily entry. Lives in `Journals/`. Drives the weekly and monthly digests.
- `session`. A worktree-scoped record of one Claude Code session. Drives `Last Session.md` rebuilds via `aggregate-sessions.py`.

## Reading the memory

Two surfaces today.

**graph-query MCP.** Claude-Code-native.

- Lives in vault `.mcp.json`.
- Loads a NetworkX knowledge graph at startup.
- Serves seven tools: `search_nodes`, `get_neighbors`, `find_path`, `query_subgraph`, `get_node_info`, `get_community_members`, `get_top_nodes`.
- Scope parameter switches between `personal` and `team` graphs.
- Node IDs use `c_` prefix by convention (e.g. `c_fear`, `c_decision_log`).

Use this from any Claude Code session for targeted graph lookups in place of reading a 500KB graph report into context.

**HTTP REST API.** A read-only stub at [`services/memory-api/`](../services/memory-api/).

Eight endpoints mirror the MCP one-to-one:

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Liveness check + which scopes loaded |
| GET | `/search` | Substring match on node id and name |
| GET | `/node/{id}` | Full attributes + top neighbors |
| GET | `/neighbors/{id}` | BFS within `max_hops` |
| GET | `/path` | Shortest path between two nodes |
| GET | `/community/{id}` | Other nodes sharing the community attribute |
| POST | `/subgraph` | JSON body: concepts, max_hops, limit, scope |
| GET | `/top-nodes` | Most-connected nodes by degree |

Auth and runtime properties:

- Bearer token via `MEMORY_API_TOKEN`. Returns 503 on every authed endpoint if unset on the server.
- Returns 401 on every authed endpoint if the client omits the token.
- Generic scopes (`personal`, `team`) for multi-tenant.
- OpenAPI 3.1 spec at `/openapi.json`. Interactive docs at `/docs`.
- In-memory NetworkX, no database, no Redis, no rate limiter.

Failure modes:

- Missing token returns 401.
- Unknown scope returns 400.
- Unknown node id returns 404.
- No path between source and target returns 200 with `path: []`.

Honest framing: a stub. Multi-runtime consumption (any agent, not just Claude) is the explicit roadmap. Multi-runtime execution is not.

The two surfaces share one substrate. The MCP and the HTTP API both read the same `graph.json` produced by `graphify` (NetworkX node-link JSON, edges live under `links`, not `edges`). The schema is the index, the file is the row, the cross-type contract fields are the signals consumers sort and filter on.

## Bi-temporal memory

The substrate is bi-temporal by design. Two clocks run side by side and they answer different questions.

Transaction-time lives in the vault git log. Every commit is an append-only record of when a fact, decision, or workflow was written, edited, or amended in the substrate. Reverting a write does not erase the history. Audit and provenance queries ride this clock.

Validity-time lives in YAML frontmatter. The fields `decision_date` (when the choice was made), `last_verified` (when a human last confirmed the entry), and `observed_at` (inside `provenance` entries, when the source captured the fact) describe when a claim was true in the world. A stale entry can sit in the vault long after its validity window closed.

The two clocks separate the question "what did we believe on day X" (transaction-time replay against git history) from "was that belief still accurate on day X" (validity-time check against frontmatter). Typed primitives carry both clocks on every write.

The operational consequences are two scripts. `scripts/stale-rule-check.py` walks the typed entries, computes `last_verified + freshness_days`, and flags anything past its validity window. The proposed-update drafter writes draft replacement entries with status `proposed` and waits for human approval. `templates/RESOLVER.md.template` documents the resolution shape: which entries are canonical, which are drafts, which are deprecated. The resolver is the read-time aggregator that consumers ask "what does the substrate say right now."

## Writing the memory

Three paths today.

1. **Claude session writes.** The session-close cascade scans the conversation, files captures to `Session Captures.md`, decisions to `Meta/Decisions/`, journal seeds verbatim to the next `/journal` queue, action items to canonical to-do lists. Deterministic. Runs at session end. Nothing stays trapped in chat transcripts. Agent-side enforcement of pillar 5.

2. **Hooks at write time.** Pre-tool-use hooks intercept every `Write` and `Edit`, validate frontmatter against the appropriate schema in `templates/schemas/`, and block on violation. Examples:
    - Timezone-naive calendar event blocked.
    - Contractor task missing required fields blocked.
    - Frontmatter that fails YAML parse blocked before the file lands.
   Hooks are Python checks against the actual write payload. Not LLM-judged. Agent-side enforcement of pillar 2.

3. **Connectors.** [`skills/ingest-github/`](../skills/ingest-github/) is the proven pattern:
    - Pull raw events from a source.
    - Normalize each event into a typed entry.
    - Populate cross-type contract fields on every write.
    - Write one file per row.
   Adding further sources is a matter of writing a normalizer per source. The architecture does not change.

Ingestion endpoints over HTTP are not exposed today. That decision is consulting-vs-SaaS strategy and is deferred.

## The cross-type frontmatter contract

Five shared fields on every typed primitive.

- None required at the schema level (legacy entries pre-date the contract).
- Populated over time as entries are re-extracted, re-verified, joined to sources.

| Field | Type | What it means |
|---|---|---|
| `provenance` | array of objects | Independent sources backing this entry. Each carries `source_type` (one of `slack`, `email`, `notion`, `gdoc`, `whatsapp`, `calendar`, `claude-session`, `manual`), `source_id`, `source_url`, `captured_at` (ISO 8601). Append, never overwrite. |
| `confidence` | number, 0.0 to 1.0 | How sure the system is the entry is accurate. Set at capture time. Re-graded during verification passes. |
| `freshness_days` | integer | Days the entry stays fresh before re-verification is recommended. Volatility-driven: a person's role rotates faster than a market segment definition. |
| `last_verified` | ISO 8601 date | When a human or re-extraction pass last confirmed the entry. |
| `source_count` | integer | Independent sources backing this entry. Derivable from `provenance.length`. Stored for fast filtering. |

Together these fields let any consumer ask three questions cheaply:

- Is this still fresh.
- How sure are we.
- Where did this come from.

That is the substrate for two patterns:

- **Trust-graded retrieval.** The agent prefers high-confidence, recently-verified, multi-source entries when assembling context.
- **Stale-entry surfacing.** A scheduled scan flags entries whose `last_verified + freshness_days < today` and queues them for human re-verification.

Full contract: [`templates/schemas/README.md`](../templates/schemas/README.md).

## The dogfood loop

After every operational unit a writeback skill scans the unit's folder and extracts three categories of typed memory.

Operational units:

- Event production cycle.
- Sprint or on-call shift.
- Deal cycle.
- Client engagement.
- Surgical case or patient encounter.
- Semester.

Three categories extracted:

- **Decisions made under pressure.** What was decided, why, alternatives considered. Outcome field blank.
- **Exceptions taken when the standard runbook did not fit.** Frequency counter increments on each observation. After three observations the system flags it as a request the runbook should accommodate.
- **Playbook deltas surfaced by the post-cycle review.** Status `proposed`. Waits for human approval. Nothing edits the live playbook automatically.

The pattern is generic. The tuning (what counts as a unit, which folder it scans, which exceptions matter) is per-vertical.

Compounding effect:

- After a month the vault holds dozens of decision files with outcome fields, exception files with frequency counters, playbook delta files awaiting review.
- The frequency counter on a recurring exception fires before the human notices the pattern.
- The outcome field on a 60-day-old decision surfaces as a scheduled review item before the founder remembers the decision was made.

Vertical thesis spelled out: [`docs/DOGFOOD.md`](DOGFOOD.md).

## Honest scope

Three things this is not.

**Not a vector DB.**

- The substrate is markdown plus a knowledge graph.
- Vector retrieval rides on top if you add it, against the same files.
- No embeddings table, no Pinecone, no Chroma, no Qdrant shipped in the box.
- Bring your own if you want them.
- Most agents reading this substrate today never need vector retrieval, because graph traversal plus frontmatter filters covers the common queries (path between two concepts, neighbors at N hops, all entries of type X with `confidence > 0.7`).

**Not a multi-tenant SaaS.**

- The runtime runs on the operator's machine.
- The team folder convention (a `team/` directory bidirectionally synced with a separate team vault) is a code primitive, not a tenant boundary.
- Two operators on two machines is two installs, not two tenants.
- The team-vault pattern exists so a co-founder pair or a small operator group can share a substrate without leaking either personal vault.

**Not an agent framework.**

- This is a memory layer agents read and write through.
- No planner, no router, no agent loop, no tool dispatch.
- Bring your own agent: Claude Code, the Anthropic SDK, OpenAI, Gemini, a custom runtime, anything that speaks MCP or HTTP.
- Memory and execution are separate concerns. Confusing the two leads to all-in-one frameworks that lock the operator to one vendor's runtime.

## Architecture coverage

We did not invent these primitives. The catalect "company brain" framing names them. Below is what we shipped versus what is deliberate roadmap. The full build entry, including optimizations applied and acceptance criteria, lives in PRD UUID `97b2c7ad-4c31-46d5-aa49-457006b47ba3`.

| Catalect primitive | Shipped today | File paths | Roadmap | Score / 10 |
|---|---|---|---|---|
| Zero-migration ingestion at scale | Personal ingestion connectors with cross-source ID linking via `entity_ids` | `skills/ingest-github/`, `skills/ingest-youtube/`, `skills/ingest-health/` | Webhook surface, real-time event stream, multi-tenant boundary | 6 |
| Autonomous memory synthesis | Pattern recognition + instinct capture | `skills/patterns/`, `skills/evolve/`, `skills/instinct-export/` | Confidence-weighted auto-promotion past `proposed`, named-entity disambiguation across sources | 5 |
| Bi-temporal resolver | RESOLVER.md primitive + stale-rule check + proposed-update drafter | `templates/RESOLVER.md.template`, `templates/RESOLVER-README.md`, `scripts/stale-rule-check.py` | Validity-time conflict resolution heuristics, branch-merge for parallel decision threads | 6 |
| Structured agentic execution | skill.json schema + frontmatter validator hook | `templates/schemas/skill.json`, `hooks/validate-skill-frontmatter.py` | Runtime enforcement of skill.json contract, capability-scoped sandboxing | 5 |
| Closed-loop learning | post-tool-use learnings hook + episodic-to-procedural promotion | `hooks/post-tool-use-learnings.py`, `scripts/promote-episodic-to-procedural.py` | Cron-runnable consolidation, demotion path for stale procedural rules | 5 |

Scores are honest, not aspirational. A 6 means the primitive is shipped and exercised across at least two integration paths. A 5 means the primitive is shipped and smoke-tested but not yet load-tested across multiple operators.

## Build standards compliance

This build follows the codified standards in [`docs/BUILD_STANDARDS.md`](BUILD_STANDARDS.md) and [`docs/MCP_BUILD_RUNBOOK.md`](MCP_BUILD_RUNBOOK.md). Both documents are part of the repo and define the pre-build checklist, the optimization-decision log, and the cross-reference contract for build artifacts.

Applied during this build:

- Shared utility surface across ingestion connectors so adding a new source costs one normalizer file.
- Idempotent connectors. Same input twice produces the same files. No duplicate writes, no clobbered frontmatter.
- Schema validation hooks for skill.json frontmatter and the cross-type contract on every write.
- Personal-data scrub gate on every public-repo diff before merge, per the codified word-boundary regex rule.
- No-em-dash voice rule applied to all prose in this doc and the PRD.
- Cross-type frontmatter contract populated on every connector write (`provenance`, `confidence`, `freshness_days`, `last_verified`, `source_count`, `memory_class`, `entity_ids`).

Deferred for follow-up builds, named so they do not get lost:

- Existing-implementation audit at `docs/EXISTING-IMPL-AUDIT.md`. Per-source audit deferred to per-source PRDs when each connector hits load.
- Full API surface scan deferred to per-source PRDs. The four connectors shipped here use minimal-viable endpoint coverage.
- Integration test suite at `tests/integration/test_e2e_pipeline.py`: a cross-cutting suite meant to exercise all five primitives in one sequence. Deferred, not yet shipped. `tests/integration/` already holds real per-feature suites today (for example `test_v05_hooks.py`, `test_open_core_boundary.sh`); the single cross-cutting file promised here, plus load-testing across multiple operators, lands in a follow-up build.

## Building with this

Three concrete entry points. Each is a 2-hour task, not a project.

1. **Read the memory.**
    - Install the `graph-query` MCP via vault `.mcp.json`, or
    - Run [`services/memory-api/`](../services/memory-api/) on localhost: `uvicorn app:app --host 127.0.0.1 --port 8765`.
    - The MCP path gives you typed tool calls inside Claude Code.
    - The HTTP path gives you an OpenAPI 3.1 surface any agent runtime can consume.
    - Both speak the same substrate. Curl the OpenAPI doc, generate a client, point any agent at it.

2. **Write the memory.** Write a connector skill. Follow the [`skills/ingest-github/`](../skills/ingest-github/) pattern:
    - A `SKILL.md` describing the trigger.
    - An `ingest.py` that pulls events from the source API and normalizes each event into a typed entry.
    - Cross-type frontmatter (`provenance`, `confidence`, `freshness_days`, `last_verified`, `source_count`) populated on every write.
    - One file per row.
    - The hookify rules in `.claude/hookify.*.md` validate the frontmatter at write time and block malformed writes before they corrupt downstream consumers.

3. **Extend the schemas.** Adding a new typed primitive is three artifacts:
    - A new JSON Schema in `templates/schemas/`.
    - A hookify validation rule that fires on writes to the new primitive's folder.
    - Optionally, an aggregator script following the `aggregate-decisions.py` and `aggregate-sessions.py` pattern. Walk the source folder, parse YAML, validate against schema, sort by date or confidence, concatenate into the rebuild target.

The schema is the index, the file is the row, the aggregator is the read.

## Status, gaps, roadmap

**Today.**

- Read-only memory API stub.
- Six typed primitives backed by JSON Schema.
- Two operational primitives (`journal`, `session`) backed by aggregator scripts.
- Cross-type contract enforced at hook write-time.
- One working connector (Slack).
- Aggregators for decisions and sessions in production.
- The `graph-query` MCP shipped and stable across many Claude Code sessions.
- Dogfood loop running on a real operating company across multiple operational cycles.

**Sixty-day commitment.**

- Connector marketplace pattern proven across at least three sources (Slack plus two others, target: Notion and either Email or GDrive).
- HTTP API and OpenAPI spec live and consumed by at least one non-Claude runtime.
- Edge-case auto-capture pattern proven on Slack data: when a message contains a decision phrase, an exception phrase, or a playbook-gap phrase, a typed entry drafts automatically and queues for human approval before landing in canonical storage.
- Auto-capture is the bridge from "agents read the substrate" to "agents write the substrate without losing the audit trail."

**Not on this roadmap.**

- SaaS deployment.
- Multi-tenant runtime.
- Ingestion endpoints over HTTP.
- Embedded vector store.
- Agent framework.
- Cloud-hosted graph database.

These are deliberate omissions, not gaps. The substrate question is upstream of those choices, and the substrate has to harden before the choices get made. Operators who want any of those today should write them on top of the existing substrate, not wait for a managed offering.

---

For more:

- [`README.md`](../README.md): founder-voice positioning, install path, use-case narrative.
- [`RELIABILITY_MANIFESTO.md`](../RELIABILITY_MANIFESTO.md): substrate philosophy, five architectural pillars in full.
- [`docs/MEMORY_SYSTEM.md`](MEMORY_SYSTEM.md): typed-memory primer aimed at vault operators.
- [`docs/DOGFOOD.md`](DOGFOOD.md): dogfood vertical thesis.
- [`templates/schemas/README.md`](../templates/schemas/README.md): schema contract in full.
- [`services/memory-api/README.md`](../services/memory-api/README.md): HTTP runtime setup.
