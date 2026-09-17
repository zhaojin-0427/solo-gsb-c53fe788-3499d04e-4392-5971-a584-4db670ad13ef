"""进程内事件总线：持久化事件 + SSE 实时广播。

注意：``emit`` 会自开连接写库，严禁在一个尚未提交的写事务中调用
（SQLite 同一时刻只允许一个写者，会自死锁）。事务内请先用
``db.add_event_dict`` 落库，提交后再调用 ``broadcast``。
"""
import asyncio
import json

from . import db


class EventHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self, maxsize: int = 1000) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def emit(self, run_id: str, kind: str, data: dict | None = None, *,
             at_ms: int | None = None, attempt: int | None = None,
             inherited: bool = False) -> dict:
        """事件落库并向所有 SSE 客户端广播（独立短事务）。"""
        conn = db.get_conn()
        try:
            event = db.add_event_dict(
                conn, run_id, kind, data or {},
                at_ms=at_ms, attempt=attempt, inherited=inherited)
            conn.commit()
        finally:
            conn.close()
        self.broadcast(event)
        return event

    def broadcast(self, event: dict) -> None:
        """只广播，不落库（用于事务内已落库的事件）。"""
        payload = json.dumps(event, ensure_ascii=False)
        for q in tuple(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # 慢客户端丢弃：它会通过 after-id 回放补齐
                pass


hub = EventHub()
