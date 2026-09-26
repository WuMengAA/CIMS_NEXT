"""实时通知下发服务。

按 NewAPI.md: POST /{client_id}/command/send-notification
"""

import json
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.tenant.context import get_tenant_id
from app.api.schemas.base import StatusResponse
from app.core.auth.rbac import require_permission
from app.api.schemas.notification import NotificationPayload
from app.models.database import get_db, CommandQueueRecord
from app.grpc.api.Protobuf.Server import ClientCommandDeliverScRsp_pb2
from app.grpc.api.Protobuf.Command import SendNotification_pb2
from app.grpc.api.Protobuf.Enum import Retcode_pb2, CommandTypes_pb2

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/{client_id}/command/send-notification", response_model=StatusResponse,
             dependencies=[Depends(require_permission("command.execute"))])
async def push_notify(
    client_id: str,
    request: Request,
    payload: NotificationPayload = Body(...),
    db: AsyncSession = Depends(get_db),
):
    """下发格式化的桌面通知。

    除走 gRPC 长连接实时推送外，同时把通知写入 command_queue 持久化队列
    （pending 状态），供「未建立 gRPC 连接」的教室端插件通过 HTTP 轮询取走。
    这样即使设备不在线/未激活集控，命令也不会静默丢失。
    """
    servicer = getattr(request.app.state, "command_servicer", None)

    # 0) 设备动作限制（控制限制 · 关键逻辑在服务端）：
    #    若本次载荷内嵌 stelarith_task 且其 action 在该设备的限制名单里，直接拒绝下发
    #    （设备端根本不会收到被禁动作）。限制由操控端经设置端点按设备配置。
    restricted = await _check_action_restrictions(client_id, payload, db)
    if restricted:
        raise HTTPException(403, f"该设备的动作限制禁止执行: {restricted}")

    # 1) 持久化落库（插件轮询自取的数据源）
    try:
        record = CommandQueueRecord(
            client_id=client_id,
            command_type="SendNotification",
            payload=json.dumps(payload.model_dump(), ensure_ascii=False),
            status="pending",
        )
        db.add(record)
        await db.commit()
    except Exception as e:  # 落库失败不应阻断 gRPC 主链路
        logger.warning("[%s] 命令队列落库失败: %s", client_id, e)
        await db.rollback()

    # 2) gRPC 实时推送（在线设备即时生效）
    if not servicer:
        return StatusResponse(status="error", message="gRPC 通道未开启")

    # 只把官方 SendNotification 认识的字段传进 protobuf（2026-09-26 修复）：
    # schema 扩展字段（Kind/RequireAck/ReplyPresets/AutoDismissSeconds/NoticeId/Tts）
    # 是 v2.1 新增呈现选项，官方 protobuf 消息不认识 —— 直接
    # `SendNotification(**model_dump())` 会抛 ValueError 导致下发 500 熔断。
    _PROTO_FIELDS = (
        "MessageMask", "MessageContent", "OverlayIconLeft", "OverlayIconRight",
        "IsEmergency", "IsSpeechEnabled", "IsEffectEnabled", "IsSoundEnabled",
        "IsTopmost", "DurationSeconds", "RepeatCounts",
    )
    data = payload.model_dump()
    notify_kwargs = {k: data[k] for k in _PROTO_FIELDS if k in data}

    notify = SendNotification_pb2.SendNotification(**notify_kwargs)

    cmd = ClientCommandDeliverScRsp_pb2.ClientCommandDeliverScRsp(
        RetCode=Retcode_pb2.Success,
        Type=CommandTypes_pb2.SendNotification,
        Payload=notify.SerializeToString(),
    )

    # 租户 id 优先 request.state（/account 路径），回退 ContextVar（/command 路径）
    try:
        tid = request.state.tenant_id if hasattr(request.state, "tenant_id") else None
        if not tid:
            tid = get_tenant_id()
    except RuntimeError:
        tid = get_tenant_id()
    await servicer.send_command(tid, client_id, cmd)

    # 扩展字段不丢：持久化载荷已含全部字段（见上方落库），gRPC 通道按官方语义推送。
    if data.get("Kind"):
        logger.info("[%s] 通知扩展 kind=%s（gRPC 载荷经 protobuf 字段过滤）",
                    client_id, data["Kind"])
    return StatusResponse(status="success", message="通知已递送")


async def _check_action_restrictions(
    client_id: str,
    payload: NotificationPayload,
    db: AsyncSession,
) -> str:
    """若载荷含 stelarith_task 且其 action 被该设备限制，返回被禁的动作名；否则空串。"""
    try:
        import json as _json
        from sqlalchemy import select as _select
        from app.models.database import ClientProfile

        # 解析载荷里的 stelarith_task（MessageContent 内嵌 JSON）
        content = payload.MessageContent or payload.MessageMask or ""
        action = ""
        if content.strip().startswith("{"):
            try:
                doc = _json.loads(content)
                if isinstance(doc, dict):
                    tsk = doc.get("stelarith_task")
                    if isinstance(tsk, dict):
                        action = str(tsk.get("action") or "")
                    elif "action" in doc:  # 裸任务对象
                        action = str(doc.get("action") or "")
            except (ValueError, TypeError):
                pass
        if not action:
            return ""

        profile = (
            await db.execute(_select(ClientProfile).where(ClientProfile.client_id == client_id))
        ).scalar_one_or_none()
        if profile is None:
            return ""
        try:
            blocked = _json.loads(profile.action_restrictions or "[]")
        except Exception:
            return ""
        if isinstance(blocked, list) and any(str(b).strip() == action for b in blocked):
            return action
        return ""
    except Exception:
        # 校验失败宁可放行（fail-open）也不阻断正常下发 ——
        # 限制是管控增强，解析异常不应让全校通知一起挂掉。
        return ""

