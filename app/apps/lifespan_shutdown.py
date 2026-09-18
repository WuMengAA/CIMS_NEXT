"""全局应用生命周期 — 优雅停机。"""

import asyncio
import logging

from app.core.redis.pool import close_redis
from app.core.logging import get_port_logger, PORT_TAG_GRPC

logger = logging.getLogger(__name__)
grpc_logger = get_port_logger(PORT_TAG_GRPC)


async def _shutdown(app):
    """优雅停机：停止 gRPC 和 Redis。"""
    logger.info("正在执行优雅停机...")
    grpc_s = getattr(app.state, "grpc_server", None)
    if grpc_s:
        grpc_logger.info("正在停止 gRPC 服务器...")
        await grpc_s.stop(grace=5)
        grpc_logger.info("gRPC 服务器已停止")
    logger.info("正在关闭 Redis 连接池...")
    await close_redis()

    # 停止定时广播调度器
    stop_event = getattr(app.state, "scheduler_stop", None)
    task = getattr(app.state, "scheduler_task", None)
    if stop_event is not None:
        stop_event.set()
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
    logger.info("系统停机完成")
