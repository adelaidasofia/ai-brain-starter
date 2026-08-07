#!/usr/bin/env python3
"""UserPromptSubmit hook: ONE gentle, optional email ask — and only at the
right moment.

This REPLACES the old scripts/email-gate-hook.py, which fired on every prompt
of every session and nagged forever until a marker existed. That was a stealth
reversal of docs/adr/0002-no-email-gate.md and the thing real users complained
about ("asked for my email over and over again, even while journaling").

The email is asked at exactly two moments now:
  1. First-time install  -> the setup interview, Phase 24.4 (NOT this hook).
  2. After a git pull that brings a NEW version, when there is still no email
     on file -> this hook, at most once, then a long cooldown.

This hook NEVER:
  - fires on a normal working session when nothing was pulled,
  - mentions a token or asks anyone to paste anything,
  - blocks the user's original request,
  - re-asks after the user has given an email OR declined.

Fire conditions (ALL must hold):
  1. EMAIL_GATE_BYPASS != "1".
  2. Marker ~/.claude/.ai-brain-starter-email-on-file does NOT exist. The
     marker's existence means the question is settled (token captured, or
     "recorded", or "declined") — we never ask again once it exists.
  3. The installed skill clone's git HEAD CHANGED since we last saw it — i.e.
     a `git pull` actually landed a new version. The very first run only
     records the current HEAD and stays silent (first-install is Phase 24.4's
     job, not this hook's).
  4. We have not asked within COOLDOWN_DAYS (so two updates in a short window
     never double-ask).

Fail-open everywhere: any error -> passthrough. A funnel nudge must never
break a working session.

Bypass:
  - touch ~/.claude/.ai-brain-starter-email-on-file   (settles it)
  - export EMAIL_GATE_BYPASS=1                         (skips this hook)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
MARKER = os.path.join(HOME, ".claude", ".ai-brain-starter-email-on-file")
ASK_STATE = os.path.join(HOME, ".claude", ".ai-brain-starter-email-ask-state.json")
SKILL_DIR = os.path.join(HOME, ".claude", "skills", "ai-brain-starter")
COOLDOWN_DAYS = 14
COOLDOWN_SECONDS = COOLDOWN_DAYS * 24 * 3600


def _passthrough() -> int:
    sys.stdout.write(json.dumps({"continue": True, "suppressOutput": True}))
    sys.stdout.write("\n")
    return 0


def _load_state() -> dict:
    try:
        with open(ASK_STATE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(ASK_STATE), exist_ok=True)
        with open(ASK_STATE, "w") as f:
            json.dump(state, f)
    except OSError:
        pass


def _current_head() -> str | None:
    """HEAD of the installed skill clone, or None if it can't be determined
    (no git, not a repo, git not on PATH). None -> we can't tell whether an
    update landed, so we stay silent."""
    if not os.path.isdir(os.path.join(SKILL_DIR, ".git")):
        return None
    try:
        result = subprocess.run(
            ["git", "-C", SKILL_DIR, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    return head or None


def main() -> int:
    if os.environ.get("EMAIL_GATE_BYPASS") == "1":
        return _passthrough()
    # Settled already (token / "recorded" / "declined" all count): never ask.
    if os.path.exists(MARKER):
        return _passthrough()

    head = _current_head()
    if head is None:
        return _passthrough()

    state = _load_state()
    last_seen = state.get("last_seen_head")
    last_asked = state.get("last_asked_ts", 0)

    # First run ever: record HEAD and stay silent. First-install asking is
    # Phase 24.4's job; this hook only catches the post-update moment.
    if not last_seen:
        state["last_seen_head"] = head
        _save_state(state)
        return _passthrough()

    # No new version pulled since we last looked: nothing to do.
    if head == last_seen:
        return _passthrough()

    # An update landed. Respect the cooldown so two updates in a short window
    # never double-ask; advance last_seen either way so we don't re-fire on
    # the same update next prompt.
    now = time.time()
    if last_asked and (now - last_asked) < COOLDOWN_SECONDS:
        state["last_seen_head"] = head
        _save_state(state)
        return _passthrough()

    state["last_seen_head"] = head
    state["last_asked_ts"] = now
    _save_state(state)

    lang = _detect_lang_hint()
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": _ES_BLOCK if lang == "es" else _EN_BLOCK,
        }
    }
    sys.stdout.write(json.dumps(payload))
    sys.stdout.write("\n")
    return 0


def _detect_lang_hint() -> str:
    raw = os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    if raw[:2].lower() == "es":
        return "es"
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleLocale"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0 and result.stdout.strip()[:2].lower() == "es":
                return "es"
        except (OSError, subprocess.SubprocessError):
            pass
    return "en"


_EN_BLOCK = """[ai-brain-starter — optional post-update note]
ai-brain-starter just updated to a new version on this machine, and there is no email on file for this user (~/.claude/.ai-brain-starter-email-on-file is missing). This is the ONE allowed moment to make a light, optional, freely-declinable email ask. It is NOT a gate. Handle the user's actual request normally — do not block it, do not lead with this. Weave in ONE short ask, then move on.

Rules:
- One ask only. Never a token. Never ask them to paste anything. Never a browser tab needed.
- Never imply anything stops working, syncs less, or is limited without an email.

Say something like, in the user's language: "Quick optional thing, no pressure — ai-brain-starter just updated. If you want, I can add you to the list for occasional update notes (there's also a free founder workflow audit). What's your best email? Or just say skip — everything works exactly the same either way."

IF they give an email (use their name from CLAUDE.md if you know it, else just the email):
```bash
EMAIL="their@email.com" NAME="Their Name" QM_LANG="en" python3 - <<'PY'
import json, os, urllib.request
payload = json.dumps({
    "email": os.environ["EMAIL"],
    "name": os.environ.get("NAME", ""),
    "lang": os.environ.get("QM_LANG", "en"),
    "stage": "post_install",
}).encode()
req = urllib.request.Request(
    "https://mycelium-ai.co/api/install/quick-mint",
    data=payload, headers={"content-type": "application/json"}, method="POST")
mp = os.path.expanduser("~/.claude/.ai-brain-starter-email-on-file")
try:
    with urllib.request.urlopen(req, timeout=12) as r:
        body = json.loads(r.read().decode() or "{}")
    with open(mp, "w") as f:
        f.write((body.get("token") or "recorded") + "\\n")
    os.chmod(mp, 0o600)
    print("ok")
except Exception as e:
    print(f"failed: {e}")
PY
```
On `ok`: one warm line ("You're on the list — thanks.") and resume. On `failed: ...`: stay calm ("Noted."), do not alarm them, resume; we will try again on a future update.

IF they decline outright (no / never / don't ask again): record it so we NEVER ask again:
```bash
mkdir -p ~/.claude && printf 'declined\\n' > ~/.claude/.ai-brain-starter-email-on-file && chmod 600 ~/.claude/.ai-brain-starter-email-on-file
```
Then one warm line ("All good — you're all set.") and resume.

IF they say "later" / "maybe" (not a hard no): do nothing — write no file. We will not ask again until the next update lands, and not within two weeks. Resume their request."""


_ES_BLOCK = """[ai-brain-starter — nota opcional post-actualización]
ai-brain-starter se acaba de actualizar en esta máquina y no hay email guardado para esta persona (no existe ~/.claude/.ai-brain-starter-email-on-file). Este es el ÚNICO momento permitido para hacer una pregunta de email liviana, opcional y fácil de rechazar. NO es un muro. Atendé lo que la persona pidió normalmente — no lo bloquees, no arranques con esto. Meté UNA pregunta corta y seguí.

Reglas:
- Una sola pregunta. Nunca un token. Nunca le pidas que pegue nada. Nunca hace falta abrir el navegador.
- Nunca insinúes que algo deja de funcionar, sincroniza menos, o queda limitado sin email.

Decí algo como: "Una cosa rápida y opcional, sin presión — ai-brain-starter se acaba de actualizar. Si querés, te agrego a la lista para novedades de vez en cuando (también hay una auditoría gratuita de flujo de trabajo para founders). ¿Cuál es tu mejor email? O decí saltar — todo funciona exactamente igual."

SI dan un email (usá su nombre del CLAUDE.md si lo sabés, si no, solo el email):
```bash
EMAIL="su@email.com" NAME="Su Nombre" QM_LANG="es" python3 - <<'PY'
import json, os, urllib.request
payload = json.dumps({
    "email": os.environ["EMAIL"],
    "name": os.environ.get("NAME", ""),
    "lang": os.environ.get("QM_LANG", "es"),
    "stage": "post_install",
}).encode()
req = urllib.request.Request(
    "https://mycelium-ai.co/api/install/quick-mint",
    data=payload, headers={"content-type": "application/json"}, method="POST")
mp = os.path.expanduser("~/.claude/.ai-brain-starter-email-on-file")
try:
    with urllib.request.urlopen(req, timeout=12) as r:
        body = json.loads(r.read().decode() or "{}")
    with open(mp, "w") as f:
        f.write((body.get("token") or "recorded") + "\\n")
    os.chmod(mp, 0o600)
    print("ok")
except Exception as e:
    print(f"failed: {e}")
PY
```
Si imprime `ok`: una línea cálida ("Listo, quedaste en la lista — gracias.") y seguí. Si imprime `failed: ...`: mantené la calma ("Anotado."), no la alarmes, seguí; reintentamos en una próxima actualización.

SI rechazan de plano (no / nunca / no me preguntes más): guardalo para NUNCA volver a preguntar:
```bash
mkdir -p ~/.claude && printf 'declined\\n' > ~/.claude/.ai-brain-starter-email-on-file && chmod 600 ~/.claude/.ai-brain-starter-email-on-file
```
Después una línea cálida ("Todo en orden.") y seguí.

SI dicen "después" / "tal vez" (no es un no rotundo): no hagas nada — no escribas ningún archivo. No volvemos a preguntar hasta la próxima actualización, y no antes de dos semanas. Seguí con lo que pedían."""


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(main())
    except Exception:
        sys.exit(_passthrough())
