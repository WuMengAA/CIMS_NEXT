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
    db: AsyncSession = Depends(get_db),
):
    """把一台设备划入班级（client_profiles.class_id = class_id）。"""
    cls = (
        await db.execute(select(Class).where(Class.id == class_id))
    ).scalar_one_or_none()
    if not cls:
        raise HTTPException(404, f"班级 {class_id} 不存在")

    prof = (
        await db.execute(select(ClientProfile).where(ClientProfile.client_id == client_id))
    ).scalar_one_or_none()
    if not prof:
        raise HTTPException(404, f"设备 {client_id} 的配置档案不存在")
    prof.class_id = class_id
    await db.commit()
    return {"status": "success", "client_id": client_id, "class_id": class_id}


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