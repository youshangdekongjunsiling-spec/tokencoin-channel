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

import dashboard


class RunningDashboard:
    def __init__(self, root: Path):
        self.ui = root / "ui"
        self.ui.mkdir()
        (self.ui / "index.html").write_text("<!doctype html><title>TokenCoin</title>", encoding="utf-8")
        self.server = dashboard.create_server(root / "state", 0, token="test-token", ui_root=self.ui)
        self.state = self.server.state
        assert self.server.token == "test-token"
        assert self.server.origin == self.server.base_url
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_port

    def request(self, method, path, body=None, token=True, origin=True, host=None, content_type="application/json"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        authority = host or "127.0.0.1:" + str(self.port)
        headers = {"Host": authority}
        if token:
            headers["Authorization"] = "Bearer test-token"
        if origin:
            headers["Origin"] = "http://" + authority
        data = None
        if body is not None:
            data = json.dumps(body)
            headers["Content-Type"] = content_type
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        return response.status, value, dict(response.getheaders())

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.state.close()


class DashboardSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = RunningDashboard(Path(self.temp.name))

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_host_origin_auth_and_json_guards(self):
        status, _, _ = self.app.request("GET", "/api/status", token=False, origin=False)
        self.assertEqual(status, 401)
        status, _, _ = self.app.request("GET", "/api/status", host="evil.example", origin=False)
        self.assertEqual(status, 403)
        status, _, _ = self.app.request("POST", "/api/supplier/stop", {}, origin=False)
        self.assertEqual(status, 403)
        status, _, _ = self.app.request("POST", "/api/supplier/stop", {}, host="evil.example")
        self.assertEqual(status, 403)
        status, _, _ = self.app.request("POST", "/api/supplier/stop", {}, content_type="text/plain")
        self.assertEqual(status, 415)
        status, _, headers = self.app.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_static_path_is_confined(self):
        status, body, _ = self.app.request("GET", "/", token=False, origin=False)
        self.assertEqual(status, 200)
        self.assertIn(b"TokenCoin", body)
        status, _, _ = self.app.request("GET", "/../scripts/channel.py", token=False, origin=False)
        self.assertEqual(status, 404)
        status, _, _ = self.app.request("GET", "/%2e%2e/scripts/channel.py", token=False, origin=False)
        self.assertEqual(status, 404)

    def test_close_stops_owned_job_process(self):
        job = self.app.state._new_job("test_sleep")
        worker = threading.Thread(target=self.app.state._run_quiet_job,
                                  args=(job, [sys.executable, "-c", "import time; time.sleep(60)"]))
        worker.start()
        process = None
        for _ in range(50):
            process = self.app.state.job_processes.get(job["id"])
            if process:
                break
            time.sleep(.02)
        self.assertIsNotNone(process)
        self.app.state.close()
        worker.join(timeout=5)
        self.assertIsNotNone(process.poll())
        self.assertFalse(self.app.state.job_processes)

    def test_second_controller_for_same_state_is_rejected(self):
        with self.assertRaises(dashboard.ApiError) as raised:
            dashboard.DashboardState(self.app.state.root, self.app.ui, token="second-token")
        self.assertEqual(raised.exception.code, "dashboard_already_running")


class DashboardTwoNodeTests(unittest.TestCase):
    def setUp(self):
        self.temp_a = tempfile.TemporaryDirectory()
        self.temp_b = tempfile.TemporaryDirectory()
        self.a = RunningDashboard(Path(self.temp_a.name))
        self.b = RunningDashboard(Path(self.temp_b.name))

    def tearDown(self):
        self.b.close()
        self.a.close()
        self.temp_b.cleanup()
        self.temp_a.cleanup()

    def test_mock_invite_join_status_revoke(self):
        start = {"mode": "mock", "name": "Alice", "model": "tokencoin-demo",
                 "host": "127.0.0.1", "port": 0, "allow_private_http": False}
        status, supplier, _ = self.a.request("POST", "/api/supplier/start", start)
        self.assertEqual(status, 200)
        self.assertEqual(supplier["state"], "ready")

        status, created, _ = self.a.request("POST", "/api/supplier/invites", {
            "base_url": supplier["base_url"], "label": "Bob", "requests": 2, "minutes": 10,
            "allow_private_http": False})
        self.assertEqual(status, 200)
        invitation = created["invitation"]
        channel_id = invitation["channel_id"]
        self.assertIn("token", invitation)

        status, imported, _ = self.b.request("POST", "/api/peers/import", {
            "name": "alice", "invitation": invitation, "allow_private_http": False})
        self.assertEqual(status, 200)
        self.assertEqual(imported["status"]["channel_id"], channel_id)

        status, peers, _ = self.b.request("GET", "/api/peers")
        self.assertEqual(status, 200)
        self.assertEqual(peers["peers"][0]["name"], "alice")
        self.assertNotIn("token", peers["peers"][0])

        status, remote, _ = self.b.request("GET", "/api/peers/alice/status")
        self.assertEqual(status, 200)
        self.assertEqual(remote["remaining_requests"], 2)

        status, revoked, _ = self.a.request("POST", "/api/supplier/revoke", {"channel_id": channel_id})
        self.assertEqual(status, 200)
        self.assertTrue(revoked["revoked"])
        status, error, _ = self.b.request("GET", "/api/peers/alice/status")
        self.assertEqual(status, 409)
        self.assertEqual(error["error"]["code"], "peer_rejected_status")


if __name__ == "__main__":
    unittest.main()
