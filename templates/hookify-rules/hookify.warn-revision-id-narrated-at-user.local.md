---
name: warn-revision-id-narrated-at-user
enabled: true
event: stop
action: warn
conditions:
  - field: transcript
    operator: regex_match
    pattern: '(?i)\b(?:saved|saving|stored|snapshot|snapshotted|commit|committed|checkpoint|revision|rollback|revert(?:ed)?)\b[^.\n]{0,40}?(?<![0-9a-f"\-])(?=[0-9a-f]{7,40}(?![0-9a-f"\-]))(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}'
---

**A raw revision id was said out loud to the user.** Something in this session read like *saved it as* followed by a bare 7-40 character hex id. That id is maintainer plumbing; it means nothing to someone who does not use git, and it makes a safe automatic action sound like something they now have to manage. Say what was kept and where, in one plain sentence, with no id. Full rule: `⚙️ Meta/rules/session-close.md` → **Plain-language register**.

*Shape, not vocabulary.* The pattern matches the SYMPTOM — a narration verb followed within 40 characters by a bare hex id carrying both digits and letters — never the phrase "commit sha". An earlier version pinned that phrase and was inert: the incident string *saved it as* + a bare id did not contain it.

*Two false positives it deliberately accepts.* The `transcript` field is the whole transcript file, so (a) once the shape appears it keeps matching on every later Stop in that session, and (b) it cannot tell the model narrating an id from the user typing one. Both are tolerable because a Stop warn is written to `~/.claude/hookify-blocks.log` and never shown to anyone mid-session; read a fire as "check the last few turns," never as "every turn since is guilty." Matches cannot span transcript records: real newlines separate records and `[^.\n]` stops at one.

*Excluded by construction:* ids adjacent to `"` or `-` (the transcript's own `uuid` / `sessionId` / `parentUuid` values, which a bare `\b[0-9a-f]{7,40}\b` matches on nearly every line), all-digit runs, and all-letter runs like `deadbeef`.
