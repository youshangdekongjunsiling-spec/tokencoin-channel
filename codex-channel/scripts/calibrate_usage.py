"""Offline experimental calibration of reported tokens to weekly-limit percentage.

This is an estimator, not a billing formula or an enforcement mechanism.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path


FEATURES = (
    "uncached_input_tokens",
    "cached_input_tokens",
    "output_tokens",
)
MIN_TRAINING = 8
MIN_VALIDATION = 4
MAX_EXACT_TOKEN_COUNT = 2**53


class CalibrationError(ValueError):
    """The supplied dataset cannot support the requested calibration."""


def _percentage(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalibrationError(f"{field} must be a finite non-negative number")
    try:
        value = float(value)
    except OverflowError as exc:
        raise CalibrationError(
            f"{field} must be a finite non-negative number"
        ) from exc
    if not math.isfinite(value) or value < 0:
        raise CalibrationError(f"{field} must be a finite non-negative number")
    return value


def _token_count(value, field):
    if isinstance(value, bool) or not isinstance(value, int):
        raise CalibrationError(f"{field} must be a non-negative integer")
    if value < 0 or value > MAX_EXACT_TOKEN_COUNT:
        raise CalibrationError(
            f"{field} must be between 0 and {MAX_EXACT_TOKEN_COUNT}"
        )
    return value


def _timestamp(value, field):
    if not isinstance(value, str):
        raise CalibrationError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CalibrationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CalibrationError(f"{field} must include a timezone")
    return parsed


def _sample(row):
    if not isinstance(row, dict):
        raise CalibrationError("each sample must be an object")
    required_text = (
        "task_id",
        "model",
        "reasoning_effort",
        "service_tier",
        "calibration_scope",
    )
    values = {}
    for field in required_text:
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            raise CalibrationError(f"{field} must be a non-empty string")
        values[field] = value
    if row.get("data_status") != "available":
        raise CalibrationError("data_status must be available")
    if row.get("usage_source") != "included_plan":
        raise CalibrationError("usage_source must be included_plan")
    if row.get("token_source") != "upstream_reported":
        raise CalibrationError("token_source must be upstream_reported")
    completed = _timestamp(row.get("completed_at"), "completed_at")
    data_as_of = _timestamp(row.get("data_as_of"), "data_as_of")
    if data_as_of < completed:
        raise CalibrationError("data_as_of does not cover task completion")
    values["x"] = [
        _token_count(row.get(field), field) / 1_000_000.0 for field in FEATURES
    ]
    values["y"] = _percentage(
        row.get("weekly_limit_percent"), "weekly_limit_percent"
    )
    return values


def _solve_3x3(matrix, vector):
    augmented = [list(matrix[i]) + [vector[i]] for i in range(3)]
    tolerance = 1e-12
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= tolerance:
            raise CalibrationError("training feature matrix is rank deficient")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        for j in range(column, 4):
            augmented[column][j] /= divisor
        for row in range(3):
            if row == column:
                continue
            factor = augmented[row][column]
            for j in range(column, 4):
                augmented[row][j] -= factor * augmented[column][j]
    return [augmented[i][3] for i in range(3)]


def _fit(rows):
    scales = [math.sqrt(sum(row["x"][j] ** 2 for row in rows)) for j in range(3)]
    if any(scale == 0 for scale in scales):
        raise CalibrationError("training feature matrix is rank deficient")
    normalized = [[row["x"][j] / scales[j] for j in range(3)] for row in rows]
    gram = [[sum(x[i] * x[j] for x in normalized) for j in range(3)] for i in range(3)]
    rhs = [sum(x[i] * row["y"] for x, row in zip(normalized, rows)) for i in range(3)]
    scaled_coefficients = _solve_3x3(gram, rhs)
    coefficients = [scaled_coefficients[j] / scales[j] for j in range(3)]
    if any(not math.isfinite(value) or value < 0 for value in coefficients):
        raise CalibrationError("unconstrained fit produced a negative coefficient")
    return coefficients


def calibrate(payload):
    if not isinstance(payload, dict):
        raise CalibrationError("input must contain training and validation arrays")
    training_raw = payload.get("training")
    validation_raw = payload.get("validation")
    if not isinstance(training_raw, list) or len(training_raw) < MIN_TRAINING:
        raise CalibrationError(f"training requires at least {MIN_TRAINING} samples")
    if not isinstance(validation_raw, list) or len(validation_raw) < MIN_VALIDATION:
        raise CalibrationError(f"validation requires at least {MIN_VALIDATION} samples")
    training = [_sample(row) for row in training_raw]
    validation = [_sample(row) for row in validation_raw]
    ids = [row["task_id"] for row in training + validation]
    if len(ids) != len(set(ids)):
        raise CalibrationError("task IDs must be unique across training and validation")
    strata = {
        (
            row["model"],
            row["reasoning_effort"],
            row["service_tier"],
            row["calibration_scope"],
        )
        for row in training + validation
    }
    if len(strata) != 1:
        raise CalibrationError(
            "all samples must use one model/reasoning-effort/service-tier/calibration-scope stratum"
        )
    coefficients = _fit(training)
    errors = [
        abs(sum(value * coefficient for value, coefficient in zip(row["x"], coefficients)) - row["y"])
        for row in validation
    ]
    model, effort, tier, scope = next(iter(strata))
    return {
        "profile_schema_version": 1,
        "label": "experimental_estimate",
        "billing_status": "not_billing",
        "calibration_scope_hash": hashlib.sha256(scope.encode("utf-8")).hexdigest(),
        "stratum": {"model": model, "reasoning_effort": effort, "service_tier": tier},
        "coefficients": {
            f"weekly_limit_percentage_points_per_million_{feature}": coefficient
            for feature, coefficient in zip(FEATURES, coefficients)
        },
        "validation": {
            "mae_percentage_points": sum(errors) / len(errors),
            "max_error_percentage_points": max(errors),
        },
        "sample_counts": {"training": len(training), "validation": len(validation)},
        "limitations": [
            "No official quota formula was obtained.",
            "This estimate is not billing data or a hard limit and does not change channel budgets.",
            "Reasoning tokens are already included in output_tokens.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.samples.read_text(encoding="utf-8"))
        result = calibrate(payload)
    except (OSError, json.JSONDecodeError, CalibrationError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
