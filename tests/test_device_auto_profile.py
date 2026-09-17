"""设备档案自愈（client_profiles 自动建档）—— 回归保护。

2026-09-18 背景：此前**生产代码没有任何路径**会创建 `client_profiles`
（只有租户初始化脚本会建）。后果是新装的教室机会陷入一个极难自查的状态：

    面板上看得见它（心跳进了 client_status）
    却绑不了班（POST /class/device/assign → 404「配置档案不存在」）

界面上有这台机器、点了就是失败 —— 首次实测的 P0（大屏显示真实课表）
正卡在这里。现由两处自愈，本文件锁死它们的行为，防止后续重构再删掉：

  1. `client/status.py` —— 设备**首次上报心跳即建档**，且绝不覆盖已指派班级
  2. `class_routes.py`  —— `/device/assign` **按需建档**，支持「先指派、后装机」
"""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from app.apps.client_app import client_app
from app.apps.management_app import management_app
from app.core.tenant.context import set_search_path
from app.models.class_model import Class
from app.models.database import AsyncSessionLocal, ClientProfile
from tests.conftest import TEST_ACCOUNT_ID

# 注意：class 路由直接挂在 management_app 根下（`include_router(mgr_router)`），
# 没有 /account/{id} 前缀；租户由 AdminAuth 令牌中的 tenant_id 解析。
# 写成 /account/{tid}/class/... 会一律 404，且这种假 404 会让「期望 404」的
# 断言假通过 —— 排查时务必先用存在的班级验证一次 200 路径。


def _uid() -> str:
    """测试设备标识。带前缀便于事后在库里认出是测试残留。"""
    return "probe-" + uuid.uuid4().hex[:12]


async def _get_profile(client_id: str):
    async with AsyncSessionLocal() as db:
        await set_search_path(db)
        return (
            await db.execute(
                select(ClientProfile).where(ClientProfile.client_id == client_id)
            )
        ).scalar_one_or_none()


async def _delete_profile(client_id: str) -> None:
    async with AsyncSessionLocal() as db:
        await set_search_path(db)
        p = (
            await db.execute(
                select(ClientProfile).where(ClientProfile.client_id == client_id)
            )
        ).scalar_one_or_none()
        if p is not None:
            await db.delete(p)
            await db.commit()


async def _ensure_class(class_id: str, name: str = "测试班") -> None:
    async with AsyncSessionLocal() as db:
        await set_search_path(db)
        c = (
            await db.execute(select(Class).where(Class.id == class_id))
        ).scalar_one_or_none()
        if c is None:
            # created_at/updated_at 必须显式给：Class 模型的 python 默认值仍是
            # datetime.utcnow（Python 3.12+ 弃用），在 -W error 的测试配置下
            # 这个 DeprecationWarning 会直接把 INSERT 打成 StatementError。
            db.add(
                Class(
                    id=class_id,
                    name=name,
                    resource_set_id="",
                    sort_order=0,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()


async def _post_status(uid: str, body: dict | None = None) -> "object":
    """按教室机方式上报一次心跳（Host 头带租户 slug）。"""
    transport = ASGITransport(app=client_app)
    async with AsyncClient(
        transport=transport, base_url="http://test-school.localhost"
    ) as ac:
        return await ac.post(
            f"/api/v1/client/{uid}/status",
            json=body if body is not None else {"host": "PROBE-PC"},
        )


@pytest.mark.asyncio
async def test_first_status_report_creates_profile():
    """新设备第一次上报心跳就要有档案 —— 否则面板绑不了班。"""
    uid = _uid()
    try:
        r = await _post_status(uid)
        assert r.status_code == 200, r.text

        p = await _get_profile(uid)
        assert p is not None, "首报心跳应自动建档，否则 /device/assign 会 404"
        # 班级留空，等管理端指派；资源名全部走默认（由 server_default 兜底）
        assert (p.class_id or "") == ""
    finally:
        await _delete_profile(uid)


@pytest.mark.asyncio
async def test_status_report_never_overwrites_assigned_class():
    """管理端指派是权威归属：心跳自报的 class_id 不得覆盖它。

    这条是安全网 —— 若心跳能改写班级，某台机器一次乱报就会把自己（和它的
    课表/广播定向）挪到别的班，而且没有任何报错。
    """
    uid = _uid()
    assigned = "class_probe_assigned"
    try:
        await _ensure_class(assigned)
        async with AsyncSessionLocal() as db:
            await set_search_path(db)
            db.add(ClientProfile(client_id=uid, class_id=assigned))
            await db.commit()

        # 设备自报一个完全不同的班级
        r = await _post_status(uid, {"host": "PROBE-PC", "class_id": "class_self_reported"})
        assert r.status_code == 200, r.text

        p = await _get_profile(uid)
        assert p.class_id == assigned, "心跳覆盖了管理端指派的班级"
    finally:
        await _delete_profile(uid)


@pytest.mark.asyncio
async def test_assign_creates_profile_when_missing(admin_headers, test_superadmin_user):
    """「先指派、后装机」：设备还没上报过心跳时也要能绑班并建档。

    价值：可以提前在面板把明天要装的机器划进班级，装完一启动就直接拿到
    本班课表，不必等现场再补一次操作。
    """
    uid = _uid()
    class_id = "class_probe_prebind"
    try:
        await _ensure_class(class_id, "预绑定测试班")

        transport = ASGITransport(app=management_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/class/device/assign",
                params={"class_id": class_id, "client_id": uid},
                headers=admin_headers,
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "success"
        assert body["created_profile"] is True, "缺失档案时应自动建档"
        assert body["class_id"] == class_id

        p = await _get_profile(uid)
        assert p is not None
        assert p.class_id == class_id
    finally:
        await _delete_profile(uid)


@pytest.mark.asyncio
async def test_assign_still_rejects_unknown_class(admin_headers, test_superadmin_user):
    """班级不存在时仍然 404 —— 自动建档不能把「班级写错」也一并吞掉。"""
    uid = _uid()
    transport = ASGITransport(app=management_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/class/device/assign",
            params={"class_id": "class_does_not_exist", "client_id": uid},
            headers=admin_headers,
        )
    assert r.status_code == 404, r.text
    # 班级不存在 → 不应留下孤儿档案
    assert await _get_profile(uid) is None
