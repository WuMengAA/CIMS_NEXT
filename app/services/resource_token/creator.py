"""资源令牌发放逻辑。

生成受限的、短效的、且可选绑定 IP 地址的资源访问令牌。
"""

import secrets
from app.core.redis.accessor import get_redis
from app.core.tenant.context import get_schema

_TOKEN_LENGTH = 64
_DEFAULT_TTL = 300
_DEFAULT_MAX_USES = 1


async def create_token(
    tenant_id: str,
    resource_type: str,
    name: str,
    *,
    ttl: int = _DEFAULT_TTL,
    max_uses: int = _DEFAULT_MAX_USES,
    client_ip: str = "",
) -> str:
    """存储资源访问令牌及关联的元数据。"""
    rd = get_redis()
    token = secrets.token_urlsafe(_TOKEN_LENGTH)
    key = f"token:{token}"

    await rd.hset(
        key,
        mapping={
            "tenant_id": tenant_id,
            "resource_type": resource_type,
            "name": name,
            "remaining_uses": str(max_uses),
            "client_ip": client_ip,
            # 记录生成时的租户 schema：/get 按此读取，避免落库与读取 schema 错位
            "schema": get_schema(),
        },
    )
    await rd.expire(key, ttl)
    return token
