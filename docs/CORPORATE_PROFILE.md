# Corporate / Hardened Install Profile

A compliance-ready install path for organizations that need to review and approve
exactly what lands on a machine before rolling this out to a team. It was added
after an enterprise security review of the workshop install: the same setup that
works for an individual needs a locked-down, reviewable variant for a company.

The profile changes **defaults**, not capability. Everything it excludes can be
re-enabled later, by hand, after your security team approves it.

---

## TL;DR

```bash
# 1. Review what WOULD be installed — changes nothing, prints the manifest:
bash bootstrap.sh --profile corporate --dry-run

# 2. Install with the hardened defaults:
bash bootstrap.sh --profile corporate
```

```powershell
# Windows (PowerShell):
powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1 -Profile corporate -DryRun   # review
powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1 -Profile corporate           # install
```

The environment-variable form is equivalent (useful for MDM / scripted rollout):

```bash
CORPORATE_PROFILE=1 bash bootstrap.sh
```
```powershell
$env:CORPORATE_PROFILE = "1"; powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1
```

After the run, the exact manifest is written to
`~/.claude/.ai-brain-starter-corporate-manifest.md` for your records.

---

## What the profile does

| # | Default behavior | Corporate profile |
|---|---|---|
| 1 | Installs first-party skills **plus** seven third-party plugin marketplaces (Sentry, Stripe, Cloudflare, claude-seo, superpowers, marketingskills, Trail of Bits) | Installs **only** the minimal named set: first-party ai-brain-starter skills + `obsidian@obsidian-skills` + `context7`. All third-party marketplaces skipped. |
| 2 | Two external MCP servers registered (`granola`, `chatprd`) + `playwright` plugin | All three excluded — they egress data or drive a browser. Opt-in after review. |
| 3 | Optional email signup + a best-effort install ping | Both off: `EMAIL_GATE_BYPASS=1`, `MYCELIUM_NO_PING=1`, plus Claude Code telemetry env enforced in `settings.json`. |
| 4 | Auto-updates: a self-update hook pulls the skill repo every ~6 days; Claude Code auto-updates | Versions pinned: self-update hook disabled via a sentinel; Claude Code autoupdater off. Updates require a reviewed manual re-run. |
| 5 | On a fresh machine, may install Homebrew (sudo) and symlink the Obsidian CLI into `/usr/local/bin` (sudo) | No sudo. Runs entirely in user space. Missing runtimes (Python, Node, Obsidian) are reported, not auto-installed — provision them via your IT-approved, version-pinned channel. |

It also **emits a reviewable component manifest** (see below) so a security team
can approve the exact set before install — run with `--dry-run` to get it without
changing anything.

---

## Component manifest (what gets installed)

This is the canonical list. The installer prints the same content at the end of
every corporate run and writes it to `~/.claude/.ai-brain-starter-corporate-manifest.md`.

### Installed

| Component | Version / pin | Source | Why it's in the minimal set |
|---|---|---|---|
| ai-brain-starter skill (+ bundled first-party skills: graphify, daily-journal, insights, patterns, meeting-todos, second-brain-mapping, …) | Pinned to the git revision you install; self-update disabled | `https://github.com/mycelium-hq/ai-brain-starter` | The vault workflow itself. Ships in-repo — no per-skill network fetch. |
| `obsidian@obsidian-skills` | Marketplace pin | `github: kepano/obsidian-skills` | Obsidian/vault authoring skills — core knowledge-worker need. |
| `context7` | Claude Code plugin | Anthropic plugin registry | Documentation lookup. Read-only, no data egress. |
| Python 3.10+, Node.js, pipx, GitHub CLI, graphify | **Not installed by this profile** | Your IT-approved channel | Runtime prerequisites. Provision + pin them yourself so versions stay under your control. |

### Excluded by default (and why)

| Component | Why excluded |
|---|---|
| Marketplaces: `sentry-skills`, `stripe`, `cloudflare`, `claude-seo`, `superpowers`, `marketingskills`, `trailofbits` | Developer/marketing tooling, not what a knowledge-worker vault needs. Each is a third-party marketplace = added supply-chain surface. |
| `playwright` plugin | Browser automation. Out of scope for a knowledge-worker vault and an unnecessary capability to grant. |
| `granola` MCP (`mcp.granola.ai`) | External URL MCP. Meeting data leaves the machine. |
| `chatprd` MCP (`app.chatprd.ai`) | External URL MCP. Conversation context leaves the machine. |
| Shell-execution-capable Obsidian community plugins — e.g. **"Shell Commands"**, **"Hider"** | **Never installed or recommended, in any profile.** These were the abuse vector in the REF6598 / PHANTOMPULSE RAT campaign documented by Elastic Security Labs (April 2026): a malicious Obsidian plugin with shell-execution capability was used to run a remote-access trojan. The corporate profile additionally recommends Obsidian **Restricted Mode** (below) so no community plugin loads at all in a sensitive vault. |

---

## Telemetry and network: what's off

The corporate profile enforces these in `~/.claude/settings.json` (`env` block),
so they persist for every future Claude Code session, not just the install:

| Variable | Effect |
|---|---|
| `DISABLE_TELEMETRY=1` | No usage telemetry from Claude Code. |
| `DISABLE_ERROR_REPORTING=1` | No crash/error reports. |
| `DISABLE_FEEDBACK_COMMAND=1` | Disables the in-product feedback command. |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` | Single switch that disables non-essential network calls, including the autoupdater. |
| `DISABLE_AUTOUPDATER=1` | Belt-and-suspenders pin of the Claude Code CLI version. |
| `MYCELIUM_NO_PING=1` | No install ping to the project's server (also pre-creates the dedup sentinel). |

During the install itself, `EMAIL_GATE_BYPASS=1` is forced, so no email is
collected and `quick-mint` is never called.

> These keys are **enforced** (overwritten to the hardened value) in corporate
> mode — `settings.json` is backed up first. That is the one place the corporate
> profile intentionally overrides the installer's usual never-clobber behavior,
> because enforcing the hardened value is the whole point.

---

## Version pinning and updates

By default this project keeps itself current with a `UserPromptSubmit` hook that
`git pull`s the skill repo roughly every six days. Corporate installs pin instead:

- The installer creates `~/.claude/.ai-brain-starter-pinned`. The self-update
  hook checks for that file first and short-circuits — so the skill stays at the
  revision you approved.
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` + `DISABLE_AUTOUPDATER=1` pin the
  Claude Code CLI.
- Obsidian: deploy your IT-approved, version-pinned build and disable in-app
  automatic updates (Settings → General → Automatic updates).

**To update later:** review the new revision, then re-run
`bash bootstrap.sh --profile corporate` (it re-pulls only when you run it). To
return to automatic updates, delete `~/.claude/.ai-brain-starter-pinned`.

---

## Operator-side hardening (manual)

The installer can't make these choices for you — they depend on your environment:

- **Keep the vault OUTSIDE any cloud-synced folder** (OneDrive, iCloud Drive,
  Dropbox, Google Drive). A notes vault under a sync client means every note is
  replicated to a third-party cloud, and the `.git` machinery can thrash the
  sync daemon. Put it under a plain local path (e.g. `~/vault`).
- **Enable Obsidian Restricted Mode** for sensitive vaults: Settings →
  Community plugins → **turn OFF** "Community plugins" (or keep
  `.obsidian/community-plugins.json` as `[]`). No third-party Obsidian plugin
  code runs in Restricted Mode — the simplest defense against the shell-plugin
  abuse class above.
- **Review `settings.json` permission allowlists** before broad rollout. Prefer
  narrow command patterns over wildcards. (See [SECURITY.md](../SECURITY.md).)

---

## Re-enabling an excluded component after review

Nothing is removed permanently — the profile just doesn't add it. After your
security team approves a specific component, enable it by hand:

- **A skipped marketplace/plugin:** inside Claude Code, run
  `/plugin marketplace add <owner/repo>` then `/plugin install <name>`.
- **An external MCP (granola / chatprd):** add it to `~/.claude/.mcp.json`. The
  standard installer shows the exact entries; or re-run bootstrap **without**
  the corporate profile (note: that also re-enables the other standard defaults).
- **playwright:** add `"playwright": true` to `enabledPlugins` in
  `~/.claude/settings.json`.

---

## Security-team review checklist

Before approving a rollout:

- [ ] Run `bash bootstrap.sh --profile corporate --dry-run` and read the emitted manifest.
- [ ] Confirm the pinned ai-brain-starter revision (SHA) matches one you've reviewed.
- [ ] Confirm `~/.claude/settings.json` `env` carries the six telemetry/pin keys above.
- [ ] Confirm `~/.claude/.mcp.json` contains no external URL MCPs you didn't approve.
- [ ] Confirm the vault path is not under a cloud-sync folder.
- [ ] Confirm Obsidian Restricted Mode is on for sensitive vaults.
- [ ] Confirm Obsidian + Claude Code auto-update are disabled.

This profile is a **floor for a managed rollout, not a replacement for your EDR /
SIEM / MDM.** See [SECURITY.md](../SECURITY.md) for the project's overall threat
model and non-goals.
