---
name: warn-filesystem-walk-without-bounded-read
enabled: true
event: file
action: warn
conditions:
  - field: file_path
    operator: regex_match
    pattern: '\.py$'
  - field: content
    operator: regex_match
    pattern: '(?s)^(?=.*(?:\b(?:os\.)?f?walk\s*\(|\.walk\s*\(|\.rglob\s*\(|(?:\.|\b)i?glob\s*\([^)]*(?:\*\*|recursive\s*=\s*True)|\bcopytree\s*\())(?=.*(?:\.read_(?:text|bytes)\s*\(|\bopen\s*\([^,\n)]*(?:\)|,\s*[''"]r)|\b(?:copy|copy2|copyfile|copytree)\s*\()).*$'
---

This edit appears to add a recursive Python file walker that reads content without
the shared `safe_read` boundary. A cloud placeholder, FIFO, stalled mount, or FUSE
file can otherwise freeze the whole walk.

Before keeping the edit, run the product repo's AST guard against the complete
file:

```bash
python3 scripts/check-cloud-safe-file-walkers.py --check path/to/file.py
```

Use `safe_read_text` or `safe_read_bytes` from `hooks/_lib/safe_read.py`. The
primitive rejects special/offline files, caps bytes and wall time, and bounds the
number of timed-out workers that may linger.

The file-event pattern is an early nudge over the changed text. The AST command is
the authority because it follows helpers across the complete file and stays silent
for metadata-only walkers and write-only fixtures.
