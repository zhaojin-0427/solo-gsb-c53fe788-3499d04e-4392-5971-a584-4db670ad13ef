"""End-to-end tests for the drill & replay console."""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

# the test harness must point WH_DB_PATH at a throwaway database
os.environ.setdefault(
    "WH_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="wh-test-"), "test.db"))

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            yield ac


async def _make_scenario(client: AsyncClient, rules, **kw) -> str:
    payload = {
        "name": "t", "target_url": "http://t/hooks/x",
        "body": {"a": 1}, "secret": "s3cr3t",
        "backoff": {"kind": "exponential", "base_ms": 1000, "factor": 2,
                    "max_delay_ms": 60000, "jitter": "none"},
        "rules": rules, "max_attempts": kw.get("max_attempts", 5),
    }
    payload.update(kw)
    r = await client.post("/api/scenarios", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _wait_terminal(client: AsyncClient, did: str, branch_id: str,
                         timeout: float = 5.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/api/deliveries/{did}")
        b = next(x for x in r.json()["branches"] if x["id"] == branch_id)
        if b["final_state"]:
            return b
        await asyncio.sleep(0.03)
    raise AssertionError("branch never reached terminal state")


@pytest.mark.anyio
async def test_retry_then_delivered(client):
    sid = await _make_scenario(client, [
        {"attempt_no": 1, "action": "status", "status_code": 503},
        {"attempt_no": 2, "action": "timeout"},
        {"attempt_no": 0, "action": "status", "status_code": 200},
    ])
    r = await client.post("/api/deliveries",
                          json={"scenario_id": sid, "speed": 100})
    did = r.json()["id"]
    d = await client.get(f"/api/deliveries/{did}")
    root = d.json()["branches"][0]["id"]
    b = await _wait_terminal(client, did, root)

    assert b["final_state"] == "delivered"
    d = (await client.get(f"/api/deliveries/{did}")).json()
    b = d["branches"][0]
    outcomes = [(a["attempt_no"], a["outcome"], a["status_code"], a["action"])
                for a in b["attempts"]]
    assert outcomes == [
        (1, "retry", 503, "status"),
        (2, "retry", None, "timeout"),
        (3, "delivered", 200, "status"),
    ]
    # exponential backoff: attempt2 +1000ms, attempt3 +2000ms (virtual)
    a1, a2, a3 = b["attempts"]
    assert a2["started_at_vtime"] - a1["started_at_vtime"] == 1000
    assert a3["started_at_vtime"] - a2["started_at_vtime"] == 2000
    # signatures and keys present
    assert all(a["idempotency_key"] and a["signature"] for a in b["attempts"])


@pytest.mark.anyio
async def test_exhausted_and_persisted_state(client):
    sid = await _make_scenario(client, [
        {"attempt_no": 0, "action": "disconnect", "status_code": 200},
    ], max_attempts=3)
    did = (await client.post("/api/deliveries",
                             json={"scenario_id": sid, "speed": 1000})).json()["id"]
    d = await client.get(f"/api/deliveries/{did}")
    root = d.json()["branches"][0]["id"]
    b = await _wait_terminal(client, did, root)
    assert b["final_state"] == "exhausted"
    assert b["next_attempt_no"] == 4


@pytest.mark.anyio
async def test_pause_jump_and_scheduled_future(client):
    sid = await _make_scenario(client, [
        {"attempt_no": 1, "action": "status", "status_code": 500},
        {"attempt_no": 0, "action": "status", "status_code": 200},
    ], max_attempts=3)
    did = (await client.post("/api/deliveries",
                             json={"scenario_id": sid, "paused": True})).json()["id"]
    d = (await client.get(f"/api/deliveries/{did}")).json()
    root = d["branches"][0]
    assert root["paused"] is True
    assert len(root["attempts"]) == 0

    # jump to exactly first scheduled vtime => attempt 1 fires, attempt 2 scheduled
    await client.post(f"/api/branches/{root['id']}/jump",
                      json={"vtime_ms": root["origin_vtime"]})
    d = (await client.get(f"/api/deliveries/{did}")).json()
    b = d["branches"][0]
    assert len(b["attempts"]) == 1
    assert b["attempts"][0]["status_code"] == 500
    assert b["final_state"] is None

    # jump past attempt 2 scheduled time => delivered
    await client.post(f"/api/branches/{root['id']}/jump",
                      json={"vtime_ms": b["next_vtime"]})
    b = await _wait_terminal(client, did, b["id"])
    assert b["final_state"] == "delivered"


@pytest.mark.anyio
async def test_fork_replays_with_new_rules_and_compares(client):
    sid = await _make_scenario(client, [
        {"attempt_no": 0, "action": "status", "status_code": 500},
    ], max_attempts=3)
    did = (await client.post("/api/deliveries",
                             json={"scenario_id": sid, "speed": 1000})).json()["id"]
    d = (await client.get(f"/api/deliveries/{did}")).json()
    root = d["branches"][0]
    await _wait_terminal(client, did, root["id"])

    # fork after attempt 1, flip the default rule to 200
    r = await client.post(f"/api/branches/{root['id']}/fork", json={
        "label": "fix-200", "after_attempt": 1,
        "rules": [{"attempt_no": 0, "action": "status", "status_code": 200}],
        "max_attempts": 3, "speed": 1000,
    })
    child = r.json()["id"]
    b = await _wait_terminal(client, did, child)
    assert b["final_state"] == "delivered"

    d = (await client.get(f"/api/deliveries/{did}")).json()
    cb = next(x for x in d["branches"] if x["id"] == child)
    inherited = [a for a in cb["attempts"] if a["is_inherited"]]
    new = [a for a in cb["attempts"] if not a["is_inherited"]]
    assert len(inherited) == 1 and inherited[0]["attempt_no"] == 1
    assert len(new) == 1 and new[0]["attempt_no"] == 2 and new[0]["status_code"] == 200
    # inherited attempt kept the parent key; the new attempt's key differs from
    # the root branch's attempt-2 key
    rb = next(x for x in d["branches"] if x["parent_branch_id"] is None)
    assert inherited[0]["idempotency_key"] == rb["attempts"][0]["idempotency_key"]
    assert new[0]["idempotency_key"] != rb["attempts"][1]["idempotency_key"]
    # the successful fork attempt really entered the inbox once
    inbox = (await client.get("/api/inbox")).json()
    keys = [x["idempotency_key"] for x in inbox]
    assert new[0]["idempotency_key"] in keys
    assert keys.count(new[0]["idempotency_key"]) == 1


@pytest.mark.anyio
async def test_nonretriable_4xx_terminates(client):
    sid = await _make_scenario(client, [
        {"attempt_no": 1, "action": "status", "status_code": 400},
    ])
    did = (await client.post("/api/deliveries",
                             json={"scenario_id": sid, "speed": 1000})).json()["id"]
    d = (await client.get(f"/api/deliveries/{did}")).json()
    root = d["branches"][0]["id"]
    b = await _wait_terminal(client, did, root)
    assert b["final_state"] == "failed"
    d = (await client.get(f"/api/deliveries/{did}")).json()
    assert len(d["branches"][0]["attempts"]) == 1


@pytest.mark.anyio
async def test_external_hook_idempotent_and_signed(client):
    sid = await _make_scenario(client, [], secret="abc")
    body = b'{"a":1}'
    import hmac
    import hashlib
    key = "k-123"
    sig = hmac.new(b"abc", b"\n".join([key.encode(), b"0", body]),
                   hashlib.sha256).hexdigest()
    h = {"Idempotency-Key": key, "X-Webhook-Timestamp": "0",
         "X-Webhook-Signature": sig, "Content-Type": "application/json"}
    r1 = await client.post(f"/hooks/{sid}", content=body, headers=h)
    r2 = await client.post(f"/hooks/{sid}", content=body, headers=h)
    assert r1.json()["status"] == "accepted"
    assert r2.json()["status"] == "duplicate"

    bad = dict(h)
    bad["X-Webhook-Signature"] = "deadbeef"
    r3 = await client.post(f"/hooks/{sid}", content=body, headers=bad)
    assert r3.status_code == 401


@pytest.mark.anyio
async def test_restart_resumes_without_duplicate_delivery(client):
    """Simulate a container restart mid-flight: stop executor + close the DB,
    reopen, start a new executor. Due attempts must fire exactly once and an
    already-confirmed delivery must never be duplicated."""
    from app.db import connect
    from app.executor import Executor
    from app.core import root_idempotency_key

    sid = await _make_scenario(client, [
        {"attempt_no": 1, "action": "status", "status_code": 200},
    ])
    # paused delivery: nothing delivered yet
    did = (await client.post("/api/deliveries",
                             json={"scenario_id": sid, "paused": True, "speed": 1000})).json()["id"]
    d = (await client.get(f"/api/deliveries/{did}")).json()
    root = d["branches"][0]["id"]
    origin = d["branches"][0]["origin_vtime"]

    # "restart" the whole process: stop executor and reopen the DB connection
    ex: Executor = app.state.executor
    old_db = app.state.db
    await ex.stop()
    await old_db.close()
    new_db = await connect(os.environ["WH_DB_PATH"])
    app.state.db = new_db
    new_ex = Executor(new_db, app.state.bus)
    app.state.executor = ex = new_ex
    await new_ex.start()
    try:
        # branch is still paused and has zero attempts — state survived
        d = (await client.get(f"/api/deliveries/{did}")).json()
        b = d["branches"][0]
        assert b["paused"] is True and len(b["attempts"]) == 0

        # resume and let it run to terminal
        await client.post(f"/api/branches/{root}/resume")
        b = await _wait_terminal(client, did, root)
        assert b["final_state"] == "delivered"

        # force a redundant pump: nothing must be re-sent
        await new_ex.pump_branch(root)

        inbox = (await client.get("/api/inbox")).json()
        seed_row = await (await new_db.execute(
            "SELECT seed FROM deliveries WHERE id=?", (did,))).fetchone()
        key = root_idempotency_key(seed_row["seed"], 1)
        hits = [x for x in inbox if x["idempotency_key"] == key]
        assert len(hits) == 1
    finally:
        await new_ex.stop()
        await new_db.close()
        # reconnect a healthy state for later tests / shutdown
        app.state.db = await connect(os.environ["WH_DB_PATH"])
        app.state.executor = Executor(app.state.db, app.state.bus)
        await app.state.executor.start()


@pytest.mark.anyio
async def test_backoff_kinds_and_deterministic_jitter(client):
    from app.core import backoff_delay_ms
    bo = {"kind": "fixed", "base_ms": 500, "factor": 2, "max_delay_ms": 0, "jitter": "none"}
    assert backoff_delay_ms(bo, 2, "x") == 500
    assert backoff_delay_ms(bo, 5, "x") == 500
    bo = {"kind": "linear", "base_ms": 100, "factor": 2, "max_delay_ms": 0, "jitter": "none"}
    assert backoff_delay_ms(bo, 2, "x") == 100
    assert backoff_delay_ms(bo, 4, "x") == 300
    bo = {"kind": "exponential", "base_ms": 100, "factor": 2, "max_delay_ms": 1000,
          "jitter": "full"}
    v1 = backoff_delay_ms(bo, 3, "material")
    v2 = backoff_delay_ms(bo, 3, "material")
    assert v1 == v2 and 0 <= v1 <= 400
