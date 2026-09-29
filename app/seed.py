"""载入本地样例：道路区段、雨水口、班组、车辆、联系人，
以及堵塞 / 退回 / 已完成三类工单样例。幂等：已存在则跳过。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from . import config
from .db import connect, init_db

BLOCK_LABEL = {0: "正常", 1: "轻度", 2: "中度", 3: "严重"}


def now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _write_sample_photo(path: Path, title: str, color: str, subtitle: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480" viewBox="0 0 640 480">
  <rect width="640" height="480" fill="{color}"/>
  <rect x="0" y="300" width="640" height="180" fill="#9aa3ab"/>
  <ellipse cx="320" cy="330" rx="150" ry="60" fill="#3a3f45"/>
  <ellipse cx="320" cy="322" rx="120" ry="44" fill="#1c1f23"/>
  <g stroke="#6b727a" stroke-width="4">
    <line x1="200" y1="322" x2="440" y2="322"/>
    <line x1="214" y1="306" x2="426" y2="338"/>
    <line x1="214" y1="338" x2="426" y2="306"/>
  </g>
  <rect x="24" y="22" width="230" height="62" rx="8" fill="rgba(0,0,0,0.55)"/>
  <text x="42" y="62" font-family="sans-serif" font-size="28" fill="#ffffff">{title}</text>
  <text x="42" y="120" font-family="sans-serif" font-size="22" fill="#ffffff">{subtitle}</text>
</svg>
"""
    path.write_text(svg, encoding="utf-8")


def _sample_photos(seed: int) -> dict[str, str]:
    before = config.SAMPLE_DIR / f"before_{seed}.svg"
    after = config.SAMPLE_DIR / f"after_{seed}.svg"
    reset = config.SAMPLE_DIR / f"reset_{seed}.svg"
    _write_sample_photo(before, "清掏前", "#6b5a45", "淤积明显 / 井盖周边淤泥")
    _write_sample_photo(after, "清掏后", "#2e6f8e", "井室见底 / 水流顺畅")
    _write_sample_photo(reset, "复位检查", "#3f6b4f", "井盖归位 / 无翘动")
    return {
        "before": str(before.relative_to(config.UPLOAD_DIR)),
        "after": str(after.relative_to(config.UPLOAD_DIR)),
        "reset": str(reset.relative_to(config.UPLOAD_DIR)),
    }


SEGMENTS = [
    # code, name, polygon (示意图坐标 0..1000), contact
    ("RM-01", "滨河路一段",
     [[60, 120], [330, 90], [360, 300], [300, 470], [70, 450]],
     "周文彬", "13800000101"),
    ("RM-02", "柳荫街片区",
     [[380, 110], [640, 90], [680, 320], [620, 470], [380, 450]],
     "林秀兰", "13800000202"),
    ("RM-03", "梧桐大道南段",
     [[690, 130], [950, 120], [960, 460], [700, 450], [670, 300]],
     "高建国", "13800000303"),
]

# code, segment_index, x, y, status, block_level
INLETS = [
    ("YS-0101", 0, 130, 200, "normal", 0),
    ("YS-0102", 0, 210, 250, "blocked", 3),       # 严重堵塞，已建单（样例：堵塞待派）
    ("YS-0103", 0, 260, 360, "blocked", 2),
    ("YS-0201", 1, 450, 200, "accepted", 0),      # 样例：已完成
    ("YS-0202", 1, 520, 260, "pending_accept", 2),# 样例：验收退回，继续沿用原工单
    ("YS-0203", 1, 590, 350, "normal", 0),
    ("YS-0204", 1, 440, 370, "blocked", 2),       # 未建单，供现场建单演示
    ("YS-0301", 2, 760, 220, "normal", 0),
    ("YS-0302", 2, 850, 300, "normal", 1),
    ("YS-0303", 2, 900, 400, "normal", 0),
]

CREWS = [
    ("班组甲（滨河）", "赵大勇", "13900001111"),
    ("班组乙（柳荫）", "孙志强", "13900002222"),
    ("班组丙（梧桐）", "钱卫东", "13900003333"),
]

VEHICLES = [
    ("京A·W301", "马师傅", "13700001001", 3000),
    ("京A·W302", "冯师傅", "13700002002", 2500),
    ("京A·W303", "韩师傅", "13700003003", 4000),
]

USERS = [
    ("inspect", "巡查员小陈", "inspector", None),
    ("dispatch", "调度员老吴", "dispatcher", None),
    ("super", "主管郑姐", "supervisor", None),
    ("accept", "验收员老何", "acceptor", None),
    ("crew1", "赵大勇", "crew", 1),
    ("crew2", "孙志强", "crew", 2),
]


def seed(reset: bool = False) -> None:
    """reset=True 时删除旧库重建，保证一条命令载入全新样例。"""
    if reset and config.DB_PATH.exists():
        config.DB_PATH.unlink()
    init_db()
    with connect() as conn:
        if conn.execute("SELECT COUNT(*) c FROM road_segments").fetchone()["c"]:
            return

        seg_ids = []
        for code, name, polygon, cname, cphone in SEGMENTS:
            cur = conn.execute(
                "INSERT INTO road_segments(code,name,polygon,contact_name,contact_phone)"
                " VALUES(?,?,?,?,?)",
                (code, name, json.dumps(polygon), cname, cphone),
            )
            seg_ids.append(cur.lastrowid)

        inlet_ids = {}
        for code, si, x, y, status, level in INLETS:
            cur = conn.execute(
                "INSERT INTO inlets(code,segment_id,x,y,status,block_level,updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (code, seg_ids[si], x, y, status, level, now()),
            )
            inlet_ids[code] = cur.lastrowid

        crew_ids = []
        for name, leader, phone in CREWS:
            cur = conn.execute(
                "INSERT INTO crews(name,leader,phone) VALUES(?,?,?)",
                (name, leader, phone),
            )
            crew_ids.append(cur.lastrowid)

        veh_ids = []
        for plate, driver, phone, cap in VEHICLES:
            cur = conn.execute(
                "INSERT INTO vehicles(plate,driver,phone,capacity_kg) VALUES(?,?,?,?)",
                (plate, driver, phone, cap),
            )
            veh_ids.append(cur.lastrowid)

        for uname, disp, role, crew_id in USERS:
            conn.execute(
                "INSERT INTO users(username,display_name,role,crew_id) VALUES(?,?,?,?)",
                (uname, disp, role, crew_id),
            )

        t0 = datetime.now().replace(microsecond=0)

        # ---- 样例 1：严重堵塞，已建单待派工 ----
        p1 = _sample_photos(1)
        conn.execute(
            "INSERT INTO work_orders(code,inlet_id,segment_id,block_level,issue_kind,"
            "issue_note,created_by,created_at,status,priority,guard_radius_m,guard_note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("WO-BLOCK-01", inlet_ids["YS-0102"], seg_ids[0], 3, "storm_urgent",
             "暴雨预警前巡查发现：井口淤积严重，排水明显不畅", "巡查员小陈",
             _iso(t0 - timedelta(hours=5)), "open", 1, 12.0,
             "井周 12 米设锥桶与警戒带，雨水蓝围挡"),
        )
        # 该单只有“清掏前”照片
        oid1 = conn.execute("SELECT id FROM work_orders WHERE code='WO-BLOCK-01'").fetchone()[0]
        conn.execute(
            "INSERT INTO photos(order_id,dispatch_id,phase,kind,path,uploaded_by,uploaded_at)"
            " VALUES(?,?, 'before','clean', ?,?,?)",
            (oid1, None, p1["before"], "巡查员小陈", _iso(t0 - timedelta(hours=5))),
        )
        conn.execute(
            "INSERT INTO feedbacks(inlet_id,order_id,reporter,contact,content,category,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (inlet_ids["YS-0102"], oid1, "居民刘阿姨", "13500007788",
             "一下雨这片就积水，味大，麻烦尽快疏通", "积水反味", _iso(t0 - timedelta(hours=6))),
        )

        # 相邻的另一处中度堵塞（同区段、距 YS-0102 约 121m），可直接合并派车；
        # 更远或跨区段的点位由后端按 150m 边界拦截。
        p4 = _sample_photos(4)
        conn.execute(
            "INSERT INTO work_orders(code,inlet_id,segment_id,block_level,issue_kind,"
            "issue_note,created_by,created_at,status,priority,guard_radius_m,guard_note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("WO-BLOCK-02", inlet_ids["YS-0103"], seg_ids[0], 2, None,
             "片区巡查：半边井箅被淤泥覆盖", "巡查员小陈",
             _iso(t0 - timedelta(hours=3)), "open", 0, 8.0, "井周 8 米警戒"),
        )
        oid4 = conn.execute("SELECT id FROM work_orders WHERE code='WO-BLOCK-02'").fetchone()[0]
        conn.execute(
            "INSERT INTO photos(order_id,dispatch_id,phase,kind,path,uploaded_by,uploaded_at)"
            " VALUES(?,?,'before','clean',?,?,?)",
            (oid4, None, p4["before"], "巡查员小陈", _iso(t0 - timedelta(hours=3))),
        )

        # ---- 样例 2：验收退回，沿用原工单继续处理 ----
        p2 = _sample_photos(2)
        conn.execute(
            "INSERT INTO work_orders(code,inlet_id,segment_id,block_level,issue_kind,"
            "issue_note,created_by,created_at,status,priority,guard_radius_m,guard_note,"
            "reject_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("WO-REJECT-01", inlet_ids["YS-0202"], seg_ids[1], 2, "silt_spill",
             "清掏后少量淤泥遗撒在路缘", "巡查员小陈",
             _iso(t0 - timedelta(days=1, hours=4)), "rejected", 0, 10.0,
             "井周 10 米警戒", "复位照片显示井缘有残泥，冲洗不彻底，请重新冲洗并清理路缘"),
        )
        oid2 = conn.execute("SELECT id FROM work_orders WHERE code='WO-REJECT-01'").fetchone()[0]
        conn.execute(
            "INSERT INTO dispatches(code,order_id,crew_id,vehicle_id,start_at,end_at,"
            "status,merged_group_id,created_at) VALUES(?,?,?,?,?,?,'completed',NULL,?)",
            ("DSP-REJ-01", oid2, crew_ids[1], veh_ids[1],
             _iso(t0 - timedelta(days=1, hours=2)), _iso(t0 - timedelta(days=1)),
             _iso(t0 - timedelta(days=1, hours=3))),
        )
        did2 = conn.execute("SELECT id FROM dispatches WHERE code='DSP-REJ-01'").fetchone()[0]
        for i, step in enumerate(config.STEP_ORDER):
            conn.execute(
                "INSERT INTO work_steps(dispatch_id,step,done_at,operator,note)"
                " VALUES(?,?,?,?,?)",
                (did2, step, _iso(t0 - timedelta(days=1) - timedelta(minutes=40 - i * 10)),
                 "孙志强", None),
            )
        conn.execute(
            "INSERT INTO clean_outputs(dispatch_id,bag_count,measured_kg,weighed_at,created_at)"
            " VALUES(?,?,?,?,?)",
            (did2, 6, 168.5, _iso(t0 - timedelta(days=1)), _iso(t0 - timedelta(days=1))),
        )
        conn.execute(
            "INSERT INTO photos(order_id,dispatch_id,phase,kind,path,uploaded_by,uploaded_at)"
            " VALUES(?,?, 'before','clean', ?,?,?)",
            (oid2, did2, p2["before"], "孙志强", _iso(t0 - timedelta(days=1, hours=2))),
        )
        conn.execute(
            "INSERT INTO photos(order_id,dispatch_id,phase,kind,path,uploaded_by,uploaded_at)"
            " VALUES(?,?, 'after','clean', ?,?,?)",
            (oid2, did2, p2["after"], "孙志强", _iso(t0 - timedelta(days=1, minutes=20))),
        )
        conn.execute(
            "INSERT INTO photos(order_id,dispatch_id,phase,kind,path,uploaded_by,uploaded_at)"
            " VALUES(?,?, 'after','reset', ?,?,?)",
            (oid2, did2, p2["reset"], "孙志强", _iso(t0 - timedelta(days=1, minutes=10))),
        )
        conn.execute(
            "INSERT INTO acceptances(order_id,dispatch_id,result,reviewer,comment,"
            "created_at,decided_at) VALUES(?,?, 'reject', ?,?,?,?)",
            (oid2, did2, "验收员老何", "复位照片显示井缘有残泥，冲洗不彻底",
             _iso(t0 - timedelta(hours=20)), _iso(t0 - timedelta(hours=19))),
        )

        # ---- 样例 3：已完成并验收通过 ----
        p3 = _sample_photos(3)
        conn.execute(
            "INSERT INTO work_orders(code,inlet_id,segment_id,block_level,issue_kind,"
            "issue_note,created_by,created_at,status,priority,guard_radius_m,guard_note,"
            "accepted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("WO-DONE-01", inlet_ids["YS-0201"], seg_ids[1], 1, None,
             "例行巡查发现轻度淤积", "巡查员小陈",
             _iso(t0 - timedelta(days=3)), "accepted", 0, None, None,
             _iso(t0 - timedelta(days=2, hours=23))),
        )
        oid3 = conn.execute("SELECT id FROM work_orders WHERE code='WO-DONE-01'").fetchone()[0]
        conn.execute(
            "INSERT INTO dispatches(code,order_id,crew_id,vehicle_id,start_at,end_at,"
            "status,merged_group_id,created_at) VALUES(?,?,?,?,?,?,'completed',NULL,?)",
            ("DSP-DONE-01", oid3, crew_ids[1], veh_ids[0],
             _iso(t0 - timedelta(days=3)), _iso(t0 - timedelta(days=2, hours=23)),
             _iso(t0 - timedelta(days=3))),
        )
        did3 = conn.execute("SELECT id FROM dispatches WHERE code='DSP-DONE-01'").fetchone()[0]
        for i, step in enumerate(config.STEP_ORDER):
            conn.execute(
                "INSERT INTO work_steps(dispatch_id,step,done_at,operator,note)"
                " VALUES(?,?,?,?,?)",
                (did3, step, _iso(t0 - timedelta(days=3) - timedelta(minutes=45 - i * 12)),
                 "孙志强", None),
            )
        conn.execute(
            "INSERT INTO clean_outputs(dispatch_id,bag_count,measured_kg,revised_kg,"
            "revise_reason,revised_by,revised_at,weighed_at,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (did3, 4, 132.0, 126.5, "过磅含空袋与雨水，核减 5.5kg",
             "主管郑姐", _iso(t0 - timedelta(days=3)),
             _iso(t0 - timedelta(days=3)), _iso(t0 - timedelta(days=3))),
        )
        conn.execute(
            "INSERT INTO weight_revisions(dispatch_id,old_kg,new_kg,reason,operator,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (did3, 132.0, 126.5, "过磅含空袋与雨水，核减 5.5kg",
             "主管郑姐", _iso(t0 - timedelta(days=3))),
        )
        for phase, kind, photo, mins in [
            ("before", "clean", p3["before"], 130),
            ("after", "clean", p3["after"], 30),
            ("after", "reset", p3["reset"], 20),
        ]:
            conn.execute(
                "INSERT INTO photos(order_id,dispatch_id,phase,kind,path,uploaded_by,uploaded_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (oid3, did3, phase, kind, photo, "孙志强",
                 _iso(t0 - timedelta(days=3) - timedelta(minutes=mins))),
            )
        conn.execute(
            "INSERT INTO acceptances(order_id,dispatch_id,result,reviewer,comment,"
            "created_at,decided_at) VALUES(?,?, 'pass', ?,?,?,?)",
            (oid3, did3, "验收员老何", "井室畅通，复位牢固",
             _iso(t0 - timedelta(days=3)), _iso(t0 - timedelta(days=2, hours=23))),
        )
        conn.execute(
            "INSERT INTO vehicle_handovers(dispatch_id,vehicle_id,planned_arrival,arrived_at,"
            "status,bags_loaded,net_kg,note,updated_by,updated_at)"
            " VALUES(?,?,?,?, 'handed', ?,?,?,?,?)",
            (did3, veh_ids[0], _iso(t0 - timedelta(days=3)),
             _iso(t0 - timedelta(days=3)), 4, 126.5, "联单已签字", "调度员老吴",
             _iso(t0 - timedelta(days=3))),
        )
        conn.execute(
            "INSERT INTO feedbacks(inlet_id,order_id,reporter,contact,content,category,"
            "created_at,handled,handle_note) VALUES(?,?,?,?,?,?,?,1,?)",
            (inlet_ids["YS-0201"], oid3, "居民王大爷", "13600006677",
             "疏通后下雨不积水了，点赞", "表扬", _iso(t0 - timedelta(days=2, hours=20)),
             "已电话回访致谢"),
        )

        # 一条待触发的暴雨前复查提醒
        conn.execute(
            "INSERT INTO reminders(inlet_id,order_id,due_at,message,created_at)"
            " VALUES(?,?,?,?,?)",
            (inlet_ids["YS-0102"], oid1,
             _iso(t0 + timedelta(seconds=config.STORM_RECHECK_SECONDS)),
             "暴雨前复查：YS-0102 须在降雨前再次开盖核查", now()),
        )


if __name__ == "__main__":
    seed(reset="--reset" in __import__("sys").argv)
    print("样例数据已载入", config.DB_PATH)
