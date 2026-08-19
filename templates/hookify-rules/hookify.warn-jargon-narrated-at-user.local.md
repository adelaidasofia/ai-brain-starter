---
name: warn-jargon-narrated-at-user
enabled: true
event: stop
action: warn
conditions:
  - field: transcript
    operator: regex_match
    pattern: '(?i)(git snapshot|bash task|\bmutex\b|worktrees?|commit sha|\.gitignore\b|\.zsh_secrets|paste (your |an |the )?api key|merge strategy|force[- ]push|cherry-pick|\brebase\b|another session (is |was )?(working|running) in parallel)'
---

**Machinery narrated at a non-developer.** This session's transcript contains a developer term — "worktree", "commit SHA", "mutex", ".gitignore", "rebase", an instruction to paste an API key, or similar — said straight to the user. Most people running this vault are not developers (see CLAUDE.md `## Plain-Language Rules — NON-NEGOTIABLE`). Say what happened in plain language, never how.

*Scope:* fires on any Stop once jargon has appeared anywhere in the session transcript — it does not re-clear when a later turn goes clean, because the official engine's `stop` event only exposes the FULL transcript, not just the latest turn. Treat a fire as "check the last few turns," not "every turn since is guilty." Advisory (`warn`), never blocks the session from ending. Add or trim terms in the pattern to match your own vault's jargon.
