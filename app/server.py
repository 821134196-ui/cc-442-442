"""零依赖 HTTP 服务：静态前端 + JSON API + 本地附件托管。

身份切换：请求头 X-Username（本地样例账号）。
"""
from __future__ import annotations

import json
import mimetypes
import re
from email.parser import BytesParser
from email.policy import default as email_default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, services
from .auth import load_user
from .errors import ServiceError
from .reminders import reminders

WEB_DIR = Path(__file__).resolve().parent / "web"


# ---------------------------------------------------------------- multipart

def parse_multipart(body: bytes, content_type: str) -> dict[str, dict]:
    """返回 {字段名: {filename?, value: bytes}}。"""
    header = b"Content-Type: " + content_type.encode() + b"\r\n\r\n"
    msg = BytesParser(policy=email_default).parsebytes(header + body)
    fields: dict[str, dict] = {}
    for part in msg.iter_parts() if msg.is_multipart() else []:
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        fields[name] = {"filename": filename, "value": part.get_payload(decode=True) or b""}
    return fields


# ---------------------------------------------------------------- 路由表

class Handler(BaseHTTPRequestHandler):
    server_version = "StormwaterPlatform/1.0"

    # -- 基础工具
    def _send_json(self, payload, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _user(self):
        return load_user(self.headers.get("X-Username"))

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("application/json"):
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                raise ServiceError("请求体不是合法 JSON", 400)
        if ctype.startswith("application/x-www-form-urlencoded"):
            return {k: v[0] for k, v in parse_qs(raw.decode()).items()}
        raise ServiceError(f"不支持的 Content-Type: {ctype}", 400)

    def _multipart(self) -> dict[str, dict]:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        return parse_multipart(body, self.headers.get("Content-Type", ""))

    def _query(self) -> dict:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def log_message(self, fmt, *args):  # 安静一点
        pass

    # -- 静态文件
    def _serve_file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(404, "Not found")
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, rel: str) -> None:
        # 防目录穿越
        root = (WEB_DIR).resolve()
        target = (root / rel).resolve()
        if root not in target.parents and target != root:
            self.send_error(403)
            return
        if target.is_dir():
            target = target / "index.html"
        self._serve_file(target)

    def _serve_upload(self, rel: str) -> None:
        root = config.UPLOAD_DIR.resolve()
        target = (root / rel).resolve()
        if root not in target.parents:
            self.send_error(403)
            return
        self._serve_file(target)

    # ---------------------------------------------------------- GET
    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            p = parsed.path
            q = self._query()
            if p.startswith("/api/"):
                self._user()  # 所有 API 均需身份（X-Username）
            if p == "/api/map":
                return self._send_json(services.map_service.overview())
            if p == "/api/directory":
                return self._send_json(services.map_service.directory())
            if p == "/api/orders":
                return self._send_json(services.order_service.list(
                    q.get("status"), q.get("issue_kind"),
                    int(q["segment_id"]) if q.get("segment_id") else None))
            m = re.fullmatch(r"/api/orders/(\d+)", p)
            if m:
                return self._send_json(services.order_service.get(int(m.group(1))))
            if p == "/api/schedule":
                return self._send_json(services.dispatch_service.schedule(
                    int(q["crew_id"]) if q.get("crew_id") else None,
                    q.get("date_from"), q.get("date_to")))
            m = re.fullmatch(r"/api/dispatches/(\d+)", p)
            if m:
                return self._send_json(services.dispatch_service.get(int(m.group(1))))
            if p == "/api/handovers":
                return self._send_json(services.vehicle_service.handovers())
            if p == "/api/acceptances":
                return self._send_json(services.acceptance_service.ledger(q.get("result")))
            if p == "/api/feedbacks":
                return self._send_json(services.feedback_service.list(
                    int(q["handled"]) if q.get("handled") is not None else None))
            if p == "/api/reminders":
                return self._send_json(services.reminder_service.list_pending())

            if p.startswith("/uploads/"):
                return self._serve_upload(p[len("/uploads/"):])
            if p == "/":
                return self._serve_static("index.html")
            if not p.startswith("/api/"):
                return self._serve_static(p.lstrip("/"))
            self.send_error(404, "未知接口")
        except ServiceError as e:
            self._send_json({"error": e.code, "message": e.message}, e.status)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": "internal", "message": str(e)}, 500)

    # ---------------------------------------------------------- POST
    def do_POST(self) -> None:
        try:
            p = urlparse(self.path).path
            user = self._user()

            if p == "/api/orders":
                b = self._body()
                return self._send_json(services.order_service.create(
                    user, int(b["inlet_id"]), int(b["block_level"]),
                    b.get("issue_kind") or None, b.get("issue_note") or None,
                    float(b["guard_radius_m"]) if b.get("guard_radius_m") else None,
                    b.get("guard_note") or None,
                    int(b.get("priority", 0)),
                    bool(b.get("schedule_recheck", False)),
                    int(b["recheck_minutes"]) if b.get("recheck_minutes") else None), 201)

            m = re.fullmatch(r"/api/orders/(\d+)/issues", p)
            if m:
                b = self._body()
                return self._send_json(services.order_service.report_issue(
                    user, int(m.group(1)), b["issue_kind"], b.get("note")))

            m = re.fullmatch(r"/api/orders/(\d+)/photo-missing", p)
            if m:
                b = self._body()
                return self._send_json(services.order_service.mark_photo_missing(
                    user, int(m.group(1)), b.get("note")))

            if p == "/api/dispatches":
                b = self._body()
                ids = b.get("order_ids") or [int(b["order_id"])]
                return self._send_json(services.dispatch_service.dispatch(
                    user, [int(x) for x in ids], int(b["crew_id"]),
                    b["start_at"], b["end_at"],
                    int(b["vehicle_id"]) if b.get("vehicle_id") else None,
                    b.get("note")), 201)

            m = re.fullmatch(r"/api/dispatches/(\d+)/steps", p)
            if m:
                b = self._body()
                return self._send_json(services.step_service.record(
                    user, int(m.group(1)), b["step"], b.get("note")))

            m = re.fullmatch(r"/api/dispatches/(\d+)/bags", p)
            if m:
                b = self._body()
                return self._send_json(services.step_service.record_bags(
                    user, int(m.group(1)), int(b["bag_count"])))

            m = re.fullmatch(r"/api/orders/(\d+)/photos", p)
            if m:
                form = self._multipart()
                required = ("phase", "kind", "file")
                for key in required:
                    if key not in form:
                        raise ServiceError(f"缺少字段 {key}", 422)
                dispatch_id = None
                if form.get("dispatch_id") and form["dispatch_id"]["value"]:
                    dispatch_id = int(form["dispatch_id"]["value"])
                return self._send_json(services.photo_service.upload(
                    user, int(m.group(1)),
                    form["phase"]["value"].decode(), form["kind"]["value"].decode(),
                    form["file"]["filename"] or "photo.bin",
                    form["file"]["value"], dispatch_id), 201)

            m = re.fullmatch(r"/api/dispatches/(\d+)/arrive", p)
            if m:
                return self._send_json(services.vehicle_service.arrive(
                    user, int(m.group(1))))

            m = re.fullmatch(r"/api/dispatches/(\d+)/missing", p)
            if m:
                b = self._body()
                return self._send_json(services.vehicle_service.mark_missing(
                    user, int(m.group(1)), b.get("note")))

            m = re.fullmatch(r"/api/dispatches/(\d+)/vehicle", p)
            if m:
                b = self._body()
                return self._send_json(services.vehicle_service.assign_vehicle(
                    user, int(m.group(1)), int(b["vehicle_id"])))

            m = re.fullmatch(r"/api/dispatches/(\d+)/weigh", p)
            if m:
                return self._send_json(services.vehicle_service.weigh(
                    user, int(m.group(1))))

            m = re.fullmatch(r"/api/dispatches/(\d+)/revise-weight", p)
            if m:
                b = self._body()
                return self._send_json(services.vehicle_service.revise_weight(
                    user, int(m.group(1)), float(b["new_kg"]), b.get("reason", "")))

            m = re.fullmatch(r"/api/orders/(\d+)/acceptance/request", p)
            if m:
                return self._send_json(services.acceptance_service.request(
                    user, int(m.group(1))))

            m = re.fullmatch(r"/api/orders/(\d+)/acceptance/decide", p)
            if m:
                b = self._body()
                return self._send_json(services.acceptance_service.decide(
                    user, int(m.group(1)), b["result"], b.get("comment")))

            if p == "/api/feedbacks":
                b = self._body()
                return self._send_json(services.feedback_service.create(
                    user, b["content"], b.get("reporter", user.display_name),
                    b.get("contact"), b.get("category"),
                    int(b["inlet_id"]) if b.get("inlet_id") else None,
                    int(b["order_id"]) if b.get("order_id") else None), 201)

            m = re.fullmatch(r"/api/feedbacks/(\d+)/handle", p)
            if m:
                b = self._body()
                return self._send_json(services.feedback_service.handle(
                    user, int(m.group(1)), b.get("note", "")))

            if p == "/api/reminders":
                b = self._body()
                return self._send_json(services.reminder_service.create(
                    user, int(b["inlet_id"]) if b.get("inlet_id") else None,
                    int(b["order_id"]) if b.get("order_id") else None,
                    b["due_at"], b["message"]), 201)

            if p == "/api/reminders/fire":
                return self._send_json({"fired": services.reminder_service.fire_due()})

            self.send_error(404, "未知接口")
        except ServiceError as e:
            self._send_json({"error": e.code, "message": e.message}, e.status)
        except (KeyError, ValueError) as e:
            self._send_json({"error": "validation_error", "message": f"参数错误：{e}"}, 422)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": "internal", "message": str(e)}, 500)


def run(host: str = "127.0.0.1", port: int = 8000) -> None:
    config.ensure_dirs()
    reminders._arm_next()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"社区雨水口清掏管理平台已启动：http://{host}:{port}")
    print("身份切换：页面右上角选择（请求头 X-Username）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
