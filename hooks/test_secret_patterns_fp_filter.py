#!/usr/bin/env python3
"""Regression tests for the 2026-07-14 alert-layer FP-filter carve-out.

A database-migration verification run fired 16 false positives from the
PostToolUse secrets hook in one session: 1x the bare 64-hex container ID
echoed on its own stdout line by `docker run -d ... postgres:16` (the
`Digest: sha256:<hex>` line right above it was ALREADY excluded by the
existing `(?<!sha256:)` lookbehind in hex-256bit-secret), and 15x drizzle
migration content-hashes printed as psql rows shaped
`  1 | <64-hex> | 1782448308040` by
`psql ... -c "SELECT id, hash, created_at FROM drizzle.__drizzle_migrations
ORDER BY id;"`.

The fix is ALERT-LAYER ONLY: `filter_tool_output_false_positives()` in
`_lib/secret_patterns.py` reclassifies individual hex-256bit-secret hits as
content-addressed IDs when the *command* context proves it (a docker
lifecycle verb + detached run, or a __drizzle_migrations query) AND the
match sits on a line shaped like that ID echo. It is wired ONLY into
`detect-secrets-in-bash-output.py` (the integration tests below hit that
hook directly). `scan()` / `redact()` — and every other consumer: the
SessionEnd scrub, the launchd corpus scan, the SessionStart surfacer — are
untouched and stay fully aggressive. A suppressed hit is still written to
the audit log with a reason, same discipline as the existing
`_is_self_secret_inspection` suppression.

Run: python3 hooks/test_secret_patterns_fp_filter.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS))

from _lib.secret_patterns import filter_tool_output_false_positives, redact, scan  # noqa: E402


def _load(name: str, filename: str):
    """Load a hyphenated-filename hook module by path (not import-able normally)."""
    spec = importlib.util.spec_from_file_location(name, HOOKS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load("detect_secrets_hook", "detect-secrets-in-bash-output.py")

_DRIZZLE_CMD = (
    'psql "postgresql://postgres:localtest@localhost:5544/appdb" -c '
    '"SELECT id, hash, created_at FROM drizzle.__drizzle_migrations ORDER BY id;"'
)


def _run_hook(payload: dict, log_path: Path):
    """Feed `payload` to the hook's main() via stdin, with LOG_PATH pointed at
    `log_path`. Returns (exit_code, stdout_json, stderr_text)."""
    hook.LOG_PATH = log_path
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    out_buf, err_buf = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            rc = hook.main()
    finally:
        sys.stdin = old_stdin
    stdout_text = out_buf.getvalue().strip()
    stdout_json = json.loads(stdout_text.splitlines()[-1]) if stdout_text else {}
    return rc, stdout_json, err_buf.getvalue()


# ── 1. the incident, reproduced verbatim ────────────────────────────────────

def test_incident_docker_run_detached_id_echo():
    """1x container ID echoed bare by `docker run -d`; the sha256: digest
    line right above it is already excluded by the existing regex (proving
    scan() itself needs no change)."""
    command = (
        'docker run -d --name test-pg -p 5544:5432 '
        '-e POSTGRES_PASSWORD=localtest postgres:16 2>&1; echo "RUN_EXIT:$?"'
    )
    hex_digest = "8badf00d" * 8
    hex_echo = "deadc0de" * 8
    output = (
        "Unable to find image 'postgres:16' locally\n"
        "16: Pulling from library/postgres\n"
        f"Digest: sha256:{hex_digest}\n"
        "Status: Downloaded newer image for postgres:16\n"
        f"{hex_echo}\n"
        "RUN_EXIT:0\n"
    )
    hits = scan(output)
    assert dict(hits).get("hex-256bit-secret") == 1, (
        f"expected exactly 1 hex-256bit-secret hit (digest line already "
        f"excluded by the sha256: lookbehind), got {hits}"
    )
    remaining, suppressed = filter_tool_output_false_positives(hits, output, command)
    assert remaining == [], f"expected all suppressed, got remaining={remaining}"
    assert suppressed == [("hex-256bit-secret", 1, "docker-cli-id-echo")], suppressed


def test_incident_drizzle_15_rows():
    """15x drizzle migration-hash psql rows, all suppressed."""
    command = _DRIZZLE_CMD
    lines = [" id |  hash  | created_at", "----+--------+------------"]
    for i in range(1, 16):
        hex_i = f"{i:064x}"
        lines.append(f"  {i} | {hex_i} | 1782448308{i:03d}")
    lines.append("(15 rows)")
    output = "\n".join(lines) + "\n"
    hits = scan(output)
    assert dict(hits).get("hex-256bit-secret") == 15, hits
    remaining, suppressed = filter_tool_output_false_positives(hits, output, command)
    assert remaining == [], f"expected all suppressed, got remaining={remaining}"
    assert suppressed == [("hex-256bit-secret", 15, "drizzle-migration-hash")], suppressed


# ── 2. negative controls (must still fire) ──────────────────────────────────

def test_negative_export_secret_plain_still_fires():
    command = "./deploy.sh"
    hexv = "cafebabe" * 8
    output = f"export SECRET={hexv}"
    hits = scan(output)
    remaining, suppressed = filter_tool_output_false_positives(hits, output, command)
    assert remaining == hits, f"expected unchanged, got {remaining}"
    assert suppressed == []


def test_negative_export_secret_inside_drizzle_output_still_fires():
    """The __drizzle_migrations command marker does NOT blanket-suppress —
    only lines actually shaped like a migration row get reclassified."""
    command = _DRIZZLE_CMD
    hex_1 = f"{1:064x}"
    hex_2 = f"{2:064x}"
    hex_z = "deadbeef" * 8
    output = (
        f"  1 | {hex_1} | 1782448308040\n"
        f"  2 | {hex_2} | 1782448308041\n"
        f"export SECRET={hex_z}\n"
    )
    hits = scan(output)
    assert dict(hits).get("hex-256bit-secret") == 3, hits
    remaining, suppressed = filter_tool_output_false_positives(hits, output, command)
    assert remaining == [("hex-256bit-secret", 1)], f"expected exactly 1 remaining, got {remaining}"
    assert suppressed == [("hex-256bit-secret", 2, "drizzle-migration-hash")], suppressed


def test_negative_bare_hex_without_context_still_fires():
    """openssl output IS a real secret — no docker/drizzle context at all."""
    command = "openssl rand -hex 32"
    hexv = "deadbeef" * 8
    hits = scan(hexv)
    remaining, suppressed = filter_tool_output_false_positives(hits, hexv, command)
    assert remaining == hits, f"expected unchanged, got {remaining}"
    assert suppressed == []


def test_negative_docker_exec_not_id_echo():
    """`docker exec` is deliberately absent from the allowlist — its stdout
    is arbitrary program output that can contain a real secret."""
    command = "docker exec app printenv SECRET_KEY"
    hexv = "deadbeef" * 8
    hits = scan(hexv)
    remaining, suppressed = filter_tool_output_false_positives(hits, hexv, command)
    assert remaining == hits, f"expected unchanged, got {remaining}"
    assert suppressed == []


def test_negative_docker_run_foreground_not_id_echo():
    """`docker run` without -d/--detach is foreground — its stdout is
    arbitrary program output, not an ID echo."""
    command = "docker run --rm img generate-key"
    hexv = "deadbeef" * 8
    hits = scan(hexv)
    remaining, suppressed = filter_tool_output_false_positives(hits, hexv, command)
    assert remaining == hits, f"expected unchanged, got {remaining}"
    assert suppressed == []


# ── 3. docker allowlist positive coverage ───────────────────────────────────

def test_docker_stop_rm_id_echo_suppressed():
    command = "docker stop 5e2f && docker rm 5e2f"
    hexv = "deadbeef" * 8
    hits = scan(hexv)
    remaining, suppressed = filter_tool_output_false_positives(hits, hexv, command)
    assert remaining == []
    assert suppressed == [("hex-256bit-secret", 1, "docker-cli-id-echo")], suppressed


# ── 4. drizzle shape coverage ────────────────────────────────────────────────

def test_drizzle_expanded_display_and_bare_column():
    command = _DRIZZLE_CMD
    hexv = "deadbeef" * 8
    for output in (f"hash | {hexv}", hexv):
        hits = scan(output)
        remaining, suppressed = filter_tool_output_false_positives(hits, output, command)
        assert remaining == [], f"expected suppressed for {output!r}, got {remaining}"
        assert suppressed == [("hex-256bit-secret", 1, "drizzle-migration-hash")], (output, suppressed)


# ── 5. other patterns pass through untouched ────────────────────────────────

def test_other_patterns_pass_through_untouched():
    command = "docker run -d --name x postgres:16"
    hexv = "deadbeef" * 8
    hits = [("postgres-url-password", 1), ("hex-256bit-secret", 1)]
    remaining, suppressed = filter_tool_output_false_positives(hits, hexv, command)
    assert remaining == [("postgres-url-password", 1)], remaining
    assert suppressed == [("hex-256bit-secret", 1, "docker-cli-id-echo")], suppressed


# ── 6. scrub layer proof (the filter did not weaken scan/redact) ───────────

def test_scrub_layer_stays_aggressive():
    hexv = "deadbeef" * 8
    text = f"  1 | {hexv} | 1782448308040"
    redacted, hit_names = redact(text)
    assert "hex-256bit-secret" in hit_names, hit_names
    assert "[REDACTED-hex-256bit]" in redacted, redacted
    assert hexv not in redacted, "raw hex survived redact()"


# ── 7. empty command ─────────────────────────────────────────────────────────

def test_empty_command_no_filtering():
    hexv = "deadbeef" * 8
    hits = [("hex-256bit-secret", 1)]
    remaining, suppressed = filter_tool_output_false_positives(hits, hexv, "")
    assert remaining == hits
    assert suppressed == []


# ── 8. integration: the real hook end-to-end ────────────────────────────────

def test_hook_end_to_end_all_suppressed():
    hex_digest = "8badf00d" * 8
    hex_echo = "deadc0de" * 8
    payload = {
        "session_id": "t",
        "tool_input": {"command": "docker run -d --name x postgres:16"},
        "tool_response": {"stdout": f"Digest: sha256:{hex_digest}\n{hex_echo}\n"},
    }
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "secret-detection-log.jsonl"
        rc, stdout_json, _stderr = _run_hook(payload, log_path)
        assert rc == 0
        assert stdout_json.get("suppressOutput") is True, stdout_json
        assert "hookSpecificOutput" not in stdout_json, stdout_json

        entries = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert len(entries) == 1, entries
        entry = entries[0]
        assert entry["alert_suppressed"] is True, entry
        assert entry["hits"] == [], entry
        assert entry["fp_suppressed"] == [
            {"pattern": "hex-256bit-secret", "count": 1, "reason": "docker-cli-id-echo"}
        ], entry


def test_hook_end_to_end_mixed_still_alerts():
    hex_1 = f"{1:064x}"
    hex_2 = f"{2:064x}"
    hex_z = "cafebabe" * 8
    payload = {
        "session_id": "t",
        "tool_input": {"command": _DRIZZLE_CMD},
        "tool_response": {
            "stdout": (
                f"  1 | {hex_1} | 1782448308040\n"
                f"  2 | {hex_2} | 1782448308041\n"
                f"export SECRET={hex_z}\n"
            )
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "secret-detection-log.jsonl"
        rc, stdout_json, _stderr = _run_hook(payload, log_path)
        assert rc == 0
        ctx = stdout_json.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "SECRETS DETECTED" in ctx, stdout_json
        assert "hex-256bit-secret×1" in ctx, stdout_json

        entries = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert len(entries) == 1, entries
        entry = entries[0]
        assert entry["hits"] == [{"pattern": "hex-256bit-secret", "count": 1}], entry
        assert entry["fp_suppressed"] == [
            {"pattern": "hex-256bit-secret", "count": 2, "reason": "drizzle-migration-hash"}
        ], entry
        assert entry["alert_suppressed"] is False, entry


def main() -> None:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  [OK] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001 — surface any error as a failure
            failed += 1
            print(f"  [ERROR] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
