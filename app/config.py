from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
PHOTO_DIR = UPLOAD_DIR / "photos"
SAMPLE_DIR = UPLOAD_DIR / "sample"
DB_PATH = DATA_DIR / "stormwater.db"

# 相邻点位合并派车的判定：同一道路区段且平面距离（米）不超过该值
MERGE_MAX_DISTANCE = 150.0

# 工序顺序：开盖 -> 清掏 -> 冲洗 -> 复位
STEP_ORDER = ["open", "clean", "flush", "reset"]
STEP_LABELS = {
    "open": "开盖",
    "clean": "清掏",
    "flush": "冲洗",
    "reset": "复位",
}

# 暴雨前复查提醒默认提前量（秒）。测试可直接调 ReminderManager.schedule。
STORM_RECHECK_SECONDS = 1800

ALLOWED_PHOTO_EXT = {"jpg", "jpeg", "png", "webp", "svg"}

ROLE_INSPECTOR = "inspector"      # 巡查员
ROLE_DISPATCHER = "dispatcher"    # 调度员
ROLE_CREW = "crew"                # 班组
ROLE_SUPERVISOR = "supervisor"    # 主管
ROLE_ACCEPTOR = "acceptor"        # 验收员

ROLE_LABELS = {
    "inspector": "巡查员",
    "dispatcher": "调度员",
    "crew": "班组",
    "supervisor": "主管",
    "acceptor": "验收员",
}

ISSUE_KINDS = {
    "storm_urgent": "暴雨前加急",
    "cover_damaged": "井盖破损",
    "backflow": "积水反涌",
    "silt_spill": "淤泥遗撒",
    "vehicle_missing": "车辆未到",
    "photo_missing": "照片缺失",
}


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPLOAD_DIR, PHOTO_DIR, SAMPLE_DIR):
        d.mkdir(parents=True, exist_ok=True)
