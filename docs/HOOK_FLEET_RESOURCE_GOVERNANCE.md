---
name: hook-fleet-resource-governance
description: The SessionStart hook fleet this repo ships is what EVERY install runs (free self-install and paid commercial installs alike). Its resource-governance invariant + provenance + how it's verified.
---

# Hook-fleet resource governance

## Why this doc exists

This repo's `hooks.json` is the source of truth for the Claude Code SessionStart
hook fleet. That fleet is what runs on **every** install — a free self-install
and a paid commercial install built on this substrate are the **same fleet**.
There is no separate "paid" hook fleet.

A SessionStart hook fires once per Claude Code session. A machine running many
concurrent sessions runs every SessionStart hook many times at once. So a single
SessionStart hook that does heavy or unbounded work is a per-session multiplier:
under N concurrent sessions it becomes N concurrent heavy jobs.

On 2026-06-05 that exact shape hard-froze a machine (load 36, total freeze): the
corpus-walk secret scan ran on SessionStart, its cooldown was stamped *after* the
slow scan, and four concurrent sessions each launched their own full-corpus walk.
The fix was twofold and both halves live here: the scan moved **off** SessionStart
(scheduled job + cached-findings surfacer), and a stuck-hook reaper now ships **on**
SessionStart. This doc records the invariant so it can't silently regress.

## Provenance — what an install actually wires

```
bootstrap.sh
   └─ git clone (idempotent) ai-brain-starter@main → ~/.claude/skills/ai-brain-starter/
        └─ scripts/install-hooks-user-level.py
              └─ reads the canonical root hooks.json
                    └─ merges its SessionStart block into ~/.claude/settings.json
```

- The clone tracks **`main`** (it refuses to pull over a locally diverged clone,
  but it does not pin an old tag). A fresh install gets current `main`.
- `install-hooks-user-level.py` is idempotent and additive: it never removes a
  user's own hooks, only merges the ai-brain-starter set.
- **Native desktop / commercial products that wrap this substrate do not vendor
  or re-wire this fleet.** Where they spawn the Claude CLI for their own agent
  loop they run it with SessionStart/SessionEnd hooks disabled, and where they
  need vault-safety logic they re-implement it natively rather than shelling out
  to these Python hooks. So the only thing that wires this SessionStart fleet onto
  any machine — free or paid — is the path above.

Net: "is the paid install's hook fleet hardened?" reduces to "is `ai-brain-starter@main`
hardened?" — and the rest of this doc is the answer.

## The invariant

1. **No unbounded or corpus-scale work on SessionStart.** A SessionStart hook
   reads small, bounded state (a marker file, one directory level, a bounded
   subprocess with a timeout). Anything that walks a large corpus, syncs over the
   network, or otherwise scales with the user's data belongs on a **scheduled job**
   or a **cached-findings surfacer**, never the synchronous cold-start path.
2. **The protective reaper ships on SessionStart.** `remediate-runaway-procs.py`
   (CLASS-2 stuck-hook reaper) is the backstop that kills a wedged `~/.claude/hooks`
   process; it must be present in every install.

## Audit — the shipped SessionStart set

Every hook in the canonical `hooks.json` SessionStart block, and why each is
bounded (read against this repo at the time of writing — re-run the verification
below to confirm on any later revision):

| Hook | Work it does | Bound |
|---|---|---|
| `lint-claude-settings.py` (+ `--test`) | parse + lint one JSON file | single small file |
| `check-claude-code-version.sh` | read one cached version string | cached, no walk |
| `first-week-checkin.py` | read an install-date marker | marker read + budget guard |
| `migrate-to-user-level.py` | one-shot settings migration check | idempotent, no walk |
| `surface-orphan-claude-branches.py` | `git for-each-ref` + one `iterdir` | bounded git op + 1 dir level |
| `surface-stranded-session-artifacts.py` | `iterdir` of `.claude/worktrees/` | 1 dir level + subprocess `timeout=15` |
| `surface-orphan-worktree-snapshots.py` | `rglob` under the small snapshots dir | snapshot tree only, results capped |
| `worktree-footprint-signal.py` | `iterdir` of the worktree dir | 1 dir level |
| `surface-backup-status.py` | `iterdir` of repo root + `git` | 1 dir level + subprocess `timeout=30` |
| `enforce-worktree-cap.py` | count worktrees vs cap | bounded count + budget guard |
| `remediate-runaway-procs.py` | scan `~/.claude/hooks` procs, reap wedged ones | dual age/CPU gate, no FS walk |

None walks the user's vault corpus or `~/.claude/projects` transcripts.

**Deliberately NOT on SessionStart** (the two "heavy / concurrent" classes the
freeze taught us to keep off the cold-start path):

- `scan-prior-sessions-for-secrets.py` — corpus-walk secret scan. Runs as a
  scheduled job; SessionStart only reads its cached findings. (Hardened anyway —
  single-instance lock, stamp-at-start cooldown, incremental, `os.nice`, wall
  budget, per-file cap — so even a manual re-add can't pile up.)
- `health-auto-sync.py` — network wearable sync. Opt-in power-user file; the
  default chain syncs once per day on `/journal` Stop, not per session.

## Authoring a new SessionStart hook (the four guards, by construction)

Most SessionStart hooks need none of this: read a marker, do one `iterdir`
level, or run a bounded subprocess, and the hook is bounded by construction. The
rule below applies only when a hook **recursive-walks** — `os.walk`, `rglob`,
`glob('**')`, `find` without `-maxdepth 1`, `grep -r`.

A recursive / corpus-scale walk on SessionStart MUST carry all three guards (this
is the exact shape that froze a machine on 2026-06-05):

1. **single-instance lock** — `fcntl.flock(..., LOCK_EX | LOCK_NB)`; a concurrent
   session backs off instead of starting a second walk.
2. **cooldown stamped AT START** — claim the cooldown marker BEFORE the walk, so a
   session that starts mid-walk sees it and skips. Stamping it *after* the walk is
   the precise 2026-06-05 bug.
3. **wall-clock deadline** — break the loop on a `time.time()` budget (and
   `os.nice(10)` so it never starves the foreground session).

Skeleton — the reference implementation is `hooks/scan-prior-sessions-for-secrets.py`:

```python
import fcntl, os, time
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
MARKER = HOOK_DIR / ".last-myhook"          # cooldown, stamped at START
LOCK   = HOOK_DIR / ".myhook.lock"          # single-instance guard
COOLDOWN, BUDGET = 6 * 3600, 60

def main() -> int:
    last = float(MARKER.read_text()) if MARKER.exists() else 0
    if time.time() - last < COOLDOWN:
        return 0                              # fast cooldown path
    fh = LOCK.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0                              # another instance holds it; back off
    MARKER.write_text(f"{time.time():.0f}")   # stamp BEFORE the walk
    os.nice(10)
    deadline = time.time() + BUDGET
    for path in some_root.rglob("*"):
        if time.time() > deadline:
            break                             # wall-clock bound
        ...
    return 0
```

If the walked root is small **by construction** (one worktree-snapshot dir, a
fixed machinery folder — not a data corpus), the three guards are not needed;
declare it instead, co-located at the walk:

```python
# sessionstart-walk-bounded: <why this root is small / bounded — e.g. one
# worktree-snapshot dir, output capped; not a data corpus>
for entry in snap_dir.rglob("*"):
    ...
```

The reason is required (a bare token does not exempt). Check a hook before wiring
it, and audit the whole fleet:

```bash
python3 scripts/audit-sessionstart-boundedness.py --check hooks/<new-hook>.py
python3 scripts/audit-sessionstart-boundedness.py --all   # CI gate; lists every exemption
```

## How it's enforced (regression guards, all in `scripts/ci.sh`)

- `tests/integration/test_sessionstart_freeze_class_excluded.sh` — asserts the
  canonical `hooks.json` SessionStart set **excludes** the corpus-walk scan and
  **includes** the reaper. Ships with negative controls that re-add the scan /
  drop the reaper and confirm the guard trips.
- `tests/integration/test_sessionstart_boundedness.sh` — the **forward** guard
  (via `scripts/audit-sessionstart-boundedness.py`): asserts that every
  SessionStart-wired hook doing a recursive / corpus-scale walk carries all three
  bounded-hook guards (flock + stamp-at-START + wall deadline) or a co-located
  `# sessionstart-walk-bounded:` exemption. Mechanizes the **Bound** column above
  so it cannot silently rot when hook #N+1 is added. Ships pos/neg controls.
- `tests/integration/test_footprint_sla.sh` — the **fan-out** guard (via
  `scripts/footprint-sla-check.py --gate`): asserts the fleet's per-event /
  per-tool cold-start fan-out and the default-on daemon count stay within
  `footprint-budgets.json`. The boundedness guard above governs each hook's
  *work shape*; this one governs the *fleet's footprint* (how many cold starts a
  hot event pays). See [adr/0004-footprint-sla-gate.md](adr/0004-footprint-sla-gate.md).
  Ships pos/neg controls.
- `tests/integration/test_scan_prior_single_instance.sh` — a second concurrent
  scan run backs off (no pile-up), even if the scan is invoked directly.
- `tests/integration/test_remediate_runaway_procs.sh` — the reaper's pos/neg
  controls (reaps a wedged hook proc; never reaps self or a non-python proc).
- `services/health-mcp/tests/test_v05_hooks.py` — asserts `health-auto-sync.py`
  stays out of the default SessionStart block.

## Footprint SLA (fan-out + daemon budget)

The boundedness invariant above keeps any single hook from doing unbounded work.
A second, orthogonal failure is the fleet getting *wider*: each wired hook is a
cold `python3` start, so a hot event costs interpreter-startup × fan-out, and
that count silently re-grows as hooks are added (SLOW-INSTALL-FROM-LAZY-PLUMBING).

`scripts/footprint-sla-check.py` is the budget gate for that width. It keys only
on **deterministic** axes (no timing, no hook execution — hooks for one event run
concurrently, so felt latency is `MAX`, not `SUM`, and a wall-clock gate would
flake on CI and teach bypass):

- **per-event / per-tool substrate cold-start fan-out** vs `footprint-budgets.json`
  (ALL substrate-python entries count — `once: true` is IGNORED in settings.json,
  so a `once` hook re-fires every event, not once; `PreToolUse`/`PostToolUse` are
  per-tool; `[ -f ~/.claude/hooks/… ]`-guarded maintainer hooks no-op on a fresh
  install and are excluded);
- **default-on daemon count** — the default install (`bootstrap.sh`) wires 0;
- **dead `once: true` flags** — any `once` in this settings-merged `hooks.json` is a
  no-op that silently makes a "once-per-session" hook re-fire every event (the
  recurring-injection bug class, MYC-2359). A hard breach: move the hook to
  SessionStart (fires once per session-segment → lands in the cached prefix) or drop
  the flag. See [adr/0005-cache-position-injected-context.md](adr/0005-cache-position-injected-context.md).

Budgets are `measured + headroom`, so the gate ships green and bites on growth;
`_baseline_measured` records the snapshot. Tighten with `--update-budgets` as the
fan-out drops. Per-hook timing and per-message injected bytes are **advisory**
(`--measure --execute`) — measured and reported with the correct `MAX` semantics,
never gated. Full rationale: [adr/0004-footprint-sla-gate.md](adr/0004-footprint-sla-gate.md).

```bash
python3 scripts/footprint-sla-check.py --gate       # CI gate; exit 1 over budget, 2 on internal error
python3 scripts/footprint-sla-check.py --measure     # human report (add --execute for advisory timing)
python3 scripts/footprint-sla-check.py --selftest    # built-in pos/neg controls
```

## Scheduling priority (the launchd axis)

The budgets above bound how much work the fleet does. They do NOT bound the
priority it does it at, and that is a separate way to freeze a machine.

A LaunchAgent left at normal scheduling priority competes with WindowServer for
CPU and disk. One is harmless. A fleet is not: measured 2026-08-19 on a 10-core
host, 68 user agents (16 firing at least hourly, one every 60s) with none
declaring itself background drove load to 43.78, then 109, and hard-froze the UI
twice. RAM was fine throughout - 2GB free, swap flat - so the failure reads as a
crash and misdirects every diagnosis at memory. Load, not memory, was the story.

The default install wires **0 daemons**, so a fresh install does not start here.
This is a GROWTH failure: it arrives as the operator adds jobs over months.

Rules:

- Every LaunchAgent carries `ProcessType=Background` (nice 10 + throttled CPU)
  AND `LowPriorityIO=true` (disk yields to interactive). `LowPriorityIO` alone is
  NOT enough - it covers disk only, leaving the job competing for CPU.
- `scripts/com.granola-export.plist` is the shipped template and carries both.
  Any new template must too: the template is what teaches the pattern.
- Reload jobs with `RunAtLoad=true` ONE AT A TIME. Reloading a fleet together
  fires every one of them at once, which is the same storm you are fixing.
- A plist edit is deployed, not committed. Mirror it to wherever the plists are
  tracked or the next installer run silently reverts it to normal priority.

`hooks/surface-unniced-launchagents.py` detects the drift (pos/neg control:
`tests/integration/test_surface_unniced_launchagents.sh`). It is deliberately
NOT wired by default - SessionStart fan-out is at budget, and a growth failure
does not warrant a cold `python3` start for every user on every session. Wire it
by hand, or run it on demand, on a machine whose agent fleet is growing.

## Verify on a fresh install

The repo being hardened is not the same as a fresh install landing the hardened
wiring. To prove it on any revision, install into a throwaway `$HOME` and assert
the SessionStart wiring:

```bash
FRESH="$(mktemp -d)"; mkdir -p "$FRESH/.claude"
HOME="$FRESH" python3 scripts/install-hooks-user-level.py \
  --hooks-source "$PWD/hooks.json" --quiet

python3 - "$FRESH/.claude/settings.json" <<'PY'
import json, sys
ss = json.dumps(json.load(open(sys.argv[1])).get("hooks", {}).get("SessionStart", []))
assert "remediate-runaway-procs.py" in ss,            "reaper must be wired"
assert "scan-prior-sessions-for-secrets.py" not in ss, "corpus scan must stay off SessionStart"
assert "health-auto-sync.py" not in ss,                "network sync must stay off SessionStart"
print("fresh-install SessionStart wiring OK")
PY
```

Expected: `fresh-install SessionStart wiring OK`.
