#!/usr/bin/env python3
"""Negative control for the vault-context trigger list.

THE DEFECT. `STRATEGIC_SIGNALS` was a hardcoded English list, and roughly a
third of it was one person's vocabulary (`accenture`, `substack`, `nyc`,
`high-rise`, `after the shock`) beside a TOPIC_MAP pointing at
`🚀 team-vault/…` paths that exist in exactly one vault on earth.

On a vault kept in Spanish the hook resolved the vault correctly, matched zero
signals, and exited 0 — indistinguishable from "no strategic question was
asked". Measured on a real install: *"qué prioridades tengo esta semana y cómo
va la estrategia"* → 0 matches; the same sentence in English → full injection.
That is the same silent no-op the vault-resolution fix (MYC-3529) was written
to kill, reached by a different road, which is why the guard here is a NEGATIVE
CONTROL and not a smoke test: it hands the hook the exact input it claims to
catch and asserts it fires.

The last case is the CLASS guard. Fixing today's list does nothing if personal
vocabulary can be re-added to a shipped pack tomorrow, so the shipped packs are
asserted to contain none of it. Personal terms belong in the per-user override
(`~/.claude/.vault-context-signals.json`), which is per-user by construction.

Run: python3 hooks/test_vault_context_signals.py
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

HOOK = Path(__file__).resolve().parent / "vault-context.py"
REPO = HOOK.parent.parent
PACK_DIR = REPO / "templates" / "vault-context"

failures = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}{(' — ' + detail) if detail else ''}")
        failures.append(label)


def load_module():
    spec = importlib.util.spec_from_file_location("vault_context", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fires(signals, prompt: str) -> bool:
    p = prompt.lower().strip()
    return any(re.search(sig, p) for sig in signals)


vc = load_module()
signals, topic_map = vc.load_signals(["en", "es"], user_file="")

# 1. THE REGRESSION ITSELF — the sentence that measured zero matches.
check(
    "es: the sentence that silently matched nothing now fires",
    fires(signals, "qué prioridades tengo esta semana y cómo va la estrategia"),
)

# 2. Spanish coverage beyond the one reported sentence, accents optional.
for prompt in [
    "cuál es mi plan de negocio para el cliente nuevo",
    "en qué estoy trabajando ahora",
    "que decision tengo pendiente",  # no accents, as typed in a terminal
    "cómo va la cartera y el flujo de caja",
    "por dónde arranco hoy",
    "qué sigue con el proyecto",
]:
    check(f"es fires: {prompt!r}", fires(signals, prompt))

# 3. No English regression.
for prompt in [
    "what should i prioritize this week",
    "how is the client revenue looking",
    "what's my plan for the raise",
]:
    check(f"en fires: {prompt!r}", fires(signals, prompt))

# 4. Still quiet on the ordinary. A trigger list that fires on everything
#    injects the vault into every prompt and is its own kind of broken.
for prompt in [
    "arregla el bug del parser de fechas",
    "what's the syntax for a bash for loop",
    "traduce este párrafo al inglés",
    "git rebase -i no me deja continuar",
]:
    check(f"quiet on: {prompt!r}", not fires(signals, prompt))

# 5. No packs on disk (hook copied without templates/) → BUILTIN floor, never
#    an empty list. An empty list is the silent no-op wearing a new hat.
original_find = vc.find_repo_root
with tempfile.TemporaryDirectory() as tmp:
    vc.find_repo_root = lambda: Path(tmp)
    floor, _ = vc.load_signals(["en", "es"], user_file="")
    vc.find_repo_root = original_find
check("no packs on disk falls back to the builtin floor", floor == vc.BUILTIN_STRATEGIC_SIGNALS)
check("builtin floor still fires on plain english", fires(floor, "what should i prioritize"))

# 5b. THE DEPLOYED COPY. This hook is installed to ~/.claude/hooks/, where the
#     walk-up finds no templates/ — the packs must still resolve through the
#     canonical clone, or Spanish ships to everyone and loads for no one.
with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)
    deployed_dir = home / ".claude" / "hooks"
    deployed_dir.mkdir(parents=True)
    deployed = deployed_dir / "vault-context.py"
    deployed.write_text(HOOK.read_text(encoding="utf-8"), encoding="utf-8")
    (deployed_dir / "_lib").mkdir()
    for lib in (HOOK.parent / "_lib").glob("*.py"):
        (deployed_dir / "_lib" / lib.name).write_text(lib.read_text(encoding="utf-8"), encoding="utf-8")
    clone_packs = home / ".claude" / "skills" / "ai-brain-starter" / "templates" / "vault-context"
    clone_packs.mkdir(parents=True)
    for pack in PACK_DIR.glob("*.json"):
        (clone_packs / pack.name).write_text(pack.read_text(encoding="utf-8"), encoding="utf-8")

    vault = home / "Brain"
    (vault / "⚙️ Meta").mkdir(parents=True)
    (vault / "⚙️ Meta" / "Current Priorities.md").write_text("- cerrar la P.O.\n", encoding="utf-8")
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
               VAULT_CONTEXT_SIGNALS_FILE=str(home / "none.json"))
    dep = subprocess.run(
        [sys.executable, str(deployed)],
        input=json.dumps({"cwd": str(vault), "prompt": "qué prioridades tengo hoy"}),
        capture_output=True, text=True, env=env,
    )
check("deployed copy resolves packs through the canonical clone",
      "cerrar la P.O." in dep.stdout, f"stdout={dep.stdout[:200]!r} stderr={dep.stderr[:200]!r}")

# 6. Per-user override contributes signals AND a topic map.
with tempfile.TemporaryDirectory() as tmp:
    override = Path(tmp) / "signals.json"
    override.write_text(json.dumps({
        "strategic_signals": [r"\bwiqqu\b", r"\bcltiene\b"],
        "topic_map": [{"signals": [r"\bwiqqu\b"], "files": ["Negocios/WIQQU.md"]}],
    }), encoding="utf-8")
    user_signals, user_topics = vc.load_signals(["en", "es"], user_file=str(override))
check("override adds personal vocabulary", fires(user_signals, "cómo va wiqqu"))
check("override adds a topic map", user_topics and user_topics[0][1] == ["Negocios/WIQQU.md"])

# 7. A hand-edited override with a broken pattern must not take the hook down.
with tempfile.TemporaryDirectory() as tmp:
    bad = Path(tmp) / "bad.json"
    bad.write_text(json.dumps({"strategic_signals": [r"\bfine\b", r"[unclosed"]}), encoding="utf-8")
    try:
        salvaged, _ = vc.load_signals(["en", "es"], user_file=str(bad))
        ok = fires(salvaged, "this is fine") and not any("[unclosed" == s for s in salvaged)
    except re.error:
        ok = False
check("uncompilable override pattern is dropped, not raised", ok)

# 8. A malformed / missing override file is a no-op, not a crash.
with tempfile.TemporaryDirectory() as tmp:
    junk = Path(tmp) / "junk.json"
    junk.write_text("{not json", encoding="utf-8")
    try:
        vc.load_signals(["en", "es"], user_file=str(junk))
        vc.load_signals(["en", "es"], user_file=str(Path(tmp) / "absent.json"))
        ok = True
    except Exception as e:  # noqa: BLE001 - any exception here is the failure
        ok = False
        print(f"      raised: {e}")
check("malformed and missing override files fail open", ok)

# 9. END TO END: the hook actually injects, through stdin/stdout, on Spanish.
with tempfile.TemporaryDirectory() as tmp:
    vault = Path(tmp) / "Brain"
    meta = vault / "⚙️ Meta"
    meta.mkdir(parents=True)
    (meta / "Current Priorities.md").write_text("- cerrar la P.O. de ZTE\n", encoding="utf-8")
    env = dict(os.environ, VAULT_CONTEXT_SIGNALS_FILE=str(Path(tmp) / "none.json"))
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"cwd": str(vault), "prompt": "qué prioridades tengo hoy"}),
        capture_output=True, text=True, env=env,
    )
    injected = ""
    if proc.stdout.strip():
        try:
            injected = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        except (ValueError, KeyError):
            injected = ""
check("end to end: spanish prompt injects Current Priorities", "ZTE" in injected,
      f"stdout={proc.stdout[:200]!r} stderr={proc.stderr[:200]!r}")

# 10. CLASS GUARD — no personal vocabulary in a pack every install loads.
PERSONAL = ["accenture", "substack", "nyc", "high-rise", "high.rise",
            "after the shock", "onde", "team-vault", "venue tool"]
for pack in sorted(PACK_DIR.glob("*.json")):
    raw = pack.read_text(encoding="utf-8").lower()
    hits = [term for term in PERSONAL if term in raw]
    check(f"{pack.name} ships no personal vocabulary", not hits, f"found {hits}")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("ALL PASS: vault-context fires in Spanish, stays quiet on ordinary prompts, "
      "and ships no personal vocabulary.")
