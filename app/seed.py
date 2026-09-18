"""First-run demo scenario so a fresh container is immediately explorable."""
from __future__ import annotations

import json
import time

from .models import canonical_body


async def seed_if_empty(conn) -> bool:
    n = await (await conn.execute("SELECT COUNT(*) AS c FROM scenarios")).fetchone()
    if n["c"] > 0:
        return False

    body = canonical_body({
        "event": "payment.captured",
        "order_id": "ord_8842",
        "amount": 19900,
        "currency": "CNY",
        "customer_id": "cus_77",
    })
    backoff = {"kind": "exponential", "base_ms": 1000, "factor": 2.0,
               "max_delay_ms": 60000, "jitter": "full"}
    # first two attempts fail in different ways, then a 200 — classic retry story
    rules = [
        {"attempt_no": 1, "action": "timeout",
         "status_code": 200, "detail": "网关读取超时 (read timeout)"},
        {"attempt_no": 2, "action": "disconnect",
         "status_code": 200, "detail": "连接被重置 (connection reset)"},
        {"attempt_no": 3, "action": "status",
         "status_code": 503, "detail": "服务暂不可用"},
        {"attempt_no": 0, "action": "status",
         "status_code": 200, "detail": "恢复后的成功请求"},
    ]
    await conn.execute(
        "INSERT INTO scenarios(id, name, target_url, body, secret, backoff_json,"
        " rules_json, max_attempts, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("demo00000001", "支付回调（示例）", "http://localhost:8000/hooks/demo",
         body, "demo-secret", json.dumps(backoff), json.dumps(rules), 6, time.time()),
    )
    await conn.commit()
    return True
