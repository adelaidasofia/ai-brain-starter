# Security

This repo sets up an Obsidian vault plus Claude Code on your own machine. It is a local tool. There is no web app, no server, no shared database, no user accounts. That shapes what "security" means here.

If you are used to web-app security advice (Row-Level Security, CORS, Security Headers), those concepts do not apply to this project. They are about protecting shared services from remote attackers. This project has neither.

What DOES apply: four practical habits that protect your own data and machine.

---

## 1. Keep secrets out of the repo

Some setup steps ask for API keys (Gemini for image generation, OpenAI, etc.). Put them in your shell profile (`~/.zshrc` on Mac, `setx` on Windows) or a secrets manager. Never paste them into a tracked file.

The `.gitignore` in this repo already blocks the most common leak paths:

- `.env`, `.env.local`, `.env.production` (local secrets files)
- `*.key`, `*.pem`, `*.pfx` (private keys)
- `secrets.json`
- `.claude/settings.local.json` (your per-machine Claude Code settings)

If you accidentally commit a secret: rotate the key immediately, then remove it from git history. Deleting the file in a new commit is NOT enough, the old commit still contains it.

---

## 2. Skills and hooks run code on your machine

Claude Code skills and hooks are shell scripts, Python scripts, or instructions that tell Claude to run commands. Before you install a skill or hook from anywhere other than this repo, open the files and read them. You are looking for:

- Shell commands that call external URLs you do not recognize
- Scripts that read files outside the vault
- Hooks that run on every prompt or every file save (these run a lot)

Installing a skill blindly is like running a shell script someone DM'd you. The skills in this repo are reviewed by the maintainer. Third-party skills are not.

---

## 3. MCP servers can read your whole vault

MCP servers are helpers that let Claude talk to external tools (Google Workspace, Linear, Slack, etc.). Once connected, an MCP server typically has read access to whatever context you give it in conversation. Some have read access to your filesystem.

Before connecting an MCP server:

- Check who publishes it (official vendor vs. community fork)
- Check what scopes it requests (read-only is safer than read-write)
- Prefer official MCP servers from the vendor over third-party mirrors

The `docs/POWER_TOOLS.md` file lists MCPs this project recommends. If you add others, apply the same filter.

---

## 4. Claude Code permissions

Claude Code asks before running commands by default. You can allowlist command patterns to skip the prompt. Every allowlist entry is a tradeoff: less friction, but also less chance for you to catch a bad command.

Good defaults:

- Review your `.claude/settings.json` and `.claude/settings.local.json` allowlists occasionally
- Never allowlist `rm -rf` or `git push --force` broadly
- Prefer narrow patterns (`Bash(git status)`) over wildcards (`Bash(*)`)

If Claude asks for permission to do something and you are not sure what it does, say no and ask Claude to explain first.

---

## 5. What the install sends to our servers (and what it never sends)

Local-first by default. Your vault, journals, notes, and files never leave your machine. The install does not require an email and does not phone home about your content.

There is exactly one opt-in. At the end of setup you may choose to give an email (for occasional update notes and a free workflow audit). If you do, an install token is minted: `mycelium-ai.co/api/install/quick-mint` receives the email, name, and language you gave, plus a short OS label (e.g. `mac-arm`). That is the email submission you chose to make.

While that token exists on your machine, three best-effort, fail-open events may be sent. Each carries only the token and a coarse signal — never any content:

- **install started** and **install completed** — the token plus an OS string (e.g. `Darwin 24.5.0 arm64`).
- **first journal saved** (one time only) — the token plus the calendar date. Not the journal text. Not a word of it.

If you never give an email, or you decline the ask, none of these fire. A decline is recorded locally and nothing is sent.

What is **never** sent, under any path: journal text, note contents, file contents, file names, your contacts, or anything from your vault. Only the opaque token tied to the email you chose to give, an OS label, and (once) a single date. The token is local to your machine; delete `~/.claude/.ai-brain-starter-email-on-file` to stop all of the above.

The rule this section states — every egress is disclosed AND user-stoppable — is
also implemented in the paid Studio app, which has its own analytics path and its
own opt-out. Both are declared under `egress-disclosure` in
`scripts/paired-implementations.json`. Changing the policy here means changing it
there too; the list names where.

---

## 6. Corporate / hardened install profile

If you are rolling this out across a team — or your security team needs to review and approve exactly what lands on a machine before install — use the corporate profile:

```bash
bash bootstrap.sh --profile corporate --dry-run   # review the manifest, change nothing
bash bootstrap.sh --profile corporate             # install with hardened defaults
```

(On Windows: `.\bootstrap.ps1 -Profile corporate`. The env-var form `CORPORATE_PROFILE=1` is equivalent, for scripted/MDM rollout.)

By default the corporate profile:

- Installs a **minimal, named** plugin/skill set only — first-party skills plus `obsidian@obsidian-skills` and `context7`. It skips every third-party marketplace (Sentry, Stripe, Cloudflare, SEO, marketing skills, etc.).
- **Excludes shell-execution-capable Obsidian community plugins** (e.g. "Shell Commands", "Hider") and recommends Obsidian Restricted Mode. Those plugins were the abuse vector in the REF6598 / PHANTOMPULSE RAT campaign (Elastic Security Labs, April 2026).
- **Excludes external-egress components** by default: the `granola` and `chatprd` MCP servers and the `playwright` browser plugin.
- **Turns telemetry off** and pins versions (disables the self-update hook and the Claude Code autoupdater).
- **Skips every sudo step** (Homebrew, the `/usr/local/bin` symlink) and runs entirely in user space.
- **Emits a reviewable component manifest** (exact components + versions + source URLs) to `~/.claude/.ai-brain-starter-corporate-manifest.md` and to stdout, so a security team can approve before install.

Full spec, the canonical manifest, the update workflow, and a security-team review checklist live in **[docs/CORPORATE_PROFILE.md](docs/CORPORATE_PROFILE.md)**.

---

## Non-goals: what this security model deliberately does NOT do

A useful security model is also a fence around what it does not try to handle. Listing non-goals stops over-trust ("the vault must be doing X for me, since X is a security thing") and over-fear ("the vault is missing X, so it's insecure") simultaneously.

This Vault Security Pack does NOT:

- **Execute discovered packages.** Reading a `package.json` or a lockfile never calls `npm install`, never runs install scripts, never executes whatever the package would have done if it were run.
- **Download package contents or fetch threat intelligence at runtime.** Catalog input (CVE list, advisory feed) is operator-supplied. The pack does not phone home to fetch advisories on its own. You decide which feed to trust and feed it in.
- **Parse source code.** This is a structural choice. Source parsing is a much larger attack surface and a much larger maintenance burden. The pack reads lockfiles + manifests + config metadata only.
- **Inventory installed packages on your machine.** That is a separate concern. If you need it, use a dedicated endpoint-inventory tool (e.g. [perplexityai/bumblebee](https://github.com/perplexityai/bumblebee) — Apache 2.0, single Go binary, read-only). The vault security pack covers commit-time + push-time leak prevention; bumblebee covers installed-state inventory for supply-chain incident response.
- **Rotate credentials for you.** If the pack catches a secret on push, you rotate the leaked credential yourself. Automatic rotation requires runtime access to every service the credential touches; the pack is local-tooling, not a credential manager.
- **Promise zero false positives.** Word-boundary regex on names + API-key regex on tokens will sometimes match harmless strings. The pack errs toward over-flagging because false negatives are unrecoverable (a leaked secret stays leaked) and false positives are cheap to dismiss.
- **Replace a real EDR / SIEM / SCA platform.** This is a personal-vault security model. If your threat model includes targeted attackers, regulated data, or a fleet of devices, you need real infrastructure. The pack is a floor, not a ceiling.

The pattern of explicit non-goals is cherry-picked from [perplexityai/bumblebee SECURITY.md](https://github.com/perplexityai/bumblebee/blob/main/SECURITY.md), which lists *"execute discovered packages"*, *"download package contents or fetch threat intelligence at runtime"*, and *"parse source code"* as deliberate out-of-scope items. Good security tools name their fence; better tools cite the upstream they learned the fencing pattern from.

## Reporting a security issue

If you find a security issue in this repo (a leaked secret, a script that does something unexpected, a hook that can be exploited), open a GitHub issue with the label `security` or contact the maintainer directly. Do not include the full exploit details in a public issue if the fix requires coordination.

There is no bounty program. This is a personal project maintained by one person.
