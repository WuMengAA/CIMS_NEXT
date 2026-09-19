"""教室端命令轮询自取 + 执行确认接口。

「插件轮询自取」方案的端点：
- GET  /v1/client/{client_id}/command/queued  取走本设备 pending 命令（置 delivered）
- POST /v1/client/{client_id}/command/ack     客户端执行完成后上报结果（置 done/failed）

消费语义（v2 修正，解决「离线丢命令」「命令被吞」）：
- pending   = 未被取走
- delivered = 已被 poller 取走，等待客户端执行确认
- done      = 客户端确认执行完成
- failed    = 客户端确认执行失败（可重试/人工介入）

旧 status 字段保留映射：pending→pending / delivered→pending / done→done / failed→done
（新逻辑一律以 ack_status 为准，status 仅作兼容视图。）

鉴权：与 manifest 一致——通过 TenantMiddleware 的 Host 头 `<slug>.<BASE_DOMAIN>`
识别租户，无需额外会话凭证；按 client_id(=uid) 定向查询本设备命令。
"""

from datetime import datetime, timezone
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.models.database import get_db, CommandQueueRecord

router = APIRouter()
logger = logging.getLogger(__name__)

_MAX_BATCH = 50


def _now():
    return datetime.now(timezone.utc)


class AckRequest(BaseModel):
    """客户端命令执行确认请求。"""

    command_ids: list[int]
    status: str = "done"  # done | failed，缺省 done


@router.get("/v1/client/{client_id}/command/queued")
async def poll_queued_commands(
    request: Request,
    client_id: str,
    db: AsyncSession = Depends(get_db),
):
    """返回该设备的 pending 命令，并立即标记为 delivered（原子取走，等待确认）。

    返回结构：
    {
      "client_id": "...",
      "count": N,
      "commands": [
        {"id": 1, "type": "SendNotification", "payload": "...", "created_at": "..."}
      ]
    }
    """
    slug = getattr(request.state, "tenant_slug", "Unknown")

    # 1) 取出该设备 pending 命令（限制批量）
    sel = (
        select(CommandQueueRecord.id, CommandQueueRecord.command_type,
               CommandQueueRecord.payload, CommandQueueRecord.created_at)
        .where(
            CommandQueueRecord.client_id == client_id,
            CommandQueueRecord.ack_status == "pending",
        )
        .order_by(CommandQueueRecord.id.asc())
        .limit(_MAX_BATCH)
    )
    rows = (await db.execute(sel)).all()

    if not rows:
        logger.info("[%s][client=%s] 命令轮询: 无待执行命令", slug, client_id)
        return {"client_id": client_id, "count": 0, "commands": []}

    ids = [r.id for r in rows]
    # 2) 原子标记为 delivered（取走，但等待客户端执行确认，防离线丢命令）
    await db.execute(
        update(CommandQueueRecord)
        .where(CommandQueueRecord.id.in_(ids))
        .values(
            ack_status="delivered",
            status="delivered",
            delivered_at=_now(),
        )
    )
    await db.commit()

    commands = [
        {
            "id": r.id,
            "type": r.command_type,
            "payload": r.payload,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    logger.info(
        "[%s][client=%s] 命令轮询: 取走 %d 条（delivered）",
        slug, client_id, len(commands),
    )
    return {"client_id": client_id, "count": len(commands), "commands": commands}


@router.get("/v1/client/{client_id}/command/stats")
async def queued_command_stats(
    request: Request,
    client_id: str,
    db: AsyncSession = Depends(get_db),
):
    """**只读**命令队列健康度：各状态计数 + 最老的 pending 时间。

    为什么必须单独开一个只读接口（而不是复用 /command/queued）：
      `/command/queued` 是**取走**语义 —— 读一次就把命令标记为 delivered（等客户端
      ack），原子生效。把它当成「看看有没有积压」的探针用，会**静默吃掉真实命令**：
      面板点了"检测"，教室里那条广播就再也不会播，且两边都不报错。
      诊断必须走只读路径。

    用途（判读方法）：
      · 持久 pending   = 设备根本没在轮询（插件没跑 / 设备离线）；
      · delivered 不 done = 插件取走了但执行后没 ack（执行报错 / ack 链路断）；
      · failed         = 明确执行失败，可人工介入。
    """
    slug = getattr(request.state, "tenant_slug", "Unknown")
    sel = (
        select(
            CommandQueueRecord.ack_status,
            func.count(CommandQueueRecord.id),
            func.min(CommandQueueRecord.created_at),
        )
        .where(CommandQueueRecord.client_id == client_id)
        .group_by(CommandQueueRecord.ack_status)
    )
    rows = (await db.execute(sel)).all()

    by_status: dict[str, int] = {}
    oldest_pending = None
    for status, count, oldest in rows:
        key = str(status or "unknown")
        by_status[key] = int(count or 0)
        if key == "pending" and oldest is not None:
            if oldest_pending is None or oldest < oldest_pending:
                oldest_pending = oldest

    logger.info(
        "[%s][client=%s] 队列只读统计: %s（不取走命令）", slug, client_id, by_status
    )
    return {
        "client_id": client_id,
        "total": sum(by_status.values()),
        "pending": by_status.get("pending", 0),
        "delivered": by_status.get("delivered", 0),
        "done": by_status.get("done", 0),
        "failed": by_status.get("failed", 0),
        "by_status": by_status,
        "oldest_pending_at": oldest_pending.isoformat() if oldest_pending else None,
    }


@router.post("/v1/client/{client_id}/command/ack")
async def ack_commands(
    request: Request,
    client_id: str,
    body: AckRequest = Body(...),
    db: AsyncSession = Depends(get_db),
):
    """客户端执行完成后上报结果。

    把指定 command_ids 置为 done 或 failed。failed 的命令可被管理端查看重试。
    """
    slug = getattr(request.state, "tenant_slug", "Unknown")
    if not body.command_ids:
        raise HTTPException(400, "command_ids 不能为空")
    if body.status not in ("done", "failed"):
        raise HTTPException(400, "status 仅允许 done / failed")

    rows = (
        await db.execute(
            select(CommandQueueRecord).where(
                CommandQueueRecord.client_id == client_id,
                CommandQueueRecord.id.in_(body.command_ids),
            )
        )
    ).scalars().all()
    if not rows:
        raise HTTPException(404, "没有匹配的命令")

    now = _now()
    for r in rows:
        r.ack_status = body.status
        r.status = "done" if body.status == "done" else "done"
        r.ack_at = now
    await db.commit()

    logger.info(
        "[%s][client=%s] 命令确认: %d 条 -> %s",
        slug, client_id, len(rows), body.status,
    )
    return {
        "client_id": client_id,
        "acked": len(rows),
        "status": body.status,
        "message": f"已确认 {len(rows)} 条命令为 {body.status}",
    }