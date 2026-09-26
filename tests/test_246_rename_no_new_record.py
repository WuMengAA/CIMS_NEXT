"""票 #246「设备改名变成新建」回归验证（方案 B：uid 为唯一身份键）。

根因：HTTP 状态上报以路径 `client_id`（生产里是可变主机名）作为
`ClientStatus` / `ClientProfile` 主键；老师改名→主机名变→路径变→查不到旧行→
INSERT 新设备记录，旧行（含班级绑定）成孤儿：绑定断裂、设备数虚增。

方案 B 验证：设备唯一身份以稳定 `uid` 为键，`host` / 展示名只是可变展示字段。
改名只 UPDATE 展示名，绝不新建设备。

覆盖两种上报形态：
  A. 固定 uid、host 不同（任务书字面验证）
  B. 固定 uid、但设备以「可变主机名」为路径上报，改名后路径变（真实 bug 场景）
     断言：两次上报解析为同一条 uid 记录，展示名更新为第二次的 host，且总数=1。
"""

import uuid

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from app.apps.client_app import client_app
from app.apps.management_app import management_app
from app.core.tenant.context import set_search_path
from app.models.client import ClientRecord, ClientStatus, ClientProfile
from app.models.database import AsyncSessionLocal
from tests.conftest import TEST_ACCOUNT_SLUG


def _u() -> str:
    return "t246-" + uuid.uuid4().hex[:14]


async def _grpc_register(db, uid: str, display: str) -> None:
    """模拟 grpc Register：写 ClientRecord（uid 为身份，client_id 为展示名）。"""
    await set_search_path(db)
    rec = (
        await db.execute(select(ClientRecord).where(ClientRecord.uid == uid))
    ).scalar_one_or_none()
    if rec is None:
        db.add(ClientRecord(uid=uid, client_id=display))
    else:
        rec.client_id = display
    await db.commit()


async def _count_status(db, uid: str) -> tuple[int, str]:
    await set_search_path(db)
    rows = (
        await db.execute(select(ClientStatus).where(ClientStatus.client_id == uid))
    ).scalars().all()
    host = rows[0].host if rows else ""
    return len(rows), host


async def _cleanup(db, uid: str) -> None:
    await set_search_path(db)
    # ClientStatus / ClientProfile 以 client_id 列存 uid 值；ClientRecord 以 uid 为主键。
    for m in (ClientStatus, ClientProfile):
        for r in (await db.execute(select(m).where(m.client_id == uid))).scalars().all():
            await db.delete(r)
    for r in (await db.execute(select(ClientRecord).where(ClientRecord.uid == uid))).scalars().all():
        await db.delete(r)
    await db.commit()


async def _post_status(token: str, host: str):
    transport = ASGITransport(app=client_app)
    async with AsyncClient(transport=transport, base_url="http://test-school.localhost") as ac:
        return await ac.post(f"/api/v1/client/{token}/status", json={"host": host})


async def _get_status(token: str):
    transport = ASGITransport(app=client_app)
    async with AsyncClient(transport=transport, base_url="http://test-school.localhost") as ac:
        return await ac.get(f"/api/v1/client/{token}/status")


@pytest.mark.asyncio
async def test_fixed_uid_diff_host_one_record():
    """Scenario A：固定 uid、host 不同 → 同一条记录，展示名更新为第二次 host。"""
    uid = _u()
    try:
        r1 = await _post_status(uid, "HOST-A")
        assert r1.status_code == 200, r1.text
        r2 = await _post_status(uid, "HOST-B")
        assert r2.status_code == 200, r2.text

        async with AsyncSessionLocal() as db:
            cnt, host = await _count_status(db, uid)
        assert cnt == 1, f"应只有 1 条设备记录，实际 {cnt} 条（改名变新建）"
        assert host == "HOST-B", f"展示名应更新为 HOST-B，实际 {host}"

        g = await _get_status(uid)
        assert g.status_code == 200, g.text
        assert g.json().get("host") == "HOST-B"
    finally:
        async with AsyncSessionLocal() as db:
            await _cleanup(db, uid)


@pytest.mark.asyncio
async def test_rename_host_token_changes_still_same_uid():
    """Scenario B（真实 bug）：固定 uid，设备以可变主机名上报；改名后路径变，
    仍解析为同一条 uid 记录，展示名更新，且绝无第二条记录。"""
    uid = _u()
    try:
        async with AsyncSessionLocal() as db:
            await _grpc_register(db, uid, "OLDNAME")

        r1 = await _post_status("OLDNAME", "OLDNAME")  # 路径=旧主机名
        assert r1.status_code == 200, r1.text

        # 老师改名 → grpc 把展示名更新为 NEWNAME（uid 不变）
        async with AsyncSessionLocal() as db:
            await _grpc_register(db, uid, "NEWNAME")

        r2 = await _post_status("NEWNAME", "NEWNAME")  # 路径=新主机名
        assert r2.status_code == 200, r2.text

        # 关键断言：两条上报只产生 1 条 uid 记录，且展示名=NEWNAME
        async with AsyncSessionLocal() as db:
            cnt, host = await _count_status(db, uid)
        assert cnt == 1, f"改名后不应新建记录，实际 {cnt} 条"
        assert host == "NEWNAME", f"展示名应更新为 NEWNAME，实际 {host}"

        # 改名后设备改用 NEWNAME / uid 上报，这两种令牌都应命中同一条记录
        for token in (uid, "NEWNAME"):
            g = await _get_status(token)
            assert g.status_code == 200, g.text
            assert g.json().get("host") == "NEWNAME", token

        # 旧主机名 OLDNAME 已成死令牌：读取不应解析到记录，更不应新建记录
        g_old = await _get_status("OLDNAME")
        assert g_old.status_code == 200, g_old.text
        assert g_old.json().get("reported") is False, "旧主机名应已失效，不应解析到记录"

        # 改名后设备数仍=1，旧名读取也未产生新记录（设备数不虚增的关键证明）
        async with AsyncSessionLocal() as db:
            cnt_after, _ = await _count_status(db, uid)
        assert cnt_after == 1, f"改名后设备数应仍为 1，实际 {cnt_after}（虚增）"

    finally:
        async with AsyncSessionLocal() as db:
            await _cleanup(db, uid)


@pytest.mark.asyncio
async def test_rename_preserves_class_binding(admin_headers, test_superadmin_user):
    """改名不应打断班级绑定：分配班级→改名上报→绑定仍在，且只 1 条记录。"""
    uid = _u()
    class_id = "class_246_binding"
    try:
        async with AsyncSessionLocal() as db:
            await _grpc_register(db, uid, "BEFORE")
            from app.models.class_model import Class
            from datetime import datetime, timezone
            c = (await db.execute(select(Class).where(Class.id == class_id))).scalar_one_or_none()
            if c is None:
                db.add(Class(id=class_id, name="246绑定班", resource_set_id="", sort_order=0,
                             review_status="approved",
                             created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc)))
                await db.commit()

        # 上报一次（建档案）
        r0 = await _post_status("BEFORE", "BEFORE")
        assert r0.status_code == 200, r0.text

        # 管理端分配班级
        transport = ASGITransport(app=management_app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            a = await ac.post(
                "/class/device/assign",
                params={"class_id": class_id, "client_id": uid},
                headers=admin_headers,
            )
        assert a.status_code == 200, a.text
        assert a.json().get("class_id") == class_id

        # 老师改名 → grpc 展示名变 AFTER，再次上报（路径=新名）
        async with AsyncSessionLocal() as db:
            await _grpc_register(db, uid, "AFTER")
        r1 = await _post_status("AFTER", "AFTER")
        assert r1.status_code == 200, r1.text

        # 关键断言：绑定仍在 + 仍只 1 条 uid 记录
        async with AsyncSessionLocal() as db:
            await set_search_path(db)
            prof = (await db.execute(
                select(ClientProfile).where(ClientProfile.client_id == uid)
            )).scalar_one_or_none()
            assert prof is not None, "改名后设备档案不应丢失"
            assert prof.class_id == class_id, f"改名不应打断班级绑定，实际 {prof.class_id}"
            cnt, _ = await _count_status(db, uid)
        assert cnt == 1, f"改名后设备数应仍为 1，实际 {cnt}（绑定断裂且虚增）"

        # 回读确认展示名已更新、绑定未变
        g = await _get_status(uid)
        assert g.json().get("host") == "AFTER"
    finally:
        async with AsyncSessionLocal() as db:
            await set_search_path(db)
            from app.models.class_model import Class
            c = (await db.execute(select(Class).where(Class.id == class_id))).scalar_one_or_none()
            if c is not None:
                await db.delete(c)
            await db.commit()
            await _cleanup(db, uid)
