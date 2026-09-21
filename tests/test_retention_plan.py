"""POST /retention-plan 增量保留规划单元测试（仅依赖标准库 + 本项目模块）。

运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from app.serial import (
    HALF,
    MAX_SERIAL,
    MOD,
    AXFR,
    GAP,
    IXFR,
    LOOP,
    TARGET_INCOMPARABLE,
    BUDGET,
    DeltaValidationError,
    build_sized_delta_chain,
    plan_retention,
)


def _two_paths_to_target():
    """两条互不相干、都能抵达 target=3 的链。

    * 1 -> 2 -> 3：共 20 字节；
    * 10 -> HALF+5 -> 3（跨半环回绕）：共 10 字节。
    """
    mid = HALF + 5
    deltas = [(1, 2, 10), (2, 3, 10), (10, mid, 5), (mid, 3, 5)]
    chain, sizes = build_sized_delta_chain(deltas)
    return chain, sizes, mid


class BuildSizedDeltaChainTests(unittest.TestCase):
    def test_dedup_identical_deltas_same_bytes(self):
        chain, sizes = build_sized_delta_chain(
            [(1, 2, 7), (1, 2, 7), (2, 3, 1)]
        )
        self.assertEqual(chain, {1: 2, 2: 3})
        self.assertEqual(sizes, {(1, 2): 7, (2, 3): 1})

    def test_reject_bytes_conflict(self):
        with self.assertRaises(DeltaValidationError) as ctx:
            build_sized_delta_chain([(1, 2, 3), (1, 2, 4)])
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "delta_bytes_conflict")
        self.assertEqual(detail["from_serial"], 1)
        self.assertEqual(detail["to_serial"], 2)
        self.assertEqual(detail["bytes"], [3, 4])

    def test_still_reject_fork(self):
        with self.assertRaises(DeltaValidationError) as ctx:
            build_sized_delta_chain([(1, 2, 3), (1, 5, 4)])
        self.assertEqual(ctx.exception.detail["error"], "delta_fork")

    def test_fork_takes_precedence_over_bytes(self):
        # 同一 from 指向不同 to：先以分叉拒绝
        with self.assertRaises(DeltaValidationError) as ctx:
            build_sized_delta_chain([(1, 2, 3), (1, 5, 4), (1, 2, 9)])
        self.assertEqual(ctx.exception.detail["error"], "delta_fork")

    def test_still_reject_non_newer(self):
        with self.assertRaises(DeltaValidationError) as ctx:
            build_sized_delta_chain([(0, HALF, 10)])
        self.assertEqual(
            ctx.exception.detail["error"], "delta_not_strictly_newer"
        )

    def test_non_newer_precedes_all_other_checks(self):
        # (1,1) 与 (1,2) 既可能被视作分叉 / 字节冲突，非严格 newer 优先
        with self.assertRaises(DeltaValidationError) as ctx:
            build_sized_delta_chain([(1, 2, 3), (1, 1, 3)])
        self.assertEqual(
            ctx.exception.detail["error"], "delta_not_strictly_newer"
        )

    def test_wrap_around_sized_delta(self):
        chain, sizes = build_sized_delta_chain([(MAX_SERIAL, 0, 42)])
        self.assertEqual(chain, {MAX_SERIAL: 0})
        self.assertEqual(sizes, {(MAX_SERIAL, 0): 42})


class PlanRetentionTests(unittest.TestCase):
    def test_priority_sum_is_primary_objective(self):
        # 预算 25：单独保留任一条链都可行（20 或 10 字节），但无法同时
        # 保留（共 30）。选择 priority 总和最大者：高 priority 副本的
        # 20 字节链胜出，即便它占用字节更多、覆盖副本数相同。
        chain, sizes, _ = _two_paths_to_target()
        retained, used, results = plan_retention(
            [(1, 9), (10, 1)], 3, chain, sizes, 25
        )
        self.assertEqual(retained, [(1, 2), (2, 3)])
        self.assertEqual(used, 20)
        self.assertEqual([r[0] for r in results], [IXFR, AXFR])
        self.assertEqual(results[1][1], BUDGET)

        # 对照：priority 对调后保留 10 字节链
        retained, used, results = plan_retention(
            [(1, 1), (10, 9)], 3, chain, sizes, 25
        )
        self.assertEqual(used, 10)
        self.assertEqual([r[0] for r in results], [AXFR, IXFR])

    def test_lower_priority_path_wins_when_other_unaffordable(self):
        chain, sizes, mid = _two_paths_to_target()
        retained, used, results = plan_retention(
            [(1, 1), (10, 2)], 3, chain, sizes, 15
        )
        self.assertEqual(retained, [(10, mid), (mid, 3)])
        self.assertEqual(used, 10)
        self.assertEqual(results[0][:2], (AXFR, BUDGET))
        self.assertEqual(results[1][0], IXFR)
        self.assertEqual(results[1][2], [(10, mid), (mid, 3)])

    def test_coverage_count_is_second_tiebreak(self):
        # priority 总和相同：覆盖两个副本（共享前缀，总字节更省）胜
        deltas = [(1, 3, 6), (2, 3, 6)]
        chain, sizes = build_sized_delta_chain(deltas)
        # 单副本路径各 6 字节；预算 6 只能二选一，priority 相同取字典序小者
        retained, _used, results = plan_retention(
            [(1, 1), (2, 1)], 3, chain, sizes, 6
        )
        self.assertEqual(retained, [(1, 3)])
        self.assertEqual(results[0][0], IXFR)
        self.assertEqual(results[1][:2], (AXFR, BUDGET))

        # 共享前缀时两副本可以同时覆盖（priority 和更大）
        shared = build_sized_delta_chain(
            [(1, 2, 3), (2, 3, 3)]
        )
        retained, used, results = plan_retention(
            [(1, 1), (2, 1)], 3, *shared, 6
        )
        self.assertEqual(retained, [(1, 2), (2, 3)])
        self.assertEqual(used, 6)
        self.assertTrue(all(r[0] == IXFR for r in results))

    def test_bytes_is_third_tiebreak(self):
        # 两条链 priority / 覆盖数相同，保留字节少者胜（同一边终点）
        deltas = [(1, 9, 5), (2, 9, 3)]
        chain, sizes = build_sized_delta_chain(deltas)
        retained, used, _ = plan_retention(
            [(1, 1), (2, 1)], 9, chain, sizes, 5
        )
        self.assertEqual(retained, [(2, 9)])
        self.assertEqual(used, 3)

    def test_lex_order_is_fourth_tiebreak(self):
        # 字节、priority、覆盖数全同：增量序列字典序小者胜
        deltas = [(1, 9, 5), (2, 9, 5)]
        chain, sizes = build_sized_delta_chain(deltas)
        retained, _used, results = plan_retention(
            [(1, 1), (2, 1)], 9, chain, sizes, 5
        )
        self.assertEqual(retained, [(1, 9)])
        self.assertEqual(results[0][0], IXFR)
        self.assertEqual(results[1][:2], (AXFR, BUDGET))

    def test_shared_prefix_counts_bytes_once(self):
        chain, sizes = build_sized_delta_chain(
            [(1, 2, 10), (2, 3, 10), (3, 4, 10)]
        )
        retained, used, results = plan_retention(
            [(1, 5), (2, 1)], 4, chain, sizes, 30
        )
        # 1->2->3->4 同时服务两个副本，并集只算一次字节
        self.assertEqual(retained, [(1, 2), (2, 3), (3, 4)])
        self.assertEqual(used, 30)
        self.assertEqual(
            [r[2] for r in results],
            [[(1, 2), (2, 3), (3, 4)], [(2, 3), (3, 4)]],
        )

    def test_budget_zero_keeps_only_zero_byte_chains(self):
        chain, sizes = build_sized_delta_chain([(1, 2, 0), (2, 3, 0)])
        retained, used, results = plan_retention(
            [(1, 1)], 3, chain, sizes, 0
        )
        self.assertEqual(retained, [(1, 2), (2, 3)])
        self.assertEqual(used, 0)
        self.assertEqual(results[0][0], IXFR)

        chain2, sizes2 = build_sized_delta_chain([(1, 2, 1)])
        retained, used, results = plan_retention(
            [(1, 1)], 2, chain2, sizes2, 0
        )
        self.assertEqual((retained, used), ([], 0))
        self.assertEqual(results[0][:2], (AXFR, BUDGET))

    def test_no_affordable_chain_still_returns_empty_plan(self):
        chain, sizes = build_sized_delta_chain([(1, 2, 10)])
        retained, used, results = plan_retention(
            [(1, 1), (9, 1)], 2, chain, sizes, 5
        )
        # 预算无法保留任何完整链：空保留集合，但不报错
        self.assertEqual((retained, used), ([], 0))
        self.assertEqual(results[0][:2], (AXFR, BUDGET))
        self.assertEqual(results[1][:2], (AXFR, GAP))

    def test_up_to_date_replica_is_always_ixfr_empty_chain(self):
        chain, sizes = build_sized_delta_chain([(1, 2, 10), (2, 5, 10)])
        retained, used, results = plan_retention(
            [(5, 7), (1, 1)], 5, chain, sizes, 0
        )
        self.assertEqual((retained, used), ([], 0))
        self.assertEqual(results[0], (IXFR, None, []))
        self.assertEqual(results[1][:2], (AXFR, BUDGET))

    def test_gap_loop_incomparable_are_structural_not_budget(self):
        deltas = [
            (1, 2, 1),
            (2, HALF - 1, 1),
            (HALF - 1, MAX_SERIAL - 1, 1),
            (MAX_SERIAL - 1, 2, 1),
        ]
        chain, sizes = build_sized_delta_chain(deltas)
        _retained, _used, results = plan_retention(
            [(9, 1), (1, 1), ((100 - HALF) % MOD, 1)],
            100,
            chain,
            sizes,
            1000,
        )
        self.assertEqual(results[0][:2], (AXFR, GAP))
        self.assertEqual(results[1][:2], (AXFR, LOOP))
        self.assertEqual(results[2][:2], (AXFR, TARGET_INCOMPARABLE))
        for _, reason, ordered in results:
            self.assertIsNotNone(reason)
            self.assertEqual(ordered, [])

    def test_retained_sorted_canonically_but_ordered_deltas_follow_chain(self):
        # 输入乱序 + 跨回绕：保留序列按 (from,to) 字典序，
        # 副本的 ordered_deltas 按实际链路顺序
        deltas = [
            (0, 1, 1),
            (MAX_SERIAL, 0, 1),
            (MAX_SERIAL - 1, MAX_SERIAL, 1),
        ]
        chain, sizes = build_sized_delta_chain(deltas)
        retained, used, results = plan_retention(
            [(MAX_SERIAL - 1, 1)], 1, chain, sizes, 10
        )
        # 保留序列按 (from,to) 整数元组字典序：
        # (0,1) < (MAX-1,MAX) < (MAX,0) —— 与链路顺序无关
        self.assertEqual(
            retained,
            [(0, 1), (MAX_SERIAL - 1, MAX_SERIAL), (MAX_SERIAL, 0)],
        )
        self.assertEqual(used, 3)
        self.assertEqual(
            results[0][2],
            [(MAX_SERIAL - 1, MAX_SERIAL), (MAX_SERIAL, 0), (0, 1)],
        )

    def test_all_twelve_replicas_are_enumerated(self):
        # 12 个副本各有独立的 1 字节链汇入同一 target，预算足够全部保留
        target = HALF
        deltas = [(i, target, 1) for i in range(1, 13)]
        chain, sizes = build_sized_delta_chain(deltas)
        replicas = [(i, 1) for i in range(1, 13)]
        retained, used, results = plan_retention(
            replicas, target, chain, sizes, 12
        )
        self.assertEqual(len(retained), 12)
        self.assertEqual(used, 12)
        self.assertTrue(all(r[0] == IXFR for r in results))

        # 预算只够 3 字节时，priority 相同 -> 覆盖数相同 -> 字节相同 ->
        # 字典序最小的 3 条边（from_serial = 1, 2, 3）胜出
        retained, used, results = plan_retention(
            replicas, target, chain, sizes, 3
        )
        self.assertEqual([frm for frm, _ in retained], [1, 2, 3])
        self.assertEqual(used, 3)
        ixfr_names = [i for i, r in enumerate(results) if r[0] == IXFR]
        self.assertEqual(ixfr_names, [0, 1, 2])


# 路由层测试：直接以函数方式调用 FastAPI 端点（无需 httpx/TestClient）。
try:
    from fastapi import HTTPException as _HTTPException
    from pydantic import ValidationError as _ValidationError

    from app.main import retention_plan
    from app.schemas import RetentionPlanRequest

    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover - CI 中 fastapi 必装
    _HAS_FASTAPI = False


@unittest.skipUnless(_HAS_FASTAPI, "fastapi 未安装")
class RetentionPlanEndpointTests(unittest.TestCase):
    def _request(self, payload):
        return retention_plan(RetentionPlanRequest.model_validate(payload))

    def _full_payload(self, budget=15):
        mid = HALF + 5
        return {
            "target": 3,
            "replicas": [
                {"name": "ns-a", "current": 1, "priority": 1},    # 20 字节链
                {"name": "ns-b", "current": 10, "priority": 2},   # 10 字节链
                {"name": "ns-c", "current": 3, "priority": 9},    # up_to_date
                {"name": "ns-d", "current": 77, "priority": 1},   # gap
            ],
            "deltas": [
                {"from_serial": 2, "to_serial": 3, "bytes": 10},
                {"from_serial": 1, "to_serial": 2, "bytes": 10},
                {"from_serial": mid, "to_serial": 3, "bytes": 5},
                {"from_serial": 10, "to_serial": mid, "bytes": 5},
                {"from_serial": 10, "to_serial": mid, "bytes": 5},  # 重复
            ],
            "budget": budget,
        }

    def test_endpoint_full_flow(self):
        mid = HALF + 5
        resp = self._request(self._full_payload())
        self.assertEqual(
            [(d.from_serial, d.to_serial, d.bytes) for d in resp.retained_deltas],
            [(10, mid, 5), (mid, 3, 5)],
        )
        self.assertEqual(resp.used_bytes, 10)

        a, b, c, d = resp.results
        self.assertEqual((a.mode, a.reason), ("axfr", "budget"))
        self.assertEqual(a.ordered_deltas, [])

        self.assertEqual(b.mode, IXFR)
        self.assertIsNone(b.reason)
        self.assertEqual(b.priority, 2)
        self.assertEqual(
            [(x.from_serial, x.to_serial) for x in b.ordered_deltas],
            [(10, mid), (mid, 3)],
        )

        # 当前值已等于目标：ixfr + 空链，不占任何字节
        self.assertEqual((c.mode, c.reason, c.ordered_deltas),
                         (IXFR, None, []))

        self.assertEqual((d.mode, d.reason), ("axfr", "gap"))

    def test_endpoint_empty_retention_is_200(self):
        resp = self._request(self._full_payload(budget=3))
        self.assertEqual(resp.retained_deltas, [])
        self.assertEqual(resp.used_bytes, 0)
        modes = {(r.name, r.mode, r.reason) for r in resp.results}
        self.assertIn(("ns-a", "axfr", "budget"), modes)
        self.assertIn(("ns-b", "axfr", "budget"), modes)
        # up_to_date 不受预算影响
        self.assertIn(("ns-c", IXFR, None), modes)
        self.assertIn(("ns-d", "axfr", "gap"), modes)

    def test_endpoint_rejects_bytes_conflict_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "target": 2,
                    "replicas": [
                        {"name": "ns-a", "current": 1, "priority": 0}
                    ],
                    "deltas": [
                        {"from_serial": 1, "to_serial": 2, "bytes": 3},
                        {"from_serial": 1, "to_serial": 2, "bytes": 4},
                    ],
                    "budget": 10,
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail["error"], "delta_bytes_conflict")
        self.assertEqual(ctx.exception.detail["bytes"], [3, 4])

    def test_endpoint_rejects_fork_and_non_newer_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "target": 2,
                    "replicas": [
                        {"name": "ns-a", "current": 1, "priority": 0}
                    ],
                    "deltas": [
                        {"from_serial": 1, "to_serial": 2, "bytes": 1},
                        {"from_serial": 1, "to_serial": 9, "bytes": 1},
                    ],
                    "budget": 10,
                }
            )
        self.assertEqual(ctx.exception.detail["error"], "delta_fork")

        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "target": HALF,
                    "replicas": [
                        {"name": "ns-a", "current": 0, "priority": 0}
                    ],
                    "deltas": [
                        {"from_serial": 0, "to_serial": HALF, "bytes": 1}
                    ],
                    "budget": 10,
                }
            )
        self.assertEqual(
            ctx.exception.detail["error"], "delta_not_strictly_newer"
        )

    def test_endpoint_duplicate_replica_name_422(self):
        with self.assertRaises(_HTTPException) as ctx:
            self._request(
                {
                    "target": 2,
                    "replicas": [
                        {"name": "ns-a", "current": 1, "priority": 0},
                        {"name": "ns-a", "current": 2, "priority": 0},
                    ],
                    "deltas": [],
                    "budget": 10,
                }
            )
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail["error"], "duplicate_replica_name"
        )

    def test_schema_rejects_more_than_twelve_replicas(self):
        with self.assertRaises(_ValidationError):
            RetentionPlanRequest.model_validate(
                {
                    "target": 2,
                    "replicas": [
                        {"name": f"ns-{i}", "current": 1, "priority": 0}
                        for i in range(13)
                    ],
                    "deltas": [],
                    "budget": 10,
                }
            )

    def test_schema_accepts_twelve_replicas(self):
        req = RetentionPlanRequest.model_validate(
            {
                "target": 2,
                "replicas": [
                    {"name": f"ns-{i}", "current": 1, "priority": 0}
                    for i in range(12)
                ],
                "deltas": [],
                "budget": 10,
            }
        )
        self.assertEqual(len(req.replicas), 12)

    def test_schema_rejects_negative_and_non_integer_fields(self):
        base = {
            "target": 2,
            "replicas": [{"name": "ns-a", "current": 1, "priority": 0}],
            "deltas": [],
            "budget": 10,
        }
        for patch in (
            {"budget": -1},
            {"budget": 1.5},
            {"budget": True},
        ):
            with self.assertRaises(_ValidationError):
                RetentionPlanRequest.model_validate({**base, **patch})

        with self.assertRaises(_ValidationError):
            RetentionPlanRequest.model_validate(
                {
                    **base,
                    "replicas": [
                        {"name": "ns-a", "current": 1, "priority": -1}
                    ],
                }
            )

        with self.assertRaises(_ValidationError):
            RetentionPlanRequest.model_validate(
                {
                    **base,
                    "deltas": [
                        {"from_serial": 1, "to_serial": 2, "bytes": -1}
                    ],
                }
            )

        with self.assertRaises(_ValidationError):
            RetentionPlanRequest.model_validate(
                {
                    **base,
                    "deltas": [
                        {"from_serial": 1, "to_serial": 2, "bytes": True}
                    ],
                }
            )


if __name__ == "__main__":
    unittest.main()
