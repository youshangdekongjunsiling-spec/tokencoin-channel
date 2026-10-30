"""Supplier-owned task attribution for Codex Responses requests.

This module only prepares request metadata.  It does not call an upstream service
or interpret subscription usage.  Client hints are opaque continuity hints, not
trusted upstream identifiers.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from pathlib import Path


MAX_HINT_BYTES = 256

_META_KEYS = {
    "installation_id",
    "session_id",
    "thread_id",
    "turn_id",
    "window_id",
    "root_thread_id",
    "parent_thread_id",
    "root_turn_id",
    "parent_turn_id",
    "forked_from_thread_id",
    "forked_from_ordinal_exclusive",
    "x-client-request-id",
    "x-codex-installation-id",
    "x-codex-window-id",
    "x-codex-turn-metadata",
    "x-codex-parent-thread-id",
}
_RESERVED_META_COMPACT = {
    "".join(character for character in value if character.isalnum())
    for value in _META_KEYS
}
_OWNED_BODY_COMPACT = _RESERVED_META_COMPACT | {"promptcachekey", "clientmetadata"}


class AttributionError(ValueError):
    """The reserved request cannot be safely associated with attribution data."""


def _connect(home):
    connection = sqlite3.connect(Path(home) / "state.sqlite3", timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_schema(home):
    """Create additive attribution tables and stable supplier-local identifiers."""
    connection = _connect(home)
    try:
        with connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_attribution_state(
                    key TEXT PRIMARY KEY,
                    value BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_attribution_tasks(
                    channel_id TEXT NOT NULL,
                    hint_hash TEXT NOT NULL,
                    upstream_task_id TEXT NOT NULL UNIQUE,
                    window_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(channel_id, hint_hash)
                );
                CREATE TABLE IF NOT EXISTS task_attribution_requests(
                    request_id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    upstream_task_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    window_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_attribution_requests_task
                    ON task_attribution_requests(upstream_task_id);
                """
            )
            _state_value(connection, "hint_hmac_key", lambda: secrets.token_bytes(32))
            _state_value(connection, "installation_id", lambda: str(uuid.uuid4()).encode())
    finally:
        connection.close()


def _state_value(connection, key, create):
    row = connection.execute(
        "SELECT value FROM task_attribution_state WHERE key=?", (key,)
    ).fetchone()
    if row is not None:
        return bytes(row["value"])
    value = create()
    connection.execute(
        "INSERT OR IGNORE INTO task_attribution_state(key,value) VALUES(?,?)",
        (key, value),
    )
    row = connection.execute(
        "SELECT value FROM task_attribution_state WHERE key=?", (key,)
    ).fetchone()
    return bytes(row["value"])


def _hint_digest(key, channel_id, hint):
    if not isinstance(hint, str):
        raise AttributionError("client thread hint must be a string")
    encoded = hint.encode("utf-8")
    if not encoded or len(encoded) > MAX_HINT_BYTES:
        raise AttributionError("client thread hint must contain 1 to 256 UTF-8 bytes")
    message = channel_id.encode("utf-8") + b"\0" + encoded
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _is_reserved_metadata_key(key):
    lowered = key.lower()
    compact = "".join(character for character in lowered if character.isalnum())
    return (
        lowered in _META_KEYS
        or lowered.startswith("x-codex-")
        or compact in _RESERVED_META_COMPACT
    )


def _prepare_body(body, task_id, turn_id, window_id, installation_id):
    if not isinstance(body, dict):
        raise AttributionError("Responses body must be an object")
    prepared = copy.deepcopy(body)
    for key in list(prepared):
        if isinstance(key, str):
            compact = "".join(
                character for character in key.lower() if character.isalnum()
            )
            if compact in _OWNED_BODY_COMPACT or key.lower().startswith("x-codex-"):
                prepared.pop(key, None)

    supplied_metadata = body.get("client_metadata")
    client_metadata = {}
    if isinstance(supplied_metadata, dict):
        client_metadata = {
            key: copy.deepcopy(value)
            for key, value in supplied_metadata.items()
            if isinstance(key, str) and not _is_reserved_metadata_key(key)
        }

    turn_metadata = {
        "installation_id": installation_id,
        "session_id": task_id,
        "thread_id": task_id,
        "turn_id": turn_id,
        "window_id": window_id,
        "request_kind": "turn",
    }
    encoded_turn_metadata = json.dumps(turn_metadata, separators=(",", ":"), ensure_ascii=True)
    client_metadata.update(
        {
            "x-codex-installation-id": installation_id,
            "session_id": task_id,
            "thread_id": task_id,
            "x-codex-window-id": window_id,
            "turn_id": turn_id,
            "x-codex-turn-metadata": encoded_turn_metadata,
        }
    )
    prepared["prompt_cache_key"] = task_id
    prepared["client_metadata"] = client_metadata
    headers = {
        "x-client-request-id": task_id,
        "session-id": task_id,
        "thread-id": task_id,
        "x-codex-window-id": window_id,
        "x-codex-turn-metadata": encoded_turn_metadata,
    }
    return prepared, headers


def prepare(home, channel_id, request_id, body, client_thread_hint=None):
    """Atomically bind a reserved request to supplier-owned task and turn IDs.

    The returned mapping contains only prepared data: ``body``, ``headers``,
    ``supplier_task_id`` and ``turn_id``.  Callers remain responsible for the
    actual upstream request.
    """
    if not isinstance(channel_id, str) or not channel_id or len(channel_id) > 128:
        raise AttributionError("invalid channel id")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise AttributionError("invalid request id")
    initialize_schema(home)
    connection = _connect(home)
    try:
        connection.execute("BEGIN IMMEDIATE")
        reserved = connection.execute(
            "SELECT channel_id FROM requests WHERE id=?", (request_id,)
        ).fetchone()
        if reserved is None or reserved["channel_id"] != channel_id:
            raise AttributionError("request is not reserved for this channel")

        existing = connection.execute(
            """SELECT upstream_task_id,turn_id,window_id
               FROM task_attribution_requests WHERE request_id=?""",
            (request_id,),
        ).fetchone()
        if existing is not None:
            task_id = existing["upstream_task_id"]
            turn_id = existing["turn_id"]
            window_id = existing["window_id"]
        else:
            task_id = None
            if client_thread_hint is not None:
                key = _state_value(connection, "hint_hmac_key", lambda: secrets.token_bytes(32))
                hint_hash = _hint_digest(key, channel_id, client_thread_hint)
                task = connection.execute(
                    """SELECT upstream_task_id,window_id FROM task_attribution_tasks
                       WHERE channel_id=? AND hint_hash=?""",
                    (channel_id, hint_hash),
                ).fetchone()
                if task is None:
                    task_id = str(uuid.uuid4())
                    window_id = str(uuid.uuid4())
                    connection.execute(
                        """INSERT INTO task_attribution_tasks
                           (channel_id,hint_hash,upstream_task_id,window_id,created_at)
                           VALUES(?,?,?,?,?)""",
                        (channel_id, hint_hash, task_id, window_id, time.time()),
                    )
                else:
                    task_id = task["upstream_task_id"]
                    window_id = task["window_id"]
            if task_id is None:
                task_id = str(uuid.uuid4())
                window_id = str(uuid.uuid4())
            turn_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO task_attribution_requests
                   (request_id,channel_id,upstream_task_id,turn_id,window_id,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (request_id, channel_id, task_id, turn_id, window_id, time.time()),
            )
        installation_id = _state_value(
            connection, "installation_id", lambda: str(uuid.uuid4()).encode()
        ).decode("ascii")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    prepared_body, headers = _prepare_body(
        body, task_id, turn_id, window_id, installation_id
    )
    return {
        "body": prepared_body,
        "headers": headers,
        "supplier_task_id": task_id,
        "turn_id": turn_id,
    }
