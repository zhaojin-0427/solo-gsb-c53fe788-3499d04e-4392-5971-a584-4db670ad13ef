"""内置 mock Webhook 接收端（原始 ASGI 子应用）。

它本身是一个“真实”的 HTTP 服务：

* 校验 Idempotency-Key 与 HMAC 签名（错误签名返回 401）；
* 按该尝试的规则执行：成功/指定状态码/挂起到超时/直接断连；
* 仅 2xx 会创建业务副作用（side_effects）并确认投递（deliveries），
  全局按幂等键去重；同一键重放时返回 ``X-Replay: 1``，不再产生副作用。

挂起 / 断连这类长时间动作期间不持有数据库连接，避免占用写锁。
"""
import json
from dataclasses import dataclass

import anyio

from . import config, db, signing


@dataclass
class Reject:
    status: int
    payload: dict
    extra_headers: list[tuple[str, str]] | None = None


@dataclass
class Context:
    action: str
    delay_ms: int
    code: int | None
    sig_valid: bool
    info: dict


async def _json_response(send, status: int, payload: dict,
                           extra_headers: list[tuple[str, str]] | None = None) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [(b"content-type", b"application/json"),
               (b"content-length", str(len(body)).encode())]
    headers.extend((k.lower().encode(), v.encode()) for k, v in (extra_headers or []))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _read_body(receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        msg = await receive()
        if msg["type"] == "http.request":
            chunks.append(msg.get("body", b""))
            if not msg.get("more_body"):
                break
        elif msg["type"] == "http.disconnect":
            break
    return b"".join(chunks)


class ReceiverApp:
    """挂载在 /receiver 下的 ASGI 子应用。"""

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await _json_response(send, 405, {"detail": "method not allowed"})
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        idem = headers.get("idempotency-key") or headers.get("x-idempotency-key")
        raw_body = await _read_body(receive)
        if not idem:
            await _json_response(send, 400, {"detail": "missing Idempotency-Key"})
            return

        # ---- 解析投递上下文（短事务，拿完即释放）----
        ctx = self._resolve(idem, headers, raw_body)
        if isinstance(ctx, Reject):
            await _json_response(send, ctx.status, ctx.payload, ctx.extra_headers)
            return

        # ---- 超时：挂起直到客户端放弃 ----
        if ctx.action == "timeout":
            try:
                with anyio.fail_after(config.RECEIVER_HANG_SEC):
                    while True:
                        msg = await receive()
                        if msg["type"] == "http.disconnect":
                            return
                        await anyio.sleep(0.1)
            except Exception:  # noqa: BLE001
                return

        # ---- 断连：发出部分响应后直接关闭，不完成响应 ----
        if ctx.action == "disconnect":
            await send({"type": "http.response.start", "status": 200,
                         "headers": [(b"content-length", b"4096")]})
            await send({"type": "http.response.body", "body": b"partial",
                        "more_body": True})
            return

        # ---- 模拟处理延迟（真实 sleep，同样不持锁）----
        if ctx.delay_ms:
            try:
                with anyio.fail_after(config.RECEIVER_HANG_SEC):
                    await anyio.sleep(ctx.delay_ms / 1000.0)
            except Exception:  # noqa: BLE001
                return

        if ctx.action == "status" and not (200 <= (ctx.code or 0) < 300):
            await _json_response(
                send, ctx.code or 500, {"status": "error", "code": ctx.code},
                [("X-Sig-Valid", "1" if ctx.sig_valid else "0")])
            return

        # ---- 2xx：确认投递，创建去重副作用 ----
        side_id, replayed = self._accept(ctx.info, raw_body)
        final_code = ctx.code if (ctx.action == "status" and ctx.code) else 200
        await _json_response(
            send, final_code,
            {"status": "accepted", "replay": replayed, "side_effect_id": side_id},
            [("X-Sig-Valid", "1" if ctx.sig_valid else "0"),
             ("X-Replay", "1" if replayed else "0"),
             ("X-Side-Effect-Id", str(side_id))],
        )

    # ----------------------------------------------------------------

    def _resolve(self, idem: str, headers: dict, raw_body: bytes):
        """返回待执行的 Context，或需要直接回复的 Reject。"""
        conn = db.get_conn()
        try:
            attempt = db.get_attempt_by_key(conn, idem)
            if not attempt:
                return Reject(400, {"detail": "unknown idempotency key"})
            run = db.get_run_row(conn, attempt["run_id"])
            if not run:
                return Reject(400, {"detail": "run not found"})
            snapshot = db._loads(run["snapshot"], {})

            sig_valid = True
            if snapshot.get("verify_signature", True):
                sig_valid = signing.verify(
                    snapshot.get("secret", ""),
                    headers.get("x-webhook-timestamp", ""),
                    raw_body,
                    headers.get("x-signature", ""),
                )
                if not sig_valid:
                    return Reject(401, {"detail": "invalid signature"},
                                   [("X-Sig-Valid", "0")])

            info = {
                "scenario_id": run["scenario_id"],
                "root_run_id": run["root_run_id"],
                "run_id": run["id"],
                "attempt": attempt["attempt"],
                "idem_key": attempt["idem_key"],
            }
            return Context(
                action=attempt["action"] or "success",
                delay_ms=attempt["planned_delay"] or 0,
                code=attempt["planned_status"],
                sig_valid=sig_valid,
                info=info,
            )
        finally:
            conn.close()

    def _accept(self, info: dict, raw_body: bytes) -> tuple[int, bool]:
        """幂等确认；已存在则回放。返回 (side_effect_id, replayed)。"""
        import sqlite3
        conn = db.get_conn()
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                # 同键并发确认正在进行：按已存在的幂等记录回放
                existing = db.get_side_effect_by_key(conn, info["idem_key"])
                if existing:
                    return existing["id"], True
                raise
            existing = db.get_side_effect_by_key(conn, info["idem_key"])
            if existing:
                conn.commit()
                return existing["id"], True
            bhash = signing.hashlib.sha256(raw_body).hexdigest()
            side_id = db.create_side_effect(
                conn,
                scenario_id=info["scenario_id"],
                root_run_id=info["root_run_id"],
                run_id=info["run_id"],
                idem_key=info["idem_key"],
                body_hash=bhash,
            )
            db.record_delivery(
                conn, idem_key=info["idem_key"], run_id=info["run_id"],
                attempt=info["attempt"], side_effect_id=side_id)
            conn.commit()
            return side_id, False
        finally:
            conn.close()


receiver_app = ReceiverApp()
