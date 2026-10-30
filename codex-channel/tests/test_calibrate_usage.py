import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import calibrate_usage


WEIGHTS = (2.5, 0.4, 8.0)


def sample(index, tokens=None, **overrides):
    tokens = tokens or (100_000 + index * 13_000, 40_000 + index * 7_000, 20_000 + index * 5_000)
    percent = sum(weight * token / 1_000_000 for weight, token in zip(WEIGHTS, tokens))
    row = {
        "task_id": f"task-{index}",
        "model": "codex-model",
        "reasoning_effort": "medium",
        "service_tier": "default",
        "calibration_scope": "account-plan-rule-epoch-a",
        "uncached_input_tokens": tokens[0],
        "cached_input_tokens": tokens[1],
        "output_tokens": tokens[2],
        "weekly_limit_percent": percent,
        "data_status": "available",
        "usage_source": "included_plan",
        "data_as_of": "2026-10-30T12:00:00Z",
        "completed_at": "2026-10-30T11:00:00Z",
        "token_source": "upstream_reported",
    }
    row.update(overrides)
    return row


def dataset():
    training_tokens = [
        (100_000, 10_000, 5_000), (20_000, 150_000, 7_000),
        (30_000, 12_000, 90_000), (110_000, 60_000, 20_000),
        (55_000, 200_000, 45_000), (250_000, 30_000, 70_000),
        (70_000, 80_000, 160_000), (180_000, 140_000, 30_000),
    ]
    validation_tokens = [
        (90_000, 35_000, 20_000), (45_000, 100_000, 80_000),
        (210_000, 50_000, 40_000), (60_000, 160_000, 110_000),
    ]
    return {
        "training": [sample(i, value) for i, value in enumerate(training_tokens)],
        "validation": [sample(100 + i, value) for i, value in enumerate(validation_tokens)],
    }


class CalibrationTests(unittest.TestCase):
    def assert_rejected(self, payload, message=None):
        with self.assertRaises(calibrate_usage.CalibrationError) as caught:
            calibrate_usage.calibrate(payload)
        if message:
            self.assertIn(message, str(caught.exception))

    def test_known_weights_and_independent_validation(self):
        result = calibrate_usage.calibrate(dataset())
        coefficients = list(result["coefficients"].values())
        for actual, expected in zip(coefficients, WEIGHTS):
            self.assertAlmostEqual(actual, expected, places=9)
        self.assertAlmostEqual(result["validation"]["mae_percentage_points"], 0.0, places=10)
        self.assertAlmostEqual(result["validation"]["max_error_percentage_points"], 0.0, places=10)
        self.assertEqual(result["label"], "experimental_estimate")
        self.assertEqual(result["billing_status"], "not_billing")
        self.assertEqual(result["profile_schema_version"], 1)
        expected_hash = hashlib.sha256(
            b"account-plan-rule-epoch-a"
        ).hexdigest()
        self.assertEqual(result["calibration_scope_hash"], expected_hash)
        self.assertNotIn("account-plan-rule-epoch-a", json.dumps(result))

    def test_scope_hash_is_stable_and_distinguishes_scopes(self):
        first_payload = dataset()
        repeated_payload = dataset()
        other_payload = dataset()
        for row in other_payload["training"] + other_payload["validation"]:
            row["calibration_scope"] = "account-plan-rule-epoch-b"
        first = calibrate_usage.calibrate(first_payload)
        repeated = calibrate_usage.calibrate(repeated_payload)
        other = calibrate_usage.calibrate(other_payload)
        self.assertEqual(
            first["calibration_scope_hash"], repeated["calibration_scope_hash"]
        )
        self.assertNotEqual(
            first["calibration_scope_hash"], other["calibration_scope_hash"]
        )
        serialized = json.dumps((first, repeated, other))
        self.assertNotIn("account-plan-rule-epoch-a", serialized)
        self.assertNotIn("account-plan-rule-epoch-b", serialized)

    def test_holdout_error_is_measured_in_percentage_points(self):
        payload = dataset()
        for row in payload["validation"]:
            row["weekly_limit_percent"] += 0.25
        result = calibrate_usage.calibrate(payload)
        self.assertAlmostEqual(result["validation"]["mae_percentage_points"], 0.25)
        self.assertAlmostEqual(result["validation"]["max_error_percentage_points"], 0.25)

    def test_percent_may_exceed_one_hundred(self):
        payload = dataset()
        for row in payload["training"] + payload["validation"]:
            row["weekly_limit_percent"] *= 200
        self.assertTrue(any(row["weekly_limit_percent"] > 100 for row in payload["training"]))
        result = calibrate_usage.calibrate(payload)
        self.assertEqual(result["sample_counts"], {"training": 8, "validation": 4})

    def test_rank_deficient_is_rejected(self):
        payload = dataset()
        for row in payload["training"]:
            row["cached_input_tokens"] = row["uncached_input_tokens"] * 2
            row["output_tokens"] = row["uncached_input_tokens"] * 3
        self.assert_rejected(payload, "rank deficient")

    def test_missing_and_negative_cache_are_rejected(self):
        for replacement in (None, -1):
            payload = dataset()
            if replacement is None:
                payload["training"][0].pop("cached_input_tokens")
            else:
                payload["training"][0]["cached_input_tokens"] = replacement
            self.assert_rejected(payload, "cached_input_tokens")

    def test_token_counts_are_bounded_exact_integers(self):
        for invalid in (True, 1.5, 2**53 + 1):
            with self.subTest(invalid=invalid):
                payload = dataset()
                payload["training"][0]["uncached_input_tokens"] = invalid
                self.assert_rejected(payload, "uncached_input_tokens")

    def test_percentage_must_be_finite_without_conversion_overflow(self):
        for invalid in (True, -0.1, float("inf"), 10**400):
            with self.subTest(invalid=invalid):
                payload = dataset()
                payload["training"][0]["weekly_limit_percent"] = invalid
                self.assert_rejected(payload, "weekly_limit_percent")

    def test_duplicate_and_train_validation_leakage_are_rejected(self):
        for location in ("training", "validation"):
            payload = dataset()
            payload[location][-1]["task_id"] = payload["training"][0]["task_id"]
            self.assert_rejected(payload, "unique")

    def test_partial_stale_and_mixed_strata_are_rejected(self):
        cases = [
            ("data_status", "partial", "data_status"),
            ("data_as_of", "2026-10-30T10:00:00Z", "does not cover"),
            ("model", "other-model", "one model"),
            ("reasoning_effort", "high", "one model"),
            ("service_tier", "priority", "one model"),
            ("calibration_scope", "other-account-plan-epoch", "calibration-scope"),
        ]
        for field, value, message in cases:
            with self.subTest(field=field):
                payload = dataset()
                payload["validation"][0][field] = value
                self.assert_rejected(payload, message)

    def test_negative_fitted_coefficient_is_rejected(self):
        payload = dataset()
        for row in payload["training"]:
            x = (row["uncached_input_tokens"], row["cached_input_tokens"], row["output_tokens"])
            row["weekly_limit_percent"] = (10 * x[0] - 0.1 * x[1] + 10 * x[2]) / 1_000_000
        self.assert_rejected(payload, "negative coefficient")


if __name__ == "__main__":
    unittest.main(verbosity=2)
