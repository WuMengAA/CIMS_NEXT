"""全局应用生命周期管理 — 启动阶段。

控制数据库初始化、Redis 连接池分配以及 gRPC 服务器启动。
"""

import logging
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import (
    validate_config,
    BASE_DOMAIN,
    CLIENT_PORT,
    MANAGEMENT_PORT,
    ADMIN_PORT,
    GRPC_PORT,
)
from app.core.redis.pool import init_redis
from app.models.database import (
    init_db,
    ensure_tenant_schema,
    AsyncSessionLocal,
    Account,
)
from app.grpc.server.bootstrap import serve_grpc
from app.core.logging import get_port_logger, PORT_TAG_GRPC
from app.apps.lifespan_shutdown import _shutdown

logger = logging.getLogger(__name__)
grpc_logger = get_port_logger(PORT_TAG_GRPC)


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """FastAPI 生命周期管理。"""
    validate_config()
    await _startup(app)
    yield
    await _shutdown(app)


async def _reconcile_tenant_schemas() -> None:
    """补建所有已有租户 Schema 中的缺失表。

    新增模型（如 command_queue）时，已存在的租户 Schema 不会自动建表；
    这里在启动时遍历 accounts（public 表），为每个活跃租户调 ensure_tenant_schema，
    幂等补建缺失表，避免运行期因缺表而 500。
    """
    from sqlalchemy import select

    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(Account.slug))).scalars().all()
    except Exception as e:  # pragma: no cover
        logger.warning("读取账户列表失败，跳过租户 Schema 补建: %s", e)
        return

    for slug in rows:
        try:
            await ensure_tenant_schema(slug)
        except Exception as e:  # pragma: no cover
            logger.warning("[schema=%s] 租户 Schema 补建失败: %s", slug, e)
    if rows:
        logger.info("已完成 %d 个租户 Schema 补齐", len(rows))


async def _startup(app: FastAPI):
    """启动所有后端服务。"""
    logger.info("正在初始化数据库连接...")
    await init_db()
    await _reconcile_tenant_schemas()
    logger.info("正在初始化 Redis 连接池...")
    await init_redis()

    # 把限流到底"认不认得真实客户端 IP"打出来。公网部署时这一行不确认，
    # 后面排"全网设备一起被封"的问题会非常难 —— 现象是集体 429。
    from app.core.client_ip import describe_trust_config

    logger.info(
        "客户端 IP 判定：可信代理 = %s（多租户基域 = %s）",
        describe_trust_config(),
        BASE_DOMAIN,
    )

    grpc_logger.info("正在启动 gRPC (%d)...", GRPC_PORT)
    grpc_s, cmd_s, sess_m = await serve_grpc()
    app.state.grpc_server = grpc_s
    app.state.command_servicer = cmd_s
    app.state.session_manager = sess_m
    logger.info(
        "就绪 — C:%d M:%d A:%d G:%d",
        CLIENT_PORT,
        MANAGEMENT_PORT,
        ADMIN_PORT,
        GRPC_PORT,
    )

    # 启动定时广播调度器（后台常驻任务）
    from app.services.scheduled_broadcast import run_scheduler

    stop_event = asyncio.Event()
    app.state.scheduler_stop = stop_event
    app.state.scheduler_task = asyncio.create_task(run_scheduler(stop_event))
    logger.info("定时广播调度器任务已创建")

    # 自助切班 · 到期自动回退巡检（复用同一个 stop_event，_shutdown 一并停）
    from app.services.class_swap_auto import run_swap_auto_rollback

    app.state.swap_rollback_task = asyncio.create_task(run_swap_auto_rollback(stop_event))
    logger.info("自助切班到期自动回退巡检任务已创建")
