---
type: doc
title: Bi-Temporal Resolver
last_updated: 2026-05-01
---

# Bi-Temporal Resolver

The catalect Bi-Temporal Resolver is the layer that sits on top of the typed-memory schemas and answers three questions cheaply: which rule applies to this query, is that rule still fresh, and which of two competing rules wins. It is composed of five scripts, one query skill, plus the rendered index file.

## The seven files

1. `scripts/resolver-build.py` is the aggregator. It walks `Meta/Decisions/`, `Meta/Workflows/`, `Meta/Exceptions/`, `Meta/Facts/` inside the configured vault root, parses YAML frontmatter on every `.md` file, derives a status per row, runs conflict detection across rules sharing the same `pattern`, runs branch-merge detection across decisions whose `last_verified` is within 30 days of each other, and emits `Meta/RESOLVER.md`. CLI: `--vault-root PATH --out PATH --dry-run`.
2. `scripts/resolver-conflict-report.py` is the standalone JSON conflict + branch-merge report. Reuses `resolver-build.py`'s detection logic without writing RESOLVER.md. Read-only. Prints a JSON document. Exit 0 = clean, exit 2 = conflicts or branch-merge candidates surfaced, exit 1 = error. CLI: `--vault-root PATH --include-rules`.
3. `scripts/resolver-branch-merge-prompt.py` drafts an operator merge prompt for each parallel-branch pair the aggregator surfaced. Output: `Meta/Branch-Merge-Candidates/<sha8>.md` per pair. Idempotent; resolved prompts are skipped unless `--force`. CLI: `--vault-root PATH --dry-run --force`.
4. `scripts/stale-rule-check.py` is the detector. It computes `(today - last_verified)` for every typed-memory entry and reports anything past `freshness_days`. Exit code 2 when stale rules surface, 3 when entries could not be evaluated at all (unparseable frontmatter, no `last_verified`, no `freshness_days`), 0 when clean. 3 is distinct on purpose: an entry that cannot be judged has not passed, and a corpus of undated rules must not read as a clean board. CLI: `--vault-root PATH --threshold-days N --json`.
5. `scripts/proposed-update-drafter.py` is the downstream surfacer. Given a changed source file, it greps the vault for wikilink and path references, then prepends an idempotent proposed-update HTML comment block at the top of each downstream file. CLI: `--vault-root PATH --changed-file PATH --dry-run`.
6. `skills/resolver-query/` is the read-only query skill. Trigger `/resolver-query <natural-language-question>`. Parses `Meta/RESOLVER.md`, ranks rules by deterministic token overlap, and returns either a decisive match, a ranked candidate list, or `match_kind: none`. The skill never calls an external LLM; the host Claude session does the natural-language framing.
7. `templates/RESOLVER.md.template` is the single-Read-readable reference for what the rendered index looks like. The aggregator reproduces this shape; the template documents it for humans.

## The bi-temporal data model

Every rule carries two clocks.

- Validity-time is `last_verified` (or, for decisions, `decision_date`). It answers when the rule was last confirmed to describe the world. A rule whose `last_verified` is older than `freshness_days` is `stale`, even if the file was edited yesterday.
- Transaction-time is the git commit time of the source file. It answers when the rule was written into the vault. The aggregator does not currently surface transaction-time directly because the validity clock is what governs trust.

The `under-review` status fires on a different signal: a decision whose `outcome` is filled in but whose `pattern` is still empty. That is the bridge between resolution and lesson-learned.

## Conflict resolution

Two rules conflict when they share a non-empty `pattern` field AND their subjects overlap. Subject is built from the source file's first H1 plus any `topic` or `subject` frontmatter field; overlap is a case-folded keyword match.

When a conflict group surfaces, the rule with the most recent `last_verified` wins (validity-time precedence). Older rules in the group get `status: superseded` plus a `superseded_by` field naming the winner's `rule_id`. The `Meta/RESOLVER.md` output carries:

- A `conflict_count` field in the frontmatter.
- A `## Conflicts` section grouping rules by pattern, naming the winner and listing each superseded entry with its source path and last_verified date.
- A `superseded_by` column in the main Rules table.

Ties on `last_verified` (or rules with no `last_verified` at all) fall back to `rule_id` sort, lowest-first. Rules without a `last_verified` rank as oldest and lose any conflict against a rule with a real date. A standalone JSON view of the same conflict set is available via `scripts/resolver-conflict-report.py`, suitable for cron alerting.

## Supersession vs branch-merge

The resolver distinguishes two flavors of rule contention:

- **Supersession** is the clean case. Two rules share a `pattern` and overlap on subjects, but one is unambiguously newer (older `last_verified` is well outside the branch-merge window OR has a clearly stale freshness signal). Validity-time precedence applies; the older rule is marked `superseded` with a `superseded_by` pointer at the winner. No operator review is needed.
- **Branch-merge** is the parallel-decision case. Two rules share a `pattern`, contradict in `outcome`, and have `last_verified` dates within 30 days of each other. Neither is clearly newer, so the resolver cannot pick a winner via validity-time precedence; both rules ran in parallel, possibly in different teams or threads, and an operator must reconcile them. The aggregator marks each side `branch_merge_with: <other-rule-id>` (status is left unchanged), and `scripts/resolver-branch-merge-prompt.py` writes a per-pair prompt at `Meta/Branch-Merge-Candidates/<sha8>.md` with three operator options: `keep_branch_A`, `keep_branch_B`, or `merge_into_new`. The prompt also includes a merged-decision template the operator can fill in for the third path.

The 30-day window is `BRANCH_MERGE_WINDOW_DAYS` in `resolver-build.py` and tunable. Outside the window, the resolver assumes the older rule is genuinely stale and falls back to supersession.

Branch-merge prompts are idempotent: re-running the prompt drafter on the same pair overwrites the same file. Once the operator resolves a prompt by setting its frontmatter `status:` to `resolved-keep-A`, `resolved-keep-B`, or `resolved-merged`, subsequent drafter runs skip it unless `--force` is passed.

## How the resolver layer composes with the schemas

The schemas in `templates/schemas/` define the YAML frontmatter contract for each typed-memory primitive. The cross-type contract fields (`provenance`, `confidence`, `freshness_days`, `last_verified`, `source_count`) are the substrate the resolver reads. In particular, the resolver depends on `last_verified` and `freshness_days` for the stale check, on `outcome`/`pattern` for the under-review signal, and on `pattern` plus subject tokens (`topic`, `subject`, first H1) for conflict detection.

The resolver does not validate frontmatter; that is the job of `vault-schema-validator.py`. The resolver assumes the validator has already failed loud on malformed YAML. The contract is: validator at write time, resolver at read time.

## Operational use

Rerun `resolver-build.py` after any rule change so `Meta/RESOLVER.md` reflects the current pipeline. Wire `stale-rule-check.py` into the session-close cascade so stale rules surface before they rot. Wire `resolver-conflict-report.py` into a cron job (daily or weekly) so emerging conflicts surface as JSON before they propagate. Run `resolver-branch-merge-prompt.py` whenever the conflict report flags `branch_merge_count > 0` so the operator gets a per-pair prompt instead of a JSON blob. Call `proposed-update-drafter.py` whenever a high-confidence rule changes, so every downstream consumer is forced to either confirm or correct the reference before the staleness clock starts running. Use `/resolver-query` to look up which rule applies to a natural-language question without reading the full index by hand. Together these scripts turn a bag of typed-memory files into a routable, time-aware, conflict-aware, query-friendly policy index.
