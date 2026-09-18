"""Pydantic request models and config normalization helpers."""
from __future__ import annotations

import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

ActionType = Literal["status", "timeout", "disconnect"]

VALID_BACKOFF_KINDS = {"fixed", "linear", "exponential"}


class FaultRule(BaseModel):
    """Rule triggered when ``attempt`` equals ``attempt_no`` (per branch)."""

    attempt_no: int = Field(ge=0)
    action: ActionType = "status"
    status_code: int = Field(default=200, ge=100, le=599)
    detail: str = ""


class Backoff(BaseModel):
    kind: Literal["fixed", "linear", "exponential"] = "exponential"
    base_ms: int = Field(default=1000, ge=0)
    factor: float = Field(default=2.0, ge=1.0)
    max_delay_ms: int = Field(default=60_000, ge=0)
    jitter: Literal["none", "full"] = "none"

    def normalized(self) -> dict[str, Any]:
        return self.model_dump()


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    target_url: str = Field(min_length=1, max_length=500)
    body: Any = Field(default_factory=dict)
    secret: str = ""
    backoff: Backoff = Field(default_factory=Backoff)
    rules: list[FaultRule] = Field(default_factory=list)
    max_attempts: int = Field(default=5, ge=1, le=100)

    @field_validator("body")
    @classmethod
    def _body_jsonable(cls, v: Any) -> Any:
        # round-trip to guarantee JSON serializability and canonical form
        return json.loads(json.dumps(v, ensure_ascii=False, sort_keys=True))


class DeliveryIn(BaseModel):
    scenario_id: str
    paused: bool = False
    speed: float = Field(default=10.0, ge=0.01, le=100000.0)


class ForkIn(BaseModel):
    label: str = ""
    # checkpoint: fork AFTER this attempt number (None => before attempt 1)
    after_attempt: Optional[int] = Field(default=None, ge=0)
    rules: Optional[list[FaultRule]] = None
    backoff: Optional[Backoff] = None
    max_attempts: Optional[int] = Field(default=None, ge=1, le=100)
    body: Any = None
    secret: Optional[str] = None
    paused: bool = False
    speed: Optional[float] = Field(default=None, ge=0.01, le=100000.0)


class JumpIn(BaseModel):
    vtime_ms: int = Field(ge=0)


class SpeedIn(BaseModel):
    speed: float = Field(ge=0.01, le=100000.0)


def canonical_body(value: Any) -> str:
    """Canonical JSON used for signing and storage."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
