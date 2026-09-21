"""请求 / 响应 Pydantic 模型。"""

from __future__ import annotations

from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .serial import MAX_SERIAL, NEWER, EQUAL, OLDER, AMBIGUOUS, AXFR, IXFR, UP_TO_DATE

Serial32 = Annotated[int, Field(ge=0, le=MAX_SERIAL)]
Relation = Literal["newer", "equal", "older", "ambiguous"]
RELATIONS = (NEWER, EQUAL, OLDER, AMBIGUOUS)


def _coerce_serial32(value: object) -> int:
    """严格的 32 位无符号整数校验（拒绝 bool / 浮点 / 字符串）。"""
    # bool 是 int 的子类，需显式排除
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("序列号必须是 32 位无符号整数")
    if not 0 <= value <= MAX_SERIAL:
        raise ValueError(f"序列号必须在 [0, {MAX_SERIAL}] 范围内")
    return value


class ReplicaCurrent(BaseModel):
    """单个副本当前持有的序列号。"""

    name: str = Field(..., min_length=1, description="副本名称（不可重复）")
    current: int = Field(..., description="当前 32 位无符号 SOA 序列号")

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("副本名称不能为空")
        return value

    @field_validator("current", mode="before")
    @classmethod
    def _current_serial32(cls, value: object) -> int:
        return _coerce_serial32(value)


class AuditRequest(BaseModel):
    """POST /audit 请求体。"""

    proposed: int = Field(..., description="拟发布的 32 位无符号 SOA 序列号")
    replicas: List[ReplicaCurrent] = Field(..., min_length=1)

    @field_validator("proposed", mode="before")
    @classmethod
    def _proposed_serial32(cls, value: object) -> int:
        return _coerce_serial32(value)


class AuditItem(BaseModel):
    """单个副本的审计结果。"""

    name: str
    current: int
    proposed: int
    relation: Relation
    forward_distance: int = Field(
        ..., ge=0, le=MAX_SERIAL,
        description="前向模距离 (proposed - current) mod 2^32",
    )
    blocking: bool = Field(
        ..., description="是否阻塞发布：relation 不是 newer 即为 true",
    )


class AuditResponse(BaseModel):
    """POST /audit 响应体。"""

    proposed: int
    results: List[AuditItem]
    blocked: bool = Field(..., description="是否存在任一阻塞副本")
    blocking_replicas: List[str] = Field(
        ..., description="阻塞发布的副本名列表（输入顺序）",
    )


class ProposeRequest(BaseModel):
    """POST /propose 请求体。"""

    replicas: List[ReplicaCurrent] = Field(..., min_length=1)


class ProposeItem(BaseModel):
    """候选值对单个副本的推进情况。"""

    name: str
    current: int
    relation: Relation
    forward_distance: int


class ProposeResponse(BaseModel):
    """POST /propose 响应体（全部副本对该候选均为 newer）。"""

    proposed: int
    max_forward_distance: int = Field(
        ..., ge=1, le=2**31 - 1,
        description="所有副本中最大的前向模距离（被最小化的目标）",
    )
    results: List[ProposeItem]


# ---------------------------------------------------------------------------
# POST /transfer-plan
# ---------------------------------------------------------------------------

TransferMode = Literal["up_to_date", "ixfr", "axfr"]
TRANSFER_MODES = (UP_TO_DATE, IXFR, AXFR)
TransferReason = Literal["gap", "loop", "target_incomparable"]


class IxfrDelta(BaseModel):
    """一条无序 IXFR 增量：from_serial 到 to_serial（须严格 newer）。"""

    from_serial: int = Field(..., description="增量起始 32 位无符号 SOA 序列号")
    to_serial: int = Field(..., description="增量目标 32 位无符号 SOA 序列号")

    @field_validator("from_serial", "to_serial", mode="before")
    @classmethod
    def _serial32_fields(cls, value: object) -> int:
        return _coerce_serial32(value)


class TransferPlanRequest(BaseModel):
    """POST /transfer-plan 请求体。"""

    target: int = Field(..., description="目标 32 位无符号 SOA 序列号")
    replicas: List[ReplicaCurrent] = Field(..., min_length=1)
    deltas: List[IxfrDelta] = Field(
        ..., description="一组无序 IXFR 增量（允许重复，分叉将被拒绝）",
    )

    @field_validator("target", mode="before")
    @classmethod
    def _target_serial32(cls, value: object) -> int:
        return _coerce_serial32(value)


class OrderedDelta(BaseModel):
    """按链路顺序排列好的单条 IXFR 增量。"""

    from_serial: int
    to_serial: int


class TransferPlanItem(BaseModel):
    """单个副本的传输规划结果（顺序与请求中的副本一致）。"""

    name: str
    current: int
    target: int
    mode: TransferMode = Field(
        ...,
        description=(
            "up_to_date：当前值已等于目标；ixfr：存在唯一完整增量链；"
            "axfr：存在缺口/环路或目标不可比较"
        ),
    )
    reason: Optional[TransferReason] = Field(
        ..., description="仅 axfr 时给出原因：gap / loop / target_incomparable",
    )
    ordered_deltas: List[OrderedDelta] = Field(
        ..., description="ixfr 时按从当前值到目标的顺序给出增量；其余模式为空",
    )


class TransferPlanResponse(BaseModel):
    """POST /transfer-plan 响应体。"""

    target: int
    results: List[TransferPlanItem]
