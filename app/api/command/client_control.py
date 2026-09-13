"""客户端远程控制。

通过 gRPC 长连接向客户端下发实时控制指令。
按 NewAPI.md: POST /{client_id}/command/restart, POST /{client_id}/command/update-data
下发时同步写 command_queue 持久化队列，供离线教室端插件轮询取走。
"""

import json
import logging

from fastapi import APIRouter, Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.tenant.context import get_tenant_id
from app.api.schemas.base import StatusResponse
from app.models.database import get_db, CommandQueueRecord
from app.grpc.api.Protobuf.Server import ClientCommandDeliverScRsp_pb2
from app.grpc.api.Protobuf.Enum import Retcode_pb2, CommandTypes_pb2

router = APIRouter()
logger = logging.getLogger(__name__)


async def _push_cmd(
    request: Request, uid: str, cmd_type: int, db: AsyncSession = Depends(get_db)
) -> StatusResponse:
    """内部辅助：发送简单的无负载 gRPC 指令，并同步落库供轮询。"""
    # 1) 持久化落库
    type_name = _TYPE_NAMES.get(cmd_type, str(cmd_type))
    try:
        record = CommandQueueRecord(
            client_id=uid,
            command_type=type_name,
            payload=json.dumps({"type": type_name}, ensure_ascii=False),
            status="pending",
        )
        db.add(record)
        await db.commit()
    except Exception as e:
        logger.warning("[%s] 命令队列落库失败: %s", uid, e)
        await db.rollback()

    # 2) gRPC 实时推送
    servicer = getattr(request.app.state, "command_servicer", None)
    if not servicer:
        return StatusResponse(status="error", message="gRPC 服务不可用")

    cmd = ClientCommandDeliverScRsp_pb2.ClientCommandDeliverScRsp(
        RetCode=Retcode_pb2.Success, Type=cmd_type
    )
    await servicer.send_command(get_tenant_id(), uid, cmd)
    return StatusResponse(status="success", message="指令已下发")


_TYPE_NAMES = {
    CommandTypes_pb2.RestartApp: "RestartApp",
    CommandTypes_pb2.DataUpdated: "DataUpdated",
    CommandTypes_pb2.GetClientConfig: "GetClientConfig",
    CommandTypes_pb2.SendNotification: "SendNotification",
    CommandTypes_pb2.Ping: "Ping",
}


@router.post("/{client_id}/command/restart", response_model=StatusResponse)
async def restart_app(
    client_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """要求指定客户端重新启动应用。"""
    return await _push_cmd(request, client_id, CommandTypes_pb2.RestartApp, db)


@router.post("/{client_id}/command/update-data", response_model=StatusResponse)
async def force_sync(
    client_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """触发客户端立即拉取并刷新最新配置数据。"""
    return await _push_cmd(request, client_id, CommandTypes_pb2.DataUpdated, db)
