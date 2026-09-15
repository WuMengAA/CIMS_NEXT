"""跨系统账号同步端点。

website 作为账号权威：website 完成注册后，调用本端点把同一账号镜像到 CIMS，
使该用户也能用同一凭据登录 CIMS 控制台（POST /user/auth）。
仅接受携带正确 X-CIMS-Sync-Key 的内部调用；SYNC_KEY 未配置时整体禁用。

同步范围（与 website 侧 `syncUserToCims` 对齐）：
  · /sync-create —— 建号（幂等），可顺带创建 Account 空间
  · /sync-update —— 变更资料/角色/状态/班级年级（幂等，缺字段不改）

角色映射（website Role → CIMS CustomRole.code）：
  admin      → owner      （website 管理员＝CIMS 最高权限，可管理账号）
  editor     → admin
  moderator  → admin
  techrep    → teacher    （电教委员＝班级设备操作者）
  user       → viewer
  viewer     → viewer
未识别的角色一律降级为 viewer，**绝不提权**——新增权限功能时 website 是唯一权威，
CIMS 侧只在映射表里补条目，不在业务代码里写死角色判断。
"""

from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DEFAULT_ACCOUNT_SLUG, SYNC_KEY
from app.models.account import Account
from app.models.account_member import AccountMember
from app.models.session import get_db
from app.models.user import User
from app.services.crypto.hasher import hash_password
from app.services.user.account_creator import create_account  # 复用现成账号创建服务

router = APIRouter()

# ---------------------------------------------------------------------------
# website 角色 → CIMS 角色 映射表（唯一提权入口，扩展权限功能时只改这里）
# ---------------------------------------------------------------------------
WEBSITE_ROLE_TO_CIMS: dict[str, str] = {
    "admin": "owner",
    "editor": "admin",
    "moderator": "admin",
    "techrep": "teacher",
    "user": "viewer",
    "viewer": "viewer",
}

# 允许 CIMS 侧直接写入的角色码（防止 website 传任意串造成越权角色）
CIMS_ROLES: frozenset[str] = frozenset({"owner", "admin", "teacher", "viewer"})


def _map_role(role: str | None) -> str | None:
    """把 website 角色映射为 CIMS 角色码；未识别返回 None（调用方按降级处理）。"""
    if not role:
        return None
    key = role.strip().lower()
    if key in CIMS_ROLES:
        # 已经是 CIMS 角色码，原样接受
        return key
    return WEBSITE_ROLE_TO_CIMS.get(key)


def _guard(x_sync_key: str | None) -> None:
    """关闭门：未配置密钥或密钥不符则拒绝。"""
    if not SYNC_KEY or x_sync_key != SYNC_KEY:
        raise HTTPException(status_code=401, detail="同步接口未授权")


class SyncCreateRequest(BaseModel):
    """website 侧传来的账号信息（密码为明文，仅在内部网络一次性传输）。

    - `create_account=True` 时，建号后会自动为新用户创建 Account 空间并设其为 owner，
      使其能触达 `/account/{account_id}/client/*` 设备控制端点（默认 False，向后兼容）。
    - `account_name` / `account_slug` 仅在 `create_account=True` 时生效，可选。
    - `role` / `status` / `class_name` / `grade_name` 为 website 侧档案字段，建号时即可带入，
      避免「先建号再补一次 update」的两段式同步。
    """

    email: str
    password: str
    username: str | None = None
    display_name: str | None = None
    create_account: bool = False
    account_name: str | None = None
    account_slug: str | None = None
    # --- 档案 / 权限（website 权威） ---
    role: str | None = None
    status: str | None = None
    class_name: str | None = None
    grade_name: str | None = None


class SyncUpdateRequest(BaseModel):
    """website 侧账号变更同步请求（全部字段可选，只改传入的非 None 字段）。

    幂等：账号不存在时返回 `updated=False, created=False`，由 website 侧决定是否补建。
    """

    email: str
    password: str | None = None
    username: str | None = None
    display_name: str | None = None
    role: str | None = None
    status: str | None = None
    class_name: str | None = None
    grade_name: str | None = None


def _apply_profile_fields(user: User, payload: SyncUpdateRequest) -> list[str]:
    """把 website 档案字段写入 CIMS User 行，返回被改动的字段名列表。

    CIMS 的 users 表没有 class_name/grade_name 列（班级/年级在 website 侧），
    这类字段只落到 Account 空间的显示名与 client 归属上，见 `_sync_account_binding`。
    这里只处理 CIMS 真实存在的列。
    """
    changed: list[str] = []
    if payload.display_name is not None and payload.display_name != user.display_name:
        user.display_name = payload.display_name
        changed.append("display_name")
    mapped = _map_role(payload.role)
    if mapped and mapped != user.role_code:
        user.role_code = mapped
        changed.append("role_code")
    if payload.status is not None:
        # website status: active / banned（或任意非 active 视为停用）
        active = payload.status.strip().lower() == "active"
        if active != bool(user.is_active):
            user.is_active = active
            changed.append("is_active")
    return changed


async def _sync_account_binding(
    db: AsyncSession,
    user: User,
    class_name: str | None,
    grade_name: str | None,
) -> None:
    """把「班级 / 年级」同步到该用户拥有的 Account 空间的显示名。

    CIMS 没有独立的班级/年级列，但其 Account 空间是对公网暴露的租户实体
    （`<slug>.<BaseDomain>`），把班级年级写进 Account.name 可让集控面板与设备端
    直接读出「这台机器属于哪个年级的哪个班」，无需再回查 website。
    """
    if not class_name and not grade_name:
        return
    label_parts = [p for p in (grade_name, class_name) if p]
    if not label_parts:
        return
    display = "·".join(label_parts)

    member_rows = (
        await db.execute(
            select(AccountMember).where(
                AccountMember.user_id == user.id,
                AccountMember.role_in_account == "owner",
            )
        )
    ).scalars().all()
    for member in member_rows:
        account = (
            await db.execute(select(Account).where(Account.id == member.account_id))
        ).scalar_one_or_none()
        if account is not None and account.name != display:
            account.name = display


async def _ensure_default_account_membership(db: AsyncSession, user: User) -> str | None:
    """确保同步来的账号挂在「默认 Account 空间」上，返回该 account_id。

    为什么必须做：`GET /account/list` 是 `accounts JOIN account_members
    WHERE user_id = :me` —— 用户若没有任何 Account 归属，列表返回空，
    website 侧 `broadcastToClassrooms()` 就找不到 account，
    进而无法拼接 `/account/{id}/client/{uid}/command/send-notification`，
    广播链路整体哑火（实测 accounts=0 → 推送无处可发）。

    选取规则（幂等，绝不新建）：
      1. 若该用户已属于任意 Account，直接返回首个（尊重既有归属，不重排）；
      2. 否则取 `DEFAULT_ACCOUNT_SLUG` 指定的 Account（默认 `demo-class`，
         即 `tenant_demo-class` 租户对应的账号空间）；
      3. 仍找不到则取库中最早的 active Account（单校部署下的合理兜底）。
    找不到任何 Account 时返回 None（不阻断建号，仅由调用方记录）。
    """
    existing = (
        await db.execute(
            select(AccountMember).where(AccountMember.user_id == user.id).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing.account_id

    target = None
    if DEFAULT_ACCOUNT_SLUG:
        target = (
            await db.execute(
                select(Account).where(Account.slug == DEFAULT_ACCOUNT_SLUG)
            )
        ).scalar_one_or_none()
    if target is None:
        target = (
            await db.execute(
                select(Account)
                .where(Account.is_active == True)  # noqa: E712
                .order_by(Account.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
    if target is None:
        return None

    # role_in_account 与 CIMS 全局角色对齐：owner 角色的人在其空间里也是 owner
    role = "owner" if (user.role_code or "").lower() in {"owner", "admin"} else "member"
    db.add(
        AccountMember(
            id=str(uuid.uuid4()),
            user_id=user.id,
            account_id=target.id,
            role_in_account=role,
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return target.id


@router.post("/sync-create")
async def sync_create_user(
    payload: SyncCreateRequest = Body(...),
    x_sync_key: str | None = Header(default=None, alias="X-CIMS-Sync-Key"),
    db: AsyncSession = Depends(get_db),
):
    """website 注册后镜像建 CIMS 账号（active，可直接登录控制台）。

    幂等：邮箱已存在时返回 created=false，不报错（视为已同步）；
    但会把本次携带的档案/角色字段一并补齐（相当于顺手做一次 sync-update），
    避免「website 注册时带了角色、CIMS 却停在默认 normal」的不一致。
    """
    _guard(x_sync_key)

    # 基础校验
    if not payload.email or not payload.password:
        raise HTTPException(status_code=400, detail="email 与 password 必填")

    # 邮箱唯一性（幂等）
    exists = await db.execute(select(User).where(User.email == payload.email))
    existing = exists.scalar_one_or_none()
    if existing:
        # 已存在：补齐档案字段后返回（role/status/display_name 可增量对齐）
        changed = _apply_profile_fields(
            existing,
            SyncUpdateRequest(
                email=payload.email,
                display_name=payload.display_name,
                role=payload.role,
                status=payload.status,
            ),
        )
        if changed:
            await db.commit()
        await _sync_account_binding(db, existing, payload.class_name, payload.grade_name)
        # 补 Account 归属：即便影子账号是「已存在」分支，也要保证其能被
        # GET /account/list 查到，否则网站侧广播找不到 account。
        await _ensure_default_account_membership(db, existing)
        if changed or payload.class_name or payload.grade_name:
            await db.commit()
        return {
            "ok": True,
            "created": False,
            "email": payload.email,
            "updated_fields": changed,
        }

    mapped_role = _map_role(payload.role) or "normal"
    user = User(
        id=str(uuid.uuid4()),
        username=payload.username or f"user_{uuid.uuid4().hex[:8]}",
        email=payload.email,
        hashed_password=hash_password(payload.password),
        display_name=payload.display_name or payload.username or payload.email,
        role_code=mapped_role,
        is_active=(payload.status or "active").strip().lower() == "active",
        can_create_account=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()

    # 可选：为同步账号创建独立 Account 空间，设该用户为 owner
    # （打通"账号体系 → 设备控制端点 /account/{account_id}/client/*"的断层）
    account = None
    if payload.create_account:
        name = payload.account_name or payload.username or payload.email
        account = await create_account(
            name=name,
            user_id=user.id,
            db=db,
            slug=payload.account_slug,
        )

    # 建号后再同步一次班级/年级（Account 空间此刻才存在）
    await _sync_account_binding(db, user, payload.class_name, payload.grade_name)
    # 关键：把新账号挂到默认 Account 上，使其能触达 /account/{id}/client/*
    # 设备控制与广播端点（否则 /account/list 对该用户恒为空）。
    bound_account = account.id if account else None
    resolved = await _ensure_default_account_membership(db, user)
    if bound_account is None:
        bound_account = resolved
    await db.commit()

    return {
        "ok": True,
        "created": True,
        "email": payload.email,
        "account_id": bound_account,
        "role_code": user.role_code,
    }


@router.post("/sync-update")
async def sync_update_user(
    payload: SyncUpdateRequest = Body(...),
    x_sync_key: str | None = Header(default=None, alias="X-CIMS-Sync-Key"),
    db: AsyncSession = Depends(get_db),
):
    """website 侧账号变更（改资料 / 改角色 / 停启用 / 改班级年级）→ 同步到 CIMS。

    这是「账号要跟随 website 一起同步」的落点：website 的
    `update_user`（role/status）与 `PUT /api/me`（displayName/className/gradeName）
    都应调用本端点，使 CIMS 侧权限与身份不再滞后于 website。

    幂等且最小改动：只写传入的非 None 字段；账号不存在返回 created=False 而非报错，
    由 website 侧决定是否回退到 sync-create。
    """
    _guard(x_sync_key)

    if not payload.email:
        raise HTTPException(status_code=400, detail="email 必填")

    user = (
        await db.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if user is None:
        return {
            "ok": True,
            "created": False,
            "updated": False,
            "email": payload.email,
            "note": "CIMS 侧无此账号，请先调用 /user/sync-create",
        }

    changed = _apply_profile_fields(user, payload)

    # 可选：website 侧改密码后同步哈希，保证两边凭据一致
    if payload.password:
        user.hashed_password = hash_password(payload.password)
        changed.append("hashed_password")

    if changed:
        await db.commit()

    await _sync_account_binding(db, user, payload.class_name, payload.grade_name)
    # 顺手兜底：老账号可能建号时还没有 Account 归属，这里幂等补上
    await _ensure_default_account_membership(db, user)
    await db.commit()

    return {
        "ok": True,
        "created": False,
        "updated": bool(changed) or bool(payload.class_name or payload.grade_name),
        "email": payload.email,
        "updated_fields": changed,
        "role_code": user.role_code,
        "is_active": bool(user.is_active),
    }
