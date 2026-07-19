"""Install/uninstall Cursor sessionStart hook for auto-sync."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import paths

LEGACY_TASK_NAME = "CursavesAutoSync"
HOOK_COMMAND_MARKER = "cursaves-watch"
HOOK_SCRIPT_STEM = "cursaves-watch"


def _hooks_dir() -> Path:
    hooks_dir = paths.get_cursor_dot_dir() / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    return hooks_dir


def _hooks_json_path() -> Path:
    return paths.get_cursor_dot_dir() / "hooks.json"


def _hook_script_name() -> str:
    if sys.platform == "win32":
        return f"{HOOK_SCRIPT_STEM}.ps1"
    return f"{HOOK_SCRIPT_STEM}.sh"


def _hook_command() -> str:
    return f"./hooks/{_hook_script_name()}"


def _windows_hook_script() -> str:
    return """# cursaves auto-sync hook — starts background watch when Cursor opens a session
$ErrorActionPreference = 'SilentlyContinue'
$mutex = New-Object System.Threading.Mutex($false, 'Global\\CursavesWatchDetach')
if (-not $mutex.WaitOne(0)) { exit 0 }
try {
    $pidFile = Join-Path $env:USERPROFILE '.cursaves\\watch.pid'
    if (Test-Path $pidFile) {
        $pid = Get-Content $pidFile -ErrorAction SilentlyContinue
        if ($pid -and (Get-Process -Id $pid -ErrorAction SilentlyContinue)) { exit 0 }
    }
    $cursaves = (Get-Command cursaves -ErrorAction SilentlyContinue).Source
    if (-not $cursaves) { exit 0 }
    Start-Process -FilePath $cursaves -ArgumentList 'watch','--all','--detach' -WindowStyle Hidden
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
exit 0
"""


def _unix_hook_script() -> str:
    return """#!/bin/sh
# cursaves auto-sync hook — starts background watch when Cursor opens a session
PID_FILE="${HOME}/.cursaves/watch.pid"
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE" 2>/dev/null)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        exit 0
    fi
fi
if command -v cursaves >/dev/null 2>&1; then
    cursaves watch --all --detach >/dev/null 2>&1 &
fi
exit 0
"""


def _write_hook_script() -> Path:
    script_path = _hooks_dir() / _hook_script_name()
    content = _windows_hook_script() if sys.platform == "win32" else _unix_hook_script()
    script_path.write_text(content, encoding="utf-8")
    if sys.platform != "win32":
        script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script_path


def _is_cursaves_hook(entry: dict[str, Any]) -> bool:
    command = entry.get("command", "")
    return HOOK_COMMAND_MARKER in command or HOOK_SCRIPT_STEM in command


def merge_hooks_json(existing: dict[str, Any]) -> dict[str, Any]:
    """Merge cursaves sessionStart hook into hooks.json, preserving other hooks."""
    merged: dict[str, Any] = {
        "version": existing.get("version", 1),
        "hooks": dict(existing.get("hooks", {})),
    }
    session_hooks = list(merged["hooks"].get("sessionStart", []))
    session_hooks = [h for h in session_hooks if not _is_cursaves_hook(h)]
    session_hooks.append({
        "command": _hook_command(),
        "timeout": 10,
    })
    merged["hooks"]["sessionStart"] = session_hooks
    return merged


def remove_cursaves_hook(existing: dict[str, Any]) -> dict[str, Any]:
    """Remove cursaves hook entries from hooks.json."""
    merged: dict[str, Any] = {
        "version": existing.get("version", 1),
        "hooks": dict(existing.get("hooks", {})),
    }
    if "sessionStart" in merged["hooks"]:
        merged["hooks"]["sessionStart"] = [
            h for h in merged["hooks"]["sessionStart"]
            if not _is_cursaves_hook(h)
        ]
        if not merged["hooks"]["sessionStart"]:
            del merged["hooks"]["sessionStart"]
    if not merged["hooks"]:
        merged["hooks"] = {}
    return merged


def _load_hooks_json() -> dict[str, Any]:
    hooks_path = _hooks_json_path()
    if not hooks_path.exists():
        return {"version": 1, "hooks": {}}
    try:
        return json.loads(hooks_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "hooks": {}}


def _save_hooks_json(data: dict[str, Any]) -> None:
    hooks_path = _hooks_json_path()
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def remove_legacy_scheduled_task() -> bool:
    """Remove the old Windows logon scheduled task if present."""
    if sys.platform != "win32":
        return False
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", LEGACY_TASK_NAME, "/F"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def install_watch_hook(interval: int = 120) -> None:
    """Install sessionStart hook and remove legacy logon scheduled task."""
    script_path = _write_hook_script()
    merged = merge_hooks_json(_load_hooks_json())
    _save_hooks_json(merged)

    removed_task = remove_legacy_scheduled_task()

    print("cursaves watch hook installed")
    print(f"  Hook script: {script_path}")
    print(f"  Hooks config: {_hooks_json_path()}")
    print("  Trigger: Cursor sessionStart")
    print(f"  Sync interval: {interval}s (while Cursor is open)")
    if removed_task:
        print(f"  Removed legacy scheduled task: {LEGACY_TASK_NAME}")
    print()
    print("Restart Cursor fully to load the new hook.")
    print("Auto-sync starts when you open a Cursor session; stops when Cursor closes.")
    print("To remove: cursaves watch --uninstall-hook")


def uninstall_watch_hook() -> None:
    """Remove sessionStart hook, script, and legacy scheduled task."""
    script_path = _hooks_dir() / _hook_script_name()
    if script_path.exists():
        script_path.unlink()

    hooks_path = _hooks_json_path()
    if hooks_path.exists():
        merged = remove_cursaves_hook(_load_hooks_json())
        if merged.get("hooks"):
            _save_hooks_json(merged)
        else:
            hooks_path.unlink(missing_ok=True)

    removed_task = remove_legacy_scheduled_task()

    print("cursaves watch hook removed")
    if removed_task:
        print(f"  Removed legacy scheduled task: {LEGACY_TASK_NAME}")
    print("Restart Cursor to apply changes.")
