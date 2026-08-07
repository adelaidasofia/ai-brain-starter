# Contributing

Thanks for wanting to help. This project is run by one person — here's how to make it easy for both of us.

---

## Found a bug?

Open a [GitHub issue](https://github.com/mycelium-hq/ai-brain-starter/issues) and describe:
- What you were trying to do
- What happened instead
- Your setup (Mac / Windows / Linux, Obsidian version)

No template required. Plain English is fine. The more specific, the faster it gets fixed.

## Have a suggestion?

Same thing — open an issue. Describe the problem you're trying to solve, not just the solution. If you've already tested something that works, share that too.

## Want to submit a fix?

1. Fork the repo
2. Make your changes
3. Open a pull request with a one-sentence description of what changed and why

Before opening the PR, run the full CI gate locally: `bash scripts/ci.sh` (the same `py_compile` over every tracked `*.py`, the integration tests, and `shellcheck` over every tracked `*.sh` that CI runs).

**Shell scripts must run on macOS AND Linux** (users install on both; CI is ubuntu). `bash scripts/shellcheck.sh` is wired into the gate above and into CI as its own job. For the GNU-vs-BSD differences shellcheck cannot catch (`stat`, `date`, `sed -i`, bash 3.2), see [`scripts/PORTABILITY.md`](scripts/PORTABILITY.md).

**For SKILL.md changes:** The most important thing is that instructions stay prescriptive and complete. Vague instructions like "ask about habits" produce inconsistent results across different users and models. If you're changing how the skill behaves, spell out exactly what Claude should do, in what order, and with what fallbacks. See the existing phases for the right level of detail.

**For `docs/CHANGELOG.md`:** Add an entry in plain English explaining what changed and why it matters to the user — not what file was edited. For user-facing release notes, also add a short summary to `docs/RELEASES.md`.

---

## Testing bootstrap.sh changes safely

Editing `bootstrap.sh` is risky to test against your real machine — it touches `~/.claude/`, registers plugin marketplaces, edits `settings.json`, and installs system tools. The fix: a sandbox HOME.

`HOME` is what every Claude Code path resolves against (`~/.claude/`, skill clones, plugin installs, `settings.json`). Set `HOME` to a fresh empty directory, run bootstrap from there, and everything writes into the sandbox while your real config stays untouched.

```bash
SANDBOX=$(mktemp -d)
cd /path/to/your/local/ai-brain-starter
HOME="$SANDBOX" EMAIL_GATE_BYPASS=1 PREFLIGHT_BYPASS=1 bash bootstrap.sh
```

After the run, inspect `$SANDBOX/.claude/` to see what got created. To reset and re-test: `rm -rf "$SANDBOX"` and start over.

**Two flags worth knowing for tests:**

- `--dry-run` — shows what would be installed without making changes. Every install path respects this; if you add a new install step, wrap it in `do_cmd`, `git_clone_safe`, `claude_install_safe`, `claude_marketplace_safe`, or `pipx_install_safe` to keep dry-run actually dry.
- `--uninstall` — removes everything bootstrap installed (with a confirmation prompt). Useful for leaving a clean machine after testing.

**System tools (Brew, Node, Python, pipx, gh, Obsidian) are global**, so they short-circuit if already present on the host even with `HOME` redirected. That's fine — bootstrap's job is to install user-level Claude Code content, not duplicate system installs.

---

## What's welcome

- Bug fixes
- Clearer or more complete instructions in SKILL.md
- New examples in EXAMPLES.md
- Better phrasing in README or CHANGELOG
- Windows / Linux compatibility improvements
- New repair scenarios in the "Already Set Up?" section

## What's out of scope

- Turning this into a general-purpose productivity tool — it's designed around a specific opinionated workflow
- Adding complexity for edge cases most people won't hit
- Features that require external paid services (unless there's a meaningful free tier)
- Changing the tone — the voice throughout should stay warm, plain English, non-technical

---

## Questions?

Open an issue or reach out via [Substack](https://adelaidadiazroa.substack.com).
