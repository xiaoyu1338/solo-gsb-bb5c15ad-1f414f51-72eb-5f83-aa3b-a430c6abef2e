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
