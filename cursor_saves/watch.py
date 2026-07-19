"""Background daemon for automatic checkpoint + git sync."""

import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import paths

POLL_INTERVAL_DEFAULT = 15
CREATE_NO_WINDOW = 0x08000000


def get_watch_pid_path() -> Path:
    """Return path to the watch daemon PID file."""
    return paths.get_sync_dir() / "watch.pid"


def get_watch_log_path() -> Path:
    """Return path to the detached watch daemon log file."""
    return paths.get_sync_dir() / "watch.log"


def is_pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID is running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid_file(pid_path: Path) -> int | None:
    """Return the PID stored in *pid_path*, or None if missing/invalid."""
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _remove_stale_pid_file(pid_path: Path) -> bool:
    """Remove a stale PID file. Returns True if removed or absent."""
    pid = _read_pid_file(pid_path)
    if pid is not None and is_pid_alive(pid):
        return False
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def is_watch_running() -> bool:
    """Return True if another watch --all daemon is already running."""
    pid_path = get_watch_pid_path()
    pid = _read_pid_file(pid_path)
    if pid is None:
        return False
    if is_pid_alive(pid):
        return True
    _remove_stale_pid_file(pid_path)
    return False


def write_watch_pid() -> None:
    """Record the current process PID for singleton enforcement."""
    get_watch_pid_path().write_text(str(os.getpid()), encoding="utf-8")


def remove_watch_pid() -> None:
    """Remove the watch PID file on shutdown if this process owns it."""
    pid_path = get_watch_pid_path()
    pid = _read_pid_file(pid_path)
    if pid is None:
        return
    if pid != os.getpid():
        return
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass


def acquire_watch_pid() -> bool:
    """Claim the watch singleton. Returns False if another instance is running."""
    pid_path = get_watch_pid_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    for _ in range(5):
        pid = _read_pid_file(pid_path)
        if pid is not None and is_pid_alive(pid):
            return False
        if pid is not None and not _remove_stale_pid_file(pid_path):
            return False

        try:
            fd = os.open(str(pid_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue

        try:
            os.write(fd, f"{os.getpid()}\n".encode())
        finally:
            os.close(fd)
        return True

    return False


def _sleep_interruptible(
    seconds: int,
    should_continue: Callable[[], bool],
) -> None:
    """Sleep in 1-second steps, stopping early when should_continue() is False."""
    for _ in range(seconds):
        if not should_continue():
            break
        time.sleep(1)


def _run_full_sync(git_sync: bool = True, verbose: bool = False) -> tuple[int, int]:
    """Run a full bidirectional sync cycle for all workspaces.

    Returns (imported_count, pushed_count).
    """
    from .backends import get_backend
    from .cli import (
        _apply_retention_prune,
        _profile_apply_after_pull,
        _profile_export_push,
        _profile_sync_enabled,
        _pull_behind,
        _push_ahead,
    )
    from . import profile

    sync_dir = paths.get_sync_dir()
    backend = get_backend()
    snapshots_dir = paths.get_snapshots_dir()

    if _profile_sync_enabled():
        if not _profile_export_push(backend, snapshots_dir):
            return 0, 0

    if git_sync and backend.has_remote():
        if verbose:
            print(f"[{_now()}] Pulling remote...", end="", flush=True)
        ok = backend.pull(snapshots_dir)
        if verbose:
            print(" done" if ok else " failed", flush=True)

    if profile.is_profile_enabled():
        applied = _profile_apply_after_pull()
        if applied > 0:
            print(f"[{_now()}] Applied {applied} profile item(s) from remote")

    from .chat_lifecycle import is_chat_sync_enabled

    imported = 0
    pushed = 0
    if is_chat_sync_enabled():
        imported = _pull_behind(sync_dir)
        if imported > 0:
            print(f"[{_now()}] Imported {imported} conversation(s) from remote")

        pruned = _apply_retention_prune(verbose=verbose)
        if pruned > 0:
            print(f"[{_now()}] Retention: pruned {pruned} expired snapshot(s)")

        if git_sync:
            pushed = _push_ahead(sync_dir, auto=True, backend=backend)
            if pushed > 0:
                print(f"[{_now()}] Pushed {pushed} conversation(s) to remote")
        elif verbose:
            print(f"[{_now()}] Git sync disabled, skipping push")
    elif verbose:
        print(f"[{_now()}] Chat sync disabled, skipping chat pull/push")

    return imported, pushed


def _cursor_is_running() -> bool:
    from .importer import is_cursor_running

    return is_cursor_running()


def _run_sync_cycle(
    cycle: int,
    git_sync: bool,
    verbose: bool,
    label: str = "Sync cycle",
) -> None:
    if verbose:
        print(f"[{_now()}] {label} {cycle}...")
    imported, pushed = _run_full_sync(git_sync=git_sync, verbose=verbose)
    if verbose and imported == 0 and pushed == 0:
        print(f"[{_now()}] Already in sync")


def detach_watch_all(
    interval: int = 120,
    git_sync: bool = True,
    verbose: bool = False,
    poll_interval: int = POLL_INTERVAL_DEFAULT,
) -> bool:
    """Start watch --all in a background process without a console window.

    Returns True if a new daemon was started, False if one is already running.
    """
    if is_watch_running():
        return False

    cursaves_exe = shutil.which("cursaves")
    if not cursaves_exe:
        cursaves_exe = sys.executable
        cmd = [
            cursaves_exe, "-m", "cursor_saves.cli",
            "watch", "--all",
            "--interval", str(interval),
        ]
    else:
        cmd = [cursaves_exe, "watch", "--all", "--interval", str(interval)]

    if not git_sync:
        cmd.append("--no-git")
    if verbose:
        cmd.append("--verbose")
    if poll_interval != POLL_INTERVAL_DEFAULT:
        cmd.extend(["--poll-interval", str(poll_interval)])

    log_path = get_watch_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write(f"\n--- detach started {datetime.now().isoformat()} ---\n")
        log_file.flush()
        popen_kwargs = {
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = CREATE_NO_WINDOW
        else:
            popen_kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **popen_kwargs)

    return True


def watch_all_loop(
    interval: int = 120,
    git_sync: bool = True,
    verbose: bool = False,
    poll_interval: int = POLL_INTERVAL_DEFAULT,
):
    """Watch all Cursor workspaces and sync while Cursor is open.

    Sync runs only while Cursor is open. On Cursor close, runs a final sync
    and exits.
    """
    if not paths.is_sync_repo_initialized():
        print(
            "Error: Sync repo not initialized.\n"
            "Run 'cursaves init --remote <url>' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not acquire_watch_pid():
        if verbose:
            print("watch --all already running, exiting.")
        return

    print("cursaves watch --all started")
    print("  Scope: all Cursor workspaces")
    print(f"  Sync interval: {interval}s")
    print(f"  Poll interval: {poll_interval}s (only sync while Cursor is open)")
    print(f"  Git sync: {'enabled' if git_sync else 'disabled'}")
    print(f"  Machine: {paths.get_machine_id()}")
    print()
    print("Sync runs while Cursor is open; stops when Cursor closes.")
    print("Restart Cursor after imports to see chats from other machines.")
    print("Profile changes (settings, skills, hooks) may also require a restart.")
    print()

    running = True
    cycle = 0
    cursor_was_running = _cursor_is_running()

    def handle_signal(signum, frame):
        nonlocal running
        print(f"\nShutting down (received signal {signum})...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while running:
            cursor_running = _cursor_is_running()

            if not cursor_running:
                if cursor_was_running:
                    cycle += 1
                    try:
                        if verbose:
                            print(f"[{_now()}] Cursor closed, final sync...")
                        _run_sync_cycle(
                            cycle, git_sync, verbose, label="Final sync",
                        )
                    except Exception as e:
                        print(f"[{_now()}] Sync error: {e}", file=sys.stderr)
                    break
                if verbose:
                    print(f"[{_now()}] Cursor not running, waiting...")
                _sleep_interruptible(poll_interval, lambda: running)
                continue

            cycle += 1
            try:
                _run_sync_cycle(cycle, git_sync, verbose)
            except Exception as e:
                print(f"[{_now()}] Sync error: {e}", file=sys.stderr)

            if not running:
                break

            cursor_was_running = True
            for _ in range(interval):
                if not running:
                    break
                if not _cursor_is_running():
                    cursor_was_running = True
                    break
                time.sleep(1)
    finally:
        remove_watch_pid()

    print(f"\nwatch stopped after {cycle} cycle(s).")


def _now() -> str:
    """Return current time as a short string."""
    return datetime.now().strftime("%H:%M:%S")
