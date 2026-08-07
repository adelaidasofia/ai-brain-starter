"""Shared Claude-routing helper for vault scripts.

Routes Claude calls in priority order:

  1. Local `claude -p` CLI (Max plan via OAuth/keychain). DEFAULT.
  2. Anthropic SDK with ANTHROPIC_API_KEY (legacy, separate billing).
  3. Caller-supplied fallback (e.g., MiniMax). Optional.

Why this exists: vault scripts that hit `https://api.anthropic.com/v1/messages`
directly burn through API-key billing while a Max plan covers the same
model via the local `claude` CLI. Documented in
`⚙️ Meta/Critical Failure Inventory.md` "Schedule + cron surface" entry
(2026-05-10) and the substrate audit at
`🏠 Home/Substrate Audit/2026-05-10.md`.

Escape hatch: set `CLAUDE_ROUTER_PREFER_API_KEY=1` to skip the CLI step
and go directly to API-key billing. Set `CLAUDE_ROUTER_DISABLE_CLI=1`
to force the API-key path even when the CLI is present.

Usage:

    from _claude_router import call_claude_text

    text = call_claude_text(
        system="You are a JSON generator. Reply only with valid JSON.",
        user="Extract {names, dates} from: ...",
        model="haiku",          # alias OR full model ID
        max_tokens=2048,
    )

    # For structured JSON output:
    from _claude_router import call_claude_json
    obj = call_claude_json(
        system="...",
        user="...",
        model="haiku",
    )

If both the CLI and API key are unavailable, raises `RouterUnavailable`.
Caller can catch and fall through to its own fallback (MiniMax, etc).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


__all__ = [
    "call_claude_text",
    "call_claude_json",
    "RouterUnavailable",
    "RateLimitExhausted",
]


CLI_TIMEOUT_SECONDS = 600
API_TIMEOUT_SECONDS = 180


class RouterUnavailable(RuntimeError):
    """Raised when neither the CLI nor an API key is available."""


class RateLimitExhausted(RouterUnavailable):
    """Subclass for transient rate-limit refusals (weekly Max cap, 5h burst, etc.).

    Distinguished from the parent so callers can return a transient-failure exit
    code (e.g. EX_TEMPFAIL, 75) and signal a scheduler that the next run — after
    the limit window resets — will likely succeed, versus a hard config failure.
    A subclass, so existing ``except RouterUnavailable`` handlers still catch it.
    """


def _log(msg: str) -> None:
    """Stderr log so it doesn't pollute the JSON output capture."""
    print(f"[claude-router] {msg}", file=sys.stderr)


def _resolve_cli() -> str | None:
    """Find the local `claude` CLI binary, or None if missing."""
    if os.environ.get("CLAUDE_ROUTER_DISABLE_CLI") == "1":
        return None

    explicit = os.environ.get("CLAUDE_CLI_PATH")
    if explicit and Path(explicit).exists():
        return explicit

    candidate = shutil.which("claude")
    if candidate:
        return candidate

    home = Path.home()
    for fallback in [
        home / "local/node-v20.19.0-darwin-arm64/bin/claude",
        home / ".local/bin/claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ]:
        if fallback.exists():
            return str(fallback)

    return None


def _resolve_api_key() -> str | None:
    """Look for ANTHROPIC_API_KEY in env. Caller is responsible for sourcing
    ~/.zsh_secrets if needed (this helper does not read user secrets files)."""
    return os.environ.get("ANTHROPIC_API_KEY") or None


def _extract_result_envelope(stdout: str) -> dict | None:
    """Return the `--output-format json` result envelope, or None if stdout is
    not a parseable result envelope.

    None is itself a STRUCTURAL failure signal: in json mode a real response is
    always a valid envelope, so unparseable stdout can never be a success — this
    is the positive content-validity gate. Tolerant of a stray hook line leaked
    ahead of the envelope (scans from the last line back)."""
    if not stdout:
        return None
    try:
        obj = json.loads(stdout)
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    except json.JSONDecodeError:
        pass
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    return None


# Marker tuple is a failure CLASSIFIER (rate-limit vs generic-unavailable) on an
# ALREADY-determined failure — never the success gate. The old code sniffed raw
# stdout against a denylist to DECIDE success and was patched repeatedly, always
# one banner phrasing behind: an unrecognized banner (login / connection /
# weekly-limit / session-limit) was consumed as a successful response. The fix
# makes success purely structural (a valid result envelope with is_error false),
# so an unknown future banner has no envelope and can never be consumed as
# content. Bug class: PRODUCER-OUTPUT-CONSUMED-WITHOUT-CONTENT-VALIDITY-CHECK.
_RATELIMIT_MARKERS = (
    "weekly limit",
    "hit your weekly",
    "session limit",        # 5-hour rolling cap (newer CLI)
    "hit your session",
    "rate limit",
    "rate_limit_exceeded",
    "usage limit",
    "quota exceeded",
)


def _classify_cli_failure(text: str, raw: str = "") -> RouterUnavailable:
    """Map a known-failure error string to the right typed exception.

    Rate-limit markers -> RateLimitExhausted (transient; the next scheduled run
    after the window reset should succeed). Everything else (auth/network
    refusal OR an unrecognized banner) -> RouterUnavailable. Default-unavailable
    on unknown is the safe direction: a banner we don't recognize fails loudly
    instead of being consumed as a response."""
    low = (text or "").lower()
    snippet = ((raw or text or "")[:200])
    if any(m in low for m in _RATELIMIT_MARKERS):
        return RateLimitExhausted(
            f"claude CLI rate-limit signal: {snippet!r}. Transient — the next "
            "scheduled run after the limit window resets should succeed."
        )
    return RouterUnavailable(
        f"claude CLI failure (no valid result envelope): {snippet!r}. "
        "Auth/network refusal or an unrecognized banner — not a model response."
    )


def _call_via_cli(
    cli: str, system: str, user: str, model: str
) -> str:
    """Run `claude -p --output-format json` and return the envelope's result text.

    Acceptance logic (structural — independent of exit code):
      - valid result envelope, is_error=false → accept (return envelope.result)
      - valid result envelope, is_error=true  → raise (classified)
      - NO valid envelope, non-empty stdout    → raise (classify the banner)
      - NO valid envelope, empty stdout        → raise RouterUnavailable

    Success is gated on a parseable result envelope, NOT on sniffing raw text
    for a denylist of bad banners. An unknown future banner has no envelope and
    can never be returned as content — that is the whole
    PRODUCER-OUTPUT-CONSUMED-WITHOUT-CONTENT-VALIDITY-CHECK fix.

    HARD DEPENDENCY on `--output-format json`: a CLI that IGNORES the flag and
    emits plain text on exit 0 (an older `claude`, or a wrapper that strips the
    flag) has no envelope, so it is treated as UNAVAILABLE and call_claude_text
    falls through to the API-key tier. This is the safe direction (no envelope
    is never consumed as content) and is intentional, not silent.

    Exit code is no longer part of the gate: `claude -p` runs SessionStart and
    SessionEnd hooks even in print mode, and a hook failing AFTER Claude wrote
    its response exits non-zero while the envelope on stdout is still valid — so
    we accept it. The earlier MIN_RESPONSE_CHARS heuristic was wrong because
    legitimate replies can be very short ("PONG.", "Yes", "42").
    """
    cmd = [
        cli,
        "-p",
        user,
        "--append-system-prompt",
        system,
        "--model",
        model,
        "--output-format",
        "json",                 # structured envelope, not raw text
        "--no-session-persistence",
        "--disable-slash-commands",
        "--dangerously-skip-permissions",
        # Router calls are pure text/JSON generation — none invoke MCP tools.
        # Without this, `claude -p` cold-loads the whole project .mcp.json fleet
        # on EVERY call (slow + heavy footprint). With no --mcp-config alongside
        # it, --strict-mcp-config loads ZERO MCP servers.
        "--strict-mcp-config",
    ]
    _log(f"calling via CLI ({cli}, model={model})")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
            check=False,  # tolerate non-zero exits with a valid envelope
        )
    except subprocess.TimeoutExpired as e:
        raise RouterUnavailable(
            f"claude CLI timed out after {CLI_TIMEOUT_SECONDS}s"
        ) from e

    stdout = result.stdout.strip()

    # Structural content-validity gate. SUCCESS requires a valid result envelope
    # with is_error=false — independent of exit code. No valid envelope, or an
    # envelope flagged is_error, is a failure classified into the right typed
    # exception. An unknown future banner has no envelope -> RouterUnavailable,
    # never consumed as content.
    envelope = _extract_result_envelope(stdout)

    if envelope is not None and not bool(envelope.get("is_error")):
        text = envelope.get("result")
        text = "" if text is None else str(text)
        if not text.strip():
            # is_error:false but NO content (null / "" / whitespace / missing
            # result key). A real model response is never empty here; returning
            # '' as success would let call_claude_json raise a confusing
            # ValueError that is neither RateLimitExhausted nor
            # RouterUnavailable. Fail loud instead — an empty result IS the
            # degenerate error the content-validity gate must reject.
            _log("CLI returned is_error:false with an empty result — failing")
            raise RouterUnavailable(
                "claude CLI returned a success envelope with an empty result "
                "(no content). Treating as unavailable rather than returning ''."
            )
        return text.strip()

    if envelope is not None:
        # Structured error envelope — classify from its own fields.
        err_text = " ".join(
            str(envelope.get(k, ""))
            for k in ("subtype", "result", "api_error_status",
                      "stop_reason", "terminal_reason")
        )
        _log(f"CLI returned error envelope (is_error): {err_text[:160]!r}")
        raise _classify_cli_failure(err_text, raw=stdout)

    if stdout:
        # Non-JSON stdout in --output-format json mode = failure by
        # construction (an error banner, or output corruption). Classify the
        # banner; an unrecognized one defaults to RouterUnavailable.
        _log(f"CLI returned non-envelope stdout ({len(stdout)} chars); failing")
        raise _classify_cli_failure(stdout, raw=stdout)

    raise RouterUnavailable(
        f"claude CLI exit {result.returncode} with empty stdout / no JSON "
        f"envelope. stderr: {result.stderr[:300]}"
    )


def _call_via_api(
    api_key: str,
    system: str,
    user: str,
    model: str,
    max_tokens: int,
    cache_system: bool = True,
) -> str:
    """POST to api.anthropic.com Messages API. Returns the first text block.

    When `cache_system=True` (default), wraps the system prompt in a single
    `cache_control=ephemeral` block so the Anthropic prompt cache is reused
    across calls with the same prefix. The API silently no-ops when the
    prefix is below the model's cache minimum (1K tokens for Sonnet 4.5 /
    Opus 4.1, 4K for Opus 4.7 / Sonnet 4.6 / Haiku 4.5), so opt-in is
    safe at any size.

    Logs `cache_read_input_tokens` / `cache_creation_input_tokens` from the
    response so logs can confirm caching fires on repeated calls.
    """
    system_payload: object
    if cache_system:
        system_payload = [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }]
    else:
        system_payload = system
    payload = json.dumps(
        {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_payload,
            "messages": [{"role": "user", "content": user}],
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    _log(f"calling via API key (model={model}, cache_system={cache_system})")
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RouterUnavailable(
            f"Anthropic API error {e.code}: {e.read().decode()[:300]}"
        ) from e
    except urllib.error.URLError as e:
        raise RouterUnavailable(f"Anthropic network error: {e}") from e
    # Cache metrics. Non-zero read means a hit (0.1x cost for those tokens).
    usage = body.get("usage", {}) or {}
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cache_write = usage.get("cache_creation_input_tokens", 0) or 0
    if cache_read or cache_write:
        _log(f"cache: read={cache_read} tok, write={cache_write} tok")
    blocks = body.get("content", [])
    if not blocks:
        return ""
    return blocks[0].get("text", "")


def call_claude_text(
    system: str,
    user: str,
    model: str = "haiku",
    max_tokens: int = 4096,
    cache_system: bool = True,
) -> str:
    """Call Claude and return the response text.

    Routing order: CLI (Max plan) → API key. Raises `RouterUnavailable`
    if neither is available.

    Model aliases: "haiku", "sonnet", "opus", or any full model ID
    (e.g., "claude-haiku-4-5-20251001"). The CLI accepts both forms.

    `cache_system` controls Anthropic prompt caching on the API path. Default
    True. The CLI path caches automatically — Claude Code handles it
    transparently and the flag is ignored there. Set False only when the
    caller varies the system prompt per call (benchmarks, evals, sweeps).
    """
    prefer_api = os.environ.get("CLAUDE_ROUTER_PREFER_API_KEY") == "1"

    cli = None if prefer_api else _resolve_cli()
    last_ratelimit: RateLimitExhausted | None = None
    if cli is not None:
        # One retry: an unattended `claude -p` (cron, launchd, agent) can
        # intermittently hang or return a stale-OAuth refusal on the first
        # try and clear on the second. Cheap insurance for unattended
        # callers; the happy path (first attempt succeeds) is unchanged.
        cli_attempts = 2
        for cli_attempt in range(1, cli_attempts + 1):
            try:
                return _call_via_cli(cli, system, user, model)
            except RateLimitExhausted as e:
                # Retrying the SAME account inside its limit window is futile.
                # Skip straight to the API-key tier, and remember the signal so
                # a caller with no API key gets RateLimitExhausted (transient,
                # retry after reset) rather than a misleading "no CLI" error.
                # MUST precede the RouterUnavailable handler (it is a subclass).
                last_ratelimit = e
                _log(f"CLI rate-limited; not retrying, trying API fallback: {e}")
                break
            except RouterUnavailable as e:
                if cli_attempt < cli_attempts:
                    _log(f"CLI attempt {cli_attempt}/{cli_attempts} failed "
                         f"({e}); retrying")
                else:
                    _log(f"CLI failed after {cli_attempts} attempts, "
                         f"falling back to API: {e}")

    api_key = _resolve_api_key()
    if api_key is not None:
        return _call_via_api(api_key, system, user, model, max_tokens, cache_system)

    if last_ratelimit is not None:
        # Every CLI path was rate-limited and no API key is configured. Surface
        # the transient signal so a scheduled caller can back off and retry
        # after the window resets, not treat this as a hard config failure.
        raise last_ratelimit

    raise RouterUnavailable(
        "no claude CLI and no ANTHROPIC_API_KEY — install `claude` "
        "(`npm i -g @anthropic-ai/claude-code`) or export ANTHROPIC_API_KEY."
    )


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*)\n```\s*$", re.DOTALL)


def call_claude_json(
    system: str,
    user: str,
    model: str = "haiku",
    max_tokens: int = 4096,
    cache_system: bool = True,
) -> dict | list:
    """Call Claude and parse the response as JSON.

    Strips markdown code fences if the model adds them. Raises
    `RouterUnavailable` if no transport is available, or `ValueError`
    if the response is not valid JSON.

    See `call_claude_text` for `cache_system` semantics (default True).
    """
    text = call_claude_text(system, user, model, max_tokens, cache_system)
    stripped = text.strip()
    fence_match = _FENCE_RE.match(stripped)
    if fence_match:
        stripped = fence_match.group(1).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Claude returned non-JSON ({len(text)} chars): "
            f"{text[:300]!r}"
        ) from e
