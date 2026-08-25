# Graphify Optimizations

**Read this BEFORE running `/graphify` on a corpus larger than ~50 files.** Without these optimizations, a full graphify run on a notes/journal vault will burn 2-12× more LLM tokens than necessary and produce a worse graph.

## Lessons from the 2026-04-11 Stage 1 pilot (370 files, 1.48M tokens)

These 7 lessons from running /graphify on a real 2,380-file journal corpus are now baked into the scripts:

1. **iCloud cold reads hang the pipeline.** On macOS, files under `~/Desktop/` or `~/Documents/` are iCloud-synced. Cold reads (files not materialized locally) take ~200ms each vs ~0.1ms warm — making bulk scans 1000x slower, looking exactly like a hang. `graphify_prep.py` now samples 40 files at startup and prints a loud warning if reads are slow, pointing users to `brctl download "<folder>"` as the fix.

2. **Cache invalidation on re-root.** The semantic cache is keyed by `SHA256(content + null + resolved_path)`. If you move files from `graphify-input/` to `📓 Journals/`, the cache shows 0 hits despite identical content. Plan around this — don't re-root mid-pipeline.

3. **Tool-use count is the #1 token-cost predictor.** In the Stage 1 pilot, agents that used ≤15 tool calls averaged **101K tokens/chunk**; agents that used ≥35 tool calls averaged **160K tokens/chunk** — 37% waste with zero quality gain. The extraction prompt template now explicitly tells subagents to use Grep for bulk scans instead of per-file sequential Reads, with a ≤15-tool-call target.

4. **Grep > sequential Read for entity scanning.** A single Grep call over a chunk's file list finds all recurring person/place/company names faster and cheaper than 30+ sequential Read calls. Reserve Reads for the ~5 densest files where hyperedge context matters.

5. **Use the right Python env.** The `graphify` package is installed via pipx as `graphifyy`, not the system Python. For any script that imports `graphify.*`, use `/Users/<you>/.local/pipx/venvs/graphifyy/bin/python3`.

6. **Path-form wikilinks break canonicalization.** `[[📁 Folder/Name]]` and `[[Name]]` are treated as different labels in label-based dedup, producing duplicate god nodes. `graphify_canonicalize.py` now has `strip_folder_prefix()` that collapses path-form labels to their bare equivalents before canonicalizing. Combined with a "bare filenames only" rule in CLAUDE.md, this prevents new violations and fixes legacy ones automatically.

7. **`source_file` must be a specific .md path, never a directory.** An agent setting `source_file` to `"📓 Journals/"` broke `save_semantic_cache` with `Errno 21: Is a directory`, causing the entire cache save to fail and forcing the next run to re-pay for all 370 files. `graphify_canonicalize.py` now has `normalize_source_file()` that defensively strips directory paths to `""` so the cache save can skip cleanly. The extraction prompt template also has an explicit "MUST be a specific .md path" requirement with good/bad examples.

These are wrapper scripts (`scripts/graphify_prep.py`, `graphify_canonicalize.py`, `graphify_chunk.py`) that bracket the upstream graphify pipeline. They are tuned for the framework that ai-brain-starter installs (the 16 floors from Shame to Peace). The prep step automatically picks up `dominant_floors:` / `floor:` frontmatter tags and turns each into a free EXTRACTED edge to the canonical floor node.

## TL;DR — the cost problem and the fix

A naive `/graphify <vault>` on a 1,500-file markdown vault costs ~10M LLM tokens. With these scripts the same run costs ~1.5M tokens (-85%) and produces a graph with 5-10× more edges. The single biggest savings come from:

1. **Dedupe before LLM extraction** (typical: -32% to -66% files). Vaults accumulate ` 2.md` siblings, cross-directory copies from abandoned staging runs, and other near-duplicates the LLM would otherwise re-process at full cost.
2. **Pre-extract `[[wikilinks]]` with regex** (typical: -50% LLM output). Every wikilink is already an EXTRACTED edge waiting to be picked up — no LLM needed. The LLM should only do INFERRED / semantic / rationale work on top of the regex baseline.
3. **Cache after merge** (next run: ~free for unchanged files). graphify has a built-in `save_semantic_cache` API that the upstream skill never calls if you bypass its merge step. Without this, `--update` mode re-extracts everything.

## The optimized pipeline

```
graphify-input/                          ← your source folder
        ↓
[1] graphify_prep.py --apply             ← dedupe + regex preflight (no LLM)
        ↓
[2] graphify_chunk.py                    ← word-balanced chunking, skip junk
        ↓
[3] dispatch N subagents in parallel     ← LLM-only does INFERRED edges
        ↓
[4] merge chunks + preflight             ← combine regex + LLM
        ↓
[5] graphify_canonicalize.py --cache     ← collapse duplicate labels + write cache
        ↓
[6] build_from_json + cluster + HTML     ← upstream graphify functions
        ↓
graphify-out/graph.json + graph.html
```

For weekly/monthly updates, run `/graphify <path> --update`. The cache from the last full run means only new files cost LLM tokens.

## Step-by-step

### Step 1 — Dedupe + structural pre-extraction

```bash
cd <vault root>

# Dry run first (always)
python3 skills/graphify/scripts/graphify_prep.py graphify-input

# Then apply
python3 skills/graphify/scripts/graphify_prep.py graphify-input --apply
```

What this does:
- **Pass A:** finds `* 2.md` sibling duplicates and deletes md5-identical pairs (quarantines true alternate drafts to `_review_alternate_drafts/`)
- **Pass B:** finds files with the same name in multiple directories (common when previous incomplete runs left staging copies); keeps the shallowest copy, deletes md5-identical others
- Removes empty subdirs after dedupe
- Walks all remaining `.md` files with regex and writes `graphify-out/.graphify_preflight.json` containing every `[[wikilink]]` and YAML frontmatter framework tag as a node + EXTRACTED edge — these are FREE structural edges the LLM should not re-extract

The script auto-recognizes the domain-specific category labels (configurable per vault) from `dominant_floors:` or `floor:` frontmatter keys and creates a free `expresses_floor` edge from each tagged file to the canonical floor node. No LLM needed for any of this.

### Step 2 — Word-balanced chunking

```bash
python3 skills/graphify/scripts/graphify_chunk.py graphify-input \
  --target-chunks 12 \
  --skip-ai-extract
```

What this does:
- Walks all `.md` files in `graphify-input/`
- Skips files smaller than 500 words (LLM rarely finds inferred edges in tiny files)
- Skips files prefixed `[AI Extract]` (already-LLM-generated chat summaries with low marginal value)
- Greedy bin-packs the rest across N chunks balanced by **word count, not file count**
- Writes one `graphify-out/.chunk_NN_files.txt` per chunk

Why bin-packing matters: alphabetical chunking on a 566-file corpus produced one chunk with 102K words and another with 2K words — 50× variance, slow stragglers, risk of context overflow on the big chunk. Bin-packed chunks all hit ~46K words ± 50.

Why 12 chunks (not 25): each subagent pays a fixed ~1.5K of prompt overhead. 12 large chunks instead of 25 small chunks cuts overhead by ~50% with no quality penalty.

### Step 3 — Dispatch subagents

This step is done by Claude in the `/graphify` skill flow. The agent prompt should:
- Be told that `graphify-out/.graphify_preflight.json` already contains every `[[wikilink]]`
- Be told to **NOT re-extract wikilinks** — only add INFERRED / semantic / rationale / hyperedge content
- Use a strict schema: `file_type` MUST be one of `["document", "code", "image", "paper", "rationale"]` (agents will invent `person`, `concept`, etc. otherwise — see `graphify_canonicalize.py` for the auto-fix)
- Use canonical framework labels with no suffix (`Love` not `Love Floor`)
- Output 1-3 hyperedges per chunk (not 0 — always provide a non-empty `[]` example or agents skip the field)

A complete agent prompt template lives in the parent project's `⚙️ Meta/templates/graphify-extraction-prompt.md` if you want a starting point.

### Step 4 — Merge chunks + preflight

```python
import json
from pathlib import Path

all_nodes = []
all_edges = []
all_hyper = []

# Start with the regex preflight
preflight = json.loads(Path("graphify-out/.graphify_preflight.json").read_text())
all_nodes.extend(preflight["nodes"])
all_edges.extend(preflight["edges"])

# Add LLM chunks
for p in sorted(Path("graphify-out").glob(".chunk_*_result.json")):
    d = json.loads(p.read_text())
    all_nodes.extend(d.get("nodes", []))
    all_edges.extend(d.get("edges", []))
    all_hyper.extend(d.get("hyperedges", []))

Path("graphify-out/.graphify_extract.json").write_text(json.dumps({
    "nodes": all_nodes,
    "edges": all_edges,
    "hyperedges": all_hyper,
    "input_tokens": 0,
    "output_tokens": 0,
}))
```

### Step 5 — Canonicalize + write cache (CRITICAL)

```bash
python3 skills/graphify/scripts/graphify_canonicalize.py \
  graphify-out/.graphify_extract.json \
  --cache
```

What this does:
- Collapses nodes with the same canonical label (`coo_advisory_love`, `breathwork_higher_self_love`, etc. → single `c_love`)
- Strips invalid `file_type` values agents invented (forces them to `document`)
- Remaps edges to canonical IDs and dedupes
- **`--cache` writes results to `graphify-out/cache/` via `graphify.cache.save_semantic_cache`** — this is the single most important optimization for repeat runs. Without it, the next `/graphify --update` re-extracts everything at full price. With it, unchanged files are free cache hits forever (until their content changes).

### Step 6 — Standard graphify build (unchanged)

```python
from graphify.build import build_from_json
from graphify.cluster import cluster
from graphify.export import to_json, to_html
import json
from pathlib import Path

extraction = json.loads(Path("graphify-out/.graphify_extract.json").read_text())
# directed=IS_DIRECTED: True if --directed was given in the original invocation, else False.
G = build_from_json(extraction, directed=IS_DIRECTED)
communities = cluster(G)
to_json(G, communities, "graphify-out/graph.json")
to_html(G, communities, "graphify-out/graph.html")
```

## Cumulative savings

Measured on a 1,660-file vault (April 2026):

| Optimization | Savings | Cumulative cost vs naive |
|---|---|---|
| Pass A (` 2.md` dedupe) | -32% files | 68% |
| Pass B (cross-dir dedupe) | -50% of remaining | 34% |
| Skip files <500 words | -10% files | 31% |
| Skip `[AI Extract]` files | -38% remaining | 19% |
| Word-balanced chunks (12 vs 29) | -60% prompt overhead | 12% |
| Regex wikilink preflight | -50% LLM output | 8% |
| **Total naive → optimized** | **-92%** | **~8% of naive** |
| `save_semantic_cache` after merge | next run free for unchanged | weekly runs ~free |

A naive run that would cost ~12M LLM tokens drops to ~1M for the first run, and the next weekly run is essentially free.

## Known gotchas

1. **First `detect()` call sometimes hangs at 0% CPU** for 7+ minutes (Python 3.14 + pipx env startup quirk). Always wrap detect calls with a 90-second `signal.alarm()` and retry once on timeout — the second call always works.
2. **Parallel subagent cap is ~10-12.** Dispatching more than that in a single message causes 429 rate limits on follow-up agent operations. The result writes usually succeed (you just lose the closing handshake message), but be aware that "agent failed" notifications may be misleading — always check the result file directly.
3. **`signal.alarm()` is process-global.** Don't span a single timeout across multiple stages of a script (build + cluster + cache). Set fresh alarms per stage or skip alarms inside the cache write step.
4. **The graphify cache uses `SHA256(file_contents + null + resolved_path)`.** This means moving a file to a new path invalidates its cache entry even if the content is identical. Plan accordingly when reorganizing.
5. **The 16 floor names (Shame...Peace) are baked in.** They come from the framework that ai-brain-starter installs in every vault. If you've added your own framework variants, add their lowercase names to `CANONICAL_FLOORS` in `graphify_prep.py` and to `LABEL_SUFFIX_VARIANTS` in `graphify_canonicalize.py`.
6. **Cache root path gotcha when running from a temp CWD.** If you run graphify on a source outside the current working directory (e.g. a team Google Drive vault while your CWD is `/tmp/graphify_onde_team/`), `save_semantic_cache` will silently write 0 entries. Reason: it does `Path(root) / fpath` to resolve each file, and if `fpath` is relative and `root` doesn't match the actual vault root, the files don't exist relative to CWD and get skipped. **Fix: normalize every `source_file` in the extraction JSON to an absolute path before calling the cache API:**
   ```python
   VAULT_ROOT = Path("/absolute/path/to/vault")
   for n in extraction["nodes"]:
       sf = n.get("source_file", "")
       if sf and not Path(sf).is_absolute():
           for prefix in [""]:  # extend with team-vault prefixes if running on a team substrate
               candidate = VAULT_ROOT / (prefix + sf)
               if candidate.exists():
                   n["source_file"] = str(candidate)
                   break
   # ... then call save_semantic_cache as normal
   ```
7. **Dispatching subagents on a cloud-synced source is slower.** Google Drive file reads are 2-5x slower than local file reads. On a 46-file team vault run, the biggest chunk (14 files) hung past 15 minutes while the smaller ones (7-13 files) finished in 4-7 minutes. Consider smaller target-chunks when the source is cloud-synced, or run agents in waves to avoid parallel cloud read contention.
8. **Subagent sandboxes can deny Write and Bash.** Some subagent runtimes (especially when the parent hits rate limits mid-dispatch) grant Read permission but deny Write/Bash. Symptoms: agent returns "chunk NN done: X nodes, Y edges" in its message BUT no result file exists on disk. **Fallback: the agent should inline the full JSON payload in its final message so the caller can recover it.** Add this to your prompt: *"If your sandbox denies Write/Bash, print the JSON payload in a ```json code fence in your final message, followed by the 'chunk NN done' line."* Then have a recovery script that parses the agent's output file (at `/tmp/.../tasks/<agent_id>.output`) for JSON payloads in assistant message blocks. See the team vault recovery snippet in OPTIMIZATIONS.md — we recovered chunk_04 that way after its sandbox denied Write.

### Recovering a failed-write chunk from the agent output file

```python
import json, re
from pathlib import Path

AGENT_ID = "a2a15811afd19aec2"  # from the harness notification
OUTPUT_FILE = Path(f"/private/tmp/claude-501/.../tasks/{AGENT_ID}.output")

text = OUTPUT_FILE.read_text()
events = [json.loads(l) for l in text.splitlines() if l.strip()]
for ev in reversed(events):
    msg = ev.get("message", {})
    if msg.get("role") != "assistant": continue
    for block in msg.get("content", []):
        if block.get("type") != "text": continue
        txt = block.get("text", "")
        m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", txt)
        if m:
            payload = json.loads(m.group(1))
            Path(".chunk_NN_result.json").write_text(json.dumps(payload))
            print(f"recovered: {len(payload['nodes'])} nodes, {len(payload['edges'])} edges")
            break
```

## When to update this doc

Every time you run graphify and discover something new — a duplicate pattern that wasn't caught, an agent prompt issue, a chunk size sweet spot, a new graphify version — update both this doc and the corresponding script. The first run of any process is always more expensive than the second; the goal of this doc is to make sure the second run is always cheaper than the first.
