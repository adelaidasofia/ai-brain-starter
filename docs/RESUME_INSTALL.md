---
name: resume-install
description: Your setup stopped partway and said it was done — how to finish it
---

# My install stopped partway

**Symptom.** Setup told you it was finished, but you never reached the journaling
interview, your `CLAUDE.md` is mostly empty, or your vault has folders and
almost nothing else.

**Cause.** Setup runs in phases 0 through 24, each in its own file. Before
2026-08-15, only the routing table at the top of the setup guide said which file
came next, and on a long install that table stops steering. The current phase
file ended, nothing said fifteen more phases existed, and stopping looked exactly
like finishing. Nothing errored. This was a bug in setup, not something you did.

Nothing you already built is lost. Resuming continues from where you stopped.

---

## Step 1 — update

```bash
bash ~/.claude/skills/ai-brain-starter/bootstrap.sh
```

## Step 2 — open a fresh session and ask to resume

In your vault folder, start Claude Code and say:

> resume my ai-brain-starter install

Spanish: `retoma mi instalación de ai-brain-starter`

That is the whole thing on an updated install. Claude works out how far you got
by looking at your vault, tells you where it landed, and continues.

---

## If you cannot update yet

Use this instead. It is self-contained and works on an un-updated install,
because it carries the phase order itself rather than relying on the routing
table. Paste it as one message.

<details>
<summary><b>English</b></summary>

```
My ai-brain-starter install stopped partway and I never reached the interview.
Do not ask me which phase I reached — I don't know. Work it out yourself:

1. Read ~/.claude/skills/ai-brain-starter/SKILL.md
2. Look at what already exists in my vault and in ~/.claude/skills/ to infer how
   far I got. Signals, least to most advanced:
     only vault folders, no real CLAUDE.md    -> resume at phase 4
     CLAUDE.md with real content              -> phase 5
     "Meta/Last Session.md" in my vault       -> phase 6-9
     ~/.claude/skills/daily-journal/          -> phase 11
     "Meta/journal-index.json" in my vault    -> phase 19
   Tell me in one line what you found and which phase you're starting from.
   If you're unsure between two, go back one phase, not forward.
3. Run the remaining phases IN ORDER, reading each file from
   ~/.claude/skills/ai-brain-starter/phases/ immediately before executing it:
     phase-04-claude-md.md, phase-05-context-layer.md,
     phase-06-09-tools-templates.md, phase-10a-journaling.md,
     phase-10b-panel-roster.md, phase-11-external-tools.md,
     phase-12-17-imports-rules.md, phase-18-insights.md,
     phase-19-23-finish.md
   Read them one at a time, never all at once.
4. After each phase, write ~/.claude/.ai-brain-starter-progress.json as
   {"last_completed_phase": "<phase>", "ts": "<now, ISO-8601>", "version": 1}
5. Do not stop before phase 24, and do not tell me it's complete if it isn't.
   If you're running low on context, say so and tell me to open a new session.
```

</details>

<details>
<summary><b>Español</b></summary>

```
Mi instalación de ai-brain-starter se quedó a medias y nunca llegué a la
entrevista. No me preguntes en qué fase quedé, porque no lo sé. Averígualo tú:

1. Lee ~/.claude/skills/ai-brain-starter/SKILL.md
2. Mira qué existe ya en mi vault y en ~/.claude/skills/ para deducir hasta
   dónde llegué. Señales, de menos a más avanzado:
     solo carpetas del vault, sin CLAUDE.md real -> retoma en la fase 4
     CLAUDE.md con contenido real                -> fase 5
     "Meta/Last Session.md" en mi vault          -> fase 6-9
     ~/.claude/skills/daily-journal/             -> fase 11
     "Meta/journal-index.json" en mi vault       -> fase 19
   Dime en una línea qué encontraste y desde qué fase vas a seguir. Si dudas
   entre dos, vuelve una fase atrás, nunca adelante.
3. Ejecuta las fases que faltan EN ORDEN, leyendo cada archivo de
   ~/.claude/skills/ai-brain-starter/phases/ justo antes de ejecutarlo:
     phase-04-claude-md.md, phase-05-context-layer.md,
     phase-06-09-tools-templates.md, phase-10a-journaling.md,
     phase-10b-panel-roster.md, phase-11-external-tools.md,
     phase-12-17-imports-rules.md, phase-18-insights.md,
     phase-19-23-finish.md
   Léelos de a uno, nunca todos de una.
4. Al terminar cada fase escribe ~/.claude/.ai-brain-starter-progress.json con
   {"last_completed_phase": "<fase>", "ts": "<ahora, ISO-8601>", "version": 1}
5. No pares antes de la fase 24, y no me digas que terminó si no terminó.
   Si te estás quedando sin contexto, dímelo y avísame que abra sesión nueva.
```

</details>

---

## Where the interview actually is

There is more than one, which is why "I never got the interview" can mean
different things:

| Phase | What it asks you |
|---|---|
| 1 | Welcome: language, how you work, where your vault goes |
| 4 | The questions that build your `CLAUDE.md` |
| 10a | **Journaling setup — the long one most people mean** |
| 19 | Your first real journal entry, as a test drive |

If you stopped at phase 3, you missed all of them.

---

## Expect more than one sitting

The full install is around 3,700 lines of setup across 25 phases. A single
session can run out of room before the end. That is normal and no longer costs
you anything: progress is recorded after each phase, so a new session picks up
where the last one stopped. Say the resume phrase again and keep going.
