import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit
from unittest import mock

from trai import config, history, plot, provider

FAKES = Path(__file__).parent / "providers"


class PlotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        env = {
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
        }
        patches = [mock.patch.dict(os.environ, env), mock.patch.object(provider, "BUILTIN_DIR", FAKES)]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        config_home = Path(env["XDG_CONFIG_HOME"])
        config_home.mkdir(parents=True)
        (config_home / "user-dirs.dirs").write_text(
            f'XDG_DOCUMENTS_DIR="{self.tmp / "Documents"}"\n')
        self.now = datetime(2026, 11, 24, 12, tzinfo=timezone.utc).timestamp()

    def write_lines(self, *records):
        directory = history.history_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "2026-11-24.jsonl"
        path.write_text("\n".join(record if isinstance(record, str) else json.dumps(record)
                                  for record in records) + "\n")

    def record(self, provider_id, age, short=20, long=40, reset="2026-11-25T00:00:00+00:00"):
        return {
            "schema": 1,
            "provider": provider_id,
            "recorded_at": datetime.fromtimestamp(self.now - age, timezone.utc).isoformat(),
            "short": {"label": "5h", "used_percent": short, "resets_at": reset},
            "long": {"label": "7d", "used_percent": long, "resets_at": reset},
            "windows": [{"id": "model", "label": "Model 7d", "used_percent": 120,
                         "resets_at": reset}],
        }

    def page(self, uri):
        return Path(unquote(urlsplit(uri).path)).read_text()

    def test_generates_one_private_interactive_dashboard(self):
        self.write_lines(self.record("claude", 3600), self.record("claude", 1800, short=30))
        cfg = config.defaults()
        cfg["providers"] = ["codex"]
        cfg["colors"]["claude"] = "#123456"
        uri = plot.generate(cfg, "claude", "24h", self.now)
        parsed = urlsplit(uri)
        page = self.page(uri)

        self.assertEqual(parsed.query, "")
        self.assertEqual(Path(unquote(parsed.path)), self.tmp / "Documents" / "trai-usage-history.html")
        self.assertTrue(parsed.fragment.endswith("-24h"))
        self.assertEqual(page.count('class="range-view"'), 3)
        self.assertIn('data-range="24h"', page)
        self.assertIn('data-range="7d" hidden', page)
        self.assertLess(page.index(">codex<"), page.index(">claude<"))
        self.assertIn("#123456", page)
        self.assertIn('stroke-dasharray="10 6"', page)  # short is dashed
        self.assertIn("Model 7d", page)
        self.assertIn("120% used", page)
        self.assertIn("Generated", page)
        self.assertIn("Time range", page)

        path = Path(unquote(parsed.path))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_remaining_mode_and_malformed_records(self):
        bad_window = self.record("partial", 60)
        bad_window["windows"] = "bad"
        unsupported = self.record("old", 60)
        unsupported["schema"] = 2
        self.write_lines("{bad", bad_window, unsupported, self.record("claude", 60, short=25))
        cfg = config.defaults()
        cfg["providers"] = ["claude"]
        cfg["show"] = "remaining"
        page = self.page(plot.generate(cfg, now=self.now))
        self.assertIn('body data-initial-range="24h"', page)
        self.assertIn('data-range="7d" hidden', page)
        self.assertIn("75% remaining", page)
        self.assertIn("3 unreadable or unsupported history records were ignored", page)
        self.assertNotIn(">partial<", page)

    def test_reset_and_missing_intervals_split_segments(self):
        points = [
            plot.Point(self.now - 1000, 10, "2026-11-24T13:00:00+00:00", None),
            plot.Point(self.now - 900, 20, "2026-11-24T13:00:00.1+00:00", None),
            plot.Point(self.now - 800, 5, "2026-11-24T18:00:00+00:00", None),
            plot.Point(self.now - 100, 8, "2026-11-24T18:00:00+00:00", None),
        ]
        segments = plot._segments(points, self.now - 2000, self.now, 300)
        self.assertEqual([len(segment) for segment in segments], [2, 1, 1])

    def test_unused_rolling_window_remains_one_dashed_segment(self):
        points = [
            plot.Point(self.now - 600, 0, "2026-11-24T16:50:00+00:00", "5h"),
            plot.Point(self.now - 420, 0, "2026-11-24T16:53:00+00:00", "5h"),
            plot.Point(self.now - 240, 0, "2026-11-24T16:56:00+00:00", "5h"),
        ]
        segments = plot._segments(points, self.now - 1000, self.now, 360)
        path, singletons, _overflow = plot._path_for(
            segments, self.now - 1000, self.now, "used")

        self.assertEqual([len(segment) for segment in segments], [3])
        self.assertIn("L", path)
        self.assertEqual(singletons, [])

    def test_downsampling_preserves_extremes(self):
        points = [plot.Point(self.now + index, 50, None, None) for index in range(100)]
        points[25] = plot.Point(self.now + 25, 2, None, None)
        points[75] = plot.Point(self.now + 75, 98, None, None)
        sampled = plot._downsample(points, "used", limit=12)
        self.assertLessEqual(len(sampled), 14)
        self.assertIn(2, [point.used for point in sampled])
        self.assertIn(98, [point.used for point in sampled])

    def test_unknown_focus_is_rejected(self):
        cfg = config.defaults()
        cfg["providers"] = []
        with self.assertRaisesRegex(plot.PlotError, "unknown provider"):
            plot.generate(cfg, "nope", now=self.now)

    def test_plot_overwrites_one_fixed_document(self):
        cfg = config.defaults()
        cfg["providers"] = []
        first = Path(unquote(urlsplit(plot.generate(cfg, now=self.now)).path))
        legacy = first.parent / "trai" / "usage-history.html"
        legacy.parent.mkdir()
        legacy.touch()
        uris = [plot.generate(cfg, now=self.now + offset) for offset in range(1, 5)]
        self.assertTrue(all(urlsplit(uri).query == "" for uri in uris))
        self.assertTrue(all(Path(unquote(urlsplit(uri).path)) == first for uri in uris))
        self.assertEqual([path.name for path in first.parent.iterdir()], ["trai-usage-history.html"])
        self.assertFalse(legacy.exists())

    def test_missing_user_dirs_config_falls_back_to_documents(self):
        (Path(os.environ["XDG_CONFIG_HOME"]) / "user-dirs.dirs").unlink()
        with mock.patch.object(Path, "home", return_value=self.tmp / "home"):
            self.assertEqual(plot._documents_dir(), self.tmp / "home" / "Documents")


if __name__ == "__main__":
    unittest.main()
