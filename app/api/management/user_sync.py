"""跨系统账号同步端点。

website 作为账号权威：website 完成注册后，调用本端点把同一账号镜像到 CIMS，
使该用户也能用同一凭据登录 CIMS 控制台（POST /user/auth）。
仅接受携带正确 X-CIMS-Sync-Key 的内部调用；SYNC_KEY 未配置时整体禁用。
"""

from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import SYNC_KEY
from app.models.session import get_db
from app.models.user import User
from app.services.crypto.hasher import hash_password

router = APIRouter()


class SyncCreateRequest(BaseModel):
    """website 侧传来的账号信息（密码为明文，仅在内部网络一次性传输）。"""

    email: str
    password: str
    username: str | None = None
    display_name: str | None = None


@router.post("/sync-create")
async def sync_create_user(
    payload: SyncCreateRequest = Body(...),
    x_sync_key: str | None = Header(default=None, alias="X-CIMS-Sync-Key"),
    db: AsyncSession = Depends(get_db),
):
    """website 注册后镜像建 CIMS 账号（active，可直接登录控制台）。

    幂等：邮箱已存在时返回 created=false，不报错（视为已同步）。
    """
    # 关闭门：未配置密钥或密钥不符则拒绝
    if not SYNC_KEY or x_sync_key != SYNC_KEY:
        raise HTTPException(status_code=401, detail="同步接口未授权")

    # 基础校验
    if not payload.email or not payload.password:
        raise HTTPException(status_code=400, detail="email 与 password 必填")

    # 邮箱唯一性（幂等）
    exists = await db.execute(select(User).where(User.email == payload.email))
    if exists.scalar_one_or_none():
        return {"ok": True, "created": False, "email": payload.email}

    user = User(
        id=str(uuid.uuid4()),
        username=payload.username or f"user_{uuid.uuid4().hex[:8]}",
        email=payload.email,
        hashed_password=hash_password(payload.password),
        display_name=payload.display_name or payload.username or payload.email,
        role_code="normal",
        is_active=True,
        can_create_account=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    return {"ok": True, "created": True, "email": payload.email}
