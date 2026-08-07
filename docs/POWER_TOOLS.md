# Power Tools Catalog

The third-party Claude Code skills, MCP servers, and Obsidian plugins that make this setup actually work.

`/setup-brain` installs most of these automatically in Phase 0. This doc is the **why** behind each one — what it does, when to use it, and where it came from. Read it if you want to understand the stack, customize the install, or pick a subset.

Nothing in this catalog is built by this repo. They're all open-source tools by other people that this setup integrates and recommends. Attribution and source links are included for every entry.

---

## Skills (Claude Code)

These get installed into `~/.claude/skills/` and become available as `/skill-name` commands.

### Session close cascade — automatic context capture on goodbye

**What it does:** When you finish a session ("bye", "thanks that's all", "good night", "/wrap-up", emoji-only farewells, or any equivalent in EN/ES/PT), a 3-layer pipeline saves your decisions, journal seeds, to-dos, time tracking, and a rebuilt session/decision log — without you typing a special command. Layer 1 is a UserPromptSubmit hook that detects close signals via regex against language packs and pre-resolves all paths before the model sees the prompt. Layer 2 is the model writing captures into a pre-built session file shell. Layer 3 is a Stop hook that runs aggregators, performs a targeted git snapshot, sweeps retention, and fires a Haiku 4.5 fallback if the model bails on the cascade (so context never gets silently lost).

**Why it matters:** the prior architecture relied entirely on the model "noticing" closing signals and choosing to read a separate cascade rule file before responding. Three brittle steps (notice → read rule → execute), any one of which could fail silently. Now detection is deterministic, the model only does the irreducibly creative conversation-scan work, and a Haiku backstop guarantees no silent loss even if the primary model bails.

**Install:** ships with `/setup-brain`. The hook entry lives in `hooks.json`, the detector in `hooks/detect-closing-signal.py`, the language packs in `templates/closing-signals/`, and the rest in `scripts/`.

**Trigger:** any natural-language close, OR explicit detector keywords `/close`, `/wrap-up`, `/bye`, `/cerrar`, `/tchau`.

**Configuration:** add `closingSignals.custom: [...]`, `closeDetection: hybrid`, or `sessionCloseFeedback: minimal` to your CLAUDE.md frontmatter. Full reference in [`docs/SESSION_CLOSE.md`](SESSION_CLOSE.md).

**Recovery:** `python3 ~/.claude/skills/ai-brain-starter/scripts/recover-last-close.py` (resume after partial close). `python3 ~/.claude/skills/ai-brain-starter/scripts/undo-last-close.py` (rollback).

**Testing:** `python3 ~/.claude/skills/ai-brain-starter/scripts/test-closing-signals.py` runs 74 fixtures.

---

### graphify — knowledge graph from any folder

**What it does:** Turns any folder of markdown files (or code, or papers) into a navigable knowledge graph with community detection, god-node ranking, and surprising connections. The output is one HTML graph + one JSON dump + one `GRAPH_REPORT.md` summary.

**Why it matters for a vault:** Once you have ~500 notes, you stop being able to hold the whole structure in your head. Graphify gives you a map. The `GRAPH_REPORT.md` becomes the **first thing Claude reads** for any strategic question — way faster and more accurate than reading individual files. On a 4,700-file personal vault, it cuts cross-concept research from "read 10 files" to "read one report."

**Install:**
```bash
pipx install graphifyy
graphify install
```

The Claude Code skill (in `~/.claude/skills/graphify/`) wraps the CLI with optimization scripts. `/setup-brain` Phase 0 installs both. The full pipeline + lessons learned is in [`skills/graphify/RUNBOOK.md`](../skills/graphify/RUNBOOK.md).

**Trigger:** `/graphify <folder>` to build, `/graphify <folder> --update` to incrementally update.

**Source:** [graphifyy on PyPI](https://pypi.org/project/graphifyy/) (the underlying package). The Claude Code skill in this repo wraps it with vault-specific optimizations.

---

### humanizer — remove AI writing patterns from text

**What it does:** Detects and removes the telltale signs of AI-generated writing — em dash overuse, "rule of three" pacing, vague attributions, promotional inflation, filler phrases, "it's not just X, it's Y" parallelisms, AI vocabulary words. Based on Wikipedia's "Signs of AI writing" maintained by WikiProject AI Cleanup.

**Why it matters for a founder:** every external doc you write — pitch deck, investor email, blog post, landing page — needs to sound like a human. AI-flavored writing actively hurts you in fundraising contexts. Investors are pattern-matchers and "this reads like ChatGPT" is a fast way to lose trust.

**Recent versions add:**
- **Pre-flight doc-type detection** — pitch decks need different rules than blog posts (em dashes are intentional beats in pitches)
- **Mandatory voice calibration** — loads your existing writing as a reference before editing, so it doesn't flatten your style
- **Spanish-language rule library** — handles Spanglish and bilingual docs without English-flattening
- **AI-iness density check** — adapts pass strength (light/mixed/full) to how AI-flavored the input is

**Install:**
```bash
git clone https://github.com/adelaidasofia/humanizer.git ~/.claude/skills/humanizer
```

`/setup-brain` Phase 0 installs this automatically.

**Trigger:** `/humanizer <file>` or `/humanizer "<paragraph>"`.

**Source:** Originally [blader/humanizer](https://github.com/blader/humanizer); the fork at [adelaidasofia/humanizer](https://github.com/adelaidasofia/humanizer) adds the pre-flight, voice calibration, and Spanish rules. MIT licensed.

---

### nano-banana — image generation via Gemini 3 Pro Image

**What it does:** Generate, edit, and compose images using Google's Gemini 3 Pro Image model (Nano Banana Pro). Supports text-to-image, image editing, multi-image composition (up to 14 reference images), iterative refinement via chat, and Google Search-grounded image generation for real-time data visualization.

**Why it matters for a founder:** every founder needs visuals constantly — pitch deck slides, social media assets, product mockups, blog post headers, logo iterations, infographics. The traditional answer is "open Canva" or "DM your designer." This skill lets Claude generate them in-chat from a one-line description, with aspect ratios, resolutions, and refinement built in.

**Examples:**
```bash
python scripts/generate_image.py "Clean black-and-white logo with text 'Acme', sans-serif, minimalist" logo.png --aspect 1:1
python scripts/generate_image.py "Studio-lit product photo on polished concrete, 3-point softbox" hero.png --aspect 16:9 --size 4K
python scripts/edit_image.py photo.png "Add a sunset to the background" edited.png

# Two helper scripts added 2026-05-24 (cherry-picked from MIT-licensed
# Open-Generative-AI + SamurAIGPT/Generative-Media-Skills audits):

# cinema_prompt_builder.py — semantic + technical cinematic vocab
# Two composable layers: intent (semantic feeling-to-directive) + technical (camera/lens/focal/aperture)
python scripts/cinema_prompt_builder.py "founder portrait at golden hour" --intent introspective --focal-length 85mm --aperture f/1.4
python scripts/cinema_prompt_builder.py "a lone samurai in a blizzard" --intent epic --generate samurai.png
python scripts/cinema_prompt_builder.py --list  # show all intents + camera/lens/focal/aperture vocab

# blog_header.py — Substack/blog/OG header with structured deliverable
# Returns image + alt-text + title-placement guidance; auto-strips banned keyword-soup
python scripts/blog_header.py "10 productivity hacks for remote founders" --aspect 16:9 --out hacks.png
python scripts/blog_header.py "essay on the loop" --style "warm amber, editorial" --aspect 21:9 --out loop.png
```

**Companion patterns (cherry-picked from MIT audits):** before any image-gen call, check the 8 sharp edges — anti-keyword-soup (`8k`/`masterpiece`/`ultra-detailed` degrade output, strip them), anti-text-in-prompt (text rendering is unreliable; use Canva for overlays; short literal in double quotes only), positive framing not negatives ("ensure sharp focus" not "no blurry"), describe physical relationships not isolated tokens, character continuity requires verbatim repetition (no cross-image memory), Perfect Prompt formula assembles six layers (Subject + Action + Context + Composition + Lighting + Style), intent → framing+movement+lighting (use `cinema_prompt_builder.py --intent`), multi-output deliverable for blog/OG/thumbnail (image + alt-text + title-placement, use `blog_header.py`). Codify these in your own vault patterns folder once you've audited the source repos.

**Install:** Adds to Claude Code via the [devon-claude-skills marketplace](https://github.com/devonjones/devon-claude-skills):
```bash
/plugin marketplace add devonjones/devon-claude-skills
/plugin install nano-banana@devon-claude-skills
```

You also need a `GEMINI_API_KEY` environment variable from [Google AI Studio](https://ai.google.dev/).

**Source:** [devonjones/devon-claude-skills](https://github.com/devonjones/devon-claude-skills) (Devon Jones). The original standalone repo is archived; the marketplace is the active home.

---

### obra/superpowers — work-discipline skills (TDD, worktrees, debugging)

**What it does:** A bundle of generic engineering-discipline skills by Jesse Vincent. The ones that pair best with this substrate:

- `test-driven-development` — red-green-refactor with an iron-law "no production code without a failing test first" stance. Pairs with project-specific TDD skills (e.g., a Vitest+pytest variant for a multi-runtime codebase).
- `using-git-worktrees` — isolated worktree creation with safety verification. Pairs with the worktree-vs-main-vault path discipline this substrate already enforces via PreToolUse hooks.
- `root-cause-tracing` — systematic upstream-trace from a deep error to its original trigger.
- `systematic-debugging` — disciplined hypothesis-instrument-fix-regress loop for hard bugs.
- `verification-before-completion` — pre-handoff audit pattern that mirrors this substrate's session-end cascade philosophy.
- `brainstorming` — structured questioning that turns a rough idea into a design. Complements `/deconstruct` (first-principles) by sitting one step earlier in the funnel.

**Why it matters:** the substrate handles memory, voice, vault, and session lifecycle. obra/superpowers handles the *engineering work itself* — the discipline of how code gets shipped. Together they cover both halves of a founder-engineer's day.

**Install:**
```bash
git clone https://github.com/obra/superpowers.git ~/.claude/skills/superpowers
```

Each skill in `skills/<name>/` is auto-discovered by Claude Code. After cloning, restart Claude Code so the SKILL.md files load.

**Source:** [obra/superpowers](https://github.com/obra/superpowers) by Jesse Vincent. MIT licensed. 184k stars at time of writing.

---

### Eng-discipline cycle (the obra superpowers as a unit)

**What it is:** four obra/superpowers skills compose into a single engineering discipline that the substrate's TDD and modern-Python substrates both anchor against. Use them together, not piecemeal.

| Step | Iron law | Skill |
|---|---|---|
| 1. Design before code | NO IMPLEMENTATION ACTION UNTIL DESIGN APPROVED | `obra:brainstorming` |
| 2. Test before code | NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST | `obra:test-driven-development` |
| 3. Root cause before fix | NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST | `obra:systematic-debugging` (paired with `obra:root-cause-tracing` on deep cascades) |
| 4. Evidence before completion | NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE | `obra:verification-before-completion` |

**Why it matters as a unit:** any one skill in isolation slips. Brainstorming without TDD produces designed-but-untested code. TDD without verification produces "tests pass, ship" claims that miss what the tests don't cover. Verification without root-cause produces fixes that suppress symptoms. The cycle locks in a sequence where each step gates the next.

**How the substrate uses it:**
- Substrate-level enforcement: PreToolUse hooks ensure no destructive git operation runs with an in-flight uncommitted module (the verification-before-completion step's safety net).

**When to invoke each:**
- `/brainstorming` — at the start of ANY non-trivial feature or refactor, before touching code
- `/test-driven-development` — when implementing a feature or fixing a bug, before writing implementation code
- `/systematic-debugging` — when a bug, test failure, or unexpected behavior appears, before proposing a fix
- `/verification-before-completion` — when about to claim work is complete, fixed, or passing, before committing or PR

**Trigger pattern:** for a feature build with no surprises, the cycle runs forward (1 → 2 → 4). For a debug session, it usually runs (3 → 2 → 4) — root-cause first, then test the proposed fix, then verify. For a "does this work?" pass before merge, it runs (4) standalone.

**Anti-pattern this prevents:** "I implemented X, ran the tests, looks good, shipping" — three steps but missing the design (1), the failure-first discipline (2 demands a failing test), the post-fix verification (4 demands fresh evidence post-change). The substrate's session-end cascade is the periodic enforcement of step 4.

---

## Vendor-published agent-skill bundles (engineering + operations)

The substrate ships memory, voice, vault, and session lifecycle. These vendor-published bundles cover the engineering-side skills a serious build benefits from. `bootstrap.sh` installs them via Claude Code's native plugin marketplace mechanism (skip with `SKIP_VENDOR_SKILLS=1` for air-gapped installs). Each vendor maintains their own SKILL.md per platform; we do not fork, repackage, or redistribute.

**Install path: `claude plugin marketplace add <repo>` + `claude plugin install <name>@<marketplace>`.** Plugin install is the ONLY path that registers nested SKILL.md files for Claude's auto-discovery. Raw `git clone` into `~/.claude/skills/` does NOT work for bundles with nested SKILL.md (the most common shape) because Claude auto-discovery only finds top-level SKILL.md. Codified 2026-05-10 after audit caught 7 cloned bundles invisible to Claude despite being on disk.

**Licenses verified 2026-05-10.** Two non-MIT cases: Trail of Bits is CC-BY-SA-4.0 (attribution + share-alike on docs), and Vercel-labs has no LICENSE file (all-rights-reserved by default per `⚙️ Meta/rules/license-hygiene.md`). Plugin install is a user-side fetch from each vendor's GitHub, which is fair use; redistribution by `ai-brain-starter` is explicitly NOT done.

### Sentry SDK skills — production error tracking and AI monitoring

**Repo:** [getsentry/sentry-skills](https://github.com/getsentry/sentry-skills). **License:** Apache 2.0. **Stars:** 681.

**Why install:** if you are running a real backend or shipping a Next.js product, you need stack traces with breadcrumbs, not "the user said it broke." The bundle covers 28+ language-specific SDK skills (`sentry-python-sdk`, `sentry-nextjs-sdk`, `sentry-cloudflare-sdk`, `sentry-react-sdk`, plus 24 more) and a dedicated `sentry-setup-ai-monitoring` skill that instruments Anthropic, OpenAI, Vercel AI, LangChain, Google GenAI, and Pydantic AI calls.

**Install:** automatic via `bootstrap.sh`. Manual: `claude plugin marketplace add getsentry/sentry-skills && claude plugin install sentry-skills@sentry-skills`.

### Trail of Bits skills — Python toolchain + security primitives

**Repo:** [trailofbits/skills](https://github.com/trailofbits/skills). **License:** CC-BY-SA-4.0 (Creative Commons Attribution Share-Alike). **Stars:** 5,095.

**Why install:** Trail of Bits is a high-trust security firm with deep Python tooling expertise. The bundle covers 22 skills; we install `insecure-defaults` (detect hardcoded secrets, default credentials, weak crypto), `sharp-edges` (error-prone APIs and dangerous configurations), `static-analysis` (CodeQL + Semgrep + SARIF), `property-based-testing`, and `differential-review` (security-focused diff review with git-history analysis). `modern-python` (uv + ruff + ty + pytest) lives in the same bundle but is **not** auto-installed — it ships a `python3` PATH shim that would silently break the vault's own hooks; see the install note below.

**License caveat:** CC-BY-SA-4.0 is copyleft on documentation. Cloning into your own `~/.claude/skills/` is fine. **Forking the documentation into a derived work requires keeping the same license.** Do not bundle Trail of Bits content into MIT-licensed downstream repos without preserving CC-BY-SA-4.0.

**Install:** automatic via `bootstrap.sh` (installs 7 relevant plugins from the marketplace bundle: insecure-defaults, sharp-edges, property-based-testing, static-analysis, testing-handbook-skills, differential-review, ask-questions-if-underspecified). Manual: `claude plugin marketplace add trailofbits/skills && claude plugin install <name>@trailofbits` per plugin.

**`modern-python` is intentionally excluded from the default install.** Its SessionStart hook prepends a PATH shim for `python3`/`python` that prints `ERROR: use uv run python3` and exit-1s on every bare invocation. The vault's own hooks call bare `python3` (with `|| true` / `2>/dev/null`), so with that shim active the entire hook layer — session close, the write-time secret guard, context loaders, aggregators — would silently no-op. It is Python-dev tooling, not knowledge-worker tooling, and a footgun for non-developers. The substrate is nonetheless hardened to coexist with the shim (hook commands resolve an absolute interpreter at install time; shell scripts strip `*/hooks/shims` from PATH), so if you do Python development it is safe to add back: `claude plugin install modern-python@trailofbits`.

### Stripe agent-toolkit — billing integration discipline

**Repo:** [stripe/agent-toolkit](https://github.com/stripe/agent-toolkit). **License:** MIT. **Stars:** 1,541.

**Why install:** if you are integrating Stripe (subscriptions, one-off charges, Connect transfers), the official toolkit ships `stripe-best-practices` (idempotency-key handling, webhook signing verification, error-handling patterns) and `upgrade-stripe` (SDK + API version bumps without silent breakage). Prevents the most common production bugs: double charges, missed webhooks, broken upgrades.

**Install:** automatic via `bootstrap.sh`. Manual: `claude plugin marketplace add stripe/agent-toolkit && claude plugin install stripe@stripe`.

### Cloudflare skills — Core Web Vitals + Workers/D1/R2/Wrangler

**Repo:** [cloudflare/skills](https://github.com/cloudflare/skills). **License:** Apache 2.0. **Stars:** 1,486.

**Why install:** the bundle includes `web-perf` (Core Web Vitals + render-blocking audits, stack-agnostic — works for static, Next.js, Astro), plus `workers-best-practices`, `durable-objects` (stateful coordination with RPC + SQLite + WebSockets), `wrangler` (deploy KV, R2, D1, Vectorize, Queues, Workflows), `agents-sdk` (build stateful AI agents with scheduling, RPC, MCP), `sandbox-sdk` (isolated code execution on Workers), and the comprehensive `cloudflare` platform skill.

**Install:** automatic via `bootstrap.sh`. Manual: `claude plugin marketplace add cloudflare/skills && claude plugin install cloudflare@cloudflare`.

### AgriciDaniel/claude-seo — comprehensive SEO + GEO toolkit

**Repo:** [AgriciDaniel/claude-seo](https://github.com/AgriciDaniel/claude-seo). **License:** MIT. **Stars:** 6,276.

**Why install:** 25 sub-skills + 18 sub-agents covering technical SEO, on-page analysis, content quality (E-E-A-T), content briefs, schema markup, image optimization, sitemap architecture, AI search optimization (GEO, the post-LLM successor to SEO), local SEO, semantic topic clustering, search experience optimization (SXO), SEO drift monitoring, e-commerce SEO, international SEO with cultural profiles, Google SEO APIs (Search Console, PageSpeed, CrUX, GA4), and PDF report generation. Required for any consulting practice or public site that needs to be findable.

**Install:** automatic via `bootstrap.sh`. Manual: `claude plugin marketplace add AgriciDaniel/claude-seo && claude plugin install claude-seo@agricidaniel-claude-seo`.

### yusufkaraaslan/Skill_Seekers — CLI tool that auto-converts docs sites into skills

**Repo:** [yusufkaraaslan/Skill_Seekers](https://github.com/yusufkaraaslan/Skill_Seekers). **License:** MIT. **PyPI:** [`skill-seekers`](https://pypi.org/project/skill-seekers/). **Stars:** 13,388.

**Why install:** converts documentation from 17 source types into production-ready SKILL.md format for 24+ AI platforms. Each time you adopt a new vendor SDK or API with public docs (Stripe, Resend, MongoDB, internal tool), this CLI saves the manual SKILL.md authoring step. Velocity multiplier for skill creation.

**Important:** this is a CLI TOOL, not a SKILL.md-format skill. It does not auto-load when present in `~/.claude/skills/`. Use it as a command-line generator, then commit the resulting SKILL.md into the appropriate skill directory.

**Install:** automatic via `bootstrap.sh` using `pipx install skill-seekers`. Invocation: `skill-seekers <docs-url>` (see [`skillseekersweb.com`](https://skillseekersweb.com/) for full usage).

### yvgude/lean-ctx — session caching + AST compression + token reduction

**Repo:** [yvgude/lean-ctx](https://github.com/yvgude/lean-ctx). **License:** Apache 2.0. **Stars:** 1,425.

**Why install:** MCP server and context runtime for AI coding agents. Session caching, AST-aware compression, and 90+ shell patterns to reduce token usage. Direct fit for the substrate's token-optimization line. Pairs with `docs/TOKEN_OPTIMIZATION.md`.

**Install:** automatic via `bootstrap.sh`. Manual: `git clone https://github.com/yvgude/lean-ctx.git ~/.claude/skills/lean-ctx`. Then `lean-ctx init --agent claude-code` per upstream docs.

### Vercel labs agent-skills — Next.js + React patterns

**Repo:** [vercel-labs/agent-skills](https://github.com/vercel-labs/agent-skills). **License:** **NO LICENSE FILE** (all-rights-reserved by default). **Stars:** 26,343.

**Why install:** the bundle covers `next-best-practices`, `next-cache-components` (cache-aware composition patterns that determine whether hot routes are fast or slow), `next-upgrade` (Next.js version bumps), `react-best-practices`, `composition-patterns`, `web-design-guidelines`, and `react-native-skills`. Vercel's own engineering team maintains it.

**License caveat:** Vercel-labs has not added a LICENSE file. Per `⚙️ Meta/rules/license-hygiene.md`: "No LICENSE file: treat as all-rights-reserved. Reading is fine; copying is infringement." Bootstrap-clone is a user-side fetch from Vercel's own GitHub (fair use). **Do NOT fork into a derived work or redistribute.** If Vercel-labs adds a license later, this caveat updates.

**Install:** automatic via `bootstrap.sh`. Manual: `git clone https://github.com/vercel-labs/agent-skills.git ~/.claude/skills/vercel-agent-skills`.

### Catalog source

These adjacents were surfaced via [VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills) (21k stars, MIT, hand-curated). The full catalog covers 1,100+ skills from official teams plus community contributions. The monthly `~/.claude/scheduled/scan-awesome-repos.sh` job watches this and four other awesome-* repos for new entries; reports land in `⚙️ Meta/Awesome Repo Scan/<YYYY-MM>.md`.

---

## Repo-shipped CLI tools

Standalone scripts this repo bundles at `scripts/` that you run directly against any git repo — not Claude skills, no setup required beyond a Python interpreter.

### ci-cost-audit.py — find CI spend leaks before they become an invoice

**What it does:** GitHub Actions bills per JOB, rounded up to a whole minute, times the runner multiplier (Linux 1x, Windows 2x, macOS 10x) — never per workflow, never per wall-clock minute. An AI brain opens many branches at once, so the shape that empties an Actions budget is a wide fan-out of very short jobs multiplied by branches in flight, not a slow test. `ci-cost-audit.py` reports that shape for any repo: per-job floor waste, expensive runners on PR triggers, missing `concurrency` groups, `cancel-in-progress` that would deadlock a merge queue, duplicate push+pull_request triggers, and relevance predicates broad enough to match ordinary churn (scored against real file counts, so a correctly-narrow predicate is not flagged).

**Why it matters:** measured on a live fleet, one 11.5-hour window ran 2,967 jobs for $81.84 — macOS was 3.5% of the jobs and 41% of the bill, and 20% of the total was pure per-job rounding on jobs whose median runtime was 6 seconds. That is the default failure mode of any repo an agent fleet drives, and it is invisible until someone measures it.

**Use it:**
```bash
python3 scripts/ci-cost-audit.py                       # static check, current repo
python3 scripts/ci-cost-audit.py --path ../other-repo   # static check, elsewhere
python3 scripts/ci-cost-audit.py --repo owner/name --hours 24   # + real billed minutes via gh
python3 scripts/ci-cost-audit.py --self-test            # negative controls, no repo needed
```

Static mode needs no network access or credentials. Measured mode needs the `gh` CLI authenticated against the target repo.

**Run it:** once per repo when GitHub Actions billing looks off, or periodically as a client install's agent fleet grows — the cost shape scales with how many branches are open at once, not with how big the codebase is.

---

## Cheap model APIs

When the task is mechanical — extract entities, summarize a doc, classify notes — burning Opus tokens is wasteful. A cheap reasoning model costs 100–150x less and is sufficient.

### MiniMax M2.7 — cheap text processing

**What it does:** A fast, cheap reasoning model good at extraction, classification, summarization, and boilerplate generation. Not a replacement for Claude on judgment-heavy work, but a workhorse for grunt-work text processing.

**Why it matters:** Entity extraction across a 500-file vault: ~$0.30 on MiniMax vs ~$45 on Opus. For batch operations (graphify pre-processing, transcript entity extraction, bulk note tagging), the savings compound.

**Cost:** ~$0.06/M tokens at [platform.minimax.io](https://platform.minimax.io) (create a free account, add credits).

**Install:** This repo ships `scripts/minimax.sh`. After getting your API key:
```bash
export MINIMAX_API_KEY="your-key-here"  # add to ~/.zshrc
chmod +x scripts/minimax.sh

# Test it
./scripts/minimax.sh "Summarize this in 3 bullet points: Claude Code is a terminal-based AI coding assistant built by Anthropic."
```

**Route to MiniMax when:** extracting structure from raw text (meeting transcripts, docs), bulk-classifying or tagging vault notes, generating boilerplate from a template, summarizing a single document with no voice requirement.

**Route to Sonnet/Opus when:** judgment calls, writing in your voice, cross-file synthesis, anything with ambiguity.

See [`docs/TOKEN_OPTIMIZATION.md`](TOKEN_OPTIMIZATION.md) for the full routing guide.

---

## MCP servers

MCP (Model Context Protocol) servers extend Claude Code with structured tool access to external systems. Configured in `~/.claude/.mcp.json`.

### Granola — meeting transcript export

**What it does:** [Granola](https://granola.ai/) records and transcribes Zoom/Meet/Teams calls with AI-generated summaries. `scripts/granola_sync.py` pulls the full timestamped transcript from Granola's official Public API and writes it to your vault's meeting notes folder — no MCP needed (a Granola API key is required). The meeting workflow rule in CLAUDE.md (see SKILL.md Phase 4) uses this to auto-cascade meeting takeaways into:

- The meeting note itself (enriched with decisions, action items, verbatim quotes)
- The CRM contact files for every attendee (last_interaction updated, meeting note linked)
- Your team to-do file (action items extracted and assigned)
- Canonical strategy/pitch docs (decisions cascaded to the relevant doc)

**Why it matters:** without this, you spend 20 minutes after every meeting transcribing handwritten notes and updating CRM cards. With it, Claude reads the full transcript and does the cascade in one command.

**Requires a Granola plan with API access.** Open Granola → Settings → Connectors. If there is no **API keys** section, your plan does not include the API — use Google Meet + Gemini, Otter, or manual notes instead.

**Install:**
1. Generate a Granola API key: Granola → Settings → Connectors → API keys. Save it to `~/.config/granola/api-key` (chmod 600), or export `GRANOLA_API_KEY`.
2. Verify + preview: `python3 scripts/granola_sync.py --health`, then `python3 scripts/granola_sync.py --dry-run`.
3. For auto-export every 2 hours, install the LaunchAgent:
   - Copy `scripts/com.granola-export.plist` to `~/Library/LaunchAgents/`
   - Edit the script path + log path inside it
   - Run: `launchctl load ~/Library/LaunchAgents/com.granola-export.plist`

**Note on speaker labels:** The API labels your microphone channel as **You** and the other side as **Speaker** (no per-person diarization). The Granola-generated summary is also included in the exported file.

**Source:** Granola's official Public API (`public-api.granola.ai/v1`).

---

### ChatPRD — product specs and PRDs from Claude Code

**What it does:** [ChatPRD](https://www.chatprd.ai/) is an AI tool purpose-built for product requirements documents. The MCP integration lets Claude Code create, search, read, and update PRDs in your ChatPRD workspace without leaving the terminal. You can say "create a PRD for the venue search feature" and Claude writes it directly into ChatPRD.

**Why it matters:** ChatPRD has templates, version history, and shareable links for stakeholders. It's purpose-built for specs in a way that markdown files in Obsidian aren't. The MCP makes it accessible from the same place you do everything else.

**Install:** Add to your vault `.mcp.json` (the `.mcp.json` file at your vault root, NOT `~/.claude/.mcp.json`):

```json
{
  "mcpServers": {
    "ChatPRD": {
      "type": "http",
      "url": "https://app.chatprd.ai/mcp"
    }
  }
}
```

Then open Claude Code and use any ChatPRD tool — it will prompt you to authenticate via OAuth. One-time setup, then it stays connected.

**Requires:** A ChatPRD account at [chatprd.ai](https://www.chatprd.ai/).

**Source:** ChatPRD team. HTTP MCP — no server to run locally.

---

### health-mcp — Apple Health into the substrate (v0.2)

**What it does:** Imports the full Apple Health surface (108 quantity types + 47 symptom types + 14 cycle / reproductive types + ECG + iOS 17 State of Mind) into local DuckDB and exposes 32 tools across 8 categories: ingestion (XML / Simple Health Export CSV / Health Auto Export TCP-live + lab CSV import), query, analytics (recovery / sleep / strain / sleep regularity / longevity panel / somatic state / nutrition under-fuel detector / long-window YoY / audio exposure), cycle (phase + cycle-day + irregularity flag + phase-tagged metrics), symptoms / ECG / state-of-mind timelines, vault-aware (journal context with voice profile, body-literacy prompts, Floor correlation, symptom correlation, coaching context, panel context, weekly rollup, long-window with journal), and live TCP. Plus `health_recommended_labs()` returns a 16-marker reference panel with the WHY for each marker.

**Why it matters:** the substrate ships skills for daily journaling, coaching, advisory-panel synthesis, and weekly insights. Each one is more accurate when it knows how the body felt during the moments it analyzes. health-mcp closes that gap. The vault-aware tools READ journal frontmatter (`floor_level`, `floor`) and correlate biometrics with emotional Floor tags — the differentiating capability no other Apple Health MCP has. The companion skills `ingest-health` and `health-context` wire it into `/journal`, `/coaching`, `/panel`, `/patterns`, `/weekly`, `/monthly` so the body track and the emotional track meet automatically.

**Three ingestion modes** (pick by data flow):
- **XML export.zip** (free, manual, universal): iOS Health → Profile → Export All Health Data → run `health_import_xml(zip_path)`
- **Simple Health Export CSV** (free, manual): Simple Health Export iOS app → folder of `HKQuantityTypeIdentifier*.csv` → run `health_import_csv(folder_path)`
- **Health Auto Export TCP** (paid iOS app, real-time): Health Auto Export Premium → enable TCP server → run `health_live_query(metric, host, port, ...)`

**Open scoring algorithms:** Recovery (0-100, 40% HRV vs 30-day baseline + 20% RHR + 25% sleep duration + 15% sleep efficiency), Sleep (0-100, 40% duration + 25% efficiency + 20% REM% + 15% deep%), Strain (0-21 Whoop-shape, log compression). All deterministic Python with weights + formulas in `scores.py`. Directional, not diagnostic.

**Install:**
```bash
cd services/health-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Register in your project `.mcp.json`:
```json
{
  "mcpServers": {
    "health": {
      "command": "python3",
      "args": ["-m", "main"],
      "env": {"HEALTH_MCP_DB": "~/.claude/health-mcp/health.duckdb"}
    }
  }
}
```

Restart Claude Code, then run `/ingest-health <path-to-export.zip>` and `health_status()` for a smoke check.

**Composition:** `health-context` skill auto-fires when daily-journal, coaching, advisory-panel, patterns, or insights skills run. Host skills get biometric context folded into their prompts without each one re-implementing the connection layer.

**Privacy:** local-only. DuckDB stays on disk. No cloud sync, no telemetry. Vault-aware tools READ frontmatter; never write. TCP live mode is local Wi-Fi only.

**Source:** `services/health-mcp/` in this repo. Built with [FastMCP](https://github.com/jlowin/fastmcp) + [DuckDB](https://duckdb.org/) + [lxml](https://lxml.de/) for streaming XML parse. Existing-impl audit: surveyed [the-momentum/open-wearables](https://github.com/the-momentum/open-wearables), [the-momentum/apple-health-mcp-server](https://github.com/the-momentum/apple-health-mcp-server), [neiltron/apple-health-mcp](https://github.com/neiltron/apple-health-mcp), [HealthyApps/health-auto-export-mcp-server](https://github.com/HealthyApps/health-auto-export-mcp-server) before building. None covered the substrate-fit surface (Python + lightweight + multi-mode + vault-aware) so we unioned best-of-each per the WhatsApp-MCP build pattern.

---

### Recommended additional MCP servers (optional)

The Claude Code MCP ecosystem is growing fast. Other servers worth adding for a founder workflow:

- **Linear MCP** — issue/project tracking. Lets Claude read issue context and update statuses without you switching tabs.
- **Slack MCP** — read channel history, search past discussions, draft replies. Useful for "find that thing Sara said about pricing last month."
- **Gmail MCP** — read inbox, draft replies, search past threads.
- **Google Calendar MCP** — schedule meetings, find availability, check conflicts.
- **Google Drive MCP** — search/fetch Google Docs (essential if your team writes in Drive).
- **HubSpot MCP** — CRM integration if you don't use markdown CRM files.
- **Apollo MCP** — sales prospecting and enrichment.

Browse the [Anthropic MCP catalog](https://github.com/anthropics/claude-plugins-official) for the current list.

---

## Obsidian plugin stack

Install these via Obsidian Settings → Community Plugins → Browse. `/setup-brain` Phase 2 installs the core ones automatically.

### Required

- **[Dataview](https://github.com/blacksmithgu/obsidian-dataview)** — live queries against your vault. Powers the [dataview-queries.md](../templates/dataview-queries.md) library and the CRM mentions block. Without this, your CRM contact pages can't auto-list every place a person is mentioned.

- **[Templater](https://github.com/SilentVoid13/Templater)** — dynamic templates with JavaScript. Powers the journal entry template (auto-fills `creationDate`, `uuid`, prompts for floor), the CRM contact template, and the meeting note template.

### Strongly recommended

- **[Tasks](https://github.com/obsidian-tasks-group/obsidian-tasks)** — task tracking with due dates, recurring tasks, and dataview integration.

- **[YAML Properties](https://help.obsidian.md/properties)** — built into Obsidian 1.4+. Required for the frontmatter that drives all Dataview queries.

### Optional

- **[Bases](https://help.obsidian.md/bases)** — newer than Dataview, more spreadsheet-like. Good for CRM views.

- **[Outliner](https://github.com/vslinko/obsidian-outliner)** — better bullet list editing if you do a lot of nested outlining.

---

## How they fit together

This is the full stack working in concert:

1. **You write a daily journal** via `/journal` (a custom skill, not in this catalog — set up in `/setup-brain` Phase 10). Templater auto-fills the frontmatter.

2. **You run a meeting** with Granola recording. Afterward you say *"I just had a meeting with Sara"*. The meeting workflow rule in CLAUDE.md fires:
   - `granola_sync.py` has already exported the transcript to your meeting notes folder (via the LaunchAgent, or run it manually)
   - Claude reads it fully
   - Updates the meeting note with decisions + action items + verbatim quotes
   - Updates Sara's CRM contact card via the mentions block (Dataview)
   - Adds her action items to your team to-do file
   - Cascades any strategy decisions to the relevant canonical docs
   - Runs `/humanizer` on any external-facing prose written

3. **You journal at the end of the day**. `/journal` captures the patterns it noticed across the week.

4. **Weekly:** you run `/graphify Journals --update` (incremental, ~free because of the cache). The graph stays current. You run `/insights` (or `/weekly`) which reads `journal-index.json` (built by `build-journal-index.py`) and surfaces patterns.

5. **Strategic moment:** you ask Claude *"what does my vault say about X?"*. Claude reads `graphify-out/GRAPH_REPORT.md` first (god nodes, communities, hyperedges) instead of grepping individual files. The answer comes with context, not just keyword matches.

6. **You write a pitch deck**. `nano-banana` generates the visuals. `humanizer` cleans the copy. The Decision Log records why you made each major framing call so you can grade them later.

None of these tools are mine. They're all open source by other people. What `/setup-brain` does is **install them and wire them together** with the right CLAUDE.md rules, templates, and folder structure so the whole stack acts like one tool.
