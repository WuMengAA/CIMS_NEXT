"""班级与班级资源集模型。

填补 CIMS 缺失的「班级」聚合层：
- `Class`：一个班级，绑定一份资源集（resource_set_id），聚合多台设备。
- `ClassResourceSet`：班级版资源引用（与 client_profiles 同构 7 列），
  manifest 生成可复用同一套 _build_manifest。
- `ClassAuditLog`：班级操作的 append-only 审计流水（创建/提交/审核/绑定…）。
- `ClassPreview`：班级预览图（160x90 JPEG），由班级内设备周期压缩上传覆盖。

## 重构要点（2026-09-20：不以课表为划分依据）

旧模型把「课表」当成班级的划分依据（导入课表 → 自动派生出 `class_NN` 并绑设备），
导致「新机器自动归 1 班」「班级无法作为一个可管理的实体」等问题。新模型改为
**文件夹式**：

- 班级由管理端**显式创建**（像新建文件夹），编号统一为「xx届x班」；
- **课表只是班级的附加资源之一**（仍旧保留下发），不再反推/派生班级；
- 每个班级带 **属主** `owner_user_id`（逻辑引用 public.users.id）与
  **审核态** `review_status`（pending/approved/rejected），配合审核与多用户隔离；
- 「运行系统」（Windows/macOS/Linux…）是**设备级属性**，不落在班级上；
  组合显示名 = ``f"{code}_{device.os_name}"``（例如「2025届3班_Windows」）。

作为租户表存在：不在 schema_init 的 _PUBLIC_ONLY 集合中，因此会跟随
每个租户 Schema 自动建表，天然实现「同一租户内班级隔离」。
"""

import re
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, LargeBinary, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

# --------------------------------------------------------------------------- #
# 审核态取值（字符串常量，避免魔法串散落各处）
# --------------------------------------------------------------------------- #
REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
REVIEW_STATUSES = (REVIEW_PENDING, REVIEW_APPROVED, REVIEW_REJECTED)


def make_class_code(graduation_year: int, class_number: int) -> str:
    """班级编号显示名：``2025届3班``（不含运行系统）。"""
    return f"{graduation_year}届{class_number}班"


def make_class_id(graduation_year: int, class_number: int) -> str:
    """班级内部主键：``class_2025_3``（确定性、人可读、租户内唯一）。"""
    return f"class_{int(graduation_year)}_{int(class_number)}"


def combine_device_label(code: str, os_name: str) -> str:
    """班级编号 + 设备运行系统 的组合显示名，如 ``2025届3班_Windows``。

    运行系统缺失时只回班级编号，避免出现难看的尾随下划线。
    """
    code = (code or "").strip()
    os_name = (os_name or "").strip()
    if code and os_name:
        return f"{code}_{os_name}"
    return code or os_name


# --------------------------------------------------------------------------- #
# 运行系统归一化
# --------------------------------------------------------------------------- #
# .NET 的 `Environment.OSVersion.VersionString` 在 Windows 上给出的是
# 「Microsoft Windows NT 10.0.26200.0」这种长串，直接拼进组合显示名就是
# 「2025届3班_Microsoft Windows NT 10.0.26200.0」—— 又长又会被 32 字符截断。
# 组合显示名只需要「Windows / macOS / Linux」这一级粒度，故在此归一。
_OS_FAMILY_RES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"windows", re.I), "Windows"),
    (re.compile(r"\bmac\s?os\b|macos|darwin|osx", re.I), "macOS"),
    (re.compile(r"\blinux\b", re.I), "Linux"),
    (re.compile(r"\bandroid\b", re.I), "Android"),
    (re.compile(r"\bios\b", re.I), "iOS"),
    (re.compile(r"\bfreebsd\b", re.I), "FreeBSD"),
)


def normalize_os_family(raw: str, max_len: int = 16) -> str:
    """把客户端上报的运行系统串归一成**家族名**，如 ``Windows``。

    已知家族（Windows/macOS/Linux/Android/iOS/FreeBSD）按关键字识别；
    识别不出时退回首个词（例如 macOS/Linux 在 .NET 下都会报 ``Unix 24.1.0``，
    此时只能得到笼统的 ``Unix``）—— 总比把整条版本串塞进显示名要好。

    归一不到任何非空内容时返回空串，由调用方决定是否沿用旧值。
    """
    s = (raw or "").strip()
    if not s:
        return ""
    for pattern, family in _OS_FAMILY_RES:
        if pattern.search(s):
            return family
    head = s.split()[0].strip(" .,;:()[]") if s.split() else ""
    return (head or s)[:max_len]


class Class(Base):
    """一个班级（文件夹式实体）。

    ``id`` 是内部主键（形如 ``class_2025_3``，历史数据可能是 ``class_08``），
    ``code`` 才是给人看的「xx届x班」。二者刻意分开：编号可能因学校口径调整而
    变化，内部主键保持不变，避免牵连设备绑定/资源引用。
    """

    __tablename__ = "classes"

    # 内部主键，如 'class_2025_3'（旧数据 'class_08'）
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 显示名（文件夹名），如 '2025届3班微机室'
    name: Mapped[str] = mapped_column(String(128), default="", server_default=text("''"))
    # 班级编号显示名（不含运行系统），如 '2025届3班'
    code: Mapped[str] = mapped_column(String(128), default="", server_default=text("''"))
    # 届 / 班号（结构化字段，便于排序与快捷选择；历史数据可为空）
    graduation_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    class_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 绑定班级资源集
    resource_set_id: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    # 排序权重
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    # --- 多用户隔离与内容审核 --- #
    # 属主：public.users.id 的逻辑引用（跨 schema 不建 FK，删除用户不影响班级）
    owner_user_id: Mapped[str] = mapped_column(
        String, default="", server_default=text("''"), index=True,
        comment="班级创建者（public.users.id）；空=系统/历史班级"
    )
    # 审核态：pending=待审 / approved=通过 / rejected=驳回
    review_status: Mapped[str] = mapped_column(
        String(16), default=REVIEW_PENDING, server_default=text("'pending'"), index=True,
        comment="内容审核状态；只有 approved 的班级才允许绑定设备/下发"
    )
    reviewed_by: Mapped[str] = mapped_column(
        String, default="", server_default=text("''"), comment="审核人 public.users.id"
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reject_reason: Mapped[str] = mapped_column(
        String(512), default="", server_default=text("''"), comment="驳回原因"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        server_default=func.now()
    )


class ClassResourceSet(Base):
    """一个班级绑定的资源引用集（与 client_profiles 同构，供 manifest 复用）。

    ⚠️ 课表（ClassPlan/TimeLayout/Subjects）在这里只是**资源引用**：
    班级不因资源变化而增删，资源也不反推班级。
    """

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
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        server_default=func.now()
    )


class ClassAuditLog(Base):
    """班级操作审计流水（append-only，不做更新/删除）。

    记录「谁、何时、对哪个班、做了什么」，用于审核追溯与串扰排查。
    刻意与班级解耦（不建 FK）：班级被删后，其操作痕迹仍需保留。
    """

    __tablename__ = "class_audit_log"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=lambda: uuid.uuid4().hex
    )
    class_id: Mapped[str] = mapped_column(
        String(64), default="", server_default=text("''"), index=True
    )
    actor_user_id: Mapped[str] = mapped_column(
        String, default="", server_default=text("''"), comment="操作者 public.users.id"
    )
    # create | submit | approve | reject | assign | unassign | resource_write | preview_upload
    action: Mapped[str] = mapped_column(String(32), default="", server_default=text("''"))
    detail: Mapped[str] = mapped_column(
        Text, default="{}", server_default=text("'{}'"), comment="JSON 明细"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, server_default=func.now(), index=True
    )


class ClassPreview(Base):
    """班级预览图（160x90 JPEG），每班一行、覆盖式更新。

    由班级内设备（ClassIsland 插件 / 桌面客户端）每 30s 截屏 → 压缩为 160x90
    → 上传覆盖。管理端「多班级图形化卡片」据此实时显示各班画面。
    尺寸在服务端强校验（见 preview_routes），避免不同端各自缩放导致卡片错位。
    """

    __tablename__ = "class_previews"

    class_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    width: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    height: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # 上传来源设备，便于排查「某个班的预览图一直被某台机器覆盖」
    source_client_id: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        server_default=func.now(), index=True
    )
