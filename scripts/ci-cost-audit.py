#!/usr/bin/env python3
"""ci-cost-audit.py — find the CI spend leaks in a repo BEFORE they become an invoice.

An AI brain opens many branches at once. That is the point of it — and it is
also why CI cost stops behaving the way people expect. GitHub Actions does not
bill per workflow or per wall-clock minute. It bills:

    per JOB, rounded UP to a whole minute, times the runner multiplier
    (Linux 1x, Windows 2x, macOS 10x)

So the thing that empties an Actions budget is rarely a slow test. It is a wide
fan-out of very short jobs, multiplied by the number of branches in flight. A
workflow with 13 six-second jobs bills 13 minutes, not one. Ten such workflows
across twenty open PRs bills 2,600 minutes for about twenty minutes of work.

This script reports that shape for any repo:

  STATIC  (no network, always runs) — reads .github/workflows/*.yml and flags:
      * jobs that will pay the 1-minute floor for near-zero work
      * expensive runners (macOS/Windows) on per-push triggers
      * missing `concurrency` groups (superseded pushes keep burning)
      * `cancel-in-progress` on merge_group (deadlocks a merge queue)
      * relevance predicates broad enough to match ordinary churn
      * duplicate push+pull_request triggers on the same branch

  MEASURED (--repo owner/name, needs `gh`) — pulls real job records and reports
      actual billed minutes per workflow, the share that is pure rounding, and
      the runner-multiplier concentration. Skipped jobs are EXCLUDED: they never
      allocate a runner and are not billed, and counting them sends you after
      the wrong workflow.

Usage:
    python3 scripts/ci-cost-audit.py                      # static, current repo
    python3 scripts/ci-cost-audit.py --path ../other-repo  # static, elsewhere
    python3 scripts/ci-cost-audit.py --repo owner/name --hours 24
    python3 scripts/ci-cost-audit.py --self-test          # negative controls

Exit: 0 clean or advisory-only, 1 findings at or above --fail-on, 2 bad input.

Operating rule this enforces: "A Cost Gate Is Unproven Until the Burn Rate
Moves" — CI is billed per job, at a one-minute floor, times the runner
multiplier.
"""
# exit-contract: ENFORCING

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
from _lib.safe_read import safe_read_text  # noqa: E402

# Per-minute USD, GitHub-hosted standard runners, private repos.
RATE = {"linux": 0.008, "windows": 0.016, "macos": 0.08}
MULTIPLIER = {"linux": 1, "windows": 2, "macos": 10}

SEVERITY_ORDER = {"advisory": 0, "warn": 1, "high": 2, "unevaluated": 3}


def runner_os(label: str) -> str:
    low = label.lower()
    if "macos" in low or "mac-" in low:
        return "macos"
    if "windows" in low or "win-" in low:
        return "windows"
    return "linux"


class Finding:
    def __init__(self, severity: str, workflow: str, title: str, detail: str, fix: str):
        self.severity = severity
        self.workflow = workflow
        self.title = title
        self.detail = detail
        self.fix = fix

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.severity} {self.workflow}: {self.title}>"


# --------------------------------------------------------------------------
# STATIC ANALYSIS
# --------------------------------------------------------------------------

def _read(path: Path) -> str | None:
    """Bounded, cloud-safe read (a workflow file may sit on a synced mount)."""
    res = safe_read_text(path, timeout=5.0, max_bytes=2_000_000)
    return res.text if res.ok else None


def _load_yaml(text: str):
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        return yaml.safe_load(text)
    except Exception:
        return None


def _triggers(doc) -> dict:
    # PyYAML parses the bare key `on:` as the boolean True.
    if not isinstance(doc, dict):
        return {}
    raw = doc.get("on", doc.get(True, {}))
    if isinstance(raw, str):
        return {raw: {}}
    if isinstance(raw, list):
        return {k: {} for k in raw}
    return raw if isinstance(raw, dict) else {}


# A directory prefix named by a relevance predicate is "fat" — and so likely to
# hold the gate permanently open — at or above this many files beneath it.
BROAD_PREFIX_FILES = 50

# Directories that inflate a file count without being source churn.
_SKIP_DIRS = {".git", "node_modules", "target", "dist", "build", "out",
              ".next", "__pycache__", ".venv", "venv", "vendor"}


def _count_files(root: Path, cap: int = 5000) -> int:
    """Count files under `root`, skipping build/vendor noise. Bounded by `cap`.

    Metadata-only: never reads file CONTENT, so this stays outside the
    cloud-safe-walker ratchet's scope by construction.
    """
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        n += len(filenames)
        if n >= cap:
            return cap
    return n


def analyze_workflow(path: Path, text: str, repo_root: Path | None = None) -> list[Finding]:
    repo_root = repo_root if repo_root is not None else Path(".")
    findings: list[Finding] = []
    name = path.name
    doc = _load_yaml(text)
    if doc is None:
        # "unparsed" is a FAILURE TO MEASURE, not a low-severity finding, and
        # filing it as advisory made this auditor report success having checked
        # nothing. Measured on origin/main 8b9038b: PyYAML is absent on the CI
        # runner, so all 11 workflows landed here, every static rule was
        # skipped, and `--fail-on high` -- the step literally named "runs clean
        # against this repo's own workflows" -- passed green while a HIGH
        # finding (a 10x macOS runner ungated on every PR push) sat unreported.
        # The same command on the same commit exits 1 locally, where PyYAML is
        # installed. Severity "unevaluated" is ordered above "high" so no
        # --fail-on threshold can filter it away.
        return [Finding("unevaluated", name, "unparsed",
                        "PyYAML unavailable or file did not parse; static checks "
                        "were SKIPPED for it. A skipped check is not a passed check.",
                        "pip install pyyaml, or fix the YAML.")]

    trig = _triggers(doc)
    jobs = doc.get("jobs") or {}
    if not isinstance(jobs, dict):
        jobs = {}

    is_pr = "pull_request" in trig or "pull_request_target" in trig
    is_push = "push" in trig
    has_mq = "merge_group" in trig

    # 1. concurrency group missing on a PR-triggered workflow
    conc = doc.get("concurrency")
    if is_pr and not conc:
        findings.append(Finding(
            "high", name, "no concurrency group",
            "PR-triggered with no `concurrency:` — every superseded push keeps "
            "running to completion and is billed in full.",
            "Add `concurrency: {group: <wf>-${{ github.event.pull_request.number "
            "|| github.ref }}, cancel-in-progress: ${{ github.event_name == "
            "'pull_request' }}}`."))

    # 2. cancel-in-progress unconditionally true while merge_group is a trigger
    if has_mq and isinstance(conc, dict):
        cip = conc.get("cancel-in-progress")
        if cip is True:
            findings.append(Finding(
                "high", name, "cancel-in-progress deadlocks the merge queue",
                "`cancel-in-progress: true` with a `merge_group` trigger: cancelling "
                "a queued group means its required checks never report, and the "
                "queue can never merge.",
                "Make it conditional: `cancel-in-progress: ${{ github.event_name "
                "== 'pull_request' }}`."))

    # 3. push + pull_request on the same branches = double billing per change
    if is_push and is_pr:
        push_branches = (trig.get("push") or {}).get("branches") if isinstance(trig.get("push"), dict) else None
        findings.append(Finding(
            "warn", name, "push and pull_request both trigger",
            f"Both `push`{f' (branches: {push_branches})' if push_branches else ''} and "
            "`pull_request` fire. If the merge queue already validates the predicted "
            "merge, the post-merge push run re-proves an identical tree."
            + (" `merge_group` is configured here, so the push run is likely redundant."
               if has_mq else ""),
            "Drop `push:` when a merge queue (or required PR) already covers the branch."))

    # 4. expensive runners on per-push triggers
    for key, job in jobs.items():
        if not isinstance(job, dict):
            continue
        runs_on = job.get("runs-on")
        labels: list[str] = []
        if isinstance(runs_on, str):
            labels = [runs_on]
        elif isinstance(runs_on, list):
            labels = [x for x in runs_on if isinstance(x, str)]
        elif isinstance(runs_on, dict):
            labels = [str(v) for v in runs_on.values()]
        text_labels = " ".join(labels) + " " + json.dumps(job.get("strategy", ""), default=str)
        osname = runner_os(text_labels)
        if osname != "linux" and is_pr:
            gated = bool(job.get("needs")) and bool(job.get("if"))
            # Severity tracks the actual multiplier, not merely "not Linux".
            # macOS at 10x is a different class of exposure from Windows at 2x:
            # in the incident that produced this script, macOS was 3.5% of jobs
            # and 41% of the bill. Flagging both identically would over-weight
            # Windows and train maintainers to ignore the high band — the one
            # failure mode a cost linter cannot afford.
            if gated:
                sev = "advisory"
            elif MULTIPLIER[osname] >= 10:
                sev = "high"
            else:
                sev = "warn"
            # The remedy text below has always offered "leave it ungated
            # deliberately and say so in a comment" -- but nothing could read a
            # comment, so the only way to satisfy this rule was to change the
            # CI topology. A job may now declare the exemption in YAML, where a
            # diff can see it and a reviewer can argue with it. The reason is
            # required and must be a real sentence: an exemption nobody has to
            # justify is an allowlist.
            deliberate = ""
            job_env = job.get("env")
            if isinstance(job_env, dict):
                deliberate = str(job_env.get("CI_COST_DELIBERATE") or "").strip()
            if len(deliberate) >= 40:
                sev = "advisory"
            findings.append(Finding(
                sev, name,
                f"{osname} runner on a PR trigger (job `{key}`)"
                + (" [declared deliberate]" if len(deliberate) >= 40 else ""),
                f"{osname} bills {MULTIPLIER[osname]}x Linux. This job boots on every PR "
                f"push{'' if gated else ' with no needs+if relevance gate'}."
                + (f" Declared deliberate: {deliberate}" if len(deliberate) >= 40 else ""),
                "Gate it behind a cheap Linux relevance job, and prefer running the "
                "expensive matrix on `merge_group` (the predicted merge) rather than "
                "every intermediate push. If the job proves a cross-platform "
                "guarantee that must hold on every change, leave it ungated "
                "deliberately and say so in a comment."))

    # 5. relevance predicates broad enough to match ordinary churn.
    #
    # A bare directory prefix is NOT suspicious on its own — `apps/apple/` may be
    # exactly the dependency set of an Apple-only job. It becomes suspicious when
    # the prefix covers a LOT of files, because then routine commits under that
    # tree keep the gate permanently open. That is the MYC-3135 shape: the
    # predicate named a 126-file crate to protect behaviour living in 15 files.
    # Counting real files is what separates the two; guessing from the string
    # alone produces false positives, and a linter that cries wolf gets ignored.
    for m in re.finditer(r"""(?:pattern|surface)\s*=\s*['"]([^'"]+)['"]""", text):
        pat = m.group(1) or ""
        prefixes = re.findall(r"([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/)(?:\||\)|$)", pat)
        fat: list[tuple[str, int]] = []
        for pref in dict.fromkeys(prefixes):
            d = repo_root / pref
            if not d.is_dir():
                continue
            n = _count_files(d)
            if n >= BROAD_PREFIX_FILES:
                fat.append((pref, n))
        if fat:
            worst = ", ".join(f"{p} ({n} files)" for p, n in sorted(fat, key=lambda x: -x[1])[:3])
            findings.append(Finding(
                "warn", name, "relevance predicate matches a large tree",
                f"Predicate names directory prefixes covering many files: {worst}. "
                "Routine commits under those trees keep the gate open, so it trims "
                "little or nothing — verify its real hit rate before trusting it.",
                "Predicate by CONTENT (does the changed file actually carry the "
                "conditional / import the driver?) and re-measure the burn RATE "
                "after shipping — a gate that merged is not a gate that worked."))
            break

    # 6. per-job floor: many jobs with no `needs` all boot in parallel
    if len(jobs) >= 6 and is_pr:
        ungated = [k for k, j in jobs.items()
                   if isinstance(j, dict) and not j.get("needs")]
        if len(ungated) >= 6:
            findings.append(Finding(
                "warn", name, f"{len(ungated)} ungated parallel jobs pay the floor",
                f"{len(ungated)} jobs start with no `needs:` — each is billed a minimum "
                "of one full minute even if it runs for two seconds.",
                "Merge jobs that share a runner image and setup into one job with "
                "sequential steps. Never merge a REQUIRED check (matching is by job "
                "name; renaming deadlocks open PRs, and folding it in as a step makes "
                "the gate silently deletable)."))

    return findings


def static_audit(root: Path) -> tuple[list[Finding], int]:
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return ([Finding("advisory", "-", "no workflows",
                         f"No .github/workflows under {root}.", "Nothing to audit.")], 0)
    findings: list[Finding] = []
    count = 0
    # Non-recursive: workflows are a flat directory by GitHub's own definition.
    for path in sorted(wf_dir.glob("*.yml")) + sorted(wf_dir.glob("*.yaml")):
        text = _read(path)
        if text is None:
            findings.append(Finding("advisory", path.name, "unreadable",
                                    "safe_read could not read this file.",
                                    "Check the mount / permissions."))
            continue
        count += 1
        findings.extend(analyze_workflow(path, text, root))
    return findings, count


# --------------------------------------------------------------------------
# MEASURED ANALYSIS
# --------------------------------------------------------------------------

def _gh_run(args: list[str]) -> str | None:
    """Run `gh` and return stdout, or None if the CALL ITSELF failed.

    None means "this did not happen" (gh missing, non-zero exit, timeout). It
    never means "there were no records". Collapsing those two into one value is
    what let this gate look healthy while dead - see _decode_stream.
    """
    try:
        # encoding pinned: text=True alone decodes with the locale encoding,
        # which is cp1252 on a stock Windows box -- gh emits UTF-8 job names.
        out = subprocess.run(["gh", *args], capture_output=True, text=True,
                             encoding="utf-8", timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _decode_stream(raw: str | None) -> list | None:
    """Decode gh output that may be SEVERAL concatenated JSON values.

    `gh api --paginate` against an endpoint returning an OBJECT - which both
    /actions/runs and /actions/runs/<id>/jobs do - emits one complete object
    PER PAGE, back to back. That is not valid JSON. The previous code called
    json.loads() on it, caught the inevitable JSONDecodeError, and returned
    None, which the caller then reported as "no runs in window, or gh
    unavailable / unauthenticated".

    So the MEASURED half of this auditor returned an empty result on every repo
    busy enough to have more than one page of runs - i.e. exactly the repos
    whose spend it exists to measure - while blaming the operator's gh auth.
    A cost gate is unproven until the burn rate moves, and this one could not
    read the burn rate at all. Decoding page by page is the fix.
    """
    if raw is None:
        return None
    dec = json.JSONDecoder()
    idx, n, pages = 0, len(raw), []
    while idx < n:
        while idx < n and raw[idx].isspace():
            idx += 1
        if idx >= n:
            break
        try:
            obj, idx = dec.raw_decode(raw, idx)
        except ValueError:
            return None
        pages.append(obj)
    return pages


def _gh_pages(args: list[str], key: str) -> list | None:
    """Every record under `key`, merged across pages. None = the gh call failed."""
    pages = _decode_stream(_gh_run(args))
    if pages is None:
        return None
    merged: list = []
    for page in pages:
        if isinstance(page, dict):
            merged.extend(page.get(key, []) or [])
        elif isinstance(page, list):
            merged.extend(page)
    return merged


def measured_audit(repo: str, hours: int) -> list[str]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    lines: list[str] = []
    runs = _gh_pages(["api", "--paginate",
                      f"/repos/{repo}/actions/runs"
                      f"?created=%3E%3D{since.date().isoformat()}&per_page=100"],
                     "workflow_runs")
    # Distinguish the three causes the old single message conflated. "The call
    # failed" and "the repo spent nothing" must never print the same line.
    if runs is None:
        return ["  (could NOT measure: the `gh` call failed - missing binary,"
                " not authenticated, or an API error.",
                "   Check `gh auth status`. This is NOT a zero-spend result.)"]
    ids: list[int] = []
    cutoff = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in runs:
        started = r.get("run_started_at") or ""
        if started and started >= cutoff:
            ids.append(r["id"])
    if not ids:
        return [f"  (gh reached the API and returned {len(runs)} run(s), none started"
                f" inside the last {hours}h window - widen it with --hours.)"]

    agg: dict[str, dict] = defaultdict(lambda: {"jobs": 0, "wmin": 0, "secs": 0.0, "floor": 0})
    totals = {"wmin": 0, "floorwmin": 0, "cost": 0.0, "jobs": 0}
    by_os: dict[str, dict] = defaultdict(lambda: {"jobs": 0, "min": 0})

    for rid in ids[:400]:  # bounded: 400 runs is plenty to characterise a repo
        # Same paginated-object shape as the runs endpoint above: a run with
        # more than 100 jobs pages, and a plain json.loads() would drop it.
        jobs = _gh_pages(["api", "--paginate",
                          f"/repos/{repo}/actions/runs/{rid}/jobs?per_page=100&filter=all"],
                         "jobs")
        if jobs is None:
            continue
        for j in jobs:
            # A skipped job never allocates a runner and is NOT billed.
            if j.get("conclusion") == "skipped":
                continue
            s, e = j.get("started_at"), j.get("completed_at")
            if not s or not e:
                continue
            try:
                secs = (datetime.fromisoformat(e.replace("Z", "+00:00"))
                        - datetime.fromisoformat(s.replace("Z", "+00:00"))).total_seconds()
            except ValueError:
                continue
            if secs <= 0:
                continue
            osname = runner_os(" ".join(j.get("labels") or []))
            mult = MULTIPLIER[osname]
            billed = max(1, math.ceil(secs / 60.0))
            wf = j.get("workflow_name") or "?"
            a = agg[wf]
            a["jobs"] += 1
            a["wmin"] += billed * mult
            a["secs"] += secs
            a["floor"] += billed * mult - (secs / 60.0) * mult
            totals["wmin"] += billed * mult
            totals["floorwmin"] += billed * mult - (secs / 60.0) * mult
            totals["cost"] += billed * RATE[osname]
            totals["jobs"] += 1
            by_os[osname]["jobs"] += 1
            by_os[osname]["min"] += billed

    if not totals["jobs"]:
        return ["  (no billable jobs found in window)"]

    lines.append(f"  window: last {hours}h   billable jobs: {totals['jobs']}")
    lines.append(f"  weighted minutes charged: {totals['wmin']:,}"
                 f"   estimated cost: ${totals['cost']:,.2f}")
    pct = totals["floorwmin"] / totals["wmin"] * 100 if totals["wmin"] else 0
    lines.append(f"  PURE PER-JOB ROUNDING: {totals['floorwmin']:,.0f} weighted min ({pct:.0f}% of the bill)")
    lines.append("")
    lines.append("  runner concentration:")
    for osn in ("macos", "windows", "linux"):
        if by_os[osn]["jobs"]:
            share = by_os[osn]["min"] * MULTIPLIER[osn] / totals["wmin"] * 100
            lines.append(f"    {osn:<8} {by_os[osn]['jobs']:>5} jobs "
                         f"({by_os[osn]['jobs']/totals['jobs']*100:>4.1f}% of jobs)  "
                         f"{share:>4.0f}% of the bill")
    lines.append("")
    lines.append("  top workflows by weighted minutes:")
    for wf, a in sorted(agg.items(), key=lambda kv: -kv[1]["wmin"])[:12]:
        med = a["secs"] / a["jobs"]
        lines.append(f"    {wf[:44]:<44} {a['jobs']:>4} jobs {a['wmin']:>6,} wmin "
                     f"(avg {med:>5.0f}s, {a['floor']/max(a['wmin'],1)*100:>3.0f}% floor)")
    return lines


# --------------------------------------------------------------------------
# SELF-TEST (negative controls: the auditor must FAIL on a bad workflow)
# --------------------------------------------------------------------------

BAD_NO_CONCURRENCY = """
name: bad
on:
  pull_request:
    branches: [main]
jobs:
  a:
    runs-on: ubuntu-latest
    steps: [{run: "echo hi"}]
"""

BAD_MQ_CANCEL = """
name: bad-mq
on:
  pull_request:
  merge_group:
concurrency:
  group: x
  cancel-in-progress: true
jobs:
  a:
    runs-on: ubuntu-latest
    steps: [{run: "echo hi"}]
"""

BAD_MACOS = """
name: bad-mac
on:
  pull_request:
concurrency:
  group: x
  cancel-in-progress: true
jobs:
  m:
    runs-on: macos-15
    steps: [{run: "echo hi"}]
"""

GOOD = """
name: good
on:
  pull_request:
  merge_group:
concurrency:
  group: good-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
jobs:
  gate:
    runs-on: ubuntu-latest
    steps: [{run: "echo gate"}]
  mac:
    needs: [gate]
    if: ${{ needs.gate.outputs.relevant != 'false' }}
    runs-on: macos-15
    steps: [{run: "echo mac"}]
"""


# An ungated macOS job that DECLARES why, and one that offers a token reason.
# The pair is the point: if only the first existed, a marker that downgraded on
# any non-empty string would pass it and the reason requirement would be
# decorative.
BAD_MACOS_DECLARED = """
on: [pull_request]
jobs:
  mac:
    runs-on: macos-15
    env:
      CI_COST_DELIBERATE: >-
        Only surface proving bash 3.2 parsing; the guarantee must hold on every
        change and a real bug shipped through this gap.
    steps: [{run: "echo mac"}]
"""

BAD_MACOS_TOKEN_REASON = """
on: [pull_request]
jobs:
  mac:
    runs-on: macos-15
    env:
      CI_COST_DELIBERATE: "needed"
    steps: [{run: "echo mac"}]
"""


def self_test() -> int:
    rc = 0

    def titles(text: str) -> set[str]:
        return {f.title for f in analyze_workflow(Path("t.yml"), text)}

    if _load_yaml(GOOD) is None:
        # Returning 0 here made the negative-control step ("the auditor must
        # still bite") report the same green whether the controls bit or never
        # ran at all. A control that cannot run has proven nothing.
        print("  UNEVALUATED: PyYAML unavailable; the static negative controls "
              "could NOT run. This is not a pass.")
        return 2

    def sev_of(text: str, needle: str) -> str:
        for f in analyze_workflow(Path("t.yml"), text):
            if needle in f.title:
                return f.severity
        return "<absent>"

    # --- the new acceptance marker, both directions --------------------------
    got = sev_of(BAD_MACOS_DECLARED, "macos runner")
    if got != "advisory":
        print(f"  FAIL: a DECLARED deliberate macOS job should downgrade to "
              f"advisory, got {got}")
        rc = 1
    got = sev_of(BAD_MACOS_TOKEN_REASON, "macos runner")
    if got != "high":
        print(f"  FAIL: a token one-word reason must NOT buy the exemption; "
              f"expected high, got {got}. An exemption nobody has to justify "
              f"is an allowlist.")
        rc = 1

    # --- an unparsed workflow is UNEVALUATED, never advisory -----------------
    broken = analyze_workflow(Path("t.yml"), "this: [is: not: valid: yaml")
    if not broken or broken[0].severity != "unevaluated":
        print(f"  FAIL: an unparseable workflow must be severity 'unevaluated', "
              f"got {[f.severity for f in broken]}. Filing a failure-to-measure "
              f"as advisory is what let --fail-on high pass on a repo where "
              f"nothing parsed.")
        rc = 1

    checks = [
        ("missing concurrency is caught", BAD_NO_CONCURRENCY,
         lambda t: any("concurrency" in x for x in t), True),
        ("merge-queue deadlock is caught", BAD_MQ_CANCEL,
         lambda t: any("merge queue" in x for x in t), True),
        ("unguarded macOS on PR is caught", BAD_MACOS,
         lambda t: any("macos runner" in x for x in t), True),
        ("a well-formed workflow is NOT flagged high", GOOD,
         lambda t: not any("concurrency" in x or "merge queue" in x for x in t), True),
    ]
    for label, text, pred, want in checks:
        got = pred(titles(text))
        ok = got == want
        print(f"  [{'ok  ' if ok else 'FAIL'}] {label}")
        if not ok:
            rc = 1

    # The GOOD workflow's macOS job is gated (needs + if) -> advisory, not high.
    gf = [f for f in analyze_workflow(Path("t.yml"), GOOD) if "macos" in f.title]
    ok = bool(gf) and all(f.severity == "advisory" for f in gf)
    print(f"  [{'ok  ' if ok else 'FAIL'}] a GATED macOS job is advisory, not high")
    if not ok:
        rc = 1

    # ---- MEASURED-half controls ------------------------------------------
    # The static checks above all passed for months while the MEASURED half was
    # dead: `gh api --paginate` returns one JSON object PER PAGE, json.loads()
    # rejected the concatenation, and the error surfaced as "no runs in window".
    # A measurement that silently reports nothing is indistinguishable from a
    # repo that spent nothing, so these controls pin the decoder directly.
    one_page = '{"total_count": 2, "workflow_runs": [{"id": 1}, {"id": 2}]}'
    two_pages = one_page + "\n" + '{"total_count": 2, "workflow_runs": [{"id": 3}]}'

    def merged(raw):
        pages = _decode_stream(raw)
        if pages is None:
            return None
        out = []
        for p in pages:
            out.extend(p.get("workflow_runs", []) or [])
        return out

    measured_checks = [
        ("single-page gh output decodes", merged(one_page), [{"id": 1}, {"id": 2}]),
        ("MULTI-page --paginate output decodes (the bug this gate missed)",
         merged(two_pages), [{"id": 1}, {"id": 2}, {"id": 3}]),
        ("a failed gh call stays None, never an empty result", merged(None), None),
        ("unparseable output stays None, never an empty result",
         merged("not json at all"), None),
    ]
    for label, got, want in measured_checks:
        ok = got == want
        print(f"  [{'ok  ' if ok else 'FAIL'}] {label}")
        if not ok:
            rc = 1

    if rc == 0:
        print("  ci-cost-audit self-test: all controls pass "
              "(auditor bites on bad workflows, stays quiet on good ones).")
    return rc


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=".", help="repo root to audit statically")
    ap.add_argument("--repo", help="owner/name — also pull real billed-job data via gh")
    ap.add_argument("--hours", type=int, default=24, help="measurement window (default 24)")
    # Default is "high", not "never". CI already passes --fail-on high
    # explicitly (lint.yml), so this changes only the hand-run path -- which
    # was the one printing HIGH findings in full and exiting 0. "never" stays
    # available for a survey run that deliberately wants no verdict.
    ap.add_argument("--fail-on", choices=["advisory", "warn", "high", "never"],
                    default="high", help="exit 1 at or above this severity")
    ap.add_argument("--self-test", action="store_true", help="run negative controls")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    findings, n = static_audit(root)
    print(f"CI COST AUDIT — {root}")
    print(f"  workflows scanned: {n}")
    print()
    if not findings:
        print("  no static findings.")
    else:
        for sev in ("unevaluated", "high", "warn", "advisory"):
            group = [f for f in findings if f.severity == sev]
            if not group:
                continue
            print(f"  === {sev.upper()} ({len(group)}) ===")
            for f in group:
                print(f"    [{f.workflow}] {f.title}")
                print(f"        {f.detail}")
                print(f"        fix: {f.fix}")
            print()

    if args.repo:
        print("MEASURED (real billed jobs)")
        for line in measured_audit(args.repo, args.hours):
            print(line)
        print()

    print("Reminder: a cost gate is unproven until the burn rate moves. "
          "Re-run with --repo after shipping a gate and compare weighted min/hour.")

    unevaluated = [f for f in findings if f.severity == "unevaluated"]
    if unevaluated:
        print(f"\nUNEVALUATED: {len(unevaluated)} workflow(s) could not be parsed, "
              f"so their static checks never ran. This is not a clean audit -- it "
              f"is an absent one. Install PyYAML (pip install pyyaml) or fix the "
              f"YAML, then re-run.")
        return 2

    if args.fail_on != "never":
        threshold = SEVERITY_ORDER[args.fail_on]
        if any(SEVERITY_ORDER[f.severity] >= threshold for f in findings):
            return 1
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print
    # can't crash. This CLI prints findings containing em dashes.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
