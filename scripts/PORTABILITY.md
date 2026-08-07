# Portable idioms (macOS + Linux + Windows)

ai-brain-starter ships ~80 `*.sh` that users run on **macOS** (bash 3.2, BSD
coreutils) and **Linux** (bash 4+/5, GNU coreutils). CI runs on **ubuntu**. A
script that is green on a Mac can still fail on a user's Linux box, or on CI.
**Windows** users run bootstrap.ps1 + the Python hooks; section 5 below is the
Windows contract — read it before touching hooks.json, the installer, or any
hook that builds paths or spawns subprocesses.

`scripts/shellcheck.sh` (run in CI by the `shellcheck` job in
`.github/workflows/lint.yml`, and locally by `scripts/ci.sh`) catches most of the
quoting and correctness class. It does **not** catch the GNU-vs-BSD flag
differences below. Those are on you. Each has a reference implementation in this
repo.

## 1. File mtime: `stat`

GNU and BSD `stat` take different flags, and the failure mode is SILENT:

- GNU/Linux: `stat -c %Y FILE`  gives the epoch mtime
- BSD/macOS: `stat -f %m FILE`  gives the epoch mtime

The trap: GNU `stat -f` means `--file-system`. So `stat -f %m FILE` on Linux
**exits 0** and prints non-numeric filesystem text instead of failing. Relying on
`||` (the exit code) hands you garbage, not a fallback. Validate that the result
is numeric after each attempt.

Reference: `scripts/_session_close_guard.sh`, function `_close_lock_mtime`:

```bash
m=$(stat -c %Y "$1" 2>/dev/null)                                   # GNU/Linux
case "$m" in ''|*[!0-9]*) m=$(stat -f %m "$1" 2>/dev/null) ;; esac # BSD/macOS
case "$m" in ''|*[!0-9]*) m="" ;; esac   # neither gave a plain integer -> empty
```

## 2. Date arithmetic: `date`

- BSD/macOS: `date -v-7d +%Y-%m-%d`
- GNU/Linux: `date -d '7 days ago' +%Y-%m-%d`

Detect support, do not guess the platform:

Reference: `scripts/session-end-hook.sh`:

```bash
if date -v-7d +%Y-%m-%d >/dev/null 2>&1; then
  CUTOFF=$(date -v-7d +%Y-%m-%d)             # BSD/macOS
else
  CUTOFF=$(date -d '7 days ago' +%Y-%m-%d)   # GNU/Linux
fi
```

## 3. In-place edit: `sed -i`

`sed -i` is NOT portable. GNU is `sed -i 's/x/y/' f`; BSD requires an explicit
backup-suffix argument, `sed -i '' 's/x/y/' f`. Mixing the two either errors or
silently writes a stray backup file.

Do not use `sed -i` at all. Write to a temp file and `mv`:

```bash
sed 's/PLACEHOLDER/value/' template > "$tmp" && mv "$tmp" dest
```

Reference: `scripts/install-closed-loop-daemon.sh` and
`scripts/install-vault-daily-maintenance.sh` both substitute placeholders via a
temp file precisely because `sed -i` differs across macOS and GNU.

## 4. bash 3.2 (macOS ships it)

macOS ships **bash 3.2** (2007) as `/bin/bash`, and many users invoke hooks and
scripts under it. Avoid bash-4-only features, or guard them:

- No `mapfile` / `readarray -d` (the `-d` delimiter flag is bash 4.4+). Build
  arrays with a NUL-delimited read loop instead:
  `files=(); while IFS= read -r -d '' f; do files+=("$f"); done < <(... -print0)`.
  Reference: `scripts/shellcheck.sh` and `scripts/ci.sh` both build their file
  lists this way.
- `"${arr[@]}"` on an **empty** array under `set -u` errors on bash 3.2/4.3.
  Guard with `if [ "${#arr[@]}" -eq 0 ]; then ...; fi` before expanding.
- No associative arrays (`declare -A`).
- No `${var^^}` / `${var,,}` case conversion (bash 4+). Use `tr`.

## 5. Windows (native, not WSL)

Claude Code on native Windows executes hook commands under PowerShell 5.1,
PowerShell 7, cmd.exe, or Git Bash depending on version + configuration. NO
POSIX one-liner survives all four:

- `||` / `&&` — parse error in PowerShell 5.1.
- `[ -f X ] && ...` — POSIX test; fails everywhere on Windows.
- `2>/dev/null` — `/dev/null` does not exist; cmd/PowerShell try to open it as
  a file path.
- `python3` — not a real command on stock Windows; the PATH entry may be the
  Microsoft Store alias STUB that opens the Store instead of running.
- A QUOTED first token (`"C:\...\python.exe" args`) — string literal, not an
  invocation, in PowerShell.

The one command shape all four shells parse identically: **a bare PATH command
followed by quoted arguments.** Rules that follow from this:

1. **hooks.json stays POSIX** — the installer (`install-hooks-user-level.py`)
   rewrites every command at install time on Windows into
   `py -3 "<abs>/scripts/hook_runner.py" --fallback <silent|allow> "<abs>/<hook>.py"`.
   `hook_runner.py` reproduces the shell forms' failure-masking (missing script
   -> fallback JSON; exit 2 blocks propagate; other failures -> fallback JSON).
   New hooks need NO Windows-specific wiring as long as they are plain
   `python3 <script> 2>/dev/null || echo '<json>'` template commands.
2. **bash-only hooks are omitted on Windows** by the installer (reported in its
   summary). A hook that must reach Windows needs a Python implementation.
3. **In hook Python code**: never hardcode `/tmp` (use `tempfile.gettempdir()`;
   exception: a path a POSIX-only bash producer writes literally), never spawn
   `python3`/`/usr/bin/python3` (use `sys.executable`), never call `ps`, `kill`,
   `launchctl`, `pbcopy`, `osascript` without an `os.name != "posix"` early
   silent exit, and normalize `str(path).replace("\\", "/")` before matching
   markers like `/.claude/worktrees/`.
4. **User-facing fix commands** must be per-platform: `py -3 ...` /
   `powershell -ExecutionPolicy Bypass -File "<abs>.ps1"` on Windows (absolute
   paths — `~`, `$HOME`, `%USERPROFILE%` each expand in only SOME shells),
   `python3` / `bash ...` elsewhere. References: `surface-deployed-hooks-behind.py`
   (`FIX_CMD`), `surface-backup-status.py` (`_BACKUP_PREFIX`).
5. **.ps1 counterparts** exist for the user-run machinery: bootstrap,
   vault-backup, relocate-vault, relocate-machinery-sidecar, drift-check,
   update-check, preflight, diagnose. The auto-update pipeline
   (`ai-brain-auto-update.py`, `sync-skills.py`, `install-hooks-user-level.py`,
   `hook_runner.py`) is pure Python — one implementation for all platforms;
   the same-named `.sh` files are thin delegators kept for old wirings.

## What shellcheck DOES catch (so you do not have to memorize it)

Unquoted expansions (SC2086), `cd` without `|| exit` (SC2164), useless `eval`
(SC2294), redirection ordering (SC2069), masked return values (SC2155), and more,
at error + warning severity. Run it before you push:

```bash
bash scripts/shellcheck.sh
# or the full local gate (py_compile + integration tests + shellcheck):
bash scripts/ci.sh
```

A genuine false-positive is silenced at the source line with an inline shellcheck
disable directive carrying a one-line reason, never by lowering the severity gate.
