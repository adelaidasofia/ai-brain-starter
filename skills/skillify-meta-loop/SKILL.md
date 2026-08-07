---
name: skillify-meta-loop
description: 'Use when a recurring bug, support question, client incident, or manual task surfaces 2+ times, when a codified rule keeps getting violated despite being written down, when a teammate or client hits an issue anyone could hit, or when delivery/setup friction repeats. Triggers: "skillify this", "this keeps happening", "we hit this last week too", "make a skill for X", "automate this", "every client hits this", "I keep doing X manually", "turn this into a skill".'
source: github.com/garrytan/gbrain
trigger: '"skillify this" / "this keeps happening" / "we hit this last week too" / "make a skill for X" / "automate this" / "every client hits this"'
---

Bug → durable skill in 10 steps. The loop runs same-session, no queueing.

## When to invoke
| Signal | Example |
|---|---|
| Same issue 2+ times | Two clients hit the same install error |
| Manual repeat | Wrote the same onboarding email 3× |
| Codified rule violated | A typo slipped past a lint rule despite it being codified |
| Support question pattern | 3 teams asked "how do we wire X" |
| Delivery friction | Bespoke setup hour you shouldn't be spending each time |

If the trigger is a one-off accident with no pattern signal, skip skillify. The loop is for repeats.

## The 10 steps

### 1. Scaffold
Pick the skill's home:
- Private skills repo → `~/dev/<your>-skills/<name>/`
- Public substrate → `<repo>/skills/<name>/` (if generic + no personal data)
- Team repo → `~/dev/<team>-skills/<name>/`

`mkdir <path> && touch <path>/SKILL.md`. Don't symlink yet.

### 2. State the trigger
In SKILL.md frontmatter `trigger:` field: every phrase a user might type that should fire this skill. Cover bilingual surfaces if relevant. Cover the explicit invocation (`/skill-name`) AND natural-language patterns. Be over-inclusive; pruning false-positives is cheaper than missing the signal.

### 3. Define the failure mode
What does the world look like WITHOUT this skill? One paragraph. Be specific: name the file, the rule, the client, the hour cost. Without this, the skill ages into a "feels useful but no one runs it" file.

### 4. Write the steps in caveman form
Terse, IF/THEN, bullets, no preamble. Drop articles. Drop multi-paragraph rationale. The skill must be USABLE on read, not appreciable.

### 5. Provide one real example
Concrete, with real file paths, real flags, real outputs. Not `<your-key-here>`. Not `# do the thing`. The first real example is the most-cited part of the skill.

### 6. Add the discoverability path
Pick ONE: top-level SKILL.md (auto-loads metadata); plugin marketplace registration; hookify trigger reminder; CLI tool with explicit invocation. Document which path is wired. A skill nobody can find = a skill that doesn't exist.

### 7. Wire the verification harness (if behavior-changing)
Every behavior-changing skill ships a check that proves it works. Forms: pytest in `tests/` if Python; a "good vs bad" example pair; a scheduled verification script (cron/launchd) if the skill prevents drift. If documentation-only, skip.

### 8. Audit for personal-data leakage (if public)
For skills landing in any public repo: run a personal-data scrub — word-boundary regex on real names, company names, private paths. Example patterns: `\bYourName\b`, `\bYourCompany\b`, `/Users/youruser/`. Any match → scrub or move to private repo.

### 9. Commit + push
Commit with explicit paths. Public: scrub → commit → push → auto-merge own PR when CI green. Private: commit + push.

### 10. Add to skill-usage tracking + cross-link
Document the skill in your memory index. Add a one-line entry if it's a CLI tool. Link from related rule files. The first invocation logs to a skill-usage log automatically; that's how you know the discoverability wiring works.

## Common failure modes
| Failure | Diagnosis | Fix |
|---|---|---|
| Skill exists but never fires | Discoverability not wired (step 6 skipped) | Add hookify reminder OR plugin marketplace OR top-level SKILL.md |
| Skill fires but wrong context | Trigger too narrow (step 2 underspecified) | Add more natural-language phrases, common typos, bilingual variants |
| Skill ages into stale advice | No verification harness (step 7 skipped) | Add a scheduled check OR a pytest assert that flags drift |
| Skill duplicates existing one | Skipped grep before scaffold | `grep -r <pattern>` across your skills dirs first |
| Skill leaks personal data publicly | Skipped step 8 | Scrub via word-boundary regex; revoke public commit if pushed |

## Source
Adapted from garrytan/gbrain Skillify Meta-Loop pattern. Generalizes across vault rules, public skills, and team playbooks.
