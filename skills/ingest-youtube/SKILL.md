---
name: ingest-youtube
description: Use when the user says /ingest-youtube <url-or-channel> [--days N], pastes a YouTube URL (youtube.com or youtu.be) wanting a transcript or summary in the vault, asks to ingest, capture, sync, transcribe, or pull a YouTube video, channel, talk, podcast, or keynote into the vault, or wants a video's captions or content available to the knowledge graph. Not for downloading video files, live streams, or non-YouTube sources (Vimeo, Twitch).
---

# ingest-youtube — YouTube-to-vault connector

> **`{SKILL_DIR}`** = this skill's own folder (locally: the directory this SKILL.md lives in; a served brain substitutes the real absolute path before you read this). If a path does not resolve, name the missing file and stop — never guess another location.

Ingests YouTube transcripts into the vault as markdown the graphify pipeline can read and the rest of the AI Brain Starter substrate (decision log, session-close cascade, hooks) can act on.

Same connector pattern as `ingest-github`: adding a new source means a new normalizer, not a new architecture.

## When to use

- User says `/ingest-youtube <url>` for a single video
- User says `/ingest-youtube <channel-handle> [--days N]` for a channel's recent uploads
- User asks to capture, sync, ingest, transcribe, or pull a talk/podcast/keynote into the vault
- User pastes a YouTube URL and asks for a transcript or summary
- User mentions wanting a video's content available to the knowledge graph

Do NOT use for:
- Downloading the actual video file (use `yt-dlp` directly with `-f best`)
- Live streams (transcripts are not stable)
- Non-YouTube sources (Vimeo, Twitch, Twitter Spaces get their own connectors)
- One-off transcript reads where the user does not want a vault file (run `yt-dlp --write-auto-sub` directly and pipe to stdout)

## How it works

1. Parse the input: single URL → single-video mode. Channel handle (e.g. `@channelname`) → channel mode (last N days, default 14).
2. Verify `yt-dlp` is installed. If not, attempt `brew install yt-dlp` (macOS) or `pip3 install --user yt-dlp` and surface the install path. Abort if neither works.
3. For each video, call `yt-dlp --list-subs <url>` to enumerate available subtitles.
4. Subtitle priority: manual subs > auto-generated > Whisper fallback. Manual subs preserve creator-provided punctuation and speaker labels; auto-gen is uppercase + no punctuation; Whisper is the floor.
5. Download the highest-priority subtitle as VTT via `yt-dlp --write-sub --sub-lang <lang> --skip-download`. Default language preference: `en,es` (so non-English content is captured in its original language without forcing English).
6. Strip VTT timing markers and merge into clean prose paragraphs. Preserve speaker labels if the source had them.
7. Pull video metadata (title, channel, upload date, duration, video_id, URL) via `yt-dlp --print-json --skip-download`.
8. Slugify the channel name and video title. Write to `External Inputs/YouTube/<channel-slug>/<YYYY-MM-DD>-<video-slug>.md`.
9. Scan transcript for trigger keywords (decision, framework, model, principle, "the lesson is", playbook, anti-pattern, case study). For each match, create a writing-seed stub at `⚙️ Meta/Captures/<YYYY-MM-DD>-youtube-<channel-slug>-<video-id>.md` so the seed lands in the captures aggregator.
10. Print summary: file path, transcript word count, language, seeds detected.

## Voice rules

- No em dashes (use commas, colons, periods, parentheses)
- Spanish-language transcripts stay in Spanish; English stays in English; bilingual transcripts preserve language code-switches
- Speaker labels quoted verbatim from the source subtitle file
- No paraphrasing or summarization at ingest time. Summaries happen downstream via `/note-todos` or `/repurpose-talk`.

## Invocation

The skill is a thin orchestrator. The actual ingestion runs in Python at `{SKILL_DIR}/ingest.py`.

When invoked:

1. Parse arguments: URL (single-video) or channel handle (channel mode), `--days N` (channel mode only, default 14), `--lang <code>` (override default `en,es`).
2. Single-video mode: hand the URL to `ingest.py`. The script handles `yt-dlp` calls, VTT cleanup, vault write, and seed stub creation.
3. Channel mode: enumerate the last N days of uploads via `yt-dlp --flat-playlist --print-json --dateafter <date> https://www.youtube.com/<channel>`. Pass each video URL to `ingest.py` in sequence.
4. Surface the summary to the user. If seed stubs were created, list the file paths.

## Output contract

The vault file at `External Inputs/YouTube/<channel-slug>/<YYYY-MM-DD>-<video-slug>.md` has frontmatter:

```yaml
---
type: external-input
source: youtube
video_id: <11-char ID>
url: https://www.youtube.com/watch?v=<id>
channel: <channel-name>
channel_url: https://www.youtube.com/<handle>
title: <video title>
upload_date: <YYYY-MM-DD>
duration_seconds: <int>
language: <ISO code>
subtitle_source: manual | auto | whisper
word_count: <int>
ingested_at: <ISO 8601 timestamp>
---
```

Body is the cleaned transcript as paragraph prose. If the source had speaker labels, format as `**<speaker>:** <text>` per turn.

Capture seed stubs at `⚙️ Meta/Captures/<date>-youtube-<channel-slug>-<video-id>.md` carry frontmatter that matches the existing capture schema so the captures aggregator picks them up cleanly.

## Idempotency

Re-ingesting the same video URL overwrites the same vault file (the path is deterministic from `channel-slug + upload-date + video-slug`). The seed stub filenames hash the video_id, so the same source video produces the same stub filename across re-runs. Re-runs refresh, never duplicate.

## Acceptance test

A successful run produces:
1. One new (or refreshed) file at `External Inputs/YouTube/<channel>/<date>-<title>.md` with valid frontmatter and clean prose body
2. Zero or more new capture stubs at `⚙️ Meta/Captures/<date>-youtube-<channel>-<video-id>.md`
3. A stdout summary: `Wrote N words to <path>. Language: <code>. Subtitle source: <source>. Seeds at: <paths>.`

If the video has no available subtitles and Whisper is not installed locally, write a stub file with `subtitle_source: none` and a note in the body explaining the gap, so re-runs are still idempotent and the absence is recorded.

## Whisper fallback

If `yt-dlp --list-subs` returns no manual or auto subtitles AND `whisper-cpp` is installed locally, fall back to:

1. `yt-dlp -x --audio-format mp3 -o <tmp>/<video-id>.mp3 <url>` to download audio
2. `whisper-cli <tmp>/<video-id>.mp3 --model ggml-large-v3.bin --output-vtt` to transcribe
3. Continue with the VTT cleanup pipeline

Whisper fallback is OFF by default for cost reasons. Enable per-call with `--whisper`. Local Whisper has zero per-minute cost but takes ~real-time on CPU.

## Cross-references

- For turning the transcript into LinkedIn/Substack content → `repurpose-talk` after ingest
- For pulling action items out of the transcript → `note-todos` after ingest
- For knowledge-graph extraction across many transcripts → `graphify` on `External Inputs/YouTube/` directly
