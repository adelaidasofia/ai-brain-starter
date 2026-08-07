#!/usr/bin/env python3
"""Negative-control test for scripts/relocate-sweep.py — the code-repo-aware
residual-reference sweep that decides whether it is safe to drop the symlink a
relocation left behind.

A guard earns trust only by failing on the thing it catches
(docs/CLOUD_SYNC.md + the deployed-not-committed-not-working discipline). This
asserts every classification + provenance + verdict behaviour the sweep promises:

  CLASSIFY (3-way): a reference to the old path is one of
    - EXECUTED   : a load-bearing position (a code line, a markdown code-span /
                   fenced block, a JSON string VALUE under a live key). BLOCKS the
                   symlink drop. (positive control — MUST be caught)
    - DOC-POINTER: a human-readable position (a comment, a docstring, markdown
                   prose). Cosmetic; never blocks. (negative control — must NOT
                   land in EXECUTED, or it would create false NO-GO churn)
    - KEEP       : intentional (the relocate tooling's own OLD= source, a JSON
                   dict KEY = a dead project key, an inert permissions matcher,
                   a relocate-keep-marked line). Excluded from the verdict.

  PROVENANCE: a git repo is grepped at its CANONICAL ref (origin/main), NOT only
    the working tree — a stale checked-out branch can HIDE a ref that origin/main
    still carries (the WRONG-ARTIFACT-VERIFIED class). The sweep must find a ref
    that exists on origin/main even when the working tree (a different branch)
    lacks it, AND find an uncommitted working-tree-only ref.

  .claude.json BLAST RADIUS: dict KEYS that hold the old path are cosmetic (dead
    keys, never looked up); string VALUES are load-bearing. The sweep reports the
    split and validates the JSON parses (a malformed file warns, never crashes).

  VERDICT + EXIT: any EXECUTED ref -> NO-GO + exit 1; none -> GO + exit 0; a
    clean tree -> GO with zero executed (the NO-OP/GO detector); usage error -> 2.

Hermetic: a temp sandbox with its own roots, --config-dir, and --claude-json;
--no-auto-discover keeps the real machine untouched. Run:
    python3 scripts/test-relocate-sweep.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SWEEP = HERE / "relocate-sweep.py"

fails = 0


def pass_(msg: str) -> None:
    print(f"PASS  {msg}")


def fail(msg: str) -> None:
    global fails
    print(f"FAIL  {msg}")
    fails += 1


def run_sweep(*args, timeout=None):
    """Run the sweep in --json mode; return (exit_code, parsed_obj_or_None, raw).
    On a hang past `timeout`, returns ("TIMEOUT", None, msg) so a test FAILs
    instead of hanging forever."""
    try:
        proc = subprocess.run(
            [sys.executable, str(SWEEP), "--json", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT", None, "subprocess timed out (the tool hung)"
    obj = None
    try:
        obj = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        pass
    return proc.returncode, obj, proc.stdout + proc.stderr


def klass_of(obj, needle: str):
    """Return the classification of the first finding whose path contains needle."""
    for f in obj.get("findings", []):
        if needle in f.get("path", ""):
            return f.get("klass")
    return None


def findings_with(obj, needle: str):
    return [f for f in obj.get("findings", []) if needle in f.get("path", "")]


def classes_of(obj, needle: str):
    """Set of classifications across ALL findings for a path (a file may carry a
    code ref AND prose mentions; the sweep emits one finding per occurrence)."""
    return {f.get("klass") for f in findings_with(obj, needle)}


def git(*args: str, cwd: str) -> None:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
    )
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    if not SWEEP.exists():
        fail(f"relocate-sweep.py not found at {SWEEP} (build it first)")
        return 1

    tmp = tempfile.mkdtemp()
    # A generic old path — NEVER a real personal path (public-repo scrub).
    OLD = f"{tmp}/Desktop/Old Vault"
    NEW = f"{tmp}/Brain"
    base, leaf = "Desktop", "Old Vault"

    # ---------------------------------------------------------------------
    # Root 1: a plain (non-git) filesystem tree with one ref per file type.
    # ---------------------------------------------------------------------
    fsroot = Path(tmp) / "fsroot"
    (fsroot).mkdir(parents=True)

    # .py — a load-bearing assignment (EXECUTED) + a docstring + a comment (DOC).
    (fsroot / "recreator.py").write_text(
        '"""Writes to ' + OLD + ' historically (docstring mention)."""\n'
        "import os\n"
        "# old default was " + OLD + " (comment mention)\n"
        'VAULT_ROOT = "' + OLD + '/sub"\n'
        "os.makedirs(VAULT_ROOT, exist_ok=True)\n"
    )
    # .py — the piecewise pathlib form is a load-bearing expression (EXECUTED).
    (fsroot / "piecewise.py").write_text(
        "from pathlib import Path\n"
        'ROOT = Path.home() / "' + base + '" / "' + leaf + '"\n'
    )
    # .py — a docstring-only mention with NO code ref: must be DOC-POINTER only.
    (fsroot / "doconly.py").write_text(
        '"""Legacy note: this module used to default to ' + OLD + '."""\n'
        "X = 1\n"
    )
    # .sh — a cd into the old path is EXECUTED; a leading-# comment is DOC.
    (fsroot / "run.sh").write_text(
        "#!/usr/bin/env bash\n"
        "# once cd " + OLD + " (comment)\n"
        'cd "' + OLD + '" || exit 1\n'
    )
    # .md — old path inside a fenced block is EXECUTED; in prose it is DOC.
    (fsroot / "prose.md").write_text(
        "The vault used to live at " + OLD + " before the move (prose only).\n"
    )
    (fsroot / "fenced.md").write_text(
        "Deploy:\n\n```bash\ncd \"" + OLD + "\"\nmake\n```\n"
    )
    # KEEP: a relocate-keep-marked line is intentional.
    (fsroot / "keepme.sh").write_text(
        "#!/usr/bin/env bash\n"
        'OLD="' + OLD + '"  # relocate-keep: migration source\n'
    )
    # KEEP: an anti-recreator assertion (asserts the old path is ABSENT) is the
    # OPPOSITE of a recreator — must not be flagged as executed.
    (fsroot / "antiassert.py").write_text(
        "def _default_root():\n    return '/somewhere/else'\n\n"
        'assert "' + OLD + '" not in str(_default_root())\n'
    )

    # ---------------------------------------------------------------------
    # Root 2: a git repo where origin/main CARRIES the ref but the checked-out
    # branch does NOT (provenance: canonical must still find it), PLUS an
    # uncommitted working-tree-only ref.
    # ---------------------------------------------------------------------
    origin = Path(tmp) / "origin.git"
    repo = Path(tmp) / "coderepo"
    git("init", "--bare", str(origin), cwd=tmp)
    git("clone", str(origin), str(repo), cwd=tmp)
    (repo / "config.py").write_text('CWD = "' + OLD + '"  # load-bearing\n')
    git("add", "config.py", cwd=str(repo))
    git("commit", "-m", "add config", cwd=str(repo))
    # default branch may be 'main' or 'master'; force a 'main' on origin.
    git("branch", "-M", "main", cwd=str(repo))
    git("push", "-u", "origin", "main", cwd=str(repo))
    # Move to a different branch and DELETE the ref from the working tree.
    git("checkout", "-b", "feature", cwd=str(repo))
    (repo / "config.py").write_text("CWD = 'repointed'  # fixed on this branch\n")
    git("commit", "-am", "repoint on feature", cwd=str(repo))
    # An uncommitted working-tree-only recreator (never pushed).
    (repo / "uncommitted.sh").write_text('cd "' + OLD + '"\n')

    # ---------------------------------------------------------------------
    # A sandbox ~/.claude.json: a dead project KEY (cosmetic) + a live string
    # VALUE (load-bearing) both referencing the old path.
    # ---------------------------------------------------------------------
    claude_json = Path(tmp) / "claude.json"
    # indent=2 so each value is on its own line (a real ~/.claude.json is indented)
    # — keeps per-finding snippets meaningful instead of the whole one-line blob.
    claude_json.write_text(json.dumps({
        "projects": {OLD: {"history": []}},          # dict KEY = cosmetic
        "statusLine": {"command": OLD + "/bin/x"},   # string VALUE = load-bearing
        "permissions": {"allow": ["Read(" + OLD + "/**)"]},  # inert matcher = KEEP
    }, indent=2))

    # =====================================================================
    # RUN 1: full sweep over both roots + the .claude.json.
    # =====================================================================
    rc, obj, raw = run_sweep(
        "--old", OLD, "--new", NEW,
        "--no-auto-discover",
        "--root", str(fsroot),
        "--root", str(repo),
        "--claude-json", str(claude_json),
        "--config-dir", str(Path(tmp) / "noexist-claude"),
    )
    if obj is None:
        fail(f"sweep did not emit parseable JSON (rc={rc}). Output:\n{raw}")
        print(f"\nFAILED: {fails}")
        return 1

    # ---- CLASSIFY: EXECUTED positive controls ----------------------------
    # Membership, not first-finding: recreator.py carries prose AND a code ref;
    # the code ref must be EXECUTED even though earlier lines are prose.
    for name, label in [
        ("recreator.py", "py assignment (load-bearing)"),
        ("piecewise.py", "py piecewise pathlib expression"),
        ("run.sh", "sh cd command"),
        ("fenced.md", "md fenced block"),
    ]:
        cs = classes_of(obj, name)
        (pass_ if "executed" in cs else fail)(f"EXECUTED: {label} ({name}) -> {cs}")

    # the .claude.json string VALUE must be EXECUTED
    sv = [f for f in obj.get("findings", []) if f.get("file_type") == "json"
          and f.get("klass") == "executed"]
    (pass_ if sv else fail)("EXECUTED: .claude.json statusLine.command string value")

    # ---- CLASSIFY: DOC-POINTER (must NOT block) --------------------------
    k = klass_of(obj, "prose.md")
    (pass_ if k == "doc-pointer" else fail)(f"DOC-POINTER: md prose (prose.md) -> {k}")
    k = klass_of(obj, "doconly.py")
    (pass_ if k == "doc-pointer" else fail)(f"DOC-POINTER: py docstring-only (doconly.py) -> {k}")
    # doconly.py must NOT appear as executed anywhere (the core false-NO-GO guard)
    bad = [f for f in findings_with(obj, "doconly.py") if f.get("klass") == "executed"]
    (pass_ if not bad else fail)("DOC-POINTER: docstring-only never classified EXECUTED")

    # recreator.py has BOTH a docstring/comment mention AND a code ref; the file's
    # finding set must include an EXECUTED one (the code line) — not silently
    # downgraded to doc just because prose mentions exist.
    rk = {f.get("klass") for f in findings_with(obj, "recreator.py")}
    (pass_ if "executed" in rk else fail)("MIXED: recreator.py code line stays EXECUTED amid prose")

    # ---- CLASSIFY: KEEP --------------------------------------------------
    k = klass_of(obj, "keepme.sh")
    (pass_ if k == "keep" else fail)(f"KEEP: relocate-keep-marked line (keepme.sh) -> {k}")
    ka = klass_of(obj, "antiassert.py")
    (pass_ if ka == "keep" else fail)(f"KEEP: anti-recreator `assert ... not in` line (antiassert.py) -> {ka}")
    # The inert permissions matcher in .claude.json must be KEEP, not EXECUTED.
    # Match on the semantic `reason` field, NOT the snippet: a finding's snippet is
    # path-length-fragile (a compact one-line JSON or a long tmp path can push an
    # unrelated key into/out of the 160-char window). `reason` is precise per-finding.
    perm = [f for f in obj.get("findings", []) if "permission" in f.get("reason", "").lower()]
    perm_exec = [f for f in perm if f.get("klass") == "executed"]
    perm_keep = [f for f in perm if f.get("klass") == "keep"]
    (pass_ if perm_keep and not perm_exec else fail)(
        f"KEEP: inert permissions matcher kept, not executed (keep={len(perm_keep)} exec={len(perm_exec)})")

    # ---- PROVENANCE ------------------------------------------------------
    canon = [f for f in findings_with(obj, "config.py")
             if "canonical" in f.get("provenance", "")]
    (pass_ if canon else fail)("PROVENANCE: origin/main ref found though working tree lacks it")
    wt = [f for f in findings_with(obj, "uncommitted.sh")
          if "working" in f.get("provenance", "")]
    (pass_ if wt else fail)("PROVENANCE: uncommitted working-tree-only ref found + labelled")

    # ---- .claude.json blast radius --------------------------------------
    cj = obj.get("claude_json", {})
    (pass_ if cj.get("dict_keys", 0) >= 1 else fail)(".claude.json: dead dict KEY counted (cosmetic)")
    (pass_ if cj.get("string_values", 0) >= 1 else fail)(".claude.json: live string VALUE counted (load-bearing)")
    (pass_ if cj.get("valid") is True else fail)(".claude.json: validated as parseable JSON")

    # ---- VERDICT + exit --------------------------------------------------
    (pass_ if obj.get("verdict") == "NO-GO" else fail)(f"VERDICT: NO-GO with executed refs (got {obj.get('verdict')})")
    (pass_ if rc == 1 else fail)(f"EXIT: 1 on NO-GO (got {rc})")

    # =====================================================================
    # RUN 2: GO path — a clean root with only a DOC-POINTER + a KEEP, no
    # executed refs anywhere -> GO + exit 0 (the NO-OP/GO detector).
    # =====================================================================
    goroot = Path(tmp) / "goroot"
    goroot.mkdir()
    (goroot / "history.md").write_text("We moved from " + OLD + " long ago (prose).\n")
    (goroot / "keep.sh").write_text('OLD="' + OLD + '"  # relocate-keep\n')
    rc2, obj2, raw2 = run_sweep(
        "--old", OLD, "--new", NEW,
        "--no-auto-discover", "--root", str(goroot),
        "--config-dir", str(Path(tmp) / "noexist-claude"),
    )
    if obj2 is None:
        fail(f"GO-run did not emit JSON (rc={rc2}): {raw2}")
    else:
        (pass_ if obj2.get("counts", {}).get("executed", -1) == 0 else fail)(
            "GO detector: zero executed refs over a doc+keep-only tree")
        (pass_ if obj2.get("verdict") == "GO" else fail)(f"VERDICT: GO (got {obj2.get('verdict')})")
        (pass_ if rc2 == 0 else fail)(f"EXIT: 0 on GO (got {rc2})")

    # =====================================================================
    # RUN 3: malformed .claude.json warns, never crashes (fail-loud, not fatal).
    # =====================================================================
    bad_json = Path(tmp) / "bad.json"
    bad_json.write_text("{ not valid json ,,, ")
    rc3, obj3, raw3 = run_sweep(
        "--old", OLD, "--no-auto-discover", "--root", str(goroot),
        "--claude-json", str(bad_json),
        "--config-dir", str(Path(tmp) / "noexist-claude"),
    )
    if obj3 is None:
        fail(f"malformed-claude-json run did not emit JSON (rc={rc3}): {raw3}")
    else:
        cj3 = obj3.get("claude_json", {})
        (pass_ if cj3.get("valid") is False else fail)("malformed .claude.json reported valid=false (not a crash)")
        (pass_ if rc3 in (0, 1) else fail)(f"malformed .claude.json did not crash the sweep (rc={rc3})")

    # =====================================================================
    # RUN 4: usage error (no --old) -> exit 2.
    # =====================================================================
    rc4, _, _ = run_sweep("--no-auto-discover", "--root", str(goroot))
    (pass_ if rc4 == 2 else fail)(f"EXIT: 2 on usage error / missing --old (got {rc4})")
    rc4b, _, _ = run_sweep(
        "--old", OLD, "--read-timeout", "inf", "--no-auto-discover", "--root", str(goroot)
    )
    (pass_ if rc4b == 2 else fail)(
        f"EXIT: 2 on non-finite --read-timeout instead of crashing (got {rc4b})"
    )

    large_root = Path(tmp) / "large-root"
    large_root.mkdir()
    (large_root / "recreator.py").write_text(
        ("# bounded filler\n" * 70_000) + f'CWD = "{OLD}"\n'
    )
    rc4c, obj4c, raw4c = run_sweep(
        "--old", OLD,
        "--no-auto-discover",
        "--root", str(large_root),
        "--config-dir", str(Path(tmp) / "no-config"),
    )
    if obj4c is None:
        fail(f"oversized executable config emitted no JSON (rc={rc4c}): {raw4c[:200]}")
    else:
        (pass_ if rc4c == 1 and obj4c.get("verdict") == "NO-GO" else fail)(
            "INCOMPLETE: oversized executable config can never publish GO"
        )
        (pass_ if obj4c.get("incomplete") else fail)(
            "INCOMPLETE: oversized skipped file is explicit in the report"
        )

    watch_cfg = Path(tmp) / "oversized-watch-config"
    watch_cfg.mkdir()
    (watch_cfg / "relocations.json").write_text(
        json.dumps([{"old": OLD, "new": NEW, "padding": "x" * 1_100_000}])
    )
    rc4d, obj4d, raw4d = run_sweep(
        "--watch",
        "--no-auto-discover",
        "--root", str(goroot),
        "--config-dir", str(watch_cfg),
    )
    if obj4d is None:
        fail(f"oversized watch manifest emitted no JSON (rc={rc4d}): {raw4d[:200]}")
    else:
        (pass_ if rc4d == 1 and obj4d.get("verdict") == "ALARM" else fail)(
            "WATCH: present oversized manifest fails closed with ALARM"
        )
        (pass_ if obj4d.get("manifest_error") else fail)(
            "WATCH: unreadable manifest state is explicit"
        )

    invalid_cfg = Path(tmp) / "invalid-watch-config"
    invalid_cfg.mkdir()
    (invalid_cfg / "relocations.json").write_text(
        json.dumps([{"old": 123, "new": 456}])
    )
    rc4e, obj4e, raw4e = run_sweep(
        "--watch",
        "--no-auto-discover",
        "--root", str(goroot),
        "--config-dir", str(invalid_cfg),
    )
    if obj4e is None:
        fail(f"typed-invalid watch manifest emitted no JSON (rc={rc4e}): {raw4e[:200]}")
    else:
        (pass_ if rc4e == 1 and obj4e.get("verdict") == "ALARM" else fail)(
            "WATCH: non-string old/new entry fails closed with ALARM"
        )
        (pass_ if (invalid_cfg / "relocate-watch-state.json").is_file() else fail)(
            "WATCH: invalid manifest still publishes an ALARM cache"
        )

    # =====================================================================
    # RUN 5: BOUNDED READ — a blocking file (FIFO) is rejected, never opened.
    # The shared primitive recognizes special files before content IO; unknown
    # stalls/placeholders still take the hard-timeout path tested separately.
    # =====================================================================
    fifo_root = Path(tmp) / "fiforoot"
    fifo_root.mkdir()
    (fifo_root / "normal.txt").write_text("history: vault was at " + OLD + "\n")
    if hasattr(os, "mkfifo"):
        made_fifo = True
        try:
            os.mkfifo(str(fifo_root / "blocker.txt"))
        except OSError:
            made_fifo = False
        if made_fifo:
            rc5, obj5, raw5 = run_sweep(
                "--old", OLD, "--no-auto-discover", "--root", str(fifo_root),
                "--read-timeout", "1",
                "--config-dir", str(Path(tmp) / "noexist-claude"),
                timeout=30)
            if rc5 == "TIMEOUT":
                fail("BOUNDED READ: sweep HUNG on a FIFO — un-hangable invariant broken")
            elif obj5 is None:
                fail(f"BOUNDED READ: no JSON (rc={rc5}): {raw5[:200]}")
            else:
                pass_("BOUNDED READ: sweep completed on a FIFO root (did not hang)")
                warned = any("not-regular" in w for w in obj5.get("warnings", []))
                (pass_ if warned else fail)("BOUNDED READ: the FIFO was rejected + warned")
    else:
        pass_("BOUNDED READ: os.mkfifo unavailable here — skipped (cross-platform)")

    # =====================================================================
    # RUN 6: WORKTREE DE-DUP — sibling worktrees of ONE repo collapse to one,
    # so a canonical ref is not reported once per worktree (the dogfood noise).
    # =====================================================================
    wrepo = Path(tmp) / "wtrepo"
    wrepo.mkdir()
    git("init", cwd=str(wrepo))
    (wrepo / "cfg.py").write_text('CWD = "' + OLD + '"\n')
    git("add", "cfg.py", cwd=str(wrepo))
    git("commit", "-m", "cfg", cwd=str(wrepo))
    git("branch", "-M", "main", cwd=str(wrepo))
    wt2 = Path(tmp) / "wtrepo-wt2"
    git("worktree", "add", "-b", "wt2", str(wt2), cwd=str(wrepo))
    rc6, obj6, raw6 = run_sweep("--old", OLD, "--no-auto-discover",
                                "--root", str(wrepo), "--root", str(wt2),
                                "--config-dir", str(Path(tmp) / "noexist-claude"))
    if obj6 is None:
        fail(f"WORKTREE DEDUP: no JSON (rc={rc6}): {raw6[:200]}")
    else:
        cfg = [f for f in obj6.get("findings", []) if "cfg.py" in f.get("path", "")]
        (pass_ if len(cfg) == 1 else fail)(
            f"WORKTREE DEDUP: cfg.py reported once across 2 sibling worktrees (got {len(cfg)})")
    rc6b, obj6b, _ = run_sweep("--old", OLD, "--no-auto-discover",
                               "--root", str(wrepo), "--root", str(wt2),
                               "--include-worktrees",
                               "--config-dir", str(Path(tmp) / "noexist-claude"))
    if obj6b is not None:
        cfgb = [f for f in obj6b.get("findings", []) if "cfg.py" in f.get("path", "")]
        (pass_ if len(cfgb) == 2 else fail)(
            f"WORKTREE DEDUP: --include-worktrees scans both worktrees (got {len(cfgb)})")

    print()
    if fails:
        print(f"FAILED: {fails}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
