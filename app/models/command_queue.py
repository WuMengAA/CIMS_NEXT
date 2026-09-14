"""命令投递队列表模型。

「插件轮询自取」方案的核心存储：CIMS 下发 stelarith_task / 控制指令时，
除了走 gRPC 长连接内存队列推送外，同时把命令写入本表（pending 状态）；
教室端 ClassIsland 插件周期性地通过 HTTP 轮询取走并执行（标记 done）。

作为租户表存在：不在 schema_init 的 _PUBLIC_ONLY 集合中，因此会跟随
每个租户 Schema 自动建表，天然实现「同一租户内下发 + 取走」的隔离。
"""

from datetime import datetime
from sqlalchemy import DateTime, String, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class CommandQueueRecord(Base):
    """一条待教室端执行的投递命令记录。"""

    __tablename__ = "command_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 目标设备 UID（与 ClientRecord.uid / ClientProfile.client_id 一致）
    client_id: Mapped[str] = mapped_column(String, index=True)
    # 命令类型名，如 SendNotification / RestartApp / DataUpdated / stelarith_task
    command_type: Mapped[str] = mapped_column(String, default="")
    # 完整载荷（JSON 文本）。通知类为 NotificationPayload；stelarith_task 以原文作封装
    payload: Mapped[str] = mapped_column(Text, default="")
    # 消费语义：
    #   pending   = 未被取走
    #   delivered = 已被 poller 取走，等待客户端执行确认
    #   done      = 客户端确认执行完成
    #   failed    = 客户端确认执行失败（可重试）
    ack_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # 旧字段 status 兼容保留（pending/done 语义映射到 ack_status）
    status: Mapped[str] = mapped_column(String, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ack_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
