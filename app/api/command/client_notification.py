"""实时通知下发服务。

按 NewAPI.md: POST /{client_id}/command/send-notification
"""

import json
import logging

from fastapi import APIRouter, Request, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.tenant.context import get_tenant_id
from app.api.schemas.base import StatusResponse
from app.api.schemas.notification import NotificationPayload
from app.models.database import get_db, CommandQueueRecord
from app.grpc.api.Protobuf.Server import ClientCommandDeliverScRsp_pb2
from app.grpc.api.Protobuf.Command import SendNotification_pb2
from app.grpc.api.Protobuf.Enum import Retcode_pb2, CommandTypes_pb2

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/{client_id}/command/send-notification", response_model=StatusResponse)
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

    notify = SendNotification_pb2.SendNotification(**payload.model_dump())

    cmd = ClientCommandDeliverScRsp_pb2.ClientCommandDeliverScRsp(
        RetCode=Retcode_pb2.Success,
        Type=CommandTypes_pb2.SendNotification,
        Payload=notify.SerializeToString(),
    )

    await servicer.send_command(get_tenant_id(), client_id, cmd)
    return StatusResponse(status="success", message="通知已递送")

