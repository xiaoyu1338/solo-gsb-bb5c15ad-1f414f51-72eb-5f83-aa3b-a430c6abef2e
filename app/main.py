"""FastAPI 应用入口：DNS SOA 序列号推进审计 API。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from .schemas import (
    AuditItem,
    AuditRequest,
    AuditResponse,
    OrderedDelta,
    ProposeItem,
    ProposeRequest,
    ProposeResponse,
    RetentionDelta,
    RetentionPlanItem,
    RetentionPlanRequest,
    RetentionPlanResponse,
    TransferPlanItem,
    TransferPlanRequest,
    TransferPlanResponse,
)
from .serial import (
    build_delta_chain,
    build_sized_delta_chain,
    compare_serial,
    DeltaValidationError,
    forward_distance,
    plan_retention,
    plan_transfer,
    propose_serial,
)

app = FastAPI(
    title="DNS SOA 序列号推进审计 API",
    description=(
        "基于 RFC 1982 环形算术，对多个 DNS 副本的 SOA 序列号推进进行审计，"
        "并推导对所有副本都严格更新的最佳拟发布值。"
    ),
    version="1.0.0",
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _check_duplicate_names(names: list[str]) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise HTTPException(
                status_code=422,
                detail={"error": "duplicate_replica_name", "name": name},
            )
        seen.add(name)


@app.post("/audit", response_model=AuditResponse)
def audit(request: AuditRequest) -> AuditResponse:
    """逐一判定拟发布值相对各副本当前序列号的关系。

    * newer：严格更新且可比较（1 <= 前向距离 < 2^31）
    * equal：相等（距离 0）
    * older：回退（2^31 < 距离 < 2^32）
    * ambiguous：差值正好 2^31（正对点，RFC 1982 规定不可比较）

    非 newer 的副本均为阻塞副本。
    """
    _check_duplicate_names([r.name for r in request.replicas])

    results = []
    blocking_replicas = []
    for replica in request.replicas:
        relation = compare_serial(replica.current, request.proposed)
        distance = forward_distance(replica.current, request.proposed)
        blocking = relation != "newer"
        if blocking:
            blocking_replicas.append(replica.name)
        results.append(
            AuditItem(
                name=replica.name,
                current=replica.current,
                proposed=request.proposed,
                relation=relation,
                forward_distance=distance,
                blocking=blocking,
            )
        )

    return AuditResponse(
        proposed=request.proposed,
        results=results,
        blocked=bool(blocking_replicas),
        blocking_replicas=blocking_replicas,
    )


@app.post("/propose", response_model=ProposeResponse)
def propose(request: ProposeRequest) -> ProposeResponse:
    """推导对所有副本都严格更新且可比较的候选序列号。

    最小化最大前向距离；同分取数值最小者。
    若各副本当前序列号无法被任意单一值同时严格推进（例如存在相差 2^31
    的正对点，或序列跨越超过半环），返回 409。
    """
    _check_duplicate_names([r.name for r in request.replicas])

    outcome = propose_serial([r.current for r in request.replicas])
    if outcome is None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "no_valid_proposal",
                "message": (
                    "不存在能对所有副本都严格更新且可比较的候选值："
                    "副本当前序列号跨越半环（含相差 2^31 的正对点）"
                ),
            },
        )

    proposed, max_distance = outcome
    results = [
        ProposeItem(
            name=replica.name,
            current=replica.current,
            relation=compare_serial(replica.current, proposed),
            forward_distance=forward_distance(replica.current, proposed),
        )
        for replica in request.replicas
    ]

    return ProposeResponse(
        proposed=proposed,
        max_forward_distance=max_distance,
        results=results,
    )


@app.post("/transfer-plan", response_model=TransferPlanResponse)
def transfer_plan(request: TransferPlanRequest) -> TransferPlanResponse:
    """规划各副本到目标 SOA 序列号的增量 / 全量传输。

    先对无序增量集合做整体校验（422）：

    * 每条增量的 to_serial 必须按 RFC 1982 严格新于 from_serial；
    * 同一 from_serial 指向不同 to_serial 的分叉增量被拒绝；
    * 完全相同的重复增量去重。

    再为每个副本沿增量链逐跳追踪（遵循 RFC 1982 环形顺序，不做整数排序）：

    * 当前值已等于目标：``up_to_date``；
    * 存在唯一完整链抵达目标：``ixfr``，并返回按序排列的 ordered_deltas；
    * 存在缺口（gap）、环路（loop）或目标与当前值不可比较
      （target_incomparable，前向距离恰为 2^31）：``axfr`` 并给出 reason。
    """
    _check_duplicate_names([r.name for r in request.replicas])

    raw_deltas = [(d.from_serial, d.to_serial) for d in request.deltas]
    try:
        chain = build_delta_chain(raw_deltas)
    except DeltaValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)

    results = []
    for replica in request.replicas:
        mode, reason, ordered = plan_transfer(
            replica.current, request.target, chain
        )
        results.append(
            TransferPlanItem(
                name=replica.name,
                current=replica.current,
                target=request.target,
                mode=mode,
                reason=reason,
                ordered_deltas=[
                    OrderedDelta(from_serial=frm, to_serial=to)
                    for frm, to in ordered
                ],
            )
        )

    return TransferPlanResponse(target=request.target, results=results)


@app.post("/retention-plan", response_model=RetentionPlanResponse)
def retention_plan(request: RetentionPlanRequest) -> RetentionPlanResponse:
    """在字节预算内选择要保留的 IXFR 增量集合。

    复用 :func:`build_delta_chain` 的全部增量合法性 / 环形链路规则，并额外
    要求完全相同的重复增量携带相同 ``bytes``，否则返回 **422**
    （``error=delta_bytes_conflict``）；非严格 newer、分叉同样返回 422。

    副本数不超过 12。先逐副本复用 :func:`plan_transfer` 的追踪判定
    （``gap`` / ``loop`` / ``target_incomparable`` 无论保留哪些增量都只能
    AXFR；当前值已等于目标的副本天然 ``ixfr`` 且不占字节），再穷举副本子集，
    在总字节预算内选择保留集合，依次：

    1. 最大化拥有到目标完整链（``ixfr``）副本的 **priority 总和**；
    2. 最大化覆盖副本数；
    3. 最小化保留增量占用字节；
    4. 最小化保留增量序列字典序（按 ``(from_serial, to_serial)`` 排序）。

    存在完整链但保留集合无法覆盖的副本标记为 ``axfr / budget``。
    预算不足以保留任何完整链时保留集合为空、``used_bytes=0``，仍返回 200。
    """
    _check_duplicate_names([r.name for r in request.replicas])

    raw_deltas = [
        (d.from_serial, d.to_serial, d.bytes) for d in request.deltas
    ]
    try:
        chain, sizes = build_sized_delta_chain(raw_deltas)
    except DeltaValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)

    retained, used_bytes, planned = plan_retention(
        [(r.current, r.priority) for r in request.replicas],
        request.target,
        chain,
        sizes,
        request.budget,
    )

    retained_deltas = [
        RetentionDelta(
            from_serial=frm, to_serial=to, bytes=sizes[(frm, to)]
        )
        for frm, to in retained
    ]

    results = []
    for replica, (mode, reason, ordered) in zip(request.replicas, planned):
        results.append(
            RetentionPlanItem(
                name=replica.name,
                current=replica.current,
                target=request.target,
                priority=replica.priority,
                mode=mode,
                reason=reason,
                ordered_deltas=[
                    OrderedDelta(from_serial=frm, to_serial=to)
                    for frm, to in ordered
                ],
            )
        )

    return RetentionPlanResponse(
        target=request.target,
        budget=request.budget,
        retained_deltas=retained_deltas,
        used_bytes=used_bytes,
        results=results,
    )
