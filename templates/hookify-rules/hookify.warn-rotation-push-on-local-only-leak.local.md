---
name: warn-rotation-push-on-local-only-leak
enabled: true
event: prompt
action: warn
conditions:
  - field: prompt
    operator: regex_match
    pattern: \b(rotate|rotation|rotated|rotating)\b
---

> **KNOWN INERT on the official hookify plugin (as of 2026-07-18).** `event: prompt`
> rules never fire there: Claude Code sends the submitted text under the payload key
> `prompt`, but the engine reads `user_prompt` — so the field resolves empty and the
> condition never matches. This is true for BOTH field spellings, so there is no
> template-side workaround. Upstream fix in flight: anthropics/claude-code#79873.
> Copy this rule for the threat model it documents; do not rely on it firing until
> that PR ships. (Verified against the official engine, not a local fork.)

**Before recommending secret rotation, check the leak vector.**

The reflex when a secret-detection layer fires is "rotate the secret." That reflex is wrong when the leak is confined to surfaces the user already trusts. Rotation has real cost (broken integrations, downtime, attention churn); spend that budget on real exposure.

**DO NOT recommend rotation** when the leak is confined to:
- Local machine only (session transcripts under `~/.claude/projects/...`, local logs, files on disk that never sync)
- The model API itself (the LLM-provider's context window, request/response payloads — already a trusted vendor for the operator)
- Both combined

**DO recommend rotation** when the vector extends beyond that envelope:
- Public internet (GitHub push, public gist, pastebin, social post, indexed forum)
- Third-party services the operator hasn't authorized (random SaaS, scraping endpoint, analytics pipelines)
- Untrusted accounts or machines (someone else's laptop, public terminal, shared workstation)
- Broadcast channels where bystanders or non-confidants could read it (external Slack, mass email, public Discord)

**How to apply when a secret-detection hook fires + you're tempted to push rotation:**
1. Identify the leak vector — where did the secret actually land?
2. If local + model-API only: mention briefly ("secret hit session transcript, no broader exposure") and move on; do NOT push rotation
3. If broader: flag the vector + recommend rotation with a specific runbook reference

## Why this hookify rule exists

A codified threat model can live in a memory file and still get violated at response-generation time when the secret-detection signal triggers the "rotate" reflex. The signal arrives faster than the threat-model lookup. This prompt-time warn closes the gap structurally — it injects the threat model into context BEFORE the response generates, so the check happens against the data, not the reflex.

Pattern source: the rule existed as a written memory entry but kept getting violated on real incidents. The structural fix is the prompt-time warn that injects the threat model exactly when the user prompt contains the trigger keyword. Codified after the rule was violated twice in 24 hours despite being explicitly written down.

Bug class this addresses: **RULE-VIOLATION-RECURRENCE-WITHOUT-STRUCTURAL-GUARD** — codified rule exists, model didn't apply it, codifying-it-again-as-prose doesn't help; the fix is a structural prompt-time injection at the moment the rule should fire.

## Bypass

`ROTATION_PUSH_BYPASS=1 <prompt>` if you genuinely need to recommend rotation independent of the leak vector (e.g. routine credential lifecycle, post-incident hardening for an unrelated reason). The bypass is for prompt-level escape; the rule itself stays codified.

## Related

- `hooks/scan-prior-sessions-for-secrets.py` — the SessionStart hook that surfaces unredacted secrets in prior session JSONLs (and now auto-scrubs closed worktrees with `VAULT_ROOT` set)
- `hooks/_lib/secret_patterns.py` — the shared pattern registry used by every secret-detection layer
- A vault-side `feedback_secret_leak_threat_model.md` memory entry codifying the operator's specific local-vs-broader envelope (this is the user-specific scope of the universal principle above)
