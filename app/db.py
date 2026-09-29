"""SQLite 连接、建表与基础封装。"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sectors (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    -- 本地样例地图尺寸（像素即示意米）
    map_w INTEGER NOT NULL DEFAULT 900,
    map_h INTEGER NOT NULL DEFAULT 620
);

CREATE TABLE IF NOT EXISTS road_segments (
    id INTEGER PRIMARY KEY,
    sector_id INTEGER NOT NULL REFERENCES sectors(id),
    name TEXT NOT NULL,
    -- 折线点 JSON: [[x,y],...]
    points TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inlets (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,           -- 雨水口编号 YS-0101
    sector_id INTEGER NOT NULL REFERENCES sectors(id),
    segment_id INTEGER REFERENCES road_segments(id),
    x REAL NOT NULL,
    y REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'normal',   -- normal 正常 / blocked 堵塞 / cleaning 作业中
    block_level INTEGER NOT NULL DEFAULT 0,  -- 0 畅通 1 轻微 2 中度 3 严重
    cover_ok INTEGER NOT NULL DEFAULT 1,     -- 井盖是否完好
    last_cleaned_at TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    address TEXT,
    inlet_id INTEGER REFERENCES inlets(id)
);

CREATE TABLE IF NOT EXISTS residents_feedback (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id),
    inlet_id INTEGER REFERENCES inlets(id),
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',     -- open / linked / replied
    reply TEXT,
    order_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('patrol','dispatcher','crew','inspector','supervisor'))
    -- 巡查员 / 调度员 / 班组 / 验收员 / 主管
);

CREATE TABLE IF NOT EXISTS crews (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    leader TEXT NOT NULL,
    phone TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '#1f7ae0'
);

CREATE TABLE IF NOT EXISTS vehicles (
    id INTEGER PRIMARY KEY,
    plate TEXT NOT NULL,
    driver TEXT NOT NULL,
    capacity_kg INTEGER NOT NULL DEFAULT 2000,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS work_orders (
    id INTEGER PRIMARY KEY,
    order_no TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT 'normal',
    -- normal 常规 / rush 暴雨加急 / cover_damage 井盖破损
    -- / backflow 积水反涌 / spill 淤泥遗撒
    source TEXT NOT NULL DEFAULT 'patrol',   -- patrol / resident
    status TEXT NOT NULL DEFAULT 'pending',
    -- pending 待派工 / dispatched 已派工 / working 作业中
    -- / submitted 待验收 / accepted 已通过 / rejected 待整改 / cancelled 已取消
    block_level INTEGER NOT NULL DEFAULT 1,
    description TEXT,
    reporter TEXT,
    contact_id INTEGER REFERENCES contacts(id),
    crew_id INTEGER REFERENCES crews(id),
    vehicle_id INTEGER REFERENCES vehicles(id),
    planned_start TEXT,
    planned_end TEXT,
    alert_radius REAL NOT NULL DEFAULT 0,    -- 警戒范围（示意米）
    alert_note TEXT,
    urgent INTEGER NOT NULL DEFAULT 0,
    merged_from TEXT,                        -- 合单来源单号 JSON
    reject_reason TEXT,
    created_at TEXT NOT NULL,
    dispatched_at TEXT,
    accepted_at TEXT,
    storm_at TEXT                            -- 暴雨预计时间（加急单）
);

-- 工单-雨水口（一对多即合单；验收按行独立进行）
CREATE TABLE IF NOT EXISTS order_inlets (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES work_orders(id),
    inlet_id INTEGER NOT NULL REFERENCES inlets(id),
    bag_count INTEGER,
    reset_checked INTEGER NOT NULL DEFAULT 0,
    reset_note TEXT,
    submit_status TEXT,                      -- submitted / accepted / rejected
    submitted_at TEXT,
    accepted_at TEXT,
    reject_reason TEXT,
    UNIQUE(order_id, inlet_id)
);

CREATE TABLE IF NOT EXISTS work_steps (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES work_orders(id),
    inlet_id INTEGER NOT NULL REFERENCES inlets(id),
    step TEXT NOT NULL CHECK(step IN ('open','clean','flush','reset')),
    operator TEXT NOT NULL,
    note TEXT,
    recorded_at TEXT NOT NULL,
    UNIQUE(order_id, inlet_id, step)
);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES work_orders(id),
    inlet_id INTEGER NOT NULL REFERENCES inlets(id),
    phase TEXT NOT NULL CHECK(phase IN ('before','after')),
    file_path TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(order_id, inlet_id, phase)
);

CREATE TABLE IF NOT EXISTS acceptance_records (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES work_orders(id),
    inlet_id INTEGER NOT NULL REFERENCES inlets(id),
    inspector TEXT NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('passed','rejected')),
    comment TEXT,
    created_at TEXT NOT NULL
);

-- 清运车次/车辆交接
CREATE TABLE IF NOT EXISTS haul_trips (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES work_orders(id),
    vehicle_id INTEGER REFERENCES vehicles(id),
    planned_at TEXT,
    status TEXT NOT NULL DEFAULT 'dispatched',
    -- dispatched 已派车 / arrived 已到场 / weighed 已过磅 / completed 已完成 / noshow 车辆未到
    bag_count INTEGER,
    declared_weight_kg REAL,                 -- 班组自报重量
    measured_weight_kg REAL,                 -- 过磅适配器返回
    revised_weight_kg REAL,                  -- 主管修订重量
    revision_reason TEXT,
    revised_by TEXT,
    revised_at TEXT,
    handover_crew TEXT,
    handover_driver TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    arrived_at TEXT,
    weighed_at TEXT,
    completed_at TEXT
);

-- 应急事件（井盖破损/积水反涌/淤泥遗撒/车辆未到/照片缺失的处置留痕）
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    -- cover_damage / backflow / spill / vehicle_missing / photo_missing
    inlet_id INTEGER REFERENCES inlets(id),
    order_id INTEGER REFERENCES work_orders(id),
    trip_id INTEGER REFERENCES haul_trips(id),
    description TEXT,
    handled_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',     -- open / handled
    created_at TEXT NOT NULL,
    handled_at TEXT
);

-- 进程内提醒（暴雨前复查等）
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    order_id INTEGER REFERENCES work_orders(id),
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    fire_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / fired
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON work_orders(status);
CREATE INDEX IF NOT EXISTS idx_order_inlets_order ON order_inlets(order_id);
CREATE INDEX IF NOT EXISTS idx_steps_order ON work_steps(order_id);
CREATE INDEX IF NOT EXISTS idx_photos_order ON photos(order_id);
CREATE INDEX IF NOT EXISTS idx_trips_order ON haul_trips(order_id);
CREATE INDEX IF NOT EXISTS idx_reminders_fire ON reminders(status, fire_at);
"""


class DB:
    """线程内复用连接；写操作串行化，保证派工冲突检查可靠。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        self.write_lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def get(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self.connect()
            self._local.conn = conn
        return conn

    def init(self) -> None:
        conn = self.get()
        conn.executescript(SCHEMA)

    def query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.get().execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self.get().execute(sql, args).fetchone()

    def insert(self, table: str, **fields) -> int:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.get().execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(fields.values())
        )
        return cur.lastrowid

    def update(self, table: str, where: str, where_args: tuple, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.get().execute(
            f"UPDATE {table} SET {sets} WHERE {where}",
            tuple(fields.values()) + where_args,
        )


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for key, val in list(d.items()):
        if isinstance(val, str) and key in ("points", "merged_from") and val.startswith("["):
            try:
                d[key] = json.loads(val)
            except json.JSONDecodeError:
                pass
    return d
