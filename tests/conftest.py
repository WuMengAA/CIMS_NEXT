"""共享测试夹具 - 初始化 PG、Redis 和默认测试账户。"""

import os
import sys
import asyncio
import warnings
from datetime import datetime, timezone
from pathlib import Path

# Windows 默认的 ProactorEventLoop 无法用于 psycopg 的异步模式，
# 会在夹具初始化阶段报 "Psycopg cannot use the 'ProactorEventLoop' to run in async mode"。
# 必须在任何事件循环被创建之前切成 SelectorEventLoop。
# 生产环境跑在 Linux 上，不受这一条影响。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 在导入任何依赖 imghdr 的模块之前抑制此弃用警告
warnings.filterwarnings(
    "ignore", message="imghdr was removed", category=DeprecationWarning
)

import pytest_asyncio  # noqa: E402

# 加载 .env 文件（确保测试使用真实配置）
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

# 仅在 .env 未提供时使用回退默认值
os.environ.setdefault("CIMS_KEY_FILE", "/tmp/test_cims_server.key")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:password@localhost:5432/cims",
)
os.environ.setdefault("REDIS_URL", "redis://:password@localhost:6379/0")
os.environ["CIMS_BASE_DOMAIN"] = "localhost"

from app.core.redis import init_redis  # noqa: E402
from app.models.database import init_db, AsyncSessionLocal, Account  # noqa: E402
from app.models.database import ensure_tenant_schema  # noqa: E402
from app.core.tenant import tenant_ctx, schema_ctx  # noqa: E402
from app.core.security.state import (  # noqa: E402
    get_ip_failures_dict,
    get_global_requests,
    set_cc_state,
)


def _reset_rate_limit_state():
    """清空进程内的限流状态。

    为什么每个测试都要做：限流中间件按「对端 IP」累计 60 秒窗口内的 ≥400 响应数，
    阈值 5 次即封禁。测试全部共用同一个 TestClient，对端 IP 恒为 "testclient"，
    而安全类测试**故意**制造大量 4xx —— 于是计数器一路涨到阈值，把 "testclient"
    打进封禁。此后所有测试（包括完全无关的登录、manifest）一律收到 429，
    表现为一整片「assert 429 == 200」的假失败，把真 bug 淹没掉。

    注意这与生产行为无关：生产里每个客户端有自己的 IP，不会互相连坐。
    这里清的是测试进程的内存态。
    """
    get_ip_failures_dict().clear()
    get_global_requests().clear()
    set_cc_state(False)

TEST_ACCOUNT_ID = "test-account-00000000"
TEST_ACCOUNT_SLUG = "test-school"
TEST_ACCOUNT_NAME = "Test School"
TEST_SCHEMA = f"tenant_{TEST_ACCOUNT_SLUG}"

# 向后兼容别名（供旧测试文件使用）
TEST_TENANT_ID = TEST_ACCOUNT_ID
TEST_TENANT_SLUG = TEST_ACCOUNT_SLUG
TEST_TENANT_NAME = TEST_ACCOUNT_NAME


@pytest_asyncio.fixture(autouse=True)
async def setup_infra():
    """初始化 PG 表、Redis、确保测试账户存在并设置 ContextVar。"""
    # 测试之间必须隔离限流状态，否则前一个测试的 4xx 会把 "testclient" 封掉，
    # 导致后续测试全部 429（详见 _reset_rate_limit_state 的说明）。
    _reset_rate_limit_state()
    await init_redis()
    await init_db()
    await ensure_tenant_schema(TEST_ACCOUNT_SLUG)
    # 确保测试账户存在
    async with AsyncSessionLocal() as db:
        from sqlalchemy import select

        result = await db.execute(select(Account).where(Account.id == TEST_ACCOUNT_ID))
        if result.scalar_one_or_none() is None:
            db.add(
                Account(
                    id=TEST_ACCOUNT_ID,
                    name=TEST_ACCOUNT_NAME,
                    slug=TEST_ACCOUNT_SLUG,
                    api_key="test-api-key",
                    is_active=True,
                    created_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()
    t_token = tenant_ctx.set(TEST_ACCOUNT_ID)
    s_token = schema_ctx.set(TEST_SCHEMA)
    yield TEST_ACCOUNT_ID
    schema_ctx.reset(s_token)
    tenant_ctx.reset(t_token)


@pytest_asyncio.fixture()
async def test_account(setup_infra):
    """兼容别名：返回测试账户 ID。"""
    return setup_infra


@pytest_asyncio.fixture()
async def admin_token():
    """生成用于测试的管理员会话令牌。"""
    from app.services.crypto.token_factory import create_session_token

    # 创建测试超管用户
    user_id = "test-superadmin-user"
    return await create_session_token(user_id)


@pytest_asyncio.fixture()
async def admin_headers(admin_token):
    """便捷夹具：管理端请求的认证头。"""
    return {"Authorization": f"Bearer {admin_token}"}


@pytest_asyncio.fixture()
async def test_superadmin_user():
    """确保测试超管用户存在并返回其信息。

    2026-09-26 补：require_permission 走「账户成员制」—— owner 角色在
    AccountMember 里才能拿到 {"*"} 全权限。此前只建 User 不建成员，
    权限补全后测试超管被 403（因为它不是任何账户的 owner）。
    这里同步建 AccountMember(owner)，与真实部署的「超管=账户 owner」一致。
    """
    from app.models.user import User
    from app.models.account_member import AccountMember
    from app.services.crypto.hasher import hash_password
    from sqlalchemy import select

    user_id = "test-superadmin-user"
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.id == user_id))
        if result.scalar_one_or_none() is None:
            db.add(
                User(
                    id=user_id,
                    username="test_superadmin",
                    email="admin@test.com",
                    hashed_password=hash_password("TestPassword123!"),
                    display_name="测试超管",
                    role_code="superadmin",
                    is_active=True,
                    created_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()
        # 账户成员（owner）：require_permission 以此判定 {"*"} 全权限
        member = (
            await db.execute(
                select(AccountMember).where(
                    AccountMember.user_id == user_id,
                    AccountMember.account_id == TEST_ACCOUNT_ID,
                )
            )
        ).scalar_one_or_none()
        if member is None:
            db.add(
                AccountMember(
                    user_id=user_id,
                    account_id=TEST_ACCOUNT_ID,
                    role_in_account="owner",
                    joined_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()
    return user_id


@pytest_asyncio.fixture()
async def command_headers():
    """生成旧式 command 令牌头（向后兼容）。"""
    from app.services.auth_token import generate_token

    token = await generate_token("command", tenant_id=TEST_ACCOUNT_ID)
    return {"Authorization": f"Bearer {token}"}
