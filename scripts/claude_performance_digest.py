#!/usr/bin/env python3
"""
Claude Performance Self-Improvement System: Weekly Digest

Reads Claude Code JSONL session data, computes effectiveness metrics,
diagnoses problems, writes prescriptive to-dos. Zero external deps.

Usage:
    python3 claude_performance_digest.py [--days N] [--dry-run]
"""

import json
import os
import sys
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Config (tune per deployment) ──────────────────────────────────────

THRESHOLDS = {
    "one_shot_min": 0.75,          # alert if below 75%
    "exploration_max": 0.35,       # alert if exploration > 35% for 3+ days
    "agent_turns_max": 5,          # alert if avg subagent turns > 5
    "opus_max": 0.70,              # alert if Opus > 70% of turns
    "hookify_repeat": 10,          # alert if same rule fires 10+/week
    "recurring_error_sessions": 3, # alert if same tool error in 3+ sessions
}

LOOKBACK_DAYS = 7

# ── Paths (self-locating) ────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
VAULT_ROOT = SCRIPT_DIR.parent.parent  # ⚙️ Meta/scripts/ -> vault root
PERFORMANCE_DIR = VAULT_ROOT / "⚙️ Meta" / "Performance"
TODO_FILE = VAULT_ROOT / "⚙️ Meta" / "Claude To-dos.md"
PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# ── Tool classification ──────────────────────────────────────────────

CODING_TOOLS = {"Edit", "Write", "NotebookEdit"}
EXPLORE_TOOLS = {"Read", "Glob", "Grep"}
DEBUG_TOOLS = {"Bash"}
DELEGATE_TOOLS = {"Agent"}

# Minimum thinking chars to classify a no-tool turn as Planning vs Conversation
PLANNING_THINKING_MIN = 500

def classify_turn(tool_names, thinking_chars=0):
    """Classify a turn by its dominant tool category."""
    if not tool_names:
        if thinking_chars >= PLANNING_THINKING_MIN:
            return "Planning"
        return "Conversation"
    s = set(tool_names)
    if s & CODING_TOOLS:
        return "Coding"
    if s & DELEGATE_TOOLS:
        return "Delegation"
    if s & EXPLORE_TOOLS and not (s & CODING_TOOLS):
        return "Exploration"
    if s & DEBUG_TOOLS and not (s & CODING_TOOLS):
        return "Debugging"
    return "Exploration"


# ── JSONL parsing ────────────────────────────────────────────────────

def iter_jsonl(path):
    """Stream-parse JSONL, skip bad lines."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except (OSError, IOError):
        return


def find_recent_sessions(days):
    """Find JSONL files with mtime in last N days."""
    cutoff = time.time() - (days * 86400)
    sessions = []
    if not PROJECTS_ROOT.exists():
        return sessions
    for project_dir in PROJECTS_ROOT.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            try:
                if jsonl_file.stat().st_mtime >= cutoff:
                    sessions.append(jsonl_file)
            except OSError:
                continue
    return sessions


# Optional: map project directory substrings to clean display labels.
# Leave empty to auto-extract from directory paths.
# Example: PROJECT_LABELS = {"my-notes": "Notes", "work-repo": "Work"}
PROJECT_LABELS = {}


def extract_project_name(project_dir_name):
    """Extract human-readable project name from directory."""
    name = project_dir_name
    wt_match = re.match(r"(.+?)--claude-worktrees-.+", name)
    if wt_match:
        name = wt_match.group(1)
    for needle, label in PROJECT_LABELS.items():
        if needle.lower() in name.lower():
            return label
    path = name.replace("-", "/")
    skip = {"Users", "Desktop", "Documents", "Library", "CloudStorage"}
    segments = [s for s in path.split("/") if s and s not in skip]
    if not segments:
        return project_dir_name
    return " ".join(segments[-2:]) if len(segments) >= 2 else segments[-1]


# ── Metric computation ───────────────────────────────────────────────

def analyze_session(jsonl_path):
    """Analyze one session file, return metrics dict."""
    session_id = jsonl_path.stem
    project = extract_project_name(jsonl_path.parent.name)

    turns = []           # list of {model, tools, is_edit, timestamp}
    edit_files = {}      # file -> [sequence of events: 'edit', 'bash', 'edit']
    agent_spawns = 0
    tool_errors = []     # (tool_name, error_snippet)
    hookify_rules = []   # rule names detected in thinking

    prev_msg_type = None

    for record in iter_jsonl(jsonl_path):
        rec_type = record.get("type")

        if rec_type == "assistant":
            msg = record.get("message", {})
            model = msg.get("model", "unknown")
            content = msg.get("content", [])
            tool_names = []
            edited_files = []
            thinking_chars = 0

            for block in content:
                btype = block.get("type")
                if btype == "thinking":
                    thinking_chars += len(block.get("thinking", ""))
                if btype == "tool_use":
                    name = block.get("name", "")
                    tool_names.append(name)
                    inp = block.get("input", {})

                    # Track edits by file
                    if name in ("Edit", "Write"):
                        fp = inp.get("file_path", "")
                        if fp:
                            edited_files.append(fp)
                            edit_files.setdefault(fp, []).append("edit")

                    if name == "Bash":
                        # Check if Bash after an edit (potential retry)
                        for fp in list(edit_files.keys()):
                            last_events = edit_files[fp]
                            if last_events and last_events[-1] == "edit":
                                edit_files[fp].append("bash")

                    if name == "Agent":
                        agent_spawns += 1

                # Scan thinking for hookify rule firings
                if btype == "thinking":
                    thinking_text = block.get("thinking", "")
                    # Look for hookify rule names in quotes: hookify rule "no-em-dash"
                    for match in re.finditer(r'hookify\s+rule\s+["\']([a-z0-9_-]+)["\']', thinking_text, re.IGNORECASE):
                        hookify_rules.append(match.group(1).strip())
                    # Look for "rule_id: xxx" patterns from hookify output
                    for match in re.finditer(r'rule_id["\s:]+([a-z0-9_-]+)', thinking_text, re.IGNORECASE):
                        hookify_rules.append(match.group(1).strip())
                    # Count generic hookify mentions as "hookify-unspecified" for volume tracking
                    if re.search(r'hookify\s+(?:block|fire|trigger|caught|reject)', thinking_text, re.IGNORECASE):
                        hookify_rules.append("hookify-fired")

            turns.append({
                "model": model,
                "tools": tool_names,
                "category": classify_turn(tool_names, thinking_chars),
                "timestamp": record.get("timestamp", ""),
                "edited_files": edited_files,
            })

        elif rec_type == "tool_result" or rec_type == "tool_error":
            # Track tool errors
            msg = record.get("message", {})
            content = msg.get("content", []) if isinstance(msg.get("content"), list) else []
            for block in content:
                if block.get("is_error") or rec_type == "tool_error":
                    text = block.get("text", "")[:200] if isinstance(block.get("text"), str) else ""
                    tool_id = block.get("tool_use_id", "")
                    tool_errors.append((tool_id, text))

    # ── Compute one-shot rate ──
    # An edit is "one-shot" if: Edit on file X is NOT followed by Bash then another Edit on same file
    total_edited_files = len(edit_files)
    retry_files = 0
    for fp, events in edit_files.items():
        # Pattern: edit -> bash -> edit = retry
        event_str = " ".join(events)
        if "edit bash edit" in event_str:
            retry_files += 1

    one_shot_rate = (total_edited_files - retry_files) / total_edited_files if total_edited_files > 0 else None

    # ── Model distribution ──
    model_counts = Counter()
    for t in turns:
        m = t["model"]
        if "opus" in m.lower():
            model_counts["opus"] += 1
        elif "sonnet" in m.lower():
            model_counts["sonnet"] += 1
        elif "haiku" in m.lower():
            model_counts["haiku"] += 1
        else:
            model_counts["other"] += 1

    # ── Activity distribution ──
    category_counts = Counter(t["category"] for t in turns)

    return {
        "session_id": session_id,
        "project": project,
        "turns": len(turns),
        "model_counts": dict(model_counts),
        "category_counts": dict(category_counts),
        "one_shot_rate": one_shot_rate,
        "total_edits": total_edited_files,
        "retry_edits": retry_files,
        "agent_spawns": agent_spawns,
        "tool_errors": tool_errors,
        "hookify_rules": hookify_rules,
    }


def analyze_subagents(jsonl_path):
    """Analyze subagent files for a session."""
    subagent_dir = jsonl_path.parent / jsonl_path.stem / "subagents"
    if not subagent_dir.exists():
        return []

    agents = []
    for meta_file in subagent_dir.glob("*.meta.json"):
        try:
            with open(meta_file) as f:
                meta = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        agent_id = meta_file.stem.replace(".meta", "")
        jsonl_file = subagent_dir / f"{agent_id}.jsonl"

        turn_count = 0
        if jsonl_file.exists():
            for record in iter_jsonl(jsonl_file):
                if record.get("type") == "assistant":
                    turn_count += 1

        agents.append({
            "agent_type": meta.get("agentType", "unknown"),
            "description": meta.get("description", ""),
            "turns": turn_count,
        })

    return agents


# ── Report generation ────────────────────────────────────────────────

def generate_report(sessions_data, agents_data, days):
    """Generate the weekly markdown report."""
    today = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    # Aggregate metrics
    total_turns = sum(s["turns"] for s in sessions_data)
    total_edits = sum(s["total_edits"] for s in sessions_data)
    total_retries = sum(s["retry_edits"] for s in sessions_data)
    total_agents = sum(s["agent_spawns"] for s in sessions_data)

    # Global one-shot rate
    global_one_shot = (total_edits - total_retries) / total_edits if total_edits > 0 else None

    # Model mix across all sessions
    global_models = Counter()
    for s in sessions_data:
        global_models.update(s["model_counts"])
    total_model_turns = sum(global_models.values()) or 1

    # Activity distribution
    global_categories = Counter()
    for s in sessions_data:
        global_categories.update(s["category_counts"])
    total_cat_turns = sum(global_categories.values()) or 1

    # Project allocation
    project_turns = Counter()
    for s in sessions_data:
        project_turns[s["project"]] += s["turns"]

    # Agent analysis
    all_agents = []
    for agent_list in agents_data:
        all_agents.extend(agent_list)
    avg_agent_turns = (sum(a["turns"] for a in all_agents) / len(all_agents)) if all_agents else 0

    # Hookify firings
    hookify_all = []
    for s in sessions_data:
        hookify_all.extend(s["hookify_rules"])
    hookify_counts = Counter(hookify_all)

    # Tool errors across sessions
    error_patterns = defaultdict(set)  # error_snippet -> set of session_ids
    for s in sessions_data:
        for tool_id, error_text in s["tool_errors"]:
            key = error_text[:80] if error_text else "unknown"
            error_patterns[key].add(s["session_id"])

    # ── Build report ──
    lines = []
    lines.append("---")
    lines.append(f"creationDate: {today}")
    lines.append("type: performance-digest")
    lines.append(f"period: {start} to {today}")
    lines.append(f"sessions: {len(sessions_data)}")
    lines.append(f"total_turns: {total_turns}")
    lines.append(f"one_shot_rate: {global_one_shot:.1%}" if global_one_shot is not None else "one_shot_rate: N/A")
    lines.append("---")
    lines.append("")
    lines.append(f"# Performance Digest: {start} to {today}")
    lines.append("")
    lines.append(f"**{len(sessions_data)} sessions** across {len(project_turns)} projects. {total_turns} assistant turns. {total_edits} files edited.")
    lines.append("")

    # 1. Activity distribution
    lines.append("## Activity Distribution")
    lines.append("")
    lines.append("| Category | Turns | % |")
    lines.append("|----------|-------|---|")
    for cat in ["Coding", "Exploration", "Debugging", "Delegation", "Planning", "Conversation"]:
        count = global_categories.get(cat, 0)
        pct = count / total_cat_turns * 100
        lines.append(f"| {cat} | {count} | {pct:.0f}% |")
    lines.append("")

    # 2. One-shot rate
    lines.append("## One-Shot Edit Rate")
    lines.append("")
    if global_one_shot is not None:
        status = "on target" if global_one_shot >= THRESHOLDS["one_shot_min"] else "BELOW TARGET"
        lines.append(f"**{global_one_shot:.0%}** ({total_edits} files edited, {total_retries} required retries) - {status}")
        if total_retries > 0:
            lines.append("")
            lines.append("Top retry files:")
            retry_files_all = Counter()
            for s in sessions_data:
                # Recount from raw data
                pass  # Covered by aggregate above
            lines.append(f"- {total_retries} file(s) had edit-bash-edit retry cycles")
    else:
        lines.append("No edits this period.")
    lines.append("")

    # 3. Agent spawn analysis
    lines.append("## Agent Spawns")
    lines.append("")
    lines.append(f"**{total_agents} agents spawned** across all sessions.")
    if all_agents:
        lines.append(f"Average turns per agent: **{avg_agent_turns:.1f}**")
        agent_type_counts = Counter(a["agent_type"] for a in all_agents)
        lines.append("")
        lines.append("| Type | Count | Avg Turns |")
        lines.append("|------|-------|-----------|")
        for atype, count in agent_type_counts.most_common():
            avg = sum(a["turns"] for a in all_agents if a["agent_type"] == atype) / count
            lines.append(f"| {atype} | {count} | {avg:.1f} |")
    lines.append("")

    # 4. Model mix
    lines.append("## Model Mix")
    lines.append("")
    lines.append("| Model | Turns | % |")
    lines.append("|-------|-------|---|")
    for model in ["opus", "sonnet", "haiku", "other"]:
        count = global_models.get(model, 0)
        pct = count / total_model_turns * 100
        lines.append(f"| {model.title()} | {count} | {pct:.0f}% |")
    lines.append("")

    # 5. Project allocation
    lines.append("## Project Allocation")
    lines.append("")
    lines.append("| Project | Turns | % |")
    lines.append("|---------|-------|---|")
    for proj, count in project_turns.most_common():
        pct = count / total_turns * 100
        lines.append(f"| {proj} | {count} | {pct:.0f}% |")
    lines.append("")

    # 6. Hookify firings
    if hookify_counts:
        lines.append("## Hookify Firings")
        lines.append("")
        lines.append("| Rule | Firings |")
        lines.append("|------|---------|")
        for rule, count in hookify_counts.most_common():
            lines.append(f"| {rule} | {count} |")
        lines.append("")

    # ── Diagnostics ──
    lines.append("## Diagnostics")
    lines.append("")
    prescriptions = []
    prescription_types = []

    # D1: Low one-shot rate
    if global_one_shot is not None and global_one_shot < THRESHOLDS["one_shot_min"]:
        rx = f"One-shot rate is {global_one_shot:.0%} (target: >{THRESHOLDS['one_shot_min']:.0%}). Review top retry files. Are pre-reads missing? Add plan-before-edit for complex changes."
        prescriptions.append(rx)
        prescription_types.append("LOW ONE-SHOT RATE")
        lines.append(f"- **LOW ONE-SHOT RATE**: {rx}")

    # D2: Exploration overhead
    explore_pct = global_categories.get("Exploration", 0) / total_cat_turns if total_cat_turns else 0
    if explore_pct > THRESHOLDS["exploration_max"]:
        rx = f"Exploration is {explore_pct:.0%} of turns (target: <{THRESHOLDS['exploration_max']:.0%}). Audit repeated Read/Grep/Glob patterns. Add frequently-needed paths to session-start context."
        prescriptions.append(rx)
        prescription_types.append("EXPLORATION OVERHEAD")
        lines.append(f"- **EXPLORATION OVERHEAD**: {rx}")

    # D3: Verbose agents
    if all_agents and avg_agent_turns > THRESHOLDS["agent_turns_max"]:
        rx = f"Avg agent turns is {avg_agent_turns:.1f} (target: <{THRESHOLDS['agent_turns_max']}). Review agent prompts: add file paths, expected output format, and clear scope."
        prescriptions.append(rx)
        prescription_types.append("VERBOSE AGENTS")
        lines.append(f"- **VERBOSE AGENTS**: {rx}")

    # D4: Model routing
    opus_pct = global_models.get("opus", 0) / total_model_turns
    if opus_pct > THRESHOLDS["opus_max"]:
        rx = f"Opus handles {opus_pct:.0%} of turns (target: <{THRESHOLDS['opus_max']:.0%}). Check if Sonnet suffices for standard exploration and file edits."
        prescriptions.append(rx)
        prescription_types.append("MODEL ROUTING")
        lines.append(f"- **MODEL ROUTING**: {rx}")

    # D5: Hookify repeat offender
    for rule, count in hookify_counts.most_common():
        if count >= THRESHOLDS["hookify_repeat"]:
            rx = f"Hookify rule '{rule}' fired {count} times. Escalate from reactive (hookify) to proactive (CLAUDE.md rule or session-start reminder)."
            prescriptions.append(rx)
            prescription_types.append("HOOKIFY REPEAT")
            lines.append(f"- **HOOKIFY REPEAT**: {rx}")

    # D6: Recurring errors
    for error_text, session_ids in error_patterns.items():
        if len(session_ids) >= THRESHOLDS["recurring_error_sessions"]:
            rx = f"Tool error appears in {len(session_ids)} sessions: '{error_text}'. Investigate root cause, add guard or workaround."
            prescriptions.append(rx)
            prescription_types.append("RECURRING ERROR")
            lines.append(f"- **RECURRING ERROR**: {rx}")

    if not prescriptions:
        lines.append("All metrics within targets. No prescriptions this week.")

    lines.append("")

    # ── Trending placeholder ──
    lines.append("## Trending")
    lines.append("")
    # Check for prior week's report
    prior_reports = sorted(PERFORMANCE_DIR.glob("weekly-*.md"), reverse=True)
    # Skip the one we're about to write
    prior_reports = [r for r in prior_reports if r.stem != f"weekly-{today}"]
    if prior_reports:
        lines.append(f"Prior report: [[{prior_reports[0].stem}]]")
        lines.append("")
        # Read prior report's frontmatter for comparison
        try:
            with open(prior_reports[0]) as f:
                prior_text = f.read()
            prior_osr_match = re.search(r"one_shot_rate:\s*([\d.]+%|N/A)", prior_text)
            if prior_osr_match and global_one_shot is not None:
                prior_val = prior_osr_match.group(1)
                lines.append(f"- One-shot rate: {global_one_shot:.0%} (prior: {prior_val})")
            prior_sessions = re.search(r"sessions:\s*(\d+)", prior_text)
            if prior_sessions:
                lines.append(f"- Sessions: {len(sessions_data)} (prior: {prior_sessions.group(1)})")
        except OSError:
            pass
    else:
        lines.append("*Baseline week. No prior data for comparison.*")

    lines.append("")
    lines.append(f"*Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} by claude_performance_digest.py*")

    return "\n".join(lines), prescriptions, prescription_types


MEMORY_FILE = Path.home() / ".claude" / "CLAUDE.md"


def _load_dedupe_sources(vault_root: Path) -> str:
    """Collect text from all places a shipped rule might already live.

    Checks: ~/.claude/CLAUDE.md, vault CLAUDE.md, cross-session MEMORY.md
    files under ~/.claude/projects/, vault rules files, and ~/.claude/hooks/*.py.
    Returns a single concatenated string for tag-presence checks.
    """
    chunks = []

    # 1. ~/.claude/CLAUDE.md (global)
    global_claude = Path.home() / ".claude" / "CLAUDE.md"
    if global_claude.exists():
        try:
            chunks.append(global_claude.read_text(errors="ignore"))
        except OSError:
            pass

    # 2. Vault CLAUDE.md
    vault_claude = vault_root / "CLAUDE.md"
    if vault_claude.exists():
        try:
            chunks.append(vault_claude.read_text(errors="ignore"))
        except OSError:
            pass

    # 3. Cross-session memory files under ~/.claude/projects/
    projects_root = Path.home() / ".claude" / "projects"
    if projects_root.exists():
        for mem in projects_root.rglob("MEMORY.md"):
            try:
                chunks.append(mem.read_text(errors="ignore"))
            except OSError:
                pass

    # 4. Vault rules files (⚙️ Meta/rules/*.md)
    rules_dir = vault_root / "⚙️ Meta" / "rules"
    if rules_dir.exists():
        for f in rules_dir.glob("*.md"):
            try:
                chunks.append(f.read_text(errors="ignore"))
            except OSError:
                pass

    # 5. ~/.claude/hooks/*.py (tags embedded in hook comments/code)
    hooks_dir = Path.home() / ".claude" / "hooks"
    if hooks_dir.exists():
        for f in hooks_dir.glob("*.py"):
            try:
                chunks.append(f.read_text(errors="ignore"))
            except OSError:
                pass

    return "\n".join(chunks)


# Map prescription types to MEMORY.md rules (behavioral) vs to-dos (investigation)
BEHAVIORAL_RULES = {
    "VERBOSE AGENTS": "Agent briefings must include: specific file paths, expected output format, and scope boundary. Target: <8 turns per agent. Current avg: {value}.",
    "MODEL ROUTING": "Use Sonnet for standard exploration and file edits. Reserve Opus for complex reasoning, planning, and multi-step changes. Current Opus usage: {value}.",
    "LOW ONE-SHOT RATE": "Before editing a file, read it first. For multi-part changes, plan the full edit sequence before starting. Current one-shot rate: {value}.",
    "EXPLORATION OVERHEAD": "Add frequently-needed file paths to session-start context. Avoid re-discovering the same paths across sessions. Current exploration: {value}.",
}

def apply_prescriptions(prescriptions, prescription_types):
    """Write behavioral rules to MEMORY.md, investigation items to Claude To-dos."""
    if not prescriptions:
        return 0, 0

    today = datetime.now().strftime("%Y-%m-%d")
    marker = "auto-generated, performance-digest"
    rules_written = 0
    todos_written = 0

    # ── Behavioral rules -> MEMORY.md ──
    # Check ALL knowledge stores so already-shipped rules don't re-fire.
    dedupe_text = _load_dedupe_sources(VAULT_ROOT)

    new_rules = []
    written_types = []  # track which types were actually written (not deduped)
    for rx, rx_type in zip(prescriptions, prescription_types):
        if rx_type in BEHAVIORAL_RULES:
            rule_key = rx_type.lower().replace(" ", "_")
            tag = f"performance_{rule_key}"
            if tag in dedupe_text:
                # Rule already exists somewhere in the knowledge stack — skip
                continue
            value = rx.split("is ")[-1].split(".")[0] if "is " in rx else "see digest"
            rule_text = BEHAVIORAL_RULES[rx_type].format(value=value)
            new_rules.append(f"- [{rx_type} fix]({tag}.md) | {rule_text} (updated {today}, {marker})")
            written_types.append(rx_type)

    if new_rules:
        with open(MEMORY_FILE, "a") as f:
            for rule in new_rules:
                f.write(rule + "\n")
        rules_written = len(new_rules)

    # ── Investigation items -> Claude To-dos ──
    existing_todos = ""
    if TODO_FILE.exists():
        with open(TODO_FILE, "r") as f:
            existing_todos = f.read()

    new_todos = []
    for rx, rx_type in zip(prescriptions, prescription_types):
        if rx_type not in BEHAVIORAL_RULES:
            # Non-behavioral prescriptions go to to-dos for investigation
            short_key = rx[:60]
            if short_key in existing_todos:
                continue
            new_todos.append(f"- [ ] **Performance: {rx[:80]}...** {rx} ({today}, {marker})")

    # Add a to-do only for rule types that were actually written (not deduped)
    if rules_written > 0:
        rule_summary = f"- [ ] **Performance: {rules_written} behavioral rule(s) written to CLAUDE.md.** Review and validate: {', '.join(written_types)}. ({today}, {marker})"
        if "behavioral rule(s) written" not in existing_todos:
            new_todos.append(rule_summary)

    if new_todos:
        with open(TODO_FILE, "a") as f:
            f.write("\n")
            for todo in new_todos:
                f.write(todo + "\n")
        todos_written = len(new_todos)

    return rules_written, todos_written


# ── Main ─────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv
    no_report = "--no-report" in sys.argv
    days = LOOKBACK_DAYS
    for i, arg in enumerate(sys.argv):
        if arg == "--days" and i + 1 < len(sys.argv):
            days = int(sys.argv[i + 1])

    print(f"Scanning sessions from last {days} days...")
    session_files = find_recent_sessions(days)
    print(f"Found {len(session_files)} session file(s).")

    if not session_files:
        print("No recent sessions found. Nothing to report.")
        return

    sessions_data = []
    agents_data = []
    for sf in session_files:
        print(f"  Analyzing: {sf.name[:40]}...")
        data = analyze_session(sf)
        sessions_data.append(data)
        agents = analyze_subagents(sf)
        agents_data.append(agents)

    report, prescriptions, prescription_types = generate_report(sessions_data, agents_data, days)

    # Ensure output directory exists
    PERFORMANCE_DIR.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    report_path = PERFORMANCE_DIR / f"weekly-{today}.md"

    if dry_run:
        print("\n--- DRY RUN (report not saved) ---\n")
        print(report)
        print(f"\nPrescriptions: {len(prescriptions)}")
        for p in prescriptions:
            print(f"  - {p}")
        return

    # Write report file (skip with --no-report for prescriptions-only mode)
    if not no_report:
        with open(report_path, "w") as f:
            f.write(report)
        print(f"Report saved: {report_path}")
    else:
        print("Report skipped (--no-report mode).")

    # Apply prescriptions: behavioral rules to MEMORY.md, investigations to Claude To-dos
    rules_written, todos_written = apply_prescriptions(prescriptions, prescription_types)
    if rules_written:
        print(f"Wrote {rules_written} behavioral rule(s) to CLAUDE.md")
    if todos_written:
        print(f"Appended {todos_written} item(s) to Claude To-dos.md")
    if not prescriptions:
        print("No prescriptions triggered.")

    print("Done.")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
