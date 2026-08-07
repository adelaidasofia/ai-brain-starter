#!/usr/bin/env python3
"""
instinct_lib.py — shared core for the Instinct Engine v2.

The Instinct Engine turns flat-file agent memories (feedback_*.md /
discovery_*.md) into a self-improving, confidence-weighted, project-scoped,
portable instinct library. This module is the safe substrate the CLI
(`instinct.py`) and the skills consume.

Design contracts (load-bearing — do not weaken without a regression test):

  1. SURGICAL frontmatter editing. We only ever touch four managed keys
     (confidence / observations / last_seen / project_id). Every other
     frontmatter line and the entire body are preserved byte-for-byte. We
     never reorder, reformat, or drop a key we did not author. This is what
     makes it safe to run `backfill` across hundreds of real memory files.

  2. Stdlib-only for the hot path. Frontmatter read/edit needs no third-party
     dependency. PyYAML is used ONLY for export/import (a separate, opt-in
     surface) and import fails LOUD if PyYAML is absent — never silently.

  3. Confidence math is bounded and principled (see CONFIDENCE section). A
     reinforce nudges up with diminishing returns; a correction halves; a
     staleness decay erodes after a grace period on a half-life curve.

  4. project_id defaults to "global". Existing memories ARE cross-project, so
     backfill never hides anything — project isolation is opt-in for FUTURE
     instincts.
"""

from __future__ import annotations

import sys
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIDENCE model
# ---------------------------------------------------------------------------
# Seed confidence maps the existing categorical `strength:` taxonomy
# (explicit | correction | implicit, codified in feedback_memory_durability)
# onto a 0..1 scale. Strongest signal (user stated the rule verbatim) seeds
# highest; an inferred-but-unconfirmed pattern seeds lowest.
SEED_BY_STRENGTH = {
    "explicit": 0.90,
    "correction": 0.75,
    "implicit": 0.50,
}
SEED_DEFAULT = 0.60  # memory with no strength field and no other signal

# When `strength:` is absent we seed from the memory TYPE + content. A
# `feedback_*` memory is a codified preference/correction by nature (higher
# confidence); a `discovery_*` memory is an audit/finding (informational).
# A feedback memory whose body carries hard-rule language is treated as an
# explicit codified rule. This is a numeric SEED only — it never sets the
# categorical `strength:` label (that stays judgment-gated, per the
# preference-strength rule); the seed decays and reinforces from here.
SEED_FEEDBACK_CODIFIED = 0.82
SEED_FEEDBACK = 0.72
SEED_DISCOVERY = 0.60
CODIFIED_SIGNALS = (
    "never", "always", "banned", "non-negotiable", "codified", " must ",
    "do not", "don't", "mandatory", "required", "critical", "rule:",
)

CONF_FLOOR = 0.05  # a corrected/stale instinct never hits 0 — it can recover
CONF_CEIL = 0.99   # never fully certain; leaves headroom for a future correction

REINFORCE_ALPHA = 0.15      # reinforce: c' = c + alpha*(1-c)  (diminishing returns)
CORRECTION_FACTOR = 0.50    # correct:   c' = max(floor, c*0.5) (sharp drop)

DECAY_GRACE_DAYS = 30       # no decay for the first month after last_seen
DECAY_HALFLIFE_DAYS = 180   # after grace, confidence halves every ~6 months unseen

# Clustering / evolve thresholds
EVOLVE_MIN_CONFIDENCE = 0.80   # a cluster must be this confident to propose a skill
EVOLVE_MIN_CLUSTER = 2         # ...and contain at least this many instincts

MANAGED_KEYS = ("confidence", "observations", "last_seen", "project_id")
INSTINCT_GLOBS = ("feedback_*.md", "discovery_*.md")

PROJECT_GLOBAL = "global"
PROJECT_VAULT = "personal-vault"


def clamp(x: float, lo: float = CONF_FLOOR, hi: float = CONF_CEIL) -> float:
    return max(lo, min(hi, x))


def seed_confidence(strength: str | None,
                    mtype: str | None = None,
                    text: str | None = None) -> float:
    """Initial confidence for a memory.

    An explicit `strength:` always wins. Absent that, seed from the memory
    type + content so the genuinely-codified rules are not flattened to the
    default. `mtype`/`text` are optional so read-time fallbacks can call this
    with just `strength`.
    """
    if strength:
        s = strength.strip().lower()
        if s in SEED_BY_STRENGTH:
            return SEED_BY_STRENGTH[s]
    t = (mtype or "").strip().lower()
    if t == "feedback":
        if text and any(sig in text.lower() for sig in CODIFIED_SIGNALS):
            return SEED_FEEDBACK_CODIFIED
        return SEED_FEEDBACK
    if t == "discovery":
        return SEED_DISCOVERY
    return SEED_DEFAULT


def reinforce_confidence(c: float) -> float:
    """A repeat observation with no contradiction nudges confidence up."""
    return clamp(c + REINFORCE_ALPHA * (1.0 - c))


def correct_confidence(c: float) -> float:
    """An explicit user correction halves confidence (sharp, recoverable)."""
    return clamp(c * CORRECTION_FACTOR)


def decayed_confidence(c: float, last_seen: date, today: date | None = None) -> float:
    """Staleness decay: flat for `grace` days, then a half-life curve."""
    today = today or date.today()
    days = (today - last_seen).days
    if days <= DECAY_GRACE_DAYS:
        return clamp(c)
    extra = days - DECAY_GRACE_DAYS
    return clamp(c * (0.5 ** (extra / DECAY_HALFLIFE_DAYS)))


# ---------------------------------------------------------------------------
# project scoping
# ---------------------------------------------------------------------------
def _git_remote_url(start: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            url = out.stdout.strip()
            return url or None
    except Exception:
        return None
    return None


def current_project_id(cwd: str | os.PathLike | None = None) -> str:
    """Stable project identity for the CURRENT working tree.

    - Inside the personal vault  -> "personal-vault"
    - A git repo with an origin  -> sha256(normalized-origin-url)[:12]
    - Anything else              -> "global" (never hide instincts on doubt)
    """
    p = Path(cwd or os.getcwd()).resolve()
    parts = p.parts
    # Vault detection: a path segment that is an "⚙️ Meta"/"Meta"-bearing vault.
    # The personal vault root contains a directory whose name ends with "Meta".
    probe = p
    for _ in range(12):
        try:
            if any(child.is_dir() and child.name.endswith("Meta") for child in probe.iterdir()):
                # Heuristic: a vault, not a code repo (code repos use docs/, not "⚙️ Meta").
                if any(child.name.endswith("Meta") and "⚙" in child.name for child in probe.iterdir()):
                    return PROJECT_VAULT
        except (OSError, PermissionError):
            pass
        if probe.parent == probe:
            break
        probe = probe.parent

    url = _git_remote_url(p)
    if url:
        norm = re.sub(r"\.git$", "", url.strip().lower())
        norm = re.sub(r"^https?://", "", norm)
        norm = re.sub(r"^git@([^:]+):", r"\1/", norm)
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]
    return PROJECT_GLOBAL


# ---------------------------------------------------------------------------
# memory-dir resolution
# ---------------------------------------------------------------------------
def resolve_memory_dir(explicit: str | None = None) -> Path | None:
    """Find the Agent Memory directory.

    Priority: explicit arg -> $INSTINCT_MEMORY_DIR -> walk up for a
    "*Meta/Agent Memory" dir -> the known personal-vault path.
    """
    if explicit:
        d = Path(explicit).expanduser()
        return d if d.is_dir() else None

    env = os.environ.get("INSTINCT_MEMORY_DIR")
    if env:
        d = Path(env).expanduser()
        if d.is_dir():
            return d

    probe = Path.cwd().resolve()
    # If we're in a worktree, the "⚙️ Meta" lives at the main vault, not here.
    for _ in range(10):
        for child_name in ("⚙️ Meta", "Meta"):
            cand = probe / child_name / "Agent Memory"
            if cand.is_dir():
                return cand
        if probe.parent == probe:
            break
        probe = probe.parent

    # Generic fallback: Claude Code's native per-project memory dir, which the
    # AI Brain Starter symlinks to the vault's "Agent Memory". Works for any
    # user without hardcoding a vault path.
    for cand in sorted((Path.home() / ".claude" / "projects").glob("*/memory")):
        if cand.is_dir():
            return cand
    return None


# ---------------------------------------------------------------------------
# frontmatter: surgical read / edit (stdlib only, byte-preserving)
# ---------------------------------------------------------------------------
_FM_LINE = re.compile(r"^([A-Za-z0-9_\-]+):\s?(.*)$")


@dataclass
class Instinct:
    path: Path
    raw: str                                  # full original file text
    fm_lines: list[str] = field(default_factory=list)  # frontmatter lines (no fences)
    body: str = ""                            # everything after the closing ---
    has_frontmatter: bool = False

    # parsed convenience fields (managed + a few read-only ones)
    @property
    def fm(self) -> dict[str, str]:
        d: dict[str, str] = {}
        for line in self.fm_lines:
            m = _FM_LINE.match(line)
            if m and not line.startswith(" "):  # top-level keys only
                d[m.group(1)] = m.group(2).strip()
        return d

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.fm.get(key, default)

    @property
    def slug(self) -> str:
        return self.path.stem

    @property
    def kind(self) -> str:
        return "feedback" if self.path.name.startswith("feedback_") else (
            "discovery" if self.path.name.startswith("discovery_") else "other")


def parse_instinct(path: Path) -> Instinct:
    raw = path.read_text(encoding="utf-8")
    lines = raw.split("\n")
    if not lines or lines[0].strip() != "---":
        return Instinct(path=path, raw=raw, has_frontmatter=False, body=raw)
    # find closing fence
    close = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close = i
            break
    if close is None:
        return Instinct(path=path, raw=raw, has_frontmatter=False, body=raw)
    fm_lines = lines[1:close]
    body = "\n".join(lines[close + 1:])
    return Instinct(path=path, raw=raw, fm_lines=fm_lines, body=body, has_frontmatter=True)


def set_managed_fields(inst: Instinct, updates: dict[str, object]) -> str:
    """Return new file text with ONLY the managed keys in `updates` set.

    Surgical: existing managed key lines are replaced in place; missing ones
    are appended at the end of the frontmatter block. All other frontmatter
    lines and the entire body are preserved exactly. Inserts a frontmatter
    block if the file has none (degenerate case; real memories all have one).
    """
    # normalize values to strings
    str_updates = {k: _fmt_value(v) for k, v in updates.items() if k in MANAGED_KEYS}
    if not str_updates:
        return inst.raw

    if not inst.has_frontmatter:
        fm = "\n".join(f"{k}: {v}" for k, v in str_updates.items())
        return f"---\n{fm}\n---\n{inst.raw}"

    new_fm: list[str] = list(inst.fm_lines)
    remaining = dict(str_updates)
    for idx, line in enumerate(new_fm):
        m = _FM_LINE.match(line)
        if m and not line.startswith(" "):
            key = m.group(1)
            if key in remaining:
                new_fm[idx] = f"{key}: {remaining.pop(key)}"
    # append any keys not already present
    for key in MANAGED_KEYS:
        if key in remaining:
            new_fm.append(f"{key}: {remaining.pop(key)}")

    return "---\n" + "\n".join(new_fm) + "\n---\n" + inst.body


def _fmt_value(v: object) -> str:
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".") if v != int(v) else f"{v:.1f}"
    if isinstance(v, date) and not isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def write_instinct(inst: Instinct, new_text: str, backup: bool = True) -> bool:
    """Write new_text to inst.path. Idempotent: no write if unchanged.

    On first managed-field write to a file, a single .bak-instinct copy is
    made (never overwritten on later runs) so the pre-engine state is always
    recoverable.
    """
    if new_text == inst.raw:
        return False
    if backup:
        bak = inst.path.with_suffix(inst.path.suffix + ".bak-instinct")
        if not bak.exists():
            try:
                bak.write_text(inst.raw, encoding="utf-8")
            except OSError:
                pass
    inst.path.write_text(new_text, encoding="utf-8")
    return True


def iter_instinct_paths(memory_dir: Path):
    seen: set[Path] = set()
    for pattern in INSTINCT_GLOBS:
        for p in sorted(memory_dir.glob(pattern)):
            if p.suffix == ".md" and ".bak" not in p.name and p not in seen:
                seen.add(p)
                yield p


def file_mtime_date(path: Path) -> date:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date()
    except OSError:
        return date.today()


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip().strip('"').strip("'")
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s[:len(fmt) + 2] if "T" in fmt else s[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_float(s: str | None, default: float | None = None) -> float | None:
    if s is None:
        return default
    try:
        return float(str(s).strip())
    except (TypeError, ValueError):
        return default


def parse_int(s: str | None, default: int = 0) -> int:
    try:
        return int(float(str(s).strip()))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# instinct domain inference (for /evolve clustering + export)
# ---------------------------------------------------------------------------
DOMAIN_KEYWORDS = {
    "voice": ["voice", "em dash", "humaniz", "substack", "prose", "tone", "firewall"],
    "git": ["git", "commit", "branch", "worktree", "push", "merge", "rebase"],
    "linear": ["linear", "myc-", "ond-", "issue", "kickoff"],
    "security": ["secret", "ssrf", "scrub", "inject", "vuln", "cve", "sandbox", "hardening"],
    "memory": ["memory", "instinct", "confidence", "frontmatter", "vault", "durab"],
    "build": ["build", "tdd", "test", "deploy", "ci", "mcp", "hook", "regression"],
    "panel": ["panel", "advisory", "coaching", "dissent"],
    "ops": ["session", "close", "broadcast", "delegat", "calendar", "email"],
}


def infer_domain(inst: Instinct) -> str:
    hay = (inst.get("name", "") + " " + inst.get("description", "") + " "
           + inst.slug + " " + inst.body[:400]).lower()
    best, best_hits = "general", 0
    for domain, kws in DOMAIN_KEYWORDS.items():
        hits = sum(1 for kw in kws if kw in hay)
        if hits > best_hits:
            best, best_hits = domain, hits
    return best


if __name__ == "__main__":
    # tiny smoke when run directly
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    import sys
    print("instinct_lib OK; current_project_id:", current_project_id())
    md = resolve_memory_dir(sys.argv[1] if len(sys.argv) > 1 else None)
    print("memory_dir:", md)
    if md:
        print("instinct files:", sum(1 for _ in iter_instinct_paths(md)))
