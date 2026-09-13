"""教室端命令轮询自取接口。

「插件轮询自取」方案的关键端点：CIMS 把下发命令落库到 command_queue 表后，
教室端 ClassIsland 插件周期性地调用本接口，取走本设备的 pending 命令并执行。

鉴权：与 manifest 一致——通过 TenantMiddleware 的 Host 头 `<slug>.<BASE_DOMAIN>`
识别租户，无需额外会话凭证；按 client_id(=uid) 定向查询本设备命令。
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.models.database import get_db, CommandQueueRecord

router = APIRouter()
logger = logging.getLogger(__name__)

_MAX_BATCH = 50


@router.get("/v1/client/{client_id}/command/queued")
async def poll_queued_commands(
    request: Request,
    client_id: str,
    db: AsyncSession = Depends(get_db),
):
    """返回该设备的全部 pending 命令，并立即将它们标记为 done（原子取走）。

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

    # 1) 取出该设备全部 pending 命令（限制批量，防止积压撑爆单次响应）
    sel = (
        select(CommandQueueRecord.id, CommandQueueRecord.command_type,
               CommandQueueRecord.payload, CommandQueueRecord.created_at)
        .where(
            CommandQueueRecord.client_id == client_id,
            CommandQueueRecord.status == "pending",
        )
        .order_by(CommandQueueRecord.id.asc())
        .limit(_MAX_BATCH)
    )
    rows = (await db.execute(sel)).all()

    if not rows:
        logger.info("[%s][client=%s] 命令轮询: 无待执行命令", slug, client_id)
        return {
            "client_id": client_id,
            "count": 0,
            "commands": [],
        }

    ids = [r.id for r in rows]
    # 2) 原子地把这批标记为 done（取走即消费，防止重复执行）
    await db.execute(
        update(CommandQueueRecord)
        .where(CommandQueueRecord.id.in_(ids))
        .values(status="done")
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
        "[%s][client=%s] 命令轮询: 取走 %d 条已执行",
        slug, client_id, len(commands),
    )
    return {
        "client_id": client_id,
        "count": len(commands),
        "commands": commands,
    }
