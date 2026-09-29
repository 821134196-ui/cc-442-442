"""业务规则层：建单、派工冲突、工序门禁、合单、验收、修订、应急入口、提醒。"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .db import DB

STEPS = ["open", "clean", "flush", "reset"]
STEP_CN = {"open": "开盖", "clean": "清掏", "flush": "冲洗", "reset": "复位"}
ACTIVE_ORDER_STATES = ("dispatched", "working", "submitted", "rejected")
EMERGENCY_KINDS = ("cover_damage", "backflow", "spill", "vehicle_missing", "photo_missing")
ADJACENT_LIMIT = 120.0  # 相邻判定距离（示意米）

ROLES = {
    "patrol": "巡查员",
    "dispatcher": "调度员",
    "crew": "班组",
    "inspector": "验收员",
    "supervisor": "主管",
}


class ServiceError(Exception):
    def __init__(self, message: str, code: str = "bad_request", status: int = 400, payload=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.payload = payload or {}


def now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts)


def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


class WeighbridgeAdapter:
    """模拟地磅适配器：按车牌稳定回传过磅重量（含票据号）。"""

    def __init__(self, jitter: float = 0.03):
        self.jitter = jitter

    def weigh(self, plate: str, declared_kg: float, ticket_no: str) -> dict:
        seed = int(hashlib.sha256(f"{plate}:{ticket_no}".encode()).hexdigest()[:8], 16)
        factor = 1 + ((seed % 1000) / 1000.0 * 2 - 1) * self.jitter
        measured = round(declared_kg * factor, 1)
        return {
            "ticket": f"WB-{ticket_no}",
            "plate": plate,
            "declared_weight_kg": declared_kg,
            "measured_weight_kg": measured,
            "stable": True,
            "weighed_at": now(),
        }


class Service:
    def __init__(self, db: DB, upload_dir: Path | str = "uploads"):
        self.db = db
        self.upload_dir = Path(upload_dir)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        (self.upload_dir / "photos").mkdir(exist_ok=True)
        self.weigher = WeighbridgeAdapter()
        self._reminder_lock = threading.Lock()

    # ---------- 权限 ----------
    def user(self, user_id: int) -> dict:
        row = self.db.one("SELECT * FROM users WHERE id=?", (user_id,))
        if not row:
            raise ServiceError("用户不存在", "no_user", 401)
        return dict(row)

    def require(self, user_id: int, *roles: str) -> dict:
        u = self.user(user_id)
        if u["role"] not in roles:
            raise ServiceError(
                f"{ROLES.get(u['role'], u['role'])}无权执行该操作，需要：{'/'.join(ROLES[r] for r in roles)}",
                "forbidden", 403,
            )
        return u

    # ---------- 查询 ----------
    def map_overview(self) -> dict:
        sectors = [dict(r) for r in self.db.query("SELECT * FROM sectors ORDER BY id")]
        segments = [dict(r) for r in self.db.query("SELECT * FROM road_segments ORDER BY id")]
        for s in segments:
            s["points"] = json.loads(s["points"])
        inlets = [dict(r) for r in self.db.query(
            "SELECT id,code,sector_id,segment_id,x,y,status,block_level,cover_ok FROM inlets ORDER BY code")]
        return {"sectors": sectors, "segments": segments, "inlets": inlets}

    def inlet_detail(self, inlet_id: int) -> dict:
        row = self.db.one("SELECT * FROM inlets WHERE id=?", (inlet_id,))
        if not row:
            raise ServiceError("雨水口不存在", "not_found", 404)
        inlet = dict(row)
        inlet["contacts"] = [dict(r) for r in self.db.query(
            "SELECT * FROM contacts WHERE inlet_id=?", (inlet_id,))]
        inlet["orders"] = [dict(r) for r in self.db.query(
            "SELECT o.id AS id,o.order_no,o.type,o.status,o.created_at FROM work_orders o "
            "JOIN order_inlets oi ON oi.order_id=o.id WHERE oi.inlet_id=? ORDER BY o.id DESC",
            (inlet_id,))]
        return inlet

    def orders(self, status: str | None = None) -> list[dict]:
        sql = ("SELECT o.*, c.name AS crew_name, v.plate AS vehicle_plate, "
               "(SELECT GROUP_CONCAT(i2.code) FROM order_inlets oi "
               " JOIN inlets i2 ON i2.id=oi.inlet_id WHERE oi.order_id=o.id) AS inlet_codes "
               "FROM work_orders o LEFT JOIN crews c ON c.id=o.crew_id "
               "LEFT JOIN vehicles v ON v.id=o.vehicle_id")
        args: tuple = ()
        if status:
            sql += " WHERE o.status=?"
            args = (status,)
        sql += " ORDER BY o.urgent DESC, o.id"
        return [dict(r) for r in self.db.query(sql, args)]

    def order_detail(self, order_id: int) -> dict:
        row = self.db.one("SELECT * FROM work_orders WHERE id=?", (order_id,))
        if not row:
            raise ServiceError("工单不存在", "not_found", 404)
        order = dict(row)
        order["inlets"] = []
        for li in self.db.query(
            "SELECT oi.*, i.code, i.x, i.y, i.segment_id FROM order_inlets oi "
            "JOIN inlets i ON i.id=oi.inlet_id WHERE oi.order_id=?", (order_id,)):
            item = dict(li)
            item["steps"] = [dict(r) for r in self.db.query(
                "SELECT * FROM work_steps WHERE order_id=? AND inlet_id=? ORDER BY recorded_at, id",
                (order_id, li["inlet_id"]))]
            item["photos"] = [dict(r) for r in self.db.query(
                "SELECT id,phase,file_path,created_at FROM photos WHERE order_id=? AND inlet_id=?",
                (order_id, li["inlet_id"]))]
            order["inlets"].append(item)
        order["trips"] = [dict(r) for r in self.db.query(
            "SELECT t.*, v.plate FROM haul_trips t LEFT JOIN vehicles v ON v.id=t.vehicle_id "
            "WHERE t.order_id=? ORDER BY t.id", (order_id,))]
        order["incidents"] = [dict(r) for r in self.db.query(
            "SELECT * FROM incidents WHERE order_id=? ORDER BY id", (order_id,))]
        order["reminders"] = [dict(r) for r in self.db.query(
            "SELECT * FROM reminders WHERE order_id=? ORDER BY fire_at", (order_id,))]
        return order

    def crew_schedule(self, date: str | None = None) -> list[dict]:
        rows = self.db.query(
            "SELECT o.id,o.order_no,o.type,o.status,o.planned_start,o.planned_end,o.urgent,"
            "o.description,c.id AS crew_id,c.name AS crew_name,c.leader,c.phone,"
            "(SELECT GROUP_CONCAT(i2.code) FROM order_inlets oi "
            " JOIN inlets i2 ON i2.id=oi.inlet_id WHERE oi.order_id=o.id) AS inlet_codes "
            "FROM work_orders o JOIN crews c ON c.id=o.crew_id "
            "WHERE o.status IN ('dispatched','working','submitted','rejected') ORDER BY c.id,o.planned_start")
        out = []
        for r in rows:
            d = dict(r)
            if date and d.get("planned_start") and not d["planned_start"].startswith(date):
                continue
            out.append(d)
        return out

    def clean_records(self) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT a.id,a.created_at,a.result,a.comment AS acceptance_comment,i.code AS inlet_code,"
            "o.order_no,a.inspector,oi.bag_count "
            "FROM acceptance_records a JOIN inlets i ON i.id=a.inlet_id "
            "JOIN work_orders o ON o.id=a.order_id "
            "LEFT JOIN order_inlets oi ON oi.order_id=a.order_id AND oi.inlet_id=a.inlet_id "
            "ORDER BY a.id DESC")]

    def vehicle_handovers(self) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT t.*, v.plate, v.driver, o.order_no,"
            "(SELECT GROUP_CONCAT(i2.code) FROM order_inlets oi "
            " JOIN inlets i2 ON i2.id=oi.inlet_id WHERE oi.order_id=o.id) AS inlet_codes "
            "FROM haul_trips t JOIN work_orders o ON o.id=t.order_id "
            "LEFT JOIN vehicles v ON v.id=t.vehicle_id ORDER BY t.id DESC")]

    # ---------- 建单 ----------
    def create_order(self, user_id: int, inlet_ids: list[int], block_level: int,
                     description: str = "", alert_radius: float = 0, alert_note: str = "",
                     order_type: str = "normal", source: str = "patrol",
                     urgent: bool = False, storm_at: str | None = None,
                     contact_id: int | None = None, reporter: str | None = None) -> dict:
        u = self.require(user_id, "patrol", "dispatcher", "supervisor")
        if not inlet_ids:
            raise ServiceError("请至少选择一个雨水口", "no_inlet")
        if block_level not in (1, 2, 3):
            raise ServiceError("堵塞等级应为 1/2/3", "bad_level")
        for iid in inlet_ids:
            if not self.db.one("SELECT id FROM inlets WHERE id=?", (iid,)):
                raise ServiceError(f"雨水口 {iid} 不存在", "not_found", 404)
            busy = self.db.one(
                "SELECT o.id,o.order_no,o.status FROM order_inlets oi JOIN work_orders o ON o.id=oi.order_id "
                "WHERE oi.inlet_id=? AND o.status IN ('pending','dispatched','working','submitted','rejected')",
                (iid,))
            if busy:
                raise ServiceError(
                    f"雨水口已在工单 {busy['order_no']}（{busy['status']}）中，不能重复建单",
                    "inlet_busy", 409)

        with self.db.write_lock:
            conn = self.db.get()
            conn.execute("BEGIN IMMEDIATE")
            try:
                n = conn.execute("SELECT COUNT(*) c FROM work_orders").fetchone()["c"] + 1
                order_no = f"{'UR' if urgent or order_type == 'rush' else 'WO'}-{n:04d}"
                oid = self.db.insert(
                    "work_orders",
                    order_no=order_no, type=order_type, source=source,
                    status="pending", block_level=block_level, description=description,
                    reporter=reporter or u["name"], contact_id=contact_id,
                    alert_radius=alert_radius, alert_note=alert_note,
                    urgent=1 if urgent or order_type == "rush" else 0,
                    storm_at=storm_at, created_at=now(),
                )
                for iid in inlet_ids:
                    self.db.insert("order_inlets", order_id=oid, inlet_id=iid)
                for iid in inlet_ids:
                    self.db.update("inlets", "id=?", (iid,), status="blocked", block_level=block_level)
                if alert_radius > 0:
                    self.db.insert("notifications", kind="alert_zone",
                                   message=f"工单 {order_no} 已划定警戒范围 {alert_radius:g}m：{alert_note}",
                                   created_at=now())
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        # 暴雨加急：生成暴雨前复查提醒（进程内任务）
        if (urgent or order_type == "rush") and storm_at:
            self._schedule_storm_review(oid, order_no, storm_at)
        return self.order_detail(oid)

    def _schedule_storm_review(self, order_id: int, order_no: str, storm_at: str,
                               lead_hours: float = 2.0) -> dict:
        fire = _parse(storm_at) - timedelta(hours=lead_hours)
        fire_s = fire.replace(microsecond=0).isoformat()
        rid = self.db.insert(
            "reminders", order_id=order_id, type="storm_review",
            message=f"暴雨前复查：工单 {order_no} 须在降雨前完成清掏复位，请确认现场状态",
            fire_at=fire_s, created_at=now())
        return {"id": rid, "fire_at": fire_s}

    def check_due_reminders(self) -> list[dict]:
        """进程内提醒任务周期调用：到点的提醒转通知。"""
        fired = []
        with self._reminder_lock:
            rows = self.db.query(
                "SELECT r.*, o.order_no FROM reminders r JOIN work_orders o ON o.id=r.order_id "
                "WHERE r.status='pending' AND r.fire_at<=?", (now(),))
            for r in rows:
                self.db.update("reminders", "id=?", (r["id"],), status="fired")
                self.db.insert("notifications", kind=r["type"], message=r["message"], created_at=now())
                fired.append(dict(r))
        return fired

    # ---------- 相邻合单 ----------
    def _inlet_xy(self, inlet_id: int) -> tuple[int, tuple[float, float], int | None]:
        r = self.db.one("SELECT id,x,y,segment_id FROM inlets WHERE id=?", (inlet_id,))
        if not r:
            raise ServiceError(f"雨水口 {inlet_id} 不存在", "not_found", 404)
        return r["id"], (r["x"], r["y"]), r["segment_id"]

    def _adjacency_components(self, inlet_ids: list[int]) -> list[list[int]]:
        """同一道路区段或直线距离<=阈值即相邻；返回连通分量。"""
        info = {iid: self._inlet_xy(iid) for iid in inlet_ids}
        parent = {iid: iid for iid in inlet_ids}

        def find(a: int) -> int:
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a: int, b: int) -> None:
            parent[find(a)] = find(b)

        ids = list(inlet_ids)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                _, pa, sa = info[a]
                _, pb, sb = info[b]
                same_segment = sa is not None and sa == sb
                if same_segment or dist(pa, pb) <= ADJACENT_LIMIT:
                    union(a, b)
        groups: dict[int, list[int]] = {}
        for iid in ids:
            groups.setdefault(find(iid), []).append(iid)
        return list(groups.values())

    def merge_orders(self, user_id: int, order_ids: list[int]) -> dict:
        """相邻点位可合并派车（多张待派工单合为一张）。验收仍按点位独立。"""
        self.require(user_id, "dispatcher")
        if len(order_ids) < 2:
            raise ServiceError("合单至少选择两张工单", "merge_count")
        orders = []
        for oid in order_ids:
            o = self.db.one("SELECT * FROM work_orders WHERE id=?", (oid,))
            if not o:
                raise ServiceError(f"工单 {oid} 不存在", "not_found", 404)
            if o["status"] != "pending":
                raise ServiceError(f"工单 {o['order_no']} 已派工，不能合并", "merge_state", 409)
            orders.append(o)

        inlet_ids = [r["inlet_id"] for r in self.db.query(
            f"SELECT inlet_id FROM order_inlets WHERE order_id IN ({','.join('?' * len(order_ids))})",
            tuple(order_ids))]
        components = self._adjacency_components(inlet_ids)
        if len(components) > 1:
            codes = [self.db.one("SELECT code FROM inlets WHERE id=?", (i,))["code"] for i in inlet_ids]
            raise ServiceError(
                "合单点必须彼此相邻（同一路段或距离 ≤ %gm），%s 不属于同一相邻片区" % (
                    ADJACENT_LIMIT, codes), "merge_not_adjacent", 422)

        with self.db.write_lock:
            conn = self.db.get()
            conn.execute("BEGIN IMMEDIATE")
            try:
                target = orders[0]
                merged_nos = [o["order_no"] for o in orders[1:]]
                max_level = max(o["block_level"] for o in orders)
                urgent = any(o["urgent"] for o in orders)
                for o in orders[1:]:
                    for r in self.db.query("SELECT inlet_id FROM order_inlets WHERE order_id=?", (o["id"],)):
                        conn.execute(
                            "INSERT OR IGNORE INTO order_inlets(order_id,inlet_id) VALUES(?,?)",
                            (target["id"], r["inlet_id"]))
                    conn.execute("DELETE FROM order_inlets WHERE order_id=?", (o["id"],))
                    conn.execute("UPDATE work_orders SET status='cancelled' WHERE id=?", (o["id"],))
                    self.db.insert("notifications", kind="merge",
                                   message=f"工单 {o['order_no']} 已并入 {target['order_no']}（仅合派车，验收仍逐点进行）",
                                   created_at=now())
                self.db.update("work_orders", "id=?", (target["id"],),
                               block_level=max_level,
                               urgent=1 if urgent else target["urgent"],
                               merged_from=json.dumps(merged_nos, ensure_ascii=False))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.order_detail(target["id"])

    # ---------- 派工与班组冲突 ----------
    @staticmethod
    def _windows_overlap(s1: str, e1: str, s2: str, e2: str) -> bool:
        a0, a1, b0, b1 = _parse(s1), _parse(e1), _parse(s2), _parse(e2)
        if not all([a0, a1, b0, b1]):
            return False
        return a0 < b1 and b0 < a1

    def dispatch(self, user_id: int, order_id: int, crew_id: int, vehicle_id: int,
                 planned_start: str, planned_end: str) -> dict:
        """调度员派工；并发分配同一班组到重叠时段必须被阻止。"""
        self.require(user_id, "dispatcher")
        o = self.db.one("SELECT * FROM work_orders WHERE id=?", (order_id,))
        if not o:
            raise ServiceError("工单不存在", "not_found", 404)
        if o["status"] not in ("pending", "rejected"):
            raise ServiceError(f"工单状态为 {o['status']}，不能派工", "dispatch_state", 409)
        if not _parse(planned_start) or not _parse(planned_end):
            raise ServiceError("计划时间格式应为 ISO 时间", "bad_time")
        if _parse(planned_start) >= _parse(planned_end):
            raise ServiceError("结束时间必须晚于开始时间", "bad_time")
        if not self.db.one("SELECT id FROM crews WHERE id=?", (crew_id,)):
            raise ServiceError("班组不存在", "not_found", 404)
        if not self.db.one("SELECT id FROM vehicles WHERE id=? AND active=1", (vehicle_id,)):
            raise ServiceError("车辆不存在或停用", "not_found", 404)

        with self.db.write_lock:
            conn = self.db.get()
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT o.order_no,o.planned_start,o.planned_end FROM work_orders o "
                    "WHERE o.crew_id=? AND o.status IN ('dispatched','working','submitted','rejected')",
                    (crew_id,)).fetchall()
                for r in rows:
                    if self._windows_overlap(planned_start, planned_end,
                                             r["planned_start"], r["planned_end"]):
                        raise ServiceError(
                            f"班组时段冲突：{r['order_no']} 已占用 {r['planned_start']} ~ {r['planned_end']}",
                            "crew_conflict", 409)
                self.db.update(
                    "work_orders", "id=?", (order_id,),
                    crew_id=crew_id, vehicle_id=vehicle_id,
                    planned_start=planned_start, planned_end=planned_end,
                    status="dispatched", dispatched_at=now())
                self.db.insert(
                    "haul_trips", order_id=order_id, vehicle_id=vehicle_id,
                    planned_at=planned_start, status="dispatched", created_at=now())
                self.db.insert("notifications", kind="dispatch",
                               message=f"工单 {o['order_no']} 已派工", created_at=now())
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.order_detail(order_id)

    def start_work(self, user_id: int, order_id: int) -> dict:
        self.require(user_id, "crew", "dispatcher")
        o = self.db.one("SELECT * FROM work_orders WHERE id=?", (order_id,))
        if not o or o["status"] != "dispatched":
            raise ServiceError("仅已派工工单可开工", "state", 409)
        self.db.update("work_orders", "id=?", (order_id,), status="working")
        for r in self.db.query("SELECT inlet_id FROM order_inlets WHERE order_id=?", (order_id,)):
            self.db.update("inlets", "id=?", (r["inlet_id"],), status="cleaning")
        return self.order_detail(order_id)

    # ---------- 现场工序 ----------
    def _working_order(self, order_id: int) -> dict:
        o = self.db.one("SELECT * FROM work_orders WHERE id=?", (order_id,))
        if not o:
            raise ServiceError("工单不存在", "not_found", 404)
        if o["status"] not in ("working", "rejected"):
            raise ServiceError(f"工单状态 {o['status']}，不能记录工序", "state", 409)
        return dict(o)

    def _check_inlet_in_order(self, order_id: int, inlet_id: int) -> None:
        if not self.db.one("SELECT id FROM order_inlets WHERE order_id=? AND inlet_id=?",
                           (order_id, inlet_id)):
            raise ServiceError("该雨水口不属于此工单", "not_in_order", 404)

    def record_step(self, user_id: int, order_id: int, inlet_id: int, step: str,
                    note: str = "") -> dict:
        """开盖→清掏→冲洗→复位，必须顺序记录。"""
        u = self.require(user_id, "crew")
        if step not in STEPS:
            raise ServiceError("工序应为 open/clean/flush/reset", "bad_step")
        self._working_order(order_id)
        self._check_inlet_in_order(order_id, inlet_id)
        idx = STEPS.index(step)
        done = {r["step"] for r in self.db.query(
            "SELECT step FROM work_steps WHERE order_id=? AND inlet_id=?", (order_id, inlet_id))}
        for prev in STEPS[:idx]:
            if prev not in done:
                raise ServiceError(
                    f"工序顺序缺失：须先完成「{STEP_CN[prev]}」才能记录「{STEP_CN[step]}」",
                    "step_order", 409)
        if step in done:
            raise ServiceError(f"「{STEP_CN[step]}」已记录，不能重复", "step_exists", 409)
        ts = now()
        sid = self.db.insert("work_steps", order_id=order_id, inlet_id=inlet_id, step=step,
                             operator=u["name"], note=note, recorded_at=ts)
        if step == "reset":
            self.db.update("order_inlets", "order_id=? AND inlet_id=?", (order_id, inlet_id),
                           reset_checked=1, reset_note=note or "井盖复位检查通过")
        return {"id": sid, "step": step, "recorded_at": ts}

    def save_photo(self, user_id: int, order_id: int, inlet_id: int, phase: str,
                   filename: str, content: bytes) -> dict:
        u = self.require(user_id, "crew")
        if phase not in ("before", "after"):
            raise ServiceError("照片阶段应为 before/after", "bad_phase")
        self._working_order(order_id)
        self._check_inlet_in_order(order_id, inlet_id)
        suffix = Path(filename).suffix or ".svg"
        rel = Path("photos") / f"o{order_id}_i{inlet_id}_{phase}{suffix}"
        path = self.upload_dir / rel
        path.write_bytes(content)
        old = self.db.one("SELECT id,file_path FROM photos WHERE order_id=? AND inlet_id=? AND phase=?",
                          (order_id, inlet_id, phase))
        if old:
            self.db.update("photos", "id=?", (old["id"],), file_path=str(rel),
                           uploaded_by=u["name"], created_at=now())
        else:
            self.db.insert("photos", order_id=order_id, inlet_id=inlet_id, phase=phase,
                           file_path=str(rel), uploaded_by=u["name"], created_at=now())
        # 补拍缺失照片：关闭对应照片缺失事件
        self.db.update("incidents",
                       "order_id=? AND inlet_id=? AND kind='photo_missing' AND status='open'",
                       (order_id, inlet_id), status="handled", handled_at=now(),
                       handled_by=u["name"])
        return {"phase": phase, "file_path": str(rel)}

    def set_bag_count(self, user_id: int, order_id: int, inlet_id: int, bags: int) -> dict:
        self.require(user_id, "crew")
        self._working_order(order_id)
        self._check_inlet_in_order(order_id, inlet_id)
        if not isinstance(bags, int) or bags < 0:
            raise ServiceError("淤泥袋数应为非负整数", "bad_bags")
        self.db.update("order_inlets", "order_id=? AND inlet_id=?", (order_id, inlet_id),
                       bag_count=bags)
        return {"inlet_id": inlet_id, "bag_count": bags}

    def _completion_check(self, order_id: int, inlet_id: int) -> list[str]:
        """返回该点位缺失项（空列表表示可申请验收）。"""
        missing = []
        phases = {r["phase"] for r in self.db.query(
            "SELECT phase FROM photos WHERE order_id=? AND inlet_id=?", (order_id, inlet_id))}
        if "before" not in phases:
            missing.append("清掏前照片")
        if "after" not in phases:
            missing.append("清掏后照片")
        li = self.db.one("SELECT bag_count,reset_checked FROM order_inlets WHERE order_id=? AND inlet_id=?",
                         (order_id, inlet_id))
        if li["bag_count"] is None:
            missing.append("淤泥袋数")
        if not li["reset_checked"]:
            missing.append("复位检查")
        done = {r["step"] for r in self.db.query(
            "SELECT step FROM work_steps WHERE order_id=? AND inlet_id=?", (order_id, inlet_id))}
        for s in STEPS:
            if s not in done:
                missing.append(STEP_CN[s] + "工序")
        return missing

    # ---------- 申请验收 / 验收 ----------
    def submit_acceptance(self, user_id: int, order_id: int, inlet_ids: list[int] | None = None) -> dict:
        """班组申请验收。合单按点位分别申请，材料/工序不全的点位被拦下。"""
        self.require(user_id, "crew")
        o = self._working_order(order_id)
        targets = inlet_ids
        if not targets:
            targets = [r["inlet_id"] for r in self.db.query(
                "SELECT inlet_id FROM order_inlets WHERE order_id=?", (order_id,))]
        blocked: dict[str, list[str]] = {}
        submitted = []
        for iid in targets:
            self._check_inlet_in_order(order_id, iid)
            missing = self._completion_check(order_id, iid)
            if missing:
                code = self.db.one("SELECT code FROM inlets WHERE id=?", (iid,))["code"]
                blocked[code] = missing
                continue
            self.db.update("order_inlets", "order_id=? AND inlet_id=?", (order_id, iid),
                           submit_status="submitted", submitted_at=now())
            submitted.append(iid)
        if blocked:
            raise ServiceError(
                "以下点位材料/工序不齐全，不能申请验收：" +
                "；".join(f"{c} 缺 {'、'.join(m)}" for c, m in blocked.items()),
                "completion_missing", 422, payload={"blocked": blocked, "submitted": submitted})

        # 全部点位都已申请（且此前没有未通过项）→ 工单进入待验收
        pending = self.db.one(
            "SELECT COUNT(*) c FROM order_inlets WHERE order_id=? AND "
            "(submit_status IS NULL OR submit_status='rejected')", (order_id,))["c"]
        if pending == 0:
            self.db.update("work_orders", "id=?", (order_id,), status="submitted")
        return {"submitted": submitted, "order_status":
                self.db.one("SELECT status FROM work_orders WHERE id=?", (order_id,))["status"]}

    def review_acceptance(self, user_id: int, order_id: int, inlet_id: int,
                          passed: bool, comment: str = "") -> dict:
        """验收员逐点位通过/退回；合单不得整单通过，退回继续沿用原工单。"""
        u = self.require(user_id, "inspector")
        o = self.db.one("SELECT * FROM work_orders WHERE id=?", (order_id,))
        if not o:
            raise ServiceError("工单不存在", "not_found", 404)
        if o["status"] not in ("submitted", "rejected", "working"):
            raise ServiceError("该工单未处于验收环节", "state", 409)
        self._check_inlet_in_order(order_id, inlet_id)
        li = self.db.one("SELECT submit_status FROM order_inlets WHERE order_id=? AND inlet_id=?",
                         (order_id, inlet_id))
        if not passed:
            # 退回：原工单继续沿用，点位回到整改
            self.db.update("order_inlets", "order_id=? AND inlet_id=?", (order_id, inlet_id),
                           submit_status="rejected", reject_reason=comment)
            self.db.update("work_orders", "id=?", (order_id,), status="rejected", reject_reason=comment)
            self.db.insert("acceptance_records", order_id=order_id, inlet_id=inlet_id,
                           inspector=u["name"], result="rejected", comment=comment, created_at=now())
            if not comment:
                raise ServiceError("退回必须填写整改意见", "reason_required")
            self.db.insert("notifications", kind="reject",
                           message=f"工单 {o['order_no']} 点位验收退回：{comment}", created_at=now())
            return {"result": "rejected", "order_id": order_id, "inlet_id": inlet_id}

        missing = self._completion_check(order_id, inlet_id)
        if missing:
            # 兜底：材料被删等异常情况，登记照片缺失事件
            self.db.insert("incidents", kind="photo_missing", inlet_id=inlet_id, order_id=order_id,
                           description="验收时发现材料缺失：" + "、".join(missing),
                           handled_by=u["name"], status="open", created_at=now())
            raise ServiceError("验收材料不齐全：" + "、".join(missing), "completion_missing", 422)
        self.db.insert("acceptance_records", order_id=order_id, inlet_id=inlet_id,
                       inspector=u["name"], result="passed", comment=comment, created_at=now())
        self.db.update("order_inlets", "order_id=? AND inlet_id=?", (order_id, inlet_id),
                       submit_status="accepted", accepted_at=now(), reject_reason=None)
        # 该点位恢复正常
        self.db.update("inlets", "id=?", (inlet_id,), status="normal", block_level=0)

        remaining = self.db.one(
            "SELECT COUNT(*) c FROM order_inlets WHERE order_id=? AND submit_status!='accepted'",
            (order_id,))["c"]
        if remaining == 0:
            self.db.update("work_orders", "id=?", (order_id,), status="accepted", accepted_at=now())
            self.db.insert("notifications", kind="accept",
                           message=f"工单 {o['order_no']} 全部点位验收通过", created_at=now())
        return {"result": "passed", "remaining": remaining,
                "order_status": self.db.one("SELECT status FROM work_orders WHERE id=?",
                                            (order_id,))["status"]}

    # ---------- 车辆交接 / 过磅 / 重量修订 ----------
    def trip_arrive(self, user_id: int, trip_id: int) -> dict:
        self.require(user_id, "crew", "dispatcher")
        t = self.db.one("SELECT * FROM haul_trips WHERE id=?", (trip_id,))
        if not t:
            raise ServiceError("车次不存在", "not_found", 404)
        if t["status"] not in ("dispatched", "noshow"):
            raise ServiceError(f"车次状态 {t['status']} 不可签到到场", "state", 409)
        self.db.update("haul_trips", "id=?", (trip_id,), status="arrived", arrived_at=now())
        return {"id": trip_id, "status": "arrived"}

    def handover(self, user_id: int, trip_id: int, bag_count: int, declared_weight_kg: float,
                 handover_crew: str, note: str = "") -> dict:
        """班组与司机现场交接淤泥袋数与自报重量。"""
        self.require(user_id, "crew")
        t = self.db.one("SELECT * FROM haul_trips WHERE id=?", (trip_id,))
        if not t or t["status"] not in ("arrived",):
            raise ServiceError("车辆到场后才能交接", "state", 409)
        if bag_count < 0 or declared_weight_kg < 0:
            raise ServiceError("袋数与重量不能为负", "bad_value")
        self.db.update("haul_trips", "id=?", (trip_id,), status="weighed",
                       bag_count=bag_count, declared_weight_kg=declared_weight_kg,
                       handover_crew=handover_crew, note=note, weighed_at=now())
        # 模拟地磅适配器
        v = self.db.one("SELECT plate FROM vehicles WHERE id=?", (t["vehicle_id"],))
        result = self.weigher.weigh(v["plate"], declared_weight_kg, str(trip_id))
        self.db.update("haul_trips", "id=?", (trip_id,),
                       measured_weight_kg=result["measured_weight_kg"])
        return {"id": trip_id, **result}

    def complete_trip(self, user_id: int, trip_id: int, handover_driver: str | None = None) -> dict:
        self.require(user_id, "crew", "dispatcher")
        t = self.db.one("SELECT * FROM haul_trips WHERE id=?", (trip_id,))
        if not t or t["status"] != "weighed":
            raise ServiceError("过磅后才能完成清运", "state", 409)
        self.db.update("haul_trips", "id=?", (trip_id,), status="completed",
                       handover_driver=handover_driver, completed_at=now())
        return {"id": trip_id, "status": "completed"}

    def revise_weight(self, user_id: int, trip_id: int, revised_weight_kg: float,
                      reason: str) -> dict:
        """清运重量修订仅主管可执行，且必须写明原因。"""
        u = self.require(user_id, "supervisor")
        if not reason or not reason.strip():
            raise ServiceError("修订重量必须写明原因", "reason_required")
        if revised_weight_kg < 0:
            raise ServiceError("修订重量不能为负", "bad_value")
        t = self.db.one("SELECT * FROM haul_trips WHERE id=?", (trip_id,))
        if not t:
            raise ServiceError("车次不存在", "not_found", 404)
        if t["measured_weight_kg"] is None:
            raise ServiceError("该车次尚未过磅，不能修订", "not_weighed", 409)
        self.db.update("haul_trips", "id=?", (trip_id,),
                       revised_weight_kg=revised_weight_kg, revision_reason=reason.strip(),
                       revised_by=u["name"], revised_at=now())
        self.db.insert("notifications", kind="weight_revision",
                       message=f"车次 #{trip_id} 重量由 {t['measured_weight_kg']}kg 修订为 "
                               f"{revised_weight_kg}kg（{u['name']}：{reason.strip()}）",
                       created_at=now())
        return {"id": trip_id, "revised_weight_kg": revised_weight_kg}

    # ---------- 六类应急/特殊处理入口 ----------
    def report_incident(self, user_id: int, kind: str, description: str,
                        inlet_id: int | None = None, order_id: int | None = None,
                        trip_id: int | None = None) -> dict:
        u = self.require(user_id, "patrol", "dispatcher", "crew", "inspector", "supervisor")
        if kind not in EMERGENCY_KINDS:
            raise ServiceError(f"事件类型应为 {EMERGENCY_KINDS}", "bad_kind")
        iid = self.db.insert("incidents", kind=kind, inlet_id=inlet_id, order_id=order_id,
                             trip_id=trip_id, description=description,
                             handled_by=u["name"], status="open", created_at=now())
        self.db.insert("notifications", kind=kind,
                       message=f"应急事件（{kind}）：{description}", created_at=now())
        return {"id": iid, "kind": kind, "status": "open"}

    def rush_before_storm(self, user_id: int, inlet_ids: list[int], storm_at: str,
                          description: str = "") -> dict:
        """暴雨前加急入口：建加急单并生成暴雨前复查提醒。"""
        if not _parse(storm_at):
            raise ServiceError("降雨预计时间格式不正确", "bad_time")
        return self.create_order(user_id, inlet_ids, block_level=2,
                                 description=description or "暴雨前加急清掏",
                                 order_type="rush", urgent=True, storm_at=storm_at)

    def cover_damage(self, user_id: int, inlet_id: int, description: str) -> dict:
        """井盖破损：登记事件、标记井盖、自动建破损处置单。"""
        self.require(user_id, "patrol", "dispatcher")
        if not self.db.one("SELECT id FROM inlets WHERE id=?", (inlet_id,)):
            raise ServiceError("雨水口不存在", "not_found", 404)
        self.db.update("inlets", "id=?", (inlet_id,), cover_ok=0)
        inc = self.report_incident(user_id, "cover_damage", description, inlet_id=inlet_id)
        order = self.create_order(user_id, [inlet_id], block_level=1,
                                  description=f"井盖破损处置：{description}",
                                  order_type="cover_damage", urgent=True,
                                  alert_radius=30, alert_note="井盖破损，周边警戒")
        return {"incident": inc, "order": order}

    def backflow(self, user_id: int, inlet_id: int, description: str) -> dict:
        """积水反涌：加急处置单。"""
        self.require(user_id, "patrol", "dispatcher")
        self.report_incident(user_id, "backflow", description, inlet_id=inlet_id)
        return self.create_order(user_id, [inlet_id], block_level=3,
                                 description=f"积水反涌：{description}",
                                 order_type="backflow", urgent=True,
                                 alert_radius=50, alert_note="积水反涌，禁止靠近")

    def spill_report(self, user_id: int, order_id: int, description: str) -> dict:
        """淤泥遗撒：登记事件，由主管关闭确认。"""
        self.require(user_id, "crew", "patrol", "dispatcher")
        if not self.db.one("SELECT id FROM work_orders WHERE id=?", (order_id,)):
            raise ServiceError("工单不存在", "not_found", 404)
        return self.report_incident(user_id, "spill", description, order_id=order_id)

    def vehicle_noshow(self, user_id: int, trip_id: int, description: str = "车辆未到场") -> dict:
        """车辆未到：车次置 noshow 并登记事件，调度员可重新派车。"""
        u = self.require(user_id, "crew", "dispatcher")
        t = self.db.one("SELECT * FROM haul_trips WHERE id=?", (trip_id,))
        if not t:
            raise ServiceError("车次不存在", "not_found", 404)
        self.db.update("haul_trips", "id=?", (trip_id,), status="noshow", note=description)
        return self.report_incident(u["id"], "vehicle_missing", description,
                                    order_id=t["order_id"], trip_id=trip_id)

    def reassign_vehicle(self, user_id: int, order_id: int, vehicle_id: int,
                         planned_at: str | None = None) -> dict:
        """车辆未到后调度员重新派车。"""
        self.require(user_id, "dispatcher")
        o = self.db.one("SELECT * FROM work_orders WHERE id=?", (order_id,))
        if not o:
            raise ServiceError("工单不存在", "not_found", 404)
        if not self.db.one("SELECT id FROM vehicles WHERE id=? AND active=1", (vehicle_id,)):
            raise ServiceError("车辆不存在或停用", "not_found", 404)
        self.db.update("work_orders", "id=?", (order_id,), vehicle_id=vehicle_id)
        tid = self.db.insert("haul_trips", order_id=order_id, vehicle_id=vehicle_id,
                             planned_at=planned_at or o["planned_start"],
                             status="dispatched", created_at=now(),
                             note="车辆未到后重新派车")
        return {"trip_id": tid, "vehicle_id": vehicle_id}

    def mark_photo_missing(self, user_id: int, order_id: int, inlet_id: int,
                           description: str = "现场照片缺失") -> dict:
        """照片缺失单独入口（验收员/巡查登记），补齐照片后自动关闭。"""
        u = self.require(user_id, "inspector", "patrol", "supervisor")
        self._check_inlet_in_order(order_id, inlet_id)
        return self.report_incident(u["id"], "photo_missing", description,
                                    inlet_id=inlet_id, order_id=order_id)

    def close_incident(self, user_id: int, incident_id: int, note: str = "") -> dict:
        self.require(user_id, "supervisor", "dispatcher")
        r = self.db.one("SELECT * FROM incidents WHERE id=?", (incident_id,))
        if not r:
            raise ServiceError("事件不存在", "not_found", 404)
        self.db.update("incidents", "id=?", (incident_id,), status="handled",
                       handled_at=now(), handled_by=(self.user(user_id)["name"] +
                                                     (f"：{note}" if note else "")))
        return {"id": incident_id, "status": "handled"}

    # ---------- 居民反馈 ----------
    def feedback_list(self) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT f.*, c.name AS contact_name, i.code AS inlet_code FROM residents_feedback f "
            "LEFT JOIN contacts c ON c.id=f.contact_id LEFT JOIN inlets i ON i.id=f.inlet_id "
            "ORDER BY f.id DESC")]

    def link_feedback(self, user_id: int, feedback_id: int, order_id: int) -> dict:
        self.require(user_id, "dispatcher", "patrol")
        if not self.db.one("SELECT id FROM residents_feedback WHERE id=?", (feedback_id,)):
            raise ServiceError("反馈不存在", "not_found", 404)
        self.db.update("residents_feedback", "id=?", (feedback_id,),
                       status="linked", order_id=order_id)
        return {"id": feedback_id, "order_id": order_id}
