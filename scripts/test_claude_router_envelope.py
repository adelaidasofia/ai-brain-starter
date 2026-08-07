#!/usr/bin/env python3
"""Negative-control suite for the router's structured content-validity gate.

The router used to decide CLI success by sniffing raw `claude -p` text for a
denylist of bad banners (login / connection / weekly-limit / session-limit).
That denylist was patched repeatedly and was always one banner phrasing behind:
an unrecognized banner was CONSUMED as a successful response (the
PRODUCER-OUTPUT-CONSUMED-WITHOUT-CONTENT-VALIDITY-CHECK bug class).

The fix runs `claude -p --output-format json` and makes success PURELY
STRUCTURAL: a valid result envelope whose is_error is false. These tests mock
subprocess.run to feed crafted stdout into the REAL _call_via_cli and prove:

  * a valid success envelope is returned                 (positive path)
  * an is_error envelope raises (classified)             (structured error)
  * a KNOWN banner (non-JSON) raises RateLimitExhausted  (old denylist case)
  * an UNKNOWN banner (non-JSON) raises RouterUnavailable — NOT consumed.
        ^^^ THE bug-class negative control: the old denylist would miss this
            banner and return it as a "successful" response. The structural
            gate rejects it because it is not a valid envelope. Revert the gate
            and this test goes red — that is the proof the fix is load-bearing.
  * a SUCCESS envelope whose result TEXT contains "rate limit" is returned,
        not misclassified (the old denylist false-positive, now fixed)
  * a non-zero exit with a valid envelope is still accepted
        (SessionEnd-hook-failed-post-response case preserved)
  * empty stdout raises RouterUnavailable

Run: python3 scripts/test_claude_router_envelope.py
Run under pytest: pytest scripts/test_claude_router_envelope.py
Exit 0 = pass. Vanilla unittest, no third-party deps.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _claude_router as R  # noqa: E402
from _claude_router import RateLimitExhausted, RouterUnavailable  # noqa: E402


def _envelope(result="PONG", is_error=False, subtype="success", **extra):
    d = {"type": "result", "subtype": subtype, "is_error": is_error,
         "result": result}
    d.update(extra)
    return json.dumps(d)


class _Res:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class EnvelopeGate(unittest.TestCase):
    def setUp(self):
        self._orig_run = R.subprocess.run

    def tearDown(self):
        R.subprocess.run = self._orig_run

    def _run_cli(self, stdout, returncode=0, stderr=""):
        R.subprocess.run = lambda cmd, **kw: _Res(stdout, returncode, stderr)
        return R._call_via_cli("/fake/claude", "sys", "usr", "haiku")

    # --- positive path -------------------------------------------------------
    def test_valid_success_envelope_returns_result(self):
        self.assertEqual(self._run_cli(_envelope("hello world")), "hello world")

    def test_non_zero_exit_with_valid_envelope_still_accepted(self):
        # SessionEnd hook failed post-response: exit 1 but envelope is valid.
        self.assertEqual(
            self._run_cli(_envelope("body"), returncode=1), "body")

    def test_envelope_with_leading_hook_noise_is_tolerated(self):
        noisy = '{"hookSpecificOutput":{"x":1}}\n' + _envelope("clean")
        self.assertEqual(self._run_cli(noisy), "clean")

    # --- structured error envelope ------------------------------------------
    def test_is_error_envelope_with_ratelimit_text_raises_ratelimit(self):
        env = _envelope("You've hit your session limit", is_error=True,
                        subtype="error_during_execution")
        with self.assertRaises(RateLimitExhausted):
            self._run_cli(env)

    def test_is_error_envelope_generic_raises_unavailable(self):
        env = _envelope("internal error", is_error=True,
                        subtype="error_during_execution")
        with self.assertRaises(RouterUnavailable) as ctx:
            self._run_cli(env)
        self.assertNotIsInstance(ctx.exception, RateLimitExhausted)

    # --- non-JSON banners (the old denylist surface) ------------------------
    def test_known_banner_non_json_raises_ratelimit(self):
        # The 5-hour session-limit banner, but as raw (non-envelope) stdout.
        with self.assertRaises(RateLimitExhausted):
            self._run_cli("You've hit your session limit · resets 8am", returncode=1)

    def test_UNKNOWN_banner_non_json_is_rejected_not_consumed(self):
        # THE bug-class negative control. This banner matches NO marker. The old
        # denylist would have returned it as a ~60-char "successful" response.
        # The structural gate rejects it (no valid envelope) -> RouterUnavailable.
        banner = "Heads up: the flux capacitor needs 1.21 gigawatts to continue"
        with self.assertRaises(RouterUnavailable) as ctx:
            self._run_cli(banner, returncode=1)
        self.assertNotIsInstance(ctx.exception, RateLimitExhausted)

    def test_exit0_plaintext_is_rejected_not_consumed(self):
        # INTENTIONAL hard-dependency on --output-format json. A CLI that
        # IGNORES the flag and emits a valid plain-text answer on exit 0 (older
        # claude, or a wrapper that strips the flag) has NO envelope, so it is
        # treated as UNAVAILABLE — NOT consumed as content. call_claude_text
        # catches RouterUnavailable and falls through to the API-key tier
        # (fail-safe, never a wrong answer).
        with self.assertRaises(RouterUnavailable) as ctx:
            self._run_cli("The capital of France is Paris.", returncode=0)
        self.assertNotIsInstance(ctx.exception, RateLimitExhausted)

    # --- empty/null result on a "success" envelope --------------------------
    def test_is_error_false_but_null_result_raises_unavailable(self):
        with self.assertRaises(RouterUnavailable) as ctx:
            self._run_cli(_envelope(result=None))
        self.assertNotIsInstance(ctx.exception, RateLimitExhausted)

    def test_is_error_false_but_empty_result_raises_unavailable(self):
        with self.assertRaises(RouterUnavailable):
            self._run_cli(_envelope(result=""))

    def test_is_error_false_but_whitespace_result_raises_unavailable(self):
        with self.assertRaises(RouterUnavailable):
            self._run_cli(_envelope(result="   \n  "))

    def test_call_claude_json_double_parses_envelope_result(self):
        # Full path: subprocess -> envelope whose `result` is a JSON STRING ->
        # _call_via_cli extracts it -> call_claude_json parses it.
        orig = (R._resolve_cli, R._resolve_api_key)
        R.subprocess.run = lambda cmd, **kw: _Res(_envelope('{"ok": true, "n": 3}'))
        R._resolve_cli = lambda: "/fake/claude"
        R._resolve_api_key = lambda: None
        try:
            self.assertEqual(
                R.call_claude_json(system="s", user="u", model="haiku"),
                {"ok": True, "n": 3})
        finally:
            R._resolve_cli, R._resolve_api_key = orig

    # --- false-positive the old denylist had --------------------------------
    def test_success_result_mentioning_rate_limit_is_returned(self):
        # A legit response that explains rate limits must NOT be misclassified.
        text = "To avoid a rate limit, batch your requests and back off on 429."
        self.assertEqual(self._run_cli(_envelope(text)), text)

    # --- empty / malformed ---------------------------------------------------
    def test_empty_stdout_raises_unavailable(self):
        with self.assertRaises(RouterUnavailable):
            self._run_cli("", returncode=1)

    # --- footprint guard: pure-generation calls load ZERO MCP servers --------
    def test_cmd_includes_strict_mcp_config_and_json_format(self):
        # Router calls never invoke MCP tools; --strict-mcp-config (with no
        # --mcp-config) prevents cold-loading the whole .mcp.json fleet on every
        # call. Pin it so the footprint fix cannot silently regress.
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return _Res(_envelope("ok"))

        R.subprocess.run = fake_run
        R._call_via_cli("/fake/claude", "sys", "usr", "haiku")
        self.assertIn("--strict-mcp-config", captured["cmd"])
        self.assertIn("--output-format", captured["cmd"])
        self.assertIn("json", captured["cmd"])


class CallClaudeTextRouting(unittest.TestCase):
    """The CLI->API fallback loop: a rate-limit is NOT retried on the same
    account (futile in-window) and its transient signal survives to the caller."""

    def setUp(self):
        self._orig = (R._resolve_cli, R._resolve_api_key,
                      R._call_via_cli, R._call_via_api)

    def tearDown(self):
        (R._resolve_cli, R._resolve_api_key,
         R._call_via_cli, R._call_via_api) = self._orig

    def test_ratelimit_no_apikey_raises_ratelimit_and_skips_retry(self):
        calls = {"cli": 0}

        def cli_raises(*a, **k):
            calls["cli"] += 1
            raise RateLimitExhausted("weekly limit")

        R._resolve_cli = lambda: "/fake/claude"
        R._resolve_api_key = lambda: None
        R._call_via_cli = cli_raises
        with self.assertRaises(RateLimitExhausted):
            R.call_claude_text(system="s", user="u", model="haiku")
        self.assertEqual(calls["cli"], 1)  # rate-limit NOT retried

    def test_ratelimit_with_apikey_falls_to_api_without_retry(self):
        calls = {"cli": 0, "api": 0}

        def cli_raises(*a, **k):
            calls["cli"] += 1
            raise RateLimitExhausted("session limit")

        def api_ok(*a, **k):
            calls["api"] += 1
            return "from-api"

        R._resolve_cli = lambda: "/fake/claude"
        R._resolve_api_key = lambda: "sk-test"
        R._call_via_cli = cli_raises
        R._call_via_api = api_ok
        self.assertEqual(
            R.call_claude_text(system="s", user="u", model="haiku"), "from-api")
        self.assertEqual(calls["cli"], 1)  # rate-limit not retried
        self.assertEqual(calls["api"], 1)

    def test_generic_unavailable_still_retries_then_falls_to_api(self):
        # A non-rate-limit failure keeps the cheap one-retry insurance.
        calls = {"cli": 0}

        def cli_raises(*a, **k):
            calls["cli"] += 1
            raise RouterUnavailable("network blip")

        R._resolve_cli = lambda: "/fake/claude"
        R._resolve_api_key = lambda: "sk-test"
        R._call_via_cli = cli_raises
        R._call_via_api = lambda *a, **k: "from-api"
        self.assertEqual(
            R.call_claude_text(system="s", user="u", model="haiku"), "from-api")
        self.assertEqual(calls["cli"], 2)  # retried once, THEN API


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
