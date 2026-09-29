"""基于本地样例账号表的极简鉴权：请求头 X-Username 切换身份。

本地演示平台不做密码与会话；每个请求携带 X-Username，服务端查 users 表得到角色。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config
from .db import connect
from .errors import PermissionError, ServiceError


@dataclass
class CurrentUser:
    username: str
    display_name: str
    role: str
    crew_id: int | None

    @property
    def role_label(self) -> str:
        return config.ROLE_LABELS.get(self.role, self.role)


def load_user(username: str | None) -> CurrentUser:
    if not username:
        raise ServiceError("缺少 X-Username 请求头（未登录）", 401, "unauthorized")
    with connect() as conn:
        row = conn.execute(
            "SELECT username,display_name,role,crew_id FROM users WHERE username=?",
            (username,),
        ).fetchone()
    if not row:
        raise ServiceError(f"未知用户：{username}", 401, "unauthorized")
    return CurrentUser(row["username"], row["display_name"], row["role"], row["crew_id"])


def require_roles(user: CurrentUser, *roles: str) -> None:
    if user.role not in roles:
        allowed = "、".join(config.ROLE_LABELS[r] for r in roles)
        raise PermissionError(f"该操作仅 {allowed} 可执行（当前身份：{user.role_label}）")


def require_crew_of(user: CurrentUser, crew_id: int) -> None:
    """班组成员只能操作本班组派工。"""
    if user.role == config.ROLE_CREW and user.crew_id != crew_id:
        raise PermissionError("只能记录本班组的派工")
