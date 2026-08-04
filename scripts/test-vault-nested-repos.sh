#!/usr/bin/env bash
# Controls for the property-based nested-checkout exclusion.
#
# The bug: vault-backup.sh's exclude list names `./.claude/worktrees`, which is a
# denylist against an open set. On a real install a second agent CLI used a name
# the list did not cover, parked eleven checkouts of three unrelated repos in the
# vault, and added 37 GB to the backup source — pushing the offsite provider past
# its storage cap and leaving the offsite copy stale for 50+ days.
#
# These controls prove the property-based scan, and prove the fail-safe half: a
# nested repo with NO remote must stay IN the backup, because the vault may be
# its only copy. The last case asserts against a REAL tar archive, not just the
# scanner's opinion — the archive is what actually ships.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCANNER="$HERE/vault-nested-repos.py"
FAILS=0

ok()   { echo "  ok   $*"; }
bad()  { echo "  FAIL $*"; FAILS=$((FAILS+1)); }
have() { grep -qxF "$1" "$2"; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

VAULT="$TMP/vault"
mkdir -p "$VAULT/notes"
git init -q "$VAULT"; git -C "$VAULT" config user.email t@t; git -C "$VAULT" config user.name t
echo hi > "$VAULT/notes/a.md"

mkrepo() { # <path> [remote]
  mkdir -p "$1"; git init -q "$1"
  git -C "$1" config user.email t@t; git -C "$1" config user.name t
  echo x > "$1/f.txt"; git -C "$1" add f.txt; git -C "$1" commit -qm c
  [ -n "${2:-}" ] && git -C "$1" remote add origin "$2"
  return 0
}

# A checkout under a directory name NOTHING in this repo has ever listed.
mkrepo "$VAULT/.some-other-agent-worktrees/proj" "https://example.invalid/proj.git"
# A no-remote project: irreplaceable, must be KEPT.
mkrepo "$VAULT/media/no-remote-project"

OUT="$TMP/excl.txt"
python3 "$SCANNER" --vault-root "$VAULT" --relative --exclude-file "$OUT" >/dev/null 2>&1

echo "- property-based detection"
have "./.some-other-agent-worktrees/proj" "$OUT" \
  && ok "checkout under an unlisted directory name is excluded" \
  || bad "unlisted-name checkout NOT excluded (this is the whole bug)"

echo "- fail-safe: no-remote repos are never silently dropped"
if have "./media/no-remote-project" "$OUT"; then
  bad "a no-remote repo was excluded — that is data loss, not a fix"
else
  ok "no-remote repo kept in the backup"
fi

echo "- the vault's own history is never excluded"
if have "./.git" "$OUT" || have "." "$OUT"; then
  bad "the vault itself/its .git was excluded"
else
  ok "vault root and its .git are untouched"
fi

echo "- negative control: a clean vault yields nothing"
CLEAN="$TMP/clean"; mkdir -p "$CLEAN/notes"; git init -q "$CLEAN"
python3 "$SCANNER" --vault-root "$CLEAN" --relative --exclude-file "$TMP/clean.txt" >/dev/null 2>&1
if [ "$(grep -cvE '^#|^$' "$TMP/clean.txt")" = "0" ]; then
  ok "no false positives on a vault with no foreign checkout"
else
  bad "false positive on a clean vault: $(grep -vE '^#|^$' "$TMP/clean.txt")"
fi

echo "- END-TO-END: the real tar archive must not contain the foreign checkout"
# This is the predicate the RECIPIENT exercises — the archive, not the opinion.
excl=()
while IFS= read -r l; do
  case "$l" in ''|'#'*) continue;; esac
  excl+=("--exclude=$l")
done < "$OUT"
tar -czf "$TMP/a.tar.gz" "${excl[@]}" -C "$VAULT" . 2>/dev/null
LIST="$TMP/list.txt"; tar -tzf "$TMP/a.tar.gz" > "$LIST" 2>/dev/null
# Assert on the checkout's CONTENTS, which is what costs storage. tar still
# emits a zero-byte entry for the parent directory that merely CONTAINS the
# excluded checkout; that is correct and carries no data, so a substring match
# on the parent's name would be a false failure.
if grep -q 'some-other-agent-worktrees/proj' "$LIST"; then
  bad "foreign checkout contents ARE in the archive"
else
  ok "foreign checkout contents absent from the archive"
fi
if [ "$(grep -c 'some-other-agent-worktrees' "$LIST")" -le 1 ]; then
  ok "only the empty parent dir entry remains (no payload)"
else
  bad "more than the parent dir survived: $(grep 'some-other-agent-worktrees' "$LIST")"
fi
if grep -q 'no-remote-project/f.txt' "$LIST"; then
  ok "no-remote project IS in the archive (its only copy survives)"
else
  bad "no-remote project missing from the archive — data loss"
fi
if grep -q 'notes/a.md' "$LIST"; then
  ok "ordinary vault notes are still archived"
else
  bad "vault notes missing from the archive"
fi

echo
if [ "$FAILS" -gt 0 ]; then echo "FAILED ($FAILS)"; exit 1; fi
echo "all green"
