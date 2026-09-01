# 7. The home refusal has no `--force` escape

Date: 2026-08-19
Status: Accepted
Ticket: MYC-4028

## Context

The relocate helpers take a `<vault-path>` and, when it has no `.git`, create a
repository there. Validation was two checks: non-empty, and is a directory. Aim
one at `$HOME` and the user's home becomes a git working tree with `~/.ssh`,
`~/.aws`, `~/.netrc` and `~/.config/gh` inside it, one `git add -A` from being
written into git objects permanently. Objects survive every later delete, and a
subsequent `git remote add` publishes them.

`scripts/check-vault-target.py` now refuses three classes. Two of them —
`REFUSE_CREDENTIALS` and `REFUSE_NOT_A_VAULT` — are heuristics and the callers
honour `--force` on them, loudly.

`REFUSE_HOME` does not. Every other refusal in these scripts is forceable
(`--force` already overrides the live-worktree guard, the cloud-sync guard and
the backup gate), so this is a deliberate break from the file's own convention
and needs recording.

## Decision

`$HOME`, a filesystem root, a strict ancestor of `$HOME`, and system directories
are refused unconditionally. No flag, no environment variable, no override.

## Consequences

**What this costs.** A user with a genuine reason to make their home directory
an AI-brain vault cannot use these tools. They are not blocked from the outcome
— `git init` in `$HOME` themselves, then re-run, and the tool takes the
`separated` path instead of `fresh-init`. So the escape hatch exists; it just
requires stating the intent in a way a mistyped path never does.

**Why not a flag.** Three reasons, in order of weight:

1. The population this protects is non-technical by definition. A flag is only
   reachable by someone who read the refusal, and someone who reads a refusal
   carefully is not the person who typed the wrong path.
2. An escape hatch is what gets reached for under pressure. This codebase has
   measured that: agents told not to set `*_BYPASS` variables have set them
   anyway when a gate blocked them late in a task. A documented override on a
   credential-exposure guard is a comment, not a control.
3. The failure is silent, permanent and delayed. Unlike the cloud-sync melt
   (loud, immediate, reversible), nothing tells the user their SSH key is in a
   git object until the day the repo gains a remote.

**What stays reachable.** `--rollback` is deliberately NOT gated. A machine
already in this state must be able to undo it, and a guard that blocks the
repair path traps exactly the users it exists to protect. `test-check-vault-target.sh`
case E4 pins this.

**The risk we accepted.** If the refusal is ever wrong — a path shape we did not
anticipate that resolves to something home-like but is a legitimate vault — the
user has no runtime escape and must wait for a release. We judged a rare
hard-block cheaper than a common silent credential leak, and left the
`separated` path open as the pressure valve.

## Notes

The system-root list is compared **resolved**, not as strings: on macOS `/tmp`
is a symlink to `/private/tmp`, and a literal match waves it through. Case A4 in
the test suite pins that.

A second implementation of this decision lives in `mycelium-studio`
(`apps/desktop/src-tauri/src/commands/vault_safety.rs`). Both are declared under
`vault-target-refusal` in `scripts/paired-implementations.json`; read the
counterpart itself for what it enforces today.

This note deliberately does not describe that implementation's state. The
previous version did — it said the Rust twin had not taken this decision yet —
and it was written at 04:55:58Z and refuted at 05:25:12Z the same morning when
MYC-4035 landed the port. It then sat wrong for eight days, through another edit
to the file it described, because nothing in this repo runs when that one merges.
Naming a counterpart is durable; describing it is not.
`scripts/check-paired-implementations.py` (G4) now fails any such sentence.
