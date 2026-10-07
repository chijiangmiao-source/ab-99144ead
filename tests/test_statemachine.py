"""状态机代码测试：顺序裁决、规程切换、幂等/冲突、中断恢复等价性。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.statemachine import (  # noqa: E402
    DECISION_INHIBIT,
    DECISION_PASS,
    STATE_VERDICT,
    STATE_WAITING,
    Conflict,
    DrillStore,
)

BASE_THRESHOLD = 10.0
TIGHT_THRESHOLD = 5.0


def make_store(path: str) -> DrillStore:
    return DrillStore(path)


def new_drill(store: DrillStore, drill_id="drill-1"):
    store.create_drill(drill_id, "演练", "P-BASE", BASE_THRESHOLD)


class AdjudicateTests(unittest.TestCase):
    def test_pass_below_threshold(self):
        from app.statemachine import adjudicate
        decision, reason = adjudicate(4.9, 5.0)
        self.assertEqual(decision, DECISION_PASS)
        self.assertIsNone(reason)

    def test_inhibit_above_threshold(self):
        from app.statemachine import adjudicate
        decision, reason = adjudicate(5.1, 5.0)
        self.assertEqual(decision, DECISION_INHIBIT)
        self.assertIn("threshold", reason)


class OrderingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(os.path.join(self.tmp.name, "t.db"))
        new_drill(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_out_of_order_waits_then_drains_in_order(self):
        # 先到序号 2：持久化等待，水位不动
        out = self.store.submit_observation("drill-1", "d-2", 2, 1.0)
        self.assertEqual(out.state, STATE_WAITING)
        self.assertEqual(out.water_level, 0)

        # 序号 1 仍然缺号：继续等待
        out = self.store.submit_observation("drill-1", "d-3", 3, 1.0)
        self.assertEqual(out.state, STATE_WAITING)
        self.assertEqual(out.water_level, 0)

        status = self.store.drill_status("drill-1")
        self.assertEqual([w["seq"] for w in status["waiting"]], [2, 3])

        # 序号 0 到达：只裁决 0，1 仍缺
        out = self.store.submit_observation("drill-1", "d-0", 0, 1.0)
        self.assertEqual(out.state, STATE_VERDICT)
        self.assertEqual(out.verdict["seq"], 0)
        self.assertEqual(out.drained, [])
        self.assertEqual(out.water_level, 1)

        # 序号 1 到达：在同一调用内排空 1、2、3
        out = self.store.submit_observation("drill-1", "d-1", 1, 1.0)
        self.assertEqual([v["seq"] for v in out.drained], [2, 3])
        self.assertEqual(out.water_level, 4)
        status = self.store.drill_status("drill-1")
        self.assertEqual(status["water_level"], 4)
        self.assertEqual(status["waiting"], [])
        self.assertEqual([v["seq"] for v in status["verdicts"]], [0, 1, 2, 3])

    def test_procedure_switch_fixed_after_water_passes(self):
        # 先提交序号 2（读数 7：基线阈值 10 下放行，新阈值 5 下抑制）
        self.store.submit_observation("drill-1", "d-2", 2, 7.0)
        # 水位仍为 0，登记序号 2 起生效的新规程
        self.store.register_procedure(
            "drill-1", 2, "P-TIGHT", TIGHT_THRESHOLD)

        self.store.submit_observation("drill-1", "d-0", 0, 9.0)
        self.store.submit_observation("drill-1", "d-1", 1, 11.0)

        status = self.store.drill_status("drill-1")
        verdicts = {v["seq"]: v for v in status["verdicts"]}
        self.assertEqual(verdicts[0]["procedure_code"], "P-BASE")
        self.assertEqual(verdicts[0]["decision"], DECISION_PASS)
        self.assertEqual(verdicts[1]["procedure_code"], "P-BASE")
        self.assertEqual(verdicts[1]["decision"], DECISION_INHIBIT)
        # 序号 2 固定采用新规程
        self.assertEqual(verdicts[2]["procedure_code"], "P-TIGHT")
        self.assertEqual(verdicts[2]["decision"], DECISION_INHIBIT)

        # 水位越过 2 后补登记更早规程必须拒绝
        with self.assertRaises(Conflict) as cm:
            self.store.register_procedure("drill-1", 1, "P-LATE", 3.0)
        self.assertEqual(cm.exception.kind, "PROCEDURE_LATE")

        # 历史结果不得重算
        status2 = self.store.drill_status("drill-1")
        self.assertEqual(
            [v["adjudicated_at"] for v in status2["verdicts"]],
            [v["adjudicated_at"] for v in status["verdicts"]])
        self.assertEqual(verdicts[1]["procedure_code"], "P-BASE")

    def test_late_registration_after_single_pass(self):
        # 仅裁决了序号 0（水位 1），登记 effective_from=1 仍允许
        self.store.submit_observation("drill-1", "d-0", 0, 1.0)
        self.store.register_procedure("drill-1", 1, "P-1", 2.0)
        # effective_from=0 永远非法（基线已占有）
        with self.assertRaises(Conflict):
            self.store.register_procedure("drill-1", 0, "P-X", 2.0)


class DeliveryIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(os.path.join(self.tmp.name, "t.db"))
        new_drill(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_identical_retransmit_echoes_verdict(self):
        out1 = self.store.submit_observation("drill-1", "d-0", 0, 4.0)
        self.assertFalse(out1.duplicate)
        out2 = self.store.submit_observation("drill-1", "d-0", 0, 4.0)
        self.assertTrue(out2.duplicate)
        self.assertEqual(out2.state, STATE_VERDICT)
        self.assertEqual(
            out2.verdict["adjudicated_at"], out1.verdict["adjudicated_at"])

    def test_retransmit_while_waiting_echoes_waiting(self):
        self.store.submit_observation("drill-1", "d-2", 2, 4.0)
        out = self.store.submit_observation("drill-1", "d-2", 2, 4.0)
        self.assertTrue(out.duplicate)
        self.assertEqual(out.state, STATE_WAITING)

    def test_same_id_changed_content_rejected_without_mutation(self):
        self.store.submit_observation("drill-1", "d-2", 2, 4.0)
        with self.assertRaises(Conflict) as cm:
            self.store.submit_observation("drill-1", "d-2", 2, 9.9)
        self.assertEqual(cm.exception.kind, "DELIVERY_CONTENT_CHANGED")
        with self.assertRaises(Conflict) as cm:
            self.store.submit_observation("drill-1", "d-2", 3, 4.0)
        self.assertEqual(cm.exception.kind, "DELIVERY_CONTENT_CHANGED")

        # 等待记录未被改写：补齐后仍按原读数裁决
        self.store.submit_observation("drill-1", "d-0", 0, 1.0)
        self.store.submit_observation("drill-1", "d-1", 1, 1.0)
        v2 = [v for v in self.store.drill_status("drill-1")["verdicts"]
              if v["seq"] == 2][0]
        self.assertEqual(v2["reading"], 4.0)

    def test_same_sequence_different_content_rejected(self):
        self.store.submit_observation("drill-1", "d-0", 0, 4.0)
        with self.assertRaises(Conflict) as cm:
            self.store.submit_observation("drill-1", "other", 0, 4.0)
        self.assertEqual(cm.exception.kind, "SEQUENCE_TAKEN")
        # 原裁决不动
        v0 = self.store.drill_status("drill-1")["verdicts"][0]
        self.assertEqual(v0["delivery_id"], "d-0")

    def test_first_rejection_recorded(self):
        self.store.submit_observation("drill-1", "d-0", 0, 1.0)
        # 不同标识抢占同序号 → 拒绝
        with self.assertRaises(Conflict):
            self.store.submit_observation("drill-1", "dup", 0, 2.0)
        # 同标识改动读数 → 拒绝
        with self.assertRaises(Conflict):
            self.store.submit_observation("drill-1", "d-0", 0, 2.0)
        status = self.store.drill_status("drill-1")
        self.assertIsNotNone(status["first_rejection"])
        self.assertEqual(
            status["first_rejection"]["kind"], "SEQUENCE_TAKEN")
        self.assertEqual(len(status["rejections"]), 2)

    def test_each_sequence_has_single_immutable_verdict(self):
        self.store.submit_observation("drill-1", "d-0", 0, 1.0)
        self.store.submit_observation("drill-1", "d-0", 0, 1.0)  # 重传
        verdicts = self.store.drill_status("drill-1")["verdicts"]
        self.assertEqual(len(verdicts), 1)

    def test_delivery_id_scoped_to_drill(self):
        new_drill(self.store, "drill-2")
        # 同一投递标识在不同演练中是相互独立的流
        out1 = self.store.submit_observation("drill-1", "shared", 0, 1.0)
        out2 = self.store.submit_observation("drill-2", "shared", 0, 2.0)
        self.assertEqual(out1.state, STATE_VERDICT)
        self.assertEqual(out2.state, STATE_VERDICT)
        self.assertFalse(out2.duplicate)
        self.assertEqual(
            self.store.drill_status("drill-2")["verdicts"][0]["reading"], 2.0)


class RecoveryEquivalenceTests(unittest.TestCase):
    """中断重开后的结果必须与不中断执行一致。"""

    SCENARIO = [
        ("d-2", 2, 7.0),
        ("d-0", 0, 9.0),
        ("d-1", 1, 11.0),
    ]

    def _run_uninterrupted(self, path: str) -> dict:
        store = make_store(path)
        new_drill(store)
        store.submit_observation("drill-1", "d-2", 2, 7.0)
        store.register_procedure("drill-1", 2, "P-TIGHT", TIGHT_THRESHOLD)
        store.submit_observation("drill-1", "d-0", 0, 9.0)
        store.submit_observation("drill-1", "d-1", 1, 11.0)
        return store.drill_status("drill-1")

    def test_reopen_after_enqueue_and_after_advance(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "crash.db")

        # 写入等待队列后中断（丢弃 store，模拟进程退出）
        store = make_store(path)
        new_drill(store)
        store.submit_observation("drill-1", "d-2", 2, 7.0)
        store.register_procedure("drill-1", 2, "P-TIGHT", TIGHT_THRESHOLD)
        del store

        store = make_store(path)   # 重开服务
        store.recover()
        status = store.drill_status("drill-1")
        self.assertEqual(status["water_level"], 0)
        self.assertEqual([w["seq"] for w in status["waiting"]], [2])
        self.assertEqual(status["verdicts"], [])

        # 推进水位到 1 后再次中断
        store.submit_observation("drill-1", "d-0", 0, 9.0)
        ts0 = store.drill_status("drill-1")["verdicts"][0]["adjudicated_at"]
        del store

        store = make_store(path)
        store.recover()
        status = store.drill_status("drill-1")
        self.assertEqual(status["water_level"], 1)
        self.assertEqual([w["seq"] for w in status["waiting"]], [2])
        # 既有裁决时间戳未变（不重算）
        self.assertEqual(
            status["verdicts"][0]["adjudicated_at"], ts0)

        # 补齐缺口并在排空后再次中断重开
        store.submit_observation("drill-1", "d-1", 1, 11.0)
        del store
        store = make_store(path)
        store.recover()
        crashed = store.drill_status("drill-1")

        tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(tmp2.cleanup)
        uninterrupted = self._run_uninterrupted(
            os.path.join(tmp2.name, "fine.db"))

        # 裁决内容（去除时间戳）逐序号一致
        def slim(s):
            return sorted(
                ((v["seq"], v["delivery_id"], v["reading"], v["decision"],
                  v["procedure_code"], v["threshold"], v["reason"])
                 for v in s["verdicts"]))
        self.assertEqual(slim(crashed), slim(uninterrupted))
        self.assertEqual(crashed["water_level"], uninterrupted["water_level"])
        self.assertEqual(crashed["waiting"], uninterrupted["waiting"])

    def test_recover_idempotent(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "t.db")
        store = make_store(path)
        new_drill(store)
        store.submit_observation("drill-1", "d-0", 0, 1.0)
        del store
        store = make_store(path)
        store.recover()
        store.recover()
        self.assertEqual(
            store.drill_status("drill-1")["water_level"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
