#!/usr/bin/env bash
# Check Claude Code installed version against the latest GitHub release.
# When behind, also surfaces what's new (bullets between current and latest)
# so the user sees WHAT changed, not just THAT she's behind.
# Caches result for 24h to avoid hammering GitHub.
#
# Wired into SessionStart. Permanent guard against (a) falling behind on releases
# AND (b) missing release-payload features that should drive setup changes.
# Cantrill blind-spot fix codified 2026-05-08: the previous version was a tier-1
# alarm without payload — surfaced "you're behind" but not "here's the diff that
# changed worktree.baseRef behavior."

set -uo pipefail

# --- ai-brain-starter: shim-safe PATH (strip refuse-shims) ----------------
# Some machines carry a python3/python PATH shim (e.g. trailofbits
# modern-python) that exit-1s on bare invocation and would turn every bare
# python call below into a silent no-op. Drop any */hooks/shims dir from PATH
# so bare python calls here (and, via export, in children) hit a real python.
if [ "${PATH#*/hooks/shims}" != "$PATH" ]; then
  _abs_new=""; _abs_oifs=$IFS; IFS=:
  for _abs_d in $PATH; do
    case $_abs_d in */hooks/shims|*/hooks/shims/) ;; *) _abs_new=${_abs_new:+$_abs_new:}$_abs_d ;; esac
  done
  IFS=$_abs_oifs; PATH=$_abs_new; export PATH
  unset _abs_new _abs_d _abs_oifs
fi
# --------------------------------------------------------------------------

CACHE_FILE="$HOME/.claude/.claude-code-version-check"
CACHE_TTL_SEC=$((24 * 60 * 60))   # 24 hours
WARN_VERSION_GAP=3                # warn loudly if behind by N or more patch versions
DIFF_BULLET_LIMIT=8               # max bullets to surface from the changelog diff

now=$(date +%s)
if [[ -f "$CACHE_FILE" ]]; then
  last=$(stat -f %m "$CACHE_FILE" 2>/dev/null || stat -c %Y "$CACHE_FILE" 2>/dev/null || echo 0)
  age=$(( now - last ))
  if (( age < CACHE_TTL_SEC )); then
    cat "$CACHE_FILE"
    exit 0
  fi
fi

# Need gh CLI; if missing, exit silently
if ! command -v gh >/dev/null 2>&1; then
  exit 0
fi

current=$(claude --version 2>/dev/null | awk '{print $1}')
[[ -z "$current" ]] && exit 0

latest=$(gh api repos/anthropics/claude-code/releases/latest --jq .tag_name 2>/dev/null | sed 's/^v//')
[[ -z "$latest" ]] && exit 0

# Up to date — short message, cache, exit.
if [[ "$current" == "$latest" ]]; then
  : > "$CACHE_FILE"
  exit 0
fi

cur_patch=$(echo "$current" | awk -F. '{print $3}')
lat_patch=$(echo "$latest" | awk -F. '{print $3}')
gap=""
[[ -n "$cur_patch" && -n "$lat_patch" ]] && gap=$(( lat_patch - cur_patch ))

# Build the headline.
if [[ -n "$gap" ]] && (( gap >= WARN_VERSION_GAP )); then
  headline="[claude-code-version] $current → latest $latest ($gap versions behind). Upgrade: npm i -g @anthropic-ai/claude-code@latest"
else
  headline="[claude-code-version] $current → latest $latest. Upgrade when convenient: npm i -g @anthropic-ai/claude-code@latest"
fi

# Fetch the CHANGELOG.md diff between current and latest. Best-effort:
# any failure here just means we fall back to the headline-only message.
# Use a temp file rather than a pipe to avoid SIGPIPE in nested heredocs.
diff_block=""
changelog_tmp=$(mktemp -t claude-code-changelog.XXXXXX 2>/dev/null)
if [[ -n "$changelog_tmp" ]]; then
  if gh api repos/anthropics/claude-code/contents/CHANGELOG.md \
       -H "Accept: application/vnd.github.raw" \
       > "$changelog_tmp" 2>/dev/null && [[ -s "$changelog_tmp" ]]; then
    diff_block=$(python3 - "$current" "$latest" "$DIFF_BULLET_LIMIT" "$changelog_tmp" <<'PY' 2>/dev/null
import sys, re
current = sys.argv[1]
latest = sys.argv[2]
limit = int(sys.argv[3])
text = open(sys.argv[4]).read()

sections = re.split(r'^## ', text, flags=re.MULTILINE)

def parse_version(s):
    m = re.match(r'^(\d+)\.(\d+)\.(\d+)', s.strip())
    return tuple(int(x) for x in m.groups()) if m else None

cur_t = parse_version(current)
lat_t = parse_version(latest)
if not cur_t or not lat_t:
    sys.exit(0)

picked = []
for sec in sections[1:]:
    head_line = sec.split('\n', 1)[0].strip()
    v = parse_version(head_line)
    if not v:
        continue
    if cur_t < v <= lat_t:
        picked.append((v, head_line, sec))

if not picked:
    sys.exit(0)

picked.sort(reverse=True)

bullets = []
for v, head, sec in picked:
    body = sec.split('\n', 1)[1] if '\n' in sec else ''
    for line in body.splitlines():
        line = line.rstrip()
        if line.startswith('- ') and not line.startswith('  - '):
            bullets.append(f"  . [{head.split()[0]}] {line[2:]}")
            if len(bullets) >= limit:
                break
    if len(bullets) >= limit:
        break

if bullets:
    print(f"[claude-code-version] What's new since {current} (top {len(bullets)} bullets):")
    print('\n'.join(bullets))
PY
    )
  fi
  rm -f "$changelog_tmp"
fi

if [[ -n "$diff_block" ]]; then
  msg="${headline}
${diff_block}"
else
  msg="$headline"
fi

# Cache for 24h
printf '%s\n' "$msg" > "$CACHE_FILE"
echo "$msg" >&2
exit 0
