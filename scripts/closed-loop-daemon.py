#!/usr/bin/env python3
"""closed-loop-daemon.py: watch Meta/Learnings/ for new files, run promote on add.

A long-running daemon that mirrors what the hourly cron entry does, but with
seconds-to-minutes latency instead of up to 1 hour. Useful for an operator
who tunes the loop in real time and wants the resolver to see new rules
within seconds of a learning capture.

Cross-platform implementation:
- Tries `watchdog` if installed (cross-platform inotify/FSEvents-backed).
- Falls back to a polling stat-loop if not. Polling has higher latency but
  zero deps. Default poll interval: 30 seconds.

Crash protection:
- Single-instance pidfile at <vault>/⚙️ Meta/.closed-loop-daemon.pid.
- Refuses to start if pidfile exists with a live PID.
- Removes pidfile on clean shutdown via SIGTERM/SIGINT.

Calls into scripts/promote-episodic-to-procedural.py with --quiet so the
daemon's stderr stays clean except for actual errors. Each promote call
is isolated; one bad file does not crash the daemon.

Stop signals:
- SIGTERM, SIGINT (Ctrl+C): clean shutdown, pidfile removed.
- SIGHUP: re-read config from CLI args (currently a no-op stub for future
  hot-reload support).

Usage:
    python3 closed-loop-daemon.py --vault-root <vault> [--poll-seconds 30] [--meta-dir-name "⚙️ Meta"]

Install via launchd: see templates/launchd/com.abs.closed-loop-daemon.plist.template
and scripts/install-closed-loop-daemon.sh.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMOTE_SCRIPT = REPO_ROOT / "scripts" / "promote-episodic-to-procedural.py"


class DaemonAlreadyRunning(Exception):
    pass


def acquire_pidfile(pidfile: Path) -> None:
    if pidfile.exists():
        try:
            existing = int(pidfile.read_text().strip())
        except (ValueError, OSError):
            existing = 0
        if existing and pid_alive(existing):
            raise DaemonAlreadyRunning(
                f"daemon already running with PID {existing} (pidfile {pidfile})"
            )
        pidfile.unlink(missing_ok=True)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()))


def release_pidfile(pidfile: Path) -> None:
    try:
        pidfile.unlink(missing_ok=True)
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def list_learning_files(learnings_dir: Path) -> set[str]:
    if not learnings_dir.exists():
        return set()
    return {p.name for p in learnings_dir.glob("*.md")}


def run_promote(vault_root: Path) -> tuple[int, str]:
    """Run the promote script once. Returns (exit_code, brief_msg)."""
    if not PROMOTE_SCRIPT.exists():
        return 2, f"promote script missing at {PROMOTE_SCRIPT}"
    cmd = [
        sys.executable,
        str(PROMOTE_SCRIPT),
        "--vault-root", str(vault_root),
        "--quiet",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return 124, "promote timed out (>5 min)"
    except Exception as exc:
        return 1, f"promote exec error: {exc}"
    return result.returncode, (result.stdout.strip().splitlines() or [""])[-1]


def poll_loop(
    learnings_dir: Path,
    vault_root: Path,
    poll_seconds: int,
    stop_flag: list[bool],
) -> None:
    """Stat-poll the learnings directory. Calls promote on any new file."""
    seen = list_learning_files(learnings_dir)
    while not stop_flag[0]:
        time.sleep(poll_seconds)
        if stop_flag[0]:
            break
        current = list_learning_files(learnings_dir)
        new_files = current - seen
        if new_files:
            print(f"[closed-loop-daemon] {len(new_files)} new learning(s); running promote", flush=True)
            code, msg = run_promote(vault_root)
            if code != 0:
                print(f"[closed-loop-daemon] promote exit {code}: {msg}", file=sys.stderr, flush=True)
            seen = current


def watchdog_loop(
    learnings_dir: Path,
    vault_root: Path,
    stop_flag: list[bool],
) -> None:
    """Use watchdog if installed. Falls back to polling on import error."""
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        print(
            "[closed-loop-daemon] watchdog not installed; falling back to polling",
            file=sys.stderr, flush=True,
        )
        poll_loop(learnings_dir, vault_root, 30, stop_flag)
        return

    class Handler(FileSystemEventHandler):
        def on_created(self, event):
            if event.is_directory:
                return
            if not str(event.src_path).endswith(".md"):
                return
            print(f"[closed-loop-daemon] learning created: {event.src_path}; running promote", flush=True)
            code, msg = run_promote(vault_root)
            if code != 0:
                print(f"[closed-loop-daemon] promote exit {code}: {msg}", file=sys.stderr, flush=True)

    observer = Observer()
    handler = Handler()
    learnings_dir.mkdir(parents=True, exist_ok=True)
    observer.schedule(handler, str(learnings_dir), recursive=False)
    observer.start()
    try:
        while not stop_flag[0]:
            time.sleep(1)
    finally:
        observer.stop()
        observer.join()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument(
        "--meta-dir-name",
        default="⚙️ Meta",
        help="Folder name for the Meta directory (default: '⚙️ Meta')",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=30,
        help="Poll interval when watchdog is unavailable (default 30s)",
    )
    parser.add_argument(
        "--use-polling",
        action="store_true",
        help="Force polling even if watchdog is available",
    )
    args = parser.parse_args()

    vault = args.vault_root.resolve()
    if not vault.exists():
        print(f"vault-root does not exist: {vault}", file=sys.stderr)
        return 2

    meta_dir = vault / args.meta_dir_name
    learnings_dir = meta_dir / "Learnings"
    pidfile = meta_dir / ".closed-loop-daemon.pid"

    try:
        acquire_pidfile(pidfile)
    except DaemonAlreadyRunning as exc:
        print(str(exc), file=sys.stderr)
        return 3

    stop_flag = [False]

    def handle_signal(signum, _frame):
        print(f"[closed-loop-daemon] caught signal {signum}, shutting down", flush=True)
        stop_flag[0] = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    try:
        signal.signal(signal.SIGHUP, handle_signal)
    except (AttributeError, ValueError):
        pass

    print(
        f"[closed-loop-daemon] watching {learnings_dir} (vault={vault}, poll={args.poll_seconds}s)",
        flush=True,
    )

    try:
        if args.use_polling:
            poll_loop(learnings_dir, vault, args.poll_seconds, stop_flag)
        else:
            watchdog_loop(learnings_dir, vault, stop_flag)
    finally:
        release_pidfile(pidfile)
        print("[closed-loop-daemon] stopped", flush=True)
    return 0


if __name__ == "__main__":
    # Windows cp1252-console safety (#313): force UTF-8 so a non-ASCII print can't crash.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # Python 3.7+
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
