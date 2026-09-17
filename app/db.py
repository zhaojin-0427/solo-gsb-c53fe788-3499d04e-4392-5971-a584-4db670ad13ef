"""SQLite 持久化层。

所有运行状态（场景、演练、尝试、事件、投递确认、副作用）都落盘，
刷新页面 / 容器重启后调度器据此恢复，不重复已确认的投递。
"""
import json
import sqlite3
import time
from collections.abc import Iterable
from typing import Any

from . import config


# ---------------------------------------------------------------- 连接 / 初始化

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS scenarios (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    data       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id               TEXT PRIMARY KEY,
    root_run_id      TEXT NOT NULL,
    parent_run_id    TEXT,
    lineage          TEXT NOT NULL DEFAULT '[]',
    gen              INTEGER NOT NULL DEFAULT 0,
    scenario_id      TEXT NOT NULL,
    name             TEXT NOT NULL,
    label            TEXT,
    snapshot         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'running',
    vt_now           INTEGER NOT NULL,
    anchor_ms        INTEGER,
    speed            REAL NOT NULL DEFAULT 10,
    next_attempt     INTEGER NOT NULL DEFAULT 1,
    next_planned_ms  INTEGER,
    outcome          TEXT,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_root ON runs(root_run_id);
CREATE INDEX IF NOT EXISTS idx_rins_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_scenario ON runs(scenario_id);

CREATE TABLE IF NOT EXISTS attempts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    attempt        INTEGER NOT NULL,
    idem_key       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'scheduled',   -- scheduled/running/done
    result         TEXT NOT NULL DEFAULT 'pending',    -- pending/success/failure/timeout/disconnect/network_error
    action         TEXT,
    planned_status INTEGER,
    planned_delay  INTEGER NOT NULL DEFAULT 0,
    response_status INTEGER,
    sig_valid      INTEGER,
    replay         INTEGER NOT NULL DEFAULT 0,
    side_effect_id INTEGER,
    error          TEXT,
    scheduled_ms   INTEGER NOT NULL,
    started_ms     INTEGER,
    finished_ms    INTEGER,
    real_duration_ms INTEGER,
    inherited      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(run_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_attempts_run ON attempts(run_id);
CREATE INDEX IF NOT EXISTS idx_attempts_key ON attempts(idem_key);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    data      TEXT NOT NULL DEFAULT '{}',
    at_ms     INTEGER NOT NULL,
    attempt   INTEGER,
    inherited INTEGER NOT NULL DEFAULT 0,
    UNIQUE(run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);

CREATE TABLE IF NOT EXISTS deliveries (
    idem_key       TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    attempt        INTEGER NOT NULL,
    side_effect_id INTEGER NOT NULL,
    delivered_at   INTEGER NOT NULL,
    UNIQUE(side_effect_id)
);

CREATE TABLE IF NOT EXISTS side_effects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id  TEXT NOT NULL,
    root_run_id  TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    idem_key     TEXT NOT NULL UNIQUE,
    body_hash    TEXT NOT NULL,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_side_root ON side_effects(root_run_id);
"""


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def now_ms() -> int:
    return time.time_ns() // 1_000_000


# ---------------------------------------------------------------- JSON 辅助

def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    return json.loads(value) if value is not None else default


# ---------------------------------------------------------------- 场景

def create_scenario(scenario_id: str, data: dict) -> dict:
    ts = now_ms()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO scenarios (id, name, data, created_at) VALUES (?,?,?,?)",
            (scenario_id, data["name"], _dumps(data), ts),
        )
    return {"id": scenario_id, "created_at": ts, **data}


def update_scenario(scenario_id: str, data: dict) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE scenarios SET name=?, data=? WHERE id=?",
            (data["name"], _dumps(data), scenario_id),
        )
        return cur.rowcount > 0


def delete_scenario(scenario_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM scenarios WHERE id=?", (scenario_id,))
        return cur.rowcount > 0


def get_scenario(conn: sqlite3.Connection, scenario_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM scenarios WHERE id=?", (scenario_id,)).fetchone()
    if not row:
        return None
    return {"id": row["id"], "created_at": row["created_at"], **_loads(row["data"], {})}


def list_scenarios() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM scenarios ORDER BY created_at DESC").fetchall()
        return [
            {"id": r["id"], "created_at": r["created_at"], **_loads(r["data"], {})}
            for r in rows
        ]


# ---------------------------------------------------------------- 事件

def add_event_dict(
    conn: sqlite3.Connection,
    run_id: str,
    kind: str,
    data: dict | None = None,
    at_ms: int | None = None,
    attempt: int | None = None,
    inherited: bool = False,
) -> dict:
    """在给定事务内插入事件并返回可广播的事件描述（不提交）。"""
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO events (run_id, seq, kind, data, at_ms, attempt, inherited)"
        " VALUES (?,?,?,?,?,?,?)",
        (run_id, seq, kind, _dumps(data or {}), at_ms or now_ms(), attempt,
         1 if inherited else 0),
    )
    return {
        "id": cur.lastrowid,
        "run_id": run_id,
        "seq": seq,
        "kind": kind,
        "data": data or {},
        "at_ms": at_ms or now_ms(),
        "attempt": attempt,
        "inherited": inherited,
    }


def add_event(
    conn: sqlite3.Connection,
    run_id: str,
    kind: str,
    data: dict | None = None,
    at_ms: int | None = None,
    attempt: int | None = None,
    inherited: bool = False,
) -> int:
    return add_event_dict(conn, run_id, kind, data, at_ms=at_ms,
                            attempt=attempt, inherited=inherited)["seq"]


# ---------------------------------------------------------------- 演练

def insert_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    root_run_id: str,
    parent_run_id: str | None,
    lineage: list[int],
    gen: int,
    scenario_id: str,
    name: str,
    label: str | None,
    snapshot: dict,
    status: str,
    vt_now: int,
    anchor_ms: int | None,
    speed: float,
    next_attempt: int,
    next_planned_ms: int | None,
    outcome: dict | None = None,
) -> None:
    ts = now_ms()
    conn.execute(
        "INSERT INTO runs (id, root_run_id, parent_run_id, lineage, gen, scenario_id,"
        " name, label, snapshot, status, vt_now, anchor_ms, speed, next_attempt,"
        " next_planned_ms, outcome, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, root_run_id, parent_run_id, _dumps(lineage), gen, scenario_id, name,
         label, _dumps(snapshot), status, vt_now, anchor_ms, speed, next_attempt,
         next_planned_ms, _dumps(outcome) if outcome is not None else None, ts, ts),
    )


def get_run_row(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()


def list_runs(scenario_id: str | None = None, limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        if scenario_id:
            rows = conn.execute(
                "SELECT * FROM runs WHERE scenario_id=? ORDER BY created_at DESC LIMIT ?",
                (scenario_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_run_brief(r) for r in rows]


def list_family(conn: sqlite3.Connection, root_run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM runs WHERE root_run_id=? ORDER BY created_at", (root_run_id,)
    ).fetchall()


def _run_brief(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "root_run_id": row["root_run_id"],
        "parent_run_id": row["parent_run_id"],
        "lineage": _loads(row["lineage"], []),
        "gen": row["gen"],
        "scenario_id": row["scenario_id"],
        "name": row["name"],
        "label": row["label"],
        "status": row["status"],
        "outcome": _loads(row["outcome"], None),
        "next_attempt": row["next_attempt"],
        "created_at": row["created_at"],
    }


def save_clock(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    vt_now: int,
    anchor_ms: int | None,
    speed: float | None = None,
    status: str | None = None,
    next_attempt: int | None = None,
    next_planned_ms: int | None = ...,  # type: ignore[assignment]
    outcome: dict | None = None,
    outcome_reset: bool = False,
) -> None:
    sets = ["vt_now=?", "anchor_ms=?", "updated_at=?"]
    params: list[Any] = [vt_now, anchor_ms, now_ms()]
    if speed is not None:
        sets.append("speed=?")
        params.append(speed)
    if status is not None:
        sets.append("status=?")
        params.append(status)
    if next_attempt is not None:
        sets.append("next_attempt=?")
        params.append(next_attempt)
    if next_planned_ms is not ...:
        sets.append("next_planned_ms=?")
        params.append(next_planned_ms)
    if outcome_reset:
        sets.append("outcome=NULL")
    elif outcome is not None:
        sets.append("outcome=?")
        params.append(_dumps(outcome))
    params.append(run_id)
    conn.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", params)


# ---------------------------------------------------------------- 尝试

def insert_attempt(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    attempt: int,
    idem_key: str,
    action: str,
    planned_status: int | None,
    planned_delay: int,
    scheduled_ms: int,
    inherited: bool = False,
    status: str = "scheduled",
    result: str = "pending",
    response_status: int | None = None,
    sig_valid: int | None = None,
    replay: bool = False,
    side_effect_id: int | None = None,
    error: str | None = None,
    started_ms: int | None = None,
    finished_ms: int | None = None,
    real_duration_ms: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO attempts (run_id, attempt, idem_key, status, result, action,"
        " planned_status, planned_delay, response_status, sig_valid, replay,"
        " side_effect_id, error, scheduled_ms, started_ms, finished_ms,"
        " real_duration_ms, inherited)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, attempt, idem_key, status, result, action, planned_status,
         planned_delay, response_status, None if sig_valid is None else int(sig_valid),
         int(replay), side_effect_id, error, scheduled_ms, started_ms, finished_ms,
         real_duration_ms, int(inherited)),
    )
    return cur.lastrowid


def get_attempt_by_key(conn: sqlite3.Connection, idem_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM attempts WHERE idem_key=?", (idem_key,)).fetchone()


def list_attempts(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM attempts WHERE run_id=? ORDER BY attempt", (run_id,)
    ).fetchall()


def get_pending_attempt(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM attempts WHERE run_id=? AND status!='done' ORDER BY attempt LIMIT 1",
        (run_id,),
    ).fetchone()


def get_running_attempts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM attempts WHERE status='running'").fetchall()


def mark_attempt_running(conn: sqlite3.Connection, attempt_pk: int, started_ms: int) -> None:
    conn.execute(
        "UPDATE attempts SET status='running', result='pending', started_ms=? WHERE id=?",
        (started_ms, attempt_pk),
    )


def complete_attempt(
    conn: sqlite3.Connection,
    attempt_pk: int,
    *,
    result: str,
    response_status: int | None,
    sig_valid: bool | None,
    replay: bool,
    side_effect_id: int | None,
    error: str | None,
    finished_ms: int,
    real_duration_ms: int,
) -> None:
    conn.execute(
        "UPDATE attempts SET status='done', result=?, response_status=?, sig_valid=?,"
        " replay=?, side_effect_id=?, error=?, finished_ms=?, real_duration_ms=? WHERE id=?",
        (result, response_status, None if sig_valid is None else int(sig_valid),
         int(replay), side_effect_id, error, finished_ms, real_duration_ms, attempt_pk),
    )


# ---------------------------------------------------------------- 投递确认 / 副作用

def create_side_effect(
    conn: sqlite3.Connection,
    *,
    scenario_id: str,
    root_run_id: str,
    run_id: str,
    idem_key: str,
    body_hash: str,
) -> int:
    ts = now_ms()
    cur = conn.execute(
        "INSERT INTO side_effects (scenario_id, root_run_id, run_id, idem_key, body_hash, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (scenario_id, root_run_id, run_id, idem_key, body_hash, ts),
    )
    return cur.lastrowid


def get_side_effect_by_key(conn: sqlite3.Connection, idem_key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM side_effects WHERE idem_key=?", (idem_key,)).fetchone()


def record_delivery(
    conn: sqlite3.Connection,
    *,
    idem_key: str,
    run_id: str,
    attempt: int,
    side_effect_id: int,
) -> bool:
    """幂等记录“已确认投递”。返回 True 表示首次确认。"""
    try:
        conn.execute(
            "INSERT INTO deliveries (idem_key, run_id, attempt, side_effect_id, delivered_at)"
            " VALUES (?,?,?,?,?)",
            (idem_key, run_id, attempt, side_effect_id, now_ms()),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def count_family_side_effects(conn: sqlite3.Connection, root_run_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM side_effects WHERE root_run_id=?", (root_run_id,)
    ).fetchone()[0]


def count_family_deliveries(conn: sqlite3.Connection, root_run_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM deliveries d JOIN runs r ON d.run_id=r.id"
        " WHERE r.root_run_id=?", (root_run_id,)
    ).fetchone()[0]


# ---------------------------------------------------------------- 事件读取

def list_events(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)
    ).fetchall()


def max_event_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]


def events_after(conn: sqlite3.Connection, last_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE id>? ORDER BY id LIMIT 500", (last_id,)
    ).fetchall()


# ---------------------------------------------------------------- 序列化

def serialize_attempt(row: sqlite3.Row) -> dict:
    return {
        "attempt": row["attempt"],
        "idempotency_key": row["idem_key"],
        "status": row["status"],
        "result": row["result"],
        "action": row["action"],
        "planned_status": row["planned_status"],
        "planned_delay_ms": row["planned_delay"],
        "response_status": row["response_status"],
        "sig_valid": None if row["sig_valid"] is None else bool(row["sig_valid"]),
        "replay": bool(row["replay"]),
        "side_effect_id": row["side_effect_id"],
        "error": row["error"],
        "scheduled_ms": row["scheduled_ms"],
        "started_ms": row["started_ms"],
        "finished_ms": row["finished_ms"],
        "real_duration_ms": row["real_duration_ms"],
        "inherited": bool(row["inherited"]),
    }


def serialize_event(row: sqlite3.Row) -> dict:
    return {
        "seq": row["seq"],
        "kind": row["kind"],
        "data": _loads(row["data"], {}),
        "at_ms": row["at_ms"],
        "attempt": row["attempt"],
        "inherited": bool(row["inherited"]),
    }


def serialize_run(conn: sqlite3.Connection, row: sqlite3.Row, *, with_timeline: bool = True) -> dict:
    out = _run_brief(row)
    out.update({
        "snapshot": _loads(row["snapshot"], {}),
        "vt_now": row["vt_now"],
        "anchor_ms": row["anchor_ms"],
        "speed": row["speed"],
        "next_attempt": row["next_attempt"],
        "next_planned_ms": row["next_planned_ms"],
        "outcome": _loads(row["outcome"], None),
        "updated_at": row["updated_at"],
    })
    if with_timeline:
        out["attempts"] = [serialize_attempt(a) for a in list_attempts(conn, row["id"])]
        out["events"] = [serialize_event(e) for e in list_events(conn, row["id"])]
        family = list_family(conn, row["root_run_id"])
        out["branches"] = [_run_brief(r) for r in family]
        out["family_side_effects"] = count_family_side_effects(conn, row["root_run_id"])
        out["family_deliveries"] = count_family_deliveries(conn, row["root_run_id"])
    return out
