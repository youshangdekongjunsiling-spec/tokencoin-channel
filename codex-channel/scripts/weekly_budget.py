"""Experimental weekly-budget ledger with strict per-channel task attribution.

Estimates are empirical references, not billing data, confidence intervals, or
hard bounds.  Official task usage may replace an estimate only when its snapshot
covers every locally known request for that supplier-owned task.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
import time


COEFFICIENT_KEYS = (
    "weekly_limit_percentage_points_per_million_uncached_input_tokens",
    "weekly_limit_percentage_points_per_million_cached_input_tokens",
    "weekly_limit_percentage_points_per_million_output_tokens",
)
TOKEN_KEYS = ("uncached_input_tokens", "cached_input_tokens", "output_tokens")
PROFILE_WARNING = (
    "Empirical reference only; not billing data, a confidence interval, or a hard bound."
)
METHOD = "linear_token_estimate_with_covered_task_official_override"
MAX_EXACT_TOKEN_COUNT = 2**53


class BudgetError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def initialize(c):
    # executescript implicitly commits caller-owned transactions; keep DDL composable.
    statements = (
        """CREATE TABLE IF NOT EXISTS weekly_budget_profiles(
            profile_id TEXT PRIMARY KEY,
            profile_json TEXT NOT NULL,
            installed_at REAL NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS weekly_budget_state(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            active_profile_id TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS weekly_budget_grants(
            channel_id TEXT PRIMARY KEY,
            budget_percent REAL NOT NULL,
            profile_id TEXT NOT NULL,
            profile_json TEXT NOT NULL,
            created_at REAL NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS weekly_budget_official_tasks(
            channel_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            weekly_percent REAL NOT NULL,
            data_as_of REAL NOT NULL,
            covered_request_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(channel_id,task_id)
        )""",
    )
    for statement in statements:
        c.execute(statement)
    columns = {row[1] for row in c.execute("PRAGMA table_info(weekly_budget_official_tasks)")}
    if "covered_request_count" not in columns:
        # Existing snapshots fail closed until they are reconciled again.
        c.execute(
            """ALTER TABLE weekly_budget_official_tasks
               ADD COLUMN covered_request_count INTEGER NOT NULL DEFAULT 0"""
        )


def _number(value, name, *, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BudgetError("invalid_" + name)
    try:
        value = float(value)
    except OverflowError as exc:
        raise BudgetError("invalid_" + name) from exc
    if not math.isfinite(value) or value < 0 or (maximum is not None and value > maximum):
        raise BudgetError("invalid_" + name)
    return value


def _text(value, name):
    if not isinstance(value, str) or not value or len(value) > 128:
        raise BudgetError("invalid_" + name)
    return value


def _count(value, name, minimum):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BudgetError("invalid_" + name)
    return value


def _normalize_profile(profile, now=None):
    if not isinstance(profile, dict):
        raise BudgetError("invalid_profile")
    if profile.get("profile_schema_version") != 1:
        raise BudgetError("invalid_profile_schema_version")
    if profile.get("label") != "experimental_estimate":
        raise BudgetError("invalid_profile_label")
    if profile.get("billing_status") != "not_billing":
        raise BudgetError("invalid_billing_status")
    stratum = profile.get("stratum")
    if not isinstance(stratum, dict):
        raise BudgetError("invalid_stratum")
    normalized_stratum = {
        "model": _text(stratum.get("model"), "model"),
        "reasoning_effort": _text(stratum.get("reasoning_effort"), "reasoning_effort"),
        "service_tier": _text(stratum.get("service_tier"), "service_tier"),
    }
    coefficients = profile.get("coefficients")
    if not isinstance(coefficients, dict) or set(coefficients) != set(COEFFICIENT_KEYS):
        raise BudgetError("invalid_coefficients")
    normalized_coefficients = {
        key: _number(coefficients[key], "coefficient") for key in COEFFICIENT_KEYS
    }
    validation = profile.get("validation")
    if not isinstance(validation, dict):
        raise BudgetError("invalid_validation")
    mae = _number(validation.get("mae_percentage_points"), "validation_mae")
    maximum = _number(validation.get("max_error_percentage_points"), "validation_max_error")
    if maximum < mae:
        raise BudgetError("invalid_validation_order")
    counts = profile.get("sample_counts")
    if not isinstance(counts, dict):
        raise BudgetError("invalid_sample_counts")
    normalized_counts = {
        "training": _count(counts.get("training"), "training_count", 8),
        "validation": _count(counts.get("validation"), "validation_count", 4),
    }
    scope = profile.get("calibration_scope_hash")
    if not isinstance(scope, str) or re.fullmatch(r"[0-9a-fA-F]{64}", scope) is None:
        raise BudgetError("invalid_calibration_scope_hash")
    validated_until = _number(profile.get("validated_until"), "validated_until")
    current = time.time() if now is None else now
    if validated_until <= current or validated_until > current + 8 * 24 * 60 * 60:
        raise BudgetError("invalid_validated_until")
    limitations = profile.get("limitations", [])
    if not isinstance(limitations, list) or not all(
        isinstance(item, str) and len(item) <= 512 for item in limitations
    ):
        raise BudgetError("invalid_limitations")
    return {
        "profile_schema_version": 1,
        "label": "experimental_estimate",
        "billing_status": "not_billing",
        "stratum": normalized_stratum,
        "coefficients": normalized_coefficients,
        "validation": {
            "mae_percentage_points": mae,
            "max_error_percentage_points": maximum,
        },
        "sample_counts": normalized_counts,
        "calibration_scope_hash": scope.lower(),
        "validated_until": validated_until,
        "limitations": list(limitations),
    }


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _profile_public(profile_id, profile):
    return {
        "profile_id": profile_id,
        **profile,
        "valid_until": profile["validated_until"],
        "profile_warning": PROFILE_WARNING,
    }


def install_profile(c, profile):
    """Install and activate a profile inside the caller-owned transaction."""
    initialize(c)
    normalized = _normalize_profile(profile)
    encoded = _canonical(normalized)
    profile_id = hashlib.sha256(encoded.encode("ascii")).hexdigest()
    c.execute(
        "INSERT OR IGNORE INTO weekly_budget_profiles VALUES(?,?,?)",
        (profile_id, encoded, time.time()),
    )
    c.execute(
        """INSERT INTO weekly_budget_state(singleton,active_profile_id) VALUES(1,?)
           ON CONFLICT(singleton) DO UPDATE SET active_profile_id=excluded.active_profile_id""",
        (profile_id,),
    )
    return _profile_public(profile_id, normalized)


def active_profile(c):
    initialize(c)
    row = c.execute(
        """SELECT p.profile_id,p.profile_json FROM weekly_budget_state s
           JOIN weekly_budget_profiles p ON p.profile_id=s.active_profile_id
           WHERE s.singleton=1"""
    ).fetchone()
    if row is None:
        return None
    return _profile_public(row[0], json.loads(row[1]))


def create_grant(c, channel_id, percent):
    """Create one immutable grant; the caller owns commit/rollback."""
    initialize(c)
    channel_id = _text(channel_id, "channel_id")
    percent = _number(percent, "budget_percent", maximum=100)
    if percent <= 0:
        raise BudgetError("invalid_budget_percent")
    profile = active_profile(c)
    if profile is None:
        raise BudgetError("profile_required")
    if time.time() >= profile["validated_until"]:
        raise BudgetError("profile_expired")
    if c.execute("SELECT 1 FROM channels WHERE id=?", (channel_id,)).fetchone() is None:
        raise BudgetError("channel_not_found")
    if c.execute("SELECT 1 FROM requests WHERE channel_id=? LIMIT 1", (channel_id,)).fetchone():
        raise BudgetError("channel_has_requests")
    encoded = _canonical({key: value for key, value in profile.items()
                          if key not in ("profile_id", "valid_until", "profile_warning")})
    try:
        c.execute(
            "INSERT INTO weekly_budget_grants VALUES(?,?,?,?,?)",
            (channel_id, percent, profile["profile_id"], encoded, time.time()),
        )
    except sqlite3.IntegrityError as exc:
        raise BudgetError("grant_already_exists") from exc
    return summary(c, channel_id)


def _grant(c, channel_id):
    row = c.execute(
        "SELECT budget_percent,profile_id,profile_json FROM weekly_budget_grants WHERE channel_id=?",
        (channel_id,),
    ).fetchone()
    if row is None:
        return None
    return float(row[0]), row[1], json.loads(row[2])


def _usage(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _known_usage(usage, stratum):
    if usage is None or usage.get("source") != "upstream_reported":
        return None
    if usage.get("total_consistent") is False or usage.get("invalid_fields"):
        return None
    expected = {
        "requested_model": stratum["model"],
        "requested_reasoning_effort": stratum["reasoning_effort"],
        "requested_service_tier": stratum["service_tier"],
        "reported_model": stratum["model"],
        "reported_service_tier": stratum["service_tier"],
    }
    if any(usage.get(key) != value for key, value in expected.items()):
        return None
    values = []
    for key in TOKEN_KEYS:
        value = usage.get(key)
        if (isinstance(value, bool) or not isinstance(value, int) or value < 0
                or value > MAX_EXACT_TOKEN_COUNT):
            return None
        values.append(value)
    input_tokens = usage.get("input_tokens")
    if (isinstance(input_tokens, bool) or not isinstance(input_tokens, int)
            or input_tokens < 0 or values[0] + values[1] != input_tokens):
        return None
    return values


def _estimate(tokens, profile):
    value = sum(
        tokens[index] / 1_000_000.0 * profile["coefficients"][key]
        for index, key in enumerate(COEFFICIENT_KEYS)
    )
    return value if math.isfinite(value) else None


def summary(c, channel_id, now=None):
    initialize(c)
    grant = _grant(c, channel_id)
    if grant is None:
        return None
    budget, profile_id, profile = grant
    current = time.time() if now is None else now
    rows = c.execute(
        """SELECT r.id,r.status,r.ended,r.usage,a.upstream_task_id
           FROM requests r LEFT JOIN task_attribution_requests a ON a.request_id=r.id
           WHERE r.channel_id=? ORDER BY r.started,r.id""",
        (channel_id,),
    ).fetchall()
    tasks = {}
    missing = []
    running_count = 0
    for row in rows:
        record = {"id": row[0], "status": row[1], "ended": row[2],
                  "usage": _usage(row[3]), "task_id": row[4]}
        if record["status"] == "running":
            running_count += 1
        if record["task_id"] is None:
            missing.append(record)
        else:
            tasks.setdefault(record["task_id"], []).append(record)

    estimated = 0.0
    official_total = 0.0
    official_task_count = 0
    error_total = 0.0
    known_count = 0
    unknown_count = len([row for row in missing if row["status"] != "running"])
    official_rows = {
        row[0]: (float(row[1]), float(row[2]), int(row[3]))
        for row in c.execute(
            """SELECT task_id,weekly_percent,data_as_of,covered_request_count
               FROM weekly_budget_official_tasks
               WHERE channel_id=?""",
            (channel_id,),
        )
    }
    for task_id, records in tasks.items():
        non_running = [record for record in records if record["status"] != "running"]
        has_running = len(non_running) != len(records)
        ends = [record["ended"] for record in non_running]
        official = official_rows.get(task_id)
        covered = (
            official is not None and not has_running and non_running
            and len(records) == official[2]
            and all(isinstance(value, (int, float)) for value in ends)
            and official[1] >= max(float(value) for value in ends)
        )
        if covered:
            estimated += official[0]
            official_total += official[0]
            official_task_count += 1
            known_count += len(non_running)
            continue
        # A stale cumulative snapshot may already include part of a later turn.
        # Never combine it with local estimates for the same task.
        if official is not None:
            unknown_count += len(non_running)
            continue
        if has_running:
            pass
        token_rows = []
        if not has_running:
            for record in non_running:
                if record["status"] != "completed_upstream":
                    token_rows = []
                    break
                tokens = _known_usage(record["usage"], profile["stratum"])
                if tokens is None:
                    token_rows = []
                    break
                token_rows.append(tokens)
        if token_rows and len(token_rows) == len(non_running):
            totals = [sum(values[index] for values in token_rows) for index in range(3)]
            task_estimate = _estimate(totals, profile)
            if task_estimate is None:
                unknown_count += len(non_running)
            else:
                estimated += task_estimate
                error_total += profile["validation"]["max_error_percentage_points"]
                known_count += len(non_running)
        else:
            unknown_count += len(non_running)

    low = official_total + max(0.0, (estimated - official_total) - error_total)
    high = estimated + error_total
    blocked_reason = None
    if current >= profile["validated_until"]:
        blocked_reason = "profile_expired"
    elif running_count:
        blocked_reason = "running"
    elif unknown_count:
        blocked_reason = "pending_unknown"
    elif high >= budget:
        blocked_reason = "estimated_budget_reached"
    totals_unknown = bool(unknown_count or running_count)
    return {
        "mode": "estimated_weekly",
        "budget_percent": budget,
        "known_used_percent": estimated,
        "estimated_used_percent": None if totals_unknown else estimated,
        "reference_low_percent": None if totals_unknown else low,
        "reference_high_percent": None if totals_unknown else high,
        "official_used_percent": official_total,
        "official_task_count": official_task_count,
        "uncalibrated": bool(unknown_count),
        "blocked_reason": blocked_reason,
        "known_request_count": known_count,
        "unknown_request_count": unknown_count,
        "running_request_count": running_count,
        "profile_id": profile_id,
        "valid_until": profile["validated_until"],
        "method": METHOD,
        "profile_warning": PROFILE_WARNING,
    }


def check(c, channel_id, body=None, now=None):
    initialize(c)
    grant = _grant(c, channel_id)
    if grant is None:
        return None
    _budget, _profile_id, profile = grant
    if body is not None:
        if not isinstance(body, dict):
            raise BudgetError("profile_mismatch")
        reasoning = body.get("reasoning")
        actual = {
            "model": body.get("model"),
            "reasoning_effort": reasoning.get("effort") if isinstance(reasoning, dict) else None,
            "service_tier": body.get("service_tier"),
        }
        if actual != profile["stratum"]:
            raise BudgetError("profile_mismatch")
    result = summary(c, channel_id, now=now)
    if result["blocked_reason"] is not None:
        raise BudgetError(result["blocked_reason"])
    return None


def _timestamp(value):
    if isinstance(value, bool):
        raise BudgetError("invalid_data_as_of")
    if isinstance(value, (int, float)):
        try:
            value = float(value)
        except OverflowError as exc:
            raise BudgetError("invalid_data_as_of") from exc
        if math.isfinite(value) and value >= 0:
            return value
        raise BudgetError("invalid_data_as_of")
    if not isinstance(value, str):
        raise BudgetError("invalid_data_as_of")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BudgetError("invalid_data_as_of") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BudgetError("invalid_data_as_of")
    try:
        return parsed.timestamp()
    except (OverflowError, OSError) as exc:
        raise BudgetError("invalid_data_as_of") from exc


def reconcile(c, channel_id, official_payload):
    """Apply one all-or-none batch inside the caller transaction.

    Concurrent management writers should acquire ``BEGIN IMMEDIATE`` before
    calling, then commit only after this function succeeds.
    """
    initialize(c)
    grant = _grant(c, channel_id)
    if grant is None:
        raise BudgetError("grant_required")
    _budget, _profile_id, profile = grant
    if not isinstance(official_payload, dict):
        raise BudgetError("invalid_official_payload")
    supplied_scope = official_payload.get("calibration_scope_hash")
    if (not isinstance(supplied_scope, str)
            or supplied_scope.lower() != profile["calibration_scope_hash"]):
        raise BudgetError("calibration_scope_mismatch")
    threads = official_payload.get("threads")
    if not isinstance(threads, list):
        raise BudgetError("invalid_official_payload")
    seen = set()
    parsed = []
    ignored_partial = 0
    for item in threads:
        if not isinstance(item, dict):
            raise BudgetError("invalid_official_task")
        task_id = item.get("thread_id")
        if not isinstance(task_id, str) or not task_id or task_id in seen:
            raise BudgetError("duplicate_or_invalid_task")
        seen.add(task_id)
        owners = c.execute(
            """SELECT DISTINCT r.channel_id FROM task_attribution_requests a
               JOIN requests r ON r.id=a.request_id WHERE a.upstream_task_id=?""",
            (task_id,),
        ).fetchall()
        if len(owners) != 1 or owners[0][0] != channel_id:
            raise BudgetError("foreign_or_unknown_task")
        status = item.get("data_status")
        if status != "available":
            if status == "partial":
                ignored_partial += 1
                continue
            raise BudgetError("official_task_unavailable")
        if item.get("usage_source") != "included_plan":
            raise BudgetError("unsupported_usage_source")
        percent = _number(item.get("weekly_limit_percent"), "official_weekly_percent")
        parsed.append((task_id, percent))
    data_as_of = _timestamp(official_payload.get("data_as_of")) if parsed else None
    if data_as_of is not None and data_as_of > time.time() + 5:
        raise BudgetError("official_data_as_of_in_future")
    c.execute("SAVEPOINT weekly_budget_reconcile")
    try:
        for task_id, percent in parsed:
            request_rows = c.execute(
                """SELECT r.status,r.ended FROM task_attribution_requests a
                   JOIN requests r ON r.id=a.request_id
                   WHERE a.upstream_task_id=? AND r.channel_id=?""",
                (task_id, channel_id),
            ).fetchall()
            if (not request_rows or any(row[0] == "running" or row[1] is None for row in request_rows)
                    or data_as_of < max(float(row[1]) for row in request_rows)):
                raise BudgetError("official_snapshot_does_not_cover_task")
            previous = c.execute(
                """SELECT weekly_percent,data_as_of FROM weekly_budget_official_tasks
                   WHERE channel_id=? AND task_id=?""",
                (channel_id, task_id),
            ).fetchone()
            if previous is not None:
                old_percent, old_time = float(previous[0]), float(previous[1])
                if data_as_of < old_time or percent < old_percent:
                    raise BudgetError("official_data_regressed")
                if data_as_of == old_time and percent != old_percent:
                    raise BudgetError("official_data_conflict")
            c.execute(
                """INSERT INTO weekly_budget_official_tasks
                   (channel_id,task_id,weekly_percent,data_as_of,covered_request_count)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(channel_id,task_id) DO UPDATE SET
                   weekly_percent=excluded.weekly_percent,
                   data_as_of=excluded.data_as_of,
                   covered_request_count=excluded.covered_request_count""",
                (channel_id, task_id, percent, data_as_of, len(request_rows)),
            )
        c.execute("RELEASE SAVEPOINT weekly_budget_reconcile")
    except BaseException:
        c.execute("ROLLBACK TO SAVEPOINT weekly_budget_reconcile")
        c.execute("RELEASE SAVEPOINT weekly_budget_reconcile")
        raise
    result = summary(c, channel_id)
    result["reconciled_task_count"] = len(parsed)
    result["ignored_partial_task_count"] = ignored_partial
    return result
