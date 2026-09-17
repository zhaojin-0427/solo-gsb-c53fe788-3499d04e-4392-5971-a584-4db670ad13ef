"""规则解析、退避计算与投递执行器。"""
import hashlib
import time
from typing import Any

import httpx

from . import config, db, signing


# ---------------------------------------------------------------- 规则

def resolve_rule(snapshot: dict, attempt: int) -> dict:
    """按尝试次数解析规则；无显式规则时使用场景默认动作。"""
    for r in snapshot.get("rules", []):
        if r["attempt"] == attempt:
            return {
                "action": r["action"],
                "status_code": r.get("status_code"),
                "delay_ms": r.get("delay_ms", 0),
                "response_body": r.get("response_body"),
            }
    return {
        "action": snapshot.get("default_action", "success"),
        "status_code": snapshot.get("default_status_code", 200),
        "delay_ms": snapshot.get("default_delay_ms", 0),
        "response_body": None,
    }


def is_success_outcome(rule: dict) -> bool:
    if rule["action"] == "success":
        return True
    if rule["action"] == "status":
        code = rule.get("status_code") or 200
        return 200 <= code < 300
    return False


# ---------------------------------------------------------------- 退避

def backoff_delay_ms(snapshot: dict, attempt: int) -> int:
    """第 attempt 次失败后，距下一次尝试的等待毫秒。确定性、可选抖动。"""
    b = snapshot["backoff"]
    base = float(b["base_ms"])
    factor = float(b["factor"])
    delay = base * (factor ** max(0, attempt - 1))
    delay = min(delay, float(b["max_ms"]))
    if b.get("jitter"):
        # 确定性“抖动”：由场景与尝试号派生，保证回放稳定
        seed = hashlib.sha256(f"{snapshot['name']}|jitter|{attempt}".encode()).digest()
        frac = int.from_bytes(seed[:4], "big") / 0xFFFFFFFF  # [0,1)
        delay *= 0.5 + 0.5 * frac
    return int(delay)


# ---------------------------------------------------------------- 幂等键 / 签名

def make_idem_key(root_run_id: str, lineage: list[int], attempt: int, body: Any) -> str:
    return signing.idempotency_key(root_run_id, lineage, attempt, body)


def _receiver_base_url() -> str:
    return config.RECEIVER_BASE_URL


# ---------------------------------------------------------------- 执行

async def execute_attempt(run: dict, attempt_row) -> dict:
    """执行一次真实 HTTP 投递，返回结果摘要（不落库，事务由调度器统一管理）。

    调用方保证：在发起请求前，attempt 已在独立事务中持久化为 running。
    因此即使进程在投递中途崩溃，重启恢复时该尝试仍是 pending，会被重新投递；
    接收端按幂等键去重，不会产生重复的已确认副作用。
    """
    snapshot = run["snapshot"]
    body = snapshot["body"]
    secret = snapshot.get("secret", "")
    attempt_no = attempt_row["attempt"]
    idem = attempt_row["idem_key"]
    timestamp_ms = attempt_row["scheduled_ms"]  # 用计划虚拟时间做签名时间戳

    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": idem,
        "X-Idempotency-Key": idem,
        "X-Webhook-Id": run["id"],
        "X-Webhook-Attempt": str(attempt_no),
        "X-Webhook-Timestamp": str(timestamp_ms),
        "X-Signature": signing.sign(secret, timestamp_ms, body),
    }
    url = config.RECEIVER_BASE_URL + config.RECEIVER_PREFIX + snapshot.get("url_path", "/hook")

    started = db.now_ms()
    status: int | None = None
    sig_valid: bool | None = None
    replay = False
    side_effect_id: int | None = None
    error: str | None = None
    result = "network_error"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(config.REQUEST_TIMEOUT_SEC)) as client:
            resp = await client.post(url, content=signing.canonical_body(body), headers=headers)
        status = resp.status_code
        sig_header = resp.headers.get("X-Sig-Valid")
        if sig_header is not None:
            sig_valid = sig_header == "1"
        if resp.headers.get("X-Replay") == "1":
            replay = True
        raw_side = resp.headers.get("X-Side-Effect-Id")
        if raw_side:
            side_effect_id = int(raw_side)

        if status == 599:
            result = "timeout"
            error = "receiver held the connection past the client timeout"
        elif status == 598:
            result = "disconnect"
            error = "receiver dropped the connection without a response"
        elif 200 <= status < 300:
            result = "success"
        else:
            result = "failure"
            try:
                error = resp.text[:300]
            except Exception:  # noqa: BLE001
                error = f"HTTP {status}"
    except httpx.TimeoutException:
        result = "timeout"
        error = "client timeout waiting for response"
    except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.NetworkError,
            httpx.ReadError, httpx.WriteError) as exc:
        # 接收端直接掐断连接会落到这里
        result = "disconnect"
        error = f"{type(exc).__name__}: connection closed by receiver"
    except httpx.RequestError as exc:
        result = "network_error"
        error = f"{type(exc).__name__}: {exc}"

    finished = db.now_ms()
    return {
        "result": result,
        "status": status,
        "sig_valid": sig_valid,
        "replay": replay,
        "side_effect_id": side_effect_id,
        "error": error,
        "started_ms": started,
        "finished_ms": finished,
        "real_duration_ms": max(0, finished - started),
    }
