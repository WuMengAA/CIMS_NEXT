"""自助切班（班次互换）路由 —— 普通用户免开发自行互换两班课表。

挂载：management router 的 `prefix="/class/swap"`，**必须在 /class 之前 include**
（否则 class_routes 的 `GET /class/{class_id}` 会把 `GET /class/swap` 当成查班级 "swap"）。

设计文档：`D:/Stelarith/_tasks/自助切班设计方案-2026-09-23.md`
关键决策（别改，除非设计重审）：
- 互换的是 **class_resource_sets.class_plan 绑定**（服务端持久，设备重启/新设备保持），
  已在线设备另走 `stelarith_set_active_class` 立即切显示（指令失败不阻断绑定层结果）。
- **8097 认不出"谁在问"**（经网站代理持特权令牌转发）→ 操作者身份由代理透传
  `X-CIMS-Actor-Id` / `X-CIMS-Actor-Role`，这里**只当审计元数据**，权限判断在代理层。
- 状态机：pending → approved → executed → rolled_back；pending → rejected | canceled；
  执行中先置 executing 防并发重入；任一执行步失败 → failed + 审计 class_swap_failed。
- 回退 = 按创建时快照换回，执行/回退前都校验「当前绑定仍等于快照」防漂移。
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import get_db
from app.models.class_model import Class, ClassAuditLog, ClassResourceSet
from app.models.class_swap import ClassSwapConfig, ClassSwapRequest
from app.models.client import ClientProfile
from app.models.command_queue import CommandQueueRecord
from app.api.command.model_map import MODEL_MAP
from app.api.management.class_routes import _ensure_class_tenant

router = APIRouter(dependencies=[Depends(_ensure_class_tenant)])

# 进行中状态（参与冲突/上限统计）
ACTIVE_STATUSES = ("pending", "approved", "executing", "executed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _actor(request: Request | None) -> tuple[str, str]:
    if request is None:
        return "", ""
    uid = (request.headers.get("x-cims-actor-id") or "").strip()
    role = (request.headers.get("x-cims-actor-role") or "").strip()
    return uid, role


async def _get_config(db: AsyncSession) -> ClassSwapConfig:
    row = (await db.execute(select(ClassSwapConfig))).scalar_one_or_none()
    if row is not None:
        return row
    return ClassSwapConfig(id=1, requires_approval=False, auto_rollback=True, max_pending_per_class=1)


async def _audit(db: AsyncSession, class_id: str, action: str, detail: dict, actor_id: str) -> None:
    db.add(
        ClassAuditLog(
            class_id=class_id,
            actor_user_id=actor_id or "system",
            action=action,
            detail=json.dumps(detail, ensure_ascii=False),
        )
    )


async def _queue_activate(db: AsyncSession, class_id: str, plan_name: str) -> dict:
    """按「目标课表资源名」解析群并向班内设备写切班指令（复用 activate 的解析口径）。

    返回 {group_id, group_name, devices}；班内无设备 / 资源缺失都不抛错 ——
    互换是绑定层语义，指令只是让在线设备立即切显示，失败由后续拉取自愈。
    """
    row = (
        await db.execute(
            select(MODEL_MAP["ClassPlan"]).where(MODEL_MAP["ClassPlan"].name == plan_name)
        )
    ).scalar_one_or_none()
    group_id, group_name = "", ""
    if row is not None and row.content:
        try:
            envelope = json.loads(row.content) if isinstance(row.content, str) else row.content
            plans = (envelope or {}).get("ClassPlans") or {}
            groups = (envelope or {}).get("ClassPlanGroups") or {}
            group_id = str((envelope or {}).get("StelarithActiveGroup") or "").strip()
            if not group_id:
                for _pid, plan in plans.items():
                    if isinstance(plan, dict) and plan.get("AssociatedGroup"):
                        group_id = str(plan["AssociatedGroup"])
                        break
            if group_id and group_id in groups and isinstance(groups[group_id], dict):
                group_name = str(groups[group_id].get("Name") or "")
        except Exception:
            pass  # 资源解析失败不阻断互换，指令留空让设备下次拉取自愈

    devices = (
        await db.execute(select(ClientProfile.client_id).where(ClientProfile.class_id == class_id))
    ).scalars().all()
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
    return {"group_id": group_id, "group_name": group_name, "devices": len(devices)}


async def _execute_swap(db: AsyncSession, row: ClassSwapRequest, actor_id: str, via: str) -> dict:
    """事务内执行互换：置 executing → 验快照 → 换绑定 → 下发指令 → executed + 审计。"""
    if row.status != "pending":
        raise HTTPException(409, f"申请状态为 {row.status}，不能执行")
    row.status = "executing"
    await db.flush()

    a = (
        await db.execute(
            select(ClassResourceSet).where(ClassResourceSet.resource_set_id == row.from_class_id)
        )
    ).scalar_one_or_none()
    b = (
        await db.execute(
            select(ClassResourceSet).where(ClassResourceSet.resource_set_id == row.to_class_id)
        )
    ).scalar_one_or_none()
    if a is None or b is None:
        raise HTTPException(404, "参与互换的班级存在资源集缺失")
    if a.class_plan != row.from_plan_before or b.class_plan != row.to_plan_before:
        raise HTTPException(409, "班级课表已被其他操作改动，与发起时快照不一致，请重新发起")

    if row.swap_type == "swap":
        new_a, new_b = row.to_plan_before, row.from_plan_before
    else:  # oneway：A 切到 B 创建时的方案，B 不动
        new_a, new_b = row.to_plan_before, b.class_plan

    a.class_plan, b.class_plan = new_a, new_b
    await db.flush()

    push_a = await _queue_activate(db, row.from_class_id, new_a)
    push_b = await _queue_activate(db, row.to_class_id, new_b) if row.swap_type == "swap" else {"devices": 0, "group_id": "", "group_name": ""}

    row.status = "executed"
    row.executed_at = _now()
    if via == "approve":
        row.approver_user_id = actor_id
        row.approved_at = row.executed_at

    detail = {
        "swap_id": row.id,
        "from_class_id": row.from_class_id,
        "to_class_id": row.to_class_id,
        "swap_type": row.swap_type,
        "from_plan_before": row.from_plan_before,
        "to_plan_before": row.to_plan_before,
        "from_plan_after": new_a,
        "to_plan_after": new_b,
        "via": via,
        "actor": actor_id,
    }
    await _audit(db, row.from_class_id, "class_swap", detail, actor_id)
    await _audit(db, row.to_class_id, "class_swap", detail, actor_id)
    await db.commit()
    return {
        "status": "executed",
        "swap_id": row.id,
        "from_plan_after": new_a,
        "to_plan_after": new_b,
        "devices": {"from": push_a["devices"], "to": push_b["devices"]},
        "message": f"已执行：{row.from_class_id} ↔ {row.to_class_id} 课表方案已对调"
        if row.swap_type == "swap"
        else f"已执行：{row.from_class_id} 已切到 {row.to_plan_before}",
    }


async def _rollback_swap(db: AsyncSession, row: ClassSwapRequest, actor_id: str) -> dict:
    """按创建时快照换回。仅 executed 可回退；绑定已漂移则拒绝并留审计。"""
    if row.status != "executed":
        raise HTTPException(409, f"申请状态为 {row.status}，不能回退")
    a = (
        await db.execute(
            select(ClassResourceSet).where(ClassResourceSet.resource_set_id == row.from_class_id)
        )
    ).scalar_one_or_none()
    b = (
        await db.execute(
            select(ClassResourceSet).where(ClassResourceSet.resource_set_id == row.to_class_id)
        )
    ).scalar_one_or_none()
    if a is None or b is None:
        raise HTTPException(404, "参与互换的班级存在资源集缺失")
    # 回退前置校验：A 当前必为执行后的方案（swap/oneway 执行后 A 均为 to_plan_before）
    if a.class_plan != row.to_plan_before:
        raise HTTPException(409, "当前绑定与执行时快照不一致，无法自动回退，请人工核对")
    if row.swap_type == "swap" and b.class_plan != row.from_plan_before:
        raise HTTPException(409, "当前绑定与执行时快照不一致，无法自动回退，请人工核对")

    a.class_plan = row.from_plan_before
    if row.swap_type == "swap":
        b.class_plan = row.to_plan_before
    row.status = "rolled_back"
    row.rolled_back_at = _now()
    await db.flush()

    push_a = await _queue_activate(db, row.from_class_id, row.from_plan_before)
    push_b = (
        await _queue_activate(db, row.to_class_id, row.to_plan_before)
        if row.swap_type == "swap"
        else {"devices": 0, "group_id": "", "group_name": ""}
    )
    detail = {
        "swap_id": row.id,
        "from_class_id": row.from_class_id,
        "to_class_id": row.to_class_id,
        "swap_type": row.swap_type,
        "from_plan_restored": row.from_plan_before,
        "to_plan_restored": row.to_plan_before,
        "actor": actor_id,
    }
    await _audit(db, row.from_class_id, "class_swap_rollback", detail, actor_id)
    if row.swap_type == "swap":
        await _audit(db, row.to_class_id, "class_swap_rollback", detail, actor_id)
    await db.commit()
    return {
        "status": "rolled_back",
        "swap_id": row.id,
        "devices": {"from": push_a["devices"], "to": push_b["devices"]},
        "message": f"已回退：{row.from_class_id} ↔ {row.to_class_id} 恢复原课表",
    }


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

@router.post("")
async def create_swap(payload: dict = Body(...), request: Request = None, db: AsyncSession = Depends(get_db)):
    """创建互换申请；免审批模式下直接执行。"""
    from_c = str(payload.get("from_class_id") or "").strip()
    to_c = str(payload.get("to_class_id") or "").strip()
    swap_type = str(payload.get("swap_type") or "swap").strip()
    reason = str(payload.get("reason") or "").strip()
    if swap_type not in ("swap", "oneway"):
        raise HTTPException(422, "swap_type 只能是 swap（互换）或 oneway（单切）")
    if not from_c or not to_c:
        raise HTTPException(422, "from_class_id 与 to_class_id 必填")
    if from_c == to_c:
        raise HTTPException(422, "不能与自身互换")

    actor_id, actor_role = _actor(request)
    clazz_a = (await db.execute(select(Class).where(Class.id == from_c))).scalar_one_or_none()
    clazz_b = (await db.execute(select(Class).where(Class.id == to_c))).scalar_one_or_none()
    if clazz_a is None or clazz_b is None:
        raise HTTPException(404, "参与互换的班级不存在")

    crs_a = (
        await db.execute(select(ClassResourceSet).where(ClassResourceSet.resource_set_id == from_c))
    ).scalar_one_or_none()
    crs_b = (
        await db.execute(select(ClassResourceSet).where(ClassResourceSet.resource_set_id == to_c))
    ).scalar_one_or_none()
    if crs_a is None or crs_b is None:
        raise HTTPException(404, "参与互换的班级没有绑定资源集")
    if crs_a.class_plan == crs_b.class_plan:
        raise HTTPException(422, "两个班级当前课表方案相同，无需互换")

    # 生效时段（可选；解析失败按无时段处理）
    eff_start = payload.get("effective_start_at") or None
    eff_end = payload.get("effective_end_at") or None
    try:
        eff_start = datetime.fromisoformat(eff_start.replace("Z", "+00:00")) if eff_start else None
        eff_end = datetime.fromisoformat(eff_end.replace("Z", "+00:00")) if eff_end else None
    except Exception:
        raise HTTPException(422, "effective_start_at / effective_end_at 格式须为 ISO8601")
    if eff_start and eff_end and eff_start >= eff_end:
        raise HTTPException(422, "生效开始时间必须早于结束时间")

    # 重叠冲突 + 每班进行中上限（永久申请视为全时段占用）
    cfg = await _get_config(db)
    overlap = await db.execute(
        text(
            """
            SELECT 1 FROM class_swap_requests
            WHERE status IN ('pending','approved','executing','executed')
              AND (from_class_id IN (:a,:b) OR to_class_id IN (:a,:b))
              AND COALESCE(effective_start_at, '-infinity'::timestamptz)
                  < COALESCE(CAST(:end AS timestamptz), 'infinity'::timestamptz)
              AND COALESCE(effective_end_at, 'infinity'::timestamptz)
                  > COALESCE(CAST(:start AS timestamptz), '-infinity'::timestamptz)
            LIMIT 1
            """
        ),
        {
            "a": from_c,
            "b": to_c,
            "start": eff_start.isoformat() if eff_start else "-infinity",
            "end": eff_end.isoformat() if eff_end else "infinity",
        },
    )
    if overlap.first():
        raise HTTPException(422, "所选班级在对应时段已有进行中的互换申请，请先处理旧申请")
    active_cnt = (
        await db.execute(
            select(ClassSwapRequest).where(
                ClassSwapRequest.status.in_(ACTIVE_STATUSES),
                (ClassSwapRequest.from_class_id.in_((from_c, to_c)))
                | (ClassSwapRequest.to_class_id.in_((from_c, to_c))),
            )
        )
    ).scalars().all()
    involved = set()
    for r in active_cnt:
        involved.add(r.from_class_id)
        involved.add(r.to_class_id)
    max_pending = max(1, cfg.max_pending_per_class)
    if any(sum(1 for r in active_cnt if r.from_class_id == c or r.to_class_id == c) >= max_pending for c in (from_c, to_c)):
        raise HTTPException(422, "班级已有达到上限的进行中互换申请")

    row = ClassSwapRequest(
        from_class_id=from_c,
        to_class_id=to_c,
        swap_type=swap_type,
        reason=reason,
        from_plan_before=crs_a.class_plan,
        to_plan_before=crs_b.class_plan,
        status="pending",
        effective_start_at=eff_start,
        effective_end_at=eff_end,
        initiator_user_id=actor_id,
        initiator_role=actor_role,
    )
    db.add(row)
    await db.flush()

    if not cfg.requires_approval:
        try:
            return await _execute_swap(db, row, actor_id, via="auto")
        except HTTPException:
            raise
        except Exception:
            await db.rollback()
            raise HTTPException(500, "互换执行失败，请稍后重试或联系管理员")

    await db.commit()
    return {
        "status": "pending",
        "swap_id": row.id,
        "message": "已提交，等待审批",
    }


@router.get("")
async def list_swaps(
    status: str = "",
    page: int = 1,
    size: int = 50,
    db: AsyncSession = Depends(get_db),
):
    page = max(1, page)
    size = min(200, max(1, size))
    base = select(ClassSwapRequest.id)
    if status:
        base = base.where(ClassSwapRequest.status == status)
    total = len((await db.execute(base)).scalars().all())
    stmt = select(ClassSwapRequest)
    if status:
        stmt = stmt.where(ClassSwapRequest.status == status)
    rows = (
        (await db.execute(stmt.order_by(ClassSwapRequest.created_at.desc()).offset((page - 1) * size).limit(size)))
        .scalars()
        .all()
    )
    return {
        "total": total,
        "page": page,
        "size": size,
        "items": [
            {
                "id": r.id,
                "from_class_id": r.from_class_id,
                "to_class_id": r.to_class_id,
                "swap_type": r.swap_type,
                "reason": r.reason,
                "status": r.status,
                "from_plan_before": r.from_plan_before,
                "to_plan_before": r.to_plan_before,
                "initiator_user_id": r.initiator_user_id,
                "initiator_role": r.initiator_role,
                "approver_user_id": r.approver_user_id,
                "rejected_reason": r.rejected_reason,
                "effective_start_at": r.effective_start_at.isoformat() if r.effective_start_at else None,
                "effective_end_at": r.effective_end_at.isoformat() if r.effective_end_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "executed_at": r.executed_at.isoformat() if r.executed_at else None,
                "rolled_back_at": r.rolled_back_at.isoformat() if r.rolled_back_at else None,
            }
            for r in rows
        ],
    }


# ⚠️ 路由顺序：/config 必须注册在 /{swap_id} 之前，否则 FastAPI 顺序匹配
# 会把 `GET /class/swap/config` 当成查 swap_id="config" → 404「申请不存在」。
# （2026-09-24 接力棒实测复现：修前 GET /class/swap/config 恒 404。）
@router.get("/config")
async def get_swap_config(db: AsyncSession = Depends(get_db)):
    cfg = await _get_config(db)
    return {
        "requires_approval": cfg.requires_approval,
        "auto_rollback": cfg.auto_rollback,
        "max_pending_per_class": cfg.max_pending_per_class,
        "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
    }


@router.post("/config")
async def update_swap_config(payload: dict = Body(...), request: Request = None, db: AsyncSession = Depends(get_db)):
    cfg = await _get_config(db)
    cfg.id = 1
    if "requires_approval" in payload:
        cfg.requires_approval = bool(payload.get("requires_approval"))
    if "auto_rollback" in payload:
        cfg.auto_rollback = bool(payload.get("auto_rollback"))
    if "max_pending_per_class" in payload:
        cfg.max_pending_per_class = max(1, int(payload.get("max_pending_per_class") or 1))
    actor_id, _ = _actor(request)
    cfg.updated_by = actor_id
    db.add(cfg)
    await db.commit()
    return {"ok": True, "requires_approval": cfg.requires_approval, "auto_rollback": cfg.auto_rollback}


@router.get("/{swap_id}")
async def swap_detail(swap_id: str, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(ClassSwapRequest).where(ClassSwapRequest.id == swap_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "申请不存在")
    return {
        "id": row.id,
        "from_class_id": row.from_class_id,
        "to_class_id": row.to_class_id,
        "swap_type": row.swap_type,
        "reason": row.reason,
        "status": row.status,
        "from_plan_before": row.from_plan_before,
        "to_plan_before": row.to_plan_before,
        "initiator_user_id": row.initiator_user_id,
        "initiator_role": row.initiator_role,
        "approver_user_id": row.approver_user_id,
        "rejected_reason": row.rejected_reason,
        "effective_start_at": row.effective_start_at.isoformat() if row.effective_start_at else None,
        "effective_end_at": row.effective_end_at.isoformat() if row.effective_end_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "executed_at": row.executed_at.isoformat() if row.executed_at else None,
        "rolled_back_at": row.rolled_back_at.isoformat() if row.rolled_back_at else None,
    }


@router.post("/{swap_id}/cancel")
async def cancel_swap(swap_id: str, request: Request = None, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(ClassSwapRequest).where(ClassSwapRequest.id == swap_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "申请不存在")
    if row.status != "pending":
        raise HTTPException(409, f"申请状态为 {row.status}，不能撤销")
    actor_id, _ = _actor(request)
    row.status = "canceled"
    await _audit(db, row.from_class_id, "class_swap_cancel", {"swap_id": row.id, "actor": actor_id}, actor_id)
    await db.commit()
    return {"status": "canceled", "swap_id": row.id}


@router.post("/{swap_id}/approve")
async def approve_swap(swap_id: str, request: Request = None, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(ClassSwapRequest).where(ClassSwapRequest.id == swap_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "申请不存在")
    actor_id, _ = _actor(request)
    try:
        return await _execute_swap(db, row, actor_id, via="approve")
    except HTTPException:
        await db.rollback()
        raise


@router.post("/{swap_id}/reject")
async def reject_swap(swap_id: str, payload: dict = Body(...), request: Request = None, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(ClassSwapRequest).where(ClassSwapRequest.id == swap_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "申请不存在")
    if row.status != "pending":
        raise HTTPException(409, f"申请状态为 {row.status}，不能驳回")
    actor_id, _ = _actor(request)
    row.status = "rejected"
    row.rejected_reason = str(payload.get("reason") or "").strip()
    await _audit(db, row.from_class_id, "class_swap_reject", {"swap_id": row.id, "reason": row.rejected_reason, "actor": actor_id}, actor_id)
    await db.commit()
    return {"status": "rejected", "swap_id": row.id}


@router.post("/{swap_id}/rollback")
async def rollback_swap(swap_id: str, request: Request = None, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(ClassSwapRequest).where(ClassSwapRequest.id == swap_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "申请不存在")
    actor_id, _ = _actor(request)
    try:
        return await _rollback_swap(db, row, actor_id)
    except HTTPException:
        await db.rollback()
        raise


# ---------------------------------------------------------------------------
# 到期自动回退（由 app/services/class_swap_auto.py 每 30s 调用，调用方负责租户 search_path）
# ---------------------------------------------------------------------------

async def auto_rollback_due(db: AsyncSession) -> int:
    """把「已执行且到生效结束时间」且 auto_rollback=true 的申请自动换回。返回处理条数。"""
    cfg = await _get_config(db)
    if not cfg.auto_rollback:
        return 0
    now = _now()
    due = (
        (await db.execute(
            select(ClassSwapRequest).where(
                ClassSwapRequest.status == "executed",
                ClassSwapRequest.effective_end_at.is_not(None),
                ClassSwapRequest.effective_end_at < now,
            )
        ))
        .scalars()
        .all()
    )
    done = 0
    for row in due:
        try:
            await _rollback_swap(db, row, actor_id="system")
            done += 1
        except HTTPException as e:
            await db.rollback()
            await _audit(db, row.from_class_id, "class_swap_rollback_failed", {"swap_id": row.id, "reason": e.detail}, "system")
            await db.commit()
        except Exception:
            await db.rollback()
    return done
