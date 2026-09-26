"""RBAC 权限检查依赖。

提供 FastAPI 依赖函数，实现路由级的细粒度权限控制。
"""

import logging

from fastapi import Request, HTTPException
from sqlalchemy import select

from app.models.database import AsyncSessionLocal
from app.models.account_member import AccountMember
from app.models.role_permission import RolePermission
from app.models.member_permission import MemberPermission
from app.core.tenant.context import get_tenant_id, set_search_path

logger = logging.getLogger(__name__)


async def _get_member_permissions(user_id: str, account_id: str) -> set[str]:
    """汇总用户在指定账户内的有效权限集合。"""
    async with AsyncSessionLocal() as db:
        await set_search_path(db)
        # 查询成员关系
        stmt = (
            select(AccountMember)
            .where(AccountMember.user_id == user_id)
            .where(AccountMember.account_id == account_id)
        )
        member = (await db.execute(stmt)).scalar_one_or_none()
        if not member:
            return set()

        # owner 拥有所有权限
        if member.role_in_account == "owner":
            return {"*"}

        # 角色权限
        role_perms_stmt = select(RolePermission.permission_code).where(
            RolePermission.role_code == member.role_in_account
        )
        role_perms = set((await db.execute(role_perms_stmt)).scalars().all())

        # 成员级覆盖
        member_overrides_stmt = select(MemberPermission).where(
            MemberPermission.member_id == member.id
        )
        overrides = (await db.execute(member_overrides_stmt)).scalars().all()
        for ov in overrides:
            if ov.granted:
                role_perms.add(ov.permission_code)
            else:
                role_perms.discard(ov.permission_code)

        # 权限别名归一（2026-09-26 补）：种子体系用 client.manage，端点体系用 client.write，
        # 二者语义相同（管理客户端）。把 manage 扩展成 write，避免「旧种子 + 新端点」
        # 让 admin/teacher 在班级写操作上被 403 拒掉。
        if "client.manage" in role_perms:
            role_perms.add("client.write")
        if "client.write" in role_perms:
            role_perms.add("client.manage")
        # 同样的别名：command.execute 与 config.edit 互认（旧端点是 edit，新端点是 write）
        if "config.edit" in role_perms:
            role_perms.add("config.write")
            role_perms.add("config.read")
        if "config.write" in role_perms:
            role_perms.add("config.edit")

        return role_perms


def require_permission(*perms: str):
    """返回一个 FastAPI 依赖，检查当前用户是否拥有所有指定权限。"""

    async def _checker(request: Request):
        """权限检查依赖实现。"""
        user_id = getattr(request.state, "current_user_id", None)
        if not user_id:
            raise HTTPException(status_code=401, detail="未认证")
        account_id = await _resolve_account_id(request)
        if not account_id:
            raise HTTPException(status_code=403, detail="租户上下文缺失")
        granted = await _get_member_permissions(user_id, account_id)
        # owner 拥有所有权限
        if "*" in granted:
            return user_id
        for p in perms:
            if p not in granted:
                logger.warning("权限不足: user=%s perm=%s", user_id, p)
                raise HTTPException(status_code=403, detail=f"缺少权限: {p}")
        return user_id

    return _checker


async def _resolve_account_id(request: Request) -> str:
    """解析当前请求的账户 ID，多级兜底：

    ① request.state.tenant_id（AccountContextMiddleware / 会话中间件显式设置）；
    ② get_tenant_id()（TenantMiddleware 的 ContextVar）；
    ③ schema_ctx 反推：`tenant_<slug>` → resolve_account(slug)（class_routes 的
       _ensure_class_tenant 会把 search_path 钉到 tenant_{DEFAULT_ACCOUNT_SLUG}，
       但那是会话级、不设 ContextVar —— 此处从 schema 名反解 slug）；
    ④ 回退 DEFAULT_ACCOUNT_SLUG 解析（与 class_routes/scheduled_broadcast 同口径）。
    """
    # ① 中间件显式设置
    st = getattr(request.state, "tenant_id", None)
    if st:
        return str(st)
    # ② TenantMiddleware ContextVar
    try:
        tid = get_tenant_id()
        if tid:
            return tid
    except RuntimeError:
        pass
    # ③ / ④ schema 反推
    try:
        from app.core.config import DEFAULT_ACCOUNT_SLUG
        from app.core.tenant.resolver import resolve_account
        from app.core.tenant.context import schema_ctx
        from app.models.engine import AsyncSessionLocal

        schema = schema_ctx.get()
        slug = ""
        if schema and schema.startswith("tenant_"):
            slug = schema[len("tenant_") :]
        if not slug:
            slug = DEFAULT_ACCOUNT_SLUG or ""
        if not slug:
            return ""
        async with AsyncSessionLocal() as db:
            account = await resolve_account(slug, db)
        return str(account.id) if account else ""
    except Exception:
        return ""
