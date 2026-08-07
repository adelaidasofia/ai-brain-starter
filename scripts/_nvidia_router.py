"""Thin Python router for NVIDIA build (integrate.api.nvidia.com).

Mirrors the API surface of a Claude router so scripts can swap a single
import + call when the workload is grunt-work (classification / extraction /
format conversion / structured-output regex-class).

NEVER use this for: judgment, voice-sensitive prose, agentic tool-use loops,
or anything that requires Claude-quality reasoning. This is a Tier-2 helper —
the cost-vs-quality trade is worth it only for mechanical text transforms.

API:
  call_nvidia_text(system, user, model="llama", max_tokens=1000) -> str
  call_nvidia_json(system, user, model="llama", max_tokens=1000) -> dict | list

Model aliases (verified live 2026-05-10):
  llama (default) | llama4 | qwen3 | qwen3-coder | deepseek | deepseek-pro
  | nemotron | nemotron-super

Reads NVIDIA_API_KEY via canonical fallback chain (env -> .zshenv ->
.zsh_secrets -> .zshrc -> .zprofile -> .bashrc).

Raises NvidiaUnavailable if the key is missing or the endpoint errors.
"""

from __future__ import annotations

import sys
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

__all__ = ["call_nvidia_text", "call_nvidia_json", "NvidiaUnavailable", "MODELS"]


class NvidiaUnavailable(RuntimeError):
    pass


MODELS = {
    "llama": "meta/llama-3.3-70b-instruct",
    "llama3": "meta/llama-3.3-70b-instruct",
    "llama-3.3": "meta/llama-3.3-70b-instruct",
    "llama4": "meta/llama-4-maverick-17b-128e-instruct",
    "llama4-maverick": "meta/llama-4-maverick-17b-128e-instruct",
    "qwen3": "qwen/qwen3-next-80b-a3b-instruct",
    "qwen": "qwen/qwen3-next-80b-a3b-instruct",
    "qwen3-thinking": "qwen/qwen3-next-80b-a3b-thinking",
    "qwen3-coder": "qwen/qwen3-coder-480b-a35b-instruct",
    "deepseek": "deepseek-ai/deepseek-v4-flash",
    "deepseek-flash": "deepseek-ai/deepseek-v4-flash",
    "deepseek-pro": "deepseek-ai/deepseek-v4-pro",
    "nemotron": "nvidia/llama-3.1-nemotron-nano-8b-v1",
    "nemotron-nano": "nvidia/llama-3.1-nemotron-nano-8b-v1",
    "nemotron-super": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
}

ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*)\n```\s*$", re.DOTALL)


def _scan_secret_files() -> str | None:
    candidates = [
        Path.home() / ".zshenv",
        Path.home() / ".zsh_secrets",
        Path.home() / ".zshrc",
        Path.home() / ".zprofile",
        Path.home() / ".bashrc",
        Path.home() / ".bash_profile",
        Path.home() / ".profile",
        Path.home() / ".env",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "NVIDIA_API_KEY" not in line or "=" not in line:
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].strip()
                name, _, value = line.partition("=")
                if name.strip() != "NVIDIA_API_KEY":
                    continue
                return value.strip().strip('"').strip("'")
        except OSError:
            continue
    return None


def _resolve_key() -> str | None:
    return os.environ.get("NVIDIA_API_KEY") or _scan_secret_files()


def _resolve_model(model: str) -> str:
    if "/" in model:
        return model
    if model in MODELS:
        return MODELS[model]
    raise NvidiaUnavailable(
        f"unknown NVIDIA model alias {model!r}; valid: {sorted(MODELS)}"
    )


def call_nvidia_text(
    system: str,
    user: str,
    model: str = "llama",
    max_tokens: int = 1000,
    temperature: float = 0.2,
    timeout: float = 60.0,
) -> str:
    """Single-turn chat completion. Returns assistant text only."""
    api_key = _resolve_key()
    if not api_key:
        raise NvidiaUnavailable(
            "NVIDIA_API_KEY not set (checked env + .zshenv + .zsh_secrets + "
            ".zshrc + fallback chain). Add to ~/.zsh_secrets."
        )

    payload = json.dumps({
        "model": _resolve_model(model),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "stream": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise NvidiaUnavailable(f"NVIDIA HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:400]}") from e
    except urllib.error.URLError as e:
        raise NvidiaUnavailable(f"NVIDIA network error: {e}") from e

    if "error" in body:
        err = body["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise NvidiaUnavailable(f"NVIDIA API error: {msg}")

    choices = body.get("choices") or []
    if not choices:
        raise NvidiaUnavailable(f"NVIDIA empty choices: {json.dumps(body)[:300]}")

    msg = choices[0].get("message", {}) or {}
    # Reasoning models (qwen3-thinking, some nemotrons) put output in
    # reasoning_content; fall back when content is empty.
    out = msg.get("content") or msg.get("reasoning_content") or ""
    return str(out).strip()


def call_nvidia_json(
    system: str,
    user: str,
    model: str = "llama",
    max_tokens: int = 1000,
    temperature: float = 0.2,
    timeout: float = 60.0,
) -> dict | list:
    """Single-turn chat completion that strips fences + parses JSON."""
    text = call_nvidia_text(system, user, model, max_tokens, temperature, timeout)
    stripped = text.strip()
    # Strip <think>...</think> blocks first (qwen3-thinking can mix them in
    # even when content is populated).
    stripped = re.sub(r"<think>.*?</think>\s*", "", stripped, flags=re.DOTALL).strip()
    fence_match = _FENCE_RE.match(stripped)
    if fence_match:
        stripped = fence_match.group(1).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"NVIDIA returned non-JSON ({len(text)} chars): {text[:300]!r}"
        ) from e


if __name__ == "__main__":
    # Quick smoke test
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    import sys
    try:
        result = call_nvidia_text(
            system="You are a concise classifier. Output ONLY one word.",
            user="Classify the sentiment: 'I love this product!' Output: positive, negative, or neutral.",
            max_tokens=10,
        )
        print(f"smoke OK: {result!r}")
    except (NvidiaUnavailable, ValueError) as e:
        print(f"smoke FAIL: {e}", file=sys.stderr)
        sys.exit(1)
