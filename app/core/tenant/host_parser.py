"""HTTP 主机名中的租户标识提取。

提供从各种 Host 头格式中确定租户 Slug 的工具函数。
"""

from typing import Optional
from app.core.config import BASE_DOMAINS


def extract_slug_from_host(host: str) -> Optional[str]:
    """从 Host 头值中解析租户 Slug。

    支持**多个基域**（CIMS_BASE_DOMAIN + CIMS_EXTRA_BASE_DOMAINS）：
    Host 命中任一基域，就取该基域左侧的子域名作为 Slug。

    为什么要是复数：内网教室走 `<slug>.localhost`、公网走
    `<slug>.<公网域名>`。若只认单一基域，切公网当天所有内网教室立即 403。

    Args:
        host: 请求中的主机字符串（例如 'tenant.cims.com:50050'）。

    Returns:
        提取到的 Slug（子域名部分），未命中任何基域时返回 None。
    """
    hostname = host.split(":")[0].strip().lower()
    if not hostname:
        return None

    for base in BASE_DOMAINS:
        if hostname.endswith(f".{base}"):
            slug = hostname[: -(len(base) + 1)]
            if slug:
                return slug

    return None
