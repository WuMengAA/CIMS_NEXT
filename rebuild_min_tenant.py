"""重新初始化一个干净、真实的最小集控租户（对齐 ClassIsland 官方集控逻辑）。

背景：数据库已按「重新初始化」要求清空（#72）。本脚本重建 demo-class 租户，并把
ClassIsland 官方 7 类资源（ClassPlan/TimeLayout/Subjects/DefaultSettings/Policy/
Components/Credentials）真实落库，使客户端 manifest→资源 拉取链路全 200，不再出现
「资源缺失 404 → 后端安全中间件累积封禁 IP → 轮询 429」的自伤。

对齐官方逻辑：
  · 租户走 account_creator.create_account（accounts + owner member + quotas）
  · schema 走 ensure_tenant_schema 建 tenant_<slug> 及全部租户表
  · 资源内容取自 seed_resources/（由 extract_seed_resources.py 从真实 ClassIsland
    安装提取），按官方资源类型写入对应资源表
  · 客户端在 clients + client_profiles 双表登记，manifest 依此解析各资源 name
  · 预置一条 DataUpdated 待执行命令，供插件 HTTP 轮询取走闭环验证

幂等：重复执行会先删除既有 demo-class 租户再重建，可安全重跑。

用法（在 CIMS-backend 目录）：
    PYTHONPATH=. .venv/Scripts/python.exe rebuild_min_tenant.py
"""

import asyncio
import json
import os
import sys

if sys.platform == "win32":
    # psycopg async 在 Windows 需 SelectorEventLoop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# slug 与 owner —— 与插件 stelarith-sync.json 目标一致
SLUG = "demo-class"
ACCOUNT_NAME = "星璃演示班"
OWNER_USER_ID = "e3dcfd9e-d5d0-46c1-a015-b72ebd750449"  # 系统保留管理员 WuMengAA
CLIENT_UID = "lab-pc-001"

# 官方 7 类资源：种子文件 -> (租户表, 资源 name)
# name 与 manifest.py 的默认值对齐（cp/tl 为 default_classplan/default_timelayout，其余 default）
RESOURCE_MAP = [
    ("ClassPlan.json", "cp_files", "default_classplan"),
    ("TimeLayout.json", "tl_files", "default_timelayout"),
    ("Subjects.json", "sub_files", "default"),
    ("DefaultSettings.json", "settings_files", "default"),
    ("Policy.json", "policy_files", "default"),
    ("Components.json", "components_files", "default"),
    ("Credentials.json", "credentials_files", "default"),
]

SEED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_resources")


async def main() -> None:
    from app.models.engine import AsyncSessionLocal
    from app.models.schema_init import ensure_tenant_schema
    from app.services.user.account_creator import create_account

    schema_name = f"tenant_{SLUG}"

    async with AsyncSessionLocal() as db:
        # ---- 0) 幂等清理：删除已有 demo-class 租户（accounts 记录 + schema）----
        await _drop_existing(db, schema_name)
        await db.commit()

        # ---- 1) 走官方 create_account 建账号 ----
        account = await create_account(ACCOUNT_NAME, OWNER_USER_ID, db, slug=SLUG)
        print(f"[ok] 账号创建: {ACCOUNT_NAME}  slug={account.slug}  id={account.id}")

        # ---- 2) 建租户 schema + 租户表（含 command_queue / clients / 资源表）----
        schema = await ensure_tenant_schema(SLUG)
        print(f"[ok] 租户 schema: {schema}")

        # ---- 3) 写入官方 7 类资源 ----
        written = await _seed_resources(db, schema_name)
        print(f"[ok] 官方资源写入: {written} 类")

        # ---- 4) 登记客户端设备（clients）+ 资源档案（client_profiles）----
        await _register_client(db, schema_name)
        print(f"[ok] 客户端登记: {CLIENT_UID}（clients + client_profiles）")

        # ---- 5) 预置一条 DataUpdated 待执行命令（验证插件轮询取走闭环）----
        await _enqueue_data_updated(db, schema_name)
        print("[ok] 已写入 1 条 DataUpdated 待执行命令 → 插件轮询应取走执行")

        # ---- 6) 提交（关键：以上写入均在 savepoint 内，必须显式提交，否则会话关闭即回滚）----
        await db.commit()
        print("[ok] 事务已提交")

        # ---- 7) 校验（独立读，确认真实落库）----
        await _verify(db, schema_name)


async def _drop_existing(db: AsyncSession, schema_name: str) -> None:
    """删除既有同名租户（记录 + schema），保证幂等。"""
    await db.execute(text("DELETE FROM accounts WHERE slug = :s"), {"s": SLUG})
    try:
        await db.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] DROP schema 失败（可忽略）: {exc}")


async def _seed_resources(db: AsyncSession, schema_name: str) -> int:
    """把 seed_resources/ 下的官方资源写入租户 schema 的资源表。"""
    count = 0
    async with db.begin_nested():
        await db.execute(text(f'SET search_path TO "{schema_name}"'))
        for fname, table, res_name in RESOURCE_MAP:
            path = os.path.join(SEED_DIR, fname)
            if not os.path.exists(path):
                print(f"[warn] 缺少种子 {fname}，跳过 {table}")
                continue
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            json.loads(content)  # 校验是合法 JSON，避免写坏内容
            await db.execute(
                text(
                    f"""
                    INSERT INTO {table} (name, content, version, updated_at)
                    VALUES (:n, :c, 1, now())
                    ON CONFLICT (name) DO UPDATE
                        SET content = EXCLUDED.content,
                            version = {table}.version + 1,
                            updated_at = now()
                    """
                ),
                {"n": res_name, "c": content},
            )
            count += 1
    return count


async def _register_client(db: AsyncSession, schema_name: str) -> None:
    """登记物理设备与资源档案（manifest 依 client_profiles 解析各资源 name）。"""
    async with db.begin_nested():
        await db.execute(text(f'SET search_path TO "{schema_name}"'))
        await db.execute(
            text(
                """
                INSERT INTO clients (uid, client_id, mac, registered_at)
                VALUES (:uid, :cid, '', now())
                ON CONFLICT (uid) DO UPDATE
                    SET client_id = EXCLUDED.client_id
                """
            ),
            {"uid": CLIENT_UID, "cid": CLIENT_UID},
        )
        await db.execute(
            text(
                """
                INSERT INTO client_profiles
                    (client_id, class_plan, time_layout, subjects,
                     default_settings, policy, components, credentials, updated_at)
                VALUES (:cid, 'default_classplan', 'default_timelayout', 'default',
                        'default', 'default', 'default', 'default', now())
                ON CONFLICT (client_id) DO UPDATE
                    SET class_plan = EXCLUDED.class_plan,
                        time_layout = EXCLUDED.time_layout,
                        subjects = EXCLUDED.subjects,
                        default_settings = EXCLUDED.default_settings,
                        policy = EXCLUDED.policy,
                        components = EXCLUDED.components,
                        credentials = EXCLUDED.credentials,
                        updated_at = now()
                """
            ),
            {"cid": CLIENT_UID},
        )
        await db.execute(text("SET search_path TO public"))


async def _enqueue_data_updated(db: AsyncSession, schema_name: str) -> None:
    """预置一条 DataUpdated pending 命令，供插件轮询取走验证闭环。"""
    async with db.begin_nested():
        await db.execute(text(f'SET search_path TO "{schema_name}"'))
        await db.execute(
            text(
                """
                INSERT INTO command_queue (client_id, command_type, payload, status, created_at)
                VALUES (:cid, 'DataUpdated', '', 'pending', now())
                """
            ),
            {"cid": CLIENT_UID},
        )
        await db.execute(text("SET search_path TO public"))


async def _verify(_db: AsyncSession, schema_name: str) -> None:
    """用独立会话读取，确保校验的是已提交的数据（避免同会话可见未提交数据造成假象）。"""
    from app.models.engine import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await db.execute(text(f'SET search_path TO "{schema_name}"'))
        for _, table, res_name in RESOURCE_MAP:
            r = await db.execute(
                text(f"SELECT count(*), coalesce(max(length(content)),0) FROM {table} WHERE name = :n"),
                {"n": res_name},
            )
            n, size = r.fetchone()
            print(f"[info] {table:<18} name={res_name:<20} rows={n} content={size}B")
        r = await db.execute(text("SELECT client_id FROM client_profiles"))
        print(f"[info] client_profiles: {[x[0] for x in r.fetchall()]}")
        r = await db.execute(
            text(
                "SELECT id, client_id, command_type, status "
                "FROM command_queue ORDER BY id DESC LIMIT 5"
            )
        )
        for row in r.fetchall():
            m = row._mapping
            print(
                f"[info] command id={m.get('id')} client={m.get('client_id')} "
                f"type={m.get('command_type')} status={m.get('status')}"
            )
        await db.execute(text("SET search_path TO public"))


if __name__ == "__main__":
    asyncio.run(main())
