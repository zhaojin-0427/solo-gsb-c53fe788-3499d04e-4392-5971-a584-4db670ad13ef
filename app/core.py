"""Deterministic idempotency keys, HMAC signatures, rule matching and backoff."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Optional

# A fixed namespace keeps keys distinct from any other (hypothetical) usage of
# the same UUID seeds.
_KEY_NS = "wh-replay/v1"


def _sha256(*parts: str) -> str:
    h = hashlib.sha256()
    h.update(_KEY_NS.encode())
    for p in parts:
        h.update(b"\0")
        h.update(p.encode("utf-8"))
    return h.hexdigest()


def delivery_seed(scenario_id: str, created_at: float, nonce: str) -> str:
    """Stable root seed for a delivery; persisted, survives restarts."""
    return _sha256("seed", scenario_id, f"{created_at:.6f}", nonce)


def root_idempotency_key(delivery_seed: str, attempt_no: int) -> str:
    """Key of an attempt on the root branch.

    Depends only on persisted inputs, so re-running after a crash computes the
    exact same key and the inbox deduplicates the already-confirmed delivery.
    """
    return _sha256("att", delivery_seed, str(attempt_no))[:32]


def branch_key_seed(root_seed: str, parent_branch_id: str) -> str:
    """Seed used by a forked branch — different from root and from siblings."""
    return _sha256("fork", root_seed, parent_branch_id)


def forked_idempotency_key(fork_seed: str, attempt_no: int) -> str:
    return _sha256("att", fork_seed, str(attempt_no))[:32]


def sign(secret: str, *parts: str) -> str:
    return hmac.new(secret.encode("utf-8"),
                    b"\n".join(p.encode("utf-8") for p in parts),
                    hashlib.sha256).hexdigest()


def verify_signature(secret: str, signature: str, *parts: str) -> bool:
    return hmac.compare_digest(signature, sign(secret, *parts))


def jitter_unit(key_material: str) -> float:
    """Deterministic pseudo-random value in [0, 1) from key material.

    Full jitter must be reproducible across restarts — we derive it from the
    idempotency key instead of using the RNG.
    """
    d = hashlib.sha256(key_material.encode()).hexdigest()
    return int(d[:12], 16) / float(16 ** 12)


def match_rule(rules: list[dict[str, Any]], attempt_no: int) -> Optional[dict[str, Any]]:
    """Exact attempt number wins; otherwise a rule with attempt_no == 0
    acts as a catch-all default. Exact match takes precedence."""
    default = None
    for r in rules:
        if r["attempt_no"] == attempt_no:
            return r
        if r["attempt_no"] == 0 and default is None:
            default = r
    return default


def resolve_action(rules: list[dict[str, Any]], attempt_no: int) -> dict[str, Any]:
    r = match_rule(rules, attempt_no)
    if r is None:
        return {"action": "status", "status_code": 200, "detail": "no rule: success"}
    return r


def backoff_delay_ms(backoff: dict[str, Any], attempt_no: int, key_material: str) -> int:
    """Delay *before* attempt ``attempt_no`` (attempt_no >= 2)."""
    kind = backoff.get("kind", "exponential")
    base = int(backoff.get("base_ms", 1000))
    factor = float(backoff.get("factor", 2.0))
    cap = int(backoff.get("max_delay_ms", 60_000))

    if kind == "fixed":
        d = base
    elif kind == "linear":
        d = base * (attempt_no - 1)
    else:  # exponential: delay before attempt n is base * factor**(n-2)
        d = int(round(base * factor ** (attempt_no - 2)))

    # full jitter: uniform in [0, d); then the cap still applies
    if backoff.get("jitter", "none") == "full" and d > 0:
        d = int(round(d * jitter_unit(key_material)))
    if cap:
        d = min(d, cap)
    return max(0, d)


def is_retriable_status(code: int) -> bool:
    return code == 408 or code == 429 or 500 <= code <= 599
