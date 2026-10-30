import json
import sqlite3
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import weekly_budget


class WeeklyBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.c = sqlite3.connect(self.path, timeout=5)
        self.c.executescript(
            """CREATE TABLE channels(id TEXT PRIMARY KEY);
               CREATE TABLE requests(id TEXT PRIMARY KEY,channel_id TEXT,status TEXT,
                   started REAL,ended REAL,usage TEXT);
               CREATE TABLE task_attribution_requests(request_id TEXT PRIMARY KEY,
                   channel_id TEXT,upstream_task_id TEXT,turn_id TEXT,window_id TEXT,created_at REAL);"""
        )
        weekly_budget.initialize(self.c)
        self.profile = self.make_profile()
        weekly_budget.install_profile(self.c, self.profile)
        self.c.commit()

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def make_profile(self, **changes):
        value = {
            "profile_schema_version": 1,
            "label": "experimental_estimate",
            "billing_status": "not_billing",
            "stratum": {"model": "gpt-test", "reasoning_effort": "low", "service_tier": "default"},
            "coefficients": {
                "weekly_limit_percentage_points_per_million_uncached_input_tokens": 10.0,
                "weekly_limit_percentage_points_per_million_cached_input_tokens": 2.0,
                "weekly_limit_percentage_points_per_million_output_tokens": 20.0,
            },
            "validation": {"mae_percentage_points": 0.2, "max_error_percentage_points": 0.5},
            "sample_counts": {"training": 8, "validation": 4},
            "calibration_scope_hash": "a" * 64,
            "validated_until": time.time() + 86400,
            "limitations": ["experimental"],
        }
        value.update(changes)
        return value

    def grant(self, channel="friend-a", percent=20):
        self.c.execute("INSERT OR IGNORE INTO channels(id) VALUES(?)", (channel,))
        return weekly_budget.create_grant(self.c, channel, percent)

    def add(self, request_id, channel, task, *, status="completed_upstream", usage=True, ended=None):
        ended = time.time() if ended is None else ended
        value = None
        if usage:
            value = json.dumps({
                "source": "upstream_reported",
                "requested_model": "gpt-test",
                "requested_reasoning_effort": "low",
                "requested_service_tier": "default",
                "reported_model": "gpt-test",
                "reported_service_tier": "default",
                "input_tokens": 1_000_000,
                "cached_input_tokens": 250_000,
                "uncached_input_tokens": 750_000,
                "output_tokens": 100_000,
            })
        self.c.execute("INSERT INTO requests VALUES(?,?,?,?,?,?)",
                       (request_id, channel, status, ended - 1, None if status == "running" else ended, value))
        if task is not None:
            self.c.execute("INSERT INTO task_attribution_requests VALUES(?,?,?,?,?,?)",
                           (request_id, channel, task, "turn-" + request_id, "window-" + task, ended - 1))

    def official(self, tasks, data_as_of):
        return {"calibration_scope_hash": "a" * 64, "data_as_of": data_as_of, "threads": tasks}

    def item(self, task, percent, status="available"):
        return {"thread_id": task, "data_status": status,
                "usage_source": "included_plan", "weekly_limit_percent": percent}

    def test_estimates_all_records_and_isolates_friends_and_owner(self):
        self.grant("friend-a")
        self.add("a1", "friend-a", "task-a")
        self.add("b1", "friend-b", "task-b")
        self.add("owner1", "owner", None)
        result = weekly_budget.summary(self.c, "friend-a")
        self.assertAlmostEqual(result["estimated_used_percent"], 10.0)
        self.assertEqual(result["known_request_count"], 1)
        self.assertEqual(result["unknown_request_count"], 0)

    def test_unknown_is_not_zero_and_blocks(self):
        self.grant()
        self.add("known", "friend-a", "task-known")
        self.add("lost", "friend-a", "task-lost", status="unknown_incomplete_stream", usage=False)
        result = weekly_budget.summary(self.c, "friend-a")
        self.assertEqual(result["unknown_request_count"], 1)
        self.assertEqual(result["blocked_reason"], "pending_unknown")
        self.assertEqual(result["known_used_percent"], 10.0)
        self.assertIsNone(result["estimated_used_percent"])
        self.assertIsNone(result["reference_low_percent"])
        self.assertIsNone(result["reference_high_percent"])
        with self.assertRaisesRegex(weekly_budget.BudgetError, "pending_unknown"):
            weekly_budget.check(self.c, "friend-a")

    def test_official_reconcile_is_idempotent_and_covers_unknown(self):
        self.grant()
        ended = time.time() - 10
        self.add("lost", "friend-a", "task-a", status="unknown_incomplete_stream", usage=False, ended=ended)
        payload = self.official([self.item("task-a", 3.25)], ended + 1)
        first = weekly_budget.reconcile(self.c, "friend-a", payload)
        second = weekly_budget.reconcile(self.c, "friend-a", payload)
        self.assertEqual(first["estimated_used_percent"], 3.25)
        self.assertEqual(second["official_used_percent"], 3.25)
        self.assertEqual(second["unknown_request_count"], 0)
        with self.assertRaisesRegex(weekly_budget.BudgetError, "official_data_regressed"):
            weekly_budget.reconcile(
                self.c, "friend-a",
                self.official([self.item("task-a", 3.0)], ended + 2),
            )
        retained = weekly_budget.summary(self.c, "friend-a")
        self.assertEqual(retained["official_used_percent"], 3.25)

    def test_foreign_batch_rejects_atomically(self):
        self.grant("friend-a")
        self.add("a1", "friend-a", "task-a", ended=time.time() - 10)
        self.add("b1", "friend-b", "task-b", ended=time.time() - 10)
        payload = self.official([self.item("task-a", 2), self.item("task-b", 4)], time.time())
        with self.assertRaisesRegex(weekly_budget.BudgetError, "foreign_or_unknown_task"):
            weekly_budget.reconcile(self.c, "friend-a", payload)
        count = self.c.execute("SELECT COUNT(*) FROM weekly_budget_official_tasks").fetchone()[0]
        self.assertEqual(count, 0)

    def test_new_request_after_official_snapshot_returns_to_pending(self):
        self.grant()
        ended = time.time() - 20
        self.add("old", "friend-a", "task-a", ended=ended)
        weekly_budget.reconcile(self.c, "friend-a",
                                self.official([self.item("task-a", 2)], ended + 1))
        # Even when the old data_as_of is later than this new request, its saved
        # request count proves that the old snapshot did not cover the new turn.
        self.add("new", "friend-a", "task-a", ended=ended + 0.5)
        result = weekly_budget.summary(self.c, "friend-a")
        self.assertEqual(result["blocked_reason"], "pending_unknown")
        self.assertEqual(result["unknown_request_count"], 2)

    def test_future_official_snapshot_is_rejected(self):
        self.grant()
        self.add("request", "friend-a", "task-a", ended=time.time() - 1)
        payload = self.official([self.item("task-a", 1.0)], time.time() + 6)
        with self.assertRaisesRegex(weekly_budget.BudgetError, "official_data_as_of_in_future"):
            weekly_budget.reconcile(self.c, "friend-a", payload)
        count = self.c.execute(
            "SELECT COUNT(*) FROM weekly_budget_official_tasks"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_reconcile_reports_available_and_ignored_partial_counts(self):
        self.grant()
        ended = time.time() - 5
        self.add("available", "friend-a", "task-a", ended=ended)
        self.add("partial", "friend-a", "task-b", ended=ended)
        payload = self.official(
            [self.item("task-a", 1.0), self.item("task-b", None, status="partial")],
            ended + 1,
        )
        result = weekly_budget.reconcile(self.c, "friend-a", payload)
        self.assertEqual(result["reconciled_task_count"], 1)
        self.assertEqual(result["ignored_partial_task_count"], 1)

    def test_profile_expiry_and_request_stratum_block(self):
        self.grant()
        expired = weekly_budget.summary(self.c, "friend-a", now=self.profile["validated_until"] + 1)
        self.assertEqual(expired["blocked_reason"], "profile_expired")
        bodies = (
            {"model": "other", "reasoning": {"effort": "low"}, "service_tier": "default"},
            {"model": "gpt-test", "reasoning": {"effort": "high"}, "service_tier": "default"},
            {"model": "gpt-test", "reasoning": {"effort": "low"}, "service_tier": "priority"},
        )
        for body in bodies:
            with self.assertRaisesRegex(weekly_budget.BudgetError, "profile_mismatch"):
                weekly_budget.check(self.c, "friend-a", body)

    def test_grant_keeps_immutable_profile_snapshot(self):
        created = self.grant()
        changed = self.make_profile(coefficients={
            "weekly_limit_percentage_points_per_million_uncached_input_tokens": 99.0,
            "weekly_limit_percentage_points_per_million_cached_input_tokens": 99.0,
            "weekly_limit_percentage_points_per_million_output_tokens": 99.0,
        })
        active = weekly_budget.install_profile(self.c, changed)
        self.assertNotEqual(created["profile_id"], active["profile_id"])
        self.add("old-profile", "friend-a", "task-a")
        result = weekly_budget.summary(self.c, "friend-a")
        self.assertEqual(result["profile_id"], created["profile_id"])
        self.assertEqual(result["estimated_used_percent"], 10.0)

    def test_reference_upper_bound_pauses_admission(self):
        self.grant(percent=10.4)
        self.add("near-limit", "friend-a", "task-a")
        result = weekly_budget.summary(self.c, "friend-a")
        self.assertEqual(result["estimated_used_percent"], 10.0)
        self.assertEqual(result["reference_high_percent"], 10.5)
        self.assertEqual(result["blocked_reason"], "estimated_budget_reached")

    def test_official_amount_is_always_reference_lower_bound(self):
        wide_error = self.make_profile(validation={
            "mae_percentage_points": 1.0,
            "max_error_percentage_points": 20.0,
        })
        weekly_budget.install_profile(self.c, wide_error)
        self.grant()
        ended = time.time() - 10
        self.add("official", "friend-a", "task-official", ended=ended)
        weekly_budget.reconcile(
            self.c,
            "friend-a",
            self.official([self.item("task-official", 3.0)], ended + 1),
        )
        self.add("estimated", "friend-a", "task-estimated")
        result = weekly_budget.summary(self.c, "friend-a")
        self.assertEqual(result["official_used_percent"], 3.0)
        self.assertEqual(result["reference_low_percent"], 3.0)

    def test_inconsistent_or_invalid_reported_usage_is_unknown(self):
        self.grant()
        self.add("inconsistent", "friend-a", "task-a")
        self.add("invalid", "friend-a", "task-b")
        rows = self.c.execute(
            "SELECT id,usage FROM requests WHERE channel_id='friend-a'"
        ).fetchall()
        for request_id, encoded in rows:
            usage = json.loads(encoded)
            if request_id == "inconsistent":
                usage["total_consistent"] = False
            else:
                usage["invalid_fields"] = ["cached_input_tokens"]
            self.c.execute("UPDATE requests SET usage=? WHERE id=?",
                           (json.dumps(usage), request_id))
        result = weekly_budget.summary(self.c, "friend-a")
        self.assertEqual(result["unknown_request_count"], 2)
        self.assertIsNone(result["estimated_used_percent"])

    def test_running_hides_total_but_keeps_known_subtotal(self):
        self.grant()
        self.add("known", "friend-a", "task-known")
        self.add("running", "friend-a", "task-running", status="running", usage=False)
        result = weekly_budget.summary(self.c, "friend-a")
        self.assertEqual(result["blocked_reason"], "running")
        self.assertEqual(result["known_used_percent"], 10.0)
        self.assertIsNone(result["estimated_used_percent"])

    def test_initialize_does_not_commit_caller_transaction(self):
        self.c.execute("INSERT INTO requests VALUES(?,?,?,?,?,?)",
                       ("rollback-me", "friend-a", "completed_upstream", 1, 2, None))
        weekly_budget.initialize(self.c)
        self.c.rollback()
        count = self.c.execute(
            "SELECT COUNT(*) FROM requests WHERE id='rollback-me'"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_legacy_official_rows_migrate_fail_closed(self):
        path = Path(self.tmp.name) / "legacy.sqlite3"
        c = sqlite3.connect(path)
        c.executescript(
            """CREATE TABLE weekly_budget_official_tasks(
                   channel_id TEXT NOT NULL,task_id TEXT NOT NULL,
                   weekly_percent REAL NOT NULL,data_as_of REAL NOT NULL,
                   PRIMARY KEY(channel_id,task_id));
               INSERT INTO weekly_budget_official_tasks VALUES('friend','task',1.0,2.0);"""
        )
        weekly_budget.initialize(c)
        columns = {row[1] for row in c.execute(
            "PRAGMA table_info(weekly_budget_official_tasks)"
        )}
        covered = c.execute(
            "SELECT covered_request_count FROM weekly_budget_official_tasks"
        ).fetchone()[0]
        c.close()
        self.assertIn("covered_request_count", columns)
        self.assertEqual(covered, 0)

    def test_grant_rejects_missing_or_historical_channel(self):
        with self.assertRaisesRegex(weekly_budget.BudgetError, "channel_not_found"):
            weekly_budget.create_grant(self.c, "missing", 10)
        self.c.execute("INSERT INTO channels(id) VALUES('historical')")
        self.add("old", "historical", "task-old")
        with self.assertRaisesRegex(weekly_budget.BudgetError, "channel_has_requests"):
            weekly_budget.create_grant(self.c, "historical", 10)

    def test_grant_is_immutable_and_concurrent_creation_has_one_winner(self):
        self.grant("existing", 10)
        with self.assertRaisesRegex(weekly_budget.BudgetError, "grant_already_exists"):
            weekly_budget.create_grant(self.c, "existing", 20)
        self.c.commit()
        self.c.execute("INSERT INTO channels(id) VALUES('racing')")
        self.c.commit()

        def create():
            c = sqlite3.connect(self.path, timeout=5)
            try:
                c.execute("BEGIN IMMEDIATE")
                weekly_budget.create_grant(c, "racing", 15)
                c.commit()
                return "created"
            except weekly_budget.BudgetError as exc:
                c.rollback()
                return exc.code
            finally:
                c.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _value: create(), range(2)))
        self.assertCountEqual(results, ["created", "grant_already_exists"])

    def test_invalid_profile_is_not_installed(self):
        bad = self.make_profile(validation={"mae_percentage_points": 2, "max_error_percentage_points": 1})
        with self.assertRaises(weekly_budget.BudgetError):
            weekly_budget.install_profile(self.c, bad)

    def test_profile_schema_overflow_and_expired_grant_are_rejected(self):
        missing_schema = self.make_profile()
        missing_schema.pop("profile_schema_version")
        with self.assertRaisesRegex(weekly_budget.BudgetError, "invalid_profile_schema_version"):
            weekly_budget.install_profile(self.c, missing_schema)
        huge = self.make_profile(coefficients={
            key: 10**1000 for key in weekly_budget.COEFFICIENT_KEYS
        })
        with self.assertRaisesRegex(weekly_budget.BudgetError, "invalid_coefficient"):
            weekly_budget.install_profile(self.c, huge)
        self.c.execute("INSERT INTO channels(id) VALUES('expired')")
        with mock.patch.object(weekly_budget.time, "time", return_value=self.profile["validated_until"] + 1):
            with self.assertRaisesRegex(weekly_budget.BudgetError, "profile_expired"):
                weekly_budget.create_grant(self.c, "expired", 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
