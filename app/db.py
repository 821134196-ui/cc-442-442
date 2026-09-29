"""SQLite schema 与连接管理。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS road_segments (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    polygon TEXT NOT NULL,          -- JSON [[x,y],...] 片区多边形（示意图坐标）
    contact_name TEXT,
    contact_phone TEXT
);

CREATE TABLE IF NOT EXISTS inlets (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,      -- 雨水口编号，如 YS-0102
    segment_id INTEGER NOT NULL REFERENCES road_segments(id),
    x REAL NOT NULL,
    y REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'normal',
        -- normal 正常 / blocked 堵塞 / cleaning 清掏中 / pending_accept 待验收 / accepted 已验收
    block_level INTEGER NOT NULL DEFAULT 0,   -- 0 无, 1 轻度, 2 中度, 3 严重
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crews (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    leader TEXT NOT NULL,
    phone TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vehicles (
    id INTEGER PRIMARY KEY,
    plate TEXT NOT NULL UNIQUE,
    driver TEXT NOT NULL,
    phone TEXT NOT NULL,
    capacity_kg INTEGER NOT NULL
);

-- 巡查发现 / 建单
CREATE TABLE IF NOT EXISTS work_orders (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    inlet_id INTEGER NOT NULL REFERENCES inlets(id),
    segment_id INTEGER NOT NULL REFERENCES road_segments(id),
    block_level INTEGER NOT NULL,
    issue_kind TEXT,                -- 六类专项入口，可空（常规巡查）
    issue_note TEXT,
    created_by TEXT NOT NULL,       -- 巡查员
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,          -- open/dispatched/working/ready/pending_accept/accepted/rejected/done
    priority INTEGER NOT NULL DEFAULT 1,  -- 0 普通 1 加急
    guard_radius_m REAL,           -- 警戒范围（米）
    guard_note TEXT,
    parent_order_id INTEGER REFERENCES work_orders(id), -- 合单主单
    reject_reason TEXT,
    accepted_at TEXT
);

-- 合单分组：一次“相邻点位合并派车”产生一个分组
CREATE TABLE IF NOT EXISTS merge_groups (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    dispatch_id INTEGER,           -- 关联派工
    note TEXT
);

CREATE TABLE IF NOT EXISTS merge_group_items (
    group_id INTEGER NOT NULL REFERENCES merge_groups(id) ON DELETE CASCADE,
    order_id INTEGER NOT NULL REFERENCES work_orders(id),
    PRIMARY KEY (group_id, order_id)
);

-- 派工单 / 班组日程。一个工单可因退回产生多条派工（沿用原工单）。
CREATE TABLE IF NOT EXISTS dispatches (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    order_id INTEGER NOT NULL REFERENCES work_orders(id),
    crew_id INTEGER NOT NULL REFERENCES crews(id),
    vehicle_id INTEGER REFERENCES vehicles(id),
    start_at TEXT NOT NULL,         -- ISO 本地时间
    end_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',
        -- scheduled/arrived/vehicle_missing/completed/cancelled
    merged_group_id INTEGER REFERENCES merge_groups(id),
    created_at TEXT NOT NULL
);

-- 现场工序记录：开盖/清掏/冲洗/复位
CREATE TABLE IF NOT EXISTS work_steps (
    id INTEGER PRIMARY KEY,
    dispatch_id INTEGER NOT NULL REFERENCES dispatches(id) ON DELETE CASCADE,
    step TEXT NOT NULL,             -- open/clean/flush/reset
    done_at TEXT NOT NULL,
    operator TEXT NOT NULL,
    note TEXT,
    UNIQUE (dispatch_id, step)
);

-- 清掏产出：淤泥袋数 / 重量
CREATE TABLE IF NOT EXISTS clean_outputs (
    id INTEGER PRIMARY KEY,
    dispatch_id INTEGER NOT NULL UNIQUE REFERENCES dispatches(id) ON DELETE CASCADE,
    bag_count INTEGER NOT NULL CHECK (bag_count >= 0),
    measured_kg REAL,               -- 车辆过磅重量（模拟适配器）
    revised_kg REAL,                -- 主管修订后重量
    revise_reason TEXT,
    revised_by TEXT,
    revised_at TEXT,
    weighed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
    dispatch_id INTEGER REFERENCES dispatches(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,            -- before / after
    kind TEXT NOT NULL DEFAULT 'clean', -- clean 清掏 / reset 复位检查
    path TEXT NOT NULL,             -- uploads 相对路径
    uploaded_by TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);

-- 车辆交接
CREATE TABLE IF NOT EXISTS vehicle_handovers (
    id INTEGER PRIMARY KEY,
    dispatch_id INTEGER NOT NULL UNIQUE REFERENCES dispatches(id) ON DELETE CASCADE,
    vehicle_id INTEGER REFERENCES vehicles(id),
    planned_arrival TEXT NOT NULL,
    arrived_at TEXT,
    status TEXT NOT NULL DEFAULT 'waiting', -- waiting/arrived/missing/replaced/handed
    bags_loaded INTEGER,
    net_kg REAL,
    note TEXT,
    updated_by TEXT,
    updated_at TEXT
);

-- 验收
CREATE TABLE IF NOT EXISTS acceptances (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES work_orders(id),
    dispatch_id INTEGER NOT NULL REFERENCES dispatches(id),
    result TEXT NOT NULL,           -- pending/pass/reject
    reviewer TEXT NOT NULL,
    comment TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);

-- 居民反馈
CREATE TABLE IF NOT EXISTS feedbacks (
    id INTEGER PRIMARY KEY,
    inlet_id INTEGER REFERENCES inlets(id),
    order_id INTEGER REFERENCES work_orders(id),
    reporter TEXT NOT NULL,
    contact TEXT,
    content TEXT NOT NULL,
    category TEXT,
    created_at TEXT NOT NULL,
    handled INTEGER NOT NULL DEFAULT 0,
    handle_note TEXT
);

-- 重量修订审计
CREATE TABLE IF NOT EXISTS weight_revisions (
    id INTEGER PRIMARY KEY,
    dispatch_id INTEGER NOT NULL REFERENCES dispatches(id),
    old_kg REAL,
    new_kg REAL NOT NULL,
    reason TEXT NOT NULL,
    operator TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 暴雨前复查提醒（进程内任务也落库，重启不丢）
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    inlet_id INTEGER REFERENCES inlets(id),
    order_id INTEGER REFERENCES work_orders(id),
    due_at TEXT NOT NULL,
    message TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- 简单账号（演示用，明文仅本地样例）
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    crew_id INTEGER REFERENCES crews(id)
);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else config.DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    config.ensure_dirs()
    path = Path(db_path) if db_path else config.DB_PATH
    with sqlite3.connect(str(path)) as conn:
        conn.executescript(SCHEMA)
