import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import task_attribution


class TaskAttributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        connection = sqlite3.connect(self.home / "state.sqlite3")
        with connection:
            connection.execute(
                "CREATE TABLE requests(id TEXT PRIMARY KEY, channel_id TEXT NOT NULL)"
            )
        connection.close()
        task_attribution.initialize_schema(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def reserve(self, request_id, channel_id):
        connection = sqlite3.connect(self.home / "state.sqlite3")
        with connection:
            connection.execute(
                "INSERT INTO requests(id,channel_id) VALUES(?,?)",
                (request_id, channel_id),
            )
        connection.close()

    def prepare(self, request_id, channel_id="channel-a", hint="same-hint", body=None):
        self.reserve(request_id, channel_id)
        return task_attribution.prepare(
            self.home,
            channel_id,
            request_id,
            body or {"model": "codex", "input": "do work", "stream": True},
            hint,
        )

    def test_same_hint_is_stable_but_scoped_to_channel(self):
        first = self.prepare("request-1")
        second = self.prepare("request-2")
        foreign = self.prepare("request-3", channel_id="channel-b")
        self.assertEqual(first["supplier_task_id"], second["supplier_task_id"])
        self.assertNotEqual(first["turn_id"], second["turn_id"])
        self.assertNotEqual(first["supplier_task_id"], foreign["supplier_task_id"])

    def test_client_native_ids_and_parentage_do_not_pass_through(self):
        forged = "00000000-0000-0000-0000-000000000000"
        body = {
            "model": "codex",
            "input": [{"role": "user", "content": "keep me"}],
            "prompt_cache_key": forged,
            "parent_thread_id": forged,
            "root_thread_id": forged,
            "client_metadata": {
                "thread_id": forged,
                "session_id": forged,
                "parent_thread_id": forged,
                "root_turn_id": forged,
                "ParentThreadId": forged,
                "root-thread-id": forged,
                "x-codex-parent-thread-id": forged,
                "x-codex-turn-metadata": json.dumps({"thread_id": forged}),
                "safe_extension": "preserved",
            },
        }
        result = self.prepare("request-forged", body=body)
        prepared = result["body"]
        task_id = result["supplier_task_id"]
        self.assertEqual(prepared["input"], body["input"])
        self.assertNotIn("parent_thread_id", prepared)
        self.assertNotIn("root_thread_id", prepared)
        self.assertEqual(prepared["prompt_cache_key"], task_id)
        self.assertEqual(prepared["client_metadata"]["safe_extension"], "preserved")
        encoded = prepared["client_metadata"]["x-codex-turn-metadata"]
        self.assertNotIn(forged, encoded)
        self.assertNotIn("parent", encoded)
        self.assertNotIn("root", encoded)
        self.assertNotIn("ParentThreadId", prepared["client_metadata"])
        self.assertNotIn("root-thread-id", prepared["client_metadata"])

    def test_native_task_identity_is_consistent_and_turn_is_separate(self):
        result = self.prepare("request-native")
        task_id = result["supplier_task_id"]
        metadata = result["body"]["client_metadata"]
        turn_metadata = json.loads(metadata["x-codex-turn-metadata"])
        self.assertEqual(result["body"]["prompt_cache_key"], task_id)
        for name in ("session-id", "thread-id", "x-client-request-id"):
            self.assertEqual(result["headers"][name], task_id)
        self.assertEqual(metadata["session_id"], task_id)
        self.assertEqual(metadata["thread_id"], task_id)
        self.assertEqual(turn_metadata["thread_id"], task_id)
        self.assertEqual(turn_metadata["turn_id"], result["turn_id"])
        self.assertNotEqual(result["turn_id"], task_id)

    def test_restart_and_repeat_prepare_keep_request_association(self):
        result = self.prepare("request-restart")
        task_attribution.initialize_schema(self.home)
        repeated = task_attribution.prepare(
            self.home,
            "channel-a",
            "request-restart",
            {"model": "codex", "input": "same reservation", "stream": True},
            "different-hint-is-ignored-after-binding",
        )
        self.assertEqual(result["supplier_task_id"], repeated["supplier_task_id"])
        self.assertEqual(result["turn_id"], repeated["turn_id"])

    def test_concurrent_first_use_atomically_shares_one_task(self):
        self.reserve("request-race-1", "channel-a")
        self.reserve("request-race-2", "channel-a")
        barrier = threading.Barrier(2)

        def run(request_id):
            barrier.wait()
            return task_attribution.prepare(
                self.home,
                "channel-a",
                request_id,
                {"model": "codex", "input": request_id},
                "new-shared-task",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(run, ("request-race-1", "request-race-2")))
        self.assertEqual(first["supplier_task_id"], second["supplier_task_id"])
        self.assertNotEqual(first["turn_id"], second["turn_id"])

    def test_request_must_belong_to_claimed_channel(self):
        self.reserve("request-bound", "channel-a")
        with self.assertRaises(task_attribution.AttributionError):
            task_attribution.prepare(
                self.home, "channel-b", "request-bound", {}, "forged-hint"
            )
        connection = sqlite3.connect(self.home / "state.sqlite3")
        count = connection.execute(
            "SELECT COUNT(*) FROM task_attribution_requests"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(count, 0)

    def test_missing_hint_creates_one_task_per_request(self):
        first = self.prepare("request-no-hint-1", hint=None)
        second = self.prepare("request-no-hint-2", hint=None)
        self.assertNotEqual(first["supplier_task_id"], second["supplier_task_id"])

    def test_hints_are_bounded_and_not_stored_in_plaintext(self):
        self.reserve("request-long", "channel-a")
        with self.assertRaises(task_attribution.AttributionError):
            task_attribution.prepare(
                self.home,
                "channel-a",
                "request-long",
                {"model": "codex"},
                "x" * (task_attribution.MAX_HINT_BYTES + 1),
            )
        secret_hint = "plain-client-hint-must-not-be-stored"
        self.prepare("request-secret", hint=secret_hint)
        self.assertNotIn(secret_hint.encode(), (self.home / "state.sqlite3").read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
