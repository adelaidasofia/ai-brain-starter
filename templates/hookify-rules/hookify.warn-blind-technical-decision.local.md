---
name: warn-blind-technical-decision
enabled: true
event: stop
action: warn
conditions:
  - field: transcript
    operator: regex_match
    pattern: '(?i)(?:\b(?:should i|shall i|do you want me to|want me to|would you like me to|do you want these|should these)\b[^?\n]{0,120}?\b(?:git|commit|commits|committed|push|pushed|merge|merged|branch|branches|stash|rebase|revert|repo|repository|version history)\b[^?\n]{0,120}?\?|\b(?:commit and push|which merge strategy|keep or delete the branch|merge or rebase)\b[^?\n]{0,60}\?)'
---

**A turn ended on a decision the user has no basis to make.** Something in this session asked them to choose between two machinery outcomes — save these files or leave them, push or hold, which merge strategy, keep or delete a branch. They cannot weigh that, so whatever they answer is a guess, and a guess against their own data. Do the safe thing, then say what was done in one plain sentence. Full rule: `⚙️ Meta/rules/session-close.md` → **Plain-language register**.

*Shape, not phrase list.* The pattern matches an offer-to-act opener plus a machinery noun plus a question mark inside one transcript record. The earlier five-phrase allow-list missed the incident verbatim in plain wording, because plain wording is exactly what a plain-language rule pushes the model toward: *There are 56 changed files. Should I save these to git, or leave them.*

*Two false positives it deliberately accepts.* Same as the revision-id rule: the `transcript` field is the whole file, so a fire persists for the rest of the session, and a user who types a machinery question themselves trips it. A Stop warn goes only to `~/.claude/hookify-blocks.log`, never to the user, so neither one trains anybody to ignore anything.
