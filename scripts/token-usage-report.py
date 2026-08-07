#!/usr/bin/env python3
"""
token-usage-report.py — real per-session token tracker + tool breakdown + prescriptions.

Walks Claude Code session JSONLs in ~/.claude/projects/<vault-hash>/, extracts
assistant `usage` blocks AND tool_use blocks, aggregates by session, day, model,
cwd, and tool name. Cross-references with vault session-summary files. Estimates
USD cost using current Anthropic pricing. Computes prescriptive recommendations.

Output: <VAULT_ROOT>/⚙️ Meta/Token Usage Report.md (overwrite).

Why: Claude Code does not surface per-session token totals OR per-tool token
breakdowns OR prescriptive routing recommendations in any UI. The session JSONL
is the only ground-truth log.

Pricing reference (May 2026; update PRICING dict when rates change):
  Sonnet 4.6: $3/M input, $15/M output, $3.75/M cache write 1h,
              $0.30/M cache read, $0.75/M cache write 5m
  Opus 4.7:   $15/M input, $75/M output, $18.75/M cache write 1h,
              $1.50/M cache read, $3.75/M cache write 5m
  Haiku 4.5:  $1/M input, $5/M output, $1.25/M cache write 1h,
              $0.10/M cache read, $0.25/M cache write 5m

Usage:
  VAULT_ROOT=/path/to/vault python3 token-usage-report.py
  VAULT_ROOT=/path/to/vault python3 token-usage-report.py --days 30
  VAULT_ROOT=/path/to/vault python3 token-usage-report.py --days 7 --top 20
  VAULT_ROOT=/path/to/vault python3 token-usage-report.py --no-compare
  VAULT_ROOT=/path/to/vault python3 token-usage-report.py --session SESSION_ID

Auto-detects SESSIONS_DIR from VAULT_ROOT path: replaces all `/` with `-` and
prepends `~/.claude/projects/`. Override with CLAUDE_SESSIONS_DIR env var.

Inspired by JuliusBrussee/caveman's /caveman-stats. Two layers added 2026-05-09:
per-message model attribution (not equal-split per session) + tool-call breakdown
+ per-cwd aggregation + week-over-week comparison + prescriptive recommendations
(cost-if-Sonnet-equivalent, force-close savings estimate, cache reuse ratio,
expensive-tool flagging).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Auto-detect VAULT_ROOT from script location (scripts/ at vault root) or env var
_SCRIPT_DIR = Path(__file__).resolve().parent
VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", str(_SCRIPT_DIR.parent)))


def derive_sessions_dir(vault_root: Path) -> Path:
    """Claude Code stores session JSONLs under ~/.claude/projects/<sanitized>/.

    The sanitized name is the absolute vault path with `/` replaced by `-` and
    a leading `-` prepended. Override via CLAUDE_SESSIONS_DIR env var if needed.
    """
    override = os.environ.get("CLAUDE_SESSIONS_DIR")
    if override:
        return Path(override)
    sanitized = "-" + str(vault_root.resolve()).lstrip("/").replace("/", "-")
    return Path.home() / ".claude" / "projects" / sanitized


SESSIONS_DIR = derive_sessions_dir(VAULT_ROOT)
META_DIR = VAULT_ROOT / "⚙️ Meta"
VAULT_SESSIONS_DIR = META_DIR / "Sessions"
OUTPUT = META_DIR / "Token Usage Report.md"

# Per-million-token pricing in USD. Update when Anthropic shifts.
PRICING = {
    "sonnet": {
        "input": 3.0,
        "output": 15.0,
        "cache_write_1h": 3.75,
        "cache_write_5m": 0.75,
        "cache_read": 0.30,
    },
    "opus": {
        "input": 15.0,
        "output": 75.0,
        "cache_write_1h": 18.75,
        "cache_write_5m": 3.75,
        "cache_read": 1.50,
    },
    "haiku": {
        "input": 1.0,
        "output": 5.0,
        "cache_write_1h": 1.25,
        "cache_write_5m": 0.25,
        "cache_read": 0.10,
    },
}


def model_family(model: str) -> str:
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    return "sonnet"


def cost_usd(usage: dict, model: str) -> float:
    fam = model_family(model)
    p = PRICING[fam]
    cc = usage.get("cache_creation", {}) or {}
    return (
        usage.get("input_tokens", 0) * p["input"]
        + usage.get("output_tokens", 0) * p["output"]
        + cc.get("ephemeral_1h_input_tokens", 0) * p["cache_write_1h"]
        + cc.get("ephemeral_5m_input_tokens", 0) * p["cache_write_5m"]
        + usage.get("cache_read_input_tokens", 0) * p["cache_read"]
    ) / 1_000_000


def cost_if_sonnet(usage: dict) -> float:
    """What this turn WOULD have cost on Sonnet. Used for opus→sonnet savings estimates."""
    p = PRICING["sonnet"]
    cc = usage.get("cache_creation", {}) or {}
    return (
        usage.get("input_tokens", 0) * p["input"]
        + usage.get("output_tokens", 0) * p["output"]
        + cc.get("ephemeral_1h_input_tokens", 0) * p["cache_write_1h"]
        + cc.get("ephemeral_5m_input_tokens", 0) * p["cache_write_5m"]
        + usage.get("cache_read_input_tokens", 0) * p["cache_read"]
    ) / 1_000_000


def parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def walk_sessions(cutoff: datetime | None, cutoff_end: datetime | None = None) -> tuple[dict, dict, dict, dict]:
    """Walk session JSONLs.

    Returns (sessions, by_model, by_cwd, by_tool).
      sessions: {session_id: {start, end, cwd, models, turns, totals, tools, sonnet_equivalent_cost}}
      by_model: {family: {input, output, cache_write, cache_read, cost, turns}}
      by_cwd: {short_cwd: {sessions: int, turns, cost, models}}
      by_tool: {tool_name: {count, sessions: set}}
    """
    if not SESSIONS_DIR.exists():
        print(f"sessions dir not found: {SESSIONS_DIR}", file=sys.stderr)
        return {}, {}, {}, {}

    sessions: dict[str, dict] = {}
    by_model: dict[str, dict] = defaultdict(
        lambda: {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "cost": 0.0, "turns": 0}
    )
    by_cwd: dict[str, dict] = defaultdict(
        lambda: {"sessions": set(), "turns": 0, "cost": 0.0, "models": set()}
    )
    by_tool: dict[str, dict] = defaultdict(lambda: {"count": 0, "sessions": set()})

    for jsonl_path in SESSIONS_DIR.glob("*.jsonl"):
        sid = jsonl_path.stem
        if cutoff:
            mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                continue
        try:
            with jsonl_path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if r.get("type") != "assistant":
                        continue
                    m = r.get("message")
                    if not isinstance(m, dict):
                        continue
                    usage = m.get("usage")
                    if not usage:
                        continue
                    ts = parse_ts(r.get("timestamp"))
                    if cutoff and ts and ts < cutoff:
                        continue
                    if cutoff_end and ts and ts >= cutoff_end:
                        continue
                    if sid not in sessions:
                        sessions[sid] = {
                            "session_id": sid,
                            "start": ts,
                            "end": ts,
                            "cwd": r.get("cwd", "?"),
                            "models": set(),
                            "turns": 0,
                            "input": 0,
                            "output": 0,
                            "cache_write_1h": 0,
                            "cache_write_5m": 0,
                            "cache_read": 0,
                            "cost": 0.0,
                            "sonnet_equivalent_cost": 0.0,
                            "tools": defaultdict(int),
                            "opus_turns": 0,
                            "sonnet_turns": 0,
                            "haiku_turns": 0,
                        }
                    s = sessions[sid]
                    if ts:
                        if not s["start"] or ts < s["start"]:
                            s["start"] = ts
                        if not s["end"] or ts > s["end"]:
                            s["end"] = ts
                    model = m.get("model", "")
                    if model:
                        s["models"].add(model)
                    s["turns"] += 1

                    in_tokens = usage.get("input_tokens", 0)
                    out_tokens = usage.get("output_tokens", 0)
                    cc = usage.get("cache_creation", {}) or {}
                    cw_1h = cc.get("ephemeral_1h_input_tokens", 0)
                    cw_5m = cc.get("ephemeral_5m_input_tokens", 0)
                    cr = usage.get("cache_read_input_tokens", 0)
                    msg_cost = cost_usd(usage, model)
                    msg_cost_sonnet = cost_if_sonnet(usage)

                    s["input"] += in_tokens
                    s["output"] += out_tokens
                    s["cache_write_1h"] += cw_1h
                    s["cache_write_5m"] += cw_5m
                    s["cache_read"] += cr
                    s["cost"] += msg_cost
                    s["sonnet_equivalent_cost"] += msg_cost_sonnet

                    fam = model_family(model)
                    s[f"{fam}_turns"] += 1
                    bm = by_model[fam]
                    bm["input"] += in_tokens
                    bm["output"] += out_tokens
                    bm["cache_write"] += cw_1h + cw_5m
                    bm["cache_read"] += cr
                    bm["cost"] += msg_cost
                    bm["turns"] += 1

                    content = m.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tool_name = block.get("name", "?")
                                s["tools"][tool_name] += 1
                                by_tool[tool_name]["count"] += 1
                                by_tool[tool_name]["sessions"].add(sid)
        except OSError as e:
            print(f"skip {jsonl_path.name}: {e}", file=sys.stderr)

    for s in sessions.values():
        cwd_key = short_cwd(s["cwd"])
        bc = by_cwd[cwd_key]
        bc["sessions"].add(s["session_id"])
        bc["turns"] += s["turns"]
        bc["cost"] += s["cost"]
        for mdl in s["models"]:
            bc["models"].add(mdl)

    return sessions, dict(by_model), dict(by_cwd), dict(by_tool)


def format_int(n: int) -> str:
    return f"{n:,}"


def short_cwd(cwd: str) -> str:
    if not cwd or cwd == "?":
        return "?"
    if "worktrees/" in cwd:
        return "wt:" + cwd.split("worktrees/", 1)[1].split("/", 1)[0][:24]
    if str(VAULT_ROOT) in cwd:
        return "vault"
    return Path(cwd).name[:24]


def primary_model(models: set[str]) -> str:
    if not models:
        return "?"
    fams = [model_family(m) for m in models]
    if "opus" in fams and "sonnet" in fams:
        return "opus→sonnet"
    if "opus" in fams:
        return "opus"
    if "haiku" in fams:
        return "haiku"
    return "sonnet"


def find_vault_session_summary(session: dict) -> tuple[str | None, str | None]:
    """Match a JSONL session to a vault session-summary file by worktree slug + date."""
    if not VAULT_SESSIONS_DIR.exists() or not session.get("start"):
        return None, None
    cwd = session.get("cwd", "")
    slug = None
    if "worktrees/" in cwd:
        slug = cwd.split("worktrees/", 1)[1].split("/", 1)[0]
    target_date = session["start"].astimezone().strftime("%Y-%m-%d")
    target_date_compact = session["start"].astimezone().strftime("%Y%m%d")
    candidates = []
    for path in VAULT_SESSIONS_DIR.glob("*.md"):
        name = path.name
        if slug and slug in name:
            candidates.append((path, 0))
            continue
        if target_date in name or target_date_compact in name:
            candidates.append((path, 1))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[1])
    chosen = candidates[0][0]
    try:
        text = chosen.read_text(encoding="utf-8")
        body = text
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end > 0:
                body = text[end + 5:]
        excerpt = ""
        for para in body.split("\n\n"):
            para = para.strip()
            if not para or para.startswith("#"):
                continue
            excerpt = para[:180].replace("\n", " ")
            break
        return chosen.name, excerpt or "(no body excerpt)"
    except OSError:
        return chosen.name, "(read error)"


def render_report(
    sessions: dict,
    by_model: dict,
    by_cwd: dict,
    by_tool: dict,
    days: int,
    top_n: int,
    prev_window: dict | None = None,
) -> str:
    if not sessions:
        return "# Token Usage Report\n\n(no sessions found)\n"

    rows = list(sessions.values())
    total_in = sum(s["input"] for s in rows)
    total_out = sum(s["output"] for s in rows)
    total_cw1h = sum(s["cache_write_1h"] for s in rows)
    total_cw5m = sum(s["cache_write_5m"] for s in rows)
    total_cr = sum(s["cache_read"] for s in rows)
    total_cost = sum(s["cost"] for s in rows)

    by_day: dict[str, dict] = defaultdict(lambda: {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "cost": 0.0, "sessions": 0, "turns": 0})
    for s in rows:
        if not s["start"]:
            continue
        d = s["start"].astimezone().strftime("%Y-%m-%d")
        by_day[d]["input"] += s["input"]
        by_day[d]["output"] += s["output"]
        by_day[d]["cache_write"] += s["cache_write_1h"] + s["cache_write_5m"]
        by_day[d]["cache_read"] += s["cache_read"]
        by_day[d]["cost"] += s["cost"]
        by_day[d]["sessions"] += 1
        by_day[d]["turns"] += s["turns"]

    out = ["# Token Usage Report", ""]
    out.append(f"*Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} from `~/.claude/projects/.../*.jsonl`. Window: last {days} days. Source: `scripts/token-usage-report.py`.*")
    out.append("")
    out.append("> **Caveat**: Counts every assistant turn the SDK emitted, including tool-use turns. Cache reads dominate after the first turn of any cached system prompt; raw input is small once warm. USD figures are estimates based on May-2026 Anthropic pricing.")
    out.append("")

    out.append("## Window totals")
    out.append("")
    out.append(f"- **Sessions**: {len(rows):,}")
    out.append(f"- **Assistant turns**: {sum(s['turns'] for s in rows):,}")
    out.append(f"- **Input tokens (uncached)**: {format_int(total_in)}")
    out.append(f"- **Output tokens**: {format_int(total_out)}")
    out.append(f"- **Cache write (1h+5m)**: {format_int(total_cw1h + total_cw5m)}")
    out.append(f"- **Cache read**: {format_int(total_cr)}")
    out.append(f"- **Estimated cost**: **${total_cost:,.2f}**")
    if prev_window:
        prev_cost = prev_window.get("cost", 0.0)
        prev_sessions = prev_window.get("sessions", 0)
        prev_turns = prev_window.get("turns", 0)
        cost_delta = total_cost - prev_cost
        cost_pct = (cost_delta / prev_cost * 100) if prev_cost > 0.01 else 0.0
        arrow = "↑" if cost_delta > 0 else ("↓" if cost_delta < 0 else "→")
        out.append("")
        out.append(f"**Week-over-week** (prior {days}d window):")
        out.append(f"- Cost: ${prev_cost:,.2f} → ${total_cost:,.2f} ({arrow} ${abs(cost_delta):,.2f}, {cost_pct:+.0f}%)")
        if prev_sessions:
            out.append(f"- Sessions: {prev_sessions} → {len(rows)} ({(len(rows)-prev_sessions)/prev_sessions*100:+.0f}%)")
        if prev_turns:
            out.append(f"- Turns: {format_int(prev_turns)} → {format_int(sum(s['turns'] for s in rows))} ({(sum(s['turns'] for s in rows)-prev_turns)/prev_turns*100:+.0f}%)")
    out.append("")

    out.append("## Per-day")
    out.append("")
    out.append("| Date | Sessions | Turns | Input | Output | Cache wr | Cache rd | Cost (USD) |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for d in sorted(by_day.keys(), reverse=True):
        v = by_day[d]
        out.append(f"| {d} | {v['sessions']} | {v['turns']:,} | {format_int(v['input'])} | {format_int(v['output'])} | {format_int(v['cache_write'])} | {format_int(v['cache_read'])} | ${v['cost']:.2f} |")
    out.append("")

    out.append("## Per-model")
    out.append("")
    out.append("| Model | Turns | Input | Output | Cache wr | Cache rd | Cost (USD) |")
    out.append("|---|---:|---:|---:|---:|---:|---:|")
    for fam in sorted(by_model.keys(), key=lambda f: -by_model[f]["cost"]):
        v = by_model[fam]
        out.append(f"| {fam} | {v['turns']:,} | {format_int(v['input'])} | {format_int(v['output'])} | {format_int(v['cache_write'])} | {format_int(v['cache_read'])} | ${v['cost']:.2f} |")
    out.append("")

    out.append("## Per-worktree (where the spend lands)")
    out.append("")
    cwd_rows = [{"cwd": k, "sessions": len(v["sessions"]), "turns": v["turns"], "cost": v["cost"], "models": v["models"]} for k, v in by_cwd.items()]
    cwd_rows.sort(key=lambda x: -x["cost"])
    out.append("| Worktree / cwd | Sessions | Turns | Models | Cost (USD) | % of total |")
    out.append("|---|---:|---:|---|---:|---:|")
    for c in cwd_rows[:15]:
        pct = c["cost"] / max(total_cost, 0.01) * 100
        models_short = ",".join(sorted({model_family(m) for m in c["models"]}))
        out.append(f"| {c['cwd']} | {c['sessions']} | {c['turns']:,} | {models_short} | ${c['cost']:.2f} | {pct:.0f}% |")
    out.append("")

    rows_sorted = sorted(rows, key=lambda s: -s["cost"])[:top_n]
    out.append(f"## Top {top_n} sessions by cost (with vault summary cross-ref)")
    out.append("")
    out.append("| Start | Worktree | Model | Turns | Cost | If Sonnet | Top tool | Vault session-file |")
    out.append("|---|---|---|---:|---:|---:|---|---|")
    for s in rows_sorted:
        ts = s["start"].astimezone().strftime("%Y-%m-%d %H:%M") if s["start"] else "?"
        top_tool = "—"
        if s["tools"]:
            top_name = max(s["tools"].items(), key=lambda kv: kv[1])
            top_tool = f"{top_name[0]}×{top_name[1]}"
        sonnet_eq = s["sonnet_equivalent_cost"]
        sonnet_eq_label = f"${sonnet_eq:.0f}" if primary_model(s["models"]) != "sonnet" else "—"
        vault_file, _ = find_vault_session_summary(s)
        vault_link = f"[[{vault_file[:-3]}]]" if vault_file else "(none)"
        out.append(f"| {ts} | {short_cwd(s['cwd'])} | {primary_model(s['models'])} | {s['turns']:,} | ${s['cost']:.0f} | {sonnet_eq_label} | {top_tool} | {vault_link} |")
    out.append("")

    out.append("## Tool calls (window total)")
    out.append("")
    tool_rows = sorted([(name, v["count"], len(v["sessions"])) for name, v in by_tool.items()], key=lambda x: -x[1])
    out.append("| Tool | Calls | Sessions used | Avg calls/session |")
    out.append("|---|---:|---:|---:|")
    for name, count, n_sessions in tool_rows[:20]:
        avg = count / max(n_sessions, 1)
        out.append(f"| `{name}` | {count:,} | {n_sessions} | {avg:.1f} |")
    out.append("")

    out.append("## Drift signals + prescriptions")
    out.append("")
    if rows_sorted:
        biggest = rows_sorted[0]
        out.append(f"- **Single biggest session**: ${biggest['cost']:.2f} on {biggest['start'].strftime('%Y-%m-%d') if biggest['start'] else '?'} ({biggest['turns']} turns, {primary_model(biggest['models'])})")

    if by_model.get("opus") and total_cost > 0.01:
        opus_cost = by_model["opus"]["cost"]
        opus_share = opus_cost / total_cost * 100
        out.append(f"- **Opus cost share**: {opus_share:.0f}% of total spend (${opus_cost:.2f}/${total_cost:.2f})")
        if opus_share > 60:
            full_sonnet_cost = sum(s["sonnet_equivalent_cost"] for s in rows)
            full_savings = total_cost - full_sonnet_cost
            out.append(f"  - ⚠ Above 60% — your model-routing rule (default Sonnet for execution) suggests review.")
            out.append(f"  - **If every turn this window had been Sonnet**: ~${full_sonnet_cost:.0f} instead of ${total_cost:.0f} (savings ~${full_savings:.0f}). Even half-converting saves ~${full_savings / 2:.0f}.")

    if rows:
        avg_turns = sum(s["turns"] for s in rows) / len(rows)
        long_sessions = [s for s in rows if s["turns"] >= 60]
        very_long = [s for s in rows if s["turns"] >= 100]
        out.append(f"- **Average turns/session**: {avg_turns:.1f}")
        if long_sessions:
            long_cost = sum(s["cost"] for s in long_sessions)
            tail_cost_estimate = sum(s["cost"] * max(0, s["turns"] - 60) / s["turns"] for s in long_sessions)
            out.append(f"- **Sessions ≥60 turns** (force-close threshold): {len(long_sessions)} — ${long_cost:.2f} total")
            out.append(f"  - **Estimated cost of the tail past turn-60 in those sessions**: ~${tail_cost_estimate:.0f}.")
        if very_long:
            out.append(f"- **Sessions ≥100 turns**: {len(very_long)} — these are the worst length-cap violations")

    if total_cw1h + total_cw5m > 0:
        cache_reuse = total_cr / max(total_cw1h + total_cw5m, 1)
        out.append(f"- **Cache reuse ratio** (read / write): {cache_reuse:.1f}× — higher is better. <3× means context kept exploding without reuse.")

    expensive_tools_in_top_sessions = defaultdict(int)
    for s in rows_sorted[:5]:
        for tool, n in s["tools"].items():
            if n >= 20:
                expensive_tools_in_top_sessions[tool] += 1
    if expensive_tools_in_top_sessions:
        flagged = ", ".join(f"`{t}` (in {n}/5)" for t, n in expensive_tools_in_top_sessions.items())
        out.append(f"- **Top-5 sessions ran these tools heavily** (≥20 calls): {flagged}.")

    out.append("")
    out.append("---")
    out.append("")
    out.append("*Re-run anytime with* `VAULT_ROOT=/path/to/vault python3 scripts/token-usage-report.py`. *Default window: 30 days. Pass `--days N` for custom window. `--top N` for more rows. `--no-compare` to skip W/W. `--session SESSION_ID` for forensic deep-dive.*")
    return "\n".join(out)


def render_session_drilldown(session_id: str) -> str:
    jsonl = SESSIONS_DIR / f"{session_id}.jsonl"
    if not jsonl.exists():
        return f"# Session {session_id}\n\n(jsonl not found)\n"
    out = [f"# Session {session_id} — drilldown", ""]
    cumulative = 0.0
    turn_n = 0
    try:
        with jsonl.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("type") != "assistant":
                    continue
                m = r.get("message")
                if not isinstance(m, dict) or not m.get("usage"):
                    continue
                turn_n += 1
                model = m.get("model", "?")
                cost = cost_usd(m["usage"], model)
                cumulative += cost
                ts = parse_ts(r.get("timestamp"))
                ts_str = ts.astimezone().strftime("%H:%M:%S") if ts else "?"
                tools = []
                text_excerpt = ""
                content = m.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "tool_use":
                                tools.append(block.get("name", "?"))
                            elif block.get("type") == "text" and not text_excerpt:
                                text_excerpt = (block.get("text", "") or "")[:80].replace("\n", " ")
                tools_str = ", ".join(tools) if tools else "—"
                out.append(f"**Turn {turn_n}** [{ts_str}] {model_family(model)} ${cost:.3f} (cum ${cumulative:.2f}) — tools: {tools_str} — text: {text_excerpt!r}")
    except OSError as e:
        out.append(f"(read error: {e})")
    out.append("")
    out.append(f"**Total turns**: {turn_n}, **Total cost**: ${cumulative:.2f}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1] if __doc__ else "")
    ap.add_argument("--days", type=int, default=30, help="window in days (default 30)")
    ap.add_argument("--top", type=int, default=15, help="N top sessions to list (default 15)")
    ap.add_argument("--no-compare", action="store_true", help="skip week-over-week comparison")
    ap.add_argument("--session", type=str, help="forensic drilldown on one session ID")
    args = ap.parse_args()

    if args.session:
        report = render_session_drilldown(args.session)
        out_path = META_DIR / f"Token Usage - Session {args.session[:8]}.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"wrote {out_path}")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    sessions, by_model, by_cwd, by_tool = walk_sessions(cutoff)
    prev_window = None
    if not args.no_compare:
        prev_cutoff = cutoff - timedelta(days=args.days)
        prev_sessions, _, _, _ = walk_sessions(prev_cutoff, cutoff)
        prev_window = {
            "cost": sum(s["cost"] for s in prev_sessions.values()),
            "sessions": len(prev_sessions),
            "turns": sum(s["turns"] for s in prev_sessions.values()),
        }

    report = render_report(sessions, by_model, by_cwd, by_tool, args.days, args.top, prev_window)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(report, encoding="utf-8")
    print(f"wrote {OUTPUT} ({len(sessions)} sessions, {sum(s['turns'] for s in sessions.values()):,} turns)")
    print(f"total cost (window {args.days}d): ${sum(s['cost'] for s in sessions.values()):,.2f}")
    if prev_window:
        delta = sum(s['cost'] for s in sessions.values()) - prev_window["cost"]
        print(f"vs prior {args.days}d: ${prev_window['cost']:,.2f} ({delta:+,.2f})")


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    main()
