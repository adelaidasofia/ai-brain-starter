---
name: warn-blind-technical-decision
enabled: true
event: stop
action: warn
conditions:
  - field: transcript
    operator: regex_match
    pattern: '(?i)(commit and push\?|should i (commit|push|merge)|which merge strategy|keep or delete the branch|do you want me to (commit|push|merge))'
---

**Turn ended on a decision a non-developer cannot answer.** This transcript asks the user to choose commit-or-not, push-or-not, a merge strategy, or a branch's fate — a technical call, not a plain-language one. Either do the safe thing and say what you did in one plain sentence, or ask a question anyone could answer without knowing what git is. Never strand the user on a technical either/or (see CLAUDE.md `## Plain-Language Rules — NON-NEGOTIABLE`).

*Scope:* fires on any Stop once this phrasing has appeared anywhere in the transcript, for the same full-transcript reason as the jargon rule above. Advisory (`warn`), never blocks the session from ending. Add or trim phrasing in the pattern to match your own vault's decision points.
