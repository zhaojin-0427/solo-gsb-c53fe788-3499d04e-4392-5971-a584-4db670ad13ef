"""REST + SSE HTTP layer."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from . import executor as exm
from .core import verify_signature
from .db import get_db
from .models import DeliveryIn, ForkIn, JumpIn, ScenarioIn, SpeedIn

router = APIRouter()


# ---------------------------------------------------------------------------
# serialization
# ---------------------------------------------------------------------------

def _scenario(row) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "target_url": row["target_url"],
        "body": json.loads(row["body"]), "body_raw": row["body"],
        "secret": row["secret"], "backoff": json.loads(row["backoff_json"]),
        "rules": json.loads(row["rules_json"]), "max_attempts": row["max_attempts"],
        "created_at": row["created_at"],
    }


def _branch(row) -> dict[str, Any]:
    return {
        "id": row["id"], "delivery_id": row["delivery_id"],
        "parent_branch_id": row["parent_branch_id"], "label": row["label"],
        "target_url": row["target_url"], "body": json.loads(row["body"]),
        "secret": row["secret"], "backoff": json.loads(row["backoff_json"]),
        "rules": json.loads(row["rules_json"]), "max_attempts": row["max_attempts"],
        "next_attempt_no": row["next_attempt_no"], "last_status": row["last_status"],
        "paused": bool(row["paused"]), "speed": row["speed"],
        "origin_vtime": row["origin_vtime"], "next_vtime": row["next_vtime"],
        "base_vtime": row["base_vtime"], "base_wall": row["base_wall"],
        "final_state": row["final_state"],
        "vtime_now": exm.vtime_now(row),
    }


def _attempt(row) -> dict[str, Any]:
    return dict(row) | {
        "duplicate": bool(row["duplicate"]), "is_inherited": bool(row["is_inherited"]),
    }


def _event(row) -> dict[str, Any]:
    return {
        "id": row["id"], "branch_id": row["branch_id"],
        "event_type": row["event_type"], "vtime": row["vtime"],
        "payload": json.loads(row["payload"]),
    }


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------

@router.get("/api/scenarios")
async def list_scenarios(request: Request):
    db = get_db(request)
    rows = await (await db.execute(
        "SELECT s.*, COUNT(d.id) AS delivery_count FROM scenarios s"
        " LEFT JOIN deliveries d ON d.scenario_id=s.id"
        " GROUP BY s.id ORDER BY s.created_at DESC")).fetchall()
    out = []
    for r in rows:
        item = _scenario(r)
        item["delivery_count"] = r["delivery_count"]
        out.append(item)
    return out


@router.post("/api/scenarios", status_code=201)
async def create_scenario(request: Request, data: ScenarioIn):
    db = get_db(request)
    sid = await exm.create_scenario(db, data)
    await db.commit()
    return {"id": sid}


@router.get("/api/scenarios/{sid}")
async def get_scenario(request: Request, sid: str):
    db = get_db(request)
    r = await (await db.execute("SELECT * FROM scenarios WHERE id=?", (sid,))).fetchone()
    if r is None:
        raise HTTPException(404, "scenario not found")
    return _scenario(r)


@router.put("/api/scenarios/{sid}")
async def update_scenario(request: Request, sid: str, data: ScenarioIn):
    db = get_db(request)
    r = await (await db.execute("SELECT id FROM scenarios WHERE id=?", (sid,))).fetchone()
    if r is None:
        raise HTTPException(404, "scenario not found")
    await exm.update_scenario(db, sid, data)
    await db.commit()
    return {"ok": True}


@router.delete("/api/scenarios/{sid}")
async def delete_scenario(request: Request, sid: str):
    db = get_db(request)
    await db.execute("DELETE FROM scenarios WHERE id=?", (sid,))
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# deliveries & branches
# ---------------------------------------------------------------------------

@router.post("/api/deliveries", status_code=201)
async def create_delivery(request: Request, data: DeliveryIn):
    db = get_db(request)
    try:
        did = await exm.create_delivery(db, request.app.state.bus, data)
    except KeyError:
        raise HTTPException(404, "scenario not found")
    return {"id": did}


@router.get("/api/deliveries")
async def list_deliveries(request: Request, scenario_id: Optional[str] = None):
    db = get_db(request)
    if scenario_id:
        cur = await db.execute(
            "SELECT * FROM deliveries WHERE scenario_id=? ORDER BY created_at DESC",
            (scenario_id,))
    else:
        cur = await db.execute("SELECT * FROM deliveries ORDER BY created_at DESC")
    rows = await cur.fetchall()
    out = []
    for d in rows:
        bcur = await db.execute(
            "SELECT * FROM branches WHERE delivery_id=? ORDER BY created_at", (d["id"],))
        out.append({"id": d["id"], "scenario_id": d["scenario_id"],
                    "created_at": d["created_at"],
                    "branches": [_branch(b) for b in await bcur.fetchall()]})
    return out


@router.get("/api/deliveries/{did}")
async def get_delivery(request: Request, did: str):
    db = get_db(request)
    d = await (await db.execute("SELECT * FROM deliveries WHERE id=?", (did,))).fetchone()
    if d is None:
        raise HTTPException(404, "delivery not found")
    bcur = await db.execute(
        "SELECT * FROM branches WHERE delivery_id=? ORDER BY created_at", (did,))
    branches = [_branch(b) for b in await bcur.fetchall()]
    result = {"id": d["id"], "scenario_id": d["scenario_id"],
              "created_at": d["created_at"], "branches": branches}
    for b in result["branches"]:
        acur = await db.execute(
            "SELECT * FROM attempts WHERE branch_id=? ORDER BY attempt_no", (b["id"],))
        b["attempts"] = [_attempt(a) for a in await acur.fetchall()]
        ecur = await db.execute(
            "SELECT * FROM events WHERE branch_id=? ORDER BY id", (b["id"],))
        b["events"] = [_event(e) for e in await ecur.fetchall()]
    return result


@router.delete("/api/deliveries/{did}")
async def delete_delivery(request: Request, did: str):
    db = get_db(request)
    await db.execute("DELETE FROM deliveries WHERE id=?", (did,))
    await db.commit()
    return {"ok": True}


@router.post("/api/branches/{bid}/fork", status_code=201)
async def fork_branch(request: Request, bid: str, data: ForkIn):
    db = get_db(request)
    try:
        child = await exm.fork_branch(db, request.app.state.bus,
                                      request.app.state.executor, bid, data)
    except KeyError:
        raise HTTPException(404, "branch not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": child}


@router.post("/api/branches/{bid}/resume")
async def resume_branch(request: Request, bid: str):
    db = get_db(request)
    await exm.resume_branch(db, request.app.state.bus, request.app.state.executor, bid)
    return {"ok": True}


@router.post("/api/branches/{bid}/pause")
async def pause_branch(request: Request, bid: str):
    db = get_db(request)
    await exm.pause_branch(db, request.app.state.bus, request.app.state.executor, bid)
    return {"ok": True}


@router.post("/api/branches/{bid}/speed")
async def set_speed(request: Request, bid: str, data: SpeedIn):
    db = get_db(request)
    await exm.set_speed(db, request.app.state.bus, request.app.state.executor,
                        bid, data.speed)
    return {"ok": True}


@router.post("/api/branches/{bid}/jump")
async def jump_branch(request: Request, bid: str, data: JumpIn):
    db = get_db(request)
    try:
        await exm.jump_branch(db, request.app.state.bus, request.app.state.executor,
                              bid, data.vtime_ms)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ---------------------------------------------------------------------------
# inbox (receiver side) + external ingest
# ---------------------------------------------------------------------------

@router.get("/api/inbox")
async def list_inbox(request: Request, scenario_id: Optional[str] = None):
    db = get_db(request)
    if scenario_id:
        cur = await db.execute(
            "SELECT * FROM inbox WHERE scenario_id=? ORDER BY received_at_vtime DESC",
            (scenario_id,))
    else:
        cur = await db.execute(
            "SELECT * FROM inbox ORDER BY received_at_vtime DESC LIMIT 200")
    rows = await cur.fetchall()
    return [{"idempotency_key": r["idempotency_key"], "branch_id": r["branch_id"],
             "scenario_id": r["scenario_id"], "attempt_no": r["attempt_no"],
             "received_at_vtime": r["received_at_vtime"],
             "payload": json.loads(r["payload"])} for r in rows]


# ---------------------------------------------------------------------------
# external webhook receiver (the "user's service" side)
#
# Executor deliveries are simulated in-process, but this endpoint demonstrates
# the receiver contract: idempotent by Idempotency-Key and authenticated with
# an X-Webhook-Signature HMAC when the scenario has a secret.
# ---------------------------------------------------------------------------

@router.post("/hooks/{sid}")
async def receive_hook(request: Request, sid: str):
    db = get_db(request)
    sc = await (await db.execute("SELECT * FROM scenarios WHERE id=?", (sid,))).fetchone()
    if sc is None:
        raise HTTPException(404, "no such endpoint")
    raw = (await request.body()).decode("utf-8")
    key = request.headers.get("idempotency-key", "")
    sig = request.headers.get("x-webhook-signature", "")
    ts = request.headers.get("x-webhook-timestamp", "")
    if not key:
        raise HTTPException(400, "missing Idempotency-Key")
    if sc["secret"] and not verify_signature(sc["secret"], sig, key, ts, raw):
        raise HTTPException(401, "invalid signature")

    existing = await (await db.execute(
        "SELECT 1 FROM inbox WHERE idempotency_key=?", (key,))).fetchone()
    if existing is None:
        await db.execute(
            "INSERT INTO inbox(idempotency_key, branch_id, scenario_id, attempt_no,"
            " received_at_vtime, payload, created_at) VALUES (?,?,?,?,?,?,?)",
            (key, "external", sid, 0, exm.now_ms(), raw, time.time()))
        await db.commit()
        return {"status": "accepted", "idempotency_key": key}
    return {"status": "duplicate", "idempotency_key": key}


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------

@router.get("/api/stream")
async def stream(request: Request):
    bus = request.app.state.bus
    after_id = 0
    lid = request.headers.get("last-event-id")
    if lid and lid.isdigit():
        after_id = int(lid)
    q, backlog = bus.subscribe(after_id)

    async def gen():
        try:
            yield b"retry: 2000\n\n"
            for eid, etype, payload in backlog:
                yield f"id: {eid}\nevent: {etype}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
            while True:
                try:
                    eid, etype, payload = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"id: {eid}\nevent: {etype}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
                except asyncio.TimeoutError:
                    yield b": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
