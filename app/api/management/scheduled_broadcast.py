"""定时广播管理接口（管理端 8097，需在租户上下文内调用）。

提供配置的增删改查 + 手动立即触发。调度触发本身由后台调度器
（app.services.scheduled_broadcast）完成，本模块只负责配置 CRUD。
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from app.core.auth.rbac import require_permission

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DEFAULT_ACCOUNT_SLUG
from app.core.tenant.context import schema_ctx, set_search_path
from app.models.database import get_db
from app.models.scheduled_broadcast import ScheduledBroadcast
from app.models.class_model import Class
from app.services.scheduled_broadcast import compute_next_run, _fire_schedule

router = APIRouter()


async def _ensure_tenant(db: AsyncSession) -> str:
    """确定性地把会话 search_path 钉到目标租户 Schema。

    背景：8097（management）只在 `/accounts/{id}/...` 路径下由
    AccountContextMiddleware 设置租户上下文；面板经网站代理转发的是
    `/scheduled-broadcast/...`（无账户前缀），此时 schema_ctx 为默认 "public"。
    若不显式设置，会话会沿用连接池里上一条 client(8096) 请求残留的
    search_path（"连接池碰运气"）——同一接口可能落到不同租户，绝不可接受。

    因此：优先用已显式设置的租户 Schema；否则回退到 DEFAULT_ACCOUNT_SLUG
    （面板默认目标租户，与 .env 一致），保证读写恒定落在同一租户。
    """
    schema = schema_ctx.get()
    if not schema or schema == "public":
        schema = f"tenant_{DEFAULT_ACCOUNT_SLUG}"
    await set_search_path(db, schema)
    return schema


def _parse_dt(value: str | None) -> datetime | None:
    """把面板传来的 ISO 时间解析成 tz-aware UTC；空/非法返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_dict(sch: ScheduledBroadcast) -> dict:
    return {
        "id": sch.id,
        "name": sch.name,
        "schedule_type": sch.schedule_type,
        "run_at": sch.run_at.isoformat() if sch.run_at else None,
        "weekday": sch.weekday,
        "title": sch.title,
        "content": sch.content,
        "target_class_id": sch.target_class_id or "",
        "enabled": bool(sch.enabled),
        "last_run_at": sch.last_run_at.isoformat() if sch.last_run_at else None,
        "next_run_at": sch.next_run_at.isoformat() if sch.next_run_at else None,
        "created_at": sch.created_at.isoformat() if sch.created_at else None,
    }


@router.get("/list")
async def list_schedules(db: AsyncSession = Depends(get_db)):
    """列出本租户全部定时广播配置。"""
    await _ensure_tenant(db)
    rows = (
        await db.execute(select(ScheduledBroadcast).order_by(ScheduledBroadcast.id.desc()))
    ).scalars().all()
    return {"status": "success", "count": len(rows), "items": [_to_dict(r) for r in rows]}


@router.post("/create", dependencies=[Depends(require_permission("command.execute"))])
async def create_schedule(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
):
    """新建一条定时广播配置。

    body: {name, schedule_type('once'|'daily'|'weekly'), run_at(ISO),
           weekday(0-6, weekly 用), title, content, target_class_id(''|class_id), enabled}
    """
    await _ensure_tenant(db)

    stype = payload.get("schedule_type", "once")
    if stype not in ("once", "daily", "weekly"):
        raise HTTPException(400, "schedule_type 必须为 once/daily/weekly")

    run_at = _parse_dt(payload.get("run_at"))
    if run_at is None:
        raise HTTPException(400, "run_at 缺失或格式非法（需 ISO8601）")

    target = (payload.get("target_class_id") or "").strip()
    if target:
        cls = (
            await db.execute(select(Class).where(Class.id == target))
        ).scalar_one_or_none()
        if not cls:
            raise HTTPException(404, f"目标班级 {target} 不存在")

    sch = ScheduledBroadcast(
        name=payload.get("name", "") or "",
        schedule_type=stype,
        run_at=run_at,
        weekday=payload.get("weekday"),
        title=payload.get("title", "") or "",
        content=payload.get("content", "") or "",
        target_class_id=target,
        enabled=bool(payload.get("enabled", True)),
    )
    sch.next_run_at = compute_next_run(sch)
    db.add(sch)
    await db.commit()
    await db.refresh(sch)
    return {"status": "success", "item": _to_dict(sch)}


@router.put("/{sid}", dependencies=[Depends(require_permission("command.execute"))])
async def update_schedule(
    sid: int,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
):
    """更新一条配置（字段可部分覆盖）。"""
    await _ensure_tenant(db)
    sch = (
        await db.execute(select(ScheduledBroadcast).where(ScheduledBroadcast.id == sid))
    ).scalar_one_or_none()
    if not sch:
        raise HTTPException(404, f"定时广播 {sid} 不存在")

    if "name" in payload:
        sch.name = payload["name"]
    if "schedule_type" in payload:
        st = payload["schedule_type"]
        if st not in ("once", "daily", "weekly"):
            raise HTTPException(400, "schedule_type 必须为 once/daily/weekly")
        sch.schedule_type = st
    if "run_at" in payload:
        run_at = _parse_dt(payload["run_at"])
        if run_at is None:
            raise HTTPException(400, "run_at 格式非法")
        sch.run_at = run_at
    if "weekday" in payload:
        sch.weekday = payload["weekday"]
    if "title" in payload:
        sch.title = payload["title"]
    if "content" in payload:
        sch.content = payload["content"]
    if "target_class_id" in payload:
        target = (payload["target_class_id"] or "").strip()
        if target:
            cls = (
                await db.execute(select(Class).where(Class.id == target))
            ).scalar_one_or_none()
            if not cls:
                raise HTTPException(404, f"目标班级 {target} 不存在")
        sch.target_class_id = target

    sch.next_run_at = compute_next_run(sch)
    await db.commit()
    await db.refresh(sch)
    return {"status": "success", "item": _to_dict(sch)}


@router.post("/{sid}/toggle", dependencies=[Depends(require_permission("command.execute"))])
async def toggle_schedule(
    sid: int,
    enabled: bool = Body(default=True, embed=True),
    db: AsyncSession = Depends(get_db),
):
    """启用/停用一条配置（停用后不再被调度器触发）。"""
    await _ensure_tenant(db)
    sch = (
        await db.execute(select(ScheduledBroadcast).where(ScheduledBroadcast.id == sid))
    ).scalar_one_or_none()
    if not sch:
        raise HTTPException(404, f"定时广播 {sid} 不存在")
    sch.enabled = bool(enabled)
    if enabled:
        sch.next_run_at = compute_next_run(sch)
    await db.commit()
    return {"status": "success", "enabled": sch.enabled, "next_run_at": sch.next_run_at.isoformat() if sch.next_run_at else None}


@router.post("/{sid}/fire-now", dependencies=[Depends(require_permission("command.execute"))])
async def fire_now(sid: int, db: AsyncSession = Depends(get_db)):
    """手动立即触发一次（便于测试，不消耗 recurring 周期语义：once 同正常触发）。"""
    await _ensure_tenant(db)
    sch = (
        await db.execute(select(ScheduledBroadcast).where(ScheduledBroadcast.id == sid))
    ).scalar_one_or_none()
    if not sch:
        raise HTTPException(404, f"定时广播 {sid} 不存在")
    now = datetime.now(timezone.utc)
    n = await _fire_schedule(sch, db, now)
    return {
        "status": "success",
        "delivered": n,
        "target": sch.target_class_id or "全校",
        "next_run_at": sch.next_run_at.isoformat() if sch.next_run_at else None,
    }


@router.delete("/{sid}", dependencies=[Depends(require_permission("command.execute"))])
async def delete_schedule(sid: int, db: AsyncSession = Depends(get_db)):
    """删除一条配置。"""
    await _ensure_tenant(db)
    sch = (
        await db.execute(select(ScheduledBroadcast).where(ScheduledBroadcast.id == sid))
    ).scalar_one_or_none()
    if not sch:
        raise HTTPException(404, f"定时广播 {sid} 不存在")
    await db.delete(sch)
    await db.commit()
    return {"status": "success", "deleted": sid}
