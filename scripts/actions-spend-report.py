#!/usr/bin/env python3
"""actions-spend-report.py - measure where a repo's billed Actions minutes go.

WHY THIS EXISTS
---------------
`ci-cost-audit.py` is a STATIC auditor: it reads workflow YAML and flags shapes
known to waste money (no concurrency group, ungated macOS, a merge-queue
deadlocking cancel-in-progress). It cannot tell you what the bill actually IS,
so it cannot tell you whether a fix moved the burn rate - and a cost gate is
unproven until the burn rate moves.

This script is the dynamic half. It reads the Actions **jobs** API for the last
N runs and reports billed minutes per workflow, using GitHub's own billing rule:

    billed_minutes = ceil(job_seconds / 60) * runner_multiplier

That per-job ROUNDING is the point. A workflow that fans out 14 jobs which each
finish in 4 seconds bills 14 minutes, not 1. The gap between billed minutes and
real compute is reported here as `floor-waste`: money paid for rounding rather
than for work. A wide fan-out of very short jobs is what empties an Actions
budget - not slow tests.

USAGE
-----
    python3 scripts/actions-spend-report.py --repo owner/name
    python3 scripts/actions-spend-report.py --repo owner/name --runs 40
    python3 scripts/actions-spend-report.py --repo owner/name --workflow lint --per-job
    python3 scripts/actions-spend-report.py --repo owner/name --json

Requires the `gh` CLI, authenticated (`gh auth status`). No token is read,
stored, or printed by this script - every API call goes through `gh api`.

Re-run it with the SAME --runs before and after a CI change and post both
tables: that before/after IS the proof the change worked.
"""
import argparse
import json
import math
import subprocess
import sys
from collections import defaultdict

# Keep this CLI printable on a Windows cp1252 console (scripts/check-utf8-stdout.py).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # Python 3.7+
    except (AttributeError, ValueError):
        pass

# GitHub's published per-minute multipliers, keyed by the runner label a job
# reports. A job billed at 2x that finishes in 61 seconds costs FOUR minutes.
# https://docs.github.com/billing/managing-billing-for-github-actions
MULTIPLIERS = (
    ("macos", 10),
    ("windows", 2),
    ("ubuntu", 1),
    ("linux", 1),
    ("self-hosted", 0),  # self-hosted minutes are not billed by GitHub
)
DEFAULT_MULTIPLIER = 1


def runner_multiplier(labels):
    """Map a job's runner labels to GitHub's billing multiplier."""
    joined = " ".join(labels or []).lower()
    for token, mult in MULTIPLIERS:
        if token in joined:
            return mult
    return DEFAULT_MULTIPLIER


def gh_api(path, paginate=False):
    """Call `gh api <path>` and return parsed JSON. Fails loud, never silently."""
    cmd = ["gh", "api", path]
    if paginate:
        cmd.append("--paginate")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        sys.exit("error: the `gh` CLI is not installed. See https://cli.github.com/")
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        sys.exit("error: gh api %s failed:\n  %s" % (path, stderr))
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        sys.exit("error: gh api %s returned unparseable JSON: %s" % (path, exc))


def job_seconds(job):
    """Wall-clock seconds GitHub bills this job for, from its own timestamps."""
    started, completed = job.get("started_at"), job.get("completed_at")
    if not started or not completed:
        return 0.0
    # Timestamps are RFC3339 Zulu. Parse without a dependency and without
    # datetime.fromisoformat's pre-3.11 refusal of a trailing 'Z'.
    import datetime

    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        t0 = datetime.datetime.strptime(started, fmt)
        t1 = datetime.datetime.strptime(completed, fmt)
    except ValueError:
        return 0.0
    return max(0.0, (t1 - t0).total_seconds())


def collect(repo, run_limit, workflow_filter):
    """Return (per-workflow totals, per-job totals, runs actually counted)."""
    runs_doc = gh_api(
        "repos/%s/actions/runs?per_page=%d" % (repo, min(run_limit, 100))
    )
    runs = runs_doc.get("workflow_runs", [])[:run_limit]

    by_workflow = defaultdict(
        lambda: {"billed": 0, "real": 0.0, "jobs": 0, "runs": set()}
    )
    by_job = defaultdict(lambda: {"billed": 0, "real": 0.0, "jobs": 0})
    counted = 0

    for run in runs:
        wf = run.get("name") or "(unnamed)"
        if workflow_filter and workflow_filter.lower() not in wf.lower():
            continue
        jobs_doc = gh_api(
            "repos/%s/actions/runs/%s/jobs?per_page=100" % (repo, run["id"])
        )
        jobs = jobs_doc.get("jobs", [])
        if not jobs:
            continue
        counted += 1
        for job in jobs:
            # A skipped job never occupies a runner and is never billed.
            if job.get("conclusion") == "skipped":
                continue
            secs = job_seconds(job)
            mult = runner_multiplier(job.get("labels"))
            billed = math.ceil(secs / 60.0) * mult if secs > 0 else 0
            real = (secs / 60.0) * mult

            w = by_workflow[wf]
            w["billed"] += billed
            w["real"] += real
            w["jobs"] += 1
            w["runs"].add(run["id"])

            key = (wf, job.get("name") or "(unnamed)")
            j = by_job[key]
            j["billed"] += billed
            j["real"] += real
            j["jobs"] += 1

    return by_workflow, by_job, counted


def render(by_workflow, by_job, counted, repo, show_jobs):
    total_billed = sum(w["billed"] for w in by_workflow.values())
    total_real = sum(w["real"] for w in by_workflow.values())
    waste = total_billed - total_real

    print("Actions spend - %s" % repo)
    print("method: jobs API, billed = ceil(job_seconds/60) x runner multiplier")
    print("sample: %d runs\n" % counted)

    head = "%-32s %10s %6s %6s %12s" % (
        "workflow",
        "billed-min",
        "jobs",
        "share",
        "floor-waste",
    )
    print(head)
    print("-" * len(head))
    ordered = sorted(by_workflow.items(), key=lambda kv: -kv[1]["billed"])
    for name, w in ordered:
        share = (w["billed"] / total_billed * 100) if total_billed else 0
        print(
            "%-32s %10d %6d %5.0f%% %11dm"
            % (name[:32], w["billed"], w["jobs"], share, round(w["billed"] - w["real"]))
        )
    print("-" * len(head))
    pct = (waste / total_billed * 100) if total_billed else 0
    print(
        "%-32s %10d %6s %6s %11dm  (%.0f%% of billed is rounding, not compute)"
        % ("TOTAL", total_billed, "", "", round(waste), pct)
    )

    if show_jobs:
        print("\nper-job breakdown")
        jhead = "%-24s %-40s %10s %6s %12s" % (
            "workflow",
            "job",
            "billed-min",
            "runs",
            "floor-waste",
        )
        print(jhead)
        print("-" * len(jhead))
        for (wf, jn), j in sorted(by_job.items(), key=lambda kv: -kv[1]["billed"]):
            print(
                "%-24s %-40s %10d %6d %11dm"
                % (wf[:24], jn[:40], j["billed"], j["jobs"], round(j["billed"] - j["real"]))
            )

    return total_billed, waste, pct


def main():
    ap = argparse.ArgumentParser(
        description="Measure billed GitHub Actions minutes per workflow."
    )
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--runs", type=int, default=40, help="how many recent runs (default 40)")
    ap.add_argument("--workflow", default="", help="only this workflow (substring match)")
    ap.add_argument("--per-job", action="store_true", help="also break down by job name")
    ap.add_argument("--json", action="store_true", dest="as_json", help="machine-readable output")
    ap.add_argument(
        "--fail-over-waste",
        type=float,
        default=None,
        metavar="PCT",
        help="exit 1 if floor waste exceeds this percent of billed minutes",
    )
    args = ap.parse_args()

    by_workflow, by_job, counted = collect(args.repo, args.runs, args.workflow)
    if not counted:
        sys.exit("error: no runs with jobs found for %s (check --repo / --workflow)" % args.repo)

    if args.as_json:
        payload = {
            "repo": args.repo,
            "runs_sampled": counted,
            "workflows": {
                name: {
                    "billed_minutes": w["billed"],
                    "real_minutes": round(w["real"], 2),
                    "floor_waste_minutes": round(w["billed"] - w["real"], 2),
                    "jobs": w["jobs"],
                    "runs": len(w["runs"]),
                }
                for name, w in by_workflow.items()
            },
        }
        total_billed = sum(w["billed"] for w in by_workflow.values())
        total_real = sum(w["real"] for w in by_workflow.values())
        payload["total_billed_minutes"] = total_billed
        payload["total_floor_waste_minutes"] = round(total_billed - total_real, 2)
        payload["floor_waste_pct"] = (
            round((total_billed - total_real) / total_billed * 100, 1) if total_billed else 0
        )
        print(json.dumps(payload, indent=2))
        pct = payload["floor_waste_pct"]
    else:
        _, _, pct = render(by_workflow, by_job, counted, args.repo, args.per_job)

    if args.fail_over_waste is not None and pct > args.fail_over_waste:
        sys.exit(
            "\nFAIL: floor waste %.0f%% exceeds the --fail-over-waste ceiling of %.0f%%"
            % (pct, args.fail_over_waste)
        )


if __name__ == "__main__":
    main()
