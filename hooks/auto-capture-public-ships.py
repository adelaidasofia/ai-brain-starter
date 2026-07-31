#!/usr/bin/env python3
"""SessionEnd hook: auto-capture public substrate ships to the user's consulting brand Pending Signals.

Scans public repos under ~/dev/* for git commits in the last 24h (day boundary
5:30 AM, measured in `$VAULT_TZ` or this machine's local zone — see resolve_tz()).
Appends one-line bullets per repo to today's Pending Signals/<date>.md.
Idempotent: skips repos already represented in today's file.

Karpathy-dissent safe: public repos only. Personal-data scrub gate prevents leaks.
Private repos (private-memory-repo, private-concierge-repo, private-org/*) NOT touched
because their commit messages can contain client/strategy context — those still
require manual paste per /journal Step 8.7.

Bypass: `AUTO_CAPTURE_SHIPS_BYPASS=1`.
"""
from __future__ import annotations

# utf8-stdout-ok: every console write in this module is `print(json.dumps(...))`,
# and json.dumps defaults to ensure_ascii=True, so non-ASCII is escaped to \uXXXX
# BEFORE it reaches stdout -- verified for accented text and emoji alike. That
# holds for the runtime-resolved zone name too, which on Windows is a local zone
# name the OS has already localised into the user's own language. The emoji in
# PENDING_SUBPATH is a filesystem path, never printed. So there is no cp1252
# console crash to guard against here; the reconfigure shim would be cargo-cult.
# Replaces this file's SEV-4-json-encoded row in scripts/utf8-stdout-baseline.txt,
# per that file's rule: rows are DELETED, never re-pinned to stay quiet.

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from _lib.vault_root import vault_root_for  # noqa: E402
except Exception:  # fail-open: a SessionEnd capture must never break the close
    def vault_root_for(target: Path):  # type: ignore
        return None

PENDING_SUBPATH = ("🍄 the user's consulting brand", "📋 Pending Signals")
DEV = Path.home() / "dev"


def resolve_tz() -> tzinfo:
    """The zone the 5:30 AM day boundary is measured in.

    `$VAULT_TZ` when it names a real IANA key; otherwise this machine's own
    local UTC offset. Deliberately TOTAL -- it never raises. It is called at
    import, and this is a SessionEnd hook: anything that throws while resolving
    a timezone takes the session close down with it.

    Why this is not a bare `ZoneInfo(...)` constant: the module shipped as
        TZ = ZoneInfo("America/user-local-tz")
    an unsubstituted template placeholder that is not an IANA key. Nothing in
    the repo ever substituted it, so every install raised ZoneInfoNotFoundError
    at IMPORT, before main() ran -- the hook exited 1 on every machine and never
    captured a single ship. An import-time crash is the maximally silent
    failure: there is no partial run to notice.

    The fallback is load-bearing well beyond that one bug. Windows CPython has
    no system tz database, so `ZoneInfo("America/New_York")` -- a perfectly valid
    key -- raises the SAME error unless the PyPI `tzdata` package happens to be
    installed. A fixed local offset is less correct than a named zone across a
    DST transition, and enormously more correct than not running at all.

    No default city is guessed. The placeholder means the intended zone is
    unknown; inventing one would file ships under the wrong day silently, which
    is the failure mode this hook is supposed to prevent.
    """
    name = os.environ.get("VAULT_TZ", "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except Exception:  # unknown key, absent tzdata, or a malformed value
            pass
    # Guaranteed aware: .astimezone() on a naive datetime attaches the local zone.
    return datetime.now().astimezone().tzinfo


TZ = resolve_tz()

PUBLIC_REPOS = [
    "ai-brain-starter",
    "humanizer",
    "mycelium-site",
    "slack-mcp",
    "imessage-mcp",
    "parse-mcp",
    "luma-mcp",
    "graph-query-mcp",
    "github-mcp",
    "whatsapp-mcp",
    "apollo-mcp",
    "google-workspace-mcp",
    "substack-mcp",
    "rescuetime-mcp",
    "investor-relations-mcp",
]


def target_date() -> str:
    now = datetime.now(TZ)
    if now.hour < 5 or (now.hour == 5 and now.minute < 30):
        target = now - timedelta(days=1)
    else:
        target = now
    return target.strftime("%Y-%m-%d")


def window_start() -> datetime:
    td = datetime.strptime(target_date(), "%Y-%m-%d")
    return datetime(td.year, td.month, td.day, 5, 30, tzinfo=TZ)


def commits_today(repo: Path, since: datetime) -> list[str]:
    if not (repo / ".git").exists():
        return []
    iso = since.strftime("%Y-%m-%d %H:%M:%S %z")
    try:
        out = subprocess.run(
            ["git", "log", f"--since={iso}", "--pretty=format:%s", "--no-merges"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return [s for s in out.stdout.splitlines() if s.strip()]
    except (subprocess.TimeoutExpired, OSError):
        return []


def existing_repos(file: Path) -> set[str]:
    if not file.exists():
        return set()
    text = file.read_text()
    return {r for r in PUBLIC_REPOS if f"`{r}`" in text}


def pending_dir() -> Path | None:
    """Where today's Pending Signals file lives, in the vault owning this cwd.

    MYC-3529 — resolved per invocation, not from a naive
    `os.environ.get("VAULT_ROOT", str(Path.home() / "vault"))` at import. This
    hook WRITES, and append_bullets() calls mkdir(parents=True), so the naive
    default did not merely read the wrong place: UNSET, on every vault not
    literally named "vault", it CREATED `~/vault/🍄 .../📋 Pending Signals/` and
    filed the day's ships into a phantom vault the user never opens. The signals
    were not lost loudly, they were lost quietly, which is worse.

    None = no vault identifiable from the cwd and no $VAULT_ROOT fallback. The
    caller must skip; there is no correct place to invent.
    """
    vault = vault_root_for(Path.cwd())
    if vault is None:
        return None
    return vault.joinpath(*PENDING_SUBPATH)


def append_bullets(pending: Path, bullets: list[str]) -> None:
    if not bullets:
        return
    pending.mkdir(parents=True, exist_ok=True)
    file = pending / f"{target_date()}.md"
    if not file.exists():
        file.write_text(
            f"---\ntype: pending-signals\nworkspace: mycelium\ncreated: {target_date()}\n---\n\n"
        )
    text = file.read_text().rstrip() + "\n\n" + "\n\n".join(bullets) + "\n"
    file.write_text(text)


def main() -> int:
    if os.environ.get("AUTO_CAPTURE_SHIPS_BYPASS") == "1":
        print(json.dumps({"continue": True, "suppressOutput": True}))
        return 0

    pending = pending_dir()
    if pending is None:
        # No vault to file into. Say so instead of inventing ~/vault.
        print(json.dumps({"continue": True, "suppressOutput": True, "tz": str(TZ),
                          "captured": 0, "skipped": "no vault resolved"}))
        return 0

    file = pending / f"{target_date()}.md"
    seen = existing_repos(file)
    since = window_start()
    captured_at = datetime.now(TZ).strftime("%H:%M")
    bullets = []

    for name in PUBLIC_REPOS:
        if name in seen:
            continue
        repo_path = DEV / name
        commits = commits_today(repo_path, since)
        if not commits:
            continue
        body = "; ".join(c.replace("`", "'") for c in commits[:6])
        if len(commits) > 6:
            body += f"; +{len(commits) - 6} more"
        bullets.append(
            f"- `{name}` shipped today ({len(commits)} commits): {body}. "
            f"— source: `~/dev/{name}` git log · captured: {captured_at} (auto)"
        )

    append_bullets(pending, bullets)
    # `tz` is reported, not assumed: an unset/bogus $VAULT_TZ degrades to a local
    # offset, and a degradation nobody can see is the bug this hook just had.
    print(json.dumps({"continue": True, "suppressOutput": True, "tz": str(TZ),
                      "captured": len(bullets)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
