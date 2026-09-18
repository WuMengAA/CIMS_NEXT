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
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant.context import get_tenant_id
from app.models.session import get_db
from app.models.class_model import Class, ClassResourceSet
from app.models.client import ClientProfile
from app.models.command_queue import CommandQueueRecord
from app.api.command.model_map import MODEL_MAP
from app.api.command.payload_validator import validate_payload
from app.api.command.version_check import check_version
from app.api.command.timetable_validator import validate_classplan_references
from app.services.schedule_importer import DEFAULT_GROUP_GUID, GLOBAL_GROUP_GUID

router = APIRouter()

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


@router.post("/create")
async def create_class(
    class_id: str,
    name: str,
    db: AsyncSession = Depends(get_db),
):
    """创建班级并绑定一份默认资源集。

    class_id 建议形如 class_3p1（人可读）。同一租户内唯一。
    """
    tid = get_tenant_id()
    if not tid:
        raise HTTPException(400, "租户上下文缺失")
    exists = (
        await db.execute(select(Class).where(Class.id == class_id))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(409, f"班级 {class_id} 已存在")

    rs = ClassResourceSet(resource_set_id=class_id, updated_at=_now(), **DEFAULT_RESOURCE)
    cls = Class(id=class_id, name=name, resource_set_id=class_id, created_at=_now(), updated_at=_now())
    db.add(rs)
    db.add(cls)
    await db.commit()
    return {
        "status": "success",
        "class_id": class_id,
        "resource_set_id": class_id,
        "message": f"班级 {name} 已创建",
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
                "active_class_group": st.active_class_group if st else "",
                "modules": _load(st.modules_json if st else None, {}),
                "plugins": _load(st.plugins_json if st else None, []),
                "extra": _load(st.extra_json if st else None, {}),
            }
        )

    return {
        "fresh_seconds": FRESH_SECONDS,
        "count": len(devices),
        "online_count": sum(1 for d in devices if d["online"]),
        "devices": devices,
        # 可选班级清单（指派下拉用）：与 classes 同源，但只暴露 class_id/name。
        "suggest": [
            {"class_id": c.id, "name": c.name}
            for c in classes
        ],
        "classes": [
            {
                "class_id": c.id,
                "name": c.name,
                "devices": sorted(p.client_id for p in profs if p.class_id == c.id),
            }
            for c in classes
        ],
    }


@router.get("/list")
async def list_classes(db: AsyncSession = Depends(get_db)):
    """列出全部班级（含设备数）。"""
    rows = (await db.execute(select(Class).order_by(Class.sort_order, Class.name))).scalars().all()
    out = []
    for cls in rows:
        dev_count = (
            await db.execute(
                select(func.count()).select_from(ClientProfile).where(ClientProfile.class_id == cls.id)
            )
        ).scalar() or 0
        out.append(
            {
                "class_id": cls.id,
                "name": cls.name,
                "resource_set_id": cls.resource_set_id,
                "sort_order": cls.sort_order,
                "device_count": dev_count,
                "updated_at": str(cls.updated_at),
            }
        )
    return out


@router.post("/device/assign")
async def assign_device_to_class(
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

    prof = (
        await db.execute(select(ClientProfile).where(ClientProfile.client_id == client_id))
    ).scalar_one_or_none()
    # 自动建档：设备可能还没上报过心跳（刚装机、或想提前预绑定），此前这里直接
    # 404「配置档案不存在」，导致面板上看得见设备却绑不了班。改为按需建空档
    # （class_id 随后由下面统一赋值），使「先指派、后装机」也能生效——装好一启动
    # 就直接拿到本班课表，不必等现场再补一次操作。
    created_profile = False
    if not prof:
        prof = ClientProfile(client_id=client_id)
        db.add(prof)
        created_profile = True

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
    client_id: str,
    db: AsyncSession = Depends(get_db),
):
    """把设备移出班级（回退设备级配置）。"""
    prof = (
        await db.execute(select(ClientProfile).where(ClientProfile.client_id == client_id))
    ).scalar_one_or_none()
    if not prof:
        raise HTTPException(404, f"设备 {client_id} 的配置档案不存在")
    prof.class_id = ""
    await db.commit()
    return {"status": "success", "client_id": client_id, "class_id": ""}


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