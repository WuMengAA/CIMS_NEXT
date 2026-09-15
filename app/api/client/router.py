"""客户端 API 聚合路由。

将清单发现和资源访问端点聚合到客户端 API 应用的主路由中。
"""

from fastapi import APIRouter
from .manifest import router as manifest_router
from .resource import router as resource_router
from .command_poll import router as command_poll_router
from .messages import router as messages_router

router = APIRouter()

router.include_router(manifest_router)
router.include_router(resource_router)
router.include_router(command_poll_router)
# 只读「消息中心」视图：教室端插件拉取最近广播/通知历史（不同于消费型 command/queued）
router.include_router(messages_router)
