"""客户端注册与配置模型。

存储物理设备的硬件标识（MAC 地址）以及各 ClassIsland 终端关联的配置档案。
Schema 隔离后不再需要 tenant_id 列。
"""

from datetime import datetime
from sqlalchemy import DateTime, String, text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Index
from .base import Base


class ClientRecord(Base):
    """已注册物理客户端设备的数据库记录。

    `server_default` 是必需的：登记脚本常用裸 SQL INSERT，只给 Python 侧 default=
    在裸 INSERT 下不生效，会直接撞 NOT NULL。
    """

    __tablename__ = "clients"

    uid: Mapped[str] = mapped_column(String, primary_key=True)
    client_id: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    mac: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ClientProfile(Base):
    """客户端与特定资源文件之间的关联映射。"""

    __tablename__ = "client_profiles"

    client_id: Mapped[str] = mapped_column(String, primary_key=True)
    # server_default 必不可少：裸 SQL 的 INSERT（如 rebuild_min_tenant / 早期登记脚本）
    # 不带这一列，缺省会让 NOT NULL 直接炸。
    class_id: Mapped[str] = mapped_column(
        String, default="", server_default=text("''"), index=True,
        comment="所属班级 id（空=设备级独立配置，老行为）"
    )
    class_plan: Mapped[str] = mapped_column(
        String, default="default_classplan", server_default=text("'default_classplan'")
    )
    time_layout: Mapped[str] = mapped_column(
        String, default="default_timelayout", server_default=text("'default_timelayout'")
    )
    subjects: Mapped[str] = mapped_column(String, default="default", server_default=text("'default'"))
    default_settings: Mapped[str] = mapped_column(String, default="default", server_default=text("'default'"))
    policy: Mapped[str] = mapped_column(String, default="default", server_default=text("'default'"))
    components: Mapped[str] = mapped_column(String, nullable=True, server_default=text("'default'"))
    credentials: Mapped[str] = mapped_column(String, nullable=True, server_default=text("'default'"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
