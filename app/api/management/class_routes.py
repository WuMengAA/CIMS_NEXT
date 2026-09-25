"""班级管理路由（Phase 1 班级层 + Phase 2 课表编辑与推送）。

路由挂载约定：本模块被 include 到 management router 的 `prefix="/class"` 下，
所以这里**只写 / 之后的段**（历史写法把 `class/` 又写了一遍，实际路径变成
`/class/class/create`，已修正）。

- 班级 CRUD：创建、列表、设备划入/移出。
- 班级课表写入：POST /class/{class_id}/resource/{resource_type}/write
  事务性：写 *_files 资源内容 + 同步 class_resource_sets 指向；ClassPlan 做引用完整性校验。
- **手动添加课表**：POST /class/{class_id}/apply-week-template（建空周课表骨架）
  + 上面的 resource write（逐天填课时），两者都产出官方 Profile 信封格式。
- **从官方档案导入**：POST /class/import-from-profile（ClassIsland 档案一次切出 N 个班）。
- 班级命令广播：POST /class/{class_id}/command/{command_type} 展开班级全部设备写 command_queue。
"""

import json
from datetime import datetime, timezone
from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant.context import get_tenant_id, get_schema, safe_identifier, schema_ctx, set_search_path
from app.core.config import DEFAULT_ACCOUNT_SLUG
from app.models.session import get_db
from app.models.class_model import (
    Class,
    ClassAuditLog,
    ClassPreview,
    ClassResourceSet,
    REVIEW_APPROVED,
    REVIEW_PENDING,
    REVIEW_REJECTED,
    combine_device_label,
    make_class_code,
    make_class_id,
)
from app.models.client import ClientProfile, ClientStatus, ClientRecord
from app.models.command_queue import CommandQueueRecord
from app.models.custom_role import CustomRole
from app.models.user import User
from app.api.command.model_map import MODEL_MAP
from app.api.command.payload_validator import validate_payload
from app.api.command.version_check import check_version
from app.api.command.timetable_validator import validate_classplan_references
from app.services.schedule_importer import DEFAULT_GROUP_GUID, GLOBAL_GROUP_GUID

async def _ensure_class_tenant(db: AsyncSession = Depends(get_db)) -> None:
    """把会话 search_path 钉到当前租户（与 scheduled_broadcast._ensure_tenant 同口径）。

    动机：8097 的 /class/* 经网站代理转发时为 `/class/...`（**无** /accounts/{id}/ 前缀），
    此刻 AccountContextMiddleware 不会注入租户 Schema，schema_ctx 回落到默认 "public"。
    若不显式设置，ORM 的 `select(Class)` 会去查 `public.classes` —— 而业务表已随
    租户隔离重构迁到 `tenant_<slug>.classes`，`public.classes` 仅剩一张空壳（已 rename
    成 `_legacy_classes`）。结果是 `list_classes` / 单班查询等**全部 500 逻辑熔断**，
    面板班级下拉拿不到数据、静默降级成演示班级。

    规则：优先用中间件已显式设置的租户 Schema；为空/为 public 时回退到
    DEFAULT_ACCOUNT_SLUG（面板默认目标租户，与 .env 一致），保证读写恒定落同一租户。
    """
    schema = schema_ctx.get()
    if not schema or schema == "public":
        schema = f"tenant_{DEFAULT_ACCOUNT_SLUG}"
    await set_search_path(db, schema)


router = APIRouter(dependencies=[Depends(_ensure_class_tenant)])

# 审核门槛：角色 priority ≥ 该值视为「可审核」（管理员/所有者）。
# 后续若新增独立「审核」角色，只要其 priority 落在此区间即自动生效，无需改代码。
REVIEW_MIN_PRIORITY = 80

# 班级预览图规格（服务端强校验，避免各端缩放口径不一致导致卡片错位）
PREVIEW_W = 160
PREVIEW_H = 90
PREVIEW_MAX_BYTES = 256 * 1024

# resource_type → class_resource_sets 列
RESOURCE_TO_CRS_COL = {
    "ClassPlan": "class_plan",
    "TimeLayout": "time_layout",
    "Subjects": "subjects",
    "DefaultSettings": "default_settings",
    "Policy": "policy",
    "Components": "components",
    "Credentials": "credentials",
}

# resource_type → *_files 表的默认资源名（ClassPlan 用默认，其余用 default）
RESOURCE_DEFAULT_NAME = {
    "ClassPlan": "default_classplan",
    "TimeLayout": "default_timelayout",
    "Subjects": "default",
    "DefaultSettings": "default",
    "Policy": "default",
    "Components": "default",
    "Credentials": "default",
}

# resource_type → 班级专属资源名前缀（缺省 name 时生成 cp_<class_id> 之类的独立资源，
# 避免把某个班的课表写进全校共享的 default_* 资源里而污染其他班级）
RESOURCE_PREFIX = {
    "ClassPlan": "cp",
    "TimeLayout": "tl",
    "Subjects": "sub",
    "DefaultSettings": "ds",
    "Policy": "pol",
    "Components": "comp",
    "Credentials": "cred",
}

DEFAULT_RESOURCE = {
    "class_plan": "default_classplan",
    "time_layout": "default_timelayout",
    "subjects": "default",
    "default_settings": "default",
    "policy": "default",
    "components": "default",
    "credentials": "default",
}


def _now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# 审核 / 隔离 / 预览 工具
# --------------------------------------------------------------------------- #


async def _actor_priority(db: AsyncSession, user_id: str) -> int:
    """查当前用户的全局角色优先级（无用户/无角色→0）。

    `users` / `custom_roles` 是 public 全局表；租户会话的 search_path 为
    ``"tenant_x", public``，因此可直接查询、无需切 schema。
    """
    if not user_id:
        return 0
    role_code = (
        await db.execute(select(User.role_code).where(User.id == user_id))
    ).scalar_one_or_none()
    if not role_code:
        return 0
    pr = (
        await db.execute(select(CustomRole.priority).where(CustomRole.code == role_code))
    ).scalar_one_or_none()
    return int(pr or 0)


async def _is_reviewer(db: AsyncSession, user_id: str) -> bool:
    """是否具备审核权限（管理员/所有者及以上）。"""
    return await _actor_priority(db, user_id) >= REVIEW_MIN_PRIORITY


def _audit(
    db: AsyncSession, class_id: str, actor: str, action: str, detail: dict | None = None
) -> None:
    """写一条班级审计流水（append-only，随当前事务一起提交）。"""
    db.add(
        ClassAuditLog(
            class_id=class_id or "",
            actor_user_id=actor or "",
            action=action,
            detail=json.dumps(detail or {}, ensure_ascii=False),
            created_at=_now(),
        )
    )


def _jpeg_size(raw: bytes) -> tuple[int, int] | None:
    """纯 Python 解析 JPEG 宽高（读 SOF 段），不引入 Pillow 依赖。

    返回 ``(width, height)``；非 JPEG 或解析失败返回 ``None``。
    """
    if len(raw) < 4 or raw[0] != 0xFF or raw[1] != 0xD8:  # SOI
        return None
    i, n = 2, len(raw)
    while i + 9 < n:
        if raw[i] != 0xFF:
            i += 1
            continue
        marker = raw[i + 1]
        if marker == 0xFF:  # 填充字节
            i += 1
            continue
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:  # 无长度字段
            i += 2
            continue
        seg_len = (raw[i + 2] << 8) | raw[i + 3]
        # SOF0..SOF15（排除 DHT=C4 / JPG=C8 / DAC=CC）承载宽高
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = (raw[i + 5] << 8) | raw[i + 6]
            width = (raw[i + 7] << 8) | raw[i + 8]
            return width, height
        i += 2 + seg_len
    return None


@router.post("/create")
async def create_class(
    request: Request,
    class_id: str = "",
    name: str = "",
    graduation_year: int | None = None,
    class_number: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    """创建班级（**文件夹式**，不再由课表派生）。

    两种调用方式：

    1. 结构化（推荐）：传 `graduation_year` + `class_number`
       → 内部主键自动生成 `class_2025_3`，编号 `code` 为 `2025届3班`；
    2. 旧式兼容：只传 `class_id` + `name`（无届/班号，`code` 回落到 `name`）。

    审核态：
      · 审核人（管理员/所有者）创建 → 直接 `approved`；
      · 普通用户创建 → `pending`（**待审**，通过审核前不允许绑定设备）。
    """
    # 租户守卫：非 /accounts/ 前缀路径（如经网站代理转发的 /class/...）下
    # AccountContextMiddleware 不会设置 tenant_ctx —— 直接 get_tenant_id() 会抛
    # RuntimeError("No tenant context set")，建班/审核等写操作全部 500 逻辑熔断
    # （线上实测 2026-09-25）。三级解析：
    #   ① tenant_ctx 优先（/accounts/ 前缀路径由中间件设置）；
    #   ② 其次 schema_ctx（tenant_<slug>）；
    #   ③ 兜底 DEFAULT_ACCOUNT_SLUG（_ensure_class_tenant 已把 search_path
    #      钉到 tenant_{DEFAULT_ACCOUNT_SLUG}，所属租户即该默认账户）。
    # 注意：schema_ctx 的 ContextVar 默认值恒为 "public"（_ensure_class_tenant
    # 只改 SQL search_path 不改 ContextVar），故 schema=="public" 必须走兜底。
    try:
        tid = get_tenant_id()
    except LookupError:
        tid = ""
    if not tid:
        schema = get_schema() or "public"
        if schema.startswith("tenant_"):
            tid = schema[len("tenant_") :]
        elif schema == "public":
            tid = DEFAULT_ACCOUNT_SLUG
        else:
            tid = schema
    if not tid:
        raise HTTPException(400, "租户上下文缺失")

    actor = getattr(request.state, "current_user_id", "") or ""
    reviewer = await _is_reviewer(db, actor)

    structured = graduation_year is not None and class_number is not None
    if not class_id:
        if not structured:
            raise HTTPException(400, "请提供 class_id，或同时提供 graduation_year 与 class_number")
        class_id = make_class_id(graduation_year, class_number)

    code = make_class_code(graduation_year, class_number) if structured else (name or class_id)
    if not name:
        name = code

    exists = (
        await db.execute(select(Class).where(Class.id == class_id))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(409, f"班级 {class_id} 已存在")

    status = REVIEW_APPROVED if reviewer else REVIEW_PENDING
    rs = ClassResourceSet(resource_set_id=class_id, updated_at=_now(), **DEFAULT_RESOURCE)
    cls = Class(
        id=class_id,
        name=name,
        code=code,
        graduation_year=graduation_year,
        class_number=class_number,
        resource_set_id=class_id,
        sort_order=int(class_number or 0),
        owner_user_id=actor,
        review_status=status,
        reviewed_by=actor if reviewer else "",
        reviewed_at=_now() if reviewer else None,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(rs)
    db.add(cls)
    _audit(db, class_id, actor, "create", {"name": name, "code": code, "review_status": status})
    await db.commit()
    return {
        "status": "success",
        "class_id": class_id,
        "code": code,
        "resource_set_id": class_id,
        "owner_user_id": actor,
        "review_status": status,
        "message": f"班级 {name} 已创建" + ("（待审核）" if status == REVIEW_PENDING else ""),
    }


@router.get("/pending")
async def list_pending_classes(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """列出**待审核**班级（仅审核人可见）。

    供管理端「审核队列」视图使用：普通用户新建的班级落在这里，
    审核通过后才会出现在可绑定设备的下拉里。
    """
    actor = getattr(request.state, "current_user_id", "") or ""
    if not await _is_reviewer(db, actor):
        raise HTTPException(403, "无权查看审核队列（需要审核/管理及以上角色）")

    rows = (
        await db.execute(
            select(Class).where(Class.review_status == REVIEW_PENDING).order_by(Class.created_at)
        )
    ).scalars().all()
    return {
        "status": "success",
        "count": len(rows),
        "classes": [
            {
                "class_id": c.id,
                "code": c.code,
                "name": c.name,
                "owner_user_id": c.owner_user_id,
                "graduation_year": c.graduation_year,
                "class_number": c.class_number,
                "review_status": c.review_status,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in rows
        ],
    }


@router.post("/{class_id}/review")
async def review_class(
    class_id: str,
    request: Request,
    action: str = Body(..., embed=True),
    reason: str = Body("", embed=True),
    db: AsyncSession = Depends(get_db),
):
    """审核班级：`action` 取 `approve` / `reject`（仅审核人）。

    通过后班级才允许绑定设备 / 下发资源；驳回时记录原因（`reject_reason`）。
    """
    actor = getattr(request.state, "current_user_id", "") or ""
    if not await _is_reviewer(db, actor):
        raise HTTPException(403, "无权审核班级（需要审核/管理及以上角色）")

    cls = (await db.execute(select(Class).where(Class.id == class_id))).scalar_one_or_none()
    if not cls:
        raise HTTPException(404, f"班级 {class_id} 不存在")

    act = (action or "").strip().lower()
    if act not in ("approve", "reject"):
        raise HTTPException(400, "action 必须是 approve 或 reject")

    approved = act == "approve"
    cls.review_status = REVIEW_APPROVED if approved else REVIEW_REJECTED
    cls.reviewed_by = actor
    cls.reviewed_at = _now()
    cls.reject_reason = "" if approved else (reason or "")
    cls.updated_at = _now()
    _audit(db, class_id, actor, "approve" if approved else "reject", {"reason": reason})
    await db.commit()
    return {
        "status": "success",
        "class_id": class_id,
        "review_status": cls.review_status,
        "reviewed_by": actor,
        "reject_reason": cls.reject_reason,
        "message": f"班级已{'通过审核' if approved else '被驳回'}",
    }


@router.get("/device-map")
async def device_class_map(db: AsyncSession = Depends(get_db)):
    """设备 ↔ 班级 映射（「一班一号、不共享」的权威视图）。

    返回：
      devices: {client_id: class_id}          未绑定班级的设备值为 ""（设备级独立配置）
      classes: [{class_id, name, devices:[…]}] 按班级归组，便于面板直接渲染

    为什么需要这个接口：
      · `client_profiles.class_id` 是**单值字段**，一台设备只能属于一个班 ——
        这正是「一班一号」的数据层保证；但此前没有任何接口把这个事实暴露出来，
        面板看不到设备归属，广播也无法「按班级定向」。
      · 广播定向的实现依赖它：把「目标班级名/号」解析成具体的设备 uid 集合，
        否则只能全量广播（＝向全校推送，与定向语义不符）。
    """
    rows = (await db.execute(select(Class).order_by(Class.sort_order, Class.name))).scalars().all()
    profs = (await db.execute(select(ClientProfile))).scalars().all()

    by_class: dict[str, list[str]] = {c.id: [] for c in rows}
    devices: dict[str, str] = {}
    for p in profs:
        devices[p.client_id] = p.class_id or ""
        if p.class_id and p.class_id in by_class:
            by_class[p.class_id].append(p.client_id)

    return {
        "devices": devices,
        "classes": [
            {"class_id": c.id, "name": c.name, "devices": sorted(by_class.get(c.id, []))}
            for c in rows
        ],
    }


@router.get("/device-status")
async def device_status(db: AsyncSession = Depends(get_db)):
    """设备**真实状态**总览（面板「设备控制 / 远程控制 / 插件管理」的唯一数据源）。

    与 `/device-map` 的区别：
      · `/device-map` 只回答「设备 ↔ 班级」这一个静态问题（用于广播定向解析）；
      · 本接口叠加**心跳遥测**（client_status 表），回答「现在活着吗、是哪台机器、
        什么版本、模块开了哪些、装了哪些插件」。面板不再需要拿配置时间冒充在线状态。

    在线判定：`reported_at` 距今 ≤ FRESH_SECONDS（90s，约 3 个心跳周期）。
    从未上报的设备 `online=false, reported=false` —— 与"曾经上线过但已离线"区分开，
    前者说明设备还没接上集控（或端点不通），后者说明设备掉线。

    未绑定班级的设备也会出现在 `devices` 里（`class_id=""`），否则管理端会"看不见"
    这台机器，也就无法把它指派进班级。
    """
    from app.api.client.status import FRESH_SECONDS
    from app.models.client import ClientStatus

    now = datetime.now(timezone.utc)

    classes = (await db.execute(select(Class).order_by(Class.sort_order, Class.name))).scalars().all()
    class_names = {c.id: c.name for c in classes}
    class_codes = {c.id: (c.code or c.name) for c in classes}

    profs = (await db.execute(select(ClientProfile))).scalars().all()
    statuses = {s.client_id: s for s in (await db.execute(select(ClientStatus))).scalars().all()}

    def _load(raw: str | None, fallback):
        try:
            return json.loads(raw) if raw else fallback
        except (TypeError, ValueError):
            return fallback

    devices: list[dict] = []
    # 以 client_profiles 为主体（管理端认识的设备），再并入只上报过心跳的设备
    all_ids = sorted({p.client_id for p in profs} | set(statuses.keys()))
    profile_by_id = {p.client_id: p for p in profs}

    def _skip_as_orphan(cid: str) -> bool:
        """同一物理主机若以多个 client_id 上报过，只保留「在线 或 已绑班」的那个活跃身份，
        把纯孤儿（无心跳/长期离线 且 未绑班）从列表剔除，避免 device-status 出现
        「同名 host 一在线一离线」的假冲突（例：N7-20091211 既报过 n7-20091211 又报过 lab-pc-001）。"""
        st = statuses.get(cid)
        prof = profile_by_id.get(cid)
        # 有主动绑定或正在线上报 → 绝不当作孤儿
        if prof is not None and prof.class_id:
            return False
        if st is not None and st.reported_at is not None:
            age = (now - st.reported_at).total_seconds()
            if age <= FRESH_SECONDS:
                return False
        # 检查是否有其它同 host 身份在当前更活跃/已绑定
        if st and st.host:
            dupes = [
                o for o in all_ids
                if o != cid and statuses.get(o) and statuses[o].host == st.host
            ]
            active = [
                o for o in dupes
                if (profile_by_id.get(o) and profile_by_id[o].class_id)
                or (
                    statuses.get(o) and statuses[o].reported_at
                    and (now - statuses[o].reported_at).total_seconds() <= FRESH_SECONDS
                )
            ]
            if active:
                return True
        return False

    all_ids = [cid for cid in all_ids if not _skip_as_orphan(cid)]

    for cid in all_ids:
        prof = profile_by_id.get(cid)
        st = statuses.get(cid)
        age = None
        if st is not None and st.reported_at is not None:
            age = (now - st.reported_at).total_seconds()
        devices.append(
            {
                "client_id": cid,
                "class_id": (prof.class_id if prof else "") or "",
                # bound=false 即"尚未绑定班级"：面板据此高亮 + 指派下拉，
                # 插件端据此弹 OOBE 引导，消除"新机器自动归1班且找不到绑定入口"。
                "bound": bool(prof.class_id if prof else ""),
                "class_name": class_names.get((prof.class_id if prof else "") or "", ""),
                "online": age is not None and age <= FRESH_SECONDS,
                "reported": st is not None,
                "age_seconds": int(age) if age is not None else None,
                "reported_at": st.reported_at.isoformat() if st and st.reported_at else None,
                "host": st.host if st else "",
                "ip": st.ip if st else "",
                "version": st.version if st else "",
                "os_name": st.os_name if st else "",
                "active_class_group": st.active_class_group if st else "",
                "modules": _load(st.modules_json if st else None, {}),
                "plugins": _load(st.plugins_json if st else None, []),
                "extra": _load(st.extra_json if st else None, {}),
            }
        )

    # 班级组合显示名：编号 + 该班设备最常见的运行系统（如 2025届3班_Windows）
    def _class_display(cid_class: str) -> str:
        counts: dict[str, int] = {}
        for d in devices:
            if (d.get("class_id") or "") == cid_class:
                o = d.get("os_name") or ""
                if o:
                    counts[o] = counts.get(o, 0) + 1
        dom = max(counts.items(), key=lambda kv: kv[1])[0] if counts else ""
        return combine_device_label(class_codes.get(cid_class, cid_class), dom)

    return {
        "fresh_seconds": FRESH_SECONDS,
        "count": len(devices),
        "online_count": sum(1 for d in devices if d["online"]),
        "devices": devices,
        # 可选班级清单（指派下拉用）：只暴露展示与门控所需字段
        "suggest": [
            {
                "class_id": c.id,
                "name": c.name,
                "code": class_codes.get(c.id, ""),
                "display_code": _class_display(c.id),
                "review_status": c.review_status,
                "reviewable": (c.review_status or REVIEW_PENDING) == REVIEW_APPROVED,
            }
            for c in classes
        ],
        "classes": [
            {
                "class_id": c.id,
                "name": c.name,
                "code": class_codes.get(c.id, ""),
                "display_code": _class_display(c.id),
                "review_status": c.review_status,
                "devices": sorted(p.client_id for p in profs if p.class_id == c.id),
            }
            for c in classes
        ],
    }


@router.get("/list")
async def list_classes(
    request: Request,
    scope: str = "auto",
    db: AsyncSession = Depends(get_db),
):
    """列出班级（含设备数与组合显示名）。

    **多用户隔离**（`scope`）：
      · `auto`（默认）：审核人看到全部；普通用户只看**自己创建的**班级，
        外加**无属主的历史/系统班级**（否则老数据会在界面上凭空消失）；
      · `mine`：强制只看自己创建的；
      · `all`：仅审核人可用，越权返回 403。

    每项附带 `display_code` —— 班级编号 + 该班设备最常见的运行系统，
    例如 `2025届3班_Windows`；运行系统取自设备级心跳（`client_status.os_name`）。
    """
    actor = getattr(request.state, "current_user_id", "") or ""
    reviewer = await _is_reviewer(db, actor)

    stmt = select(Class)
    if scope == "all":
        if not reviewer:
            raise HTTPException(403, "无权查看全部班级")
    elif scope == "mine" or not reviewer:
        # 自己创建的 + 无属主的历史/系统班级
        stmt = stmt.where((Class.owner_user_id == actor) | (Class.owner_user_id == ""))
    stmt = stmt.order_by(Class.sort_order, Class.code, Class.name)
    rows = (await db.execute(stmt)).scalars().all()

    # 设备归属 + 设备运行系统（各一次查询，避免 N+1）
    dev_rows = (
        await db.execute(
            select(ClientProfile.class_id, ClientProfile.client_id).where(ClientProfile.class_id != "")
        )
    ).all()
    dev_by_class: dict[str, list[str]] = {}
    for cid_class, cid in dev_rows:
        dev_by_class.setdefault(cid_class, []).append(cid)

    os_rows = (await db.execute(select(ClientStatus.client_id, ClientStatus.os_name))).all()
    os_by_client = {cid: (osn or "") for cid, osn in os_rows}

    # 班级 → 课表资源名（cp_classNN）。面板的「班级下拉」必须显示**班级名**并据此
    # 去取该班课表；若只给班级 id / 资源集 id，前端就只能退回去列 ClassPlan 资源名
    # （default_classplan / cp_class01…），于是默认选中一个**空课表**资源，
    # 表现为「课表页打开是空的、而且不知道自己在看哪个班」。
    # 一次查询批量取回，避免 N+1。
    rs_rows = (
        await db.execute(select(ClassResourceSet.resource_set_id, ClassResourceSet.class_plan))
    ).all()
    plan_by_rs = {rs: (cp or "") for rs, cp in rs_rows}

    out = []
    for cls in rows:
        devs = dev_by_class.get(cls.id, [])
        counts: dict[str, int] = {}
        for cid in devs:
            o = os_by_client.get(cid, "")
            if o:
                counts[o] = counts.get(o, 0) + 1
        dominant_os = max(counts.items(), key=lambda kv: kv[1])[0] if counts else ""
        out.append(
            {
                "class_id": cls.id,
                "name": cls.name,
                "code": cls.code,
                "display_code": combine_device_label(cls.code or cls.name, dominant_os),
                "class_plan": plan_by_rs.get(cls.resource_set_id or "", ""),
                "graduation_year": cls.graduation_year,
                "class_number": cls.class_number,
                "resource_set_id": cls.resource_set_id,
                "sort_order": cls.sort_order,
                "owner_user_id": cls.owner_user_id,
                "review_status": cls.review_status,
                # 驳回原因一并下发：否则被驳回的班级在界面上只剩一个「已驳回」标签，
                # 提交人看不到为什么被拒，只能反复重提交 —— 审核闭环断在这里。
                "reject_reason": cls.reject_reason or "",
                "device_count": len(devs),
                "updated_at": str(cls.updated_at),
            }
        )
    return out


@router.post("/device/assign")
async def assign_device_to_class(
    request: Request,
    class_id: str,
    client_id: str,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """把一台设备划入班级（client_profiles.class_id = class_id）。

    「一班一号、不共享」的两条硬约束：

      1. **不共享**：`class_id` 是单值字段，赋值即转移 —— 一台设备在任一时刻
         只属于一个班。这是数据层保证，不靠调用方自觉。
      2. **不静默抢占**：设备已属于**另一个**班时，默认返回 409 而不是直接覆盖。
         早期实现是无条件 `prof.class_id = class_id`，于是「给 2 班指派一台
         原本属于 1 班的设备」会悄悄把 1 班的设备抢走 —— 1 班从此收不到自己的
         广播/课表，且没有任何报错，是最难查的一类事故。
         确实要转移时显式传 `force=true`（面板上表现为二次确认）。
    """
    cls = (
        await db.execute(select(Class).where(Class.id == class_id))
    ).scalar_one_or_none()
    if not cls:
        raise HTTPException(404, f"班级 {class_id} 不存在")

    actor = getattr(request.state, "current_user_id", "") or ""
    reviewer = await _is_reviewer(db, actor)

    # 门控 1 · 内容审核：未通过审核的班级不得绑定设备（杜绝未审内容落到教室大屏）
    if (cls.review_status or REVIEW_PENDING) != REVIEW_APPROVED:
        raise HTTPException(
            409,
            f"班级「{cls.name or cls.id}」当前状态为 {cls.review_status}，"
            f"尚未通过审核，暂不能绑定设备。",
        )
    # 门控 2 · 多用户隔离：非审核人只能操作自己创建的班级
    owner = cls.owner_user_id or ""
    if not reviewer and owner and owner != actor:
        raise HTTPException(403, "无权操作他人创建的班级")

    # 方案 B：面板传入的 client_id 可能是 uid 或主机名，先收敛成稳定 uid 再解析档案，
    # 改名（主机名变）也始终命中同一份档案，绑定关系不被打断。
    uid = client_id
    rec = (
        await db.execute(select(ClientRecord).where(ClientRecord.uid == client_id))
    ).scalar_one_or_none()
    if rec is None:
        rec = (
            await db.execute(
                select(ClientRecord).where(ClientRecord.client_id == client_id)
            )
        ).scalar_one_or_none()
    if rec is not None:
        uid = rec.uid

    prof = (
        await db.execute(
            select(ClientProfile).where(ClientProfile.client_id.in_([uid, client_id]))
        )
    ).scalar_one_or_none()
    # 自动建档：设备可能还没上报过心跳（刚装机、或想提前预绑定），此前这里直接
    # 404「配置档案不存在」，导致面板上看得见设备却绑不了班。改为按需建空档
    # （class_id 随后由下面统一赋值），使「先指派、后装机」也能生效——装好一启动
    # 就直接拿到本班课表，不必等现场再补一次操作。
    created_profile = False
    if not prof:
        prof = ClientProfile(client_id=uid)
        db.add(prof)
        created_profile = True
    elif prof.client_id != uid:
        prof.client_id = uid  # 旧行以主机名作主键 → 迁到稳定 uid

    prev = prof.class_id or ""
    if prev and prev != class_id and not force:
        prev_name = (
            await db.execute(select(Class.name).where(Class.id == prev))
        ).scalar_one_or_none() or prev
        raise HTTPException(
            409,
            f"设备 {client_id} 已属于「{prev_name}」（{prev}）。"
            f"一台设备同一时间只能属于一个班；确需转移请显式确认（force=true）。",
        )

    prof.class_id = class_id
    _audit(
        db, class_id, actor, "assign",
        {"client_id": client_id, "previous_class_id": prev, "transferred": bool(prev and prev != class_id)},
    )
    await db.commit()
    return {
        "status": "success",
        "client_id": client_id,
        "class_id": class_id,
        "previous_class_id": prev,
        "transferred": bool(prev and prev != class_id),
        "created_profile": created_profile,
    }


@router.post("/device/unassign")
async def unassign_device_from_class(
    request: Request,
    client_id: str,
    db: AsyncSession = Depends(get_db),
):
    """把设备移出班级（回退设备级配置）。"""
    # 方案 B：令牌收敛为稳定 uid 再解析档案。
    uid = client_id
    rec = (
        await db.execute(select(ClientRecord).where(ClientRecord.uid == client_id))
    ).scalar_one_or_none()
    if rec is None:
        rec = (
            await db.execute(
                select(ClientRecord).where(ClientRecord.client_id == client_id)
            )
        ).scalar_one_or_none()
    if rec is not None:
        uid = rec.uid

    prof = (
        await db.execute(
            select(ClientProfile).where(ClientProfile.client_id.in_([uid, client_id]))
        )
    ).scalar_one_or_none()
    if not prof:
        raise HTTPException(404, f"设备 {client_id} 的配置档案不存在")

    actor = getattr(request.state, "current_user_id", "") or ""
    prev = prof.class_id or ""
    if prev:
        # 多用户隔离：非审核人不能把设备从别人的班里移出
        cls = (await db.execute(select(Class).where(Class.id == prev))).scalar_one_or_none()
        if cls and not await _is_reviewer(db, actor) and (cls.owner_user_id or "") not in ("", actor):
            raise HTTPException(403, "无权操作他人创建的班级")

    prof.class_id = ""
    _audit(db, prev, actor, "unassign", {"client_id": client_id})
    await db.commit()
    return {"status": "success", "client_id": client_id, "class_id": "", "previous_class_id": prev}


@router.post("/{class_id}/resource/{resource_type}/write")
@router.put("/{class_id}/resource/{resource_type}/write")
async def write_class_resource(
    class_id: str,
    resource_type: str,
    payload: dict = Body(...),
    name: str = "",
    version: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    """写班级的某类资源。

    事务性完成三件事（Phase 2 核心）：
    1. 把 payload 写入对应 *_files 表（name 缺省用该类型默认名）
    2. validate_payload 防超大/深嵌套
    3. ClassPlan 额外做引用完整性校验（TimeLayoutId/SubjectId 必须存在）
    4. 同步 class_resource_sets.<field> 指向该资源名，使 manifest 立即用新资源
    """
    validate_payload(payload)
    model = MODEL_MAP.get(resource_type)
    if not model:
        raise HTTPException(400, f"无效资源类型 {resource_type}")
    crs_col = RESOURCE_TO_CRS_COL.get(resource_type)
    if not crs_col:
        raise HTTPException(400, f"资源类型 {resource_type} 不支持班级级写入")

    crs = (
        await db.execute(select(ClassResourceSet).where(ClassResourceSet.resource_set_id == class_id))
    ).scalar_one_or_none()
    if crs is None:
        raise HTTPException(404, f"班级 {class_id} 的资源集不存在")

    current_name = getattr(crs, crs_col, None)
    default_name = RESOURCE_DEFAULT_NAME.get(resource_type, "default")
    if name:
        res_name = name
    elif current_name and current_name != default_name:
        # 该班已有专属资源 → 就地编辑，不新建
        res_name = current_name
    else:
        res_name = f"{RESOURCE_PREFIX.get(resource_type, 'res')}_{class_id}"

    # ClassPlan 引用校验：TimeLayoutId 必须在班级作息资源里存在、科目必须在校科目资源词典里
    if resource_type == "ClassPlan":
        tl_name = getattr(crs, "time_layout", None) or default_name
        sub_name = getattr(crs, "subjects", None) or "default"
        await validate_classplan_references(db, payload, tl_name, sub_name)

    # 写 *_files 资源内容（与 data_write 同语义）
    record = (await db.execute(select(model).where(model.name == res_name))).scalar_one_or_none()
    if record:
        check_version(record, version)
    else:
        record = model(name=res_name)
    record.content = json.dumps(payload)
    record.version = (record.version or 0) + 1
    record.updated_at = _now()
    db.add(record)

    # 同步 class_resource_sets 指向新资源
    setattr(crs, crs_col, res_name)
    crs.updated_at = _now()
    db.add(crs)

    await db.commit()
    return {
        "status": "success",
        "class_id": class_id,
        "resource_type": resource_type,
        "name": res_name,
        "version": record.version,
        "message": f"班级 {class_id} 的 {resource_type} 已写入并生效",
    }


@router.get("/{class_id}/schedule")
async def get_class_schedule(class_id: str, db: AsyncSession = Depends(get_db)):
    """读回班级当前生效的课表（供网页端/插件展示「今天上什么课」）。

    返回的是**已下发资源**的真实内容（ClassResourceSet 指向的 ClassPlan + TimeLayout
    + Subjects），不是 website 侧的编辑草稿——这样教室端与网页端看到的是同一份数据。

    ClassPlan/TimeLayout/Subjects 三类资源都是官方 Profile 信封
    （ClassPlan→{ClassPlans:{},ClassPlanGroups:{}}，TimeLayout→{TimeLayouts:{}}，
    Subjects→{Subjects:{}}），这里解包后按 ClassPlanGroup 归组，输出扁平化的班级列表。
    """
    crs = (
        await db.execute(
            select(ClassResourceSet).where(ClassResourceSet.resource_set_id == class_id)
        )
    ).scalar_one_or_none()
    if crs is None:
        raise HTTPException(404, f"班级 {class_id} 的资源集不存在")

    async def _read(model, name: str | None):
        if not name:
            return None
        row = (await db.execute(select(model).where(model.name == name))).scalar_one_or_none()
        if row is None or not row.content:
            return None
        try:
            return json.loads(row.content)
        except (ValueError, TypeError):
            return None

    cp_raw = await _read(MODEL_MAP["ClassPlan"], getattr(crs, "class_plan", None))
    tl_raw = await _read(MODEL_MAP["TimeLayout"], getattr(crs, "time_layout", None))
    sub_raw = await _read(MODEL_MAP["Subjects"], getattr(crs, "subjects", None))

    # --- 解包官方 Profile 信封（拿不到时按空处理，不抛——编排期资源可能尚未写入） ---
    class_plans = (cp_raw or {}).get("ClassPlans", {}) if isinstance(cp_raw, dict) else {}
    class_plan_groups = (
        (cp_raw or {}).get("ClassPlanGroups", {}) if isinstance(cp_raw, dict) else {}
    )
    time_layouts = (tl_raw or {}).get("TimeLayouts", {}) if isinstance(tl_raw, dict) else {}
    subjects = (sub_raw or {}).get("Subjects", {}) if isinstance(sub_raw, dict) else {}

    # 科目 GUID → 名称（Subjects 是字典，键=科目 GUID）
    subject_map: dict[str, dict] = {}
    for guid, sub in (subjects or {}).items():
        if isinstance(sub, dict):
            subject_map[guid] = {
                "id": guid,
                "name": sub.get("Name") or guid,
                "initial": sub.get("Initial") or "",
                "teacher": sub.get("TeacherName") or "",
            }
        else:
            subject_map[guid] = {"id": guid, "name": str(sub), "initial": "", "teacher": ""}

    _EMPTY_GUID = "00000000-0000-0000-0000-000000000000"

    def _layout_brief(layout_id: str | None) -> dict:
        """把 TimeLayout 收成 {name, items:[{start,end,type,break_name,is_class}]}。

        真实结构：`TimeLayouts[<guid>].Layouts` 是**数组**，每项含
        StartTime/EndTime/TimeType/BreakName。TimeType: 0=上课 1=课间 2=其他。
        注意键名是 `Layouts`，不是 `LayoutItems`（早期按 LayoutItems 读会读到空数组）。
        """
        layout = (time_layouts or {}).get(layout_id or "")
        if not isinstance(layout, dict):
            return {"id": layout_id, "name": None, "items": []}
        items = []
        for it in layout.get("Layouts") or []:
            if not isinstance(it, dict):
                continue
            ttype = it.get("TimeType", 0)
            items.append(
                {
                    "start": it.get("StartTime"),
                    "end": it.get("EndTime"),
                    "type": ttype,
                    "is_class": ttype == 0,
                    "break_name": it.get("BreakName") or "",
                    "default_class_id": it.get("DefaultClassId") or "",
                }
            )
        return {
            "id": layout_id,
            "name": layout.get("Name"),
            "items": items,
        }

    plans_out = []
    for pid, plan in (class_plans or {}).items():
        if not isinstance(plan, dict):
            continue
        tl_id = plan.get("TimeLayoutId")
        tl_brief = _layout_brief(tl_id)
        # 只有「上课」时段承载课次；用它把 Classes 的槽位对齐到真实时间
        class_slots = [it for it in tl_brief["items"] if it.get("is_class")]

        # 真实结构：`Classes` 是**扁平数组**，每项就是一个课次槽位
        # （不是「天 → 课次」的二维数组；一个 ClassPlan 只代表一天）
        lessons = []
        raw_classes = plan.get("Classes") or []
        for slot_idx, lesson in enumerate(raw_classes):
            subj_guid = lesson if isinstance(lesson, str) else (
                lesson.get("SubjectId") if isinstance(lesson, dict) else None
            )
            if not subj_guid or subj_guid == _EMPTY_GUID:
                continue
            slot = class_slots[slot_idx] if slot_idx < len(class_slots) else {}
            subj = subject_map.get(subj_guid, {})
            lessons.append(
                {
                    "slot": slot_idx + 1,
                    "subject_id": subj_guid,
                    "subject": subj.get("name") or subj_guid,
                    "initial": subj.get("initial") or "",
                    "teacher": subj.get("teacher") or "",
                    "start": slot.get("start"),
                    "end": slot.get("end"),
                }
            )

        plans_out.append(
            {
                "id": pid,
                "name": plan.get("Name") or pid,
                "group_id": plan.get("AssociatedGroup"),
                "time_layout_id": tl_id,
                "time_layout": tl_brief,
                "lesson_count": len(lessons),
                "lessons": lessons,
            }
        )

    # 按课表群分组（同一群下的多个 ClassPlan 才是「同一张课表的各天」）
    groups_out = []
    for gid, gmeta in (class_plan_groups or {}).items():
        gname = gmeta.get("Name") if isinstance(gmeta, dict) else None
        members = [p for p in plans_out if p.get("group_id") == gid]
        groups_out.append(
            {
                "id": gid,
                "name": gname,
                "is_global": bool(gmeta.get("IsGlobal")) if isinstance(gmeta, dict) else False,
                "class_plans": members,
            }
        )
    # 无群归属的孤立课表也回传，避免静默丢失
    orphan = [p for p in plans_out if not p.get("group_id")]
    if orphan:
        groups_out.append(
            {"id": None, "name": "（未归群）", "is_global": False, "class_plans": orphan}
        )

    return {
        "status": "success",
        "class_id": class_id,
        "resource_set_id": crs.resource_set_id,
        "class_plan_name": getattr(crs, "class_plan", None),
        "time_layout_name": getattr(crs, "time_layout", None),
        "subjects_name": getattr(crs, "subjects", None),
        "class_plan_groups": groups_out,
        "class_plans": plans_out,
        "subjects": subject_map,
    }


@router.post("/{class_id}/command/{command_type}")
async def broadcast_to_class(
    class_id: str,
    command_type: str,
    payload: dict = Body(default={}),
    db: AsyncSession = Depends(get_db),
):
    """向班级全部设备广播命令（Phase 2 推送）。

    展开 class_id → 班级内全部 client_profiles.client_id → 逐设备写 command_queue(pending)。
    教室端插件经 HTTP poller 轮询取走执行。
    """
    devices = (
        await db.execute(
            select(ClientProfile.client_id).where(ClientProfile.class_id == class_id)
        )
    ).scalars().all()
    if not devices:
        raise HTTPException(404, f"班级 {class_id} 下没有设备")

    inserted = 0
    for cid in devices:
        db.add(
            CommandQueueRecord(
                client_id=cid,
                command_type=command_type,
                payload=json.dumps(payload, ensure_ascii=False) if payload else "",
                status="pending",
                ack_status="pending",
            )
        )
        inserted += 1
    await db.commit()
    return {
        "status": "success",
        "class_id": class_id,
        "command_type": command_type,
        "devices": inserted,
        "message": f"已向 {inserted} 台设备广播 {command_type}",
    }


@router.post("/{class_id}/activate")
async def activate_class_on_devices(
    class_id: str,
    db: AsyncSession = Depends(get_db),
):
    """让本班全部设备**切换显示**到该班的课表（远程切班）。

    背景（为什么需要独立端点）：ClassIsland 官方档案的 `SelectedClassPlanGroupId`
    只存在**本地档案**里，集控通道合并资源时只逐键合 ClassPlans/TimeLayouts/Subjects
    等字典，**不会覆盖**它。因此「这台教室机该显示哪一班」只能由教室端插件本地改档案。

    本端点把「切到哪个课表群」解析成具体 GUID（从该班资源集里的 ClassPlan 反查
    AssociatedGroup，再用 ClassPlanGroups 补出群名），然后向班级内每台设备的
    command_queue 写一条 `stelarith_set_active_class`（payload 含 group_id/group_name），
    插件轮询取走后调 StelarithProfileWriter.SetActiveClassGroup 完成切班。
    """
    crs = (
        await db.execute(
            select(ClassResourceSet).where(ClassResourceSet.resource_set_id == class_id)
        )
    ).scalar_one_or_none()
    if crs is None:
        raise HTTPException(404, f"班级 {class_id} 没有绑定资源集")

    cp_name = getattr(crs, "class_plan", None) or f"cp_{class_id}"
    row = (
        await db.execute(select(MODEL_MAP["ClassPlan"]).where(MODEL_MAP["ClassPlan"].name == cp_name))
    ).scalar_one_or_none()
    if row is None or not row.content:
        raise HTTPException(404, f"课表资源 {cp_name} 不存在")

    try:
        envelope = json.loads(row.content) if isinstance(row.content, str) else row.content
    except Exception as exc:
        raise HTTPException(500, f"课表资源解析失败：{exc}")

    plans = (envelope or {}).get("ClassPlans") or {}
    groups = (envelope or {}).get("ClassPlanGroups") or {}
    if not plans:
        raise HTTPException(422, f"课表资源 {cp_name} 里没有任何 ClassPlan")

    # 本班的课表群：优先读资源里显式标注的 StelarithActiveGroup（新导入器会写），
    # 老资源没有该字段时退化为「从第一个 ClassPlan 的 AssociatedGroup 反查」。
    group_id = str((envelope or {}).get("StelarithActiveGroup") or "").strip()
    if not group_id:
        for _pid, plan in plans.items():
            if isinstance(plan, dict) and plan.get("AssociatedGroup"):
                group_id = str(plan["AssociatedGroup"])
                break
    group_name = ""
    if group_id and group_id in groups and isinstance(groups[group_id], dict):
        group_name = str(groups[group_id].get("Name") or "")

    devices = (
        await db.execute(
            select(ClientProfile.client_id).where(ClientProfile.class_id == class_id)
        )
    ).scalars().all()
    if not devices:
        raise HTTPException(404, f"班级 {class_id} 下没有设备")

    payload = json.dumps(
        {
            "stelarith_task": {
                "action": "set_active_class",
                "scope": "class",
                "group_id": group_id,
                "group_name": group_name,
                "class_id": class_id,
            }
        },
        ensure_ascii=False,
    )

    inserted = 0
    for cid in devices:
        db.add(
            CommandQueueRecord(
                client_id=cid,
                command_type="stelarith_set_active_class",
                payload=payload,
                status="pending",
                ack_status="pending",
            )
        )
        inserted += 1
    await db.commit()
    return {
        "status": "success",
        "class_id": class_id,
        "group_id": group_id,
        "group_name": group_name,
        "class_plan_resource": cp_name,
        "devices": inserted,
        "message": f"已向 {inserted} 台设备下发切班指令（群：{group_name or group_id}）",
    }


@router.get("/groups")
async def list_class_plan_groups(db: AsyncSession = Depends(get_db)):
    """列出本租户所有**班级课表群**（供面板做切班选择器）。

    数据来源是各班 `cp_classNN` 资源里的 ClassPlanGroups 并集：
      - `class_id` / `group_id` / `group_name`：切班三要素
      - `plans`：该群下的课表张数（0 说明这个班还没真正导入课表）
      - `devices`：当前绑定到这个班的设备数
    只返回「班级自己派生出的群」（跳过默认群/全局群/档案自带的历史群），
    否则切班选择器会被几十个无意义的群淹没。
    """
    rows = (
        await db.execute(select(MODEL_MAP["ClassPlan"]))
    ).scalars().all()

    # 设备数按 class_id 聚合
    dev_rows = (
        await db.execute(
            select(ClientProfile.class_id, func.count(ClientProfile.client_id))
            .where(ClientProfile.class_id != "")
            .group_by(ClientProfile.class_id)
        )
    ).all()
    dev_count = {str(cid): int(n) for cid, n in dev_rows if cid}

    # 班级资源集：class_id ←→ cp 资源名
    crs_rows = (await db.execute(select(ClassResourceSet))).scalars().all()
    crs_by_cp = {}
    for crs in crs_rows:
        cp = getattr(crs, "class_plan", None)
        if cp:
            crs_by_cp[cp] = crs.resource_set_id

    # 班级名（有 classes 表就用它，没有就退回资源名）
    name_by_class = {}
    try:
        for cls in (await db.execute(select(Class))).scalars().all():
            name_by_class[cls.id] = cls.name or cls.id
    except Exception:
        pass

    out = []
    for row in rows:
        if not row.content or not str(row.name).startswith("cp_"):
            continue
        try:
            env = json.loads(row.content) if isinstance(row.content, str) else row.content
        except Exception:
            continue
        if not isinstance(env, dict):
            continue
        groups = env.get("ClassPlanGroups") or {}
        active = str(env.get("StelarithActiveGroup") or "").strip()
        # 统计每个群的课表数
        per_group: dict[str, int] = {}
        for _pid, plan in (env.get("ClassPlans") or {}).items():
            if isinstance(plan, dict):
                g = str(plan.get("AssociatedGroup") or "")
                if g:
                    per_group[g] = per_group.get(g, 0) + 1

        class_id = crs_by_cp.get(str(row.name), "")
        # 只保留「有课表的群」里最可能的班级群：优先 StelarithActiveGroup，否则课表最多的群
        if not active:
            if not per_group:
                continue
            active = max(per_group.items(), key=lambda kv: kv[1])[0]
        if active in (DEFAULT_GROUP_GUID, GLOBAL_GROUP_GUID):
            continue

        gmeta = groups.get(active) if isinstance(groups, dict) else None
        gname = str(gmeta.get("Name") or "") if isinstance(gmeta, dict) else ""
        out.append(
            {
                "class_id": class_id,
                "class_name": name_by_class.get(class_id, class_id),
                "class_plan_resource": str(row.name),
                "group_id": active,
                "group_name": gname,
                "plans": int(per_group.get(active, 0)),
                "devices": int(dev_count.get(class_id, 0)),
            }
        )
    out.sort(key=lambda x: (x["class_id"] or "zzzz"))
    return {"status": "success", "count": len(out), "groups": out}


# --------------------------------------------------------------------------- #
# 班级预览图（160x90 JPEG）—— 管理端查看 / 上传
#     ⚠️ 单段 GET /{class_id} 必须声明在最后，否则会吞掉 /groups、/list 等静态路由。
# --------------------------------------------------------------------------- #


@router.get("/preview-status")
async def class_preview_status(db: AsyncSession = Depends(get_db)):
    """批量返回各班预览图状态（有无 + 更新时间），供图形化卡片一次性渲染。

    刻意只回元数据、不回图片字节：卡片视图先拿到「哪个班有图」，
    再对可见卡片按需拉 `GET /{class_id}/preview`，避免一次性传输几十张图。
    """
    rows = (
        await db.execute(
            select(ClassPreview.class_id, ClassPreview.updated_at, ClassPreview.source_client_id)
        )
    ).all()
    return {
        "status": "success",
        "count": len(rows),
        "previews": [
            {
                "class_id": cid,
                "updated_at": upd.isoformat() if upd else None,
                "source_client_id": src or "",
            }
            for cid, upd, src in rows
        ],
    }


@router.get("/{class_id}/preview")
async def get_class_preview(class_id: str, db: AsyncSession = Depends(get_db)):
    """取班级预览图（image/jpeg）；不存在返回 404，前端据此显示占位。"""
    row = (
        await db.execute(select(ClassPreview).where(ClassPreview.class_id == class_id))
    ).scalar_one_or_none()
    if not row or not row.content:
        raise HTTPException(404, "该班级暂无预览图")
    return Response(
        content=row.content,
        media_type="image/jpeg",
        headers={
            # 预览图每 30s 覆盖一次：禁缓存，避免看到陈旧画面
            "Cache-Control": "no-store",
            "X-Preview-Width": str(row.width),
            "X-Preview-Height": str(row.height),
            "X-Preview-Updated-At": row.updated_at.isoformat() if row.updated_at else "",
        },
    )


@router.post("/{class_id}/preview")
async def upload_class_preview(
    class_id: str,
    request: Request,
    file: UploadFile = File(...),
    client_id: str = "",
    db: AsyncSession = Depends(get_db),
):
    """上传 / 覆盖班级预览图。

    **强校验：必须是 160x90 的 JPEG**（≤256KB）—— 各端缩放口径不一致时直接 422，
    避免卡片因分辨率不同而错位。宽高用纯 Python 解析 JPEG 头，**不引入 Pillow**。
    """
    cls = (await db.execute(select(Class).where(Class.id == class_id))).scalar_one_or_none()
    if not cls:
        raise HTTPException(404, f"班级 {class_id} 不存在")

    actor = getattr(request.state, "current_user_id", "") or ""
    owner = cls.owner_user_id or ""
    # 多用户隔离：带用户身份时，非审核人不能改别人的班（设备端上报无用户身份时放行）
    if actor and not await _is_reviewer(db, actor) and owner and owner != actor:
        raise HTTPException(403, "无权修改他人创建的班级预览图")

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "空文件")
    if len(raw) > PREVIEW_MAX_BYTES:
        raise HTTPException(413, f"预览图过大（{len(raw)} 字节），上限 {PREVIEW_MAX_BYTES}")
    size = _jpeg_size(raw)
    if size is None:
        raise HTTPException(422, "预览图必须是 JPEG 格式")
    w, h = size
    if (w, h) != (PREVIEW_W, PREVIEW_H):
        raise HTTPException(422, f"预览图尺寸必须是 {PREVIEW_W}x{PREVIEW_H}，收到 {w}x{h}")

    row = (
        await db.execute(select(ClassPreview).where(ClassPreview.class_id == class_id))
    ).scalar_one_or_none()
    if row is None:
        row = ClassPreview(class_id=class_id)
    row.content = raw
    row.width = w
    row.height = h
    row.source_client_id = client_id or row.source_client_id or ""
    row.updated_at = _now()
    db.add(row)
    _audit(
        db, class_id, actor, "preview_upload",
        {"client_id": client_id, "bytes": len(raw), "size": f"{w}x{h}"},
    )
    await db.commit()
    return {"status": "success", "class_id": class_id, "width": w, "height": h, "bytes": len(raw)    }


@router.get("/notice-replies")
async def list_notice_replies(
    request: Request,
    notice_id: str = "",
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
):
    """管理端拉取教室端经「互动弹窗」回传的回复回执。

    账户级（Bearer 鉴权，租户经令牌上下文识别）。可按 notice_id 过滤单条广播的回执，
    不传则返回本租户全部回执。回执箱用于「确认/回复」类互动广播的闭环——管理端能
    看到每台教室大屏对「收到没、回了啥」的真实反馈。

    表由设备端首条回复时惰性创建（见 client/status.py），此处同样确保存在以兼容
    「还没人回复就想看空箱」的场景。
    """
    from sqlalchemy import text as _text

    schema = safe_identifier(get_schema())
    await db.execute(
        _text(f'CREATE TABLE IF NOT EXISTS "{schema}".notice_replies ('
              f'id SERIAL PRIMARY KEY, client_id TEXT NOT NULL, '
              f'notice_id TEXT NOT NULL DEFAULT \'\', text TEXT NOT NULL, '
              f'created_at TIMESTAMPTZ NOT NULL DEFAULT now())')
    )
    lim = min(max(int(limit), 1), 500)
    nid = (notice_id or "").strip()
    rows = (
        await db.execute(
            _text(f'SELECT id, client_id, notice_id, text, created_at '
                  f'FROM "{schema}".notice_replies '
                  f"WHERE (:nid = '' OR notice_id = :nid) "
                  f"ORDER BY created_at DESC, id DESC LIMIT :lim"),
            {"nid": nid, "lim": lim},
        )
    ).mappings().all()
    return {
        "status": "success",
        "replies": [
            {
                "id": r["id"],
                "client_id": r["client_id"],
                "notice_id": r["notice_id"] or "",
                "text": r["text"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


@router.get("/{class_id}")
async def get_class_detail(
    class_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """班级详情（编号 / 属主 / 审核态 / 资源集）。

    非审核人只能看自己创建的班级或无属主的历史班级（多用户隔离）。
    """
    cls = (await db.execute(select(Class).where(Class.id == class_id))).scalar_one_or_none()
    if not cls:
        raise HTTPException(404, f"班级 {class_id} 不存在")
    actor = getattr(request.state, "current_user_id", "") or ""
    owner = cls.owner_user_id or ""
    if not await _is_reviewer(db, actor) and owner and owner != actor:
        raise HTTPException(403, "无权查看他人创建的班级")
    return {
        "status": "success",
        "class_id": cls.id,
        "name": cls.name,
        "code": cls.code,
        "graduation_year": cls.graduation_year,
        "class_number": cls.class_number,
        "resource_set_id": cls.resource_set_id,
        "owner_user_id": cls.owner_user_id,
        "review_status": cls.review_status,
        "reviewed_by": cls.reviewed_by,
        "reviewed_at": cls.reviewed_at.isoformat() if cls.reviewed_at else None,
        "reject_reason": cls.reject_reason,
        "created_at": cls.created_at.isoformat() if cls.created_at else None,
        "updated_at": cls.updated_at.isoformat() if cls.updated_at else None,
    }