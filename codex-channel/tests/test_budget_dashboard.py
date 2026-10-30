import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import channel
import dashboard
import mcp_server
import task_attribution
import weekly_budget


class FakeProcess:
    def __init__(self):
        self.running = True

    def poll(self):
        return None if self.running else 0

    def wait(self, timeout=None):
        self.running = False
        return 0

    def send_signal(self, _signal):
        self.running = False


class RunningDashboard:
    def __init__(self, root):
        ui = root / "ui"
        ui.mkdir()
        (ui / "index.html").write_text("<!doctype html><title>test</title>", "utf-8")
        self.server = dashboard.create_server(root / "state", 0, token="owner-token", ui_root=ui)
        self.state = self.server.state
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def subscription_node(self):
        home = self.state.root / "supplier" / "subscription"
        channel.initialize(
            home,
            "Owner",
            "gpt-6.1-sol",
            backend="subscription-adapter",
            upstream="http://127.0.0.1:8317/v1",
            key_env="TEST_ONLY_KEY",
        )
        task_attribution.initialize_schema(home)
        self.state.supplier_home = home
        self.state.settings["supplier"] = {
            "mode": "subscription",
            "name": "Owner",
            "model": "gpt-6.1-sol",
            "host": "127.0.0.1",
            "port": 8781,
            "allow_private_http": False,
            "connection": "manual",
        }
        self.state.supplier_process = FakeProcess()
        return home

    def request(self, path, body, *, token=True, origin=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        authority = "127.0.0.1:" + str(self.server.server_port)
        headers = {"Host": authority, "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer owner-token"
        if origin:
            headers["Origin"] = "http://" + authority
        connection.request("POST", path, json.dumps(body), headers)
        response = connection.getresponse()
        value = json.loads(response.read())
        connection.close()
        return response.status, value

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.state.close()


def profile():
    return {
        "profile_schema_version": 1,
        "label": "experimental_estimate",
        "billing_status": "not_billing",
        "calibration_scope_hash": "a" * 64,
        "stratum": {
            "model": "gpt-6.1-sol",
            "reasoning_effort": "low",
            "service_tier": "default",
        },
        "coefficients": {
            key: value
            for key, value in zip(weekly_budget.COEFFICIENT_KEYS, (1.0, 0.2, 4.0))
        },
        "validation": {
            "mae_percentage_points": 0.1,
            "max_error_percentage_points": 0.25,
        },
        "sample_counts": {"training": 8, "validation": 4},
        "limitations": ["Experimental estimate only."],
    }


class BudgetDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = RunningDashboard(Path(self.temp.name))
        self.home = self.app.subscription_node()

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def install(self, value=None):
        return self.app.request(
            "/api/supplier/calibration",
            {"profile": value or profile(), "valid_until": time.time() + 3600},
        )

    def test_calibration_requires_owner_auth_and_same_origin(self):
        body = {"profile": profile(), "valid_until": time.time() + 3600}
        self.assertEqual(self.app.request("/api/supplier/calibration", body, token=False)[0], 401)
        self.assertEqual(self.app.request("/api/supplier/calibration", body, origin=False)[0], 403)
        status, result = self.app.request("/api/supplier/calibration", body)
        self.assertEqual(status, 200)
        self.assertEqual(result["billing_status"], "not_billing")
        self.assertNotIn("TEST_ONLY_KEY", json.dumps(result))

    def test_bad_profile_does_not_replace_existing_profile(self):
        self.assertEqual(self.install()[0], 200)
        with channel.db(self.home) as connection:
            before = weekly_budget.active_profile(connection)["profile_id"]
        invalid = profile()
        invalid["coefficients"] = {}
        status, _ = self.install(invalid)
        self.assertEqual(status, 400)
        with channel.db(self.home) as connection:
            after = weekly_budget.active_profile(connection)["profile_id"]
        self.assertEqual(before, after)

    def test_percent_invite_without_profile_leaves_no_channel_or_grant(self):
        status, result = self.app.request(
            "/api/supplier/invites",
            {
                "base_url": "http://127.0.0.1:8781",
                "label": "Friend",
                "budget_mode": "estimated_weekly",
                "weekly_percent": 20,
                "requests": 1000,
                "minutes": 60,
                "allow_private_http": False,
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["code"], "profile_required")
        with channel.db(self.home) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM channels").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM weekly_budget_grants").fetchone()[0], 0)
        self.assertEqual(self.install()[0], 200)
        status, result = self.app.request(
            "/api/supplier/invites",
            {
                "base_url": "http://127.0.0.1:8781",
                "label": "Friend",
                "budget_mode": "estimated_weekly",
                "requests": 1000,
                "minutes": 60,
                "allow_private_http": False,
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["code"], "invalid_budget_percent")
        with channel.db(self.home) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM channels").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM weekly_budget_grants").fetchone()[0], 0)

    def test_mock_node_rejects_weekly_budget_mode(self):
        other_temp = tempfile.TemporaryDirectory()
        other = RunningDashboard(Path(other_temp.name))
        try:
            status, started = other.request(
                "/api/supplier/start",
                {
                    "mode": "mock",
                    "name": "Mock",
                    "model": "tokencoin-demo",
                    "host": "127.0.0.1",
                    "port": 0,
                    "allow_private_http": False,
                },
            )
            self.assertEqual(status, 200)
            status, result = other.request(
                "/api/supplier/invites",
                {
                    "base_url": started["base_url"],
                    "label": "Friend",
                    "budget_mode": "estimated_weekly",
                    "weekly_percent": 20,
                    "requests": 1000,
                    "minutes": 60,
                },
            )
            self.assertEqual(status, 409)
            self.assertEqual(result["error"]["code"], "weekly_budget_requires_subscription")
        finally:
            other.close()
            other_temp.cleanup()

    def test_official_reconcile_is_owner_api_only(self):
        self.assertEqual(self.install()[0], 200)
        status, created = self.app.request(
            "/api/supplier/invites",
            {
                "base_url": "http://127.0.0.1:8781",
                "label": "Friend",
                "budget_mode": "estimated_weekly",
                "weekly_percent": 20,
                "requests": 1000,
                "minutes": 60,
                "allow_private_http": False,
            },
        )
        self.assertEqual(status, 200)
        body = {
            "channel_id": created["invitation"]["channel_id"],
            "report": {
                "calibration_scope_hash": "a" * 64,
                "data_as_of": "2026-10-30T12:00:00Z",
                "threads": [],
            },
        }
        self.assertEqual(self.app.request("/api/supplier/reconcile", body, token=False)[0], 401)
        self.assertEqual(self.app.request("/api/supplier/reconcile", body, origin=False)[0], 403)
        status, result = self.app.request("/api/supplier/reconcile", body)
        self.assertEqual(status, 200)
        self.assertEqual(result["mode"], "estimated_weekly")

    def test_mcp_bridge_is_explicit_allowlist(self):
        plugin = mcp_server.Plugin(Path(self.temp.name) / "plugin")
        try:
            self.assertTrue(plugin.action({"method": "POST", "path": "/v1/channel", "body": {}})["isError"])
            self.assertTrue(plugin.action({"method": "POST", "path": "/api/supplier/not-a-route", "body": {}})["isError"])
        finally:
            plugin.close()

    def test_ui_exposes_estimate_and_manual_reconcile_without_claiming_guarantees(self):
        html = (ROOT / "ui" / "index.html").read_text("utf-8")
        script = (ROOT / "ui" / "app.js").read_text("utf-8")
        self.assertIn("周额度比例（估算）", html)
        self.assertIn("尚无真实校准数据，无法估算周比例", html)
        self.assertIn("本次授权最晚截止，不自动续期", html)
        self.assertIn("导入官方账单", script)
        self.assertIn("参考范围暂无", script)
        self.assertNotIn("置信区间", html + script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
