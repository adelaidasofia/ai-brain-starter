# Graphify Transcription

Reference for `--whisper-model` and video/audio transcription. Load this only when Step 2's `detect` reported one or more `video` files - a corpus with no video never reads this.

---

## Step 2.5 - Transcribe video/audio files

Skip this step entirely if `detect` returned zero `video` files.

Video and audio files cannot be read directly. Transcribe them to text first, then treat the transcripts as doc files in Step 3.

**Strategy:** read the god-node labels from `graphify-out/.graphify_detect.json` (or the analysis file if one exists from a previous run). You are already a language model - write a one-sentence domain hint yourself from those labels, then pass it to Whisper as the initial prompt. No separate API call needed.

**However**, if the corpus has *only* video files and no other docs/code, use the generic fallback prompt: `"Use proper punctuation and paragraph breaks."`

**Step 1 - Write the Whisper prompt yourself.**

Read the top labels from the detect output or a prior analysis, then compose a short domain-hint sentence, for example:

- Labels: `transformer, attention, encoder, decoder` -> `"Machine learning research on transformer architectures and attention mechanisms. Use proper punctuation and paragraph breaks."`
- Labels: `kubernetes, deployment, pod, helm` -> `"DevOps discussion about Kubernetes deployments and Helm charts. Use proper punctuation and paragraph breaks."`

**Step 2 - Transcribe:**

```bash
export GRAPHIFY_WHISPER_MODEL=base  # or whatever --whisper-model the user passed (base|small|medium|large) - must be exported
export GRAPHIFY_WHISPER_PROMPT="<the one-sentence domain hint you composed in Step 1>"
$(cat graphify-out/.graphify_python) -c "
import json, os, sys
from pathlib import Path
from graphify.transcribe import transcribe_all

detect = json.loads(Path('graphify-out/.graphify_detect.json').read_text())
video_files = detect.get('files', {}).get('video', [])
prompt = os.environ.get('GRAPHIFY_WHISPER_PROMPT', 'Use proper punctuation and paragraph breaks.')

transcript_paths = transcribe_all(video_files, initial_prompt=prompt)
# Write the JSON from Python (NOT a shell '>' redirect): transcribe_all/Whisper
# print progress to stdout, which would otherwise corrupt the JSON file.
Path('graphify-out/.graphify_transcripts.json').write_text(json.dumps(transcript_paths, ensure_ascii=False))
print(f'Transcribed {len(transcript_paths)} file(s)', file=sys.stderr)
"
```

**Whisper model:** default is `base`. If the user passed `--whisper-model <name>`, `export GRAPHIFY_WHISPER_MODEL=<name>` (it must be exported, not just assigned) before running the command above. Valid values: `base`, `small`, `medium`, `large` - larger models trade speed for transcription accuracy.

After transcription:
- Read the transcript paths from `graphify-out/.graphify_transcripts.json`.
- Add them to the docs list before dispatching semantic subagents in Step 3.
- Print how many transcripts were created: `Transcribed N video file(s) -> treating as docs`.
- If transcription fails for a file, print a warning and continue with the rest.
