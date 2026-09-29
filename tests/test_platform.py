"""测试：派工冲突（含并发）、步骤缺失、合单边界、重量修订、角色权限。

直接运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.db import DB
from app.seed import make_photo_svg, seed
from app.services import (
    ADJACENT_LIMIT, STEPS, Service, ServiceError,
)


def iso(h_from_now: float) -> str:
    return (datetime.now() + timedelta(hours=h_from_now)).replace(microsecond=0).isoformat()


class PlatformCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "platform.db"
        self.up_dir = root / "uploads"
        seed(self.db_path, self.up_dir)
        self.db = DB(self.db_path)
        self.db.init()
        self.svc = Service(self.db, self.up_dir)
        # seed 中的用户：1 巡查 2 调度 3 班组 4 验收 5 主管
        self.PATROL, self.DISPATCH, self.CREW, self.INSPECT, self.SUP = 1, 2, 3, 4, 5

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ---------- 辅助 ----------
    def _inlet_id(self, code: str) -> int:
        return self.db.one("SELECT id FROM inlets WHERE code=?", (code,))["id"]

    def _fresh_order(self, codes=("YS-0101",), level: int = 2, user: int | None = None) -> int:
        ids = [self._inlet_id(c) for c in codes]
        return self.svc.create_order(user or self.PATROL, ids, level, "测试单")["id"]

    def _dispatch(self, oid: int, crew: int = 1, vehicle: int = 1,
                  start_h: float = 24, end_h: float = 26, user: int | None = None):
        return self.svc.dispatch(user or self.DISPATCH, oid, crew, vehicle,
                                 iso(start_h), iso(end_h))

    def _photos(self, oid: int, iid: int, code: str = "YS-TEST") -> None:
        for phase in ("before", "after"):
            self.svc.save_photo(self.CREW, oid, iid, phase, f"{phase}.svg",
                                make_photo_svg(code, phase))

    def _complete_inlet(self, oid: int, iid: int, code: str = "YS-TEST",
                        bags: int = 3) -> None:
        """做完一个点位的全部现场材料：4 道工序 + 前后照片 + 袋数。"""
        for step in STEPS:
            self.svc.record_step(self.CREW, oid, iid, step)
        self._photos(oid, iid, code)
        self.svc.set_bag_count(self.CREW, oid, iid, bags)

    def _to_working(self, codes=("YS-0101",)) -> tuple[int, list[int]]:
        ids = [self._inlet_id(c) for c in codes]
        oid = self._fresh_order(codes)
        self._dispatch(oid)
        self.svc.start_work(self.CREW, oid)
        return oid, ids


# =====================================================================
# 1. 派工冲突（含并发分配同一班组）
# =====================================================================
class TestDispatchConflict(PlatformCase):
    def test_overlapping_windows_rejected(self):
        o1 = self._fresh_order(("YS-0101",))
        o2 = self._fresh_order(("YS-0105",))
        self._dispatch(o1, crew=1, start_h=24, end_h=26)
        with self.assertRaises(ServiceError) as cm:
            self._dispatch(o2, crew=1, start_h=25, end_h=27)
        self.assertEqual(cm.exception.code, "crew_conflict")
        self.assertEqual(cm.exception.status, 409)

    def test_touching_at_boundary_ok(self):
        o1 = self._fresh_order(("YS-0101",))
        o2 = self._fresh_order(("YS-0105",))
        self._dispatch(o1, crew=1, start_h=24, end_h=26)
        # 首尾相接（半开区间不重叠）应放行
        self._dispatch(o2, crew=1, start_h=26, end_h=28)

    def test_different_crew_same_time_ok(self):
        o1 = self._fresh_order(("YS-0101",))
        o2 = self._fresh_order(("YS-0105",))
        self._dispatch(o1, crew=1, start_h=24, end_h=26)
        self._dispatch(o2, crew=2, start_h=24, end_h=26)

    def test_concurrent_dispatch_only_one_wins(self):
        """并发把多张单分给同一班组同一时段：恰好一张成功。"""
        codes = ["YS-0101", "YS-0105", "YS-0201", "YS-0401", "YS-0203"]
        oids = [self._fresh_order((c,)) for c in codes]
        errors, winners = [], []
        barrier = threading.Barrier(len(oids))

        def worker(oid: int):
            barrier.wait()
            try:
                self.svc.dispatch(self.DISPATCH, oid, 1, 1, iso(30), iso(32))
                winners.append(oid)
            except ServiceError as e:
                errors.append(e.code)

        threads = [threading.Thread(target=worker, args=(o,)) for o in oids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(winners), 1, f"应有且仅有一张派工成功，实际 {winners}")
        self.assertEqual(sorted(set(errors)), ["crew_conflict"])
        self.assertEqual(len(errors), len(oids) - 1)
        row = self.db.one("SELECT status FROM work_orders WHERE id=?", (winners[0],))
        self.assertEqual(row["status"], "dispatched")

    def test_rejected_order_keeps_same_id_on_redispatch(self):
        """验收退回后继续沿用原工单，而不是新开单。"""
        oid = self._fresh_order(("YS-0101",))
        self._dispatch(oid)
        self.svc.start_work(self.CREW, oid)
        iid = self._inlet_id("YS-0101")
        self._complete_inlet(oid, iid, "YS-0101")
        self.svc.submit_acceptance(self.CREW, oid, [iid])
        self.svc.review_acceptance(self.INSPECT, oid, iid, False, "冲洗不彻底")
        before = self.db.one("SELECT COUNT(*) c FROM work_orders")["c"]
        # 退回后仍可对同一张单重新派工
        self.svc.dispatch(self.DISPATCH, oid, 2, 2, iso(48), iso(50))
        after = self.db.one("SELECT COUNT(*) c FROM work_orders")["c"]
        self.assertEqual(before, after)
        self.assertEqual(self.db.one(
            "SELECT status FROM work_orders WHERE id=?", (oid,))["status"], "dispatched")


# =====================================================================
# 2. 步骤 / 材料缺失门禁
# =====================================================================
class TestStepCompletionGate(PlatformCase):
    def test_step_must_follow_sequence(self):
        oid, iids = self._to_working(("YS-0101",))
        with self.assertRaises(ServiceError) as cm:
            self.svc.record_step(self.CREW, oid, iids[0], "clean")
        self.assertEqual(cm.exception.code, "step_order")
        self.svc.record_step(self.CREW, oid, iids[0], "open")
        self.svc.record_step(self.CREW, oid, iids[0], "clean")
        with self.assertRaises(ServiceError) as cm:
            self.svc.record_step(self.CREW, oid, iids[0], "reset")
        self.assertIn("冲洗", cm.exception.message)

    def test_duplicate_step_rejected(self):
        oid, iids = self._to_working(("YS-0101",))
        self.svc.record_step(self.CREW, oid, iids[0], "open")
        with self.assertRaises(ServiceError) as cm:
            self.svc.record_step(self.CREW, oid, iids[0], "open")
        self.assertEqual(cm.exception.code, "step_exists")

    def test_submit_blocked_when_missing_photos_bags_reset(self):
        oid, iids = self._to_working(("YS-0101",))
        iid = iids[0]
        # 只开盖+清掏
        self.svc.record_step(self.CREW, oid, iid, "open")
        self.svc.record_step(self.CREW, oid, iid, "clean")
        with self.assertRaises(ServiceError) as cm:
            self.svc.submit_acceptance(self.CREW, oid, [iid])
        self.assertEqual(cm.exception.status, 422)
        missing = cm.exception.payload["blocked"]["YS-0101"]
        for must in ("清掏前照片", "清掏后照片", "淤泥袋数", "复位检查"):
            self.assertIn(must, missing)
        self.assertIn("冲洗工序", missing)
        self.assertIn("复位工序", missing)
        # 工单未进入待验收
        self.assertEqual(self.db.one(
            "SELECT status FROM work_orders WHERE id=?", (oid,))["status"], "working")

    def test_full_materials_pass_gate_and_inlet_back_to_normal(self):
        oid, iids = self._to_working(("YS-0101",))
        iid = iids[0]
        self._complete_inlet(oid, iid, "YS-0101")
        res = self.svc.submit_acceptance(self.CREW, oid, [iid])
        self.assertEqual(res["order_status"], "submitted")
        self.svc.review_acceptance(self.INSPECT, oid, iid, True, "合格")
        self.assertEqual(self.db.one(
            "SELECT status,block_level FROM inlets WHERE id=?", (iid,))["status"], "normal")
        self.assertEqual(self.db.one(
            "SELECT status FROM work_orders WHERE id=?", (oid,))["status"], "accepted")

    def test_reject_then_continue_on_same_order(self):
        oid, iids = self._to_working(("YS-0101",))
        iid = iids[0]
        self._complete_inlet(oid, iid, "YS-0101")
        self.svc.submit_acceptance(self.CREW, oid, [iid])
        self.svc.review_acceptance(self.INSPECT, oid, iid, False, "井壁挂淤")
        # 原单继续整改：工序不可重复记录（沿用已有记录），可换后照片重新申请
        self.svc.save_photo(self.CREW, oid, iid, "after", "after.svg",
                            make_photo_svg("YS-0101", "after"))
        self.svc.submit_acceptance(self.CREW, oid, [iid])
        r = self.svc.review_acceptance(self.INSPECT, oid, iid, True, "整改到位")
        self.assertEqual(r["order_status"], "accepted")
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) c FROM acceptance_records WHERE inlet_id=?", (iid,))["c"], 2)

    def test_reject_requires_comment(self):
        oid, iids = self._to_working(("YS-0101",))
        iid = iids[0]
        self._complete_inlet(oid, iid, "YS-0101")
        self.svc.submit_acceptance(self.CREW, oid, [iid])
        with self.assertRaises(ServiceError) as cm:
            self.svc.review_acceptance(self.INSPECT, oid, iid, False, "")
        self.assertEqual(cm.exception.code, "reason_required")


# =====================================================================
# 3. 相邻合单边界（合派车不合验收）
# =====================================================================
class TestMergeBoundary(PlatformCase):
    def _two_pending(self, c1: str, c2: str) -> tuple[int, int]:
        return self._fresh_order((c1,)), self._fresh_order((c2,))

    def test_same_segment_merges(self):
        # YS-0302 与 YS-0303 同在柳岸街
        a, b = self._two_pending("YS-0302", "YS-0303")
        merged = self.svc.merge_orders(self.DISPATCH, [a, b])
        self.assertEqual(merged["id"], a)
        self.assertEqual(sorted(i["code"] for i in merged["inlets"]),
                         ["YS-0302", "YS-0303"])
        self.assertEqual(self.db.one(
            "SELECT status FROM work_orders WHERE id=?", (b,))["status"], "cancelled")

    def test_within_distance_merges_across_segments(self):
        # YS-0104(570,185) 与 YS-0402(562,250) 不同路段但距离 < 120m
        self.assertLess(
            ((570 - 562) ** 2 + (185 - 250) ** 2) ** 0.5, ADJACENT_LIMIT)
        # YS-0104 在样例作业单 WO-0004 中，先释放
        self.db.update("work_orders", "id=?", (4,), status="cancelled")
        a, b = self._two_pending("YS-0104", "YS-0402")
        merged = self.svc.merge_orders(self.DISPATCH, [a, b])
        self.assertEqual(sorted(i["code"] for i in merged["inlets"]),
                         ["YS-0104", "YS-0402"])

    def test_far_apart_rejected(self):
        # YS-0101(120,180) 与 YS-0303(740,425) 不同路段且远超阈值
        a, b = self._two_pending("YS-0101", "YS-0303")
        with self.assertRaises(ServiceError) as cm:
            self.svc.merge_orders(self.DISPATCH, [a, b])
        self.assertEqual(cm.exception.code, "merge_not_adjacent")
        self.assertEqual(cm.exception.status, 422)
        # 两单都仍待派工，未被破坏
        for oid in (a, b):
            self.assertEqual(self.db.one(
                "SELECT status FROM work_orders WHERE id=?", (oid,))["status"], "pending")

    def test_merge_needs_two_pending(self):
        a = self._fresh_order(("YS-0302",))
        with self.assertRaises(ServiceError):
            self.svc.merge_orders(self.DISPATCH, [a])
        b = self._fresh_order(("YS-0303",))
        self._dispatch(a)
        with self.assertRaises(ServiceError) as cm:
            self.svc.merge_orders(self.DISPATCH, [a, b])
        self.assertEqual(cm.exception.code, "merge_state")

    def test_merged_orders_reviewed_independently(self):
        """合单只合派车：一个点退回不能整单通过，另一个点可独立通过。"""
        a, b = self._two_pending("YS-0302", "YS-0303")
        iid_a, iid_b = self._inlet_id("YS-0302"), self._inlet_id("YS-0303")
        merged = self.svc.merge_orders(self.DISPATCH, [a, b])
        oid = merged["id"]
        self._dispatch(oid, crew=2, vehicle=2, start_h=24, end_h=26)
        self.svc.start_work(self.CREW, oid)
        self._complete_inlet(oid, iid_a, "YS-0302")
        self._complete_inlet(oid, iid_b, "YS-0303")
        # 分点申请
        self.svc.submit_acceptance(self.CREW, oid, [iid_a])
        self.svc.submit_acceptance(self.CREW, oid, [iid_b])
        # A 点退回：工单退回，但 B 点已申请状态不变
        self.svc.review_acceptance(self.INSPECT, oid, iid_a, False, "A 点需返工")
        li_b = self.db.one(
            "SELECT submit_status FROM order_inlets WHERE order_id=? AND inlet_id=?",
            (oid, iid_b))["submit_status"]
        self.assertEqual(li_b, "submitted")
        # B 点仍然可以独立通过；此时工单还有 A 点未通过
        r = self.svc.review_acceptance(self.INSPECT, oid, iid_b, True, "B 点合格")
        self.assertEqual(r["result"], "passed")
        self.assertEqual(r["remaining"], 1)
        self.assertNotEqual(r["order_status"], "accepted")
        self.assertEqual(self.db.one(
            "SELECT status FROM inlets WHERE id=?", (iid_b,))["status"], "normal")
        self.assertNotEqual(self.db.one(
            "SELECT status FROM inlets WHERE id=?", (iid_a,))["status"], "normal")


# =====================================================================
# 4. 重量修订（主管 + 原因 + 审计）
# =====================================================================
class TestWeightRevision(PlatformCase):
    def _weighed_trip(self) -> int:
        oid = self._fresh_order(("YS-0101",))
        self._dispatch(oid, crew=2, vehicle=1, start_h=24, end_h=26)
        self.svc.start_work(self.CREW, oid)
        trip = self.db.one("SELECT id FROM haul_trips WHERE order_id=?", (oid,))
        self.svc.trip_arrive(self.CREW, trip["id"])
        self.svc.handover(self.CREW, trip["id"], 6, 180.0, "班组长")
        return trip["id"]

    def test_weighbridge_returns_measured_weight(self):
        tid = self._weighed_trip()
        t = self.db.one("SELECT * FROM haul_trips WHERE id=?", (tid,))
        self.assertIsNotNone(t["measured_weight_kg"])
        self.assertAlmostEqual(t["measured_weight_kg"], 180.0, delta=180 * 0.05)

    def test_only_supervisor_with_reason(self):
        tid = self._weighed_trip()
        for uid in (self.PATROL, self.DISPATCH, self.CREW, self.INSPECT):
            with self.assertRaises(ServiceError) as cm:
                self.svc.revise_weight(uid, tid, 170.0, "扣桶重")
            self.assertEqual(cm.exception.status, 403)
        with self.assertRaises(ServiceError) as cm:
            self.svc.revise_weight(self.SUP, tid, 170.0, "   ")
        self.assertEqual(cm.exception.code, "reason_required")
        r = self.svc.revise_weight(self.SUP, tid, 170.5, "过磅含两个淤泥桶，扣减桶重 9.5kg")
        self.assertEqual(r["revised_weight_kg"], 170.5)

    def test_revision_audit_trail(self):
        tid = self._weighed_trip()
        self.svc.revise_weight(self.SUP, tid, 168.0, "复核扣减")
        t = self.db.one("SELECT * FROM haul_trips WHERE id=?", (tid,))
        self.assertEqual(t["revised_weight_kg"], 168.0)
        self.assertEqual(t["revision_reason"], "复核扣减")
        self.assertEqual(t["revised_by"], "赵管")
        self.assertIsNotNone(t["revised_at"])
        note = self.db.one(
            "SELECT message FROM notifications WHERE kind='weight_revision' ORDER BY id DESC")
        self.assertIn("168.0", note["message"])

    def test_cannot_revise_before_weighing(self):
        oid = self._fresh_order(("YS-0101",))
        self._dispatch(oid)
        tid = self.db.one("SELECT id FROM haul_trips WHERE order_id=?", (oid,))["id"]
        with self.assertRaises(ServiceError) as cm:
            self.svc.revise_weight(self.SUP, tid, 100.0, "提前修订")
        self.assertEqual(cm.exception.code, "not_weighed")


# =====================================================================
# 5. 角色权限 + 应急入口
# =====================================================================
class TestRolesAndIncidents(PlatformCase):
    def setUp(self):
        super().setUp()
        self.oid = self._fresh_order(("YS-0101",))
        self.iid = self._inlet_id("YS-0101")

    def test_dispatch_permissions(self):
        for uid in (self.PATROL, self.CREW, self.INSPECT):
            with self.assertRaises(ServiceError) as cm:
                self._dispatch(self.oid, user=uid)
            self.assertEqual(cm.exception.status, 403)
        self._dispatch(self.oid, user=self.DISPATCH)

    def test_field_steps_crew_only(self):
        self._dispatch(self.oid)
        for uid in (self.PATROL, self.DISPATCH, self.INSPECT, self.SUP):
            with self.assertRaises(ServiceError) as cm:
                self.svc.record_step(uid, self.oid, self.iid, "open")
            self.assertEqual(cm.exception.status, 403)
        self.svc.start_work(self.CREW, self.oid)
        self.svc.record_step(self.CREW, self.oid, self.iid, "open")

    def test_acceptance_inspector_only(self):
        self._dispatch(self.oid)
        self.svc.start_work(self.CREW, self.oid)
        self._complete_inlet(self.oid, self.iid, "YS-0101")
        self.svc.submit_acceptance(self.CREW, self.oid, [self.iid])
        for uid in (self.PATROL, self.DISPATCH, self.CREW, self.SUP):
            with self.assertRaises(ServiceError) as cm:
                self.svc.review_acceptance(uid, self.oid, self.iid, True, "")
            self.assertEqual(cm.exception.status, 403)
        self.svc.review_acceptance(self.INSPECT, self.oid, self.iid, True, "合格")

    def test_create_order_roles(self):
        # 班组与验收员不能建单
        for uid in (self.CREW, self.INSPECT):
            with self.assertRaises(ServiceError) as cm:
                self.svc.create_order(uid, [self._inlet_id("YS-0105")], 2)
            self.assertEqual(cm.exception.status, 403)

    def test_duplicate_inlet_order_blocked(self):
        # YS-0101 已在 self.oid 的在途单中
        with self.assertRaises(ServiceError) as cm:
            self.svc.create_order(self.PATROL, [self.iid], 2)
        self.assertEqual(cm.exception.code, "inlet_busy")

    def test_six_incident_entries(self):
        self._dispatch(self.oid)
        self.svc.start_work(self.CREW, self.oid)
        # 暴雨加急
        rush = self.svc.rush_before_storm(self.PATROL, [self._inlet_id("YS-0105")], iso(6), "预警")
        self.assertEqual(rush["type"], "rush")
        self.assertTrue(rush["urgent"])
        self.assertEqual(len(rush["reminders"]), 1)
        # 井盖破损 → 自动建单、井盖标记、警戒
        cover = self.svc.cover_damage(self.PATROL, self._inlet_id("YS-0201"), "裂纹")
        self.assertEqual(cover["order"]["type"], "cover_damage")
        self.assertEqual(self.db.one(
            "SELECT cover_ok FROM inlets WHERE id=?",
            (self._inlet_id("YS-0201"),))["cover_ok"], 0)
        # 积水反涌
        bf = self.svc.backflow(self.PATROL, self._inlet_id("YS-0301"), "反涌")
        self.assertEqual(bf["type"], "backflow")
        # 淤泥遗撒
        sp = self.svc.spill_report(self.CREW, self.oid, "路面遗撒")
        self.assertEqual(sp["kind"], "spill")
        # 车辆未到 + 重新派车
        tid = self.db.one("SELECT id FROM haul_trips WHERE order_id=?", (self.oid,))["id"]
        miss = self.svc.vehicle_noshow(self.CREW, tid)
        self.assertEqual(miss["kind"], "vehicle_missing")
        self.assertEqual(self.db.one(
            "SELECT status FROM haul_trips WHERE id=?", (tid,))["status"], "noshow")
        new = self.svc.reassign_vehicle(self.DISPATCH, self.oid, 2)
        self.assertEqual(self.db.one(
            "SELECT status FROM haul_trips WHERE id=?",
            (new["trip_id"],))["status"], "dispatched")
        # 照片缺失：登记 → 补拍后自动关闭
        pm = self.svc.mark_photo_missing(self.INSPECT, self.oid, self.iid, "缺后照片")
        self.assertEqual(pm["status"], "open")
        self.svc.save_photo(self.CREW, self.oid, self.iid, "before", "b.svg",
                            make_photo_svg("YS-0101", "before"))
        inc = self.db.one("SELECT status FROM incidents WHERE id=?", (pm["id"],))
        self.assertEqual(inc["status"], "handled")

    def test_storm_reminder_fires_on_check(self):
        # 降雨时间已过 3 小时 → 复查提醒（提前 2 小时）应立即到点
        rush = self.svc.rush_before_storm(self.PATROL, [self._inlet_id("YS-0105")],
                                          iso(-3), "突发暴雨")
        fired = self.svc.check_due_reminders()
        self.assertTrue(any(r["order_id"] == rush["id"] for r in fired))
        again = self.svc.check_due_reminders()
        self.assertFalse(any(r["order_id"] == rush["id"] for r in again))


if __name__ == "__main__":
    unittest.main()
