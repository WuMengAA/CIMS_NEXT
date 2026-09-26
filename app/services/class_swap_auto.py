"""自助切班 · 到期自动回退巡检（lifespan 后台任务）。

每 30s 遍历所有租户，把「已执行且 effective_end_at 已到」且 auto_rollback=true
的互换申请自动换回（调用 class_swap_routes.auto_rollback_due）。

复用 _reconcile_tenant_schemas 的遍历模式：逐租户 set search_path 后处理，
处理完切回 public，避免污染连接池的会话上下文。
"""

import asyncio
import logging

from sqlalchemy import select, text

from app.models.session import AsyncSessionLocal
from app.models.class_swap import ClassSwapRequest

logger = logging.getLogger(__name__)

TICK_INTERVAL = 30


async def _tick() -> None:
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(text("slug")).select_from(text("accounts")))).scalars().all()
    except Exception as e:  # pragma: no cover
        logger.warning("读取账户列表失败，跳过自动回退巡检: %s", e)
        return

    for slug in rows:
        try:
            schema = f"tenant_{slug}"
            async with AsyncSessionLocal() as db:
                await db.execute(text(f'SET search_path TO "{schema}"'))
                from app.api.management.class_swap_routes import auto_rollback_due

                n = await auto_rollback_due(db)
                if n:
                    logger.info("[%s] 自动回退互换申请 %d 条", slug, n)
                await db.execute(text("SET search_path TO public"))
        except Exception as e:  # pragma: no cover
            logger.warning("[%s] 自动回退巡检失败: %s", slug, e)


async def run_swap_auto_rollback(stop_event: asyncio.Event) -> None:
    """主循环（lifespan 后台任务）。"""
    logger.info("自助切班到期自动回退巡检已启动（每 %ds）", TICK_INTERVAL)
    while not stop_event.is_set():
        try:
            await _tick()
        except Exception as e:  # pragma: no cover
            logger.warning("自助切班巡检异常: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TICK_INTERVAL)
        except asyncio.TimeoutError:
            continue
    logger.info("自助切班到期自动回退巡检已停止")
