"""幂等键与 HMAC 签名。

键 / 签名全部由确定性输入派生：相同的（根演练、分叉谱系、尝试号、请求体）
在任意节点（本机、容器重启后）都会得到完全一致的值，从而保证：

* 接收端按幂等键去重，崩溃重放不会产生重复的“已确认投递”副作用；
* 每次尝试携带可被接收端验证的 HMAC-SHA256 签名。
"""
import hashlib
import hmac
import json
from typing import Any


def canonical_body(body: Any) -> str:
    """把任意 JSON 可序列化的请求体规范化为稳定字符串（排序键、无空白）。"""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def body_hash(body: Any) -> str:
    return hashlib.sha256(canonical_body(body).encode("utf-8")).hexdigest()


def idempotency_key(root_run_id: str, lineage: list[int], attempt: int, body: Any) -> str:
    """派生稳定幂等键（40 位十六进制）。

    lineage 为自根演练起的分叉路径（例如 [3, 1] 表示根 -> fork#3 -> fork#1），
    根演练自身为 []。分叉点之前拷贝的尝试沿用原 lineage，因此键不变；
    分叉点之后的新尝试键不同，会形成一条“新投递”分支。
    """
    lineage_str = ".".join(str(x) for x in lineage)
    material = f"{root_run_id}|{lineage_str}|{attempt}|{body_hash(body)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def sign(secret: str, timestamp_ms: int, body: Any) -> str:
    """GitHub 风格 HMAC-SHA256：签名内容为 ``<timestamp>.<canonical body>``。"""
    payload = f"{timestamp_ms}.{canonical_body(body)}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify(secret: str, timestamp_ms: str, body: bytes, signature: str) -> bool:
    if not signature or not timestamp_ms:
        return False
    expected = sign(secret, int(timestamp_ms), json.loads(body.decode("utf-8")))
    return hmac.compare_digest(expected, signature)
