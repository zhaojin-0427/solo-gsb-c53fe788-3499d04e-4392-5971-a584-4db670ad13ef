"""FastAPI application entrypoint."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import api
from .bus import Bus
from .db import connect
from .executor import Executor
from .seed import seed_if_empty

DB_PATH = os.environ.get("WH_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "wh.db"))
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    app.state.db = await connect(DB_PATH)
    app.state.bus = Bus()
    app.state.executor = Executor(app.state.db, app.state.bus)
    await seed_if_empty(app.state.db)
    await app.state.executor.start()
    try:
        yield
    finally:
        await app.state.executor.stop()
        await app.state.db.close()


app = FastAPI(title="Webhook Failure Drill & Replay Console", lifespan=lifespan)
app.include_router(api.router)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
