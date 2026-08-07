---
name: warn-delegated-task-needs-source
enabled: true
event: file
action: warn
conditions:
  - field: file_path
    operator: regex_match
    pattern: '(to-?dos?|tasks?|delegation|backlog)[^/]*\.md$'
  - field: content
    operator: regex_match
    pattern: '- \[ \](?:(?!\[\[|https?://|\n).)*\[owner::\s*[^\]]+\](?:(?!\[\[|https?://|\n).)*(?=\n|$)'
---

**Delegated task without a self-contained source.** This line has an `[owner::]` (you are handing it to someone) but no `[[wikilink]]` or URL pointing to the brief, source, or deliverable.

A task that routes the assignee back to a person for the input ("ask Sam for the link", "pídele el doc a la manager") creates a dependency chain: they ask, you forget, the task dies. Every delegated task should be executable by someone with no access to you.

**Minimum Viable Delegation** — each delegated task line should carry at least one link to:
- a brief or playbook,
- the source doc (list, tracker, spec), or
- where the deliverable goes.

Then check the four corners: **Source** (where the input lives), **Location** (where the output goes), **Shape** (what "done" looks like), **Channel** (how to report back). If any is missing, add it before filing.

*Scope:* fires on to-do / task / delegation / backlog files, on any `[owner:: …]` line with no link. Customize the filename pattern or owner field for your vault, or set `action: block` if you want it enforced hard. This is the generic version of a delegated-task-quality rule; the specific names, paths, and tools stay in your own vault.
