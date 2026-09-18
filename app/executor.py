"""Delivery executor: virtual clock, scheduling, attempts and forking."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Optional

import aiosqlite

from . import core
from .bus import Bus


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# virtual clock
# ---------------------------------------------------------------------------

def vtime_now(b: aiosqlite.Row) -> int:
    if b["paused"]:
        return b["base_vtime"]
    elapsed_ms = (time.time() - b["base_wall"]) * 1000.0 * b["speed"]
    return int(b["base_vtime"] + elapsed_ms)


async def _insert_event(conn, branch_id: str, event_type: str, vtime: Optional[int],
                        payload: dict[str, Any]) -> int:
    cur = await conn.execute(
        "INSERT INTO events(branch_id, event_type, vtime, payload, created_at)"
        " VALUES (?,?,?,?,?)",
        (branch_id, event_type, vtime, json.dumps(payload, ensure_ascii=False), time.time()),
    )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# scenarios / deliveries
# ---------------------------------------------------------------------------

async def create_scenario(conn, data) -> str:
    sid = uuid.uuid4().hex[:12]
    await conn.execute(
        "INSERT INTO scenarios(id, name, target_url, body, secret, backoff_json,"
        " rules_json, max_attempts, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, data.name, data.target_url, core_canon(data.body), data.secret,
         json.dumps(data.backoff.normalized()), json.dumps([r.model_dump() for r in data.rules]),
         data.max_attempts, time.time()),
    )
    return sid


def core_canon(value: Any) -> str:
    from .models import canonical_body
    return canonical_body(value)


async def update_scenario(conn, sid: str, data) -> None:
    await conn.execute(
        "UPDATE scenarios SET name=?, target_url=?, body=?, secret=?, backoff_json=?,"
        " rules_json=?, max_attempts=? WHERE id=?",
        (data.name, data.target_url, core_canon(data.body), data.secret,
         json.dumps(data.backoff.normalized()), json.dumps([r.model_dump() for r in data.rules]),
         data.max_attempts, sid),
    )


async def create_delivery(conn, bus: Bus, data) -> str:
    did = uuid.uuid4().hex[:12]
    bid = uuid.uuid4().hex[:12]
    seed = core.delivery_seed(data.scenario_id, time.time(), uuid.uuid4().hex)
    row = await (await conn.execute("SELECT * FROM scenarios WHERE id=?", (data.scenario_id,))).fetchone()
    if row is None:
        raise KeyError("scenario not found")

    origin = now_ms()
    await conn.execute(
        "INSERT INTO deliveries(id, scenario_id, seed, created_at) VALUES (?,?,?,?)",
        (did, data.scenario_id, seed, time.time()),
    )
    await conn.execute(
        "INSERT INTO branches(id, delivery_id, parent_branch_id, label, target_url, body,"
        " secret, backoff_json, rules_json, max_attempts, next_attempt_no, last_status,"
        " paused, speed, origin_vtime, base_vtime, base_wall, next_vtime, final_state, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (bid, did, None, "root", row["target_url"], row["body"], row["secret"],
         row["backoff_json"], row["rules_json"], row["max_attempts"],
         1, None, 1 if data.paused else 0, data.speed,
         origin, origin, time.time(), origin, None, time.time()),
    )
    eid = await _insert_event(conn, bid, "scheduled", origin,
                              {"type": "scheduled", "attempt_no": 1, "vtime": origin})
    await conn.commit()
    bus.publish(eid, "evt", {"branch_id": bid, "delivery_id": did,
                             "type": "scheduled", "attempt_no": 1, "vtime": origin})
    return did


# ---------------------------------------------------------------------------
# branch key material
# ---------------------------------------------------------------------------

async def _key_for_attempt(conn, branch: aiosqlite.Row, attempt_no: int) -> str:
    d = await (await conn.execute("SELECT seed FROM deliveries WHERE id=?",
                                  (branch["delivery_id"],))).fetchone()
    if branch["parent_branch_id"] is None:
        return core.root_idempotency_key(d["seed"], attempt_no)
    fork_seed = core.branch_key_seed(d["seed"], branch["parent_branch_id"])
    return core.forked_idempotency_key(fork_seed, attempt_no)


# ---------------------------------------------------------------------------
# one attempt
# ---------------------------------------------------------------------------

async def _run_attempt(conn, bus: Bus, b: aiosqlite.Row, attempt_no: int,
                       at_vtime: int, scheduled_vtime: Optional[int]) -> dict[str, Any]:
    """Execute one attempt.

    The simulation is instantaneous and the whole state transition is one
    transaction, so a crash can never leave an 'in-flight' attempt behind.
    """
    bid = b["id"]
    body = b["body"]
    secret = b["secret"]
    rules = json.loads(b["rules_json"])
    backoff = json.loads(b["backoff_json"])
    max_attempts = b["max_attempts"]

    key = await _key_for_attempt(conn, b, attempt_no)
    signature = core.sign(secret, key, str(at_vtime), body)
    rule = core.resolve_action(rules, attempt_no)
    action = rule["action"]

    duplicate = False
    status_code: Optional[int] = None
    detail = rule.get("detail", "") or ""
    outcome = "retry"

    if action == "status":
        status_code = int(rule.get("status_code", 200))
        existed = await (await conn.execute(
            "SELECT 1 FROM inbox WHERE idempotency_key=?", (key,))).fetchone()
        duplicate = existed is not None
        if not duplicate:
            await conn.execute(
                "INSERT INTO inbox(idempotency_key, branch_id, scenario_id, attempt_no,"
                " received_at_vtime, payload, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (key, bid, None, attempt_no, at_vtime, body, time.time()),
            )
        if 200 <= status_code < 400:
            outcome = "delivered"
        elif core.is_retriable_status(status_code):
            outcome = "retry"
        else:
            outcome = "failed"
        if not detail:
            detail = f"HTTP {status_code}" + (" (idempotent replay)" if duplicate else "")
    elif action == "timeout":
        outcome = "retry"
        detail = detail or "simulated read timeout"
    else:  # disconnect
        outcome = "retry"
        detail = detail or "simulated connection reset"

    attempt_id = uuid.uuid4().hex
    await conn.execute(
        "INSERT INTO attempts(id, branch_id, attempt_no, scheduled_at_vtime,"
        " started_at_vtime, idempotency_key, signature, action, status_code, outcome,"
        " duplicate, detail, is_inherited, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (attempt_id, bid, attempt_no, scheduled_vtime, at_vtime, key, signature,
         action, status_code, outcome, 1 if duplicate else 0, detail, 0, time.time()),
    )
    attempt_payload = {
        "type": "attempt", "attempt_no": attempt_no, "vtime": at_vtime,
        "scheduled_at_vtime": scheduled_vtime,
        "idempotency_key": key, "signature": signature,
        "action": action, "status_code": status_code, "outcome": outcome,
        "duplicate": duplicate, "detail": detail,
    }
    eid = await _insert_event(conn, bid, "attempt", at_vtime, attempt_payload)

    next_attempt = attempt_no + 1
    terminal_state: Optional[str] = None
    if outcome == "delivered":
        terminal_state = "delivered"
    elif outcome == "failed":
        # non-retriable HTTP status (4xx other than 408/429): stop immediately
        terminal_state = "failed"
    elif next_attempt > max_attempts:
        terminal_state = "exhausted"

    if terminal_state is not None:
        await conn.execute(
            "UPDATE branches SET next_attempt_no=?, last_status=?, next_vtime=NULL,"
            " final_state=?, base_vtime=?, base_wall=? WHERE id=?",
            (next_attempt, str(status_code if status_code is not None else action),
             terminal_state, at_vtime, time.time(), bid),
        )
        term_payload = {"type": "terminal", "state": terminal_state, "vtime": at_vtime,
                        "attempt_no": attempt_no}
        teid = await _insert_event(conn, bid, "terminal", at_vtime, term_payload)
    else:
        delay = core.backoff_delay_ms(backoff, next_attempt, key)
        nv = at_vtime + delay
        await conn.execute(
            "UPDATE branches SET next_attempt_no=?, last_status=?, next_vtime=?,"
            " base_vtime=?, base_wall=? WHERE id=?",
            (next_attempt, str(status_code if status_code is not None else action),
             nv, at_vtime, time.time(), bid),
        )
        sched_payload = {"type": "scheduled", "attempt_no": next_attempt,
                         "vtime": nv, "delay_ms": delay, "after_attempt": attempt_no}
        seid = await _insert_event(conn, bid, "scheduled", nv, sched_payload)

    # fill scenario_id on inbox rows (best effort, same tx)
    await conn.execute(
        "UPDATE inbox SET scenario_id=(SELECT scenario_id FROM deliveries WHERE id=?)"
        " WHERE branch_id=?", (b["delivery_id"], bid))

    await conn.commit()

    did = b["delivery_id"]
    bus.publish(eid, "evt", {"branch_id": bid, "delivery_id": did, **attempt_payload})
    if terminal_state is not None:
        bus.publish(teid, "evt", {"branch_id": bid, "delivery_id": did, **term_payload})
        return {"terminal": terminal_state}
    bus.publish(seid, "evt", {"branch_id": bid, "delivery_id": did, **sched_payload})
    return {"terminal": None, "next_vtime": nv}


# ---------------------------------------------------------------------------
# pump loop
# ---------------------------------------------------------------------------

class Executor:
    """Owns the periodic virtual-clock pump and per-branch locking."""

    def __init__(self, conn: aiosqlite.Connection, bus: Bus,
                 tick_seconds: float = 0.05) -> None:
        self.conn = conn
        self.bus = bus
        self.tick = tick_seconds
        self._task: Optional[asyncio.Task] = None
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, branch_id: str) -> asyncio.Lock:
        lk = self._locks.get(branch_id)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[branch_id] = lk
        return lk

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._pump_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def pump_branch(self, branch_id: str, allow_paused: bool = False,
                          max_steps: int = 200) -> None:
        """Fire every attempt whose scheduled virtual time has arrived."""
        async with self.lock(branch_id):
            for _ in range(max_steps):
                b = await (await self.conn.execute(
                    "SELECT * FROM branches WHERE id=?", (branch_id,))).fetchone()
                if b is None or b["final_state"] is not None:
                    return
                if b["paused"] and not allow_paused:
                    return
                nv = b["next_vtime"]
                if nv is None:
                    return
                vt = vtime_now(b)
                # also bounds a manual jump: only fire attempts scheduled up to
                # the target virtual time
                if vt < nv:
                    return
                # fire exactly at the scheduled virtual time for determinism
                await _run_attempt(self.conn, self.bus, b, b["next_attempt_no"],
                                   nv, nv)

    async def _pump_loop(self) -> None:
        while True:
            cur = await self.conn.execute(
                "SELECT id FROM branches WHERE final_state IS NULL AND paused=0")
            due_ids = [r["id"] for r in await cur.fetchall()]
            for bid in due_ids:
                b = await (await self.conn.execute(
                    "SELECT next_vtime FROM branches WHERE id=?", (bid,))).fetchone()
                if b is None:
                    continue
                # vtime evaluated under lock inside pump_branch
                try:
                    await self.pump_branch(bid)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    import logging
                    logging.getLogger("wh.executor").exception("pump failed: %s", bid)
            await asyncio.sleep(self.tick)


# ---------------------------------------------------------------------------
# clock control
# ---------------------------------------------------------------------------

async def pause_branch(conn, bus: Bus, ex: Executor, bid: str) -> None:
    async with ex.lock(bid):
        b = await (await conn.execute("SELECT * FROM branches WHERE id=?", (bid,))).fetchone()
        if b is None or b["paused"] or b["final_state"] is not None:
            return
        vt = vtime_now(b)
        await conn.execute(
            "UPDATE branches SET paused=1, base_vtime=?, base_wall=? WHERE id=?",
            (vt, time.time(), bid))
        await conn.commit()
        await _publish_clock(conn, bus, bid, vt, True, b["speed"])


async def resume_branch(conn, bus: Bus, ex: Executor, bid: str,
                        speed: Optional[float] = None) -> None:
    async with ex.lock(bid):
        b = await (await conn.execute("SELECT * FROM branches WHERE id=?", (bid,))).fetchone()
        if b is None:
            return
        vt = vtime_now(b)
        sp = speed if speed is not None else b["speed"]
        await conn.execute(
            "UPDATE branches SET paused=0, speed=?, base_vtime=?, base_wall=? WHERE id=?",
            (sp, vt, time.time(), bid))
        await conn.commit()
        await _publish_clock(conn, bus, bid, vt, False, sp)
    await ex.pump_branch(bid)


async def set_speed(conn, bus: Bus, ex: Executor, bid: str, speed: float) -> None:
    async with ex.lock(bid):
        b = await (await conn.execute("SELECT * FROM branches WHERE id=?", (bid,))).fetchone()
        if b is None:
            return
        vt = vtime_now(b)
        await conn.execute(
            "UPDATE branches SET speed=?, base_vtime=?, base_wall=? WHERE id=?",
            (speed, vt, time.time(), bid))
        await conn.commit()
        await _publish_clock(conn, bus, bid, vt, bool(b["paused"]), speed)


async def jump_branch(conn, bus: Bus, ex: Executor, bid: str, target_vtime: int) -> None:
    """Manually advance a paused clock to an absolute virtual time, firing
    every attempt scheduled up to that point."""
    async with ex.lock(bid):
        b = await (await conn.execute("SELECT * FROM branches WHERE id=?", (bid,))).fetchone()
        if b is None:
            return
        cur = vtime_now(b)
        if target_vtime < cur:
            raise ValueError("cannot rewind the virtual clock")
        await conn.execute(
            "UPDATE branches SET paused=1, base_vtime=?, base_wall=? WHERE id=?",
            (target_vtime, time.time(), bid))
        await conn.commit()
        await _publish_clock(conn, bus, bid, target_vtime, True, b["speed"])
    # pump reacquires the lock
    await ex.pump_branch(bid, allow_paused=True)


async def _publish_clock(conn, bus: Bus, bid: str, vt: int, paused: bool,
                         speed: float) -> None:
    d = await (await conn.execute("SELECT delivery_id FROM branches WHERE id=?",
                                  (bid,))).fetchone()
    bus.publish(0, "clock", {"branch_id": bid, "delivery_id": d["delivery_id"],
                            "type": "clock", "vtime": vt, "paused": paused,
                            "speed": speed})


# ---------------------------------------------------------------------------
# fork
# ---------------------------------------------------------------------------

async def fork_branch(conn, bus: Bus, ex: Executor, parent_id: str, data) -> str:
    async with ex.lock(parent_id):
        p = await (await conn.execute("SELECT * FROM branches WHERE id=?",
                                      (parent_id,))).fetchone()
        if p is None:
            raise KeyError("branch not found")
        if not p["paused"] and p["final_state"] is None:
            raise ValueError("pause the branch before forking at a checkpoint")

        last_attempt = await (await conn.execute(
            "SELECT COALESCE(MAX(attempt_no),0) FROM attempts WHERE branch_id=?",
            (parent_id,))).fetchone()
        max_done = last_attempt[0]
        cp = data.after_attempt if data.after_attempt is not None else max_done
        if cp < 0 or cp > max_done:
            raise ValueError(f"checkpoint must be between 0 and {max_done}")

        rules = ([r.model_dump() for r in data.rules] if data.rules is not None
                 else json.loads(p["rules_json"]))
        backoff = (data.backoff.normalized() if data.backoff is not None
                   else json.loads(p["backoff_json"]))
        max_attempts = data.max_attempts if data.max_attempts is not None else p["max_attempts"]
        if max_attempts <= cp:
            raise ValueError("max_attempts must be greater than the fork checkpoint")
        body = core_canon(data.body) if data.body is not None else p["body"]
        secret = data.secret if data.secret is not None else p["secret"]
        speed = data.speed if data.speed is not None else p["speed"]

        # virtual time of the checkpoint = vtime of the last inherited attempt
        cp_vt = p["origin_vtime"]
        if cp > 0:
            a = await (await conn.execute(
                "SELECT started_at_vtime FROM attempts WHERE branch_id=? AND attempt_no=?",
                (parent_id, cp))).fetchone()
            cp_vt = a["started_at_vtime"]

        child_id = uuid.uuid4().hex[:12]
        label = data.label or f"fork@{cp}"
        await conn.execute(
            "INSERT INTO branches(id, delivery_id, parent_branch_id, label, target_url, body,"
            " secret, backoff_json, rules_json, max_attempts, next_attempt_no, last_status,"
            " paused, speed, origin_vtime, base_vtime, base_wall, next_vtime, final_state, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (child_id, p["delivery_id"], parent_id, label, p["target_url"], body, secret,
             json.dumps(backoff), json.dumps(rules), max_attempts, cp + 1, p["last_status"],
             1 if data.paused else 0, speed, p["origin_vtime"], cp_vt, time.time(),
             None, None, time.time()),
        )

        # copy inherited attempts (history only — never replayed over HTTP)
        if cp > 0:
            rows = await (await conn.execute(
                "SELECT * FROM attempts WHERE branch_id=? AND attempt_no<=? ORDER BY attempt_no",
                (parent_id, cp))).fetchall()
            for r in rows:
                await conn.execute(
                    "INSERT INTO attempts(id, branch_id, attempt_no, scheduled_at_vtime,"
                    " started_at_vtime, idempotency_key, signature, action, status_code,"
                    " outcome, duplicate, detail, is_inherited, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, child_id, r["attempt_no"], r["scheduled_at_vtime"],
                     r["started_at_vtime"], r["idempotency_key"], r["signature"], r["action"],
                     r["status_code"], r["outcome"], r["duplicate"], r["detail"], 1, time.time()),
                )

        # copy the history slice of events. A terminal event of the parent is
        # never copied: the child resumes past the checkpoint with a fresh fate.
        ev_rows = await (await conn.execute(
            "SELECT * FROM events WHERE branch_id=? ORDER BY id", (parent_id,))).fetchall()
        for r in ev_rows:
            payload = json.loads(r["payload"])
            t = payload.get("type")
            if t not in ("scheduled", "attempt"):
                continue
            if payload.get("attempt_no", 10 ** 9) > cp:
                continue
            payload["inherited"] = True
            await _insert_event(conn, child_id, r["event_type"], r["vtime"], payload)

        # schedule attempt cp+1 under the child's (possibly new) backoff
        next_no = cp + 1
        d = await (await conn.execute("SELECT seed FROM deliveries WHERE id=?",
                                      (p["delivery_id"],))).fetchone()
        if p["parent_branch_id"] is None:
            key_mat = core.root_idempotency_key(d["seed"], next_no)
        else:
            key_mat = core.forked_idempotency_key(
                core.branch_key_seed(d["seed"], parent_id), next_no)
        delay = core.backoff_delay_ms(backoff, next_no, key_mat) if next_no > 1 else 0
        nv = cp_vt + delay
        await conn.execute("UPDATE branches SET next_vtime=? WHERE id=?", (nv, child_id))
        sched = {"type": "scheduled", "attempt_no": next_no, "vtime": nv,
                 "delay_ms": delay, "after_attempt": cp, "forked_from": parent_id}
        seid = await _insert_event(conn, child_id, "scheduled", nv, sched)

        fork_payload = {"type": "forked", "child_branch_id": child_id,
                        "parent_branch_id": parent_id, "after_attempt": cp,
                        "vtime": cp_vt}
        feid1 = await _insert_event(conn, parent_id, "forked", cp_vt, fork_payload)
        feid2 = await _insert_event(conn, child_id, "forked", cp_vt, fork_payload)
        await conn.commit()

    bus.publish(feid1, "evt", {"branch_id": parent_id, "delivery_id": p["delivery_id"],
                               **fork_payload})
    bus.publish(feid2, "evt", {"branch_id": child_id, "delivery_id": p["delivery_id"],
                               **fork_payload})
    bus.publish(seid, "evt", {"branch_id": child_id, "delivery_id": p["delivery_id"], **sched})

    if not data.paused:
        await ex.pump_branch(child_id)
    return child_id
