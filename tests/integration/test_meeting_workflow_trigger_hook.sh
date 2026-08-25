#!/usr/bin/env bash
# Regression test for inject-meeting-workflow-on-trigger.py
#
# Bug class: ARTIFACT-WITHOUT-AUTOMATION-WIRING. The meeting-workflow.md
# rule existed in the vault and README/POWER_TOOLS.md promised the
# "I just had a meeting" cascade fired automatically — but no
# UserPromptSubmit hook surfaced the rule on the trigger phrase. Users
# reported saying "I just had a meeting" and getting nothing back.
#
# This test asserts:
#   1. The hook is present in hooks/.
#   2. It is wired into hooks.json under UserPromptSubmit.
#   3. EN trigger phrases fire ("I just had a meeting", variants).
#   4. ES trigger phrases fire ("acabo de tener una reunión", variants).
#   5. Non-trigger meeting mentions do NOT fire (future / past-week /
#      planning / asking about a meeting — temporal-anchor discipline).
#   6. The injected payload references meeting-workflow.md so the
#      assistant actually has the cascade in context.
#   7. The bypass env var suppresses the injection.
#
# Self-contained. Exit 0 = pass. Exit 1 = fail with details.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/inject-meeting-workflow-on-trigger.py"
HOOKS_JSON="$REPO_ROOT/hooks.json"

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

# 1. Hook present
[[ -f "$HOOK" ]] || fail "hook missing at $HOOK"
[[ -x "$HOOK" ]] || chmod +x "$HOOK"

# 2. Wired into hooks.json
grep -q "inject-meeting-workflow-on-trigger.py" "$HOOKS_JSON" || \
    fail "hook not wired into hooks.json — add a UserPromptSubmit entry"

# Build a throwaway vault with a real meeting-workflow.md so the hook
# resolves a rule path (otherwise it falls back to the embedded summary,
# which is also valid but we want to verify the preferred path works).
TMP_VAULT="$(mktemp -d)"
trap 'rm -rf "$TMP_VAULT"' EXIT
mkdir -p "$TMP_VAULT/Meta/rules"
cat > "$TMP_VAULT/Meta/rules/meeting-workflow.md" <<'EOF'
---
type: rule
trigger: "I just had a meeting"
---

# Meeting workflow stub

Find the transcript. Read it. Enrich the meeting note. Update CRM.
Update to-dos. Update Decision Log. Verify backlinks. Report.
EOF
mkdir -p "$TMP_VAULT/Meta"
touch "$TMP_VAULT/Meta/Current Priorities.md"

run_hook() {
    local prompt="$1"
    local extra_env="${2:-}"
    local payload
    payload=$(python3 -c "import json,sys; print(json.dumps({'prompt': sys.argv[1]}))" "$prompt")
    if [[ -n "$extra_env" ]]; then
        eval "$extra_env" VAULT_ROOT="$TMP_VAULT" python3 "$HOOK" <<<"$payload"
    else
        VAULT_ROOT="$TMP_VAULT" python3 "$HOOK" <<<"$payload"
    fi
}

# 3. EN triggers FIRE
EN_POSITIVE=(
    # Standard "I just <verb> <det> <noun>"
    "I just had a meeting with Sara"
    "i just finished a call"
    "I just wrapped up the standup"
    "I just got out of a 1:1"
    "The meeting just ended"
    "the meeting is done"
    "meeting with John just ended"
    "pull the transcript"
    "pull my meeting notes"
    "process today's meeting"
    "file the meeting note"
    "Diego's meeting is done"
    "done with my interview"
    "wrapped up the sync"
    # Compound nouns (det + adj×0-3 + noun)
    "I just had my discovery call with the prospect"
    "I just got out of the kickoff call"
    "I just finished my client interview"
    "I just wrapped up the all-hands meeting"
    "1:1 with my manager just ended"
    "meeting with the founders just ended"
    "pull my notes from this morning's meeting"
    # Terse forms (no "I")
    "Just had a great call!"
    "just wrapped the demo"
    "done with the workshop"
    "got off the phone"
    # Artifact pulls + capture verbs
    "extract action items from the sync"
    "capture the to-dos from the meeting"
    # Natural phrasings that silently missed before (measured against a
    # 36-phrase probe of how people actually type this). Each maps to one
    # trigger added in the same change:
    "meeting's done"                      # contracted "is" riding on the noun
    "the standup's over"                  # same, different noun
    "that meeting is finally over"        # affirmative adverb between is/over
    "had a meeting just now"              # postfix "just now" temporal anchor
    "just did a call with the client"     # "did" as a completion verb
    "finished a call with the client"     # bare finished + core meeting noun
)
for p in "${EN_POSITIVE[@]}"; do
    out=$(run_hook "$p")
    if [[ -z "$out" ]]; then
        fail "EN trigger did NOT fire: $p"
    fi
    echo "$out" | grep -q "meeting-workflow auto-injected" || \
        fail "EN trigger fired but missing header marker: $p (output: $out)"
done

# 4. ES triggers FIRE (accent-insensitive)
ES_POSITIVE=(
    "Acabo de tener una reunión con Diego"
    "acabo de tener una reunion"
    "acabo de terminar la llamada"
    "acabo de salir de una junta"
    "acabo de salir del kickoff"
    "la reunión ya terminó"
    "la reunion ya termino"
    "mi reunión recién acabó"
    "ya terminé la reunión"
    "ya acabé mi llamada"
    "trae las notas de la reunión"
    "saca el transcript"
    "reunión con María ya terminó"
    # ES compound nouns
    "Acabo de tener una llamada de descubrimiento"
    "ya terminé mi entrevista con el cliente"
    "mi sync con producto recién terminó"
    # Bilingual mixing (EN noun, ES verb)
    "acabo de tener un meeting con el cliente"
    "ya terminé el call con el equipo"
)
for p in "${ES_POSITIVE[@]}"; do
    out=$(run_hook "$p")
    if [[ -z "$out" ]]; then
        fail "ES trigger did NOT fire: $p"
    fi
done

# 5. Negative cases — must NOT fire
NEGATIVE=(
    # Future
    "I have a meeting tomorrow"
    "I'm going to a meeting later"
    "Cancel my meeting"
    "Tengo una reunión mañana"
    "Voy a una reunión luego"
    "Schedule a meeting with John for next week"
    "the meeting is tomorrow"
    "the meeting will be on Tuesday"
    # Past without "just"
    "I had a meeting last week"
    "What was that meeting about?"
    "what did we discuss in yesterday's meeting?"
    "I had a great meeting last month"
    "tuvimos una reunión la semana pasada"
    "remember our meeting from March?"
    # Asking / planning / referring
    "Looking forward to the meeting"
    "I need to prep for a meeting"
    "Did you join the meeting?"
    "let's plan the agenda for the meeting"
    "¿De qué se trata la reunión?"
    "Necesito prepararme para la reunión"
    "¿quién estaba en la reunión?"
    "agenda una reunión con Diego"
    "estoy preparando la reunión"
    "what's on my calendar"
    "what's on my agenda for the meeting?"
    "who's invited to the meeting?"
    # Current / mid
    "I'm in a meeting right now"
    "estoy en una reunión ahora"
    "currently on a call"
    # Verb-of-action / code-context (FP risk)
    "build a new feature"
    "I just had to call the bank"
    "pull request review"
    "transcript me this YouTube video"
    "pull the latest deployment logs"
    "review this design"
    "process this CSV file"
    "I just had lunch"
    "I just had coffee"
    # Negation + hedges around the completion verb. These read as "done"
    # lexically but mean the meeting is STILL RUNNING. Guards ADV_DONE being
    # an allow-list; a permissive adverb slot fires the cascade on all of them.
    "the meeting is not done yet"
    "the call is not over"
    "my meeting isn't done"
    "the meeting is almost over"
    "the call is nearly finished"
    "the meeting is never over"
    "is the meeting done?"
    "is the call over yet?"
    # Bare finished/ended + an AMBIGUOUS noun is ordinary work talk, not a
    # meeting signal. Guards NOUN_EN_CORE excluding review/session/chat.
    "finished the review of the contract"
    "ended the session early"
    "saca un café"
    "trae el café"
)
for p in "${NEGATIVE[@]}"; do
    out=$(run_hook "$p" 2>/dev/null || true)
    if [[ -n "$out" ]]; then
        fail "negative case incorrectly fired: $p (output: $out)"
    fi
done

# 6. Injected payload contains the rule body (vault path preferred)
out=$(run_hook "I just had a meeting")
echo "$out" | grep -q "meeting-workflow.md" || \
    fail "injected payload does not reference meeting-workflow.md (output: $out)"
echo "$out" | grep -q "Meeting workflow stub" || \
    fail "injected payload missing rule body from vault (output: $out)"

# 7. Bypass env var
out=$(run_hook "I just had a meeting" "MEETING_WORKFLOW_BYPASS=1")
if [[ -n "$out" ]]; then
    fail "bypass env var did not suppress injection (output: $out)"
fi

# 8. Bypass via in-prompt token
out=$(run_hook "MEETING_WORKFLOW_BYPASS=1 I just had a meeting")
if [[ -n "$out" ]]; then
    fail "in-prompt bypass token did not suppress injection (output: $out)"
fi

# 9. Empty / missing prompt -> silent exit
out=$(printf '{}' | VAULT_ROOT="$TMP_VAULT" python3 "$HOOK")
if [[ -n "$out" ]]; then
    fail "empty prompt should produce no output (output: $out)"
fi

# 10. Fallback path: no vault rule file, no template -> embedded summary still fires
EMPTY_VAULT="$(mktemp -d)"
mkdir -p "$EMPTY_VAULT/Meta"
touch "$EMPTY_VAULT/Meta/Current Priorities.md"
out=$(printf '{"prompt": "I just had a meeting"}' | \
    VAULT_ROOT="$EMPTY_VAULT" \
    HOME="$EMPTY_VAULT" \
    python3 "$HOOK")
rm -rf "$EMPTY_VAULT"
if [[ -z "$out" ]]; then
    fail "fallback summary did not fire when no rule file exists"
fi
echo "$out" | grep -q "meeting-workflow fallback" || \
    fail "fallback summary missing marker (output: $out)"

echo "PASS: inject-meeting-workflow-on-trigger.py wired + EN/ES triggers fire + negatives don't fire + bypass + fallback"
