"""平台规则测试（标准库 unittest，零三方依赖）：

1. 派工时间冲突（含并发线程，验证 BEGIN IMMEDIATE 下只允许一个成功）
2. 工序/照片/袋数/复位缺失时验收门禁拦截
3. 相邻合单边界（跨区段、超 150m、已验收不可合、合单不合并验收）
4. 清运重量修订（仅主管、原因必填、审计留痕）
5. 角色权限矩阵
另含退回沿用原工单、暴雨提醒、HTTP 鉴权与上传的集成测试。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from http.server import ThreadingHTTPServer

from app import config
from app import services as S
from app.auth import CurrentUser
from app.db import connect, init_db
from app.reminders import reminders
from app.seed import seed
from app.server import Handler

INSP = lambda: CurrentUser("inspect", "巡查员小陈", "inspector", None)
DISP = lambda: CurrentUser("dispatch", "调度员老吴", "dispatcher", None)
SUP = lambda: CurrentUser("super", "主管郑姐", "supervisor", None)
ACC = lambda: CurrentUser("accept", "验收员老何", "acceptor", None)
CREW1 = lambda: CurrentUser("crew1", "赵大勇", "crew", 1)
CREW2 = lambda: CurrentUser("crew2", "孙志强", "crew", 2)


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        config.DB_PATH = os.path.join(self.tmp, "test.db")
        config.UPLOAD_DIR = Path(self.tmp) / "uploads"
        config.PHOTO_DIR = config.UPLOAD_DIR / "photos"
        config.SAMPLE_DIR = config.UPLOAD_DIR / "sample"
        init_db(config.DB_PATH)
        seed()
        self.t0 = (datetime.now() + timedelta(hours=1)).replace(microsecond=0)
        self.t1 = self.t0 + timedelta(hours=2)

    def tearDown(self) -> None:
        if reminders._timer:
            reminders._timer.cancel()
            reminders._timer = None

    def window(self, shift_min: int = 0, hours: int = 1):
        s = self.t0 + timedelta(minutes=shift_min)
        return s.isoformat(), (s + timedelta(hours=hours)).isoformat()

    def dispatch(self, order_ids, crew=1, vehicle=1, shift=0, user=None):
        s, e = self.window(shift)
        return S.dispatch_service.dispatch(
            user or DISP(), order_ids, crew, s, e, vehicle)

    def complete_dispatch(self, did, order_id, bags=5, crew=CREW1,
                          with_photos=True):
        """完整走一遍四工序 + 袋数 + 后照/复位照（样例照片）。"""
        u = crew()
        for st in config.STEP_ORDER:
            S.step_service.record(u, did, st)
        S.step_service.record_bags(u, did, bags)
        if with_photos:
            data = (b'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60">'
                    b'<rect width="80" height="60" fill="#2e6f8e"/></svg>')
            S.photo_service.upload(u, order_id, "after", "clean", "a.svg", data, did)
            S.photo_service.upload(u, order_id, "after", "reset", "r.svg", data, did)


# ============================================================ 1. 派工冲突

class TestDispatchConflict(ServiceTestBase):
    def test_overlapping_same_crew_blocked(self):
        """同班组时间重叠的第二次派工被阻止。"""
        self.dispatch([1])  # 工单1 -> 班组甲
        with self.assertRaises(S.ConflictError) as cm:
            self.dispatch([2])  # 工单2 -> 同班组、同时段
        self.assertEqual(cm.exception.code, "schedule_conflict")

    def test_back_to_back_allowed(self):
        """首尾相接不算冲突。"""
        self.dispatch([1])
        r = self.dispatch([2], shift=60)  # 上一段 [t0,t0+1h]，这一段 [t0+1h,...]
        self.assertTrue(r["dispatch_ids"])

    def test_different_crew_same_time_allowed(self):
        self.dispatch([1], crew=1)
        r = self.dispatch([2], crew=2)
        self.assertTrue(r["dispatch_ids"])

    def test_concurrent_dispatch_only_one_wins(self):
        """两个调度线程并发给同一班组派重叠时段：恰好一个成功、一个 409。"""
        barrier = threading.Barrier(2)
        outcomes: list = []

        def worker(order_id):
            try:
                barrier.wait(timeout=5)
                self.dispatch([order_id])
                outcomes.append("ok")
            except S.ConflictError:
                outcomes.append("conflict")
            except Exception as e:  # noqa: BLE001
                outcomes.append(("other", str(e)))

        threads = [threading.Thread(target=worker, args=(oid,)) for oid in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(sorted(outcomes), ["conflict", "ok"])


# ============================================================ 2. 步骤/资料缺失

class TestCompletenessGate(ServiceTestBase):
    def test_steps_must_be_in_order(self):
        did = self.dispatch([1])["dispatch_ids"][0]
        with self.assertRaises(S.ValidationError):
            S.step_service.record(CREW1(), did, "clean")  # 未开盖
        S.step_service.record(CREW1(), did, "open")
        with self.assertRaises(S.ValidationError):
            S.step_service.record(CREW1(), did, "reset")  # 跳过清掏/冲洗

    def test_step_duplicate_blocked(self):
        did = self.dispatch([1])["dispatch_ids"][0]
        S.step_service.record(CREW1(), did, "open")
        with self.assertRaises(S.ConflictError):
            S.step_service.record(CREW1(), did, "open")

    def test_bags_require_clean_step(self):
        did = self.dispatch([1])["dispatch_ids"][0]
        with self.assertRaises(S.ValidationError):
            S.step_service.record_bags(CREW1(), did, 3)

    def test_acceptance_blocked_when_missing(self):
        did = self.dispatch([1])["dispatch_ids"][0]
        # 只做工序，没有袋数和后照
        for st in config.STEP_ORDER:
            S.step_service.record(CREW1(), did, st)
        with self.assertRaises(S.ValidationError) as cm:
            S.acceptance_service.request(CREW1(), 1)
        msg = cm.exception.message
        self.assertIn("淤泥袋数", msg)
        self.assertIn("清掏后照片", msg)
        self.assertIn("复位检查照片", msg)
        # 样例工单1 已带清掏前照片，不应报前照缺失
        self.assertNotIn("缺少清掏前照片", msg)

    def test_missing_step_blocks_acceptance(self):
        did = self.dispatch([1])["dispatch_ids"][0]
        for st in ("open", "clean", "flush"):  # 缺复位
            S.step_service.record(CREW1(), did, st)
        S.step_service.record_bags(CREW1(), did, 5)
        data = (b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="6"/>')
        S.photo_service.upload(CREW1(), 1, "after", "clean", "a.svg", data, did)
        S.photo_service.upload(CREW1(), 1, "after", "reset", "r.svg", data, did)
        with self.assertRaises(S.ValidationError) as cm:
            S.acceptance_service.request(CREW1(), 1)
        self.assertIn("复位", cm.exception.message)

    def test_zero_bags_for_heavy_block_rejected(self):
        # 工单1 为严重堵塞(3)，袋数 0 也应拦截
        did = self.dispatch([1])["dispatch_ids"][0]
        self.complete_dispatch(did, 1, bags=0)
        with self.assertRaises(S.ValidationError) as cm:
            S.acceptance_service.request(CREW1(), 1)
        self.assertIn("袋数不能为 0", cm.exception.message)

    def test_full_package_passes_gate_and_restores_inlet(self):
        did = self.dispatch([1])["dispatch_ids"][0]
        self.complete_dispatch(did, 1)
        S.acceptance_service.request(CREW1(), 1)
        order = S.acceptance_service.decide(ACC(), 1, "pass", "合格")
        self.assertEqual(order["status"], "accepted")
        with connect() as conn:
            inlet = conn.execute("SELECT * FROM inlets WHERE id=2").fetchone()
        self.assertEqual(inlet["status"], "normal")
        self.assertEqual(inlet["block_level"], 0)

    def test_reject_keeps_same_order_and_can_redispatch(self):
        before = S.order_service.get(3)  # 样例退回工单
        self.assertEqual(before["status"], "rejected")
        old_code = before["code"]
        old_dispatch_count = len(before["dispatches"])
        # 退回工单可重新派工，沿用原单号
        self.dispatch([3], crew=2, user=DISP())
        after = S.order_service.get(3)
        self.assertEqual(after["code"], old_code)
        self.assertEqual(len(after["dispatches"]), old_dispatch_count + 1)
        self.assertEqual(after["status"], "dispatched")

    def test_reject_requires_comment(self):
        did = self.dispatch([1])["dispatch_ids"][0]
        self.complete_dispatch(did, 1)
        S.acceptance_service.request(CREW1(), 1)
        with self.assertRaises(S.ValidationError):
            S.acceptance_service.decide(ACC(), 1, "reject", "")


# ============================================================ 3. 合单边界

class TestMergeBoundary(ServiceTestBase):
    def test_adjacent_same_segment_merge_ok(self):
        # 工单1/2 同区段，YS-0102(210,250) 与 YS-0103(260,360) 相距约 121m
        r = self.dispatch([1, 2])
        self.assertIsNotNone(r["merge_group_id"])
        self.assertEqual(len(r["dispatch_ids"]), 2)

    def test_cross_segment_merge_blocked(self):
        # 工单3（WO-REJECT-01）位于另一区段
        with self.assertRaises(S.ValidationError) as cm:
            self.dispatch([1, 3])
        self.assertIn("同一道路区段", cm.exception.message)

    def test_over_distance_merge_blocked(self):
        # YS-0101(130,200) 与 YS-0103(260,360) 相距约 206m > 150m
        S.order_service.create(INSP(), inlet_id=1, block_level=1)
        with connect() as conn:
            new_id = conn.execute("SELECT MAX(id) id FROM work_orders").fetchone()["id"]
        with self.assertRaises(S.ValidationError) as cm:
            self.dispatch([new_id, 2])
        self.assertIn("150m", cm.exception.message)

    def test_duplicate_order_ids_rejected(self):
        with self.assertRaises(S.ValidationError):
            self.dispatch([1, 1])

    def test_accepted_order_cannot_merge(self):
        did = self.dispatch([1])["dispatch_ids"][0]
        self.complete_dispatch(did, 1)
        S.acceptance_service.request(CREW1(), 1)
        S.acceptance_service.decide(ACC(), 1, "pass")
        with self.assertRaises(S.ConflictError):
            self.dispatch([1, 2])

    def test_merged_orders_accepted_independently(self):
        """合单只合并派车：A 验收通过不影响 B，B 资料不齐不能搭车验收。"""
        r = self.dispatch([1, 2])
        d1, d2 = r["dispatch_ids"]
        self.complete_dispatch(d1, 1)
        # B 什么都没做 -> 独立门禁拦截
        with self.assertRaises(S.ValidationError):
            S.acceptance_service.request(CREW1(), 2)
        # A 正常通过
        S.acceptance_service.request(CREW1(), 1)
        S.acceptance_service.decide(ACC(), 1, "pass")
        b = S.order_service.get(2)
        self.assertEqual(b["status"], "dispatched")
        with connect() as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM inlets WHERE id=3").fetchone()["status"],
                "cleaning")

    def test_merge_reassign_vehicle_covers_all_items(self):
        r = self.dispatch([1, 2], vehicle=1)
        S.vehicle_service.mark_missing(DISP(), r["dispatch_ids"][0], "未到")
        S.vehicle_service.assign_vehicle(DISP(), r["dispatch_ids"][0], 2)
        with connect() as conn:
            plates = [conn.execute(
                "SELECT v.plate FROM dispatches d JOIN vehicles v ON v.id=d.vehicle_id"
                " WHERE d.id=?", (did,)).fetchone()["plate"]
                for did in r["dispatch_ids"]]
        self.assertEqual(plates, ["京A·W302", "京A·W302"])


# ============================================================ 4. 重量修订

class TestWeightRevision(ServiceTestBase):
    def _weighed(self):
        did = self.dispatch([1])["dispatch_ids"][0]
        for st in config.STEP_ORDER:
            S.step_service.record(CREW1(), did, st)
        S.step_service.record_bags(CREW1(), did, 6)
        result = S.vehicle_service.weigh(CREW1(), did)
        return did, result

    def test_mock_adapter_deterministic(self):
        from app.adapters import adapter
        a = adapter.weigh("DSP-X", 5)
        b = adapter.weigh("DSP-X", 5)
        self.assertEqual(a["net_kg"], b["net_kg"])
        self.assertTrue(a["stable"])

    def test_weigh_requires_bags(self):
        did = self.dispatch([1])["dispatch_ids"][0]
        with self.assertRaises(S.ValidationError):
            S.vehicle_service.weigh(CREW1(), did)

    def test_only_supervisor_and_reason_required(self):
        did, weighed = self._weighed()
        with self.assertRaises(S.PermissionError):
            S.vehicle_service.revise_weight(DISP(), did, 100, "核减")
        with self.assertRaises(S.PermissionError):
            S.vehicle_service.revise_weight(CREW1(), did, 100, "核减")
        with self.assertRaises(S.PermissionError):
            S.vehicle_service.revise_weight(ACC(), did, 100, "核减")
        with self.assertRaises(S.ValidationError):
            S.vehicle_service.revise_weight(SUP(), did, 100, "   ")
        r = S.vehicle_service.revise_weight(SUP(), did, weighed["net_kg"] - 6,
                                            "过磅含空袋与雨水，核减 6kg")
        self.assertEqual(r["final_kg"], round(weighed["net_kg"] - 6, 1))
        self.assertEqual(len(r["revisions"]), 1)
        rev = r["revisions"][0]
        self.assertEqual(rev["old_kg"], weighed["net_kg"])
        self.assertEqual(rev["operator"], "主管郑姐")
        self.assertTrue(rev["reason"])
        with connect() as conn:
            out = conn.execute(
                "SELECT * FROM clean_outputs WHERE dispatch_id=?", (did,)).fetchone()
            self.assertEqual(out["revised_kg"], round(weighed["net_kg"] - 6, 1))
            self.assertEqual(out["revised_by"], "主管郑姐")
            hv = conn.execute(
                "SELECT net_kg FROM vehicle_handovers WHERE dispatch_id=?",
                (did,)).fetchone()
            self.assertEqual(hv["net_kg"], round(weighed["net_kg"] - 6, 1))


# ============================================================ 5. 角色权限

class TestRoleMatrix(ServiceTestBase):
    def test_inspector_permissions(self):
        # 可建单
        S.order_service.create(INSP(), inlet_id=8, block_level=1)
        # 不可派工/记工序/验收/修订
        with self.assertRaises(S.PermissionError):
            self.dispatch([1], user=INSP())
        did = self.dispatch([1])["dispatch_ids"][0]
        with self.assertRaises(S.PermissionError):
            S.step_service.record(INSP(), did, "open")
        with self.assertRaises(S.PermissionError):
            S.acceptance_service.decide(INSP(), 1, "pass")
        with self.assertRaises(S.PermissionError):
            S.vehicle_service.revise_weight(INSP(), did, 1, "x")

    def test_dispatcher_permissions(self):
        self.dispatch([1])  # 可派工
        with self.assertRaises(S.PermissionError):
            S.order_service.create(DISP(), inlet_id=8, block_level=1)
        with self.assertRaises(S.PermissionError):
            S.acceptance_service.decide(DISP(), 1, "pass")

    def test_crew_scoped_to_own_crew(self):
        did1 = self.dispatch([1], crew=1)["dispatch_ids"][0]
        with self.assertRaises(S.PermissionError):
            S.step_service.record(CREW2(), did1, "open")
        # 本班组可以
        S.step_service.record(CREW1(), did1, "open")

    def test_acceptor_only_decides(self):
        did = self.dispatch([1])["dispatch_ids"][0]
        with self.assertRaises(S.PermissionError):
            S.acceptance_service.decide(DISP(), 1, "pass")
        with self.assertRaises(S.PermissionError):
            S.acceptance_service.decide(SUP(), 1, "pass")
        # 非待验收状态不可下结论
        with self.assertRaises(S.ConflictError):
            S.acceptance_service.decide(ACC(), 1, "pass")

    def test_cover_damaged_requires_guard(self):
        with self.assertRaises(S.ValidationError):
            S.order_service.create(INSP(), inlet_id=8, block_level=2,
                                   issue_kind="cover_damaged")

    def test_duplicate_open_order_blocked(self):
        with self.assertRaises(S.ConflictError):
            S.order_service.create(INSP(), inlet_id=2, block_level=3)


# ============================================================ 暴雨提醒 / 专项入口

class TestRemindersAndIssues(ServiceTestBase):
    def test_storm_urgent_creates_reminder(self):
        S.order_service.create(INSP(), inlet_id=8, block_level=2,
                               issue_kind="storm_urgent")
        pending = reminders.list_pending()
        self.assertTrue(any("YS-0301" in r["message"] for r in pending))

    def test_fire_due_reminder(self):
        past = datetime.now() - timedelta(minutes=1)
        rid = S.reminder_service.create(
            DISP(), inlet_id=2, order_id=1, due_at=past.isoformat(),
            message="到期提醒测试")["id"]
        fired = reminders.fire_due()
        self.assertIn(rid, [f["id"] for f in fired])
        self.assertEqual(reminders.fire_due(), [])  # 幂等

    def test_six_issue_entries_persist(self):
        for kind in ("storm_urgent", "cover_damaged", "backflow",
                     "silt_spill", "vehicle_missing", "photo_missing"):
            if kind == "photo_missing":
                S.order_service.mark_photo_missing(INSP(), 1, "缺复位照")
            else:
                S.order_service.report_issue(INSP(), 1, kind, "现场情况")
        o = S.order_service.get(1)
        self.assertEqual(o["issue_kind"], "photo_missing")
        self.assertIn("照片缺失", o["issue_note"])

    def test_vehicle_missing_entry(self):
        did = self.dispatch([1])["dispatch_ids"][0]
        S.vehicle_service.mark_missing(CREW1(), did, "堵在路上")
        d = S.dispatch_service.get(did)
        self.assertEqual(d["status"], "vehicle_missing")
        self.assertEqual(d["handover"]["status"], "missing")


# ============================================================ HTTP 集成（鉴权、上传、冲突状态码）

class TestHttpIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp()
        config.DB_PATH = os.path.join(cls.tmp, "http.db")
        config.UPLOAD_DIR = Path(cls.tmp) / "uploads"
        config.PHOTO_DIR = config.UPLOAD_DIR / "photos"
        config.SAMPLE_DIR = config.UPLOAD_DIR / "sample"
        init_db(config.DB_PATH)
        seed()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        if reminders._timer:
            reminders._timer.cancel()

    def call(self, method, path, user=None, body=None, raw=False):
        data = None
        headers = {}
        if body is not None:
            data = body if raw else json.dumps(body).encode()
            if not raw:
                headers["Content-Type"] = "application/json"
        if user:
            headers["X-Username"] = user
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_auth_required(self):
        status, body = self.call("GET", "/api/orders")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_dispatch_conflict_http_409(self):
        t0 = (datetime.now() + timedelta(days=1)).replace(microsecond=0)
        payload = {"order_ids": [1], "crew_id": 1,
                   "start_at": t0.isoformat(),
                   "end_at": (t0 + timedelta(hours=1)).isoformat()}
        s1, _ = self.call("POST", "/api/dispatches", "dispatch", payload)
        self.assertEqual(s1, 201)
        payload2 = dict(payload, order_ids=[2])
        s2, body = self.call("POST", "/api/dispatches", "dispatch", payload2)
        self.assertEqual(s2, 409)
        self.assertEqual(body["error"], "schedule_conflict")

    def test_permission_http_403(self):
        s, body = self.call("POST", "/api/dispatches", "inspect",
                            {"order_ids": [2], "crew_id": 1,
                             "start_at": "2030-01-01T08:00",
                             "end_at": "2030-01-01T09:00"})
        self.assertEqual(s, 403)

    def test_photo_upload_and_local_file(self):
        # 给工单2派个工
        t0 = (datetime.now() + timedelta(days=2)).replace(microsecond=0)
        s, r = self.call("POST", "/api/dispatches", "dispatch",
                         {"order_ids": [2], "crew_id": 2,
                          "start_at": t0.isoformat(),
                          "end_at": (t0 + timedelta(hours=1)).isoformat()})
        self.assertEqual(s, 201)
        did = r["dispatch_ids"][0]
        boundary = "----testboundary123"
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="6"/>'
        body = b""
        for name, value in (("phase", b"after"), ("kind", b"clean"),
                            ("dispatch_id", str(did).encode())):
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                     ).encode() + value + b"\r\n"
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\";"
                 ' filename="after.svg"\r\nContent-Type: image/svg+xml\r\n\r\n').encode() + svg + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/orders/2/photos",
            data=body, method="POST",
            headers={"X-Username": "crew2",
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=10) as resp2:
            self.assertEqual(resp2.status, 201)
            info = json.loads(resp2.read())
        self.assertTrue(os.path.exists(os.path.join(config.UPLOAD_DIR, info["path"])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
