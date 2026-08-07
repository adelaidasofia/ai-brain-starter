---
name: instinct-export
description: Use when the user runs /instinct-export or wants to share, back up, hand off, or publish learned instincts to another machine, harness, or teammate — 'export my instincts', 'make an instinct pack', 'build a house/team instinct library', 'send my rules to a teammate', 'move instincts to my new laptop'. Part of the Instinct Engine. NOT for reading or reviewing memories (use /patterns) and NOT for importing a pack (use /instinct-import).
trigger: /instinct-export
---

# /instinct-export — portable instinct pack out

> **`{SKILL_DIR}`** = this skill's own folder (locally: the directory this SKILL.md lives in; a served brain substitutes the real absolute path before you read this). Shared starter files live at the repo root two levels up: `{SKILL_DIR}/../..`. If a path does not resolve, name the missing file and stop — never guess another location.

Turns your confidence-scored instincts into a portable, evidence-backed YAML
pack. The unit of sharing is the instinct: `id / trigger / confidence / domain
/ source_repo` plus an `action` and `evidence` body.

ECC source pattern: portable instinct format with `/instinct-import`.
Reimplemented clean per license-hygiene.

## Run

```bash
# current project's instincts + globals, only confidence >= 0.70, to a file
python3 "{SKILL_DIR}/../../scripts/instinct.py" export \
  --min-confidence 0.70 --out ~/instinct-pack.yaml

# everything regardless of project scope
python3 "{SKILL_DIR}/../../scripts/instinct.py" export --all --out ~/all.yaml
```

Flags:
- `--project <id>` — scope to one project id (default: the current working tree's).
- `--min-confidence <f>` — drop instincts below this effective confidence.
- `--all` — ignore project scope, export every instinct.
- `--out <file>` — write to a file (omit to print to stdout).

`confidence` in the pack is the EFFECTIVE value (staleness decay already
applied), so an importer inherits an honest, time-adjusted score.

## Sharing note

A curated, org-specific export is a deliverable in its own right — a shared
"house instinct library" a team can install into every member's harness. Scrub
any project-private instinct before sharing a pack outside your org;
`--project global` keeps a pack to cross-project rules only.
