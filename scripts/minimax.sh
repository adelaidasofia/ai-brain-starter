#!/usr/bin/env bash
# exit-contract: NOT-A-CHECKER -- a MiniMax API wrapper for cheap text
#   generation

# minimax.sh — cheap text processing via MiniMax M2.7
# Usage: minimax.sh "your prompt here" [max_tokens]
# Cost: ~$0.06/M tokens (vs ~$15/M for Claude Opus)
#
# Setup:
#   1. Get an API key at https://platform.minimax.io
#   2. Add to ~/.zshrc or ~/.bash_profile:
#        export MINIMAX_API_KEY="your-key-here"
#   3. source ~/.zshrc (or open a new shell)
#
# Good for: entity extraction, summarization, bulk tagging, boilerplate generation
# Not for: judgment calls, cross-file synthesis, writing in your voice

set -euo pipefail

PROMPT="${1:-}"
MAX_TOKENS="${2:-1000}"

if [[ -z "$PROMPT" ]]; then
  echo "Usage: minimax.sh \"prompt\" [max_tokens]" >&2
  exit 1
fi

# Load API key from environment or .zshrc
if [[ -z "${MINIMAX_API_KEY:-}" ]]; then
  if [[ -f "$HOME/.zshrc" ]]; then
    MINIMAX_API_KEY=$(grep -m1 'MINIMAX_API_KEY' "$HOME/.zshrc" | grep -oP '(?<=")[^"]+(?=")' || true)
  fi
  if [[ -z "${MINIMAX_API_KEY:-}" ]] && [[ -f "$HOME/.bash_profile" ]]; then
    MINIMAX_API_KEY=$(grep -m1 'MINIMAX_API_KEY' "$HOME/.bash_profile" | grep -oP '(?<=")[^"]+(?=")' || true)
  fi
fi

if [[ -z "${MINIMAX_API_KEY:-}" ]]; then
  echo "Error: MINIMAX_API_KEY not set. Add it to ~/.zshrc or export it." >&2
  exit 1
fi

# Build request payload safely (handles quotes, newlines, special chars)
PAYLOAD=$(python3 -c "
import json, sys
payload = {
    'model': 'MiniMax-M2.7',
    'messages': [{'role': 'user', 'content': sys.argv[1]}],
    'max_tokens': int(sys.argv[2])
}
print(json.dumps(payload))
" "$PROMPT" "$MAX_TOKENS")

# Call the API
RESPONSE=$(curl -s -X POST "https://api.minimax.io/v1/text/chatcompletion_v2" \
  -H "Authorization: Bearer $MINIMAX_API_KEY" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")

# Extract content
python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
if 'choices' in data and data['choices']:
    print(data['choices'][0]['message']['content'])
else:
    print('API error:', json.dumps(data), file=sys.stderr)
    sys.exit(1)
" <<< "$RESPONSE"
