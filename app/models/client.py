"""客户端注册与配置模型。

存储物理设备的硬件标识（MAC 地址）以及各 ClassIsland 终端关联的配置档案。
Schema 隔离后不再需要 tenant_id 列。
"""

from datetime import datetime
from sqlalchemy import DateTime, String, Text, text, func
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


class ClientStatus(Base):
    """教室端设备**运行时状态**上报（心跳）。

    为什么单开一张表、而不是往 `client_profiles` 加列：
      · `client_profiles` 是**配置**（这台机器该用哪套课表/组件），随管理端指令变化；
      · 本表是**遥测**（这台机器现在活着吗、装了什么、模块开了哪些），由设备自己
        高频覆盖写。两者生命周期与写入方完全不同，混在一起会让"配置变更时间"
        被心跳踩掉。

    「设备状态真实显示」的数据源就是它 —— 面板不再猜、不再用演示数据：
      · `reported_at` 距离现在多久 → 在线 / 离线；
      · `host` / `ip` / `version` → 是**哪台**机器、跑的哪个版本；
      · `modules_json` → 星璃功能模块开关的真实值（面板开关的回读依据）；
      · `plugins_json` → ClassIsland 插件清单（id/名称/版本/启用/加载状态）；
      · `extra_json` → 同步快照规模、当前课表群、命令轮询计数等零散遥测。
    """

    __tablename__ = "client_status"

    client_id: Mapped[str] = mapped_column(String, primary_key=True)
    host: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    ip: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    version: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    # 设备运行系统（设备级属性）：Windows / macOS / Linux …
    # 班级编号里的「xx届x班_设备运行系统」由它拼出（见 class_model.combine_device_label）。
    os_name: Mapped[str] = mapped_column(String(32), default="", server_default=text("''"))
    # 设备自报的所属班级（管理端 client_profiles.class_id 才是权威；
    # 这里冗余一份用于交叉校验「设备认为自己属于哪个班」与「管理端指派是否一致」）
    class_id: Mapped[str] = mapped_column(
        String, default="", server_default=text("''"), index=True
    )
    # 当前激活的课表群（本地档案实际生效的那一个，远程切班后可回读验证）
    active_class_group: Mapped[str] = mapped_column(
        String, default="", server_default=text("''")
    )
    modules_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default=text("'{}'"),
        comment="星璃功能模块开关 JSON: {module_id: bool}"
    )
    plugins_json: Mapped[str] = mapped_column(
        Text, default="[]", server_default=text("'[]'"),
        comment="ClassIsland 插件清单 JSON: [{id,name,version,enabled,status,isStelarith}]"
    )
    extra_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default=text("'{}'")
    )
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True,
        comment="设备最近一次心跳时间；面板据此判定在线/离线"
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
    # 设备级动作限制（v2.2 控制限制）：JSON 数组，存储被禁用的 stelarith_task 动作
    # （如 shutdown / reboot）。由操控端/管理端设置，「关键逻辑在服务端」——
    # 下发 stelarith_task 前由服务端校验，被禁动作直接拒绝，不落到设备端。
    action_restrictions: Mapped[str] = mapped_column(
        String, default="[]", server_default=text("[]"),
        comment="禁止执行的 stelarith 动作列表（JSON 数组）"
    )
