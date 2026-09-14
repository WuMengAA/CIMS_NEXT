"""租户范围内资源文件模型的共有字段。

为按 name 索引、存储 JSON 内容及版本的表提供通用结构。
Schema 隔离后不再需要 tenant_id 列。
"""

from datetime import datetime
from sqlalchemy import DateTime, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column


class ResourceMixin:
    """资源存储表的模型 Mixin。

    `server_default` 不是可选项：资源行常被裸 SQL（CLI 导入脚本 / 重建脚本）插入，
    只有 Python 侧 `default=` 时，裸 INSERT 缺列会直接撞 NOT NULL。
    """

    name: Mapped[str] = mapped_column(String, primary_key=True)
    content: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, server_default=func.now()
    )
