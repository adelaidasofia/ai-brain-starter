---
name: repurpose-talk
description: Use when the user just gave a talk, panel, fireside chat, keynote, workshop, podcast, or webinar and wants content made from it — triggers like "I just gave a talk", "the panel is done", "just finished speaking", "turn my talk into LinkedIn posts", "repurpose this recording/transcript into posts, notes, or clips", or /repurpose-talk. Works from a recording, transcript, slides, or a from-memory brain-dump.
trigger: /repurpose-talk
version: 1.0.0
---

# Repurpose Talk

Turn a single speaking engagement into maximum content leverage. Run this after any talk, panel, fireside chat, or workshop.

## Trigger

`/repurpose-talk` or when the user says "I just gave a talk", "the panel is done", "just finished speaking"

## Input

Ask for (in this order, skip any they don't have):
1. **Recording** - video/audio file path or link
2. **Transcript** - if available (check meeting note tools, Google Docs, or manual notes)
3. **Slides** - if used
4. **If none of the above**: ask them to brain-dump the key points, best audience reactions, and any quotes they remember

## Process

### Phase 1: Extract (from whatever input is available)

Pull out:
- **3-5 key insights** they shared (the things that made people nod or take notes)
- **1-2 stories** they told (personal anecdotes are the highest-performing content)
- **Best one-liners** (quotable sentences, these become standalone posts)
- **Audience questions** that came up (each question is a content seed)
- **The main thesis** of the talk in one sentence

### Phase 1.5: Score clip candidates against the virality framework

For each candidate moment (one-liner, story beat, question, insight), tag which virality category it hits. Cherry-picked from SamurAIGPT/Generative-Media-Skills `ai-clipping` SKILL.md (MIT) — 8 categories that explain WHY short-form clips travel:

- **Hook moments** — strong opening line that stops the scroll
- **Emotional peaks** — laughter, anger, vulnerability, awe
- **Opinion bombs** — spicy, contrarian, debate-bait takes
- **Revelation moments** — "wait, what?" reframes
- **Conflict** — disagreement, tension, callouts
- **Quotable lines** — tight, screenshot-worthy phrasing
- **Story peaks** — climax of a narrative arc
- **Practical value** — actionable insight a viewer will save

A clip that hits 2+ categories is a stronger pick than one that hits 1. A clip that hits 0 isn't worth packaging — drop it.

The taxonomy is FOR RANKING + EXPLAINING, not for inventing content. Every clip still has to come from the actual talk; never confabulate a "spicy take" the speaker didn't make.

### Phase 2: Generate Content Package

Create all of the following:

**LinkedIn Posts (3-5)**
- Each post: 150-300 words, first-person, starts with a hook line
- One post per key insight
- No AI-sounding language. Run through humanizer principles if available.
- End each with a question to drive comments
- Format: hook line, story/context, insight, question

**Short-Form Notes (5-8)**
- Short-form (1-5 sentences each)
- Mix of: one-liners, micro-stories, questions, frameworks
- If using Substack: append to the Substack Notes drafts file
- If using another platform: save to a drafts file in the vault

**Article Seed (1)**
- If the talk contains enough for a full essay, create an article seed
- Title + 2-3 paragraph draft expanding the talk's core thesis

**Video Clip Plan (if recording exists)**
- Timestamp markers for the 3-5 best 30-60 second clips
- Suggested caption for each clip
- Which platform each clip is best for (LinkedIn, Instagram Reels, TikTok)

### Phase 3: Schedule

- Suggest a 2-week posting calendar: which piece goes out when
- Rule: never post more than 1 LinkedIn post per day, 2 short-form notes per day
- Space the content to maximize reach (don't dump everything day 1)

### Phase 4: Cross-pollinate

- Identify any business angles (if the talk was about their industry/product)
- Flag any investor-relevant soundbites for fundraising narrative
- Note any audience members worth following up with (add to CRM if names given)

## Output

Save the full content package to the Writing folder: `Talk Repurpose - [Event Name] - YYYY-MM-DD.md`
Append short-form notes directly to the drafts file.
Open the main file in Obsidian when done (if Local REST API is available).

## Rules

- Use the user's actual voice. Read 2-3 of their published posts first to calibrate tone.
- No em dashes anywhere.
- Run humanizer principles on every piece if the humanizer skill is available.
- If the talk was in a non-English language, create content in BOTH languages (primary + secondary audience).
- Every LinkedIn post must be standalone (someone who didn't attend the talk should understand it).
