---
name: evolve
description: Use when many related hardened instincts (high-confidence feedback_*/discovery_* memories) have piled up in one domain, when the user runs /evolve or asks to promote, cluster, graduate, or consolidate accumulated instincts into a Command, Skill, or Agent, or when files in Instinct Proposals need review. Part of the Instinct Engine. Not for one-off pattern capture (use patterns), daily journaling, or sharing instinct packs (use instinct-export / instinct-import).
trigger: /evolve
---

# /evolve — promote a cluster of instincts into a structure

> **`{SKILL_DIR}`** = this skill's own folder (locally: the directory this SKILL.md lives in; a served brain substitutes the real absolute path before you read this). Shared starter files live at the repo root two levels up: `{SKILL_DIR}/../..`. If a path does not resolve, name the missing file and stop — never guess another location.

When many related, high-confidence instincts pile up in one domain, that is a
signal to promote them into ONE reusable structure instead of leaving them as
loose memories. `/evolve` finds those clusters deterministically and drafts a
proposal you refine.

ECC source pattern: "/evolve clusters related instincts into higher-level
structures: Commands / Skills / Agents." Reimplemented clean per license-hygiene.

## Step 1 — run the clusterer (deterministic, zero LLM cost)

```bash
python3 "{SKILL_DIR}/../../scripts/instinct.py" evolve
```

It groups every instinct by inferred `domain`, computes each cluster's median
effective confidence, and for clusters that clear the propose bar
(>= 2 instincts AND median confidence >= 0.80) writes a scaffold to
`<vault>/⚙️ Meta/Instinct Proposals/proposed-skill-<domain>.md`. Clusters below
the bar print as `watch` (not yet ripe).

## Step 2 — judge each PROPOSE cluster

For each proposal the script wrote, decide the structure:

- **Command** — the cluster is ONE repeatable procedure with a clear trigger.
- **Skill** — the cluster is a coherent body of domain guidance (most common).
- **Agent** — the cluster describes an autonomous multi-step workflow.

A cluster that is really just 2-3 facets of an existing rule should be
CONSOLIDATED into that rule, not promoted. Reject those.

## Step 3 — draft (only on confirm)

For an accepted cluster, draft the real skill/command body from the member
instincts (keep each instinct's Action + Evidence). Place it in the right repo
per the install rules, wire its discoverability + automation + verification in
the SAME session (the three-layer wiring rule), then retire or link the source
memories. Delete the proposal file once adopted or rejected — proposals are
scratch, not a backlog.

## Headless/auto mode

In a cron/`--print` session: run Step 1, report the PROPOSE clusters and the
proposal paths, and STOP. Promotion to a real skill is a judgment call that
needs a human — never auto-create skills.
