import contextlib
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from trai import history


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.tmp / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.warning_dir = self.tmp / "cache" / "trai"
        self.data = {
            "short": {"label": "5h", "used_percent": 42},
            "long": None,
            "windows": [{"id": "model", "used_percent": 7}],
            "extra": {"secret": "not retained"},
        }

    @staticmethod
    def at(day):
        return datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()

    def test_record_shape_and_permissions(self):
        at = self.at("2026-11-24T14:03:12")
        history.record("claude", self.data, at, 30, self.warning_dir)
        path = history.history_dir() / "2026-11-24.jsonl"
        record = json.loads(path.read_text())
        self.assertEqual(record, {
            "schema": 1,
            "provider": "claude",
            "recorded_at": "2026-11-24T14:03:12+00:00",
            "short": self.data["short"],
            "long": None,
            "windows": self.data["windows"],
        })
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_concurrent_records_are_complete_lines(self):
        at = self.at("2026-11-24T14:03:12")
        threads = [threading.Thread(target=history.record,
                                    args=(f"provider-{i}", self.data, at, 30, self.warning_dir))
                   for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        lines = (history.history_dir() / "2026-11-24.jsonl").read_text().splitlines()
        self.assertEqual({json.loads(line)["provider"] for line in lines},
                         {f"provider-{i}" for i in range(20)})

    def test_prunes_only_expired_daily_files(self):
        directory = history.history_dir()
        directory.mkdir(parents=True)
        for name in ("2026-11-20.jsonl", "2026-11-22.jsonl", "2026-11-30.jsonl", "notes.jsonl"):
            (directory / name).touch()
        (directory / "2026-11-19.jsonl").mkdir()
        history.record("claude", self.data, self.at("2026-11-24T12:00:00"), 3, self.warning_dir)
        self.assertFalse((directory / "2026-11-20.jsonl").exists())
        for name in ("2026-11-22.jsonl", "2026-11-24.jsonl", "2026-11-30.jsonl",
                     "notes.jsonl", "2026-11-19.jsonl"):
            self.assertTrue((directory / name).exists())

    def test_warning_is_throttled_for_24_hours(self):
        error = PermissionError(13, "Permission denied")
        stderr = io.StringIO()
        with mock.patch.object(history, "_append", side_effect=error), contextlib.redirect_stderr(stderr):
            history.record("a", self.data, 100_000, 30, self.warning_dir)
            history.record("b", self.data, 100_100, 30, self.warning_dir)
            history.record("c", self.data, 100_000 + history.WARNING_INTERVAL, 30, self.warning_dir)
        self.assertEqual(len(stderr.getvalue().splitlines()), 2)

    def test_warning_repeats_when_throttle_state_cannot_be_written(self):
        blocked = self.tmp / "not-a-directory"
        blocked.touch()
        stderr = io.StringIO()
        error = PermissionError(13, "Permission denied")
        with mock.patch.object(history, "_append", side_effect=error), contextlib.redirect_stderr(stderr):
            history.record("a", self.data, 100_000, 30, blocked / "trai")
            history.record("b", self.data, 100_100, 30, blocked / "trai")
        self.assertEqual(len(stderr.getvalue().splitlines()), 2)

    def test_prune_failure_keeps_new_record(self):
        at = self.at("2026-11-24T14:03:12")
        stderr = io.StringIO()
        with mock.patch.object(history, "_prune", side_effect=PermissionError(13, "Permission denied")), \
                contextlib.redirect_stderr(stderr):
            history.record("claude", self.data, at, 30, self.warning_dir)
        path = history.history_dir() / "2026-11-24.jsonl"
        self.assertEqual(json.loads(path.read_text())["provider"], "claude")
        self.assertIn("cannot prune history", stderr.getvalue())

    def test_disabled_history_does_nothing(self):
        history.record("claude", self.data, self.at("2026-11-24T12:00:00"), 0, self.warning_dir)
        self.assertFalse(history.history_dir().exists())


if __name__ == "__main__":
    unittest.main()
