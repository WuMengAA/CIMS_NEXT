"""客户端注册与配置模型。

存储物理设备的硬件标识（MAC 地址）以及各 ClassIsland 终端关联的配置档案。
Schema 隔离后不再需要 tenant_id 列。
"""

from datetime import datetime
from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Index
from .base import Base


class ClientRecord(Base):
    """已注册物理客户端设备的数据库记录。"""

    __tablename__ = "clients"

    uid: Mapped[str] = mapped_column(String, primary_key=True)
    client_id: Mapped[str] = mapped_column(String, default="")
    mac: Mapped[str] = mapped_column(String, default="")
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ClientProfile(Base):
    """客户端与特定资源文件之间的关联映射。"""

    __tablename__ = "client_profiles"

    client_id: Mapped[str] = mapped_column(String, primary_key=True)
    class_id: Mapped[str] = mapped_column(String, default="", index=True,
                                          comment="所属班级 id（空=设备级独立配置，老行为）")
    class_plan: Mapped[str] = mapped_column(String)
    time_layout: Mapped[str] = mapped_column(String)
    subjects: Mapped[str] = mapped_column(String)
    default_settings: Mapped[str] = mapped_column(String)
    policy: Mapped[str] = mapped_column(String)
    components: Mapped[str] = mapped_column(String, nullable=True)
    credentials: Mapped[str] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
