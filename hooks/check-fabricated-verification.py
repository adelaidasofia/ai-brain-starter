#!/usr/bin/env python3
"""Stop hook. Block fabricated VERIFICATION claims in the final assistant turn.

An agent's closing message must not assert a state it never confirmed with a
tool call. Three detectors, each fails OPEN (a guard that crash-blocks Stop is
worse than the bug it catches). Evidence = NON-assistant transcript records
(tool_result outputs) + the commands actually executed (tool_use inputs). The
model cannot self-verify from its own prose.

  A. ORPHAN EVIDENCE ID — a deploy/build/run-ID- or commit-SHA-shaped token,
     presented inside backticks in a verification context, that appears in ZERO
     executed commands AND ZERO tool outputs this session. A fabricated ID is,
     by definition, nowhere in the evidence.

  B. CLAIMED-CHECK-WITHOUT-TOOL — an HTTP/curl status claim with no HTTP-fetch
     tool (curl/wget/httpie/WebFetch) executed, OR a deploy-confirmation claim
     with no deploy command (netlify/fly/vercel/deploy.sh) executed.

  C. EXTERNAL-STATE-LANDED — a claim that git/PR state has LANDED on a shared
     surface (pushed / on origin / synced / PR opened / PR merged) with no
     read of the system of record for that surface. "Committed" and "pushed"
     are separate claims: a commit is local; only a remote read (`git
     rev-parse origin/<branch>`, `git ls-remote`, `git fetch`) proves origin
     moved, and only `gh pr view --json headRefOid,state` (or `gh pr
     list`/`gh pr checks`) proves a PR exists or merged. This closes the
     failure mode where a chained `git push && gh pr create` is truncated by
     `tail`, an early step's gate-denial hides in the cut output, and the
     agent reports the whole chain as landed from the mutating command alone
     instead of reading the remote back.

Forensic exemption: a match within +-110 chars of a negation/postmortem word
(didn't, did not, never, no tool call, without running, fabricat, hallucinat,
claim-before-verify, would have) is skipped, so honest writeups pass. A
not-yet-happened match (will push, about to push, ready to push, once this is
merged, need to push) is also skipped, so plans and TODOs pass.

Bypass: FAB_VERIFY_CHECK_BYPASS=1.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Fire telemetry (MYC-285). Fail-open: a missing _lib must never break the
# guard or its tests. Without this the guard is invisible to the fleet report —
# we could not tell whether it ever fires, or is being bypassed in the field.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "_lib"))
    from guard_telemetry import log_fire
except Exception:  # pragma: no cover - telemetry is never load-bearing
    def log_fire(*_a, **_k):
        return

# Token shapes that name a concrete external artifact you can only know by
# running something: git SHAs (7-40 hex), deploy/build IDs (>=8 hex or
# hex-with-dashes), long numeric run IDs (>=8 digits).
ID_TOKEN = re.compile(
    r"`([0-9a-f]{7,40}|[0-9a-f]{8,}(?:-[0-9a-f]{4,}){1,}|\d{8,})`",
    re.IGNORECASE,
)
# Verification context keywords near a cited ID.
VERIFY_CTX = re.compile(
    r"deploy|build|commit|sha|run\s*id|run\s*#|curl|http|readyz|verif|confirm"
    r"|live|prod|green|merged|pushed|shipped|netlify|vercel|fly\b",
    re.IGNORECASE,
)
# Negation / postmortem proximity -> forensic discussion, not a live claim.
FORENSIC = re.compile(
    r"did\s*n.t|didn.t|do\s*not|does\s*n.t|never|without\s+run|no\s+tool\s*call"
    r"|fabricat|hallucinat|claim-before-verify|would\s+have|never\s+ran"
    r"|not\s+actually|no\s+such\s+call|i\s+did\s+not\s+run",
    re.IGNORECASE,
)
# Not-yet-happened proximity -> a plan/TODO or an explicit not-done statement,
# not a landed-state claim. Scoped tight: an exemption only applies when EVERY
# match of a claim sits near one of these, so a message that mixes "not pushed
# yet" with a bare "it's pushed" still blocks.
NOT_YET = re.compile(
    r"\bwill\s+(?:push|merge|open|create)|\babout\s+to\s+|\bready\s+to\s+"
    r"|\bneed(?:s|ed)?\s+to\s+|\blet\s+me\s+|\bonce\s+this\s+is\b"
    r"|\bshould\s+(?:push|merge|open)|\bplan\s+to\s+|\bnext\s+i.?ll\b"
    r"|\bgoing\s+to\s+(?:push|merge|open|create)"
    r"|\bha(?:ve|s)\s*n.t\b|\bha(?:ve|s)\s+not\b|\bhavent\b"
    r"|\bnot\s+(?:yet\s+)?(?:push|merg|open|creat)|\bstill\s+(?:un|not\s+)",
    re.IGNORECASE,
)

# Detector B claim phrases -> the tool-evidence token that MUST be present.
HTTP_CLAIM = re.compile(
    r"\bcurl\b|\bHTTP/?\d?\s*200\b|\bHTTP\s+200\b|post-deploy\s+curl"
    r"|curl\s+(?:confirms?|shows?|returns?)",
    re.IGNORECASE,
)
HTTP_EVIDENCE = re.compile(r"curl|wget|httpie|webfetch|http/2|http/1", re.IGNORECASE)
DEPLOY_CLAIM = re.compile(
    r"netlify\s+deploy|deploy(?:ed|ment)?\s+(?:id|succeed|complete|live|to\s+prod)"
    r"|post-deploy|fly\s+deploy|vercel\s+deploy",
    re.IGNORECASE,
)
DEPLOY_EVIDENCE = re.compile(
    r"netlify|deploy\.sh|fly\s+deploy|flyctl|vercel|\bnpx\s+netlify", re.IGNORECASE
)
# A CI-triggered deploy (merge -> deploy workflow) is VERIFIED by INSPECTING the
# run, not by a local deploy command: `gh run view/list` of a deploy workflow, or
# a CI run URL. Accepting it fixes the false-positive that flagged a real
# `gh run view <deploy-run>` + `curl /readyz` verification as "no deploy ran".
CI_RUN_EVIDENCE = re.compile(r"gh\s+run\b|gh_run|/actions/runs/", re.IGNORECASE)

# Detector C claim phrases -> the READ-of-record that must have executed.
# "Committed" is a local claim: the commit command itself (or a local log/
# rev-parse read) is adequate evidence, since git commit either succeeds or
# errors loudly.
COMMITTED_CLAIM = re.compile(
    r"\b(?:is|are|was|were|got|has\s+been|have\s+been)?\s*committed\b"
    r"|\bthe\s+commit\s+(?:landed|is\s+in|is\s+there)\b"
    r"|\bchanges?\s+(?:is|are)\s+committed\b",
    re.IGNORECASE,
)
COMMITTED_EVIDENCE = re.compile(
    r"git\s+commit\b|git\s+log\b|git\s+rev-parse\s+head\b|git\s+show\b",
    re.IGNORECASE,
)
# "Pushed" / "on origin" / "synced" — only a REMOTE read counts. The mutating
# `git push` command's own (possibly-truncated) output is explicitly NOT
# accepted here — that is exactly the proxy that hid the incident.
PUSHED_CLAIM = re.compile(
    r"\bpushed\b|\bon\s+origin\b|\bup(?:-|\s)?to(?:-|\s)?date\s+with\s+origin\b"
    r"|\bbranch\s+is\s+(?:live|updated)\s+on\s+(?:github|origin)\b"
    r"|\bsynced?\s+(?:to|with)\s+origin\b|\borigin\s+(?:has|is)\s+(?:the|our)\s+"
    r"(?:latest|commit)",
    re.IGNORECASE,
)
PUSHED_EVIDENCE = re.compile(
    r"git\s+ls-remote\b|git\s+fetch\b|git\s+rev-parse\s+origin/"
    r"|git\s+log\s+origin/|git\s+diff\s+origin/|git\s+branch\s+-r\b"
    r"|git\s+status\b.*origin",
    re.IGNORECASE,
)
# "PR opened" / "PR merged" — only reading the PR back counts. `gh pr create`
# / `gh pr merge` running is not evidence of success: a PreToolUse gate can
# deny the call and the agent still narrates it as if it landed.
PR_CLAIM = re.compile(
    r"\bpr\s*#\s*\d+\b|\bpull\s+request\b|\bpr\s+(?:is\s+)?(?:opened|created|open|merged)\b"
    r"|\bthe\s+pr\b[^.]{0,60}\b(?:contains|has|includes|carries)\b",
    re.IGNORECASE,
)
PR_EVIDENCE = re.compile(
    r"gh\s+pr\s+view\b|gh\s+pr\s+list\b|gh\s+pr\s+checks\b|gh\s+pr\s+status\b",
    re.IGNORECASE,
)
MERGED_CLAIM = re.compile(
    r"\bmerged\b|\bmerge\s+(?:complete|succeeded|landed|is\s+done)\b"
    r"|\bit.?s\s+merged\b|\bon\s+main\b|\blanded\s+on\s+main\b",
    re.IGNORECASE,
)


def _present(tok: str, blob: str) -> bool:
    """True only if tok GENUINELY occurs in the evidence blob (already lowercased).

    Plain substring matching let fabricated short SHAs through: in a hex-saturated
    CI session a 7-char SHA prefix coincidentally appears inside some longer hash,
    so `tok in blob` wrongly counts it present and Detector A skips it. A real
    cited short SHA is a PREFIX of the full SHA in tool output, so for a hex token
    we require it NOT be preceded by another hex digit: rejects the mid-string
    coincidence `7887e83` inside `a7887e83bb`, keeps the legit `7887e83` ->
    `7887e83a1b2c`. Numeric run-IDs must match on BOTH digit boundaries; dashed
    deploy IDs match on the left hex/dash boundary.
    """
    t = tok.lower()
    if t.isdigit():
        pat = r"(?<!\d)" + re.escape(t) + r"(?!\d)"
    elif re.fullmatch(r"[0-9a-f]+", t):
        pat = r"(?<![0-9a-f])" + re.escape(t)
    else:
        pat = r"(?<![0-9a-f-])" + re.escape(t)
    return re.search(pat, blob) is not None


def main() -> None:
    if os.environ.get("FAB_VERIFY_CHECK_BYPASS") == "1":
        log_fire("check-fabricated-verification", status="bypassed")
        sys.exit(0)
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if data.get("stop_hook_active"):
        sys.exit(0)
    tp = data.get("transcript_path", "")
    if not tp or not Path(tp).exists():
        sys.exit(0)

    try:
        last = _last_assistant_text(tp)
        if not last:
            sys.exit(0)
        # Cheap gate before the expensive read. Every detector requires a match
        # in `last`; with no claim of any kind there, `findings` is provably
        # empty, so parsing the whole transcript for evidence is pure waste.
        # Stop fires once per turn and a long session's transcript reaches tens
        # of MB, so skipping the second full parse on the common (no-claim) turn
        # is most of this guard's cost. Behavior is unchanged by construction:
        # this is the disjunction of the detectors' own entry conditions.
        if not _has_any_claim(last):
            sys.exit(0)
        result_blob, command_blob = _evidence(tp)
    except Exception:
        sys.exit(0)

    findings = []

    # Detector A — orphan evidence ID
    try:
        for m in ID_TOKEN.finditer(last):
            tok = m.group(1)
            ctx = last[max(0, m.start() - 90): m.end() + 90]
            if not VERIFY_CTX.search(ctx):
                continue
            if FORENSIC.search(last[max(0, m.start() - 110): m.end() + 110]):
                continue
            if _present(tok, result_blob) or _present(tok, command_blob):
                continue
            findings.append(
                f"ID `{tok}` cited as verification evidence, but it appears in no "
                f"tool output and no executed command this session"
            )
    except Exception:
        pass

    # Detector B — claimed check without the tool
    try:
        if HTTP_CLAIM.search(last) and not _forensic_whole(last, HTTP_CLAIM):
            if not HTTP_EVIDENCE.search(result_blob) and not HTTP_EVIDENCE.search(command_blob):
                findings.append(
                    "an HTTP/curl status (e.g. 'curl confirms HTTP 200') is claimed, but no "
                    "curl/wget/WebFetch ran this session"
                )
        if DEPLOY_CLAIM.search(last) and not _forensic_whole(last, DEPLOY_CLAIM):
            deploy_verified = (
                DEPLOY_EVIDENCE.search(command_blob)
                or CI_RUN_EVIDENCE.search(command_blob)
                or HTTP_EVIDENCE.search(command_blob)
            )
            if not deploy_verified:
                findings.append(
                    "a deploy was claimed (netlify/fly/vercel/post-deploy), but no deploy "
                    "command, CI deploy-run inspection (gh run), or live HTTP check ran "
                    "this session"
                )
    except Exception:
        pass

    # Detector C — external git/PR state claimed without a read of the record
    try:
        if COMMITTED_CLAIM.search(last) and not _claim_exempt(last, COMMITTED_CLAIM):
            if not COMMITTED_EVIDENCE.search(command_blob):
                findings.append(
                    "the change is claimed COMMITTED, but no `git commit`/`git log`/"
                    "`git rev-parse HEAD` ran this session — run `git log -1` and cite it, "
                    "or drop the claim"
                )
        if PUSHED_CLAIM.search(last) and not _claim_exempt(last, PUSHED_CLAIM):
            if not PUSHED_EVIDENCE.search(command_blob):
                findings.append(
                    "the branch is claimed PUSHED/on origin, but no read of the remote "
                    "(`git rev-parse origin/<branch>`, `git ls-remote`, `git fetch`) ran this "
                    "session — a `git push` command in the transcript is not itself proof; its "
                    "output can be truncated or the push can fail silently mid-chain. Run "
                    "`git rev-parse origin/<branch>` and cite the SHA, or say 'pushed, not yet "
                    "confirmed on origin'"
                )
        if PR_CLAIM.search(last) and not _claim_exempt(last, PR_CLAIM):
            if not PR_EVIDENCE.search(command_blob):
                findings.append(
                    "a PR is claimed opened/merged/containing specific commits, but no "
                    "`gh pr view <n> --json headRefOid,state` (or `gh pr list`/`gh pr checks`) "
                    "ran this session — `gh pr create` appearing in the transcript is not proof "
                    "it succeeded (a PreToolUse gate can deny it and the command output still "
                    "shows in a truncated tail). Run `gh pr view <n> --json headRefOid,state` "
                    "and cite what it returns, or drop the claim"
                )
        elif MERGED_CLAIM.search(last) and not _claim_exempt(last, MERGED_CLAIM):
            if not PR_EVIDENCE.search(command_blob):
                findings.append(
                    "work is claimed merged/landed on main, but no `gh pr view --json state` "
                    "(or equivalent remote read) ran this session"
                )
    except Exception:
        pass

    if not findings:
        sys.exit(0)

    bullet = "\n".join(f"  - {f}" for f in sorted(set(findings)))
    msg = (
        "Fabricated-verification check blocked the close. In the final message:\n"
        f"{bullet}\n\n"
        "A verification claim must trace to a tool call IN THIS SESSION that read the SYSTEM "
        "OF RECORD, not a local proxy: committed != pushed != PR-opened != merged, and a "
        "mutating command's own (possibly truncated) output is not proof it succeeded — read "
        "the remote back. Either (a) run the read now and cite the real result, or (b) "
        "remove/qualify the claim ('committed locally, not yet pushed'; 'PR creation was "
        "denied by a hook, work is not on origin'). Recording the failure in a session file is "
        "not the fix; producing the evidence is.\n"
        "Forensic writeups and not-yet-happened plans pass automatically. Hard bypass: "
        "FAB_VERIFY_CHECK_BYPASS=1."
    )
    log_fire("check-fabricated-verification", status="blocked", findings=len(set(findings)))
    print(json.dumps({"decision": "block", "reason": msg}))
    sys.exit(0)


def _has_any_claim(text: str) -> bool:
    """True if `text` could possibly trip ANY detector.

    This is the exact disjunction of the detectors' entry conditions:
      A -> ID_TOKEN            B -> HTTP_CLAIM | DEPLOY_CLAIM
      C -> COMMITTED | PUSHED | PR | MERGED
    Keep it in sync when adding a detector, or that detector goes dark. The
    unit suite's blocking cases fail loudly if this ever under-matches, since
    a claim that no longer reaches its detector stops being blocked.
    """
    for pat in (ID_TOKEN, HTTP_CLAIM, DEPLOY_CLAIM,
                COMMITTED_CLAIM, PUSHED_CLAIM, PR_CLAIM, MERGED_CLAIM):
        if pat.search(text):
            return True
    return False


def _claim_exempt(text: str, claim_re: "re.Pattern") -> bool:
    """True if every match of claim_re sits near a forensic negation or a
    not-yet-happened marker (plan/TODO), so it isn't a live landed-state claim."""
    matches = list(claim_re.finditer(text))
    if not matches:
        return False
    for m in matches:
        window = text[max(0, m.start() - 110): m.end() + 110]
        if not (FORENSIC.search(window) or NOT_YET.search(window)):
            return False
    return True


def _forensic_whole(text: str, claim_re: "re.Pattern") -> bool:
    """True if EVERY claim-phrase match sits near a forensic/negation word."""
    matches = list(claim_re.finditer(text))
    if not matches:
        return False
    for m in matches:
        if not FORENSIC.search(text[max(0, m.start() - 110): m.end() + 110]):
            return False
    return True


def _last_assistant_text(transcript_path: str) -> str:
    text = ""
    with open(transcript_path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "assistant":
                continue
            content = rec.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            parts = [
                c.get("text", "")
                for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            if parts:
                text = "\n".join(parts)
    return text


def _evidence(transcript_path: str) -> "tuple[str, str]":
    """(result_blob, command_blob) lowercased.

    result_blob  = text of every tool_result (the ACTUAL outputs).
    command_blob = every Bash tool_use command + url + tool names (what ran).
    Neither draws from assistant free-text, so the model can't self-verify.
    """
    results = []
    commands = []
    with open(transcript_path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = rec.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                t = c.get("type")
                if t == "tool_result":
                    results.append(_flatten(c.get("content", "")))
                elif t == "tool_use":
                    commands.append(str(c.get("name", "")))
                    inp = c.get("input", {})
                    if isinstance(inp, dict):
                        commands.append(str(inp.get("command", "")))
                        commands.append(str(inp.get("url", "")))
    return ("\n".join(results).lower(), "\n".join(commands).lower())


def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict):
                out.append(c.get("text", "") or "")
            else:
                out.append(str(c))
        return "\n".join(out)
    return str(content)


if __name__ == "__main__":
    main()
