# vendor/high-rise — pinned copy of the open-source High-Rise framework

This directory is a **vendored, pinned** copy of the open-source **High-Rise
framework**, whose single source of truth is
[`Fundacion-Lontananza/high-rise`](https://github.com/Fundacion-Lontananza/high-rise)
(public, MIT). ai-brain-starter **consumes** the framework as a downstream
dependency; it does not own it and does not keep a divergent copy.

## What's here

| File | What it is |
|---|---|
| `floors.md` | The canonical 34-floor model: floors, tiers, elevator emotions, shadow twins, EN + ES. |
| `methodology/journaling.md` | The journaling methodology (the floor-tagged daily check-in). |
| `methodology/coaching.md` | The coaching methodology. |
| `PIN.json` | The pin: upstream repo, tag, the commit that tag pointed at, and a sha256 for every vendored file. |

These files are **byte-identical to the upstream tag** recorded in `PIN.json`.
Everything in this repo that needs the framework (for example
`scripts/generate_floor_stubs.py`, which regenerates `floors/` from the
canonical list) reads it from here.

## Why vendored, not a git submodule

The substrate installs by `git clone`ing this repo into
`~/.claude/skills/ai-brain-starter`. A submodule would force every user onto
`git clone --recursive` (and silently break the installs that don't pass it)
and would reach the network at install time. Vendored files ship **inside the
clone**: the framework is present offline, at a pinned version, with zero
install-time network. That is the install-safe mechanism.

## Do not hand-edit these files

The framework is upstream's to change. To update:

```bash
# refresh from the CURRENT pinned tag (re-fetch, rewrite files + hashes)
python3 scripts/sync-high-rise.py

# move to a NEW upstream release (deliberate, reviewable re-pin)
python3 scripts/sync-high-rise.py --tag v0.2.0
```

CI runs `python3 scripts/sync-high-rise.py --check` on every PR: it fails if a
vendored file was edited by hand (its sha256 no longer matches `PIN.json`). Fix
a real content problem in `Fundacion-Lontananza/high-rise`, cut a release, then
re-pin here.
