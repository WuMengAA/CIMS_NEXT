"""自助切班（班次互换）模型：申请流水 + 单行配置。

两张表都挂在 Base 上，租户 schema 建表由 ensure_tenant_schema 的
Base.metadata.create_all 自动补建（app/models/schema_init.py），**不需要**手工迁移。

语义（见 _tasks/自助切班设计方案-2026-09-23.md）：
- class_swap_requests：一次互换申请的完整流水，状态机
  pending → approved → executed → rolled_back（approved 由免审批直通或审批通过产生）；
  pending → rejected | canceled；执行中先置 executing 防并发重入。
  创建时记录 from/to_plan_before 快照，回退 = 按快照换回。
- class_swap_config：单行（id=1）配置，requires_approval 审批开关 / auto_rollback
  到期自动回退开关 / max_pending_per_class 每班进行中申请上限。
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ClassSwapRequest(Base):
    __tablename__ = "class_swap_requests"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: uuid.uuid4().hex
    )
    from_class_id: Mapped[str] = mapped_column(String(64), index=True)
    to_class_id: Mapped[str] = mapped_column(String(64))
    # swap（互换）| oneway（单切：from 切到 to 当前的方案）
    swap_type: Mapped[str] = mapped_column(
        String(16), default="swap", server_default=text("'swap'")
    )
    reason: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    # 创建时快照（回退依据），执行/回退时以「当前值仍等于快照」校验防漂移
    from_plan_before: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    to_plan_before: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    # pending | approved | executing | executed | rolled_back | rejected | canceled | failed
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default=text("'pending'"), index=True
    )
    # 可选生效时段；effective_end_at 到期且 auto_rollback=true 时由巡检自动回退
    effective_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    effective_end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 操作者来自网站代理透传头（X-CIMS-Actor-Id / X-CIMS-Actor-Role），仅作审计元数据，
    # 权限判断在代理层（8097 认不出"谁在问"，见设计文档 §9 与记忆铁律）。
    initiator_user_id: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    initiator_role: Mapped[str] = mapped_column(String(32), default="", server_default=text("''"))
    approver_user_id: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    rejected_reason: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        server_default=func.now(),
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ClassSwapConfig(Base):
    __tablename__ = "class_swap_config"
    __table_args__ = (CheckConstraint("id = 1", name="ck_swap_config_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # false = 发起即自动执行；true = 待审批（manage 档通过才执行）
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    # true = executed 且 effective_end_at 到点后自动回退
    auto_rollback: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    max_pending_per_class: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1")
    )
    updated_by: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        server_default=func.now(),
    )
