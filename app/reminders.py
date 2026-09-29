"""暴雨前复查提醒：落库 + 进程内定时触发。"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable

from .db import connect


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


class ReminderManager:
    """单进程内的定时提醒器（守护线程，测试可直接调用 fire_due）。"""

    def __init__(self) -> None:
        self._timer: threading.Timer | None = None
        self._listeners: list[Callable[[dict], None]] = []
        self._lock = threading.Lock()

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    def schedule(self, inlet_id: int | None, order_id: int | None,
                 due_at: datetime, message: str) -> int:
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO reminders(inlet_id,order_id,due_at,message,created_at)"
                " VALUES(?,?,?,?,?)",
                (inlet_id, order_id, due_at.replace(microsecond=0).isoformat(),
                 message, datetime.now().replace(microsecond=0).isoformat()),
            )
            rid = cur.lastrowid
        self._arm_next()
        return rid

    def fire_due(self) -> list[dict]:
        """触发所有到期提醒，返回触发内容（幂等：已触发不再返回）。"""
        now = datetime.now()
        fired: list[dict] = []
        with connect() as conn:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE done=0 AND due_at<=? ORDER BY due_at",
                (now.replace(microsecond=0).isoformat(),),
            ).fetchall()
            for r in rows:
                conn.execute("UPDATE reminders SET done=1 WHERE id=?", (r["id"],))
                fired.append(dict(r))
        for item in fired:
            for fn in list(self._listeners):
                try:
                    fn(item)
                except Exception:  # 监听器异常不影响调度
                    pass
        if fired:
            self._arm_next()
        return fired

    def list_pending(self) -> list[dict]:
        with connect() as conn:
            rows = conn.execute(
                "SELECT r.*, i.code inlet_code FROM reminders r"
                " LEFT JOIN inlets i ON i.id=r.inlet_id"
                " WHERE r.done=0 ORDER BY r.due_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def _arm_next(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            pending = self.list_pending()
            if not pending:
                self._timer = None
                return
            delay = max(1.0, (_parse(pending[0]["due_at"]) - datetime.now()).total_seconds())
            t = threading.Timer(delay, self.fire_due)
            t.daemon = True
            self._timer = t
            t.start()


reminders = ReminderManager()
