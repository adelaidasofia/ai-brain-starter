---
name: resume-install
description: Your setup stopped partway and said it was done — how to finish it
---

# My install stopped partway

**Symptom.** Setup told you it was finished, but you never reached the journaling
interview, your `CLAUDE.md` is mostly empty, or your vault has folders and almost
nothing else.

**Cause.** Setup runs in 25 phases, each in its own file. Before 2026-08-15, only
the routing table at the top of the setup guide said which file came next, and on
a long install that table stops steering. The current phase file ended, nothing
said fifteen more phases existed, and stopping looked exactly like finishing.
Nothing errored. This was a bug in setup, not something you did.

Nothing you already built is lost. Resuming continues from where you stopped.

---

## How to fix it

**You do not need to type anything into a terminal.** Open a **new** Claude Code
session in your vault folder and paste one message. Pick the block for your
computer and your language, and paste the whole thing as a single message.

The prompt carries the phase order itself, so it works whether or not your copy
of the skill is up to date.

---

### Mac / Linux

<details open>
<summary><b>Español</b></summary>

```
Mi instalación de ai-brain-starter quedó a medias y nunca llegué a la entrevista.
Termínala tú. No me preguntes en qué fase quedé, porque no lo sé.

1. Lee ~/.claude/skills/ai-brain-starter/SKILL.md

2. Mira qué ya existe en mi vault y en ~/.claude/skills/ para deducir hasta dónde
   llegué, y dime en UNA línea qué encontraste. Si dudas entre dos fases, vuelve
   una atrás, nunca adelante.

3. Ejecuta las fases que falten EN ORDEN. Lee cada archivo de
   ~/.claude/skills/ai-brain-starter/phases/ justo antes de correrlo, de a uno,
   nunca todos de una:
     phase-04-claude-md.md
     phase-05-context-layer.md
     phase-06-09-tools-templates.md
     phase-10a-journaling.md
     phase-10b-panel-roster.md
     phase-11-external-tools.md
     phase-12-17-imports-rules.md
     phase-18-insights.md
     phase-19-23-finish.md
   Sáltate las que ya estén hechas.

4. Al terminar cada fase, guarda el avance en
   ~/.claude/.ai-brain-starter-progress.json con este contenido:
   {"last_completed_phase": "<fase>", "ts": "<ahora en ISO-8601>", "version": 1}

5. No me digas que terminó si no llegaste al final. Si te quedas sin contexto,
   dímelo y avísame que abra una sesión nueva y te pegue este mismo mensaje.
```

</details>

<details>
<summary><b>English</b></summary>

```
My ai-brain-starter install stopped partway and I never reached the interview.
Finish it. Do not ask me which phase I reached, because I don't know.

1. Read ~/.claude/skills/ai-brain-starter/SKILL.md

2. Look at what already exists in my vault and in ~/.claude/skills/ to work out
   how far I got, and tell me in ONE line what you found. If you're unsure
   between two phases, go back one, never forward.

3. Run the remaining phases IN ORDER. Read each file from
   ~/.claude/skills/ai-brain-starter/phases/ immediately before running it, one
   at a time, never all at once:
     phase-04-claude-md.md
     phase-05-context-layer.md
     phase-06-09-tools-templates.md
     phase-10a-journaling.md
     phase-10b-panel-roster.md
     phase-11-external-tools.md
     phase-12-17-imports-rules.md
     phase-18-insights.md
     phase-19-23-finish.md
   Skip any that are already done.

4. After each phase, save progress to
   ~/.claude/.ai-brain-starter-progress.json with this content:
   {"last_completed_phase": "<phase>", "ts": "<now, ISO-8601>", "version": 1}

5. Do not tell me it's complete if it isn't. If you're running low on context,
   say so and tell me to open a new session and paste this same message.
```

</details>

---

### Windows

The only difference is the folder paths.

<details open>
<summary><b>Español</b></summary>

```
Mi instalación de ai-brain-starter quedó a medias y nunca llegué a la entrevista.
Termínala tú. No me preguntes en qué fase quedé, porque no lo sé.

1. Lee %USERPROFILE%\.claude\skills\ai-brain-starter\SKILL.md

2. Mira qué ya existe en mi vault y en %USERPROFILE%\.claude\skills\ para deducir
   hasta dónde llegué, y dime en UNA línea qué encontraste. Si dudas entre dos
   fases, vuelve una atrás, nunca adelante.

3. Ejecuta las fases que falten EN ORDEN. Lee cada archivo de
   %USERPROFILE%\.claude\skills\ai-brain-starter\phases\ justo antes de correrlo,
   de a uno, nunca todos de una:
     phase-04-claude-md.md
     phase-05-context-layer.md
     phase-06-09-tools-templates.md
     phase-10a-journaling.md
     phase-10b-panel-roster.md
     phase-11-external-tools.md
     phase-12-17-imports-rules.md
     phase-18-insights.md
     phase-19-23-finish.md
   Sáltate las que ya estén hechas.

4. Al terminar cada fase, guarda el avance en
   %USERPROFILE%\.claude\.ai-brain-starter-progress.json con este contenido:
   {"last_completed_phase": "<fase>", "ts": "<ahora en ISO-8601>", "version": 1}

5. No me digas que terminó si no llegaste al final. Si te quedas sin contexto,
   dímelo y avísame que abra una sesión nueva y te pegue este mismo mensaje.
```

</details>

<details>
<summary><b>English</b></summary>

```
My ai-brain-starter install stopped partway and I never reached the interview.
Finish it. Do not ask me which phase I reached, because I don't know.

1. Read %USERPROFILE%\.claude\skills\ai-brain-starter\SKILL.md

2. Look at what already exists in my vault and in
   %USERPROFILE%\.claude\skills\ to work out how far I got, and tell me in ONE
   line what you found. If you're unsure between two phases, go back one, never
   forward.

3. Run the remaining phases IN ORDER. Read each file from
   %USERPROFILE%\.claude\skills\ai-brain-starter\phases\ immediately before
   running it, one at a time, never all at once:
     phase-04-claude-md.md
     phase-05-context-layer.md
     phase-06-09-tools-templates.md
     phase-10a-journaling.md
     phase-10b-panel-roster.md
     phase-11-external-tools.md
     phase-12-17-imports-rules.md
     phase-18-insights.md
     phase-19-23-finish.md
   Skip any that are already done.

4. After each phase, save progress to
   %USERPROFILE%\.claude\.ai-brain-starter-progress.json with this content:
   {"last_completed_phase": "<phase>", "ts": "<now, ISO-8601>", "version": 1}

5. Do not tell me it's complete if it isn't. If you're running low on context,
   say so and tell me to open a new session and paste this same message.
```

</details>

---

## On an up-to-date install, this is shorter

Once your copy carries the 2026-08-15 fix, the whole thing is one line in a fresh
session, on either platform:

> resume my ai-brain-starter install

Spanish: `retoma mi instalación de ai-brain-starter`

The long prompts above exist so you do not have to update first.

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

If you stopped at Phase 3, you missed all of them.

---

## Expect more than one sitting

The full install is roughly 3,700 lines of setup across 25 phases. A single
session can run out of room before the end. That is normal and no longer costs
you anything: progress is recorded after each phase, so a new session picks up
where the last one stopped. Paste the prompt again and keep going.
