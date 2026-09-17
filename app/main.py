"""FastAPI 入口：REST API + SSE + 静态前端 + 内置 mock 接收端。"""
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, runs
from .hub import hub
from .receiver import receiver_app
from .scheduler import scheduler

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    await scheduler.start()
    yield
    await scheduler.stop()


app = FastAPI(title="Webhook 故障演练与回放台", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- 接收端（优先于静态路由）

app.mount(config.RECEIVER_PREFIX, receiver_app, name="receiver")


# ---------------------------------------------------------------- API

@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/scenarios")
async def api_list_scenarios():
    return db.list_scenarios()


def _parse_scenario(payload: dict) -> dict:
    from .models import ScenarioIn
    try:
        scenario = ScenarioIn.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=_validation_detail(exc))
    data = scenario.model_dump()
    try:
        runs.validate_scenario(data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return data


@app.post("/api/scenarios", status_code=201)
async def api_create_scenario(payload: dict):
    data = _parse_scenario(payload)
    scenario_id = uuid.uuid4().hex[:12]
    return db.create_scenario(scenario_id, data)


@app.put("/api/scenarios/{scenario_id}")
async def api_update_scenario(scenario_id: str, payload: dict):
    data = _parse_scenario(payload)
    if not db.update_scenario(scenario_id, data):
        raise HTTPException(status_code=404, detail="scenario not found")
    return {"id": scenario_id, **data}


@app.delete("/api/scenarios/{scenario_id}")
async def api_delete_scenario(scenario_id: str):
    if not db.delete_scenario(scenario_id):
        raise HTTPException(status_code=404, detail="scenario not found")
    return {"ok": True}


@app.get("/api/runs")
async def api_list_runs(scenario_id: str | None = None, limit: int = 100):
    return db.list_runs(scenario_id=scenario_id, limit=max(1, min(limit, 500)))


@app.post("/api/scenarios/{scenario_id}/runs", status_code=201)
async def api_create_run(scenario_id: str, payload: dict | None = None):
    speed = (payload or {}).get("speed")
    try:
        return runs.create_run(scenario_id, speed=speed)
    except KeyError:
        raise HTTPException(status_code=404, detail="scenario not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/api/runs/{run_id}")
async def api_get_run(run_id: str):
    conn = db.get_conn()
    try:
        row = db.get_run_row(conn, run_id)
        if not row:
            raise HTTPException(status_code=404, detail="run not found")
        return db.serialize_run(conn, row)
    finally:
        conn.close()


@app.post("/api/runs/{run_id}/control")
async def api_control_run(run_id: str, payload: dict):
    try:
        return runs.control_run(run_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/api/runs/{run_id}/fork", status_code=201)
async def api_fork_run(run_id: str, payload: dict):
    from .models import ForkIn
    try:
        ForkIn.model_validate(payload)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=_validation_detail(exc))
    try:
        return runs.fork_run(run_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/api/families/{root_run_id}")
async def api_family(root_run_id: str):
    conn = db.get_conn()
    try:
        family_rows = db.list_family(conn, root_run_id)
        if not family_rows:
            raise HTTPException(status_code=404, detail="family not found")
        return {
            "root_run_id": root_run_id,
            "branches": [db.serialize_run(conn, r) for r in family_rows],
            "side_effects": db.count_family_side_effects(conn, root_run_id),
            "deliveries": db.count_family_deliveries(conn, root_run_id),
        }
    finally:
        conn.close()


@app.get("/api/side-effects")
async def api_side_effects(root_run_id: str | None = None, scenario_id: str | None = None):
    conn = db.get_conn()
    try:
        sql = "SELECT * FROM side_effects WHERE 1=1"
        params: list = []
        if root_run_id:
            sql += " AND root_run_id=?"
            params.append(root_run_id)
        if scenario_id:
            sql += " AND scenario_id=?"
            params.append(scenario_id)
        sql += " ORDER BY id DESC LIMIT 200"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------- SSE

@app.get("/api/events")
async def api_events(request: Request, after_id: int = 0):
    """SSE：先回放 after_id 之后的持久化事件，再推送实时事件（含时钟）。"""

    async def stream():
        queue = hub.subscribe()
        try:
            conn = db.get_conn()
            try:
                for row in db.events_after(conn, after_id):
                    evt = {
                        "id": row["id"],
                        "run_id": row["run_id"],
                        "seq": row["seq"],
                        "kind": row["kind"],
                        "data": json.loads(row["data"]),
                        "at_ms": row["at_ms"],
                        "attempt": row["attempt"],
                        "inherited": bool(row["inherited"]),
                    }
                    yield _sse(row["id"], evt)
            finally:
                conn.close()

            yield b": ok\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield b": ping\n\n"
                    continue
                parsed = json.loads(payload)
                yield _sse(parsed.get("id"), parsed, raw=payload)
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


def _sse(event_id, event, raw: str | None = None) -> bytes:
    data = raw if raw is not None else json.dumps(event, ensure_ascii=False)
    prefix = f"id: {event_id}\n" if event_id is not None else ""
    return f"{prefix}data: {data}\n\n".encode("utf-8")


def _validation_detail(exc):
    try:
        return json.loads(exc.json())
    except Exception:  # noqa: BLE001
        return str(exc)


# ---------------------------------------------------------------- 静态前端

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
