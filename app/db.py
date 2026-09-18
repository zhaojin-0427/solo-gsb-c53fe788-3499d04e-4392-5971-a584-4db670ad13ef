"""SQLite access layer.

A single connection is shared by the whole app. SQLite serializes statements
internally; aiosqlite exposes them through one background thread, so concurrent
coroutines are safe. Multistatement writes that must be atomic use the
``transaction`` context manager.
"""
from __future__ import annotations

import aiosqlite
from fastapi import Request

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS scenarios (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    target_url      TEXT NOT NULL,
    body            TEXT NOT NULL DEFAULT '{}',
    secret          TEXT NOT NULL DEFAULT '',
    backoff_json    TEXT NOT NULL DEFAULT '{}',
    rules_json      TEXT NOT NULL DEFAULT '[]',
    max_attempts    INTEGER NOT NULL DEFAULT 5,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id              TEXT PRIMARY KEY,
    scenario_id     TEXT NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
    seed            TEXT NOT NULL,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deliveries_scenario ON deliveries(scenario_id);

CREATE TABLE IF NOT EXISTS branches (
    id                  TEXT PRIMARY KEY,
    delivery_id         TEXT NOT NULL REFERENCES deliveries(id) ON DELETE CASCADE,
    parent_branch_id    TEXT REFERENCES branches(id) ON DELETE CASCADE,
    label               TEXT NOT NULL DEFAULT '',
    -- snapshot of the scenario at branch creation, so later edits never
    -- change an in-flight branch
    target_url          TEXT NOT NULL,
    body                TEXT NOT NULL DEFAULT '{}',
    secret              TEXT NOT NULL DEFAULT '',
    backoff_json        TEXT NOT NULL DEFAULT '{}',
    rules_json          TEXT NOT NULL DEFAULT '[]',
    max_attempts        INTEGER NOT NULL DEFAULT 5,
    next_attempt_no     INTEGER NOT NULL DEFAULT 1,
    last_status         TEXT,
    -- virtual clock
    paused              INTEGER NOT NULL DEFAULT 0,
    speed               REAL NOT NULL DEFAULT 10,
    origin_vtime        INTEGER NOT NULL,
    base_vtime          INTEGER NOT NULL,
    base_wall           REAL NOT NULL,
    next_vtime          INTEGER,
    -- terminal: delivered | exhausted; None while running
    final_state         TEXT,
    created_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_branches_delivery ON branches(delivery_id);

CREATE TABLE IF NOT EXISTS attempts (
    id                  TEXT PRIMARY KEY,
    branch_id           TEXT NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    attempt_no          INTEGER NOT NULL,
    scheduled_at_vtime  INTEGER,
    started_at_vtime    INTEGER NOT NULL,
    idempotency_key     TEXT NOT NULL,
    signature           TEXT NOT NULL,
    action              TEXT NOT NULL,
    status_code         INTEGER,
    outcome             TEXT NOT NULL,
    -- true when the internal inbox had already confirmed this key
    duplicate           INTEGER NOT NULL DEFAULT 0,
    detail              TEXT NOT NULL DEFAULT '',
    is_inherited        INTEGER NOT NULL DEFAULT 0,
    created_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_branch ON attempts(branch_id, attempt_no);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    vtime       INTEGER,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_branch ON events(branch_id, id);

CREATE TABLE IF NOT EXISTS inbox (
    idempotency_key  TEXT PRIMARY KEY,
    branch_id        TEXT NOT NULL,
    scenario_id      TEXT,
    attempt_no       INTEGER NOT NULL,
    received_at_vtime INTEGER NOT NULL,
    payload          TEXT NOT NULL,
    created_at       REAL NOT NULL
);
"""


def get_db(request: Request) -> aiosqlite.Connection:
    return request.app.state.db


async def connect(db_path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.executescript(SCHEMA)
    await conn.commit()
    return conn
