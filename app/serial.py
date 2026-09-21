"""RFC 1982 序列号算术（32 位无符号整数环形空间）。

序列号空间为模 2^32 的环。给定当前值 ``current`` 与拟发布值 ``proposed``，
定义前向（顺时针）模距离 ``d = (proposed - current) mod 2^32``，则：

* ``d == 0``          -> ``equal``      （相等）
* ``1 <= d < 2^31``   -> ``newer``      （严格更新，可比较）
* ``d == 2^31``       -> ``ambiguous``  （正对点，RFC 1982 规定不可比较）
* ``2^31 < d < 2^32`` -> ``older``      （回退，可比较）

参考：RFC 1982 <https://www.rfc-editor.org/rfc/rfc1982>，SERIAL-BOUND = 2^31。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

BITS = 32
MOD = 1 << BITS          # 2^32，序列号空间大小
HALF = 1 << (BITS - 1)   # 2^31，SERIAL-BOUND
MAX_SERIAL = MOD - 1     # 2^32 - 1，合法序列号上界

NEWER = "newer"
EQUAL = "equal"
OLDER = "older"
AMBIGUOUS = "ambiguous"


def forward_distance(current: int, proposed: int) -> int:
    """返回 *proposed* 相对 *current* 的前向模距离，取值 [0, 2^32 - 1]。"""
    return (proposed - current) % MOD


def compare_serial(current: int, proposed: int) -> str:
    """按 RFC 1982 判定 *proposed* 相对 *current* 的关系。

    返回 ``"newer"`` / ``"equal"`` / ``"older"`` / ``"ambiguous"``。
    """
    distance = forward_distance(current, proposed)
    if distance == 0:
        return EQUAL
    if distance == HALF:
        return AMBIGUOUS
    if distance < HALF:
        return NEWER
    return OLDER


def is_strictly_newer(current: int, proposed: int) -> bool:
    """*proposed* 是否对 *current* 严格更新且可比较（1 <= 距离 <= 2^31 - 1）。"""
    return compare_serial(current, proposed) == NEWER


def propose_serial(currents: List[int]) -> Optional[Tuple[int, int]]:
    """推导对所有副本都严格更新且可比较的最佳拟发布值。

    对每个副本 ``c_i``，候选 ``x`` 必须满足
    ``1 <= (x - c_i) mod 2^32 <= 2^31 - 1``。

    优化目标：最小化最大前向距离 ``max_i (x - c_i) mod 2^32``；
    若多个候选同分，取数值最小者。

    可行域是每个副本所允许的开弧 ``{c+1, ..., c+2^31-1}``（模 2^32）
    之交，非空时必为一段连续弧，其最小元素必是某个 ``(c + 1) mod 2^32``；
    而沿弧每前进一步，目标函数恰好增加 1，故只需枚举这些下界候选。

    返回 ``(proposed, max_forward_distance)``；不存在可行候选时返回 ``None``。
    """
    unique_current = set(currents)
    candidates = [(c + 1) % MOD for c in unique_current]

    feasible: List[Tuple[int, int]] = []
    for candidate in candidates:
        distances = [forward_distance(c, candidate) for c in unique_current]
        if all(1 <= d <= HALF - 1 for d in distances):
            feasible.append((max(distances), candidate))

    if not feasible:
        return None

    # 先按最大前向距离排序，同分再按候选数值排序
    max_distance, proposed = min(feasible, key=lambda item: (item[0], item[1]))
    return proposed, max_distance


# ---------------------------------------------------------------------------
# IXFR 增量链（POST /transfer-plan）
# ---------------------------------------------------------------------------

UP_TO_DATE = "up_to_date"
IXFR = "ixfr"
AXFR = "axfr"

# axfr 的 reason
GAP = "gap"
"""增量链存在缺口（某序列号没有对应的下一条增量）。"""
LOOP = "loop"
"""沿增量链行进时回到了已经过的序列号，形成环路。"""
TARGET_INCOMPARABLE = "target_incomparable"
"""副本当前值与目标序列号在 RFC 1982 下不可比较（前向距离恰为 2^31）。"""


class DeltaValidationError(ValueError):
    """增量集合非法：存在非严格 newer 的增量，或同一 from_serial 分叉。"""

    def __init__(self, detail: dict):
        super().__init__(str(detail))
        self.detail = detail


def build_delta_chain(
    deltas: List[Tuple[int, int]],
) -> dict:
    """校验并归一化一组无序 IXFR 增量，返回 ``from_serial -> to_serial`` 映射。

    处理顺序：

    1. 每条增量都必须满足 ``to_serial`` 按 RFC 1982 **严格新于**
       ``from_serial``（前向距离在 ``[1, 2^31-1]``），否则抛出
       ``DeltaValidationError``（error=delta_not_strictly_newer）；
    2. 完全相同的 ``(from_serial, to_serial)`` 重复增量去重；
    3. 同一 ``from_serial`` 指向不同 ``to_serial`` 即链分叉，抛出
       ``DeltaValidationError``（error=delta_fork）。

    校验后每个 from_serial 至多有一个后继，因此任何完整链都是唯一的。
    """
    # 第一步：逐条拒绝非严格 newer 的增量（保留输入顺序，报错确定可复现）
    for from_serial, to_serial in deltas:
        relation = compare_serial(from_serial, to_serial)
        if relation != NEWER:
            raise DeltaValidationError(
                {
                    "error": "delta_not_strictly_newer",
                    "message": (
                        "增量的 to_serial 必须按 RFC 1982 严格新于 from_serial"
                        "（前向距离在 [1, 2^31-1]）"
                    ),
                    "from_serial": from_serial,
                    "to_serial": to_serial,
                    "relation": relation,
                }
            )

    # 第二步 + 第三步：重复去重，分叉拒绝
    chain: dict = {}
    for from_serial, to_serial in deltas:
        if from_serial in chain:
            if chain[from_serial] == to_serial:
                continue  # 重复增量，去重
            raise DeltaValidationError(
                {
                    "error": "delta_fork",
                    "message": (
                        f"同一 from_serial={from_serial} 指向不同的 to_serial，"
                        "增量链不允许分叉"
                    ),
                    "from_serial": from_serial,
                    "to_serials": [chain[from_serial], to_serial],
                }
            )
        chain[from_serial] = to_serial
    return chain


def plan_transfer(
    current: int,
    target: int,
    chain: dict,
) -> Tuple[str, Optional[str], List[Tuple[int, int]]]:
    """为单个副本规划从 *current* 到 *target* 的传输方式。

    *chain* 为 :func:`build_delta_chain` 产出的 ``from_serial -> to_serial``
    映射，每条边都是 RFC 1982 严格推进。沿边逐跳追踪（**不做整数排序**，
    仅以增量自带的环形先后关系为准）：

    * ``current == target``：``(up_to_date, None, [])``；
    * 两端在 RFC 1982 下不可比较（前向距离恰为 2^31）：
      ``(axfr, target_incomparable, [])``，即使存在能连通的边也必须 AXFR；
    * 沿唯一增量链可抵达 *target*：``(ixfr, None, ordered_deltas)``；
    * 某一跳找不到后继：``(axfr, gap, [])``；
    * 行进中重复经过某序列号（环路）：``(axfr, loop, [])``。
    """
    if current == target:
        return UP_TO_DATE, None, []

    if compare_serial(current, target) == AMBIGUOUS:
        return AXFR, TARGET_INCOMPARABLE, []

    ordered: List[Tuple[int, int]] = []
    visited = {current}
    node = current
    while node != target:
        nxt = chain.get(node)
        if nxt is None:
            return AXFR, GAP, []
        if nxt in visited:
            return AXFR, LOOP, []
        ordered.append((node, nxt))
        visited.add(nxt)
        node = nxt
    return IXFR, None, ordered


# ---------------------------------------------------------------------------
# 带字节数的增量保留规划（POST /retention-plan）
# ---------------------------------------------------------------------------

MAX_REPLICAS = 12
"""retention-plan 允许的最大副本数（穷举副本子集的硬上界）。"""

BUDGET = "budget"
"""预算内无法为该副本保留任何到目标的完整增量链：必须 AXFR。"""


def build_sized_delta_chain(
    deltas: List[Tuple[int, int, int]],
) -> Tuple[dict, dict]:
    """校验并归一化一组带字节数的无序 IXFR 增量。

    复用 :func:`build_delta_chain` 的全部规则：先逐条拒绝非严格 newer 的
    增量，再对完全相同的 ``(from_serial, to_serial)`` 去重、对同一
    ``from_serial`` 指向不同 ``to_serial`` 的分叉抛错；在此之上额外校验：

    * 完全相同的重复增量必须携带相同的 ``nbytes``，否则视为同一增量的
      字节数冲突，抛出 ``DeltaValidationError``
      （error=delta_bytes_conflict）。

    返回 ``(chain, sizes)``：``from_serial -> to_serial`` 映射与
    ``(from_serial, to_serial) -> nbytes`` 字节数映射。
    """
    for from_serial, to_serial, _nbytes in deltas:
        relation = compare_serial(from_serial, to_serial)
        if relation != NEWER:
            raise DeltaValidationError(
                {
                    "error": "delta_not_strictly_newer",
                    "message": (
                        "增量的 to_serial 必须按 RFC 1982 严格新于 from_serial"
                        "（前向距离在 [1, 2^31-1]）"
                    ),
                    "from_serial": from_serial,
                    "to_serial": to_serial,
                    "relation": relation,
                }
            )

    chain: dict = {}
    sizes: dict = {}
    for from_serial, to_serial, nbytes in deltas:
        key = (from_serial, to_serial)
        if from_serial in chain:
            if chain[from_serial] != to_serial:
                raise DeltaValidationError(
                    {
                        "error": "delta_fork",
                        "message": (
                            f"同一 from_serial={from_serial} 指向不同的 to_serial，"
                            "增量链不允许分叉"
                        ),
                        "from_serial": from_serial,
                        "to_serials": [chain[from_serial], to_serial],
                    }
                )
            if sizes[key] != nbytes:
                raise DeltaValidationError(
                    {
                        "error": "delta_bytes_conflict",
                        "message": (
                            "完全相同的增量携带了不同的 bytes，"
                            "无法确定该增量的实际字节数"
                        ),
                        "from_serial": from_serial,
                        "to_serial": to_serial,
                        "bytes": [sizes[key], nbytes],
                    }
                )
            continue  # 重复增量且字节数一致，去重
        chain[from_serial] = to_serial
        sizes[key] = nbytes
    return chain, sizes


def plan_retention(
    currents_priorities: List[Tuple[int, int]],
    target: int,
    chain: dict,
    sizes: dict,
    budget: int,
) -> Tuple[
    List[Tuple[int, int]],
    int,
    List[Tuple[str, Optional[str], List[Tuple[int, int]]]],
]:
    """在总字节预算内选择要保留的增量集合。

    *currents_priorities* 为按请求顺序排列的 ``(current, priority)``，
    *priority* 非负；*chain*、*sizes* 由 :func:`build_sized_delta_chain`
    产出；*budget* 为非负总字节预算。副本数不得超过
    :data:`MAX_REPLICAS`。

    先用 :func:`plan_transfer`（复用全部环形链路规则）逐副本判定其在
    **全量**增量集合下的命运：``gap`` / ``loop`` / ``target_incomparable``
    的副本无论保留哪些增量都无法 IXFR；当前值已等于目标的副本天然拥有
    空的完整链，恒为 ``ixfr``（不占用预算）；其余副本各自对应一条唯一
    的完整链，其所需增量的字节并集必须落入预算。

    待保留集合在副本数不超过 12 的前提下通过穷举副本子集求解，依次
    最大化 / 最小化：

    1. 拥有到目标完整链（``ixfr``）的副本 **priority 总和**（最大）；
    2. 覆盖的副本数（多）；
    3. 保留增量占用的总字节（少）；
    4. 保留增量序列的字典序（小），序列按
       ``(from_serial, to_serial)`` 整数元组排序后逐元组比较。

    返回 ``(retained, used_bytes, results)``：保留的有序增量
    （``(from_serial, to_serial)`` 元组列表，按链路字典序）、其总字节数、
    以及按请求顺序排列的逐副本 ``(mode, reason, ordered_deltas)``。
    预算不足以保留任何完整链时保留集合为空（``used_bytes=0``），
    相关副本标记为 ``(axfr, budget, [])``；该情形不报错。
    """
    # 逐副本复用 plan_transfer 的判定，记录其完整链与失败原因。
    # IXFR 与 UP_TO_DATE 都拥有完整链（后者为空链，不占字节）。
    paths: List[Optional[List[Tuple[int, int]]]] = []
    reasons: List[Optional[str]] = []
    for current, _priority in currents_priorities:
        mode, reason, ordered = plan_transfer(current, target, chain)
        if mode in (IXFR, UP_TO_DATE):
            paths.append(ordered)
            reasons.append(None)
        else:  # AXFR
            paths.append(None)
            reasons.append(reason)

    # 可被保留方案覆盖的副本：天然失败的副本不参与子集枚举
    coverable = [i for i, path in enumerate(paths) if path is not None]

    n = len(coverable)
    assert n <= MAX_REPLICAS, "retention-plan 副本数必须 <= 12"

    best: Optional[Tuple] = None
    # 枚举覆盖副本子集（位掩码 0 即不保留任何增量）
    for mask in range(1 << n):
        edge_set: set = set()
        priority_sum = 0
        count = 0
        for bit in range(n):
            if mask & (1 << bit):
                idx = coverable[bit]
                edge_set.update(paths[idx])
                priority_sum += currents_priorities[idx][1]
                count += 1
        used = sum(sizes[edge] for edge in edge_set)
        if used > budget:
            continue
        key = (-priority_sum, -count, used, tuple(sorted(edge_set)))
        if best is None or key < best[0]:
            best = (key, frozenset(edge_set))

    assert best is not None, "空子集（不保留增量）必然可行"
    retained_edges = set(best[1])

    retained = sorted(retained_edges)
    used_bytes = sum(sizes[edge] for edge in retained)

    results: List[Tuple[str, Optional[str], List[Tuple[int, int]]]] = []
    for i, (current, _priority) in enumerate(currents_priorities):
        if paths[i] is None:
            # 天然失败：复用全量集合下的 gap / loop / target_incomparable
            results.append((AXFR, reasons[i], []))
        elif set(paths[i]).issubset(retained_edges):
            # 完整链（含空链）所需增量全部被保留
            results.append((IXFR, None, list(paths[i])))
        else:
            # 存在完整链但保留集合无法覆盖：预算原因
            results.append((AXFR, BUDGET, []))

    return retained, used_bytes, results
