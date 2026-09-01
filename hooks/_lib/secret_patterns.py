"""Shared secret-pattern registry.

One module, used by every secret-detection / redaction layer:
- `hooks/detect-secrets-in-bash-output.py` (PostToolUse Bash, alerts on detect)
- `hooks/scrub-session-jsonl-secrets.py` (SessionEnd, rewrites JSONLs)
- `hooks/scan-prior-sessions-for-secrets.py` (SessionStart, warns + auto-scrubs closed sessions)
- Any standalone tool that imports `redact()` or `scan()`

Adding a new pattern here automatically covers all layers. That's the
architectural property: the secret-defense surface is one regex file, not
N hooks that each evolved separately.

False-positive discipline: every pattern in this registry that has known
benign matches (Docker image digests, NPM integrity hashes, base64 blobs,
.env.example placeholders) ships with negative lookbehind/lookahead OR
context requirements that skip the benign case. The self-test at the
bottom of this file gates regressions.

Codified initially after a `heroku releases:info` command dumped a batch
of production secrets into a session transcript. The pattern registry +
auto-scrub layer were the structural fix.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretPattern:
    """A named secret pattern.

    `name` is the human-readable label that appears in the redaction marker
    and the incident report. `regex` is the compiled pattern. `redaction`
    is the replacement string (use `[REDACTED-{name}]` by convention).

    `description` documents the source so future maintainers know why this
    pattern lives in the registry.
    """

    name: str
    regex: re.Pattern[str]
    redaction: str
    description: str


# Provider-issued credentials (long-lived; high blast radius)
_PROVIDER = [
    SecretPattern(
        # The original `api\d{2}` infix was mandatory, so this pattern was blind to
        # `sk-ant-oat01-...` -- the OAuth token Claude Code itself issues and exports
        # as CLAUDE_CODE_OAUTH_TOKEN. Same class as the nvapi- miss below: the one
        # credential shape the product's own auth path uses was the one shape no
        # guard could see, and `redact()` passed it through intact.
        #
        # Matched by PROPERTY -- `sk-ant-` + letters + digits -- not by an enumerated
        # `api|oat` alternation. The infix set is OPEN, and a known-good list against
        # an open set re-opens this exact gap on the next shape Anthropic mints.
        #
        # An intermediate version of this fix used `[a-z]{3}\d{2}`, which is itself a
        # CLOSED set wearing the language of an open one: it silently missed
        # `admin01` (5 letters), `oath01` (4) and `oat1` (1 digit), each of which
        # survived redaction ENTIRELY. Measured, not hypothesised. `[a-z]{2,}\d+`
        # is the weakest structure that actually delivers the stated property.
        #
        # FALSE POSITIVES ARE THE CHEAP SIDE HERE and are accepted deliberately: a
        # long placeholder (`sk-ant-oat01-YOUR_OAUTH_TOKEN_GOES_HERE_...`, 40+ chars)
        # DOES match and gets redacted. The `{40,}` floor suppresses SHORT
        # placeholders and prose only -- it is not a placeholder filter, and an
        # earlier comment here claiming otherwise was measurably false. Redacting a
        # placeholder in a transcript costs nothing; missing a live credential does
        # not. Measured incidence across 2,030 tracked files: one new match, and it
        # is this file's own test vector.
        name="anthropic-api-key",
        regex=re.compile(r"sk-ant-[a-z]{2,}\d+-[\w\-]{40,}", re.ASCII),
        redaction="[REDACTED-anthropic-api-key]",
        description=(
            "Anthropic API key or OAuth token: sk-ant-<letters><digits>-<token>. "
            "Covers api{NN} (API key), oat{NN} (CLAUDE_CODE_OAUTH_TOKEN), and any "
            "future infix of the same shape. Does NOT match an infix with no digits."
        ),
    ),
    SecretPattern(
        # NVIDIA build-API key. Added after a real leak: a live key reached a
        # session transcript via a process listing, then turned up in 8+ files
        # including backups. The shape had been written into a prose checklist
        # a human reads, never into executable code -- so every guard reported
        # a confident CLEAN over it. If an auth path is documented as supported,
        # its credential shape belongs here, in code, not only in a checklist.
        name="nvidia-api-key",
        regex=re.compile(r"nvapi-[A-Za-z0-9_\-]{40,}", re.ASCII),
        redaction="[REDACTED-nvidia-api-key]",
        description="NVIDIA build/NIM API key, format nvapi-{token}.",
    ),
    SecretPattern(
        # Tightened after early audits: original `{20,}` matched
        # documentation placeholders like `sk-...`, `sk-xxxxx`, `sk-YOUR_KEY`.
        # Real OpenAI keys are >=48 chars after the sk- prefix (legacy) or 56
        # chars after sk-proj- (newer). Requires alphanumerics dominate (no
        # `.`, `_-` are rare). Avoids matching sk-ant- (Anthropic).
        name="openai-api-key",
        regex=re.compile(
            r"sk-(?!ant-)(?:proj-[A-Za-z0-9_\-]{56,}|[A-Za-z0-9]{48,})\b",
            re.ASCII,
        ),
        redaction="[REDACTED-openai-api-key]",
        description="OpenAI API key (sk-... >=48 chars, or sk-proj-... >=56 chars).",
    ),
    SecretPattern(
        name="hubspot-private-app-token",
        regex=re.compile(r"pat-na[12]-[\w\-]{20,}", re.ASCII),
        redaction="[REDACTED-hubspot-pat]",
        description="HubSpot private app access token, format pat-na{1,2}-{token}.",
    ),
    SecretPattern(
        name="github-pat-fine-grained",
        regex=re.compile(r"github_pat_[A-Za-z0-9_]{60,}", re.ASCII),
        redaction="[REDACTED-github-pat]",
        description="GitHub fine-grained personal access token.",
    ),
    SecretPattern(
        # `gh[ps]_` covered ghp_ and ghs_ only, while the description one line
        # below advertised gho_. The OAuth shape was never in the class, so
        # scan() and redact() passed every gho_ token through intact -- and
        # ghu_ (user-to-server) and ghr_ (refresh) were absent from BOTH
        # halves. Same GUARD-BLIND-TO-THE-CREDENTIAL-ITS-OWN-DOCS-TELL-YOU-TO-
        # USE class as the nvidia-api-key and npm-access-token entries: the
        # shape existed only in prose, and prose does not execute.
        #
        # Matched by PROPERTY -- `gh` + one LOWERCASE letter + `_` -- not by an
        # enumerated `[pousr]` class, for the reason spelled out on the
        # anthropic-api-key entry above: GitHub's prefix set is OPEN, and a
        # closed list against an open set re-opens this exact gap on the next
        # prefix minted.
        #
        # Superset, with one measured exception -- stated precisely because an
        # unqualified claim here is the same defect this entry exists to fix.
        # PER MATCH POSITION the new class matches everything the old one did
        # (200k-string fuzz: zero old matches the new regex does not match).
        # At SCAN level there is one contrived residual: `{36,}` cannot cross
        # `_`, so a benign `gh<letter>_` run ABUTTING a real token with no
        # separator can end on the token's `gh?` and orphan its body. Needs
        # two tokens concatenated with no delimiter; zero incidence in either
        # corpus below. Pinned by `github-greedy-adjacency-residual` in the
        # self-test so it stays known rather than rediscovered.
        #
        # FP cost measured, not assumed: across 1037 tracked repo files and
        # 1.2 GB of real session transcripts, `gh[a-z]_` returns the identical
        # hit set to `gh[ps]_`. The 36-char alphanumeric run does most of the
        # discriminating -- but the carrier surface did widen from 2 prefix
        # letters to 26, so `<identifier>_<hash>` shapes (`light_<40hex>`,
        # `weight_<36alnum>`) are constructible false positives even though
        # neither corpus contains one.
        #
        # Deliberately NO boundary guard (unlike npm_/B2 below, which chose the
        # opposite trade at birth): retrofitting one would REMOVE existing
        # coverage of a token embedded mid-blob. That is the trade chosen here,
        # not a claim that over-redaction is always safe -- over-redacting a
        # fragment CAN damage a neighbouring pattern's whole-credential match.
        # Measured for the realistic case: over 300 real base64url JWTs this
        # pattern fired on ZERO, and the engineered collision behaves identically
        # under the old `gh[ps]_`, so the interaction is pre-existing and not
        # introduced here.
        name="github-pat-classic",
        regex=re.compile(r"gh[a-z]_[A-Za-z0-9]{36,}", re.ASCII),
        redaction="[REDACTED-github-pat-classic]",
        description=(
            "GitHub token family: ghp_ (classic PAT), gho_ (OAuth), ghu_ "
            "(user-to-server), ghs_ (server-to-server), ghr_ (refresh). "
            "Structural match on gh + one lowercase letter + _, so a newly "
            "minted prefix of THAT shape is covered on arrival; an uppercase, "
            "digit, or multi-letter prefix would not be."
        ),
    ),
    SecretPattern(
        name="heroku-api-key",
        regex=re.compile(r"HRKU-[A-Za-z0-9]{32,}", re.ASCII),
        redaction="[REDACTED-heroku-api-key]",
        description="Heroku API key, format HRKU-{32+ chars}.",
    ),
    SecretPattern(
        name="slack-token",
        regex=re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}", re.ASCII),
        redaction="[REDACTED-slack-token]",
        description="Slack tokens (xoxa, xoxb, xoxp, xoxr, xoxs).",
    ),
    SecretPattern(
        name="stripe-secret-key",
        regex=re.compile(r"sk_(?:test|live)_[A-Za-z0-9]{24,}", re.ASCII),
        redaction="[REDACTED-stripe-secret-key]",
        description="Stripe secret key (sk_test_ or sk_live_).",
    ),
    SecretPattern(
        name="stripe-publishable-key",
        regex=re.compile(r"pk_(?:test|live)_[A-Za-z0-9]{24,}", re.ASCII),
        redaction="[REDACTED-stripe-publishable-key]",
        description="Stripe publishable key (low risk but flagged for visibility).",
    ),
    SecretPattern(
        name="aws-access-key-id",
        regex=re.compile(r"AKIA[0-9A-Z]{16}", re.ASCII),
        redaction="[REDACTED-aws-access-key-id]",
        description="AWS access key ID (the ID itself is sensitive in context).",
    ),
    SecretPattern(
        # Tightened after PDF/base64-binary content in big session JSONLs
        # tripped false-positives: `AIza` appearing mid-base64 with 35
        # alphanumerics immediately following. Real keys are delimited by
        # quote / equals / whitespace / line-start. Char-class lookbehind+
        # lookahead blocks mid-base64 matches but still matches real keys at
        # clean boundaries.
        name="google-api-key",
        regex=re.compile(
            r"(?<![A-Za-z0-9_\-])AIza[A-Za-z0-9_\-]{35}(?![A-Za-z0-9_\-])",
            re.ASCII,
        ),
        redaction="[REDACTED-google-api-key]",
        description="Google API key. Requires non-alphanumeric boundary on both sides to skip base64-binary substring false positives.",
    ),
    SecretPattern(
        name="resend-api-key",
        # Skip `.env.example` placeholders like `re_xxxxxxxxxxxxxxxxxxxx` (all
        # literal x's) or `re_YOUR_KEY_HERE`-style sentinels — those are docs,
        # not credentials. Real Resend keys are random alphanumerics.
        regex=re.compile(
            r"\bre_(?!x{20,}\b)(?!YOUR[_-])(?!your[_-])[A-Za-z0-9]{20,}",
            re.ASCII,
        ),
        redaction="[REDACTED-resend-api-key]",
        description="Resend API key. Skips `re_xxxx...` and `re_YOUR_KEY` placeholders.",
    ),
    SecretPattern(
        # Added after a Comet-style rotation leaked an npg_-prefixed password
        # standalone (not inside a postgres:// URL), bypassing the
        # postgres-url-password pattern below.
        name="neon-password",
        regex=re.compile(r"\bnpg_[A-Za-z0-9]{12,}\b", re.ASCII),
        redaction="[REDACTED-neon-password]",
        description="Neon-issued database password (npg_ prefix, >=12 chars).",
    ),
    SecretPattern(
        # Backblaze B2 applicationKey. The B2 *keyID* is non-secret (it is a
        # username); the *applicationKey* is the secret. Shape: `K` + 3-digit
        # cluster + 27 alphanumerics = 31 chars. Boundaried BOTH sides so it
        # matches at clean delimiters but skips a mid-base64 run -- same
        # discipline as the google-api-key carve-out above, because a 31-char
        # alphanumeric run is common inside encoded blobs.
        name="backblaze-b2-app-key",
        regex=re.compile(r"\bK[0-9]{3}[A-Za-z0-9]{27}\b", re.ASCII),
        redaction="[REDACTED-b2-app-key]",
        description=(
            "Backblaze B2 application key (K + 3-digit cluster + 27 chars = 31). "
            "The keyID is non-secret; this is the applicationKey."
        ),
    ),
    SecretPattern(
        # npm automation/access token. This shape is named in the operator
        # pre-push scrub checklist a human reads, and was in NO executable
        # detector on either the deployed or the substrate copy -- the same
        # GUARD-BLIND-TO-THE-CREDENTIAL-ITS-OWN-DOCS-TELL-YOU-TO-USE class the
        # nvidia-api-key entry above was added for. It is not hypothetical for
        # this repo: ci.sh, the release workflow and bootstrap.sh all reach the
        # npm registry, and an npm_ automation token is the standard credential
        # for a publish path. Boundaried both sides, same reason as B2.
        name="npm-access-token",
        regex=re.compile(r"\bnpm_[A-Za-z0-9]{36,}\b", re.ASCII),
        redaction="[REDACTED-npm-access-token]",
        description="npm automation/access token, format npm_{36+ alphanumerics}.",
    ),
]


# Embedded credentials in connection strings (the value AFTER user: AND BEFORE @)
# We redact just the password portion to keep the URL structure (host, db name,
# query params) legible.
_CONNECTION_STRING = [
    SecretPattern(
        # Redaction includes a space so the marker doesn't match the
        # password group `[^@\s]+` on a second pass — keeps redact() idempotent.
        name="postgres-url-password",
        regex=re.compile(
            r"(postgres(?:ql)?://[^:/\s]+:)([^@\s]+)(@)",
            re.ASCII | re.IGNORECASE,
        ),
        redaction=r"\1REDACTED pg password\3",
        description="Password inside a postgres:// or postgresql:// connection URL.",
    ),
    SecretPattern(
        name="redis-url-password",
        regex=re.compile(
            r"(rediss?://[^:/\s]*:)([^@\s]+)(@)",
            re.ASCII | re.IGNORECASE,
        ),
        redaction=r"\1REDACTED redis password\3",
        description="Password inside a redis:// or rediss:// connection URL.",
    ),
    SecretPattern(
        name="mongo-url-password",
        regex=re.compile(
            r"(mongodb(?:\+srv)?://[^:/\s]+:)([^@\s]+)(@)",
            re.ASCII | re.IGNORECASE,
        ),
        redaction=r"\1REDACTED mongo password\3",
        description="Password inside a mongodb:// or mongodb+srv:// connection URL.",
    ),
]


# Generic-shape patterns (heuristic; lower precision, higher recall)
_GENERIC = [
    SecretPattern(
        name="jwt-bearer",
        regex=re.compile(
            r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
            re.ASCII,
        ),
        redaction="[REDACTED-jwt]",
        description="JWT-shaped token (header.payload.signature, base64url).",
    ),
    SecretPattern(
        name="bearer-header",
        regex=re.compile(
            r"(Authorization:\s*Bearer\s+)([\w\-\.~+/=]{20,})",
            re.ASCII | re.IGNORECASE,
        ),
        redaction=r"\1[REDACTED-bearer]",
        description="Authorization: Bearer <token> header value.",
    ),
    SecretPattern(
        name="hex-256bit-secret",
        regex=re.compile(
            # Skip content-integrity hashes: sha256: / sha512: / sha384: /
            # sha1: / md5: prefixes (GitHub Releases asset digests, Docker
            # image manifests, NPM hex-integrity, etag/digest HTTP headers).
            # Added after release-asset JSON tripped this pattern on every
            # `gh release view` and `gh api .../releases` response — those
            # sha256 digests are public by design, not secrets. Each
            # lookbehind is fixed-width so compiles cleanly.
            #
            # Dot-prefix variants (sha256. / sha512. / sha384. / sha1. /
            # md5.) added for pnpm's `packageManager` field format
            # `pnpm@VERSION+sha256.HASH` (also yarn integrity
            # `<algo>-<base64>` and some lockfile formats).
            #
            # Bumblebee content-addressed record_id prefixes (scan_summary:
            # / package: / finding: / diagnostic:) added after
            # perplexityai/bumblebee (Apache-2.0 supply-chain endpoint
            # inventory tool) tripped this pattern on documented public
            # content hashes. Per upstream docs/state-model.md:
            # "`record_id` is a content-addressed hash from a canonical
            # tuple per record type, not the full JSON payload."
            # Sibling carve-out to sha256: / sha512: above. Verified
            # precise: bumblebee shapes clean, bare-hex secrets still trip.
            #
            # Skip the Workflow runtime's content-addressed agent-call cache
            # keys, serialized as `"key":"v2:<64-hex>"` in
            # subagents/workflows/wf_*/journal.jsonl. Those are sha256 cache
            # keys (a content address, not a secret). Critically the SAME
            # redact() runs in the SessionEnd scrub layer — without this
            # carve-out a scrub rewrites the cache key to [REDACTED-hex-256bit]
            # and breaks Workflow resume. The lookbehind anchors on the JSON
            # value structure `:"v<digit>:` so it survives a version bump
            # (v2 -> v3) yet never masks a bare-hex secret, which is never
            # preceded by that prefix. Fixed-width (\d = 1 char).
            r"(?<!sha256:)(?<!sha512:)(?<!sha384:)(?<!sha1:)(?<!md5:)"
            r"(?<!sha256\.)(?<!sha512\.)(?<!sha384\.)(?<!sha1\.)(?<!md5\.)"
            r"(?<!scan_summary:)(?<!package:)(?<!finding:)(?<!diagnostic:)"
            r'(?<!:"v\d:)'
            r"(?<![A-Fa-f0-9])([A-Fa-f0-9]{64})(?![A-Fa-f0-9])",
            re.ASCII,
        ),
        redaction="[REDACTED-hex-256bit]",
        description="64-char hex string (256-bit secret; HMAC, openssl rand -hex 32). Skips common content-digest prefixes (sha{1,256,384,512}:, md5:), integrity-hash dot prefixes (sha{1,256,384,512}., md5.) used by pnpm/yarn, and the Workflow runtime's content-addressed cache key shape (\":\"v<digit>:<hex>\").",
    ),
]


# Aggregate registry (order matters — connection strings first so URL password
# match wins over a JWT-like substring of the URL; provider patterns last so
# they don't shadow more specific patterns).
PATTERNS: tuple[SecretPattern, ...] = tuple(
    _CONNECTION_STRING + _PROVIDER + _GENERIC
)


def redact(text: str) -> tuple[str, list[str]]:
    """Apply every registered pattern. Return (redacted_text, hit_names).

    `hit_names` is the list of pattern names that matched at least once.
    Caller uses it for incident-reporting + the "what got rotated" stamp.

    Idempotent: redacted text passed back through `redact()` returns
    (text, []) — the redaction markers themselves don't match any pattern.
    """
    hits: list[str] = []
    out = text
    for p in PATTERNS:
        new, n = p.regex.subn(p.redaction, out)
        if n > 0:
            hits.append(p.name)
            out = new
    return out, hits


def scan(text: str) -> list[tuple[str, int]]:
    """Read-only scan. Return [(pattern_name, match_count), ...].

    Use this for detection-without-mutation paths (PostToolUse alert,
    SessionStart warning). Output is empty when the text is clean.
    """
    results: list[tuple[str, int]] = []
    for p in PATTERNS:
        matches = p.regex.findall(text)
        if matches:
            results.append((p.name, len(matches)))
    return results


# ---------------------------------------------------------------------------
# Alert-layer context filter (PostToolUse hook ONLY — scan()/redact() untouched).
#
# Two documented tool-output shapes are content-addressed IDs, not credentials,
# yet share the bare-64-hex shape with real secrets. The regex alone cannot
# express them (they need the *command* for context), and the scrub / corpus /
# SessionStart layers must stay aggressive (redacting an ID is harmless;
# missing a secret is not). So the carve-out lives here as an explicit filter
# applied only by detect-secrets-in-bash-output.py. Suppressed hits are still
# audit-logged by the caller with a reason — observability is preserved.
#
# Added 2026-07-14 after a database-migration verification run fired 16 false
# alerts in one session: 1× the container ID echoed on its own line by
# `docker run -d` (the image digest on the neighboring line was already
# skipped by the sha256: lookbehind above) and 15× drizzle migration
# content-hashes from `SELECT id, hash, created_at FROM
# drizzle.__drizzle_migrations` (psql rows `  1 | <64-hex> | <epoch-millis>`).
# Security cry-wolf trains dismissal.

# Docker lifecycle verbs whose STDOUT is by contract an ID/name echo.
# `exec`, `inspect`, `logs`, and foreground `run` are deliberately absent:
# their output is arbitrary program/config text that can contain a real
# secret (e.g. `docker exec app printenv KEY` prints the value bare on a
# line). `run` qualifies only when detached (-d / --detach / common -dit
# bundles): detached stdout is exactly the new container ID.
_DOCKER_ID_ECHO_CMD = re.compile(
    r"\bdocker\s+(?:container\s+)?"
    r"(?:create\b|start\b|stop\b|restart\b|kill\b|rm\b|wait\b|ps\b"
    r"|run(?=[^|;&\n]*\s(?:-d|--detach|-dit|-itd|-dt|-td|-di|-id)\b)"
    r")",
    re.ASCII,
)
# A line that is nothing but one 64-hex token: the docker ID-echo shape, and
# the single-column `SELECT hash FROM drizzle.__drizzle_migrations` shape.
_BARE_HEX_LINE = re.compile(r"^\s*[A-Fa-f0-9]{64}\s*$", re.ASCII)
# psql result-row shapes for drizzle.__drizzle_migrations output:
#   `  1 | <hex> | 1782448308040`  (SELECT * / SELECT id, hash, created_at;
#                                   tail also tolerates a cast timestamp)
#   `hash | <hex>`                 (\x expanded display)
_DRIZZLE_ROW_LINE = re.compile(
    r"^\s*(?:\d+\s*\|\s*[A-Fa-f0-9]{64}\s*(?:\|\s*[\d\s:.T+-]*)?"
    r"|hash\s*\|\s*[A-Fa-f0-9]{64})\s*$",
    re.ASCII,
)
_DRIZZLE_CMD_MARKER = "__drizzle_migrations"
_HEX_PATTERN_NAME = "hex-256bit-secret"


def filter_tool_output_false_positives(
    hits: list[tuple[str, int]], text: str, command: str
) -> tuple[list[tuple[str, int]], list[tuple[str, int, str]]]:
    """Alert-layer-only reclassification of hex-256bit-secret hits.

    Given `scan(text)` results plus the Bash COMMAND that produced `text`,
    suppress individual hex-256bit matches that are provably content-addressed
    IDs in this command context:

    - "docker-cli-id-echo": the command is a docker lifecycle invocation whose
      stdout is by contract an ID echo, AND the match is a line consisting of
      exactly one bare 64-hex token.
    - "drizzle-migration-hash": the command queries __drizzle_migrations, AND
      the match sits on a psql result-row line (or a bare single-column line).

    Returns (remaining_hits, suppressed) where suppressed entries are
    (pattern_name, suppressed_count, reason). Patterns other than
    hex-256bit-secret always pass through untouched. A hex match whose line
    does not match the expected shape keeps alerting (fail toward detection).
    """
    hex_entry = next((h for h in hits if h[0] == _HEX_PATTERN_NAME), None)
    if hex_entry is None or not command:
        return hits, []
    docker_ctx = bool(_DOCKER_ID_ECHO_CMD.search(command))
    drizzle_ctx = _DRIZZLE_CMD_MARKER in command
    if not (docker_ctx or drizzle_ctx):
        return hits, []

    hex_regex = next(p.regex for p in PATTERNS if p.name == _HEX_PATTERN_NAME)
    suppressed_counts: dict[str, int] = {}
    remaining = 0
    for m in hex_regex.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        line = text[line_start: line_end if line_end != -1 else len(text)]
        if docker_ctx and _BARE_HEX_LINE.match(line):
            reason = "docker-cli-id-echo"
        elif drizzle_ctx and (
            _DRIZZLE_ROW_LINE.match(line) or _BARE_HEX_LINE.match(line)
        ):
            reason = "drizzle-migration-hash"
        else:
            remaining += 1
            continue
        suppressed_counts[reason] = suppressed_counts.get(reason, 0) + 1

    if not suppressed_counts:
        return hits, []
    out_hits = [h for h in hits if h[0] != _HEX_PATTERN_NAME]
    if remaining:
        out_hits.append((_HEX_PATTERN_NAME, remaining))
    suppressed = [
        (_HEX_PATTERN_NAME, count, reason)
        for reason, count in sorted(suppressed_counts.items())
    ]
    return out_hits, suppressed


# Self-test (run `python3 -m hooks._lib.secret_patterns`)
if __name__ == "__main__":
    # This module's self-test prints pattern names and redaction markers. On a
    # Windows cp1252 console any non-ASCII byte in that output raises
    # UnicodeEncodeError and the self-test dies with no legible cause
    # (ai-brain-starter#313). Idempotent; a no-op on an already-UTF-8 console.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass

    SAMPLES = {
        "anthropic-api-key": "sk-ant-api03-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        # The oat shape (CLAUDE_CODE_OAUTH_TOKEN). Was invisible to this registry
        # until the `api\d{2}` infix was widened to `[a-z]{3}\d{2}`.
        "anthropic-oauth-token": "sk-ant-oat01-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "hubspot-pat": "pat-na1-AAAAAAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
        "postgres-url": "postgres://user:hunter2_supersecret@db.example.com:5432/dbname",
        "redis-url": "rediss://:p123abc456def@redis.example.com:6379",
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        "github-pat": "github_pat_AAAAAAAAAAAAAAAAAAAAAA_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN",
        "nvidia-api-key": "nvapi-" + "Xy7Zq2Lm9Rt4Bv6Nc8Ka1Pd3Wf5Hj0Gs7Ue2Yi4Ao6Bn1Cm",
        "hex-256": "a" * 64,
        "clean": "no secrets here just regular text and code",
    }
    for label, sample in SAMPLES.items():
        redacted, hits = redact(sample)
        print(f"{label}: hits={hits}")
        if "REDACTED" in redacted:
            print(f"  -> {redacted}")
    print("\nIdempotency check:")
    # Both Anthropic shapes: an api key and an oat OAuth token.
    for _prefix in ("sk-ant-api03-", "sk-ant-oat01-"):
        once, hits1 = redact(_prefix + "x" * 50)
        twice, hits2 = redact(once)
        assert hits1 != [], f"{_prefix} was not redacted at all, got {hits1}"
        assert once == twice, f"non-idempotent for {_prefix}"
        assert hits2 == [], f"second pass should produce no hits, got {hits2}"
    print("  OK: redaction is idempotent.")

    # False-positive exclusions — gate regressions.
    print("\nFalse-positive exclusion checks:")
    FP_SAMPLES = {
        # sha256:<hex> content digest (GitHub release asset, Docker manifest)
        "github-asset-digest": '"digest":"sha256:' + "a" * 64 + '"',
        # sha512:<hex> NPM hex-integrity, Docker manifest
        "sha512-hex": "sha512:" + "b" * 64,
        # pnpm packageManager integrity hash (dot separator, not colon)
        "pnpm-package-manager": '"packageManager": "pnpm@10.11.1+sha256.' + "c" * 64 + '"',
        # Workflow runtime content-addressed cache key — `"key":"v2:<hex>"` in
        # subagents/workflows/wf_*/journal.jsonl. The SAME redact() runs in the
        # SessionEnd scrub, so this MUST be skipped or a scrub breaks Workflow resume.
        "workflow-cache-key-v2": '{"type":"started","key":"v2:' + "e" * 64 + '","agentId":"x"}',
        # Version-robustness: a future v3: prefix must also be skipped.
        "workflow-cache-key-v3": '"key":"v3:' + "f" * 64 + '"',
        # Real 256-bit hex secret — MUST still match
        "real-hex-secret": "API_KEY=" + "d" * 64,
        # Resend literal placeholder (`re_x` * 30 in .env.example)
        "resend-placeholder-xs": "RESEND_API_KEY=re_" + "x" * 30,
        # Resend YOUR_KEY-style placeholder
        "resend-placeholder-your": "RESEND_API_KEY=re_YOUR_API_KEY_HERE_123",
        # Real Resend key — MUST still match
        "real-resend-key": "RESEND_API_KEY=re_AbCdEfGh12345678IjKlMn",
        # AIza substring inside base64-binary content (mid-blob, no boundary)
        "aiza-in-base64": "...ZNAEUVnbQjEVvEg3bvlQDn1oABZWqpKAIza" + "X" * 35 + "trm1xb...",
        # Real Google API key — MUST still match (quoted assignment)
        "real-google-api-key": 'GOOGLE_MAPS_KEY="AIza' + "Y" * 35 + '"',
        # Real Google API key at line start — MUST still match
        "real-google-api-key-bare": "AIza" + "Z" * 35,
        # Anthropic oat shape (CLAUDE_CODE_OAUTH_TOKEN) — MUST match. The SAMPLES
        # loop above only PRINTS, so an asserted vector is required for the widening
        # to be proven at all. These asserts run under `python3 -m hooks._lib
        # .secret_patterns`; the CI-registered coverage lives in
        # hooks/test_secret_patterns_anthropic.py, which scripts/ci.sh executes.
        "real-anthropic-oauth-token": "export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-" + "Kp7Vn2Qr9Xt4Bm6Zc8La1Wd3Hf5Jg0Ys7Ue2Ni4Ao6Br1Cm",
        # Existing api shape — MUST still match (regression guard on the widening).
        "real-anthropic-api-key": "sk-ant-api03-" + "Kp7Vn2Qr9Xt4Bm6Zc8La1Wd3Hf5Jg0Ys7Ue2Ni4Ao6Br1Cm",
        # Shapes the intermediate `[a-z]{3}\d{2}` silently missed. Each of these
        # survived redaction ENTIRELY before the infix was opened to `[a-z]{2,}\d+`.
        "real-anthropic-admin-key": "sk-ant-admin01-" + "Kp7Vn2Qr9Xt4Bm6Zc8La1Wd3Hf5Jg0Ys7Ue2Ni4Ao6Br1Cm",
        "real-anthropic-4letter-infix": "sk-ant-oath01-" + "Kp7Vn2Qr9Xt4Bm6Zc8La1Wd3Hf5Jg0Ys7Ue2Ni4Ao6Br1Cm",
        "real-anthropic-1digit-infix": "sk-ant-oat1-" + "Kp7Vn2Qr9Xt4Bm6Zc8La1Wd3Hf5Jg0Ys7Ue2Ni4Ao6Br1Cm",
        # Negative controls: the match is STRUCTURED (letters then digits), not a
        # blanket `sk-ant-*`. Each of these must stay silent.
        "anthropic-oat-placeholder": "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-YOUR_TOKEN_HERE",
        "anthropic-oat-too-short": "sk-ant-oat01-abc123",
        "anthropic-prose-mention": "The OAuth token is shaped sk-ant-oat01- plus a long token.",
        # No digit pair after the 3 letters → not the documented shape → no match.
        "anthropic-no-digit-infix": "sk-ant-oauth-" + "z" * 50,
        # Backblaze B2 applicationKey — MUST match. K + 3-digit cluster + 27.
        "real-b2-app-key": "B2_KEY=K005" + "A" * 27,
        # Same shape mid-base64 (no boundary either side) — MUST be skipped.
        "b2-in-base64": "x" + "K005" + "A" * 27 + "z",
        # npm automation token — MUST match. npm_ + 36.
        "real-npm-token": "NPM_TOKEN=npm_" + "B" * 36,
        # Same shape mid-base64 (no boundary either side) — MUST be skipped.
        "npm-in-base64": "x" + "npm_" + "B" * 36 + "z",
        # Longer-than-36 token — MUST still match. Pins the `,` in {36,}:
        # a bare {36} would silently miss any future longer npm shape.
        "real-npm-token-long": "NPM_TOKEN=npm_" + "C" * 44,
        # GitHub token family. The old `gh[ps]_` class covered ghp_/ghs_ only
        # while the description advertised gho_, so gho_/ghu_/ghr_ tokens were
        # redacted by NOTHING. Each of these survived redaction entirely before
        # the class was opened to `gh[a-z]_`.
        "real-github-oauth-token": "GH_TOKEN=gho_" + "A" * 36,
        "real-github-user-to-server": "GH_TOKEN=ghu_" + "C" * 36,
        "real-github-refresh-token": "GH_TOKEN=ghr_" + "E" * 36,
        # Shapes the old class already caught — regression guard on the widening.
        "real-github-classic-pat": "GH_TOKEN=ghp_" + "B" * 36,
        "real-github-server-to-server": "GH_TOKEN=ghs_" + "D" * 36,
        # A prefix letter outside the documented set. The set is OPEN, so a
        # structural match is the point: this MUST match, and would not under
        # any enumerated `[pousr]` class.
        "real-github-future-prefix": "GH_TOKEN=ghz_" + "F" * 36,
        # Negative controls: the match is STRUCTURED, not a blanket `gh*`.
        "github-prose-mention": "OAuth tokens are shaped gho_ plus 36 chars.",
        "github-token-too-short": "gho_abc123",
        "github-placeholder": "GH_TOKEN=gho_YOUR_TOKEN_HERE",
        # The fine-grained shape owns its own pattern. The structural classic
        # match must NOT also fire on it (double-redaction / wrong label).
        "github-fine-grained-not-classic": "github_pat_" + "A" * 22 + "_" + "b" * 60,
    }
    expected_hits = {
        "github-asset-digest": [],   # sha256: prefix → skipped
        "sha512-hex": [],            # sha512: prefix → skipped
        "pnpm-package-manager": [],  # sha256. prefix → skipped
        "workflow-cache-key-v2": [],  # :"v2: prefix → skipped (Workflow cache key)
        "workflow-cache-key-v3": [],  # :"v3: prefix → skipped (version-robust)
        "real-hex-secret": ["hex-256bit-secret"],
        "resend-placeholder-xs": [],
        "resend-placeholder-your": [],
        "real-resend-key": ["resend-api-key"],
        "aiza-in-base64": [],         # surrounded by alphanumerics → skipped
        "real-google-api-key": ["google-api-key"],
        "real-google-api-key-bare": ["google-api-key"],
        "real-anthropic-oauth-token": ["anthropic-api-key"],
        "real-anthropic-api-key": ["anthropic-api-key"],
        "anthropic-oat-placeholder": [],   # <40 chars after prefix
        "anthropic-oat-too-short": [],     # <40 chars after prefix
        "anthropic-prose-mention": [],     # bare shape, no token
        "real-anthropic-admin-key": ["anthropic-api-key"],
        "real-anthropic-4letter-infix": ["anthropic-api-key"],
        "real-anthropic-1digit-infix": ["anthropic-api-key"],
        # `oauth` has NO digits, so it is not the credential shape. This stays a
        # legitimate boundary: it separates a credential from a word, and unlike
        # the letter-count boundary it does not hide a real token shape.
        "anthropic-no-digit-infix": [],
        "real-b2-app-key": ["backblaze-b2-app-key"],
        "b2-in-base64": [],           # no boundary before K → skipped
        "real-npm-token": ["npm-access-token"],
        "npm-in-base64": [],          # no boundary before npm_ → skipped
        "real-npm-token-long": ["npm-access-token"],
        "real-github-oauth-token": ["github-pat-classic"],
        "real-github-user-to-server": ["github-pat-classic"],
        "real-github-refresh-token": ["github-pat-classic"],
        "real-github-classic-pat": ["github-pat-classic"],
        "real-github-server-to-server": ["github-pat-classic"],
        "real-github-future-prefix": ["github-pat-classic"],
        "github-prose-mention": [],       # bare shape, no token
        "github-token-too-short": [],     # <36 chars after prefix
        "github-placeholder": [],         # underscores break the alnum run
        # Fine-grained pattern claims it; the classic one must stay silent.
        "github-fine-grained-not-classic": ["github-pat-fine-grained"],
    }
    failures = 0
    for label, sample in FP_SAMPLES.items():
        _, hits = redact(sample)
        want = expected_hits[label]
        ok = hits == want
        print(f"  [{'OK' if ok else 'FAIL'}] {label}: got={hits} want={want}")
        if not ok:
            failures += 1
    if failures:
        raise SystemExit(f"\n{failures} FP-exclusion check(s) failed.")
    print("  OK: all FP exclusions behave as expected.")

    # Alert-layer FP filter checks (added 2026-07-14; see
    # filter_tool_output_false_positives above for the docker-run-detached +
    # drizzle-migration-hash incident this closes — PostToolUse hook only,
    # scan()/redact() themselves are untouched).
    print("\nAlert-layer FP filter checks:")
    _hx = "1" * 64
    _drizzle_cmd = 'psql -c "SELECT id, hash, created_at FROM drizzle.__drizzle_migrations"'
    AF_CASES = {
        "docker-run-detached-id-echo": (
            ([("hex-256bit-secret", 1)], _hx, "docker run -d --name x postgres:16"),
            ([], [("hex-256bit-secret", 1, "docker-cli-id-echo")]),
        ),
        "drizzle-migration-hash-row": (
            ([("hex-256bit-secret", 1)], "  1 | " + _hx + " | 1782448308040", _drizzle_cmd),
            ([], [("hex-256bit-secret", 1, "drizzle-migration-hash")]),
        ),
        "export-secret-not-suppressed-in-drizzle-context": (
            ([("hex-256bit-secret", 1)], "export SECRET=" + _hx, _drizzle_cmd),
            ([("hex-256bit-secret", 1)], []),
        ),
        "no-context-still-fires": (
            ([("hex-256bit-secret", 1)], _hx, "openssl rand -hex 32"),
            ([("hex-256bit-secret", 1)], []),
        ),
    }
    af_failures = 0
    for label, (args, want) in AF_CASES.items():
        got = filter_tool_output_false_positives(*args)
        ok = got == want
        print(f"  [{'OK' if ok else 'FAIL'}] {label}: got={got} want={want}")
        if not ok:
            af_failures += 1
    if af_failures:
        raise SystemExit(f"\n{af_failures} alert-layer FP filter check(s) failed.")
    print("  OK: alert-layer FP filter behaves as expected.")
