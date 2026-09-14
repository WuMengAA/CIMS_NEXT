"""班级与班级资源集模型。

填补 CIMS 缺失的「班级」聚合层：
- `Class`：一个班级，绑定一份资源集（class_resource_set_id），聚合多台设备。
- `ClassResourceSet`：班级版资源引用（与 client_profiles 同构 7 列），
  manifest 生成可复用同一套 _build_manifest。

作为租户表存在：不在 schema_init 的 _PUBLIC_ONLY 集合中，因此会跟随
每个租户 Schema 自动建表，天然实现「同一租户内班级隔离」。
"""

from datetime import datetime
from sqlalchemy import DateTime, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column
from .base import Base


class Class(Base):
    """一个班级。同一租户（schema）内的班级集合，服务端用 class_id 聚合资源与设备。"""

    __tablename__ = "classes"

    # 人可读主键，如 'class_3p1'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 班级名，如 '初三1班'
    name: Mapped[str] = mapped_column(String(128), default="")
    # 绑定班级资源集
    resource_set_id: Mapped[str] = mapped_column(String(64), default="")
    # 排序权重
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ClassResourceSet(Base):
    """一个班级绑定的资源引用集（与 client_profiles 同构，供 manifest 复用）。"""

    __tablename__ = "class_resource_sets"

    resource_set_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    class_plan: Mapped[str] = mapped_column(String, default="default_classplan",
                                            server_default=text("'default_classplan'"))
    time_layout: Mapped[str] = mapped_column(String, default="default_timelayout",
                                             server_default=text("'default_timelayout'"))
    subjects: Mapped[str] = mapped_column(String, default="default", server_default=text("'default'"))
    default_settings: Mapped[str] = mapped_column(String, default="default", server_default=text("'default'"))
    policy: Mapped[str] = mapped_column(String, default="default", server_default=text("'default'"))
    components: Mapped[str] = mapped_column(String, default="default", nullable=True,
                                            server_default=text("'default'"))
    credentials: Mapped[str] = mapped_column(String, default="default", nullable=True,
                                             server_default=text("'default'"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )
