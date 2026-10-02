import copy
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from trai.model import validate_output

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


def load_fetch(name):
    path = ROOT / "trai" / "providers" / name / "fetch"
    loader = importlib.machinery.SourceFileLoader(f"fetch_{name}", str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class ClaudeTest(unittest.TestCase):
    fetch = load_fetch("claude")

    def test_fixture(self):
        kind, data = validate_output(self.fetch.parse(fixture("claude")))
        self.assertEqual(kind, "data")
        self.assertEqual(data["short"]["used_percent"], 7)
        self.assertEqual(data["short"]["label"], "5h")
        self.assertEqual(data["long"]["used_percent"], 4)
        self.assertEqual(data["long"]["label"], "7d")
        self.assertEqual(data["windows"], [])
        self.assertEqual(data["extra"]["plan"], "pro")
        self.assertFalse(data["extra"]["overage"]["enabled"])

    def test_per_model_limit(self):
        response = fixture("claude")
        response["rate_limits"]["limits"].append({"kind": "weekly_model", "group": "weekly", "percent": 91,
                                                  "resets_at": "2026-10-03T09:00:00+00:00", "scope": "opus"})
        _, data = validate_output(self.fetch.parse(response))
        self.assertEqual(data["windows"][0]["label"], "Opus 7d")
        self.assertEqual(data["windows"][0]["id"], "weekly_model_opus")

    def test_fallback_without_limits_list(self):
        response = fixture("claude")
        del response["rate_limits"]["limits"]
        response["rate_limits"]["seven_day_opus"] = {"utilization": 12, "resets_at": "2026-10-03T09:00:00+00:00"}
        _, data = validate_output(self.fetch.parse(response))
        self.assertEqual(data["short"]["used_percent"], response["rate_limits"]["five_hour"]["utilization"])
        self.assertEqual(data["windows"][0]["label"], "Opus 7d")

    def test_empty_limits_list_falls_back(self):
        response = fixture("claude")
        response["rate_limits"]["limits"] = []
        _, data = validate_output(self.fetch.parse(response))
        self.assertEqual(data["short"]["used_percent"], response["rate_limits"]["five_hour"]["utilization"])

    def test_no_rate_limits(self):
        response = copy.deepcopy(fixture("claude"))
        response["rate_limits_available"] = False
        with self.assertRaises(self.fetch.Failure):
            self.fetch.parse(response)

    def test_classify(self):
        self.assertEqual(self.fetch.classify("Not logged in · Please run /login"), "auth")
        self.assertEqual(self.fetch.classify("fetch failed: ENOTFOUND"), "unavailable")
        self.assertEqual(self.fetch.classify("something odd"), "other")


class CodexTest(unittest.TestCase):
    fetch = load_fetch("codex")

    def test_fixture(self):
        kind, data = validate_output(self.fetch.parse(fixture("codex")))
        self.assertEqual(kind, "data")
        self.assertEqual(data["short"]["label"], "5h")
        self.assertEqual(data["long"]["label"], "7d")
        self.assertEqual(data["long"]["used_percent"], 3)
        self.assertTrue(data["long"]["resets_at"].endswith("+00:00"))
        self.assertEqual(data["windows"], [])
        self.assertEqual(data["extra"]["plan"], "team")
        self.assertEqual(data["extra"]["reset_credits"], 2)

    def fake_codex(self, body):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        path = os.path.join(tmp, "codex")
        with open(path, "w") as f:
            f.write("#!/usr/bin/env python3\nimport json, sys, time\n" + body)
        os.chmod(path, 0o755)
        return path

    def test_notification_and_reply_in_one_chunk(self):
        server = self.fake_codex(f"""
result = json.load(open({str(FIXTURES / "codex.json")!r}))
sys.stdin.readline()
sys.stdout.write(json.dumps({{"id": 1, "result": {{}}}}) + "\\n")
sys.stdout.flush()
request = json.loads(sys.stdin.readline())
# A notification and the reply arrive in a single write.
sys.stdout.write(json.dumps({{"method": "note"}}) + "\\n" + json.dumps({{"id": request["id"], "result": result}}) + "\\n")
sys.stdout.flush()
time.sleep(5)
""")
        start = time.monotonic()
        data = self.fetch.parse(self.fetch.query(server))
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(data["long"]["used_percent"], 3)

    def test_server_exits_immediately(self):
        server = self.fake_codex("sys.exit(1)\n")
        with self.assertRaises(self.fetch.Failure) as caught:
            self.fetch.query(server)
        self.assertEqual(caught.exception.kind, "unavailable")

    def test_model_specific_limit(self):
        result = fixture("codex")
        result["rateLimitsByLimitId"]["spark"] = {
            "limitId": "spark", "limitName": "GPT-Spark",
            "primary": {"usedPercent": 30, "windowDurationMins": 300, "resetsAt": 1790904460},
            "secondary": None,
        }
        _, data = validate_output(self.fetch.parse(result))
        self.assertEqual(data["windows"], [{
            "id": "spark_primary", "label": "GPT-Spark 5h", "used_percent": 30,
            "resets_at": "2026-10-02T01:27:40+00:00", "window_minutes": 300,
        }])


if __name__ == "__main__":
    unittest.main()
