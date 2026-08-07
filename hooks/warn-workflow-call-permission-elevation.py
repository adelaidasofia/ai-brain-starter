#!/usr/bin/env python3
"""PreToolUse Write/Edit hook: warn when a GitHub Actions workflow edit would
make a reusable workflow request a permission its caller never grants.

THE CLASS. A called (reusable) workflow's GITHUB_TOKEN can only be DOWNGRADED
from its caller's, never elevated. When a callee asks for a scope the caller
does not grant, GitHub rejects the run at STARTUP: `startup_failure`, ZERO jobs,
no annotation, no check-run, nothing readable from the API. Every gate in that
pipeline silently stops running.

WHY A HOOK AND NOT JUST A LINTER. `actionlint` exits 0 on a tree GitHub refuses
to start -- it validates each file in isolation, and this defect lives in the
RELATIONSHIP between two files. A callee is also perfectly valid on its own
(standalone it IS the top-level grant, so it has no caller to exceed). The
defect is invisible to every single-file check.

WHY IT CHECKS BOTH DIRECTIONS. This is the part that matters, and the part a
naive implementation gets wrong. The realistic way this bug is introduced is by
editing the CALLEE -- someone adds `pull-requests: read` to a reusable workflow
so one of its own jobs can read PR metadata. That edit is locally correct and
locally green. It breaks a DIFFERENT file's pipeline. So this hook checks:

  forward  -- the edited file is a CALLER: do its callees fit its grant?
  reverse  -- the edited file is a CALLEE: do its CALLERS grant what it asks?

A forward-only check would miss the exact edit that causes this in practice.

THE TRAP IT ENCODES. Declaring a `permissions:` block is not additive. It
replaces the default wholesale, so every scope you do NOT list becomes `none`.
`permissions: {contents: write}` grants contents AND REVOKES EVERYTHING ELSE.
A callee asking for any unlisted scope is therefore requesting an elevation,
even though the caller looks strictly more privileged at a glance.

WARN, NOT BLOCK. A workflow edit is not destructive and the consequence lands
at release time, not now. This surfaces the problem at authoring time with the
exact fix; a repo that wants a hard stop should also run the check in CI.

THE WARNING GOES ON STDOUT. Every hooks.json command in this repo is wired as
`<script> 2>/dev/null || echo <allow-json>` -- stderr is discarded by the shell
before Claude Code ever reads it. A diagnostic written to stderr is therefore
deployed, registered, green in its own tests, and MUTE in production. Case 9 of
the test suite pins the channel, and the harness captures stdout only so the
behavioural cases fail the same way production would.

Bypass: WORKFLOW_PERMS_BYPASS=1
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # PyYAML absent -> stay silent, never break the user's edit
    sys.exit(0)

# Token scopes are ordered: absent < read < write.
LEVELS = {"none": 0, "read": 1, "write": 2}
NAMES = {v: k for k, v in LEVELS.items()}


def _level(value: object) -> int:
    if isinstance(value, str) and value.lower() in LEVELS:
        return LEVELS[value.lower()]
    return 0


def parse_permissions(node: object) -> dict | None:
    """Normalise a `permissions:` node to {scope: level}.

    None means the workflow/job declared nothing -> it inherits, which is never
    an elevation. A DECLARED block is CLOSED: unlisted scopes are level 0.
    """
    if node is None:
        return None
    if isinstance(node, str):
        token = node.lower()
        if token == "read-all":
            return {"*": LEVELS["read"]}
        if token == "write-all":
            return {"*": LEVELS["write"]}
        return {"*": LEVELS["none"]}
    if isinstance(node, dict):
        return {str(k): _level(v) for k, v in node.items()}
    return {"*": LEVELS["none"]}


def granted(perms: dict | None, scope: str) -> int:
    if perms is None:
        return LEVELS["write"]
    if "*" in perms:
        return perms["*"]
    return perms.get(scope, LEVELS["none"])


def _load(text: str) -> dict | None:
    try:
        doc = yaml.safe_load(text)
    except Exception:
        return None
    return doc if isinstance(doc, dict) else None


def _read(path: Path) -> dict | None:
    try:
        return _load(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _call_edges(doc: dict) -> list[tuple[str, str, dict | None]]:
    """[(job_id, callee_basename, job_level_permissions_or_None)] for local calls."""
    out = []
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return out
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses")
        if not isinstance(uses, str) or not uses.startswith("./"):
            continue
        job_perms = parse_permissions(job["permissions"]) if "permissions" in job else None
        out.append((str(job_id), Path(uses).name, job_perms))
    return out


def _violations(caller_name, caller_doc, callee_name, callee_doc):
    """Elevations the caller->callee edge would take. [] if fine."""
    found = []
    requested = parse_permissions(callee_doc.get("permissions"))
    if requested is None:
        return found  # callee inherits; never an elevation
    caller_default = parse_permissions(caller_doc.get("permissions"))
    for job_id, target, job_perms in _call_edges(caller_doc):
        if target != callee_name:
            continue
        effective = job_perms if job_perms is not None else caller_default
        if effective is None:
            continue  # caller never declares -> repo default, unresolvable here
        for scope, want in sorted(requested.items()):
            if want == LEVELS["none"]:
                continue
            have = granted(effective, scope)
            if have < want:
                found.append((job_id, scope, NAMES[want], NAMES[have], caller_name))
    return found


def main() -> int:
    if os.environ.get("WORKFLOW_PERMS_BYPASS") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool = payload.get("tool_name") or ""
    if tool not in ("Write", "Edit"):
        return 0
    ti = payload.get("tool_input") or {}
    raw_path = ti.get("file_path") or ""
    if not raw_path:
        return 0

    path = Path(raw_path)
    if path.suffix not in (".yml", ".yaml"):
        return 0
    if path.parent.name != "workflows" or path.parent.parent.name != ".github":
        return 0

    # Resulting content of the edited file.
    if tool == "Write":
        new_text = ti.get("content") or ""
    else:
        try:
            current = path.read_text(encoding="utf-8")
        except Exception:
            return 0
        old, new = ti.get("old_string") or "", ti.get("new_string") or ""
        if old and old not in current:
            return 0
        new_text = current.replace(old, new, 1) if old else current

    edited = _load(new_text)
    if not edited:
        return 0

    wf_dir = path.parent
    siblings = {}
    try:
        for p in list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml")):
            if p.name == path.name:
                continue
            d = _read(p)
            if d:
                siblings[p.name] = d
    except Exception:
        return 0

    problems = []

    # FORWARD: the edited file is a caller.
    for job_id, callee_name, _ in _call_edges(edited):
        callee_doc = siblings.get(callee_name)
        if callee_doc is None:
            continue
        problems += _violations(path.name, edited, callee_name, callee_doc)

    # REVERSE: the edited file is a callee -- the realistic way this is born.
    for sib_name, sib_doc in siblings.items():
        if any(t == path.name for _, t, _ in _call_edges(sib_doc)):
            problems += _violations(sib_name, sib_doc, path.name, edited)

    if not problems:
        return 0

    seen, lines = set(), []
    for job_id, scope, want, have, caller in problems:
        key = (job_id, scope, caller)
        if key in seen:
            continue
        seen.add(key)
        lines.append(
            f"  {caller}: job `{job_id}` -> callee requests `{scope}: {want}`, "
            f"caller grants `{scope}: {have}`\n"
            f"    fix: add `{scope}: {want}` to a `permissions:` block on job `{job_id}` in {caller}"
        )

    warning = (
        "WORKFLOW PERMISSION ELEVATION -- GitHub will refuse to START this workflow.\n"
        "A called workflow's GITHUB_TOKEN can only be DOWNGRADED from its caller's.\n"
        "The run returns startup_failure with ZERO jobs, no annotation and no\n"
        "check-run, so every gate in that pipeline silently stops running.\n\n"
        + "\n".join(lines)
        + "\n\nNote: declaring `permissions:` REPLACES the default -- every scope you do\n"
        "not list becomes `none`, so an unlisted scope in a callee is an elevation.\n"
        "actionlint will not catch this; it lints each file in isolation.\n"
        "Bypass: WORKFLOW_PERMS_BYPASS=1\n"
    )

    # STDOUT, as hookSpecificOutput.additionalContext -- NOT stderr. Every
    # hooks.json command in this repo ends in `2>/dev/null || echo <allow>`, so
    # a hook that writes its diagnostic to stderr is registered, tested green,
    # and PERMANENTLY SILENT in production. Only stdout survives the wiring.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": warning,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
