---
type: runbook
---

# Graphify Lessons (1-118)

> Consolidated operational lessons from many runs. Scope: general-purpose, no vault-specific paths or data. Pair with `OPTIMIZATIONS.md` (patterns) and `RUNBOOK.md` (pipeline).

## Dedupe + preflight

**1.** Run `graphify_prep.py --apply` first. Pass A: `* 2.md` siblings. Pass B: cross-dir dupes. Combined reduction is often 60%+. Before deleting cross-dir dupes, grep the vault for subdirectory dependencies.

**2.** Wikilink regex pre-extraction yields MORE edges than LLM and is free. Roughly half of files have zero wikilinks (need full LLM); the other half have 10+ each. Always run prep first; tell agents to skip wikilinks.

**3.** Per-file scoped IDs cause node bloat. Agents emit file-stem-prefixed IDs; canonicalize merges by label. Always run `graphify_canonicalize.py` after merge.

**4.** Validate extractions before build. Agents invent invalid `file_type` values (`person`, `concept`, etc.; only `document/code/image/paper/rationale` valid). Use `validate_extraction()` + `force_valid_file_types()` + `clean_slash_label()` as pre-canonicalize pass.

**5.** Hyperedges silently disappear if prompt example shows `[]`. Request 1-3 per chunk (1-7 for concept-dense corpora, see #42).

**6.** First Python detect call hangs in some shells. Always wrap with 90s timeout and retry once.

**7.** Floor labels (or any canonical concept labels) come in multiple variants. Canonicalize normalizes them.

**8.** Build does not dedupe by label, only by ID. Canonicalize produces label-level dedup.

## Chunking + dispatch

**9.** Chunk-size-20 wastes prompt overhead (~1.5K tokens per chunk). Use 40-50 files per chunk for large batches. Never exceed 50 (see #81: 60+ causes schema collapse).

**10.** Word-balanced chunking, not alphabetical. Greedy bin-pack across word count.

**11.** Parallel cap ~10-12. For >12 chunks, dispatch in waves of 10 with 30-60s gaps. See #32 for tighter cap under usage pressure.

**12.** Skip `[AI Extract]` files (or any LLM-generated summaries) from LLM extraction. Regex handles their wikilinks; low inferred-edge yield.

**13.** Skip files <500 words from LLM extraction. Rarely have novel inferred edges beyond regex.

**14.** Chunk results stay valid even when agent's closing message fails with rate limit. JSON is written before close-out. Don't assume "agent failed" = "no data." Always check the file.

## Cache mechanics

**15.** `save_semantic_cache` MUST run before cleanup. Without it, next `--update` repays full cost. Cache keyed by `SHA256(content + null + resolved_path)`. Single most important optimization for repeated runs.

**16.** `save_semantic_cache` silently writes 0 entries when `source_file` paths are relative and CWD is a temp workdir. Fix: normalize every `source_file` to absolute paths before calling cache API.

**17.** Cache misses when corpus is re-rooted. Same content at different paths = 0 hits. Restructuring the vault invalidates cache.

**18.** Cache-hit detection must distinguish preflight stubs from real LLM extractions. `is_llm_extraction(cache_data)` discriminator: True iff (a) non-empty hyperedges OR (b) any edge/node has `confidence != "EXTRACTED"` OR (c) any edge has `confidence_score != 1.0`.

**19.** Cache upgrades are invisible by directory count. New LLM extraction overwrites matching preflight stub at same hash. Count entries with `mtime > now - 1h` AND `is_llm_extraction()` signature, not raw entry count.

## Performance + I/O gotchas

**20.** Remote-storage cold reads are 2-1000x slower. iCloud: 0.15s warm vs 200s+ for 1,000 files cold. Google Drive: large chunks hang 15+ min while small chunks finish in 4-7 min. Fix: `brctl download "<folder>"` before bulk ops; smaller `--target-chunks` for cloud-synced inputs. Smell test: 200 files in Python >5s = cold.

**21.** `signal.alarm` is process-global. Don't span timeouts across stages. Set fresh alarms per stage.

**22.** `file_hash` is slow (~3 min for ~500 files). Worth tolerating for cache correctness.

**23.** System `python3` does NOT have `graphify`. Always use your graphify venv's python for any script doing `from graphify.* import *`. Set `GRAPHIFY_PYTHON` env var to point at the venv's python3. System python3 works for `graphify_prep.py` and stdlib-only scripts.

**24.** graphify package API drifts. `suggest_questions`, `to_html`, `generate` need different args than older code. Build `community_labels` manually. `graphify_stage_finish.py` handles the drift.

## Tool-use efficiency (the big one)

**25.** Tool-use count is #1 per-chunk token cost predictor. Grep-first prompt cuts ~46% off baseline. Low tool-use chunks (≤15 calls) avg 100K tokens; high (≥35 calls) avg 160K. 37% waste, no quality loss. Prompt block in `templates/obsidian/graphify-extraction-prompt.md`: "Do NOT read files one at a time. Grep-first or batch all Reads in parallel." Target median ≤15, max 25, red flag 30+.

**26.** Subagents must set `source_file` to a specific .md path, never a directory. Breaks `save_semantic_cache` with `Errno 21: Is a directory`.

## Canonicalize + label hygiene

**27.** Path-form wikilinks don't canonicalize with bare-name wikilinks. `Folder/[[Topic]]` ≠ `Topic`. Fix: canonicalize strips folder prefixes from labels before hashing.

**28.** Long filenames become unreadable high-degree nodes. Cap file-stem labels at 60 characters with ellipsis.

**29.** Auto-patch slash labels before merge. `[[Name]]/Role Voices` etc. where `/` is "or"-separator. `clean_slash_label()` in finish script replaces `/` with `,` or `and` in non-path-shaped labels.

## Hyperedges + concept density

**30.** Hyperedge yield is consistent and high-quality. Pairwise edges can't capture these. Cap: 1-7 for concept-dense (Business, Writing, Strategy, Books, Notes); 1-3 for episodic (Journals, Daily Logs, Chat transcripts, CRM).

## Token budget + session orchestration

**31.** Per-stage token budget ≠ session usage budget. A sequence of stages totaling 2M+ tokens can exceed the rolling window. Track cumulative; default 2M per session. Above that, `/clear` and fresh session.

**32.** Wave-of-10 cap too generous under tight usage. When cumulative >1M, drop to waves of 3 with 60s gaps.

## Long-form / single-file routing

**33.** Long-form writing should use graphify's native chunker, not parallel-agent flow. Single-file mega-chunks make Grep-first useless. For long-form folders, use `/graphify "Writing/" --update`. Reserve parallel pipeline for episodic corpora.

## Subagent failure modes + recovery

**34.** Worktree-dispatched subagents can't write ANYWHERE on disk. Inline-JSON-in-message is the only escape. Applies to ALL absolute paths including /tmp. Recovery: parse agent output for either `tool_use{Write}.input.content` or triple-backtick `json` fences. Mitigations: (a) Dispatch from main session, not worktree (best fix). (b) Include fallback instruction in prompt: "If Write denied, inline complete JSON in json fence." (c) Verify `.chunk_NN_result.json` exists before merging.

## Stage-over-stage lessons (35-67)

**35.** Known-entity priming is a FOCUS DIRECTIVE, not an output filter. Primed chunks deliver ~60% higher concepts-per-Ktoken vs control. Tokens drop ~5%. Priming redirects attention to second-tier depth. Ship on every dispatch.

**36.** `new_concepts_per_ktoken` is the primary stage-over-stage efficiency KPI. Formula: `(new_nodes + new_edges) / (tokens / 1000)`. Track in `graphify_stage_finish.py`.

**37.** Concept whitelist for regex preflight pays for itself. Maintain a whitelist file in your graphify-out directory. Gains ~100 free edges per multi-corpus run. Maintenance: after each dispatch, scan LLM-only nodes for entities appearing in 3+ files, append to whitelist.

**38.** Anthropic prompt caching via `cache_control: ephemeral` saves hundreds of thousands of tokens per full run. Check SDK docs for exposure via Agent tool.

**39.** Priming validates at scale: repeatable ~0.93 concepts/Ktoken across different file lists with identical config. Sustained ~20% efficiency gain.

**40.** Hyperedge cap 5 undershoots on Notes-class corpora. Raise to 7 for concept-dense.

**41.** Efficiency converges fast across same-content chunks. One chunk's metric reliably estimates the rest within same content type.

**42.** Cap-7 hyperedges ships. Most concept-dense chunks hit cap with all interpretable; some self-restrain below cap (no Nth cluster exists). Zero padding. Self-restraint = cap functioning as upper bound, not target.

**43.** Schema drift requires post-recovery normalization. ~80% of agents improvise field names: `relationship` vs `relation`, `from`/`to` vs `source`/`target`, `members` vs `nodes`, numeric `confidence` vs split fields, custom relation names. Build a normalizer with REL_MAP for ~30 custom relations. Always normalize before merge.

**44.** Books are densest concept-per-file corpus. ~3x unique concepts per token vs journals. Prioritize concept-dense (Books/Writing/Strategy) over episodic when budget is tight.

**45.** Topic Seed (chunk-level theme detection) partially useful. Empty on Books (each book = different topic). Useful on Notes/School/Strategy where files share stem prefixes. Future: use TF-IDF over contents instead of stem detection.

## Multi-vault / wrong-root gotchas (46-54)

**46.** Wrong-root cache miss is the costliest beginner mistake. Symptoms: full folder flagged "new", 0 cache hits, 5-10x cost estimate. Always verify hit rate against a second data source.

**47.** `check_semantic_cache` hangs on cold cloud-synced inputs (6+ min at 0% CPU, re-hashes every file). Fix: after first cache check in session, never re-check. Read cache files directly, filter by mtime. No hashing, <1s.

**48.** Never rebuild graph from cache-only content; always merge into existing. Cache is a cost-optimization store, not a full graph spec. Missing cross-file inferred edges only exist in graph.json after merging. Correct path: load existing → build new from uncached → `G_existing.update(G_new)` → canonicalize → save.

**49.** Cleanup must happen AFTER successful graph save. Gate behind success flag. If intermediate step errors, leave temp files for recovery.

**50.** Python heredoc + emoji cwd paths hang zsh intermittently. Fix: write script to `/tmp/<name>.py`, then invoke separately. Two-step pattern 100% reliable.

**51.** `surprising_connections` and `suggest_questions` throw KeyError on dangling IDs after canonicalization. Wrap in try/except, fall back to empty lists. Graph itself is valid. Save graph.json BEFORE attempting analyzers.

**52.** Label-based canonicalization insufficient for file nodes with divergent labels but identical adjacency. Ship `graphify_dedupe_by_adjacency.py` with adjacency Jaccard ≥ 0.95, guards: MIN_DEGREE=8 (5 for file canonical), MIN_LABEL_OVERLAP=0.15, `pick_canonical()` prefers `file_*` over `c_*`.

**53.** Secondary vaults may place cache and graph.json in DIFFERENT directories (cache at vault root, graph at `<corpus>/Meta/graphify-out/`). Scripts must hard-code both paths correctly.

**54.** Brain-dump filenames create orphan nodes that can't be auto-canonicalized. Fix is upstream: use noun-phrase titles ≤5 words when creating notes. Rename existing brain-dumps when noticed.

## Session-start discipline

**57.** Skimming the runbook costs 30+ min per session. The STOP-READ directive is a hard gate.

**58.** Read-tool 10k-token cap: use offset+limit chunking, never Grep sampling. Three reads covers a long file.

**59.** `graphify_stage_select.py` reads FULL corpus. For capped requests ("N newest", "pick 300"), write a targeted picker: glob → sort by selection key without reading content → iterate ~1.4-2x cap → read each once for word count + cache. Selection keys: journals = YYYY-MM-DD filename parse; writing = `st_size`; CRM = `st_mtime`. Overshoot factors: journals 2.0x, writing 1.6x.

**60.** Filter-attrition overshoot: journals need 1.8-2.0x (72% attrition rate). Writing needs ~1.6x (36% attrition). Hard-code `OVERSHOOT_JOURNALS=2.0`, `OVERSHOOT_WRITING=1.6`.

**61.** 16-thread parallelism = 6-8x speedup for iCloud cold reads. `ThreadPoolExecutor(max_workers=16)` classified hundreds of files in ~90s vs ~10 min sequential. GIL releases during I/O. Sweet spot: 16 threads. Single-pass classify: read bytes once, word-count + hash from same bytes.

**62.** Scan for content-level duplicates in concept-dense corpora BEFORE chunking. Writing/Drafts folders are most likely to contain variants. Group by normalized-title Jaccard ≥0.8, flag groups of 2+. Merging 3 draft variants can save 100k+ words of redundant extraction.

**63.** When merging divergent drafts: normalize wikilinks + tags before comparison, preserve unique lines in "Recovered from earlier drafts" appendix (never silent-drop), keep backups at `/tmp/<topic>_merge_backup_YYYYMMDD_HHMM/`.

## From multi-chunk stages (64-84)

**64.** Main-session dispatch eliminates worktree Write-denial. Rule: ALWAYS dispatch from main session.

**65.** Concepts/Ktoken baseline is corpus-specific: Books/Notes/Psychology 0.85-1.10; Business/Strategy/Writing (multi-file) 0.75-1.00; Journals/Daily Logs 0.50-0.70; CRM 0.55-0.65. Kill criterion: <0.40 on any corpus type.

**66.** Single mega-file writing chunks behave differently than multi-file. 57k-word file: 0.48 concepts/Ktoken (below multi-file average). Quality is still high (7 hyperedges at cap). The "3x" claim (#44) holds for multi-file only. For >30Kw single files, route to native graphify chunker.

**67.** Cleanup must happen AFTER cache save. Correct order: validate → normalize → canonicalize → adjacency dedupe → merge → save graph → report → cache save → cleanup. ALL steps 1-8 before step 9.

**68.** Always exclude `Meta/` and `Archive/` from BOTH vaults. Filter: check for emoji-prefixed variants too.

**69.** Cap-7 generalizes to single-file writing chunks. A ~57k-word draft emitted 7 distinct multi-node hyperedges, zero padding. Ships for ALL concept-dense corpora.

**70.** Dangling edges accumulate after canonicalization. Fix: add prune step after dedupe: `clean_edges = [e for e in edges if e['source'] in node_ids and e['target'] in node_ids]`.

**71.** iCloud creates `" 2.json"` conflict copies when deleting files it's syncing. After bulk delete, always: `rm -f *" 2."*` and verify with grep.

**72.** `save_semantic_cache` crashes on directory source_files. Root cause: hyperedges with `source_file: "Books/"` (a directory). Fix: `p.exists()` → `p.is_file()` in cache.py + validator in finish script Step 1.

**73.** Complete post-dispatch pipeline ordering: (1) finish script, (2) verify cache clean, (3) verify report, (4) cleanup, (5) done. If any step fails, STOP.

**74.** Stale `_src`/`_tgt` edge metadata is root cause of `surprising_connections` KeyError. Canonicalize rewrites `source`/`target` but not internal `_src`/`_tgt`. Fix: add a step that strips stale fields after every dedupe.

**75.** Fully-cached corpora have no work to do. `graphify_stage_select.py` should report 0 eligible files and exit. Confirm and move on, don't dispatch anyway.

**76.** Reference-heavy corpora (Daily Logs, Roam-style) have lower efficiency floor (~0.49 concepts/Ktoken). Don't prioritize unless specifically asked.

**77.** Node growth decelerates past ~7K-8K nodes. Later stages add depth (edges, hyperedges), not breadth (nodes). Track edges/stage and hyperedges/stage instead of node count.

**78.** ~34% canonicalization reduction on journal batches (highest of any stage). Journals generate more redundant concept names. Expected behavior.

**79.** Dangling-edge prune catches real issues every run. One stage pruned 185 stale edges. Load-bearing step.

**80.** Operational efficiency: 5 min from "go" to "done" on 300-file batch. ~30s pick + ~170s dispatch + ~180s finish + ~5s cleanup ≈ 6.5 min.

**81.** 60+ files per chunk causes schema collapse. Half the chunks returned malformed JSON (node IDs as strings, edges as lists). Context saturation. Cap chunks at 50 files max. Never 60+.

**82.** Minimal prompt saves ~10% tokens with no quality loss. Ship as default for journal-heavy batches.

**83.** Mega-file agents can outperform multi-file chunks on framework-heavy books. A 45Kw book hit 1.02 concepts/Ktoken as single agent, higher than multi-file chunks. Route >30Kw to individual agents; expect 0.60-1.05 depending on content structure.

**84.** Self-help/spiritual books hit ~0.65 (below the 0.85 books baseline). Fewer named frameworks, more narrative. Revised sub-baselines: framework-heavy books 0.85-1.10, self-help/spiritual 0.60-0.75.

## Worktree + API key gotchas (85-86)

**85.** Worktree dispatch wastes entire batches. If you dispatch from a worktree, most agents can't write results to disk. Enforcement: add a pre-flight check. If `git rev-parse --is-inside-work-tree` returns true AND path contains `.claude/worktrees/`, STOP and switch to main before dispatching.

**86.** API keys must be exported before calling a preprocessor script. Keys in `~/.zshrc` may not be in the shell environment if `source ~/.zshrc` fails with compdef errors. Fix: grep the key from zshrc and export explicitly: `export X_API_KEY=$(grep X_API_KEY ~/.zshrc | cut -d'"' -f2)`. Better: store keys in a dotenv file that scripts source directly.

## Layout auto-detection + cache hygiene (87-104)

**87.** `graphify_stage_select.py` must auto-detect vault layout. Personal and secondary vaults have different `graphify-out/` structures. Ship: detect `<vault>/graphify-out/cache/` existence. If present plus `<vault>/<corpus>/Meta/graphify-out/`, use secondary layout (cache at vault root, chunks under corpus/Meta). Else use personal layout. Print `Layout: secondary|personal` for visibility.

**88.** Some plugins' `PreToolUse:Read` hooks silently replace Read output with a timeline plus just line 1 of the file. Workaround: Bash `cat` bypasses the hook. Durable fix: rename the plugin's hook key to disable it, run idempotently on session start.

**89.** `graphify_stage_select.py` may leak `Meta/` and `Archive/` folders into the eligible-files list. Symptom: 20-30% of "eligible" files are template docs, index files, archived drafts. Fix: `SKIP_PARTS = {"_review_alternate_drafts", "Meta", "Archive"}` plus trailing ` 2`/` 3` conflict-copy filter.

**90.** `graphify_stage_finish.py` needs full layout auto-detect (not just `--vault-root`). Checks for `<vault>/graphify-out/cache/` existence to pick secondary vs personal layout. Override flags: `--corpus-folder`, `--cache-dir`, `--chunk-prefix`, `--graph-path`, `--report-path`.

**91.** Concepts/Ktoken on re-extraction passes (where ≥50% of files already have preflight entries) falls to 0.40-0.60. Not a red flag; the LLM is adding inferred edges between existing nodes more than net-new nodes.

**92.** Tool-use counts on concept-dense corpora often exceed the 15-call target even with grep-first instructions. 21-24 is acceptable for concept-dense chunks with dense cross-file inference work; the 30+ red flag still holds. Split the grep block into "top 5 must-grep" vs "if relevant" to encourage smaller tool budgets.

**93.** SHA-only cache invalidation is too strict for actively-edited vaults. After days of normal editing, most cache entries go stale (SHA doesn't match). Fix: maintain `extraction_manifest.json` tracking `{absolute_path: {llm_time, sha, node_count, stage}}` after every finish run. Select short-circuits on `file.mtime <= manifest.llm_time + 5s slack` before falling back to SHA. Companion: `graphify_prune_stale_cache.py` for periodic cleanup of orphaned entries.

**94.** `graphify.cache` library hashes content + `\x00` + `resolved.relative_to(root)` when file is inside root, falling back to `str(resolved)` otherwise. Select and prune scripts must hash against the same variants. Any time you compute a SHA for a cache lookup, try BOTH relative-to-root and absolute path before declaring cache miss / stale.

**95.** `graphify_stage_finish.py` default paths must be VAULT-anchored. `Path(f"{base}/extraction_manifest.json")` resolves relative to CWD, not VAULT. Any time `base` is used to construct a filesystem path in finish script, it must be prefixed with `VAULT`.

**96.** After a partial cache prune, the extraction_manifest.json can lag behind the graph. Recovery: bootstrap manifest from chunk file lists (`.chunk_*_files.txt`). Write `{abs_path: {llm_time: now, sha: ..., node_count: 0}}` for every file in the chunk lists. Cost: one stat call per file, no API.

**97.** NetworkX `node_link_data()` writes edges under `"links"`, but graphify library uses `"edges"`. If you manually build a NetworkX graph from `graph.json` and write it back with `nx.node_link_data()`, the resulting JSON has an empty `"links"` and a stale `"edges"`. Normalization: after writing, check `if edges and not links: d['links'] = edges` (or vice versa). Better: never write graph.json manually with nx — always go through `graphify_stage_finish.py --num-chunks 0`.

**98a.** `save_semantic_cache` can write 0 entries silently. Root cause: `root / source_file` resolves relative paths against `VAULT/Meta/`, but source files like `Notes/Books/X.md` resolve to `VAULT/Meta/Notes/...` which doesn't exist. Fix: normalize source_files to absolute VAULT paths before calling `save_semantic_cache`.

**98b.** All `graph.json` writes in `graphify_stage_finish.py` must use `ensure_ascii=False, encoding="utf-8"`. Without it, emoji characters in node labels get escaped as `\ud83d\udc..` sequences.

**99.** `hyperedges` live in `graph.json` as a top-level key outside the NetworkX graph model. Any code that reads `graph.json`, builds a NetworkX graph from it, and writes the result back using `nx.node_link_data(G)` will silently drop the entire `hyperedges` list. Rule: NEVER write `graph.json` directly from a NetworkX object. If unavoidable: `data["hyperedges"] = existing_data.get("hyperedges", [])` before writing.

**100.** `graphify_stage_select.py` supports multiple corpus folders in a single invocation. Pass them as positional args. Bin-packing runs across the combined file list. Eliminates separate selects per folder.

**101.** `--max-files-per-chunk` guard (default 45). The bin-packer enforces a per-chunk file count ceiling. When a bin hits cap, overflow creates a new bin. Prevents #81 schema collapse. Previously, the only constraint was word count, so corpora with many short files could pack 90+ files into a single chunk.

**102.** `--out-prefix` must be resolved inside `out_dir`. When users pass `--out-prefix "notes"`, chunks should land as `.notes_chunk_NN_files.txt` in `out_dir`, not CWD.

**103.** Cache contamination grep (`grep -rl "marker|stub|fake|placeholder"`) false-positives on legitimate node labels containing substrings like "biomarker". Not a real signal unless the match is in a metadata field, not a label. Visual inspection required on hits.

**104.** `graphify_stage_finish.py` should accept `--vault-root` and `--report-title` args to avoid hardcoded vault paths.

## Coverage audit + source_file semantics (105-106)

**105.** `source_file` points to first-extraction site, not every file containing the concept. "Concept in graph" ≠ "file was extracted." Audit categories: CURRENT (path + SHA match manifest), MOVED (only basename appears), MISSING (no trace). Don't collapse them.

**106.** `graphify_stage_finish.py` under-records manifest two ways: (a) staged-path miss — `source_file: graphify-input/<flattened>.md` doesn't resolve to real file, 0 entries recorded; (b) preflight-only miss — files with only preflight wikilinks never appear in canon → no manifest entry → next audit flags MISSING. Fix: resolve via `VAULT / sf` then unflatten `A_B_C.md` with `_` → `/`; union staged sources with `.chunk_NN_files.txt`.

## Environment + multi-wave runs (107-108b)

**107.** `~/.zshrc` is interactive-only. Subprocesses (Claude Code Bash) never source it. Scripts grep-falling back to `~/.zshrc` for secrets silently fail. Canonical: put secrets in `~/.zshenv` (or a dotenv sourced by `.zshenv`). Walk: `env → .zshenv → .zsh_secrets → .zshrc → .zprofile → .bashrc → .bash_profile → .profile → .env`.

**108.** RUN `graphify_stage_finish.py`. Never hand-roll merge scripts. Hand-rolled pipelines get step ordering wrong. Finish script order: validate → canonicalize → dedupe → merge → dangling-prune → recluster → report → cache save → verify.

**108b.** `--chunk-prefix` resolves to CWD, not `<out_dir>`. Finish also hardcodes chunks starting at 01, so wave 2 (`.chunk_11_*`) fails "MISSING chunk 01". **Workaround:** rename wave chunks to `.chunk_01..N_*` before finish. **TODO:** add `--chunk-start`/`--chunk-end` (or `--chunk-dir`) params.

## Coverage semantics — what "stale" and "missing" actually mean (109-111)

**109.** Cloud sync (iCloud, GDrive, Dropbox) bumps mtimes without content edits. Coverage flags files as "stale" even when user never touched them. Causes: cloud re-downloads, OS indexing, Finder metadata touches. **Rule:** check SHA, not mtime, before concluding content changed. TODO: add SHA-based "truly stale" count to the audit.

**110.** Coverage audit SKIP_PARTS excludes folders like `AI Chats/`. Thousands of files never flagged missing. Folders with `type: ai-chat` frontmatter (not `[AI Extract]` body tag) are includable via direct `graphify_stage_select.py` targeting. **Fix:** add excluded folders to audit output with file counts.

**111.** "Missing" ≠ "valuable ungraphified content." Most missing files are stubs (<500w). Example: 933 missing → 58 eligible after ≥500w filter. **Rule:** run 500w filter against missing list before quoting counts. Real gaps = recent journals + active research, not legacy stubs.

## Corpus-specific baselines (112-113)

**112.** AI Chat exports denser than journals. Wave 1: ~1.20 concepts/Ktoken vs journals 0.50-0.70. Each chat is topically focused around one project/problem/decision → dense entity cluster. **Revised baseline: AI Chats 1.00-1.30 concepts/Ktoken.** High-ROI corpus despite being "AI-generated."

**113.** AI Chats need different extractor frame. Value isn't "what happened" (transient) but "what the user was thinking/deciding/building when they opened the chat." Prompt preamble: "Focus on TOPICS explored, DECISIONS made, CONCEPTS developed, PEOPLE/PROJECTS mentioned, PATTERNS. Ignore boilerplate AI responses." Ship as default.

## Pre-dispatch hygiene + gitignore (114-118)

**114.** Delete stale `.chunk_*_result.json` before any new dispatch. Prior runs leave results in `<out_dir>/` that the finish step merges silently. Pre-dispatch: `ls <out_dir>/.chunk_*_result.json` → `rm -f` leftovers. Same for `.chunk_*_files.txt`. Silent contamination otherwise.

**115.** Preprocessor scripts (MiniMax, other entity-extractors) are staging-folder-only. Direct-corpus dispatch (stage_select on vault paths like `"Books"`) doesn't read `.preextract.json` — subagents receive empty `{PREEXTRACT_BLOCK}`. Preprocess is wasted tokens + wall time for direct runs. Skip unless the workflow stages files into `<staging_dir>/` first.

**116.** Always dispatch a full wave (up to parallel cap, typically 10) in a single parallel message. Serial dispatch costs the same per-chunk and burns wall time with zero parallelism gain. Single-message dispatch is the only way to hit the cap.

**117.** (Reserved — environment-specific tooling lesson, not generalizable.)

**118.** `graphify-out/` is typically gitignored. graph.json, GRAPH_REPORT.md, COVERAGE_REPORT.md cannot be committed. Only files outside it (insights markdown, CLAUDE.md, scripts) go into snapshots. `git add <out_dir>/...` is a silent no-op. Confirm with `git check-ignore <path>` if uncertain.

## Report hygiene inside a real vault (119-121)

**119.** The report's `## Community Hubs (Navigation)` section links one `[[_COMMUNITY_*]]` note per community, and those notes exist ONLY in the opt-in Obsidian export (Step 6). On a default run every link is unresolved, and Obsidian renders an unresolved link as a graph node. Measured on a 14,046-node vault: 2,695 grey placeholder nodes radiating from one file, swamping the user's real graph. Always run `scripts/graphify_report_sanitize.py` after ANY `report.generate()` — including Step 5, which rewrites the whole file and otherwise reinstates every ghost. Hand-cleaning the markdown does NOT hold: the next run regenerates it. Verify with `--check` (exit 1 on any surviving ghost).

**120.** Most detected "communities" are not topics. On the same vault, 51% held a SINGLE node and 96% held ≤4 — extraction fragments. Only 101 communities had ≥5 nodes, and those covered 70% of all nodes. Label above the floor (≥5 nodes, ceiling 50 communities); listing the tail as navigation is noise, and naming it is wasted LLM spend.

**121.** Never seed labels as `Community {cid}` — whatever labeling doesn't finish stays a bare index forever, which is what users see. `scripts/graphify_seed_labels.py` names each community after its highest-degree member, skipping dates / phone numbers / attachment stubs that win on degree while saying nothing ("Money", "Fear", "Learning", not "Community 0..2"). For a report already generated with placeholders, relabel WITHOUT re-running extraction:

```bash
python3 "{SKILL_DIR}/scripts/graphify_report_sanitize.py" graphify-out/GRAPH_REPORT.md \
  --relabel-from graphify-out/graph.json
```

Only placeholder names are touched; a real semantic label is never clobbered.

## Standing rules

### Active lesson capture

Capture optimizations and gotchas THE MOMENT they surface, not at session-end. Stop and append to this file before continuing. Triggers: unexpected cost/time delta, new failure mode, workaround that worked, content-level pattern tooling can't auto-detect, any "I should remember this next time" moment.

### Every batch includes validation hypotheses

Every handoff doc must include "What this run is testing" with numbered hypotheses, predictions, measurement methods, and kill criteria. Table format: `# | Hypothesis | Prediction | How to measure | Kill criterion`. Target 4-8 per stage. Post-dispatch: append results with `measured value → verdict (SHIPS/REVISE/KILL) → runbook update action`.
