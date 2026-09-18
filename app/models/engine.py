"""SQLAlchemy 引擎与会话工厂配置。

配置异步 PostgreSQL 引擎，并提供表初始化辅助函数。
"""

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from app.core.config import DATABASE_URL
from .base import Base

# 引擎配置（基于 asyncpg/psycopg）
engine = create_async_engine(DATABASE_URL, echo=False, pool_size=10, max_overflow=20)

# 异步会话工厂
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """初始化 public Schema 中的**全局**表。

    只创建账户/用户/权限等全局表（_PUBLIC_ONLY）；租户业务表
    （client_status、classes、command_queue …）只存在于各 tenant_<slug> Schema，
    由 ensure_tenant_schema 在启动时按账户逐一创建。

    绝不在 public 创建租户业务表 —— 否则：
      1) 漏设 search_path 的请求会静默误写 public 的空壳表（P0-② 要堵死的地雷）；
      2) 每次重启都会与 _legacy_ 改名后残留的同名索引冲突，导致启动崩溃
         （DuplicateTable: ix_client_status_class_id 已存在）。
    """
    # 延迟导入避免与 schema_init 的循环依赖（schema_init 已 import 本模块 engine）
    from app.models.schema_init import _PUBLIC_ONLY

    public_tables = [t for t in Base.metadata.sorted_tables if t.name in _PUBLIC_ONLY]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=public_tables)
        )
