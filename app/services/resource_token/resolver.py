"""资源令牌解析及使用情况追踪。

解码令牌并处理剩余使用次数配额的递减。
"""

from typing import Optional, Tuple
from app.core.redis.accessor import get_redis


async def resolve_token(token: str) -> Optional[Tuple[str, str, str, str, str]]:
    """将令牌字符串转换为元数据，并递减剩余可用次数。"""
    rd = get_redis()
    key = f"token:{token}"
    data = await rd.hgetall(key)

    if not data:
        return None

    remaining = int(data["remaining_uses"]) - 1
    if remaining <= 0:
        await rd.delete(key)
    else:
        await rd.hset(key, "remaining_uses", str(remaining))

    return (
        data["tenant_id"],
        data["resource_type"],
        data["name"],
        data.get("client_ip", ""),
        data.get("schema", "public"),
    )
