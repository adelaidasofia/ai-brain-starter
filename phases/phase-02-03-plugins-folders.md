## Phase 2: Install Obsidian Plugins

**AUTO-INSTALL FIRST. Don't make the user click through Obsidian's plugin browser unless the auto-install fails.** Non-technical users miss-click in the plugin UI, install the wrong plugin, or skip the "Enable" step after "Install" — these are the top three Phase 2 support requests.

Tell the user: *"I'm going to install your Obsidian plugins in the background. These power live queries, templates, and task tracking, and let Claude work with your vault directly. Give me a few seconds."*

Then run this Python helper, substituting `[VAULT_PATH]` with the actual vault path saved in Phase 1 step 8:

```bash
python3 - <<'PY'
import json, sys, urllib.request, zipfile, io, shutil
from pathlib import Path

VAULT = Path("[VAULT_PATH]")
OBSIDIAN_DIR = VAULT / ".obsidian"
PLUGINS_DIR = OBSIDIAN_DIR / "plugins"
PLUGINS_DIR.mkdir(parents=True, exist_ok=True)

# Plugin id → GitHub repo (owner/repo). The release ZIP for each contains
# main.js, manifest.json, and (sometimes) styles.css — Obsidian's plugin format.
#
# This is the DEFAULT set — light enough to enable on a vault of any size.
# Smart Connections is deliberately NOT here: it is a heavy indexer (SQLite-
# backed embeddings of every note) that can exhaust Obsidian's single renderer
# process and crash it on open once a vault grows past a few thousand notes
# (EXC_BREAKPOINT / renderer out-of-memory). It is offered as an explicit opt-in
# instead — see "Optional: Smart Connections (opt-in)" below. graphify already
# covers explicit relationships, and where the Mycelium runtime is in use that
# runtime is the semantic retrieval layer, so Smart Connections is redundant.
PLUGINS = {
    "dataview":  "blacksmithgu/obsidian-dataview",
    "templater-obsidian": "SilentVoid13/Templater",
    "obsidian-tasks-plugin": "obsidian-tasks-group/obsidian-tasks",
    "obsidian-local-rest-api": "coddingtonbear/obsidian-local-rest-api",
    "custom-sort": "SebastianMC/obsidian-custom-sort",
}

def fetch_latest_release(repo):
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def install_plugin(plugin_id, repo):
    target = PLUGINS_DIR / plugin_id
    if (target / "main.js").exists() and (target / "manifest.json").exists():
        print(f"  = {plugin_id} already installed")
        return True
    try:
        rel = fetch_latest_release(repo)
        # Most plugins ship a ZIP asset; some ship loose main.js + manifest.json.
        zip_asset = next((a for a in rel.get("assets", []) if a["name"].endswith(".zip")), None)
        target.mkdir(parents=True, exist_ok=True)
        if zip_asset:
            with urllib.request.urlopen(zip_asset["browser_download_url"], timeout=60) as r:
                buf = io.BytesIO(r.read())
            with zipfile.ZipFile(buf) as z:
                for name in z.namelist():
                    if name.endswith(("main.js", "manifest.json", "styles.css")):
                        # Strip any leading directory in the zip
                        out_name = Path(name).name
                        with z.open(name) as src, open(target / out_name, "wb") as dst:
                            shutil.copyfileobj(src, dst)
        else:
            # Loose-asset plugins (rare): main.js + manifest.json + maybe styles.css
            for fname in ("main.js", "manifest.json", "styles.css"):
                asset = next((a for a in rel.get("assets", []) if a["name"] == fname), None)
                if asset:
                    with urllib.request.urlopen(asset["browser_download_url"], timeout=60) as r:
                        (target / fname).write_bytes(r.read())
        print(f"  + {plugin_id} installed")
        return True
    except Exception as e:
        print(f"  ! {plugin_id} install failed: {e}", file=sys.stderr)
        return False

# Install each plugin
installed = []
for pid, repo in PLUGINS.items():
    if install_plugin(pid, repo):
        installed.append(pid)

# Mark them as enabled by writing community-plugins.json (the file Obsidian
# reads to know which community plugins to load on startup).
cp_file = OBSIDIAN_DIR / "community-plugins.json"
existing = []
if cp_file.exists():
    try:
        existing = json.loads(cp_file.read_text(encoding="utf-8"))
    except Exception:
        existing = []
for pid in installed:
    if pid not in existing:
        existing.append(pid)
cp_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")

# Configure app.json — sort files by most recently modified (newest first).
# Merge with existing settings so we don't clobber anything the user already set.
app_file = OBSIDIAN_DIR / "app.json"
app_settings = {}
if app_file.exists():
    try:
        app_settings = json.loads(app_file.read_text(encoding="utf-8"))
    except Exception:
        app_settings = {}
app_settings.setdefault("fileSortOrder", "byModifiedTime")

# Default NEW notes (including ones Obsidian auto-creates when you click an
# unresolved [[wikilink]]) to 📝 Notes/, never the vault root. Obsidian's
# factory default is "root", which is why a clicked [[Amor]] / [[Pisos Bajos]]
# link on a vault whose floor notes weren't pre-created lands a loose emotion
# file at the top level. setdefault so we never override a user's own choice.
app_settings.setdefault("newFileLocation", "folder")
app_settings.setdefault("newFileFolderPath", "📝 Notes")

# Exclude machine-generated folders from Obsidian's index. The brain's hooks
# write thousands of files into these over a vault's lifetime — session stubs,
# worktree snapshots, rotating logs. They are machinery, not notes. On a mature
# vault, leaving them indexed pins "Obsidian Helper (Renderer)" at 100%+ CPU and
# makes the app feel broken. Excluding them keeps the index to your real
# documents. Merge so we never clobber filters the user set themselves.
machinery_excludes = [
    "⚙️ Meta/Worktree Snapshots/",
    "⚙️ Meta/Sessions/",
    "⚙️ Meta/logs/",
]
existing_filters = app_settings.get("userIgnoreFilters") or []
app_settings["userIgnoreFilters"] = list(dict.fromkeys(existing_filters + machinery_excludes))

app_file.write_text(json.dumps(app_settings, indent=2), encoding="utf-8")

# Write sortspec.md for custom-sort plugin — sorts ALL folders by the most
# recently modified file inside them recursively (newest first).
# Uses "advanced recursive modified" which traverses the full folder tree,
# so folders bubble up based on the newest note anywhere inside them.
sortspec_file = VAULT / "sortspec.md"
if not sortspec_file.exists():
    sortspec_file.write_text(
        "---\nsorting-spec: |\n  target-folder: /*\n  > advanced recursive modified\n---\n",
        encoding="utf-8",
    )
    print("sortspec.md created — folders will sort by most recently modified note (recursive).")
else:
    print("sortspec.md already exists — skipping.")

# Pre-activate custom-sort plugin by writing data.json with suspended: false.
# The plugin defaults to suspended: true on first install, which means it does
# nothing until the user manually clicks the ribbon toggle. Writing data.json
# here skips that manual step entirely.
custom_sort_data = PLUGINS_DIR / "custom-sort" / "data.json"
if "custom-sort" in installed and not custom_sort_data.exists():
    custom_sort_data.write_text(json.dumps({"suspended": False}, indent=2), encoding="utf-8")
    print("custom-sort pre-activated (suspended: false written to data.json).")

print(f"\nDone. Installed {len(installed)}/{len(PLUGINS)} plugins.")
print("File explorer set to sort by most recently modified.")
print("If Obsidian is currently open, the user must reload it (Cmd/Ctrl+R) for plugins to activate.")
PY
```

**After the script runs — read the output carefully before doing anything:**
- `= plugin already installed` → **success, do nothing.** This means the plugin was already present. Do NOT re-run, do NOT tell the user to install it manually, do NOT flag it as a problem. Already installed is the ideal state.
- `+ plugin installed` → freshly installed, success.
- `! plugin install failed: ...` → genuine failure (network error, GitHub rate limit, etc.). Fall back to the manual UI walkthrough for ONLY this plugin.
- `Done. Installed X/Y plugins.` at the end → if X equals Y, everything succeeded (including pre-existing ones). Move on.

**Do not retry a plugin that printed `= already installed`.** Do not ask the user to check it. Do not loop back. It's done.

- If all plugins are `=` or `+`: tell the user *"Done — all plugins are installed. If Obsidian is open right now, close and reopen it (or press Cmd+R / Ctrl+R) so they activate."*
- If any plugin failed (`!`): fall back to the manual UI walkthrough for ONLY the failed plugins.
- If the auto-install fails entirely (no Python, no network, vault path wrong): fall back to the full manual UI walkthrough below.

**Manual fallback (only if auto-install failed):**

"Now let's install the plugins that make everything work. In Obsidian: Settings → Community Plugins → Turn on community plugins → Browse."

Walk them through installing and enabling each one:

1. **Dataview** — "Search 'Dataview' → Install → Enable. Powers live queries and dashboards."
2. **Templater** — "Search 'Templater' → Install → Enable. Auto-applies templates when you create notes."
3. **Tasks** — "Search 'Tasks' → Install → Enable. Tracks to-dos across your vault."
4. **Local REST API** — "Search 'Local REST API' → Install → Enable. Lets Claude interact with your vault directly."

(Smart Connections is intentionally omitted from the default set — it is an opt-in heavy indexer; see "Optional: Smart Connections" below.)

"All installed and enabled? Let's keep going."

### Optional: Smart Connections (opt-in, not enabled by default)

Smart Connections adds semantic ("meaning-based") search over the vault. It is **deliberately not in the default install** because it is a heavy indexer: it builds SQLite-backed embeddings of every note and holds them in Obsidian's single renderer process. On a vault beyond ~5K notes, that — alongside other heavy indexers loading at once — can exhaust the renderer's memory and crash Obsidian on open (`EXC_BREAKPOINT (SIGTRAP)`, CPU pinned).

Only offer it if the user explicitly asks for semantic search AND accepts the tradeoff. If they do:

- **Scope it to a subset of folders, never the whole vault.** Point its inclusions at the few folders where semantic discovery actually helps (e.g. `📝 Notes/`, `📓 Journals/`), not everything.
- graphify already covers explicit relationships; where the Mycelium runtime is in use, that runtime is the semantic retrieval layer and Smart Connections is redundant.
- To install: add `"smart-connections": "brianpetro/obsidian-smart-connections"` back into the `PLUGINS` dict above and re-run, or manually do Community Plugins → search "Smart Connections" → Install → Enable, then immediately set its folder scope.

Full settings and crash-recovery steps live in the obsidian-plugins rule ("Large-vault plugin posture").

### Large-vault plugin posture

Obsidian renders the whole vault in one Electron renderer with a bounded memory heap, and each "indexer" plugin holds an in-memory index. Past ~5K notes, several heavy ones loading at once can crash the renderer on open. As the user's vault grows, tell them:

- **Keep Dataview** — it is the lightest indexer and the dashboards depend on it. Dataview alone opens fine even at 13K+ notes.
- **Scope or disable the heavy indexers** — Smart Connections (embeddings of every note) is heaviest; Tasks (full-vault checkbox scan) compounds it.
- Machine-generated folders are already excluded from Obsidian's index by the installer above (`userIgnoreFilters`); that bounds session/log/snapshot churn but not a plugin that indexes real notes.
- If the renderer ever crashes on open, the recovery steps (restricted mode → re-add Dataview only → add others one at a time) are in the obsidian-plugins rule ("Large-vault plugin posture").

## Phase 3: Create Folder Structure

"I'm going to create your folder structure now. This is how your vault will be organized."

**BEFORE CREATING ANYTHING — check what already exists.** The user may have already organized their vault. Scan the top-level folder first. If a folder already exists (even with a slightly different name), use it — don't create a duplicate. If a file was manually moved since setup, respect its new location. Claude's idea of "where something should go" is always subordinate to where it actually is right now. This prevents the most common setup complaint: Claude recreating files the user already moved.

Create these CORE folders in their vault (emojis are important — they make the sidebar scannable):

```
📓 Journals/
📓 Journals/Monthly Summaries/
📓 Journals/Weekly Insights/
📓 Journals/Monthly Insights/
🏠 Home/
👤 CRM/
📝 Notes/
📝 Notes/Floors/
⚙️ Meta/
⚙️ Meta/scripts/
```

**`📝 Notes/Floors/` is core, not optional.** The 34 floor concept notes and the three tier notes (Low / Middle / High Floors) created in Phase 10a live HERE, never at the vault root and never loose in `📝 Notes/`. Floors are the journaling spine, so this folder ships with every install. If you skip creating it, Phase 10a (or a later journal entry's wikilink) scatters `Disgust`, `Amor`, `Culpa`, `Soledad`, `Low Floors`, etc. across the top level — the single most common "why is my sidebar full of emotion files" complaint.

**Conditional folders — only create if relevant based on what they told you in Phase 1. These are BLOCKING conditionals, not suggestions. If the user did not explicitly opt in, DO NOT create the folder, DO NOT add it to the vault map, DO NOT reference it in their CLAUDE.md or resolver files.**

- `✍️ Writing/` — **ONLY if `WRITES_PUBLICLY = true` from Phase 1 question 5.** Journaling does NOT count (that's `📓 Journals/`). This folder is for content written with an audience in mind: blog posts, book drafts, newsletters, Substack, essays. If the user said no or was unclear, **skip this folder entirely**. Do not create `Writing/Drafts/`, do not add "Writing/" to the `⚙️ Meta/Folder Resolvers/📝 Notes.md` decision tree, do not add writing-related rules to the Phase 4 CLAUDE.md template, do not reference Writing/ anywhere downstream. The default state for a new user is: no Writing folder.
- `📚 Books/` — **ONLY if they read books and highlight/annotate.** Ask in Phase 1 or infer from their answer about existing notes. If they mention Kindle, Readwise, book notes, or reading habits, create it. Otherwise skip. Most people don't need an empty Books folder sitting in their sidebar.
- `🧠 Psychology/` — **ONLY if they explicitly mention inner work, therapy, self-help, psychology, behavioral patterns, or personal development as a focus area.** This is a niche folder. Most people's reflections live naturally in Journals/ and Notes/. Don't create it by default.
- `💼 Business/` — only if they have a business, startup, or side project
- `✅ To-dos/` — **ONLY if they want task management inside the vault.** Some people prefer external tools (Todoist, Things, Linear). If they say yes or mention wanting to track tasks: create the folder, then install the to-do system from `templates/generated/todo-system-template.md`. This gives them a two-file setup (prioritized queue + capture inbox), an Eisenhower four-quadrant Dataview view rendered at the top of the queue so top priorities are visible every time they open the file, P1/P2/P3 tiers with Dataview inline fields, an auto-refreshing This Week view, and a Waiting On tracker. If they already have a task system they like, skip it.
- `🚀 [Project Name]/` — if they have an active project/startup, give it its own emoji folder
- `🏫 School/` — only if they're a student
- `🌱 Curiosities/` — for people who want a catch-all for random interests

**Why this matters:** previously, Writing/ was created by default for almost everyone because the conditional was too weak. The result was vaults with empty Writing folders for users who don't write, and Claude trying to create drafts in folders that shouldn't exist. Fix: require explicit opt-in.

Tell them: "Done — you should see the folders in your Obsidian sidebar now. The emojis help you scan quickly. If you have a specific area of your life that needs its own folder (a creative project, school, etc.), tell me and I'll add it."

**Add any custom folders they request. Always use emojis.**

### Phase 3b — Create About Me profile file (idempotent)

After creating folders, copy the About Me template into the vault's 🏠 Home/ folder ONLY IF the file does not already exist. Re-running bootstrap must never overwrite a user's existing About Me content — it accrues over time and is the user's deep profile.

```bash
mkdir -p "[VAULT_PATH]/🏠 Home"
if [ ! -f "[VAULT_PATH]/🏠 Home/About Me.md" ]; then
  cp "$HOME/.claude/skills/ai-brain-starter/templates/Home/About Me.md" "[VAULT_PATH]/🏠 Home/About Me.md"
  # Substitute the {{DATE}} placeholder with today's date
  sed -i '' "s/{{DATE}}/$(date +%Y-%m-%d)/g" "[VAULT_PATH]/🏠 Home/About Me.md"
  echo "created [VAULT_PATH]/🏠 Home/About Me.md from template"
else
  echo "[VAULT_PATH]/🏠 Home/About Me.md already exists — preserving user content"
fi
```

This is the canonical "deep profile" file. The CLAUDE.md `## Me` line is the quick reference loaded every session; `About Me.md` is the rich profile loaded on demand for context-heavy work (panel reviews, life-history prose, advisory sessions, weekly insights).

**Sections start mostly empty.** Phase 4 fills the high-priority fields (Identity name, Work one-liner, Relationships key people). Phase 11 fills Tools-adjacent fields. Phase 13 fills Health (wearables + labs link). Every subsequent session appends new personal context revealed during conversation per the universal capture rule below.

### Phase 3c — Universal capture rule (codified, applies for the rest of install AND every future session)

**Anything personal the user reveals must land somewhere durable in the vault before the conversation moves on.** If the user volunteers a fact about themselves — age, where they grew up, a health condition, a partner's name, an old job, a hobby, a value, a fear — and that fact does not naturally fit a structured ask currently running, append it to `🏠 Home/About Me.md` in the right section (or under `## Notes` if no section fits) before moving on.

The failure mode: a user says "I have ADHD" in passing during the tools question. The model nods and moves on. The fact never lands anywhere. Six months later when the user asks "why am I forgetting things?" the model has no context. The capture must be lossless.

**Mechanics:**
- Append, never overwrite. Date the addition if it's a free-form `## Notes` entry.
- Place in the right section if obvious (health → Health, family → Relationships, etc.).
- Do not pause the conversation to confirm — just write the bullet and continue. The user can edit/remove later.
- If the fact contradicts something already in About Me (old version), surface the conflict to the user: "You said [old]; just now you said [new]. Which should stay?" Do not silently overwrite.
- Sensitive content (trauma, medical, financial in private repos) is fine to append. About Me is the user's private vault file, not shared.

This rule applies to EVERY phase from here forward and to every post-install session. It is the canonical answer to "information was given and went nowhere."

After creating folders, create resolver files in `⚙️ Meta/Folder Resolvers/` — one per key directory. Each is a short decision tree answering "does X live here?" — it prevents the vault from decaying into ambiguity as it grows. Centralized in Meta so the resolver doesn't clutter the folder it describes (a resolver file sitting in `👤 CRM/` shows up in every CRM Dataview query and next to every contact card).

First create the `⚙️ Meta/Folder Resolvers/` directory, then create these files inside it. Name each file after the folder it describes (use the folder's emoji prefix):

**⚙️ Meta/Folder Resolvers/👤 CRM.md:**
```markdown
# Does this live in CRM/?

1. Is this a real person you've interacted with or plan to? → YES: create [Name].md there
2. Is it a company, org, or brand (not a specific person)? → NO: Business/ or Notes/
3. Is it a public figure you've never met? → NO: Notes/ or Books/
4. Is it a group you have a relationship with as a whole? → YES, if you interact with them as a unit
```

**⚙️ Meta/Folder Resolvers/📝 Notes.md:**
```markdown
# Does this live in Notes/?

1. Is this from a book you read and the user has a 📚 Books/ folder? → NO: 📚 Books/. (Omit this line if no Books/ folder exists.)
2. Is this a psychology/behavioral concept and the user has a 🧠 Psychology/ folder? → Maybe: 🧠 Psychology/. (Omit this line if no Psychology/ folder exists.)
3. Is this an article, course, or how-to you learned from? → YES: create here
4. Is this a concept that belongs to a specific project? → NO: that project's folder
5. Is this your own original framework or thesis? → If short/raw, it can stay here or in a journal. If you're developing it into something longer, put it wherever your creative work lives. (Only mention `Writing/` here if the user has a `✍️ Writing/` folder — otherwise omit the whole sentence about drafts. Don't reference folders that don't exist in this user's vault.)
```
