#!/usr/bin/env bash
# test_ai_brain_auto_update.sh — negative-control gate for the substrate
# auto-updater's REACH GUARANTEE (MYC-720).
#
# WHY: the prior inline hook pulled but DELEGATED the install step to the model,
# so a merged substrate PR silently did not run until a manual re-install — the
# "deployed checkout 40 -> 131 behind, nobody noticed" recurrence. This gate
# proves the updater now DEPLOYS on its own when HEAD moves, and stays hands-off
# in every case it must not touch.
#
# Each case stands up an ISOLATED fake HOME + a fake ai-brain-starter checkout
# whose bare origin/main is one commit ahead, with STUB scripts/sync-skills.sh
# and scripts/install-hooks-user-level.py. The install stub writes DEPLOY_RAN, so
# a test can assert deploy-on-pull FIRED without invoking the real installer.
#
#   T1  behind + clean      -> ff-pulls AND deploys (DEPLOY_RAN written)   [REACH]
#   T2  pinned              -> silent, no fetch, no deploy                 [NEG]
#   T3  already up-to-date  -> silent, no deploy                           [NEG]
#   T4  rate-limited        -> silent, no deploy                           [NEG]
#   T5  dirty tree          -> BLOCKED message, no merge, no deploy        [NEG]
#   T6  divergent fork      -> BLOCKED message, no ff, no deploy           [NEG]
#
# Run: bash tests/integration/test_ai_brain_auto_update.sh  (0 = pass, 1 = fail)
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/ai-brain-auto-update.sh"
[ -f "$SCRIPT" ] || { echo "ERROR: $SCRIPT not found" >&2; exit 1; }

PASS=0; FAIL=0
ok(){ printf '  PASS: %s\n' "$1"; PASS=$((PASS+1)); }
no(){ printf '  FAIL: %s\n' "$1"; FAIL=$((FAIL+1)); }
TMPROOT="$(mktemp -d)"; trap 'rm -rf "$TMPROOT"' EXIT

# Fresh isolated state dir + a fake checkout 1 commit BEHIND its bare origin, with
# stub sync-skills.sh + install-hooks-user-level.py (the latter writes DEPLOY_RAN
# into the state dir). Echoes "<state_dir>\t<checkout>".
new_fixture() {
  local dir state origin repo
  dir=$(mktemp -d "$TMPROOT/fx.XXXXXX")
  state="$dir/state"; mkdir -p "$state"
  origin="$dir/origin.git"
  repo="$dir/checkout"
  git -c init.defaultBranch=main init -q --bare "$origin"
  git -c init.defaultBranch=main clone -q "$origin" "$repo" 2>/dev/null
  (
    cd "$repo" || exit 1
    git config user.email t@t; git config user.name t
    git symbolic-ref HEAD refs/heads/main
    mkdir -p scripts docs
    printf 'echo "sync ok"\n' > scripts/sync-skills.sh
    # install stub: honors ABS_UPDATE_STATE_DIR (inherited env) + writes the marker.
    printf '#!/usr/bin/env python3\nimport os, pathlib\nd=os.environ.get("ABS_UPDATE_STATE_DIR", os.path.expanduser("~/.claude"))\npathlib.Path(d, "DEPLOY_RAN").write_text("ran")\n' > scripts/install-hooks-user-level.py
    printf '# Changelog\n\n## latest\nnew stuff\n' > docs/CHANGELOG.md
    printf 'seed\n' > seed.txt
    git add -A; git commit -qm seed
    git push -q -u origin main
    # advance origin one commit beyond the working clone -> clone is behind by 1
    printf 'upstream\n' > upstream.txt
    git add upstream.txt; git commit -qm "upstream ahead"
    git push -q origin main
    git reset -q --hard HEAD~1
  )
  printf '%s\t%s' "$state" "$repo"
}

# run the updater against a fixture. $1=state $2=checkout ; extra env via caller.
run_upd() {
  OUT="$(ABS_UPDATE_STATE_DIR="$1" ABS_SKILL_DIR="$2" ABS_UPDATE_INTERVAL_DAYS="${INTERVAL:-0}" \
        ABS_UPDATE_DEPLOY_TIMEOUT=30 bash "$SCRIPT" 2>/dev/null)"
}
deployed(){ [ -f "$1/DEPLOY_RAN" ]; }
says(){ printf '%s' "$OUT" | grep -q "$1"; }

# ---- T1. REACH: behind + clean -> ff-pull AND deploy -------------------------
IFS=$'\t' read -r ST CO < <(new_fixture)
run_upd "$ST" "$CO"
head=$(git -C "$CO" rev-parse HEAD); om=$(git -C "$CO" rev-parse origin/main)
if [ "$head" = "$om" ] && deployed "$ST" && says 'auto-updated'; then
  ok "T1: HEAD reached origin/main AND deploy fired AND update surfaced"
else
  no "T1: reach/deploy failed (head==om:$([ "$head" = "$om" ] && echo y || echo n) deploy:$(deployed "$ST" && echo y || echo n))"
fi

# ---- T2. NEG: pinned -> no fetch, no deploy, silent --------------------------
IFS=$'\t' read -r ST CO < <(new_fixture)
touch "$ST/.ai-brain-starter-pinned"
before=$(git -C "$CO" rev-parse HEAD)
run_upd "$ST" "$CO"
after=$(git -C "$CO" rev-parse HEAD)
if [ "$before" = "$after" ] && ! deployed "$ST" && says 'suppressOutput'; then
  ok "T2: pinned -> silent, HEAD unchanged, no deploy"
else
  no "T2: pin not honored (HEAD $before->$after deploy:$(deployed "$ST" && echo y || echo n))"
fi

# ---- T3. NEG: already up-to-date -> silent, no deploy ------------------------
IFS=$'\t' read -r ST CO < <(new_fixture)
git -C "$CO" merge --ff-only origin/main --quiet   # make it current first
run_upd "$ST" "$CO"
if ! deployed "$ST" && says 'suppressOutput'; then
  ok "T3: up-to-date -> silent, no re-deploy"
else
  no "T3: re-deployed/loud when already current (deploy:$(deployed "$ST" && echo y || echo n))"
fi

# ---- T4. NEG: rate-limited (fresh LAST, interval 6d) -> silent ---------------
IFS=$'\t' read -r ST CO < <(new_fixture)
touch "$ST/.ai-brain-starter-last-update"          # just ran -> inside the window
before=$(git -C "$CO" rev-parse HEAD)
INTERVAL=6 run_upd "$ST" "$CO"
after=$(git -C "$CO" rev-parse HEAD)
if [ "$before" = "$after" ] && ! deployed "$ST"; then
  ok "T4: within rate-limit window -> no pull, no deploy"
else
  no "T4: ran inside the rate-limit window (HEAD $before->$after)"
fi

# ---- T5. NEG: dirty TRACKED file -> BLOCKED, no merge, no deploy -------------
IFS=$'\t' read -r ST CO < <(new_fixture)
printf 'handedit\n' >> "$CO/seed.txt"              # modify a TRACKED file
before=$(git -C "$CO" rev-parse HEAD)
run_upd "$ST" "$CO"
after=$(git -C "$CO" rev-parse HEAD)
if [ "$before" = "$after" ] && ! deployed "$ST" && says 'BLOCKED'; then
  ok "T5: dirty tracked file -> BLOCKED, no merge, no deploy"
else
  no "T5: dirty tracked file pulled/deployed (HEAD $before->$after deploy:$(deployed "$ST" && echo y || echo n))"
fi

# ---- T7. untracked file present -> ff STILL proceeds + deploys ---------------
# The updater's OWN .sync.log / .bak-* land in the checkout as untracked files;
# they must NOT block the pull, else the updater self-blocks forever after run 1.
IFS=$'\t' read -r ST CO < <(new_fixture)
printf 'runtime\n' > "$CO/.sync.log"               # untracked runtime artifact
run_upd "$ST" "$CO"
head=$(git -C "$CO" rev-parse HEAD); om=$(git -C "$CO" rev-parse origin/main)
if [ "$head" = "$om" ] && deployed "$ST"; then
  ok "T7: untracked runtime file does NOT block the ff-pull + deploy"
else
  no "T7: untracked file wrongly blocked the update (head==om:$([ "$head" = "$om" ] && echo y || echo n) deploy:$(deployed "$ST" && echo y || echo n))"
fi

# ---- T6. NEG: divergent fork -> BLOCKED, no ff, no deploy --------------------
IFS=$'\t' read -r ST CO < <(new_fixture)
git -C "$CO" -c user.email=t@t -c user.name=t commit -q --allow-empty -m "local diverge"
before=$(git -C "$CO" rev-parse HEAD)
run_upd "$ST" "$CO"
after=$(git -C "$CO" rev-parse HEAD)
if [ "$before" = "$after" ] && ! deployed "$ST" && says 'diverged'; then
  ok "T6: divergent fork -> BLOCKED, no ff, no deploy"
else
  no "T6: divergent fork was merged/deployed (HEAD $before->$after)"
fi

echo
echo "test_ai_brain_auto_update: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
