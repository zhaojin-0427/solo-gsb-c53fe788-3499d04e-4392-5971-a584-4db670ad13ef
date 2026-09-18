"""In-process pub/sub for Server-Sent Events.

Every committed event is fanned out to subscriber queues. Queues are bounded;
slow consumers drop oldest events (they can recover from the SQLite-backed
event endpoints / Last-Event-ID replay).
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any


class Bus:
    def __init__(self, replay_size: int = 500) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._replay: deque[tuple[int, str, dict[str, Any]]] = deque(maxlen=replay_size)

    def subscribe(self, after_id: int = 0) -> tuple[asyncio.Queue, list[tuple[int, str, dict]]]:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        backlog = [(i, t, p) for (i, t, p) in self._replay if i > after_id]
        self._subs.add(q)
        return q, backlog

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event_id: int, event_type: str, payload: dict[str, Any]) -> None:
        self._replay.append((event_id, event_type, payload))
        dead: list[asyncio.Queue] = []
        for q in self._subs:
            try:
                q.put_nowait((event_id, event_type, payload))
            except asyncio.QueueFull:
                # drop the oldest queued event to make room
                try:
                    q.get_nowait()
                except Exception:
                    pass
                try:
                    q.put_nowait((event_id, event_type, payload))
                except asyncio.QueueFull:
                    dead.append(q)
        for q in dead:
            self._subs.discard(q)
