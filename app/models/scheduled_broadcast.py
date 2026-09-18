"""定时广播配置模型（租户表）。

让管理端可以预置「在某时刻自动向某班/全校推送大屏通知」，到点由后端
后台调度器触发 command_queue 广播 —— 复用现有命令通道，插件端无需新逻辑。

作为租户表存在：不在 schema_init 的 _PUBLIC_ONLY 集合中，随每个租户 Schema
自动建表（ensure_tenant_schema），天然按租户隔离。
"""

from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ScheduledBroadcast(Base):
    """一条定时广播配置。"""

    __tablename__ = "scheduled_broadcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 展示名（面板用）
    name: Mapped[str] = mapped_column(String(128), default="", server_default=text("''"))
    # 调度类型：once（一次性）/ daily（每天）/ weekly（每周）
    schedule_type: Mapped[str] = mapped_column(
        String(16), default="once", server_default=text("'once'")
    )
    # 锚定时间（tz-aware UTC）：
    #   once   -> 触发时刻
    #   daily  -> 每天该时刻（仅取 time 分量）
    #   weekly -> 每周该 weekday 该时刻（取 time 分量 + weekday）
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, server_default=func.now()
    )
    # weekly 专用：星期几（0=周一 .. 6=周日），daily/once 忽略
    weekday: Mapped[int] = mapped_column(Integer, nullable=True)
    # 通知标题（对应 NotificationPayload.MessageMask）
    title: Mapped[str] = mapped_column(String(256), default="", server_default=text("''"))
    # 通知正文（对应 NotificationPayload.MessageContent）
    content: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    # 目标班级："" = 全校（所有设备）；否则 Class.id
    target_class_id: Mapped[str] = mapped_column(
        String(64), default="", server_default=text("''")
    )
    # 是否启用
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    # 上次实际触发时间
    last_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 下次预计触发时间（调度器据此判断到期；创建/每次触发后重算）
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, server_default=func.now()
    )
