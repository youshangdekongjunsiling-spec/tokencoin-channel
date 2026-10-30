"""Retain reported inference units for calibration, without inventing quota costs."""
from __future__ import annotations


def request_features(body):
    out = {}
    for name, value in (("requested_model", body.get("model")),
            ("requested_service_tier", body.get("service_tier")),
            ("requested_reasoning_effort", (body.get("reasoning") or {}).get("effort")
             if isinstance(body.get("reasoning"), dict) else None)):
        if isinstance(value, str) and len(value) <= 128:
            out[name] = value
    return out


def reported_usage(response, body):
    out = {"source": "upstream_reported", **request_features(body)}
    if not isinstance(response, dict):
        return out
    reported = response.get("usage")
    if not isinstance(reported, dict):
        return out
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        value = reported.get(name)
        if type(value) is int and value >= 0:
            out[name] = value
    for field, detail, target, parent in (
            ("input_tokens_details", "cached_tokens", "cached_input_tokens", "input_tokens"),
            ("output_tokens_details", "reasoning_tokens", "reasoning_tokens", "output_tokens")):
        details = reported.get(field)
        value = details.get(detail) if isinstance(details, dict) else None
        if type(value) is int and value >= 0 and (parent not in out or value <= out[parent]):
            out[target] = value
        elif value is not None:
            out.setdefault("invalid_fields", []).append(target)
    # Missing cache data means unknown, never an assumed zero-cache request.
    if "input_tokens" in out and "cached_input_tokens" in out:
        out["uncached_input_tokens"] = out["input_tokens"] - out["cached_input_tokens"]
    # Reasoning is already part of output_tokens; do not add it a second time.
    for name in ("model", "service_tier"):
        value = response.get(name)
        if isinstance(value, str) and len(value) <= 128:
            out["reported_" + name] = value
    if all(k in out for k in ("input_tokens", "output_tokens", "total_tokens")):
        out["total_consistent"] = out["input_tokens"] + out["output_tokens"] == out["total_tokens"]
    return out
