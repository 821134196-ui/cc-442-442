"""本地样例数据：片区/道路/雨水口/联系人/班组/车辆，
以及堵塞、退回、已完成三类工单样例，含生成的 SVG 前后照片。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .db import DB


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _today(h: int, m: int = 0) -> str:
    return datetime.now().replace(hour=h, minute=m, second=0, microsecond=0).isoformat()


def make_photo_svg(code: str, phase: str) -> bytes:
    """生成 4:3 的样例井口照片：before 淤堵发黑，after 通畅见雨水蓝。"""
    if phase == "before":
        water, water2, sludge, label, tag, tagfill = "#5d4a2f", "#4a3a24", "#2f2618", "淤堵", "清掏前", "#e8833a"
        debris = True
    else:
        water, water2, sludge, label, tag, tagfill = "#7cc3f2", "#3d8fd1", "#1f7ae0", "通畅", "清掏后", "#1f7ae0"
        debris = False
    parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600" viewBox="0 0 800 600">
  <rect width="800" height="600" fill="#d9dde1"/>
  <rect x="0" y="0" width="800" height="150" fill="#c7ccd2"/>
  <text x="24" y="48" font-family="sans-serif" font-size="26" fill="#5a6470">{code}</text>
  <rect x="650" y="24" width="126" height="44" rx="6" fill="{tagfill}"/>
  <text x="713" y="54" font-family="sans-serif" font-size="24" fill="#fff" text-anchor="middle">{tag}</text>
  <ellipse cx="400" cy="430" rx="300" ry="140" fill="#8f969f"/>
  <ellipse cx="400" cy="420" rx="246" ry="104" fill="#6b727b"/>
  <ellipse cx="400" cy="418" rx="208" ry="80" fill="{water}"/>
  <ellipse cx="400" cy="412" rx="208" ry="60" fill="{water2}"/>''']
    # 井盖格栅
    for i in range(-9, 10):
        x = 400 + i * 22
        parts.append(
            f'<line x1="{x}" y1="346" x2="{x - 18}" y2="486" stroke="#454b53" stroke-width="7"/>')
    if debris:
        for i, (cx, cy, r) in enumerate([
            (300, 400, 26), (360, 425, 18), (450, 398, 30), (500, 430, 20),
            (330, 445, 15), (430, 445, 14), (270, 430, 12)]):
            parts.append(f'<ellipse cx="{cx}" cy="{cy}" rx="{r}" ry="{r * 0.7}" fill="{sludge}"/>')
        parts.append('<ellipse cx="400" cy="430" rx="120" ry="26" fill="#3a2e1d" opacity="0.7"/>')
    else:
        parts.append('<ellipse cx="400" cy="440" rx="150" ry="16" fill="#a9d6f7" opacity="0.8"/>')
    parts.append(f'''<text x="400" y="568" font-family="sans-serif" font-size="28" fill="#3a4149" text-anchor="middle">{label} · 样例照片</text>
</svg>''')
    return "\n".join(parts).encode()


SECTOR = {"name": "清和园社区", "map_w": 900, "map_h": 620}

ROADS = [
    ("滨河路", [[40, 180], [300, 170], [560, 185], [860, 175]]),
    ("海棠大道", [[260, 40], [270, 260], [255, 420], [270, 590]]),
    ("柳岸街", [[300, 420], [520, 415], [860, 430]]),
    ("梧桐巷", [[560, 60], [575, 200], [560, 300]]),
    ("青石桥路", [[100, 500], [200, 455], [300, 420]]),
]

# code, road_index, x, y, status, level, cover_ok
INLETS = [
    ("YS-0101", 0, 120, 180, "normal", 0, 1),
    ("YS-0102", 0, 260, 175, "normal", 0, 1),
    ("YS-0103", 0, 420, 180, "blocked", 2, 1),
    ("YS-0104", 0, 570, 185, "cleaning", 2, 1),
    ("YS-0105", 0, 740, 180, "normal", 0, 1),
    ("YS-0201", 1, 268, 100, "normal", 0, 1),
    ("YS-0202", 1, 262, 330, "cleaning", 3, 1),
    ("YS-0203", 1, 265, 520, "normal", 0, 1),
    ("YS-0301", 2, 380, 418, "normal", 0, 1),
    ("YS-0302", 2, 560, 415, "normal", 0, 1),
    ("YS-0303", 2, 740, 425, "normal", 0, 1),
    ("YS-0401", 3, 565, 120, "normal", 0, 1),
    ("YS-0402", 3, 562, 250, "normal", 0, 1),
    ("YS-0501", 4, 180, 470, "normal", 0, 1),
]

USERS = [
    ("王巡", "patrol"), ("调调度", "dispatcher"), ("李班", "crew"),
    ("张验", "inspector"), ("赵管", "supervisor"),
]

CREWS = [
    ("甲班（清掏一组）", "李建国", "13800000101", "#1f7ae0"),
    ("乙班（清掏二组）", "周大勇", "13800000202", "#0e8a6f"),
]

VEHICLES = [
    ("苏A·W001", "马师傅", 2000),
    ("苏A·W002", "吴师傅", 2500),
    ("苏A·W003", "郑师傅", 1800),
]

CONTACTS = [
    ("刘芳", "13900000111", "滨河路12号利民便利店", "YS-0103"),
    ("陈桂兰", "13900000222", "海棠大道88号3单元", "YS-0202"),
    ("何军", "13900000333", "柳岸街5号修车铺", "YS-0301"),
    ("物业服务中心", "13900000000", "清和园社区物业楼", None),
]


def seed(db_path: str | Path, upload_dir: str | Path, reset: bool = True) -> None:
    db_path = Path(db_path)
    upload_dir = Path(upload_dir)
    if reset and db_path.exists():
        db_path.unlink()
        for suffix in ("-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    (upload_dir / "photos").mkdir(parents=True, exist_ok=True)

    db = DB(db_path)
    db.init()
    conn = db.get()
    conn.execute("BEGIN IMMEDIATE")

    sector_id = db.insert("sectors", **{"name": SECTOR["name"],
                                        "map_w": SECTOR["map_w"], "map_h": SECTOR["map_h"]})
    road_ids = []
    for name, pts in ROADS:
        road_ids.append(db.insert("road_segments", sector_id=sector_id, name=name,
                                  points=json.dumps(pts)))
    inlet_ids: dict[str, int] = {}
    for code, ri, x, y, status, level, cover_ok in INLETS:
        iid = db.insert("inlets", code=code, sector_id=sector_id,
                        segment_id=road_ids[ri], x=x, y=y, status=status,
                        block_level=level, cover_ok=cover_ok,
                        last_cleaned_at=_today(9) if status == "normal" else None)
        inlet_ids[code] = iid

    for name, role in USERS:
        db.insert("users", name=name, role=role)
    crew_ids = []
    for name, leader, phone, color in CREWS:
        crew_ids.append(db.insert("crews", name=name, leader=leader, phone=phone, color=color))
    vehicle_ids = []
    for plate, driver, cap in VEHICLES:
        vehicle_ids.append(db.insert("vehicles", plate=plate, driver=driver, capacity_kg=cap))
    for name, phone, addr, inlet_code in CONTACTS:
        db.insert("contacts", name=name, phone=phone, address=addr,
                  inlet_id=inlet_ids.get(inlet_code) if inlet_code else None)

    def add_photo(order_id: int, iid: int, code: str, phase: str, by: str, ts: str) -> None:
        rel = Path("photos") / f"o{order_id}_i{iid}_{phase}.svg"
        (upload_dir / rel).write_bytes(make_photo_svg(code, phase))
        db.insert("photos", order_id=order_id, inlet_id=iid, phase=phase,
                  file_path=str(rel), uploaded_by=by, created_at=ts)

    # ---- 工单1：巡查发现堵塞，待派工 ----
    t = _now()
    o1 = db.insert("work_orders", order_no="WO-0001", type="normal", source="resident",
                   status="pending", block_level=2,
                   description="居民反映滨河路12号门前雨水口排水不畅，落叶淤积",
                   reporter="王巡", contact_id=1, alert_radius=20,
                   alert_note="人行道侧摆放锥桶，提醒绕行", urgent=0, created_at=t)
    db.insert("order_inlets", order_id=o1, inlet_id=inlet_ids["YS-0103"])

    # ---- 工单2：验收退回，沿用原工单继续整改（在原单上补工序） ----
    ds2, de2 = _today(8), _today(10)
    o2 = db.insert("work_orders", order_no="WO-0002", type="normal", source="patrol",
                   status="rejected", block_level=3,
                   description="海棠大道88号门前雨水口严重堵塞，雨天积水",
                   reporter="王巡", crew_id=crew_ids[0], vehicle_id=vehicle_ids[0],
                   planned_start=ds2, planned_end=de2, alert_radius=25,
                   reject_reason="冲洗不彻底，井壁挂淤明显；复位后井盖有轻微翘动",
                   created_at=_today(7, 30), dispatched_at=ds2)
    i2 = inlet_ids["YS-0202"]
    db.insert("order_inlets", order_id=o2, inlet_id=i2, bag_count=4, reset_checked=1,
              submit_status="rejected", reject_reason="冲洗不彻底，井壁挂淤明显",
              submitted_at=_today(9, 20))
    for step, ts in [("open", _today(8, 20)), ("clean", _today(8, 50)),
                     ("flush", _today(9, 10)), ("reset", _today(9, 15))]:
        db.insert("work_steps", order_id=o2, inlet_id=i2, step=step,
                  operator="李班", recorded_at=ts,
                  note="井盖复位" if step == "reset" else "")
    add_photo(o2, i2, "YS-0202", "before", "李班", _today(8, 25))
    add_photo(o2, i2, "YS-0202", "after", "李班", _today(9, 12))
    db.insert("acceptance_records", order_id=o2, inlet_id=i2, inspector="张验",
              result="rejected", comment="冲洗不彻底，井壁挂淤明显；复位后井盖有轻微翘动",
              created_at=_today(9, 40))
    db.insert("haul_trips", order_id=o2, vehicle_id=vehicle_ids[0], planned_at=ds2,
              status="noshow", note="首派车辆故障未到场", created_at=ds2)
    db.insert("haul_trips", order_id=o2, vehicle_id=vehicle_ids[1], planned_at=ds2,
              status="dispatched", note="车辆未到后重新派车", created_at=_today(8, 10))
    db.insert("incidents", kind="vehicle_missing", inlet_id=i2, order_id=o2,
              description="苏A·W001 未按计划到场，已改派 W002", handled_by="调调度",
              status="handled", created_at=_today(8, 5), handled_at=_today(8, 10))

    # ---- 工单3：已完成并验收通过 ----
    ds3, de3 = _today(6), _today(8)
    o3 = db.insert("work_orders", order_no="WO-0003", type="normal", source="patrol",
                   status="accepted", block_level=2, description="柳岸街雨水口例行清掏",
                   reporter="王巡", crew_id=crew_ids[1], vehicle_id=vehicle_ids[1],
                   planned_start=ds3, planned_end=de3, created_at=_today(5),
                   dispatched_at=ds3, accepted_at=_today(8, 5))
    i3 = inlet_ids["YS-0301"]
    db.insert("order_inlets", order_id=o3, inlet_id=i3, bag_count=6, reset_checked=1,
              submit_status="accepted", submitted_at=_today(7, 30), accepted_at=_today(8, 5))
    for step, h, m in [("open", 6, 20), ("clean", 6, 50), ("flush", 7, 10), ("reset", 7, 20)]:
        db.insert("work_steps", order_id=o3, inlet_id=i3, step=step,
                  operator="周大勇", recorded_at=_today(h, m),
                  note="井盖复位，间隙均匀" if step == "reset" else "")
    add_photo(o3, i3, "YS-0301", "before", "周大勇", _today(6, 25))
    add_photo(o3, i3, "YS-0301", "after", "周大勇", _today(7, 15))
    db.insert("acceptance_records", order_id=o3, inlet_id=i3, inspector="张验",
              result="passed", comment="清掏彻底，复位平整，验收通过", created_at=_today(8, 5))
    db.insert("haul_trips", order_id=o3, vehicle_id=vehicle_ids[1], planned_at=ds3,
              status="completed", bag_count=6, declared_weight_kg=182.0,
              measured_weight_kg=180.6, handover_crew="周大勇", handover_driver="吴师傅",
              created_at=ds3, arrived_at=_today(6, 40), weighed_at=_today(7, 40),
              completed_at=_today(8, 0))

    # ---- 工单4：作业中（开盖、清掏已完成，仅前照片）----
    ds4, de4 = _today(13), _today(15)
    o4 = db.insert("work_orders", order_no="WO-0004", type="normal", source="patrol",
                   status="working", block_level=2, description="滨河路雨水口落叶堵塞",
                   reporter="王巡", crew_id=crew_ids[0], vehicle_id=vehicle_ids[2],
                   planned_start=ds4, planned_end=de4, alert_radius=15,
                   created_at=_today(12), dispatched_at=ds4)
    i4 = inlet_ids["YS-0104"]
    db.insert("order_inlets", order_id=o4, inlet_id=i4)
    db.insert("work_steps", order_id=o4, inlet_id=i4, step="open", operator="李班",
              recorded_at=_today(13, 10))
    db.insert("work_steps", order_id=o4, inlet_id=i4, step="clean", operator="李班",
              recorded_at=_today(13, 45))
    add_photo(o4, i4, "YS-0104", "before", "李班", _today(13, 15))
    db.insert("haul_trips", order_id=o4, vehicle_id=vehicle_ids[2], planned_at=ds4,
              status="arrived", created_at=ds4, arrived_at=_today(13, 5))

    # ---- 工单5：暴雨前加急（待派工），生成复查提醒 ----
    storm = (datetime.now() + timedelta(hours=6)).replace(microsecond=0).isoformat()
    fire = (datetime.now() + timedelta(hours=4)).replace(microsecond=0).isoformat()
    o5 = db.insert("work_orders", order_no="UR-0005", type="rush", source="patrol",
                   status="pending", block_level=3, description="气象台发布暴雨橙色预警，加急复查清掏",
                   reporter="王巡", urgent=1, storm_at=storm,
                   created_at=_now(), alert_radius=30, alert_note="暴雨前重点点位")
    db.insert("order_inlets", order_id=o5, inlet_id=inlet_ids["YS-0501"])
    db.insert("reminders", order_id=o5, type="storm_review",
              message="暴雨前复查：工单 UR-0005 须在降雨前完成清掏复位，请确认现场状态",
              fire_at=fire, created_at=_now())

    # ---- 居民反馈样例 ----
    db.insert("residents_feedback", contact_id=1, inlet_id=inlet_ids["YS-0103"],
              content="一下雨店门口就积水，麻烦尽快疏通", status="linked", order_id=o1,
              created_at=_today(11, 20))
    db.insert("residents_feedback", contact_id=3, inlet_id=inlet_ids["YS-0302"],
              content="前几天清掏后路面有点淤泥印迹，希望冲洗干净", status="open",
              created_at=_today(10, 5))

    conn.execute("COMMIT")
    return db


if __name__ == "__main__":
    import sys
    db = seed(sys.argv[1] if len(sys.argv) > 1 else "data/platform.db", "uploads")
    print("样例数据已载入")
