"""零依赖 HTTP 服务：JSON API + 静态前端 + 进程内暴雨提醒任务。"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .db import DB
from .services import Service

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".json": "application/json; charset=utf-8",
}


class ReminderWorker(threading.Thread):
    """进程内任务：每 10 秒扫描到期的暴雨前复查提醒。"""

    def __init__(self, svc: Service, interval: float = 10.0):
        super().__init__(daemon=True, name="reminder-worker")
        self.svc = svc
        self.interval = interval
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                fired = self.svc.check_due_reminders()
                for r in fired:
                    print(f"[提醒] {r['message']}")
            except Exception as exc:  # 任务异常不影响主进程
                print(f"[提醒任务异常] {exc}")

    def stop(self) -> None:
        self._stop.set()


def make_handler(db_path: str, upload_dir: Path):
    db = DB(db_path)
    db.init()
    svc = Service(db, upload_dir=upload_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "RainInlet/1.0"

        def log_message(self, fmt, *args):
            pass

        # ---------- 基础收发 ----------
        def _json(self, obj, status: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # 兼容普通表单
                data = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
            return data

        def _uid(self, data: dict) -> int:
            return int(data.get("user_id") or self.headers.get("X-User-Id") or 1)

        def _serve_file(self, rel: str) -> None:
            # 只允许 static 与 uploads 目录
            if rel.startswith("uploads/"):
                path = Path(upload_dir) / rel[len("uploads/"):]
            else:
                path = STATIC_DIR / rel
            try:
                path = path.resolve()
                if not (str(path).startswith(str(STATIC_DIR.resolve())) or
                        str(path).startswith(str(Path(upload_dir).resolve()))):
                    raise FileNotFoundError
                if path.is_dir():
                    path = path / "index.html"
                content = path.read_bytes()
            except (FileNotFoundError, IsADirectoryError, OSError):
                self.send_error(404)
                return
            ctype = CONTENT_TYPES.get(path.suffix, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            try:
                if path == "/api/overview":
                    self._json(svc.map_overview())
                elif path == "/api/inlet":
                    self._json(svc.inlet_detail(int(q["id"])))
                elif path == "/api/orders":
                    self._json(svc.orders(q.get("status")))
                elif path == "/api/order":
                    self._json(svc.order_detail(int(q["id"])))
                elif path == "/api/schedule":
                    self._json(svc.crew_schedule(q.get("date")))
                elif path == "/api/records":
                    self._json(svc.clean_records())
                elif path == "/api/handovers":
                    self._json(svc.vehicle_handovers())
                elif path == "/api/meta":
                    self._json({
                        "crews": [dict(r) for r in db.query("SELECT * FROM crews")],
                        "vehicles": [dict(r) for r in db.query("SELECT * FROM vehicles")],
                        "users": [dict(r) for r in db.query("SELECT * FROM users")],
                        "contacts": [dict(r) for r in db.query("SELECT * FROM contacts")],
                        "incidents": [dict(r) for r in db.query(
                            "SELECT * FROM incidents ORDER BY id DESC")],
                        "feedback": svc.feedback_list(),
                        "notifications": [dict(r) for r in db.query(
                            "SELECT * FROM notifications ORDER BY id DESC LIMIT 30")],
                        "reminders": [dict(r) for r in db.query(
                            "SELECT * FROM reminders ORDER BY fire_at")],
                    })
                elif path == "/" or path == "/index.html":
                    self._serve_file("index.html")
                elif path.startswith("/uploads/") or path.startswith("/static/"):
                    rel = path[1:]
                    if rel.startswith("static/"):
                        rel = rel[len("static/"):]
                    self._serve_file(rel)
                elif "." in path:
                    self._serve_file(path.lstrip("/"))
                else:
                    self._serve_file("index.html")
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            data = self._read_json()
            try:
                uid = self._uid(data)

                if path == "/api/orders":
                    self._json(svc.create_order(
                        uid, data["inlet_ids"], int(data["block_level"]),
                        data.get("description", ""), float(data.get("alert_radius", 0) or 0),
                        data.get("alert_note", ""), data.get("type", "normal"),
                        data.get("source", "patrol"), bool(data.get("urgent", False)),
                        data.get("storm_at"), data.get("contact_id"),
                        data.get("reporter")), 201)
                elif path == "/api/merge":
                    self._json(svc.merge_orders(uid, data["order_ids"]))
                elif path == "/api/dispatch":
                    self._json(svc.dispatch(uid, int(data["order_id"]), int(data["crew_id"]),
                                            int(data["vehicle_id"]), data["planned_start"],
                                            data["planned_end"]))
                elif path == "/api/start":
                    self._json(svc.start_work(uid, int(data["order_id"])))
                elif path == "/api/steps":
                    self._json(svc.record_step(uid, int(data["order_id"]), int(data["inlet_id"]),
                                               data["step"], data.get("note", "")), 201)
                elif path == "/api/photos":
                    # content 为 base64 或简单 SVG 文本
                    content = data.get("content", "").encode()
                    import base64
                    if data.get("encoding") == "base64":
                        content = base64.b64decode(data["content"])
                    self._json(svc.save_photo(uid, int(data["order_id"]), int(data["inlet_id"]),
                                              data["phase"], data.get("filename", "shot.svg"),
                                              content), 201)
                elif path == "/api/bags":
                    self._json(svc.set_bag_count(uid, int(data["order_id"]),
                                                 int(data["inlet_id"]), int(data["bags"])))
                elif path == "/api/submit":
                    self._json(svc.submit_acceptance(
                        uid, int(data["order_id"]),
                        [int(x) for x in data["inlet_ids"]] if data.get("inlet_ids") else None))
                elif path == "/api/review":
                    self._json(svc.review_acceptance(uid, int(data["order_id"]),
                                                     int(data["inlet_id"]),
                                                     bool(data["passed"]),
                                                     data.get("comment", "")))
                elif path == "/api/trips/arrive":
                    self._json(svc.trip_arrive(uid, int(data["trip_id"])))
                elif path == "/api/trips/handover":
                    self._json(svc.handover(uid, int(data["trip_id"]),
                                            int(data["bag_count"]), float(data["declared_weight_kg"]),
                                            data.get("handover_crew", ""), data.get("note", "")))
                elif path == "/api/trips/complete":
                    self._json(svc.complete_trip(uid, int(data["trip_id"]),
                                                 data.get("handover_driver")))
                elif path == "/api/trips/revise":
                    self._json(svc.revise_weight(uid, int(data["trip_id"]),
                                                 float(data["revised_weight_kg"]),
                                                 data["reason"]))
                elif path == "/api/incidents":
                    self._json(svc.report_incident(
                        uid, data["kind"], data.get("description", ""),
                        data.get("inlet_id"), data.get("order_id"), data.get("trip_id")), 201)
                elif path == "/api/incidents/close":
                    self._json(svc.close_incident(uid, int(data["incident_id"]),
                                                  data.get("note", "")))
                elif path == "/api/rush":
                    self._json(svc.rush_before_storm(uid, data["inlet_ids"],
                                                     data["storm_at"],
                                                     data.get("description", "")), 201)
                elif path == "/api/cover-damage":
                    self._json(svc.cover_damage(uid, int(data["inlet_id"]),
                                                data.get("description", "")), 201)
                elif path == "/api/backflow":
                    self._json(svc.backflow(uid, int(data["inlet_id"]),
                                            data.get("description", "")), 201)
                elif path == "/api/spill":
                    self._json(svc.spill_report(uid, int(data["order_id"]),
                                                data.get("description", "")), 201)
                elif path == "/api/vehicle-noshow":
                    self._json(svc.vehicle_noshow(uid, int(data["trip_id"]),
                                                  data.get("description", "车辆未到场")))
                elif path == "/api/reassign-vehicle":
                    self._json(svc.reassign_vehicle(uid, int(data["order_id"]),
                                                    int(data["vehicle_id"]),
                                                    data.get("planned_at")))
                elif path == "/api/photo-missing":
                    self._json(svc.mark_photo_missing(uid, int(data["order_id"]),
                                                      int(data["inlet_id"]),
                                                      data.get("description", "现场照片缺失")), 201)
                elif path == "/api/feedback/link":
                    self._json(svc.link_feedback(uid, int(data["feedback_id"]),
                                                 int(data["order_id"])))
                else:
                    self._json({"error": "not_found", "message": path}, 404)
            except Exception as exc:
                self._handle_error(exc)

        def _handle_error(self, exc: Exception) -> None:
            from .services import ServiceError
            if isinstance(exc, ServiceError):
                payload = {"error": exc.code, "message": exc.message}
                if exc.payload:
                    payload.update(exc.payload)
                self._json(payload, exc.status)
            elif isinstance(exc, (KeyError, ValueError, TypeError)):
                self._json({"error": "bad_request", "message": str(exc)}, 400)
            else:
                self._json({"error": "server_error", "message": str(exc)}, 500)

    return Handler, svc


def run(host: str = "127.0.0.1", port: int = 8000, db_path: str = "data/platform.db",
        upload_dir: str = "uploads") -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    handler, svc = make_handler(db_path, Path(upload_dir))
    httpd = ThreadingHTTPServer((host, port), handler)
    worker = ReminderWorker(svc)
    worker.start()
    print(f"雨水口清掏管理平台已启动: http://{host}:{port}")
    print(f"数据库: {db_path}  附件目录: {upload_dir}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        worker.stop()
        httpd.server_close()
