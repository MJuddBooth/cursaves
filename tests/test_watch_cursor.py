"""Tests for cursor-gated watch and hook installation."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from cursor_saves import paths
from cursor_saves.hook_install import (
    HOOK_SCRIPT_STEM,
    merge_hooks_json,
    remove_cursaves_hook,
)
from cursor_saves.watch import (
    acquire_watch_pid,
    get_watch_pid_path,
    is_watch_running,
    remove_watch_pid,
    watch_all_loop,
)


class TempWatchEnvMixin:
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="cursaves-watch-test-")
        self.sync_dir = Path(self._tmp) / "cursaves"
        self.sync_dir.mkdir()
        (self.sync_dir / "snapshots").mkdir()
        (self.sync_dir / ".git").mkdir()

        self.patches = [
            mock.patch.object(paths, "get_sync_dir", return_value=self.sync_dir),
            mock.patch.object(paths, "is_sync_repo_initialized", return_value=True),
            mock.patch.object(paths, "get_machine_id", return_value="test-machine"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        remove_watch_pid()
        for patch in reversed(self.patches):
            patch.stop()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestWatchPid(TempWatchEnvMixin, unittest.TestCase):
    def test_acquire_watch_pid_singleton(self):
        self.assertTrue(acquire_watch_pid())
        self.assertTrue(is_watch_running())
        self.assertFalse(acquire_watch_pid())
        remove_watch_pid()
        self.assertFalse(is_watch_running())

    def test_stale_pid_file_is_cleaned(self):
        pid_path = get_watch_pid_path()
        pid_path.write_text("999999999", encoding="utf-8")
        self.assertFalse(is_watch_running())
        self.assertFalse(pid_path.exists())

    def test_remove_watch_pid_only_own(self):
        pid_path = get_watch_pid_path()
        pid_path.write_text("999999999", encoding="utf-8")
        remove_watch_pid()
        self.assertTrue(pid_path.exists())

    def test_concurrent_acquire_only_one_wins(self):
        results: list[bool] = []
        barrier = threading.Barrier(8)

        def try_acquire():
            barrier.wait()
            results.append(acquire_watch_pid())

        threads = [threading.Thread(target=try_acquire) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(results), 1)
        remove_watch_pid()


class TestWatchCursorGate(TempWatchEnvMixin, unittest.TestCase):
    def test_sync_only_while_cursor_open_then_final_sync(self):
        sync_calls = []
        cursor_checks = iter([True, True, False])

        def fake_cursor():
            return next(cursor_checks, False)

        def fake_sync(*args, **kwargs):
            sync_calls.append("sync")
            return 0, 0

        with mock.patch("cursor_saves.watch._cursor_is_running", side_effect=fake_cursor), \
             mock.patch("cursor_saves.watch._run_full_sync", side_effect=fake_sync), \
             mock.patch("cursor_saves.watch._sleep_interruptible"), \
             mock.patch("cursor_saves.watch.time.sleep"):
            watch_all_loop(
                interval=1,
                poll_interval=1,
                verbose=False,
            )

        self.assertEqual(sync_calls, ["sync", "sync"])

    def test_waits_when_cursor_closed_at_start(self):
        sync_calls = []
        cursor_checks = iter([False, False, True, False])

        def fake_cursor():
            return next(cursor_checks, False)

        def fake_sync(*args, **kwargs):
            sync_calls.append("sync")
            return 0, 0

        with mock.patch("cursor_saves.watch._cursor_is_running", side_effect=fake_cursor), \
             mock.patch("cursor_saves.watch._run_full_sync", side_effect=fake_sync), \
             mock.patch("cursor_saves.watch._sleep_interruptible"), \
             mock.patch("cursor_saves.watch.time.sleep"):
            watch_all_loop(
                interval=1,
                poll_interval=1,
                verbose=False,
            )

        self.assertEqual(sync_calls, ["sync", "sync"])

    def test_second_instance_exits_when_already_running(self):
        acquire_watch_pid()
        with mock.patch("cursor_saves.watch._run_full_sync") as fake_sync:
            watch_all_loop(interval=1, verbose=False)
            fake_sync.assert_not_called()
        remove_watch_pid()


class TestHookInstallMerge(unittest.TestCase):
    def test_merge_preserves_existing_hooks(self):
        existing = {
            "version": 1,
            "hooks": {
                "sessionStart": [
                    {"command": "./hooks/my-custom.sh", "timeout": 5},
                ],
                "afterFileEdit": [
                    {"command": "./hooks/format.sh"},
                ],
            },
        }
        merged = merge_hooks_json(existing)
        commands = [h["command"] for h in merged["hooks"]["sessionStart"]]
        self.assertIn("./hooks/my-custom.sh", commands)
        self.assertTrue(any(HOOK_SCRIPT_STEM in c for c in commands))
        self.assertEqual(len(merged["hooks"]["sessionStart"]), 2)
        self.assertIn("afterFileEdit", merged["hooks"])

    def test_remove_cursaves_hook_keeps_others(self):
        existing = {
            "version": 1,
            "hooks": {
                "sessionStart": [
                    {"command": "./hooks/cursaves-watch.ps1", "timeout": 10},
                    {"command": "./hooks/my-custom.sh", "timeout": 5},
                ],
            },
        }
        merged = remove_cursaves_hook(existing)
        commands = [h["command"] for h in merged["hooks"]["sessionStart"]]
        self.assertEqual(commands, ["./hooks/my-custom.sh"])

    def test_merge_replaces_old_cursaves_entry(self):
        existing = {
            "version": 1,
            "hooks": {
                "sessionStart": [
                    {"command": "./hooks/cursaves-watch.sh", "timeout": 10},
                ],
            },
        }
        merged = merge_hooks_json(existing)
        session_hooks = merged["hooks"]["sessionStart"]
        self.assertEqual(len(session_hooks), 1)
        self.assertIn(HOOK_SCRIPT_STEM, session_hooks[0]["command"])


if __name__ == "__main__":
    unittest.main()
