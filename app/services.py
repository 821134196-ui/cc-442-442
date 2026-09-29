"""业务服务层：工单、派工、工序、照片、车辆交接、验收、反馈、提醒。

所有写操作在 BEGIN IMMEDIATE 事务中完成，配合冲突再检查，
保证调度员并发分配同一班组时时间冲突会被阻止。
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta

from . import config
from .adapters import adapter
from .auth import CurrentUser
from .db import connect
from .errors import ConflictError, NotFoundError, PermissionError, ServiceError, ValidationError
from .reminders import reminders

# ---------------------------------------------------------------- 工具

def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _get(conn: sqlite3.Connection, sql: str, params=()):
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise NotFoundError()
    return row


def _code(prefix: str, oid: int) -> str:
    return f"{prefix}-{now():%m%d%H%M%S}-{oid:03d}"


def inlet_distance(a: sqlite3.Row, b: sqlite3.Row) -> float:
    # 示意图坐标以米计
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


# ================================================================ 片区/雨水口

class MapService:
    def overview(self) -> dict:
        with connect() as conn:
            segs = [dict(r) for r in conn.execute(
                "SELECT * FROM road_segments ORDER BY code")]
            inlets = [dict(r) for r in conn.execute("""
                SELECT i.*, s.code segment_code, s.name segment_name,
                       (SELECT code FROM work_orders o
                         WHERE o.inlet_id=i.id AND o.status NOT IN ('accepted')
                         ORDER BY o.id DESC LIMIT 1) open_order_code
                FROM inlets i JOIN road_segments s ON s.id=i.segment_id
                ORDER BY i.code
            """)]
        return {"segments": segs, "inlets": inlets}

    def directory(self) -> dict:
        with connect() as conn:
            return {
                "crews": [dict(r) for r in conn.execute("SELECT * FROM crews ORDER BY id")],
                "vehicles": [dict(r) for r in conn.execute("SELECT * FROM vehicles ORDER BY id")],
                "contacts": [dict(r) for r in conn.execute(
                    "SELECT id,name segment_name,code segment_code,contact_name,contact_phone"
                    " FROM road_segments ORDER BY code")],
                "users": [dict(r) for r in conn.execute(
                    "SELECT username,display_name,role,crew_id FROM users ORDER BY role")],
            }


# ================================================================ 工单/巡查

class WorkOrderService:
    def create(self, user: CurrentUser, inlet_id: int, block_level: int,
               issue_kind: str | None = None, issue_note: str | None = None,
               guard_radius_m: float | None = None, guard_note: str | None = None,
               priority: int = 0, schedule_recheck: bool = False,
               recheck_minutes: int | None = None) -> dict:
        if user.role not in (config.ROLE_INSPECTOR, config.ROLE_SUPERVISOR):
            raise PermissionError("仅巡查员/主管可建单")
        if block_level not in (1, 2, 3):
            raise ValidationError("堵塞等级须为 1 轻度 / 2 中度 / 3 严重")
        if issue_kind is not None and issue_kind not in config.ISSUE_KINDS:
            raise ValidationError("未知问题类型")
        if issue_kind == "cover_damaged" and not guard_radius_m:
            raise ValidationError("井盖破损必须划定警戒范围")
        if guard_radius_m is not None and guard_radius_m <= 0:
            raise ValidationError("警戒半径须大于 0")

        conn = connect()
        inlet_code = ""
        try:
            conn.execute("BEGIN IMMEDIATE")
            inlet = _get(conn, "SELECT * FROM inlets WHERE id=?", (inlet_id,))
            inlet_code = inlet["code"]
            dup = conn.execute(
                "SELECT id FROM work_orders WHERE inlet_id=? AND status NOT IN ('accepted')",
                (inlet_id,)).fetchone()
            if dup:
                raise ConflictError(f"{inlet['code']} 已存在未闭环工单，不能重复建单")
            cur = conn.execute(
                "INSERT INTO work_orders(code,inlet_id,segment_id,block_level,issue_kind,"
                "issue_note,created_by,created_at,status,priority,guard_radius_m,guard_note)"
                " VALUES('TMP',?,?,?,?,?,?,?, 'open', ?,?,?)",
                (inlet_id, inlet["segment_id"], block_level, issue_kind,
                 issue_note, user.display_name, _iso(now()), priority,
                 guard_radius_m, guard_note),
            )
            oid = cur.lastrowid
            conn.execute(
                "UPDATE work_orders SET code=? WHERE id=?",
                (f"WO-{now():%m%d%H%M%S}-{oid:03d}", oid))
            conn.execute(
                "UPDATE inlets SET status='blocked',block_level=?,updated_at=? WHERE id=?",
                (block_level, _iso(now()), inlet_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        order_id = oid
        if schedule_recheck or issue_kind == "storm_urgent":
            minutes = recheck_minutes or config.STORM_RECHECK_SECONDS // 60
            reminders.schedule(
                inlet_id, order_id, now() + timedelta(minutes=minutes),
                f"暴雨前复查：{inlet_code} 须在降雨前再次开盖核查")
        return self.get(order_id)

    def report_issue(self, user: CurrentUser, order_id: int, issue_kind: str,
                     note: str | None = None) -> dict:
        """现场追加专项问题（积水反涌 / 淤泥遗撒 / 井盖破损等）。"""
        if user.role not in (config.ROLE_INSPECTOR, config.ROLE_CREW,
                             config.ROLE_DISPATCHER, config.ROLE_SUPERVISOR):
            raise PermissionError("当前角色不能上报问题")
        if issue_kind not in config.ISSUE_KINDS:
            raise ValidationError("未知问题类型")
        with connect() as conn:
            order = _get(conn, "SELECT * FROM work_orders WHERE id=?", (order_id,))
            merged_note = "; ".join(x for x in (order["issue_note"], note) if x)
            conn.execute(
                "UPDATE work_orders SET issue_kind=?, issue_note=? WHERE id=?",
                (issue_kind, merged_note, order_id))
            if issue_kind == "cover_damaged" and not order["guard_radius_m"]:
                conn.execute(
                    "UPDATE work_orders SET guard_radius_m=10, guard_note=COALESCE(guard_note,'井盖破损，扩大警戒')"
                    " WHERE id=?", (order_id,))
            conn.commit()
        return self.get(order_id)

    def mark_photo_missing(self, user: CurrentUser, order_id: int,
                           note: str | None = None) -> dict:
        """照片缺失专项入口：登记缺失项，验收门禁会继续拦截直到补齐。"""
        if user.role not in (config.ROLE_INSPECTOR, config.ROLE_CREW,
                             config.ROLE_DISPATCHER,
                             config.ROLE_SUPERVISOR):
            raise PermissionError("当前角色不能登记照片缺失")
        with connect() as conn:
            _get(conn, "SELECT id FROM work_orders WHERE id=?", (order_id,))
            row = conn.execute("SELECT issue_note FROM work_orders WHERE id=?",
                               (order_id,)).fetchone()
            tag_text = "【照片缺失】现场未拍齐，待补传后再申请验收"
            text = "; ".join(x for x in (row["issue_note"], note, tag_text) if x)
            conn.execute(
                "UPDATE work_orders SET issue_kind='photo_missing', issue_note=? WHERE id=?",
                (text, order_id))
            conn.commit()
        return self.get(order_id)

    def list(self, status: str | None = None, issue_kind: str | None = None,
             segment_id: int | None = None) -> list[dict]:
        sql = """SELECT o.*, i.code inlet_code, i.x, i.y, s.name segment_name,
                        (SELECT COUNT(*) FROM dispatches d WHERE d.order_id=o.id) dispatch_count
                 FROM work_orders o
                 JOIN inlets i ON i.id=o.inlet_id
                 JOIN road_segments s ON s.id=o.segment_id
                 WHERE 1=1"""
        params: list = []
        if status:
            sql += " AND o.status=?"; params.append(status)
        if issue_kind:
            sql += " AND o.issue_kind=?"; params.append(issue_kind)
        if segment_id:
            sql += " AND o.segment_id=?"; params.append(segment_id)
        sql += " ORDER BY o.priority DESC, o.id"
        with connect() as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def get(self, order_id: int) -> dict:
        with connect() as conn:
            order = conn.execute("""
                SELECT o.*, i.code inlet_code, i.x, i.y, s.name segment_name,
                       s.contact_name, s.contact_phone
                FROM work_orders o
                JOIN inlets i ON i.id=o.inlet_id
                JOIN road_segments s ON s.id=o.segment_id
                WHERE o.id=?""", (order_id,)).fetchone()
            if not order:
                raise NotFoundError("工单不存在")
            data = dict(order)
            data["dispatches"] = [dict(r) for r in conn.execute(
                "SELECT d.*, c.name crew_name, v.plate vehicle_plate,"
                " (SELECT COUNT(*) FROM work_steps ws WHERE ws.dispatch_id=d.id) step_count"
                " FROM dispatches d JOIN crews c ON c.id=d.crew_id"
                " LEFT JOIN vehicles v ON v.id=d.vehicle_id"
                " WHERE d.order_id=? ORDER BY d.id", (order_id,))]
            data["photos"] = [dict(r) for r in conn.execute(
                "SELECT * FROM photos WHERE order_id=? ORDER BY uploaded_at", (order_id,))]
            data["acceptances"] = [dict(r) for r in conn.execute(
                "SELECT * FROM acceptances WHERE order_id=? ORDER BY id", (order_id,))]
            data["feedbacks"] = [dict(r) for r in conn.execute(
                "SELECT * FROM feedbacks WHERE order_id=? ORDER BY id", (order_id,))]
            groups = conn.execute(
                "SELECT mg.* FROM merge_groups mg"
                " JOIN merge_group_items mgi ON mgi.group_id=mg.id"
                " WHERE mgi.order_id=?", (order_id,)).fetchall()
            data["merge_groups"] = []
            for g in groups:
                items = [dict(r) for r in conn.execute("""
                    SELECT o.id, o.code, i.code inlet_code FROM merge_group_items mgi
                    JOIN work_orders o ON o.id=mgi.order_id
                    JOIN inlets i ON i.id=o.inlet_id
                    WHERE mgi.group_id=?""", (g["id"],))]
                gd = dict(g); gd["orders"] = items
                data["merge_groups"].append(gd)
            return data


# ================================================================ 派工 / 合单 / 日程

class DispatchService:
    # -- 合单边界
    def _validate_merge(self, conn, orders: list[sqlite3.Row]) -> None:
        if len(orders) < 2:
            return
        seg = orders[0]["segment_id"]
        if any(o["segment_id"] != seg for o in orders):
            raise ValidationError("只有同一道路区段内的点位才能合并派车")
        inlets = {o["inlet_id"]: _get(conn, "SELECT * FROM inlets WHERE id=?",
                                      (o["inlet_id"],)) for o in orders}
        anchor = inlets[orders[0]["inlet_id"]]
        for o in orders[1:]:
            d = inlet_distance(anchor, inlets[o["inlet_id"]])
            if d > config.MERGE_MAX_DISTANCE:
                raise ValidationError(
                    f"点位相距 {d:.0f}m，超过相邻合单上限 {config.MERGE_MAX_DISTANCE:.0f}m，不能合并派车")

    def _check_conflict(self, conn, crew_id: int, start: str, end: str) -> None:
        row = conn.execute("""
            SELECT d.code, d.start_at, d.end_at, c.name crew_name FROM dispatches d
            JOIN crews c ON c.id=d.crew_id
            WHERE d.crew_id=? AND d.status!='cancelled'
              AND d.start_at < ? AND d.end_at > ?
            ORDER BY d.start_at LIMIT 1
        """, (crew_id, end, start)).fetchone()
        if row:
            raise ConflictError(
                f"时间冲突：{row['crew_name']} 在 {row['start_at']} ~ {row['end_at']}"
                f" 已有派工 {row['code']}", "schedule_conflict")

    def dispatch(self, user: CurrentUser, order_ids: list[int], crew_id: int,
                 start_at: str, end_at: str, vehicle_id: int | None = None,
                 note: str | None = None) -> dict:
        if user.role not in (config.ROLE_DISPATCHER, config.ROLE_SUPERVISOR):
            raise PermissionError("仅调度员/主管可派工")
        if not order_ids:
            raise ValidationError("请至少选择一个工单")
        if len(set(order_ids)) != len(order_ids):
            raise ValidationError("合单工单不能重复")
        start, end = _parse(start_at), _parse(end_at)
        if end <= start:
            raise ValidationError("结束时间必须晚于开始时间")

        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")  # 串行化并发派工
            crew = _get(conn, "SELECT * FROM crews WHERE id=?", (crew_id,))
            if vehicle_id is not None:
                _get(conn, "SELECT * FROM vehicles WHERE id=?", (vehicle_id,))
            orders = [_get(conn, "SELECT * FROM work_orders WHERE id=?", (oid,))
                      for oid in order_ids]
            for o in orders:
                if o["status"] not in ("open", "rejected"):
                    raise ConflictError(
                        f"工单 {o['code']} 当前状态 {o['status']}，不能派工")
            self._validate_merge(conn, orders)
            self._check_conflict(conn, crew_id, _iso(start), _iso(end))

            group_id = None
            if len(orders) > 1:
                cur = conn.execute(
                    "INSERT INTO merge_groups(code,created_at,note) VALUES(?,?,?)",
                    (f"MG-{now():%m%d%H%M%S}", _iso(now()), note))
                group_id = cur.lastrowid
                conn.execute(
                    "UPDATE merge_groups SET code=? WHERE id=?",
                    (f"MG-{now():%m%d%H%M%S}-{group_id:03d}", group_id))
                for o in orders:
                    conn.execute(
                        "INSERT INTO merge_group_items(group_id,order_id) VALUES(?,?)",
                        (group_id, o["id"]))

            dispatch_ids = []
            for o in orders:
                cur = conn.execute(
                    "INSERT INTO dispatches(code,order_id,crew_id,vehicle_id,start_at,end_at,"
                    "status,merged_group_id,created_at)"
                    " VALUES(?,?,?,?,?,?,'scheduled',?,?)",
                    (f"DSP-TMP-{o['id']}", o["id"], crew_id, vehicle_id,
                     _iso(start), _iso(end), group_id, _iso(now())))
                did = cur.lastrowid
                conn.execute(
                    "UPDATE dispatches SET code=? WHERE id=?",
                    (f"DSP-{now():%m%d%H%M%S}-{did:03d}", did))
                conn.execute(
                    "INSERT INTO vehicle_handovers(dispatch_id,vehicle_id,planned_arrival,"
                    "status,updated_at) VALUES(?,?,?,'waiting',?)",
                    (did, vehicle_id, _iso(start), _iso(now())))
                conn.execute(
                    "UPDATE work_orders SET status='dispatched', reject_reason=NULL WHERE id=?",
                    (o["id"],))
                conn.execute(
                    "UPDATE inlets SET status='cleaning',updated_at=?"
                    " WHERE id=(SELECT inlet_id FROM work_orders WHERE id=?)",
                    (_iso(now()), o["id"]))
                dispatch_ids.append(did)
            if group_id:
                conn.execute("UPDATE merge_groups SET dispatch_id=? WHERE id=?",
                             (dispatch_ids[0], group_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"dispatch_ids": dispatch_ids, "merge_group_id": group_id,
                "crew": crew["name"], "start_at": _iso(start), "end_at": _iso(end)}

    def schedule(self, crew_id: int | None = None, date_from: str | None = None,
                 date_to: str | None = None) -> list[dict]:
        sql = """SELECT d.*, c.name crew_name, c.leader, c.phone crew_phone,
                        v.plate vehicle_plate, o.code order_code, i.code inlet_code,
                        s.name segment_name, mg.id merge_group_id
                 FROM dispatches d
                 JOIN crews c ON c.id=d.crew_id
                 JOIN work_orders o ON o.id=d.order_id
                 JOIN inlets i ON i.id=o.inlet_id
                 JOIN road_segments s ON s.id=o.segment_id
                 LEFT JOIN vehicles v ON v.id=d.vehicle_id
                 LEFT JOIN merge_groups mg ON mg.id=d.merged_group_id
                 WHERE 1=1"""
        params: list = []
        if crew_id:
            sql += " AND d.crew_id=?"; params.append(crew_id)
        if date_from:
            sql += " AND d.end_at>=?"; params.append(date_from)
        if date_to:
            sql += " AND d.start_at<=?"; params.append(date_to)
        sql += " ORDER BY d.start_at, d.id"
        with connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params)]
        return rows

    def get(self, dispatch_id: int) -> dict:
        with connect() as conn:
            d = conn.execute("""
                SELECT d.*, c.name crew_name, c.id crew_row_id, v.plate vehicle_plate,
                       o.code order_code, o.status order_status, i.code inlet_code,
                       s.name segment_name, mg.id merge_group_id
                FROM dispatches d
                JOIN crews c ON c.id=d.crew_id
                JOIN work_orders o ON o.id=d.order_id
                JOIN inlets i ON i.id=o.inlet_id
                JOIN road_segments s ON s.id=o.segment_id
                LEFT JOIN vehicles v ON v.id=d.vehicle_id
                LEFT JOIN merge_groups mg ON mg.id=d.merged_group_id
                WHERE d.id=?""", (dispatch_id,)).fetchone()
            if not d:
                raise NotFoundError("派工单不存在")
            data = dict(d)
            data["steps"] = [dict(r) for r in conn.execute(
                "SELECT * FROM work_steps WHERE dispatch_id=? ORDER BY done_at", (dispatch_id,))]
            out = conn.execute(
                "SELECT * FROM clean_outputs WHERE dispatch_id=?", (dispatch_id,)).fetchone()
            data["output"] = dict(out) if out else None
            hv = conn.execute(
                "SELECT * FROM vehicle_handovers WHERE dispatch_id=?", (dispatch_id,)).fetchone()
            data["handover"] = dict(hv) if hv else None
            data["photos"] = [dict(r) for r in conn.execute(
                "SELECT * FROM photos WHERE dispatch_id=? ORDER BY uploaded_at", (dispatch_id,))]
            return data


# ================================================================ 现场工序

class StepService:
    def record(self, user: CurrentUser, dispatch_id: int, step: str,
               note: str | None = None) -> dict:
        if user.role not in (config.ROLE_CREW, config.ROLE_SUPERVISOR):
            raise PermissionError("仅班组/主管可记录工序")
        if step not in config.STEP_ORDER:
            raise ValidationError("工序须为 open/clean/flush/reset")
        with connect() as conn:
            d = _get(conn, "SELECT * FROM dispatches WHERE id=?", (dispatch_id,))
            if user.role == config.ROLE_CREW and user.crew_id != d["crew_id"]:
                raise PermissionError("只能记录本班组的派工")
            if d["status"] == "cancelled":
                raise ConflictError("派工已取消")
            # 工序必须依次：开盖 -> 清掏 -> 冲洗 -> 复位
            idx = config.STEP_ORDER.index(step)
            for prev in config.STEP_ORDER[:idx]:
                exists = conn.execute(
                    "SELECT 1 FROM work_steps WHERE dispatch_id=? AND step=?",
                    (dispatch_id, prev)).fetchone()
                if not exists:
                    raise ValidationError(
                        f"需先完成「{config.STEP_LABELS[prev]}」才能记录"
                        f"「{config.STEP_LABELS[step]}」")
            if conn.execute("SELECT 1 FROM work_steps WHERE dispatch_id=? AND step=?",
                            (dispatch_id, step)).fetchone():
                raise ConflictError(f"「{config.STEP_LABELS[step]}」已记录")
            conn.execute(
                "INSERT INTO work_steps(dispatch_id,step,done_at,operator,note)"
                " VALUES(?,?,?,?,?)",
                (dispatch_id, step, _iso(now()), user.display_name, note))
            new_status = "arrived" if d["status"] == "scheduled" else d["status"]
            if step in ("clean", "flush", "reset"):
                new_status = "working"
            conn.execute("UPDATE dispatches SET status=? WHERE id=?",
                         (new_status, dispatch_id))
            conn.commit()
        return DispatchService().get(dispatch_id)

    def record_bags(self, user: CurrentUser, dispatch_id: int, bag_count: int) -> dict:
        if user.role not in (config.ROLE_CREW, config.ROLE_SUPERVISOR,
                             config.ROLE_DISPATCHER):
            raise PermissionError("仅班组/调度/主管可登记袋数")
        if bag_count < 0:
            raise ValidationError("淤泥袋数不能为负")
        with connect() as conn:
            d = _get(conn, "SELECT * FROM dispatches WHERE id=?", (dispatch_id,))
            if user.role == config.ROLE_CREW and user.crew_id != d["crew_id"]:
                raise PermissionError("只能记录本班组的派工")
            if not conn.execute("SELECT 1 FROM work_steps WHERE dispatch_id=? AND step='clean'",
                                (dispatch_id,)).fetchone():
                raise ValidationError("需完成「清掏」工序后才能登记淤泥袋数")
            row = conn.execute(
                "SELECT id FROM clean_outputs WHERE dispatch_id=?", (dispatch_id,)).fetchone()
            if row:
                conn.execute("UPDATE clean_outputs SET bag_count=? WHERE dispatch_id=?",
                             (bag_count, dispatch_id))
            else:
                conn.execute(
                    "INSERT INTO clean_outputs(dispatch_id,bag_count,created_at)"
                    " VALUES(?,?,?)", (dispatch_id, bag_count, _iso(now())))
            conn.commit()
        return DispatchService().get(dispatch_id)


# ================================================================ 照片

class PhotoService:
    def upload(self, user: CurrentUser, order_id: int, phase: str, kind: str,
               filename: str, data: bytes, dispatch_id: int | None = None) -> dict:
        if user.role not in (config.ROLE_INSPECTOR, config.ROLE_CREW,
                             config.ROLE_SUPERVISOR, config.ROLE_DISPATCHER):
            raise PermissionError("当前角色不能上传照片")
        if phase not in ("before", "after"):
            raise ValidationError("phase 须为 before/after")
        if kind not in ("clean", "reset"):
            raise ValidationError("kind 须为 clean/reset")
        if phase == "after" and user.role == config.ROLE_INSPECTOR:
            raise PermissionError("清掏后照片由班组上传")
        if not data:
            raise ValidationError("照片内容为空")
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in config.ALLOWED_PHOTO_EXT:
            raise ValidationError("仅支持 jpg/png/webp/svg 照片")
        with connect() as conn:
            order = _get(conn, "SELECT id FROM work_orders WHERE id=?", (order_id,))
            if dispatch_id is not None:
                _get(conn, "SELECT id FROM dispatches WHERE id=? AND order_id=?",
                     (dispatch_id, order_id))
            rel_dir = f"photos/order_{order_id}"
            abs_dir = config.UPLOAD_DIR / rel_dir
            abs_dir.mkdir(parents=True, exist_ok=True)
            n = conn.execute(
                "SELECT COUNT(*) c FROM photos WHERE order_id=? AND phase=? AND kind=?",
                (order_id, phase, kind)).fetchone()["c"]
            rel_path = f"{rel_dir}/{phase}_{kind}_{n + 1}.{ext}"
            (config.UPLOAD_DIR / rel_path).write_bytes(data)
            cur = conn.execute(
                "INSERT INTO photos(order_id,dispatch_id,phase,kind,path,uploaded_by,uploaded_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (order_id, dispatch_id, phase, kind, rel_path,
                 user.display_name, _iso(now())))
            pid = cur.lastrowid
            conn.commit()
        return {"id": pid, "path": rel_path}


# ================================================================ 车辆交接 / 过磅 / 重量修订

class VehicleService:
    def _set_handover(self, conn, dispatch_id: int, **fields):
        _get(conn, "SELECT id FROM vehicle_handovers WHERE dispatch_id=?", (dispatch_id,))
        fields["updated_at"] = _iso(now())
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE vehicle_handovers SET {sets} WHERE dispatch_id=?",
                     (*fields.values(), dispatch_id))

    def arrive(self, user: CurrentUser, dispatch_id: int) -> dict:
        if user.role not in (config.ROLE_CREW, config.ROLE_DISPATCHER, config.ROLE_SUPERVISOR):
            raise PermissionError("仅班组/调度/主管可登记车辆到场")
        with connect() as conn:
            d = _get(conn, "SELECT * FROM dispatches WHERE id=?", (dispatch_id,))
            if user.role == config.ROLE_CREW and user.crew_id != d["crew_id"]:
                raise PermissionError("只能记录本班组的交接")
            self._set_handover(conn, dispatch_id, status="arrived",
                               arrived_at=_iso(now()), updated_by=user.display_name)
            if conn.execute("SELECT status FROM dispatches WHERE id=?",
                            (dispatch_id,)).fetchone()["status"] == "scheduled":
                conn.execute("UPDATE dispatches SET status='arrived' WHERE id=?",
                             (dispatch_id,))
            conn.commit()
        return DispatchService().get(dispatch_id)

    def mark_missing(self, user: CurrentUser, dispatch_id: int,
                     note: str | None = None) -> dict:
        """车辆未到专项入口。"""
        if user.role not in (config.ROLE_CREW, config.ROLE_DISPATCHER, config.ROLE_SUPERVISOR):
            raise PermissionError("仅班组/调度/主管可登记车辆未到")
        with connect() as conn:
            d = _get(conn, "SELECT * FROM dispatches WHERE id=?", (dispatch_id,))
            if user.role == config.ROLE_CREW and user.crew_id != d["crew_id"]:
                raise PermissionError("只能记录本班组的交接")
            self._set_handover(conn, dispatch_id, status="missing",
                               note=note or "车辆未按计划到场，已通知调度改派",
                               updated_by=user.display_name)
            conn.execute("UPDATE dispatches SET status='vehicle_missing' WHERE id=?",
                         (dispatch_id,))
            conn.commit()
        return DispatchService().get(dispatch_id)

    def assign_vehicle(self, user: CurrentUser, dispatch_id: int,
                       vehicle_id: int) -> dict:
        """改派车辆（车辆未到后）。合单内全部派工一并换车。"""
        if user.role not in (config.ROLE_DISPATCHER, config.ROLE_SUPERVISOR):
            raise PermissionError("仅调度员/主管可指派车辆")
        with connect() as conn:
            _get(conn, "SELECT * FROM vehicles WHERE id=?", (vehicle_id,))
            d = _get(conn, "SELECT * FROM dispatches WHERE id=?", (dispatch_id,))
            ids = [dispatch_id]
            if d["merged_group_id"]:
                ids = [r[0] for r in conn.execute(
                    "SELECT id FROM dispatches WHERE merged_group_id=?",
                    (d["merged_group_id"],))]
            for did in ids:
                conn.execute("UPDATE dispatches SET vehicle_id=? WHERE id=?",
                             (vehicle_id, did))
                self._set_handover(conn, did, vehicle_id=vehicle_id, status="waiting",
                                   updated_by=user.display_name)
            conn.commit()
        return DispatchService().get(dispatch_id)

    def weigh(self, user: CurrentUser, dispatch_id: int) -> dict:
        """调用模拟地磅适配器过磅。"""
        if user.role not in (config.ROLE_DISPATCHER, config.ROLE_CREW, config.ROLE_SUPERVISOR):
            raise PermissionError("仅调度/班组/主管可登记过磅")
        with connect() as conn:
            d = _get(conn, "SELECT * FROM dispatches WHERE id=?", (dispatch_id,))
            if user.role == config.ROLE_CREW and user.crew_id != d["crew_id"]:
                raise PermissionError("只能记录本班组的交接")
            out = conn.execute(
                "SELECT * FROM clean_outputs WHERE dispatch_id=?",
                (dispatch_id,)).fetchone()
            if not out or out["bag_count"] is None:
                raise ValidationError("请先登记淤泥袋数再去过磅")
            result = adapter.weigh(d["code"], out["bag_count"])
            if out:
                conn.execute(
                    "UPDATE clean_outputs SET measured_kg=?, weighed_at=? WHERE dispatch_id=?",
                    (result["net_kg"], _iso(now()), dispatch_id))
            else:
                conn.execute(
                    "INSERT INTO clean_outputs(dispatch_id,bag_count,measured_kg,weighed_at,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (dispatch_id, 0, result["net_kg"], _iso(now()), _iso(now())))
            self._set_handover(conn, dispatch_id, status="handed",
                               bags_loaded=out["bag_count"], net_kg=result["net_kg"],
                               updated_by=user.display_name)
            conn.commit()
        result["dispatch_id"] = dispatch_id
        return result

    def revise_weight(self, user: CurrentUser, dispatch_id: int, new_kg: float,
                      reason: str) -> dict:
        """清运重量修订：仅主管，必须写明原因，并留审计记录。"""
        if user.role != config.ROLE_SUPERVISOR:
            raise PermissionError("清运重量修订仅主管可执行")
        if not reason or not reason.strip():
            raise ValidationError("主管修订重量必须写明原因")
        if new_kg < 0:
            raise ValidationError("重量不能为负")
        with connect() as conn:
            out = conn.execute(
                "SELECT * FROM clean_outputs WHERE dispatch_id=?",
                (dispatch_id,)).fetchone()
            if not out or out["measured_kg"] is None:
                raise ValidationError("尚未过磅，无法修订")
            conn.execute(
                "UPDATE clean_outputs SET revised_kg=?, revise_reason=?, revised_by=?,"
                " revised_at=? WHERE dispatch_id=?",
                (new_kg, reason.strip(), user.display_name, _iso(now()), dispatch_id))
            conn.execute(
                "INSERT INTO weight_revisions(dispatch_id,old_kg,new_kg,reason,operator,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (dispatch_id, out["measured_kg"], new_kg, reason.strip(),
                 user.display_name, _iso(now())))
            hv = conn.execute(
                "SELECT id FROM vehicle_handovers WHERE dispatch_id=?",
                (dispatch_id,)).fetchone()
            if hv:
                conn.execute(
                    "UPDATE vehicle_handovers SET net_kg=?, updated_by=?, updated_at=?",
                    (new_kg, user.display_name, _iso(now())))
            conn.commit()
            revisions = [dict(r) for r in conn.execute(
                "SELECT * FROM weight_revisions WHERE dispatch_id=? ORDER BY id",
                (dispatch_id,))]
        return {"dispatch_id": dispatch_id, "final_kg": new_kg, "revisions": revisions}

    def handovers(self) -> list[dict]:
        with connect() as conn:
            return [dict(r) for r in conn.execute("""
                SELECT h.*, d.code dispatch_code, d.start_at, d.end_at, d.status dispatch_status,
                       d.crew_id, c.name crew_name, v.plate vehicle_plate,
                       o.code order_code, i.code inlet_code,
                       co.bag_count, co.measured_kg, co.revised_kg
                FROM vehicle_handovers h
                JOIN dispatches d ON d.id=h.dispatch_id
                JOIN crews c ON c.id=d.crew_id
                JOIN work_orders o ON o.id=d.order_id
                JOIN inlets i ON i.id=o.inlet_id
                LEFT JOIN vehicles v ON v.id=h.vehicle_id
                LEFT JOIN clean_outputs co ON co.dispatch_id=h.dispatch_id
                ORDER BY h.id DESC""")]


# ================================================================ 验收

class AcceptanceService:
    def _completeness(self, conn, order: sqlite3.Row) -> list[str]:
        """返回缺失项；空列表表示齐全。"""
        missing: list[str] = []
        d = conn.execute(
            "SELECT * FROM dispatches WHERE order_id=? AND status!='cancelled'"
            " ORDER BY id DESC LIMIT 1", (order["id"],)).fetchone()
        if not d:
            return ["尚未派工"]
        steps = {r["step"] for r in conn.execute(
            "SELECT step FROM work_steps WHERE dispatch_id=?", (d["id"],))}
        for s in config.STEP_ORDER:
            if s not in steps:
                missing.append(f"缺少工序：{config.STEP_LABELS[s]}")
        out = conn.execute(
            "SELECT * FROM clean_outputs WHERE dispatch_id=?", (d["id"],)).fetchone()
        if not out or out["bag_count"] is None:
            missing.append("缺少淤泥袋数登记")
        elif order["block_level"] >= 2 and out["bag_count"] < 1:
            missing.append("中度及以上堵塞清掏袋数不能为 0")
        before = conn.execute(
            "SELECT COUNT(*) c FROM photos WHERE order_id=? AND phase='before' AND kind='clean'",
            (order["id"],)).fetchone()["c"]
        after_clean = conn.execute(
            "SELECT COUNT(*) c FROM photos WHERE dispatch_id=? AND phase='after' AND kind='clean'",
            (d["id"],)).fetchone()["c"]
        after_reset = conn.execute(
            "SELECT COUNT(*) c FROM photos WHERE dispatch_id=? AND phase='after' AND kind='reset'",
            (d["id"],)).fetchone()["c"]
        if not before:
            missing.append("缺少清掏前照片")
        if not after_clean:
            missing.append("缺少清掏后照片")
        if not after_reset:
            missing.append("缺少复位检查照片")
        return missing

    def request(self, user: CurrentUser, order_id: int) -> dict:
        if user.role not in (config.ROLE_CREW, config.ROLE_DISPATCHER, config.ROLE_SUPERVISOR):
            raise PermissionError("仅班组/调度/主管可申请验收")
        with connect() as conn:
            order = _get(conn, "SELECT * FROM work_orders WHERE id=?", (order_id,))
            if order["status"] not in ("working", "dispatched", "arrived", "rejected", "ready"):
                raise ConflictError(
                    f"工单 {order['code']} 当前状态 {order['status']}，不能申请验收")
            missing = self._completeness(conn, order)
            if missing:
                raise ValidationError("验收资料不齐全：" + "；".join(missing))
            d = conn.execute(
                "SELECT * FROM dispatches WHERE order_id=? AND status!='cancelled'"
                " ORDER BY id DESC LIMIT 1", (order_id,)).fetchone()
            if user.role == config.ROLE_CREW and user.crew_id != d["crew_id"]:
                raise PermissionError("只能申请本班组派工的验收")
            conn.execute(
                "INSERT INTO acceptances(order_id,dispatch_id,result,reviewer,comment,created_at)"
                " VALUES(?,?,'pending',?,NULL,?)",
                (order_id, d["id"], user.display_name, _iso(now())))
            conn.execute("UPDATE work_orders SET status='pending_accept' WHERE id=?", (order_id,))
            conn.execute("UPDATE inlets SET status='pending_accept',updated_at=?"
                         " WHERE id=(SELECT inlet_id FROM work_orders WHERE id=?)",
                         (_iso(now()), order_id))
            conn.commit()
        return WorkOrderService().get(order_id)

    def decide(self, user: CurrentUser, order_id: int, result: str,
               comment: str | None = None) -> dict:
        if user.role != config.ROLE_ACCEPTOR:
            raise PermissionError("仅验收员可作出验收结论")
        if result not in ("pass", "reject"):
            raise ValidationError("结论须为 pass/reject")
        if result == "reject" and not comment:
            raise ValidationError("退回必须填写原因")
        with connect() as conn:
            order = _get(conn, "SELECT * FROM work_orders WHERE id=?", (order_id,))
            if order["status"] != "pending_accept":
                raise ConflictError("该工单不在待验收状态")
            acc = conn.execute(
                "SELECT * FROM acceptances WHERE order_id=? AND result='pending'"
                " ORDER BY id DESC LIMIT 1", (order_id,)).fetchone()
            if not acc:
                raise ConflictError("没有待处理的验收申请")
            conn.execute(
                "UPDATE acceptances SET result=?, reviewer=?, comment=?, decided_at=? WHERE id=?",
                (result, user.display_name, comment, _iso(now()), acc["id"]))
            if result == "pass":
                # 验收通过：雨水口回到正常状态
                conn.execute(
                    "UPDATE work_orders SET status='accepted', accepted_at=? WHERE id=?",
                    (_iso(now()), order_id))
                conn.execute(
                    "UPDATE inlets SET status='normal', block_level=0, updated_at=? WHERE id=?",
                    (_iso(now()), order["inlet_id"]))
                conn.execute(
                    "UPDATE dispatches SET status='completed' WHERE id=?",
                    (acc["dispatch_id"],))
            else:
                # 退回：继续沿用原工单（同号），等待重新派工/重做工序
                conn.execute(
                    "UPDATE work_orders SET status='rejected', reject_reason=? WHERE id=?",
                    (comment, order_id))
                conn.execute(
                    "UPDATE inlets SET status='blocked', updated_at=? WHERE id=?",
                    (_iso(now()), order["inlet_id"]))
            conn.commit()
        return WorkOrderService().get(order_id)

    def ledger(self, result: str | None = None) -> list[dict]:
        sql = """SELECT a.*, o.code order_code, i.code inlet_code, s.name segment_name,
                        d.code dispatch_code, c.name crew_name
                 FROM acceptances a
                 JOIN work_orders o ON o.id=a.order_id
                 JOIN inlets i ON i.id=o.inlet_id
                 JOIN road_segments s ON s.id=o.segment_id
                 JOIN dispatches d ON d.id=a.dispatch_id
                 JOIN crews c ON c.id=d.crew_id WHERE 1=1"""
        params: list = []
        if result:
            sql += " AND a.result=?"; params.append(result)
        sql += " ORDER BY a.id DESC"
        with connect() as conn:
            return [dict(r) for r in conn.execute(sql, params)]


# ================================================================ 居民反馈 / 提醒

class FeedbackService:
    def create(self, user: CurrentUser, content: str, reporter: str,
               contact: str | None = None, category: str | None = None,
               inlet_id: int | None = None, order_id: int | None = None) -> dict:
        if not content.strip():
            raise ValidationError("反馈内容不能为空")
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO feedbacks(inlet_id,order_id,reporter,contact,content,category,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (inlet_id, order_id, reporter, contact, content.strip(),
                 category, _iso(now())))
            fid = cur.lastrowid
            conn.commit()
        return {"id": fid}

    def list(self, handled: int | None = None) -> list[dict]:
        sql = """SELECT f.*, i.code inlet_code, o.code order_code FROM feedbacks f
                 LEFT JOIN inlets i ON i.id=f.inlet_id
                 LEFT JOIN work_orders o ON o.id=f.order_id WHERE 1=1"""
        params: list = []
        if handled is not None:
            sql += " AND f.handled=?"; params.append(handled)
        sql += " ORDER BY f.handled, f.id DESC"
        with connect() as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def handle(self, user: CurrentUser, feedback_id: int, note: str) -> dict:
        if user.role not in (config.ROLE_DISPATCHER, config.ROLE_SUPERVISOR, config.ROLE_INSPECTOR):
            raise PermissionError("仅巡查/调度/主管可处理反馈")
        if not note.strip():
            raise ValidationError("处理说明不能为空")
        with connect() as conn:
            _get(conn, "SELECT id FROM feedbacks WHERE id=?", (feedback_id,))
            conn.execute("UPDATE feedbacks SET handled=1, handle_note=? WHERE id=?",
                         (note.strip(), feedback_id))
            conn.commit()
        return {"id": feedback_id, "handled": 1}


class ReminderService:
    def create(self, user: CurrentUser, inlet_id: int | None, order_id: int | None,
               due_at: str, message: str) -> dict:
        if user.role not in (config.ROLE_INSPECTOR, config.ROLE_DISPATCHER, config.ROLE_SUPERVISOR):
            raise PermissionError("仅巡查/调度/主管可设置复查提醒")
        if not message.strip():
            raise ValidationError("提醒内容不能为空")
        rid = reminders.schedule(inlet_id, order_id, _parse(due_at), message.strip())
        return {"id": rid}

    def list_pending(self) -> list[dict]:
        return reminders.list_pending()

    def fire_due(self) -> list[dict]:
        return reminders.fire_due()


# 单例，便于接口层直接引用
map_service = MapService()
order_service = WorkOrderService()
dispatch_service = DispatchService()
step_service = StepService()
photo_service = PhotoService()
vehicle_service = VehicleService()
acceptance_service = AcceptanceService()
feedback_service = FeedbackService()
reminder_service = ReminderService()
