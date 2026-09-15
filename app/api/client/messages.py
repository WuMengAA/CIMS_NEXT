"""教室端只读「消息中心」端点。

用途：让 ClassIsland 插件内的「消息中心」/ 托盘「最近消息」直接拉取本设备相关的
最近广播与通知历史（**不回写、不消费命令队列**，纯只读），从而在教室一体机上也能
看到「年级电教委员聊天/广播」的最近消息，而不必打开浏览器进网页面板。

与命令队列的区别（重要）：
- `/v1/client/{cid}/command/queued` 是**消费型**接口，取走即置 delivered；
- 本端点是**只读视图**，读 ack_status ∈ {pending, delivered, done, failed} 的历史行，
  绝不改动任何状态。这样插件可以在 UI 上反复轮询刷新而不影响命令语义。

鉴权与 manifest / command_poll 一致：由 TenantMiddleware 按 Host 头
`<slug>.<BASE_DOMAIN>` 识别租户，按 client_id 定向查询，无需额外会话凭证
（设备本就持有该租户的上报身份）。
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select

from app.models.database import get_db, CommandQueueRecord, ClientProfile
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()

_MAX_LIMIT = 100


@router.get("/v1/classisland/messages")
async def get_device_messages(
    request: Request,
    client_id: str = Query(..., description="教室一体机的客户端 UID，如 lab-pc-001"),
    limit: int = Query(30, ge=1, le=_MAX_LIMIT, description="返回条数上限"),
    db: AsyncSession = Depends(get_db),
):
    """返回该设备最近的集控消息（广播/通知类命令的历史视图）。

    返回结构（对齐插件侧可直读的小写 JSON）：
    {
      "client_id": "...",
      "class_id": "...",          # 该设备所属班级（未绑定为 ""）
      "class_name": "...",
      "count": N,
      "messages": [
        {"id":1, "type":"SendNotification", "title":"...", "content":"...",
         "scope":"...", "severity":"...", "at":"2026-09-14T22:38:00+00:00",
         "ack":"done"}
      ]
    }

    `title` / `content` 从 NotificationPayload JSON 里尽力提取（MessageMask /
    MessageContent）；若载荷是 stelarith_task（面板下发的原生指令），则跳过不展示，
    避免把内部指令当成用户可见消息。
    """
    slug = getattr(request.state, "tenant_slug", "Unknown")

    # 设备 → 班级（用于回填 class_name，便于插件显示「本机属于哪个班的设备」）
    profile = (
        await db.execute(
            select(ClientProfile).where(ClientProfile.client_id == client_id)
        )
    ).scalar_one_or_none()
    class_id = getattr(profile, "class_id", "") if profile else ""
    class_name = ""
    if class_id:
        from app.models.class_model import Class
        cls = (
            await db.execute(select(Class).where(Class.id == class_id))
        ).scalar_one_or_none()
        class_name = getattr(cls, "name", "") if cls else ""

    rows = (
        await db.execute(
            select(
                CommandQueueRecord.id,
                CommandQueueRecord.command_type,
                CommandQueueRecord.payload,
                CommandQueueRecord.ack_status,
                CommandQueueRecord.created_at,
            )
            .where(CommandQueueRecord.client_id == client_id)
            .where(CommandQueueRecord.command_type.in_(
                ["SendNotification", "notification", "broadcast"]
            ))
            .order_by(CommandQueueRecord.id.desc())
            .limit(limit)
        )
    ).all()

    messages = []
    for r in rows:
        parsed = _parse_notification(r.payload)
        if parsed is None:
            # stelarith_task 等内部指令不作为用户可见消息
            continue
        messages.append(
            {
                "id": r.id,
                "type": r.command_type,
                "title": parsed.get("title") or "星璃·集控",
                "content": parsed.get("content") or "",
                "scope": parsed.get("scope") or "",
                "severity": parsed.get("severity") or "",
                "at": r.created_at.isoformat() if r.created_at else None,
                "ack": r.ack_status or "pending",
            }
        )

    return {
        "client_id": client_id,
        "class_id": class_id,
        "class_name": class_name,
        "count": len(messages),
        "messages": messages,
    }


def _parse_notification(payload: str | None) -> dict | None:
    """从命令载荷中提取用户可见的通知文本；不是通知则返回 None。"""
    if not payload:
        return None
    import json

    try:
        doc = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None

    # 面板下发的原生指令（内含 stelarith_task）不是用户可见消息 → 跳过
    if "stelarith_task" in doc:
        return None

    title = doc.get("MessageMask") or doc.get("messageMask") or doc.get("title")
    content = doc.get("MessageContent") or doc.get("messageContent") or doc.get("content")
    if not content and not title:
        return None

    # MessageContent 可能内嵌 JSON（含 stelarith_task）→ 剥离为纯文本
    if isinstance(content, str) and content.strip().startswith("{"):
        try:
            nested = json.loads(content)
            if isinstance(nested, dict):
                if "stelarith_task" in nested:
                    return None
                content = nested.get("text") or nested.get("MessageContent") or ""
        except (ValueError, TypeError):
            pass

    if isinstance(content, str) and "stelarith_task" in content:
        return None

    return {
        "title": title if isinstance(title, str) else None,
        "content": content if isinstance(content, str) else str(content or ""),
        "scope": doc.get("Scope") or doc.get("scope"),
        "severity": doc.get("Severity") or doc.get("severity") or doc.get("Level"),
    }
