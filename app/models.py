"""请求/响应数据模型。"""
from typing import Any, Literal

from pydantic import BaseModel, Field

Action = Literal["success", "timeout", "disconnect", "status"]
RunStatus = Literal["running", "paused", "succeeded", "exhausted"]


class Rule(BaseModel):
    """按尝试次数（从 1 开始）触发的故障规则。"""
    attempt: int = Field(ge=1)
    action: Action = "success"
    status_code: int | None = Field(default=None, ge=100, le=599)
    delay_ms: int = Field(default=0, ge=0, le=120000)
    response_body: str | None = None

    def is_success(self) -> bool:
        if self.action == "success":
            return True
        if self.action == "status" and self.status_code is not None and 200 <= self.status_code < 300:
            return True
        return False


class Backoff(BaseModel):
    """指数退避（默认确定性，无抖动，便于稳定回放）。"""
    base_ms: int = Field(default=500, ge=0)
    factor: float = Field(default=2.0, ge=1.0)
    max_ms: int = Field(default=30000, ge=0)
    max_attempts: int = Field(default=5, ge=1, le=50)
    jitter: bool = False


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    body: Any = {"event": "demo", "data": {"id": 42}}
    secret: str = "shh-secret"
    verify_signature: bool = True
    url_path: str = Field(default="/hook", pattern=r"^/[A-Za-z0-9_\-/]*$")
    backoff: Backoff = Backoff()
    rules: list[Rule] = []
    default_action: Action = "success"
    default_status_code: int = Field(default=200, ge=100, le=599)
    default_delay_ms: int = Field(default=0, ge=0, le=120000)


class ScenarioOut(ScenarioIn):
    id: str
    created_at: int


class RunIn(BaseModel):
    speed: float = Field(default=0, ge=0)  # 0 表示沿用服务端默认倍率


class ControlIn(BaseModel):
    action: Literal["pause", "resume", "speed", "advance"]
    speed: float | None = Field(default=None, ge=0)
    advance_ms: int | None = Field(default=None, ge=0)


class ForkIn(BaseModel):
    checkpoint_attempt: int = Field(ge=1)
    # 覆盖的故障规则（attempt 序号沿用被重放分支的序号）
    rules: list[Rule] | None = None
    default_action: Action | None = None
    default_status_code: int | None = Field(default=None, ge=100, le=599)
    default_delay_ms: int | None = Field(default=None, ge=0, le=120000)
    label: str | None = None
