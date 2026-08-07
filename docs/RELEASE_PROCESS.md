---
name: release-process
description: Maintainer-facing release procedure for ai-brain-starter — how to cut a tag, what the workflow does, what artifacts ship.
---

# Release Process

Maintainer-facing reference. User-facing release notes are in [`RELEASES.md`](RELEASES.md). Full development history is in [`CHANGELOG.md`](CHANGELOG.md).

## Two install paths in parallel

1. **Email-gated full bootstrap** at [`myceliumai.co/install`](https://myceliumai.co/install) — sets up the Obsidian vault, hooks, resolver, meeting workflow, everything that compounds across sessions. Recommended path for new users. Email gate is intentional (lead capture for paid cohorts and consulting).
2. **`--plugin-url` quick-try** for existing Claude Code 2.1.129+ users — loads the plugin (skills, commands, agents) for the current session only against an existing vault. Frame as evaluation, not substitute. Documented in [`README`](../README.md#quick-try-existing-claude-code-users).

The `--plugin-url` path requires a published GitHub release with a `.zip` artifact at the stable URL.

## Cut a release

```bash
# 1. Bump version in BOTH manifests
#    .claude-plugin/plugin.json      "version": "X.Y.Z"
#    .claude-plugin/marketplace.json "version": "X.Y.Z" (inside plugins[0])
# 2. Add a CHANGELOG entry at the top of docs/CHANGELOG.md
# 3. Add a user-facing entry at the top of docs/RELEASES.md
# 4. Commit on main
git add .claude-plugin/plugin.json .claude-plugin/marketplace.json docs/CHANGELOG.md docs/RELEASES.md
git commit -m "chore: bump to vX.Y.Z"
git push origin main

# 5. Tag and push
git tag -a vX.Y.Z -m "vX.Y.Z — short summary"
git push origin vX.Y.Z
```

The tag push triggers `.github/workflows/release.yml`. Ships in seconds. GitHub release publishes with auto-generated notes from PRs merged since the previous tag.

Semver: `vX.Y.0` for new features, `vX.Y.Z` for fixes, `vN.0.0` for breaking install-path or schema changes.

## What `release.yml` does

1. **Checkout the tag's tree** — `actions/checkout@v4` with `ref: ${{ github.ref }}`.
2. **Validate plugin manifest** — confirms `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` exist and parse as JSON.
3. **Build staged tree** — `rsync` to `/tmp/ai-brain-starter`, excluding `.git`, `.github`, `node_modules`, `__pycache__`, `.venv`, `*.bak`, `.DS_Store`, secrets (`.env`, `.env.*`, `*.key`, `*.pem`, `*.pfx`, `*.p12`, `secrets.json`, `.zsh_secrets`), per-vault state (`.claude/settings.local.json`, `.promote-state.json`, `.driftignore`). `.env.example` explicitly included.
4. **Archive** — `zip -rq` produces `ai-brain-starter.zip`; `tar czf` produces `ai-brain-starter.tar.gz`. Both archives have the `ai-brain-starter` directory at root, which is what `--plugin-url` expects.
5. **Sign** — `sha256sum` produces `.sha256` files alongside both archives.
6. **Publish** — `gh release create $TAG --generate-notes` creates the release. `--clobber` upload on workflow_dispatch re-runs.

## Artifacts

| Asset | Purpose |
|---|---|
| `ai-brain-starter.zip` | Plugin archive consumable by `claude --plugin-url` |
| `ai-brain-starter.zip.sha256` | SHA256 of the zip |
| `ai-brain-starter.tar.gz` | Cross-platform alternative archive |
| `ai-brain-starter.tar.gz.sha256` | SHA256 of the tarball |

Stable URL pattern (latest, regardless of version):

```
https://github.com/mycelium-hq/ai-brain-starter/releases/latest/download/ai-brain-starter.zip
https://github.com/mycelium-hq/ai-brain-starter/releases/latest/download/ai-brain-starter.zip.sha256
```

## Privacy gating (PR-scoped)

`.github/workflows/lint.yml` includes a `privacy` job that fires on every pull request. Scans files **changed in the PR** for tokens that match the maintainer's local hookify guard. Pre-existing committed content is never re-scanned (already passed hookify on every prior write). Pushes to main bypass — local hookify catches direct pushes; only external contributions need CI gating.

If the maintainer's hookify rule changes, update the CI scan in `lint.yml` to match.

## Re-run a release

If a release needs new assets (workflow bug shipped bad zip):

```bash
gh workflow run release.yml -f tag=vX.Y.Z
```

Workflow detects the existing release, uploads assets with `--clobber`. Release notes are not regenerated.

## Manual smoke-test (no real tag)

For workflow verification without a real release:

```bash
gh workflow run release.yml -f tag=v0.0.0-test
# verify, then clean up:
gh release delete v0.0.0-test --yes
git push origin --delete v0.0.0-test
```

## See also

- `.github/workflows/release.yml` — the workflow itself
- `.github/workflows/lint.yml` — `privacy` job + other PR gates
- `docs/CHANGELOG.md` — version history
- `docs/RELEASES.md` — user-facing release notes
