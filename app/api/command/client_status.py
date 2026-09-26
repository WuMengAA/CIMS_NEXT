"""客户端在线状态监控。

提供终端的在线/离线状态查询和详细记录查看。
按 NewAPI.md: GET /list, GET /search, GET /{client_id}, GET /{client_id}/status,
DELETE /{client_id}, POST /{client_id}/rename, POST /{client_id}/disconnect,
POST /{client_id}/disable, POST /{client_id}/enable, POST /{client_id}/config
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, Request, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.database import get_db, ClientProfile, ClientRecord
from app.models.client import ClientStatus
from app.core.tenant.context import get_tenant_id
from app.core.auth.rbac import require_permission


def _heartbeat_fresh(reported_at) -> bool:
    """最近 FRESH_SECONDS（90s）内有过 HTTP 心跳即算在线。

    背景（2026-09-26「远控残废」修复）：`online:` Redis 会话键只由 gRPC 流式
    握手路径写入（command_deliver / command_utils），而桌面端与教室插件走的是
    **HTTP 心跳**（POST /v1/client/{id}/status），只更新 client_status 表、
    从不产生会话键 → is_client_online 对它们恒为 False → 面板/客户端拿到
    status="offline" 后把重启/锁屏/截图/**远程控制**等按钮全部禁用。
    这里与 /class/device-status 使用同一契约做兜底，只影响**显示与门控**，
    不触碰 gRPC 会话语义。
    """
    if reported_at is None:
        return False
    from app.api.client.status import FRESH_SECONDS

    try:
        age = (datetime.now(timezone.utc) - reported_at).total_seconds()
    except TypeError:
        return False
    return age <= FRESH_SECONDS

router = APIRouter()


@router.get("/list")
async def list_clients(db: AsyncSession = Depends(get_db)):
    """获取当前租户下所有客户端列表（注册表 ∪ 心跳活跃表，去重）。

    「设备状态真实显示」的数据源是 client_status（心跳表）：面板里应该看到的
    是「这台机器活着吗、装了什么」，而不是只有手动预注册的 clients 表。
    2026-09-26 修复：此前只查 ClientRecord → 面板永远只显示预注册的那几台，
    心跳上来的真实机器（桌面端本机、各教室大屏）全部漏掉。
    """
    registered = set((await db.execute(select(ClientRecord.uid))).scalars().all())
    status_ids = set(
        (await db.execute(select(ClientStatus.client_id))).scalars().all()
    )
    # 合并去重，心跳设备优先；按最近上报时间倒序，让活跃设备排前面
    merged = registered | status_ids
    if not merged:
        return []
    status_rows = (
        await db.execute(
            select(ClientStatus.client_id, ClientStatus.reported_at).order_by(
                ClientStatus.reported_at.desc()
            )
        )
    ).all()
    recency = {r.client_id: r.reported_at for r in status_rows}
    # 活跃设备（最近上报过）排前面；未上报过的注册设备垫底
    ordered = [r.client_id for r in status_rows]
    rest = sorted(u for u in merged if u not in recency)
    return [*ordered, *rest]


@router.get("/search")
async def search_clients(q: str = "", db: AsyncSession = Depends(get_db)):
    """搜索客户端（合并注册表与心跳表）。"""
    registered = (await db.execute(select(ClientRecord.uid))).scalars().all()
    status_ids = (await db.execute(select(ClientStatus.client_id))).scalars().all()
    all_ids = list(dict.fromkeys([*registered, *status_ids]))
    if not q:
        return all_ids
    return [u for u in all_ids if q.lower() in (u or "").lower()]


@router.get("/{client_id}")
async def get_client_detail(
    client_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """查询特定客户端的注册详情及其当前在线状态。"""
    tid = get_tenant_id()
    record = (
        await db.execute(select(ClientRecord).where(ClientRecord.uid == client_id))
    ).scalar_one_or_none()
    # 心跳状态优先：client_status 是「设备状态真实显示」的数据源
    st = (
        await db.execute(select(ClientStatus).where(ClientStatus.client_id == client_id))
    ).scalar_one_or_none()
    if not record and not st:
        raise HTTPException(status_code=404, detail="未找到设备")
    sm = getattr(request.app.state, "session_manager", None)
    online = await sm.is_client_online(tid, client_id) if sm else False
    if not online:
        online = _heartbeat_fresh(st.reported_at if st else None)
    return {
        "uid": client_id,
        "name": (st.client_id if st else (record.client_id if record else "")) or client_id,
        "host": st.host if st else "",
        "ip": st.ip if st else "",
        "version": st.version if st else "",
        "class_id": st.class_id if st else (record.class_id if record else ""),
        "mac": record.mac if record else "",
        "status": "online" if online else ("offline"),
        "reported_at": (
            st.reported_at.isoformat() if st and st.reported_at else None
        ),
        "registered_at": (
            record.registered_at.isoformat() if record and record.registered_at else None
        ),
    }


@router.delete("/{client_id}", dependencies=[Depends(require_permission("client.write"))])
async def delete_client(client_id: str, db: AsyncSession = Depends(get_db)):
    """删除客户端。"""
    record = (
        await db.execute(select(ClientRecord).where(ClientRecord.uid == client_id))
    ).scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="未找到设备")
    await db.delete(record)
    await db.commit()
    return {"message": "已删除"}


@router.post("/{client_id}/rename", dependencies=[Depends(require_permission("client.write"))])
async def rename_client(client_id: str, db: AsyncSession = Depends(get_db)):
    """重命名客户端。"""
    return {"message": "暂未实现", "client_id": client_id}


@router.get("/{client_id}/status")
async def get_client_status(
    client_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """获取客户端在线状态（Redis 会话 或 最近 HTTP 心跳，任一命中即在线）。"""
    tid = get_tenant_id()
    sm = getattr(request.app.state, "session_manager", None)
    online = await sm.is_client_online(tid, client_id) if sm else False
    if not online:
        st = (
            await db.execute(
                select(ClientStatus).where(ClientStatus.client_id == client_id)
            )
        ).scalar_one_or_none()
        online = _heartbeat_fresh(st.reported_at if st else None)
    return {"client_id": client_id, "online": online}


@router.post("/{client_id}/disconnect", dependencies=[Depends(require_permission("command.execute"))])
async def disconnect_client(client_id: str, request: Request):
    """断开客户端连接。"""
    return {"message": "暂未实现", "client_id": client_id}


@router.post("/{client_id}/disable", dependencies=[Depends(require_permission("client.write"))])
async def disable_client(client_id: str):
    """禁用客户端。"""
    return {"message": "暂未实现", "client_id": client_id}


@router.post("/{client_id}/enable", dependencies=[Depends(require_permission("client.write"))])
async def enable_client(client_id: str):
    """启用客户端。"""
    return {"message": "暂未实现", "client_id": client_id}


@router.post("/{client_id}/config", dependencies=[Depends(require_permission("client.write"))])
async def set_client_config(client_id: str):
    """修改客户端使用的档案组。"""
    return {"message": "暂未实现", "client_id": client_id}


@router.post("/{client_id}/restrictions", dependencies=[Depends(require_permission("client.write"))])
async def set_client_restrictions(
    client_id: str,
    request: Request,
    body: dict = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
):
    """设置某台设备的动作限制（控制限制 · 关键逻辑在服务端）。

    body 形如 {"actions": ["shutdown", "reboot"]}：这些 stelarith 动作在同一租户内
    对该设备被禁止 —— 下发 stelarith_task 时由 CIMS 校验并拒绝，不落到设备端。
    传空数组 = 解除全部限制。
    """
    import json as _json

    raw = body or {}
    actions = raw.get("actions", [])
    if not isinstance(actions, list):
        raise HTTPException(400, "actions 必须是数组")
    cleaned = [
        str(a).strip()
        for a in actions
        if isinstance(a, str) and a.strip()
    ][:50]

    profile = (
        await db.execute(select(ClientProfile).where(ClientProfile.client_id == client_id))
    ).scalar_one_or_none()
    if profile is None:
        profile = ClientProfile(client_id=client_id)
        db.add(profile)
    profile.action_restrictions = _json.dumps(cleaned, ensure_ascii=False)
    await db.commit()
    return {"status": "success", "client_id": client_id, "restricted_actions": cleaned}


@router.get("/{client_id}/restrictions", dependencies=[Depends(require_permission("client.read"))])
async def get_client_restrictions(
    client_id: str,
    db: AsyncSession = Depends(get_db),
):
    """查询某台设备的动作限制。"""
    import json as _json
    profile = (
        await db.execute(select(ClientProfile).where(ClientProfile.client_id == client_id))
    ).scalar_one_or_none()
    if profile is None:
        return {"client_id": client_id, "restricted_actions": []}
    try:
        actions = _json.loads(profile.action_restrictions or "[]")
    except Exception:
        actions = []
    return {"client_id": client_id, "restricted_actions": actions if isinstance(actions, list) else []}

