import re
import tempfile
import unittest
from pathlib import Path

from trai import config
from trai.model import ValidationError, derive_label, level_of, parse_json, validate_output


class ModelTest(unittest.TestCase):
    def test_derive_label(self):
        self.assertEqual(derive_label(300), "5h")
        self.assertEqual(derive_label(10080), "7d")
        self.assertEqual(derive_label(90), "90m")
        self.assertIsNone(derive_label(None))

    def test_valid_output(self):
        kind, data = validate_output({"long": {"used_percent": 5.5, "window_minutes": 1440,
                                                "resets_at": "2026-10-02T01:00:00Z"}})
        self.assertEqual(kind, "data")
        self.assertIsNone(data["short"])
        self.assertEqual(data["long"]["label"], "1d")
        self.assertEqual(data["long"]["resets_at"], "2026-10-02T01:00:00+00:00")
        self.assertEqual(data["windows"], [])

    def test_invalid_output(self):
        for raw in (
            [],
            {},
            {"short": None},
            {"short": {"used_percent": "5"}},
            {"short": {"used_percent": -1}},
            {"short": {"used_percent": True}},
            {"short": {"used_percent": 5, "resets_at": "2026-10-02T01:00:00"}},
            {"short": {"used_percent": 5, "window_minutes": 0}},
            {"short": {"used_percent": 5}, "windows": [{"used_percent": 5}]},
            {"short": {"used_percent": 5}, "extra": []},
            {"short": {"used_percent": 5}, "windows": {}},
            {"error": {"kind": "auth"}},
        ):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                validate_output(raw)

    def test_non_finite_numbers_rejected(self):
        for text in ('{"short": {"used_percent": NaN}}', '{"short": {"used_percent": 1e999}}',
                     '{"short": {"used_percent": 5}, "extra": {"x": -Infinity}}'):
            with self.subTest(text=text), self.assertRaises(ValidationError):
                parse_json(text)

    def test_error_kind_normalised(self):
        self.assertEqual(validate_output({"error": {"kind": "weird", "message": "m"}}),
                         ("error", {"kind": "other", "message": "m"}))

    def test_levels(self):
        thresholds = {"warn": 75, "critical": 90}
        self.assertEqual([level_of(v, thresholds) for v in (74.9, 75, 90, 120)],
                         ["ok", "warn", "critical", "critical"])


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, text):
        self.path.write_text(text)
        return config.load(self.path)

    def test_missing_file_is_defaults(self):
        self.assertEqual(config.load(self.path), config.DEFAULTS)

    def test_template_matches_defaults(self):
        settings = re.compile(r"^# (\[[a-z.]+\]|[a-z_]+ = .*)")
        lines = [m.group(1) for m in map(settings.match, config.TEMPLATE.splitlines()) if m]
        cfg = self.load("\n".join(lines))
        expected = config.defaults()
        expected["providers"] = ["claude", "codex"]
        expected["path"] = ["~/.local/npm/bin"]
        expected["colors"] = {"codex": "#10a37f"}
        self.assertEqual(cfg, expected)

    def test_partial_override(self):
        cfg = self.load('show = "remaining"\n[display.ring]\nouter_diameter = 30\n[colors]\nclaude = "#123456"\n')
        self.assertEqual(cfg["show"], "remaining")
        self.assertEqual(cfg["display"]["ring"]["outer_diameter"], 30)
        self.assertEqual(cfg["display"]["ring"]["inner_diameter"], 15)
        self.assertEqual(cfg["colors"], {"claude": "#123456"})

    def test_invalid_keys_keep_the_valid_ones(self):
        with self.assertRaises(config.ConfigError) as caught:
            self.load('providers = ["a"]\npath = ["~/bin"]\nrefresh_second = 60\n'
                      '[display]\nshow_name = true\nouter_ring = "inner"\n[colors]\na = "#123456"\nb = "red"')
        cfg = caught.exception.partial
        self.assertIn("refresh_second", str(caught.exception))
        self.assertIn("outer_ring", str(caught.exception))
        self.assertEqual(cfg["providers"], ["a"])
        self.assertEqual(cfg["path"], ["~/bin"])
        self.assertTrue(cfg["display"]["show_name"])
        self.assertEqual(cfg["display"]["outer_ring"], "short")
        self.assertEqual(cfg["colors"], {"a": "#123456"})

    def test_invalid(self):
        for text in (
            "nonsense = 1",
            'show = "half"',
            "refresh_seconds = 1",
            'providers = "claude"',
            'path = "~/bin"',
            'path = [""]',
            'providers = ["a", "a"]',
            "thresholds = 5",
            "[thresholds]\nwarn = 95",
            "[display]\nshow_name = 1",
            '[display]\npanel_box = "top"',
            '[display]\nouter_ring = "inner"',
            "[display]\npanel_index = -1",
            '[display]\nlabel_color = "red"',
            "[display.ring]\nouter_width = 0",
            '[colors]\nclaude = "provider"',
            "not toml [",
        ):
            with self.subTest(text=text), self.assertRaises(config.ConfigError):
                self.load(text)


if __name__ == "__main__":
    unittest.main()
