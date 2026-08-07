#!/usr/bin/env python3
"""Gate: every shipped hookify template must be evaluable by the OFFICIAL engine.

Why this exists
---------------
We ship `templates/hookify-rules/hookify.*.local.md` for people to copy. They run
the OFFICIAL hookify plugin (anthropics/claude-code -> plugins/hookify). That engine
silently returns False for any operator it does not implement and None for any field
it cannot resolve -- so a template using a capability the official engine lacks LOADS
FINE AND NEVER FIRES. A safety rule that never fires is worse than no rule: it reads
as protection that is not there.

This gate fails loud at CI time instead. Bug class: SILENT-NO-OP-ON-UNEVALUABLE-SPEC.

Capability sets below are transcribed from the official engine. Re-verify with:
    git -C <claude-code> show origin/main:plugins/hookify/core/rule_engine.py
Update them (and shrink KNOWN_UPSTREAM_GAPS) when upstream ships new capabilities.

stdlib only, no PyYAML -- runs under `python3 -S`.
"""
import re
import sys
from pathlib import Path

# --- OFFICIAL engine capabilities (anthropics/claude-code@main, plugins/hookify) ---
# Source: rule_engine.py _check_condition (operators) + _extract_field (fields).
OFFICIAL_OPERATORS = {
    "regex_match", "contains", "equals", "not_contains", "starts_with", "ends_with",
}
OFFICIAL_FIELDS_BY_EVENT = {
    "bash":   {"command"},
    "file":   {"file_path", "new_text", "new_string", "old_text", "old_string", "content"},
    "prompt": {"user_prompt"},
    "stop":   {"reason", "transcript"},
}
OFFICIAL_FIELDS_BY_EVENT["all"] = set().union(*OFFICIAL_FIELDS_BY_EVENT.values())

# Templates knowingly ahead of the official engine, each with the upstream fix in
# flight. Keeps this gate GREEN today (never ship a known-red gate) while making the
# debt visible. DELETE an entry when its PR ships -- the gate then enforces it.
KNOWN_UPSTREAM_GAPS = {
    # block-malformed-mcp-json is NOT listed: it was rewritten to an anchored
    # negative lookahead inside a plain regex_match, so it now fires on the
    # official engine today rather than waiting on anthropics/claude-code#78715.
    "hookify.warn-rotation-push-on-local-only-leak.local.md":
        "event:prompt rules never fire on the official engine (payload key is "
        "`prompt`, engine reads `user_prompt`) -- upstream fix: anthropics/claude-code#79873",
}

FM = re.compile(r"\A---\s*\n(.*?)\n---", re.S)


def parse_template(path: Path):
    """-> (event, [(field, operator), ...]). Regex frontmatter parse, stdlib only."""
    m = FM.search(path.read_text(encoding="utf-8", errors="replace"))
    if not m:
        return None, []
    fm = m.group(1)
    ev_m = re.search(r"^event:\s*([A-Za-z_]+)", fm, re.M)
    event = ev_m.group(1) if ev_m else None

    conds = []
    cond_m = re.search(r"^conditions:\s*\n(.*?)(?=\n[A-Za-z_]+:|\Z)", fm, re.M | re.S)
    if cond_m:
        for block in re.split(r"\n\s*-\s+", "\n" + cond_m.group(1)):
            if not block.strip():
                continue
            f = re.search(r"\bfield:\s*(\S+)", block)
            o = re.search(r"\boperator:\s*(\S+)", block)
            if f:
                # engine default when `operator:` is omitted is regex_match
                conds.append((f.group(1), o.group(1) if o else "regex_match"))
    return event, conds


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    tdir = root / "templates" / "hookify-rules"
    templates = sorted(tdir.glob("hookify.*.local.md"))
    if not templates:
        print(f"ERROR: no templates found under {tdir}", file=sys.stderr)
        return 2

    new_violations, known_hits, checked = [], [], 0
    for t in templates:
        event, conds = parse_template(t)
        if event is None:
            new_violations.append((t.name, "missing `event:` in frontmatter"))
            continue
        allowed_fields = OFFICIAL_FIELDS_BY_EVENT.get(event)
        if allowed_fields is None:
            new_violations.append((t.name, f"unknown event '{event}'"))
            continue
        for field, op in conds:
            checked += 1
            problems = []
            if op not in OFFICIAL_OPERATORS:
                problems.append(f"operator '{op}' not implemented by the official engine")
            if field not in allowed_fields:
                problems.append(
                    f"field '{field}' not resolvable by the official engine for event '{event}'")
            for p in problems:
                if t.name in KNOWN_UPSTREAM_GAPS:
                    known_hits.append((t.name, p))
                else:
                    new_violations.append((t.name, p))

    print(f"hookify template capability gate: {len(templates)} templates, "
          f"{checked} conditions checked against the official engine")

    if known_hits:
        print("\nKNOWN upstream gaps (allowlisted, fix in flight):")
        seen = set()
        for name, _ in known_hits:
            if name not in seen:
                seen.add(name)
                print(f"  - {name}\n      {KNOWN_UPSTREAM_GAPS[name]}")

    stale = sorted(set(KNOWN_UPSTREAM_GAPS) - {n for n, _ in known_hits})
    if stale:
        print("\nSTALE allowlist entries (no longer violating -- delete them):")
        for n in stale:
            print(f"  - {n}")
        return 1

    if new_violations:
        print("\nFAIL: template(s) use capabilities the official engine lacks.")
        print("These rules would load and SILENTLY NEVER FIRE for anyone who copies them.\n")
        for name, why in new_violations:
            print(f"  - {name}\n      {why}")
        print("\nFix: rewrite the condition using an official capability, or land the "
              "upstream fix and add an entry to KNOWN_UPSTREAM_GAPS citing the PR.")
        return 1

    print("\nOK: no new capability violations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
