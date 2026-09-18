"""定时广播后台调度器。

与请求处理解耦：作为 lifespan 后台任务常驻，每 ~30s 遍历所有活跃租户，
对到期（next_run_at <= now）的启用配置触发一次广播（写 command_queue，
复用现有命令通道），随后重算下一次触发时间（recurring）或置为失效（once）。

多租户：本任务不在请求上下文里，get_tenant_id() 无效，因此逐租户手动
set_search_path 到 tenant_<slug> 再查询本租户 scheduled_broadcasts 表。
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from app.core.tenant.context import set_search_path
from app.models.database import AsyncSessionLocal, Account
from app.models.scheduled_broadcast import ScheduledBroadcast
from app.models.client import ClientProfile
from app.models.command_queue import CommandQueueRecord
from app.api.schemas.notification import NotificationPayload

logger = logging.getLogger(__name__)

TICK_INTERVAL = 30  # 秒


def compute_next_run(sch: ScheduledBroadcast, base: datetime | None = None) -> datetime | None:
    """计算下一次触发时间（UTC）。once 直接返回锚定时间；daily/weekly 按 time 分量递推。"""
    base = base or datetime.now(timezone.utc)
    if sch.schedule_type == "once":
        return sch.run_at
    if sch.run_at is None:
        return None
    t = sch.run_at.time().replace(tzinfo=timezone.utc)
    candidate = datetime.combine(base.date(), t)
    if sch.schedule_type == "daily":
        if candidate <= base:
            candidate += timedelta(days=1)
        return candidate
    # weekly
    wd = int(sch.weekday if sch.weekday is not None else 0) % 7
    days_ahead = (wd - candidate.weekday()) % 7
    if days_ahead == 0 and candidate <= base:
        days_ahead = 7
    return candidate + timedelta(days=days_ahead)


async def _fire_schedule(sch: ScheduledBroadcast, db, now: datetime) -> int:
    """把一条到期配置展开成命令并写 command_queue，返回触达设备数。"""
    if sch.target_class_id:
        devs = (
            await db.execute(
                select(ClientProfile.client_id).where(
                    ClientProfile.class_id == sch.target_class_id
                )
            )
        ).scalars().all()
    else:
        devs = (await db.execute(select(ClientProfile.client_id))).scalars().all()

    payload = NotificationPayload(
        MessageMask=sch.title or "",
        MessageContent=sch.content or "",
        IsTopmost=True,
        IsSpeechEnabled=False,
        DurationSeconds=8.0,
        RepeatCounts=1,
    )
    inserted = 0
    for cid in devs:
        db.add(
            CommandQueueRecord(
                client_id=cid,
                command_type="SendNotification",
                payload=payload.model_dump_json(),
                status="pending",
                ack_status="pending",
            )
        )
        inserted += 1
    sch.last_run_at = now
    if sch.schedule_type == "once":
        sch.enabled = False
        sch.next_run_at = None
    else:
        sch.next_run_at = compute_next_run(sch, now)
    await db.commit()
    return inserted


async def _tick() -> None:
    now = datetime.now(timezone.utc)
    try:
        async with AsyncSessionLocal() as s:
            slugs = (
                await s.execute(select(Account.slug).where(Account.is_active == True))  # noqa: E712
            ).scalars().all()
    except Exception as e:  # pragma: no cover
        logger.warning("定时广播：读取账户列表失败: %s", e)
        return

    for slug in slugs:
        schema = f"tenant_{slug}"
        try:
            async with AsyncSessionLocal() as db:
                await set_search_path(db, schema)
                due = (
                    await db.execute(
                        select(ScheduledBroadcast)
                        .where(ScheduledBroadcast.enabled == True)  # noqa: E712
                        .where(ScheduledBroadcast.next_run_at != None)  # noqa: E711
                        .where(ScheduledBroadcast.next_run_at <= now)
                    )
                ).scalars().all()
                for sch in due:
                    try:
                        n = await _fire_schedule(sch, db, now)
                        logger.info(
                            "[%s] 定时广播触发「%s」目标=%s 触达=%d",
                            slug, sch.name, sch.target_class_id or "全校", n,
                        )
                    except Exception as e:
                        logger.warning("[%s] 定时广播「%s」触发失败: %s", slug, sch.name, e)
                        await db.rollback()
        except Exception as e:  # pragma: no cover
            logger.warning("[%s] 定时广播租户处理失败: %s", slug, e)


async def run_scheduler(stop_event: asyncio.Event) -> None:
    """调度器主循环（lifespan 后台任务）。"""
    logger.info("定时广播调度器已启动（每 %ds 巡检）", TICK_INTERVAL)
    while not stop_event.is_set():
        try:
            await _tick()
        except Exception as e:  # pragma: no cover
            logger.warning("定时广播调度器异常: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TICK_INTERVAL)
        except asyncio.TimeoutError:
            continue
    logger.info("定时广播调度器已停止")
