import contextlib
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import fcntl
import time

from trai import cache, cli, provider

FAKES = Path(__file__).parent / "providers"


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        (self.tmp / "state").mkdir()
        env = {
            "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "TRAI_TEST_DIR": str(self.tmp / "state"),
        }
        patches = [mock.patch.dict(os.environ, env), mock.patch.object(provider, "BUILTIN_DIR", FAKES)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def config(self, text):
        path = self.tmp / "config" / "trai" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def json_of(self, *ids):
        code, out, _ = self.run_cli("status", "--json", *ids)
        self.assertEqual(code, 0)
        return {p["id"]: p for p in json.loads(out)["providers"]}

    def runs(self):
        path = self.tmp / "state" / "runs"
        return len(path.read_text().splitlines()) if path.exists() else 0

    # --- data and errors ---

    def test_status_json(self):
        p = self.json_of("ok")["ok"]
        self.assertEqual(p["level"], "warn")  # from the per-model window at 80
        self.assertEqual(p["short"]["level"], "ok")
        self.assertEqual(p["short"]["label"], "5h")
        self.assertEqual(p["windows"][0]["level"], "warn")
        self.assertEqual(p["extra"], {"plan": "test"})
        self.assertFalse(p["stale"])
        self.assertIsNone(p["error"])

    def test_provider_failures(self):
        reports = self.json_of("error-auth", "crash", "garbage", "hang")
        self.assertEqual(reports["error-auth"]["error"]["kind"], "auth")
        self.assertEqual(reports["crash"]["error"]["kind"], "crash")
        self.assertIn("boom", reports["crash"]["error"]["message"])
        self.assertEqual(reports["garbage"]["error"]["kind"], "crash")
        self.assertEqual(reports["hang"]["error"]["kind"], "timeout")
        for r in reports.values():
            self.assertEqual(r["level"], "unknown")

    def test_check_exit_codes(self):
        self.assertEqual(self.run_cli("check", "short-only")[0], 0)
        self.assertEqual(self.run_cli("check", "ok")[0], 1)
        self.assertEqual(self.run_cli("check", "ok", "--critical", "80")[0], 2)
        self.assertEqual(self.run_cli("check", "error-auth")[0], 3)
        self.assertEqual(self.run_cli("check", "error-auth", "ok", "--critical", "80")[0], 2)
        self.assertEqual(self.run_cli("check", "error-auth", "ok")[0], 3)
        self.assertEqual(self.run_cli("check", "ok", "--warn", "95")[0], 4)
        self.assertEqual(self.run_cli("check", "nope")[0], 4)
        self.assertEqual(self.run_cli("check", "--bogus")[0], 4)

    def test_check_output(self):
        _, out, _ = self.run_cli("check", "ok", "error-auth")
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("ok: WARN · 5h 42% (resets "))
        self.assertIn("7d 68% · Model 7d 80%", lines[0])
        self.assertEqual(lines[1], "error-auth: UNKNOWN · refresh failed (auth): please log in")

    # --- selection ---

    def test_missing_hidden_unless_explicit(self):
        self.assertNotIn("missing", self.json_of())
        self.assertIn("missing", self.json_of("missing"))
        self.config('providers = ["missing", "ok"]')
        self.assertEqual(list(self.json_of()), ["missing", "ok"])

    def test_config_order_and_unknown(self):
        self.config('providers = ["ok", "short-only"]')
        self.assertEqual(list(self.json_of()), ["ok", "short-only"])
        self.config('providers = ["ok", "nope"]')
        code, _, err = self.run_cli("status")
        self.assertEqual(code, 4)
        self.assertIn("nope", err)

    def test_user_provider_overrides_builtin(self):
        user = self.tmp / "config" / "trai" / "providers" / "ok"
        shutil.copytree(FAKES / "short-only", user)
        p = self.json_of("ok")["ok"]
        self.assertEqual(p["name"], "Short only")
        self.assertIsNone(p["long"])

    def test_path_reaches_providers(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        tool = bin_dir / "usage-tool"
        tool.write_text('#!/bin/sh\necho \'{"short": {"used_percent": 33}}\'\n')
        tool.chmod(0o755)
        user = self.tmp / "config" / "trai" / "providers" / "tool"
        user.mkdir(parents=True)
        (user / "provider.toml").write_text('name = "Tool"\n')
        (user / "fetch").write_text("#!/bin/sh\nexec usage-tool\n")
        (user / "fetch").chmod(0o755)
        self.assertEqual(self.json_of("tool")["tool"]["error"]["kind"], "crash")
        self.config(f'path = ["{bin_dir}"]')
        p = self.json_of("tool", "--refresh")["tool"]
        self.assertEqual(p["short"]["used_percent"], 33)

    def user_provider(self, name, script, timeout=None):
        path = self.tmp / "config" / "trai" / "providers" / name
        path.mkdir(parents=True)
        (path / "provider.toml").write_text(f'name = "{name}"\n' + (f"timeout = {timeout}\n" if timeout else ""))
        (path / "fetch").write_text("#!/bin/sh\n" + script + "\n")
        (path / "fetch").chmod(0o755)

    def test_nan_is_invalid_output(self):
        self.user_provider("nan", """echo '{"short": {"used_percent": NaN}}'""")
        error = self.json_of("nan")["nan"]["error"]
        self.assertEqual(error["kind"], "crash")
        self.assertIn("NaN", error["message"])

    def test_timeout_with_escaped_child(self):
        # The child leaves the process group but keeps stdout open.
        self.user_provider("escape", "setsid sleep 6 &\nsleep 10", timeout=0.5)
        start = time.monotonic()
        error = self.json_of("escape")["escape"]["error"]
        self.assertEqual(error["kind"], "timeout")
        self.assertLess(time.monotonic() - start, 4)

    def test_stuck_lock_returns_cache_with_error(self):
        self.user_provider("quick", """echo '{"long": {"used_percent": 95}}'""", timeout=0.2)
        self.run_cli("status", "quick")
        with mock.patch.object(cache, "LOCK_GRACE", 0.3), \
                open(self.tmp / "cache" / "trai" / "quick.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            start = time.monotonic()
            out = self.run_cli("status", "--json", "--refresh", "quick")[1]
        self.assertLess(time.monotonic() - start, 2)
        p = json.loads(out)["providers"][0]
        self.assertEqual(p["error"]["kind"], "timeout")
        self.assertEqual(p["long"]["used_percent"], 95)

    def test_gui_keeps_valid_config_on_error(self):
        self.config('providers = ["ok"]\nrefresh_second = 60')
        gui = json.loads(self.run_cli("status", "--gui")[1])
        self.assertIn("refresh_second", gui["config_error"])
        self.assertEqual([p["id"] for p in gui["providers"]], ["ok"])

    def test_color_override(self):
        self.config('[colors]\nok = "#123456"')
        self.assertEqual(self.json_of("ok")["ok"]["color"], "#123456")
        self.assertEqual(self.json_of("crash")["crash"]["color"], "#888888")

    # --- cache ---

    def test_cache_reuse_and_refresh(self):
        self.run_cli("status", "scripted")
        self.run_cli("check", "scripted")
        self.assertEqual(self.runs(), 1)
        self.run_cli("status", "scripted", "--refresh")
        self.assertEqual(self.runs(), 2)

    def test_concurrent_refresh_is_shared(self):
        (self.tmp / "state" / "slow").touch()
        prov = provider.discover()["scripted"]
        threads = [threading.Thread(target=cache.get, args=(prov, 0)) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.runs(), 1)

    def test_old_cache_format_is_refetched(self):
        path = self.tmp / "cache" / "trai" / "scripted.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"success": {"data": {"inner": {}}, "at": 9e9}, "error": null}')
        self.assertEqual(self.json_of("scripted")["scripted"]["long"]["used_percent"], 95)
        self.assertEqual(self.runs(), 1)

    def test_failure_keeps_last_data(self):
        self.run_cli("status", "scripted")
        (self.tmp / "state" / "fail").touch()
        p = self.json_of("scripted")  # cached, no refresh
        self.assertIsNone(p["scripted"]["error"])
        code, out, _ = self.run_cli("status", "--json", "--refresh", "scripted")
        p = json.loads(out)["providers"][0]
        self.assertEqual(p["error"]["kind"], "unavailable")
        self.assertTrue(p["stale"])
        self.assertEqual(p["long"]["used_percent"], 95)
        self.assertEqual(p["level"], "critical")
        # Old data plus a failed refresh is unknown for check, once older than --max-age.
        self.assertEqual(self.run_cli("check", "scripted", "--max-age", "3600")[0], 2)
        self.assertEqual(self.run_cli("check", "scripted", "--max-age", "0")[0], 3)

    def age_cache(self, pid, seconds):
        path = self.tmp / "cache" / "trai" / f"{pid}.json"
        entry = json.loads(path.read_text())
        entry["success"]["at"] -= seconds
        path.write_text(json.dumps(entry))

    def test_read_only_cache(self):
        # As for a sandboxed agent: the cache can be read, but not refreshed.
        self.run_cli("status", "scripted")
        read_only = mock.patch.object(cache, "_lock", side_effect=OSError(30, "Read-only file system"))
        with read_only:
            code, out, _ = self.run_cli("check", "scripted")  # fresh: no refresh needed
            self.assertEqual((code, out.strip()), (2, "scripted: CRITICAL · 50% · 95%"))
            self.age_cache("scripted", 300)  # older than refresh_seconds, not yet stale
            code, out, _ = self.run_cli("check", "scripted")
            self.assertEqual(code, 2)
            self.assertIn("(as of 5 min ago; refresh failed (cache): cannot write the cache: Read-only", out)
            self.age_cache("scripted", 100)  # stale
            code, out, _ = self.run_cli("check", "scripted")
            self.assertEqual(code, 3)
            self.assertTrue(out.startswith("scripted: UNKNOWN · 50% · 95%  (as of 6 min ago;"))
        self.assertEqual(self.runs(), 1)

    def test_explicit_max_age_shows_age(self):
        self.run_cli("status", "scripted")
        self.age_cache("scripted", 3 * 3600)
        code, out, _ = self.run_cli("check", "scripted", "--max-age", "86400")
        self.assertEqual((code, out.strip()), (2, "scripted: CRITICAL · 50% · 95%  (as of 3 h ago)"))
        self.assertEqual(self.runs(), 1)

    def test_internal_error_is_unknown(self):
        with mock.patch.object(cli, "cmd_check", side_effect=RuntimeError("bug")):
            code, _, err = self.run_cli("check", "scripted")
        self.assertEqual(code, 3)
        self.assertIn("RuntimeError: bug", err)

    # --- gui, providers, config ---

    def test_gui_reports_config_error(self):
        self.config("show = 3")
        code, out, _ = self.run_cli("status", "--gui")
        self.assertEqual(code, 0)
        gui = json.loads(out)
        self.assertEqual(gui["gui_schema"], 1)
        self.assertIn("show", gui["config_error"])
        self.assertEqual(gui["config"]["show"], "used")
        self.assertNotIn("missing", [p["id"] for p in gui["providers"]])

    def test_providers_listing(self):
        self.run_cli("status", "ok")
        code, out, _ = self.run_cli("providers")
        self.assertEqual(code, 0)
        rows = {line.split()[0]: line for line in out.splitlines()[1:]}
        self.assertIn("ok (just now)", rows["ok"])
        self.assertIn("never fetched", rows["crash"])

    def test_config_init(self):
        code, out, _ = self.run_cli("config", "init")
        self.assertEqual(code, 0)
        path = self.tmp / "config" / "trai" / "config.toml"
        self.assertTrue(path.exists())
        self.assertEqual(self.run_cli("config", "init")[0], 4)
        self.assertEqual(self.run_cli("config", "path")[1].strip(), str(path))


if __name__ == "__main__":
    unittest.main()
