# 5-minute install demo

This is the script for a 5-minute terminal recording that takes a stranger from zero to a working AI Brain Starter vault writing its first journal entry. Embed at the top of the README. Replaces explaining what the system does with showing it work.

---

## Recording setup

- macOS Terminal or iTerm. Plain default theme.
- Window size: 110 columns by 32 rows (large enough to read on phone).
- Font: Menlo or SF Mono, 16pt minimum. Light or dark, consistent.
- Recording tool: `asciinema` for terminal-only or QuickTime for screen plus voice.
- Total runtime target: 4:30 to 5:00. Cut at 5:00 hard.

## Pre-conditions assumed (state at start)

- Mac with Homebrew installed.
- Claude Code desktop app installed and signed in (Pro or Max).
- No existing AI Brain Starter install.
- A clean Documents folder where the demo vault will live.

## Beats and timing

Voice and screen are synchronized. Each beat is one logical step. Narration in italics. Commands shown verbatim.

### Beat 0: title card (0:00 to 0:08)

Plain text card on screen.

> **AI Brain Starter, install demo**
> *Founder's brain, installed in 5 minutes.*

No narration over the card.

### Beat 1: open Claude Code (0:08 to 0:25)

*"This is Claude Code. I'm signed in. I'm going to set up a brand-new vault by pasting one prompt."*

Cut to Claude Code app, fresh session, empty input.

### Beat 2: paste the install prompt (0:25 to 0:55)

*"This is the prompt. It tells Claude where to clone the repo, runs the bootstrap script, and starts the setup interview. One paste."*

Show the prompt being pasted from clipboard:

```
Please set up my AI Brain Starter end-to-end in this session. Clone https://github.com/mycelium-hq/ai-brain-starter.git into ~/.claude/skills/ai-brain-starter (git pull if it already exists), run bash ~/.claude/skills/ai-brain-starter/bootstrap.sh, then start the setup interview by running the setup-brain skill. Keep going through every phase without stopping. I shouldn't have to type any commands between steps.
```

Hit return. Don't narrate during clone.

### Beat 3: bootstrap runs (0:55 to 2:00)

Speed up footage 4x during install steps. Voice continues at normal pace over the sped-up footage.

*"Claude clones the repo. Runs the bootstrap script. Installs Obsidian, Python, the graphify dependencies, the skills, the hooks. I don't type anything. The system installs itself."*

Highlights to keep at normal speed (do not speed up these moments):
- The "Obsidian installed" confirmation line
- The "Hooks registered" confirmation line
- The first interview question

### Beat 4: answer the interview (2:00 to 3:30)

*"Now Claude interviews me. Who I am. What I do. What goals matter this quarter. Who the most important people in my life are. The answers shape the vault structure. There's no config file. The whole setup is a conversation."*

Show three or four representative interview turns. Pick ones that are universal:
- "What do you do for work?"
- "Who are the three people you check in with most?"
- "What's the one project you do not want to forget about this quarter?"

Speed up between interview questions.

### Beat 5: first journal entry (3:30 to 4:30)

*"Setup is done. Now I run /journal. The advisory panel meets my draft. It pushes back where my thinking is soft. The entry saves to today's date with a floor tag and a frontmatter block. From now on, every session compounds."*

Show:
- `/journal` command
- The first interview prompt from the panel
- A short journal entry (the host types two sentences live)
- The save confirmation

### Beat 6: close (4:30 to 5:00)

*"That was five minutes. The vault remembers what I just said. Tomorrow's session starts where this one ended. The whole system is on disk. It's mine."*

End card:

> **github.com/mycelium-hq/ai-brain-starter**
> *Read the Reliability Manifesto. Install yours.*

---

## What to cut if running over time

In order of priority to cut:
1. The bootstrap-install footage (further compress to 8x speed).
2. The interview questions (drop from four to two).
3. The pre-title card (start cold on the Claude Code paste).

Never cut: Beat 5 (first journal entry). That is the dopamine moment that makes someone install.

## What to add if you have extra room

Beat 5.5 (4:00 to 4:20): show the floor tag firing on the journal entry. The user sees their entry tagged with an emotional floor and gets a one-line panel reaction. That tag is the moment when the system notices the user.

## Voice notes

- "Founder's brain" not "second brain" in the title card. The tag has been overused.
- Avoid the word "powerful." Avoid "intelligent." The system is not announcing itself.
- The closing line "It's mine." is the warm beat. Land it. Don't add anything after it.

## Recording checklist

- [ ] Clean Mac (no other notifications, no calendar pop-ups).
- [ ] Terminal and Claude Code window pre-sized.
- [ ] Clipboard pre-loaded with the install prompt.
- [ ] Microphone tested (no room echo).
- [ ] Recording app set to capture screen plus audio.
- [ ] One full take dry run before the recorded take.
- [ ] After recording, play back at 1.5x to check for dead air.

---

Last updated 2026-04-30.
