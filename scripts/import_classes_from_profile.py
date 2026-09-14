"""从 ClassIsland 官方档案导入「N 个班级的真实课表」到 CIMS 租户。

做三件事（可重复执行，幂等）：

1. `--reset`：清空租户里既有的班级 / 班级资源集 / 残留测试资源行（*_test、plan_3p1 …），
   并把设备从旧班级上摘下来（client_profiles.class_id 清空），随后按 `--assign` 重新分派。
2. 写入**全校共享**的两份资源：`tl_school`（作息）、`sub_school`（科目），
   均为官方 Profile 信封形状（见 app/services/schedule_importer.py 的说明）。
3. 逐班写入 `cp_classNN`（Profile 信封：ClassPlans + ClassPlanGroups）、`classes` 行、
   `class_resource_sets` 行；班级资源集里 time_layout/subjects 都指向全校共享资源，
   class_plan 指向本班的 cp_classNN。

并把 `default_classplan` / `default_timelayout` / `default` 三份兜底资源刷成**合法空信封**，
使「未绑定班级」的设备既拿不到错课表，也不会因 404 触发 CCProtect 自封 IP。

用法（在 CIMS-backend 目录）：
    PYTHONPATH=. .venv/Scripts/python.exe scripts/import_classes_from_profile.py --reset
    PYTHONPATH=. .venv/Scripts/python.exe scripts/import_classes_from_profile.py \
        --profile "D:\\Classlsland\\data\\Profiles\\Default.json" \
        --slug demo-class --assign lab-pc-001=1 --reset
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.services.schedule_importer import (  # noqa: E402
    SUBJECTS_RESOURCE_NAME,
    TIME_LAYOUT_RESOURCE_NAME,
    DEFAULT_GROUP_GUID,
    GLOBAL_GROUP_GUID,
    build_subjects_resource,
    build_time_layout_resource,
    parse_official_profile,
    plan_class_imports,
    validate_import,
    WEEKDAY_CN,
)

DEFAULT_PROFILE = r"D:\Classlsland\data\Profiles\Default.json"

# 兜底资源名（与 app/api/client/manifest.py 的默认值一致）
FALLBACK_NAMES = {
    "cp_files": "default_classplan",
    "tl_files": "default_timelayout",
    "sub_files": "default",
}


def _load_profile(path: str) -> dict:
    with io.open(path, encoding="utf-8-sig") as f:
        return json.load(f)


async def _upsert(db, table: str, name: str, content: dict) -> None:
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
        {"n": name, "c": json.dumps(content, ensure_ascii=False)},
    )


async def _reset(db, keep_cp: set[str], keep_tl: set[str], keep_sub: set[str]) -> dict:
    """清空班级层与残留测试资源，返回清理统计。"""
    stat = {}
    for table, keep in (
        ("cp_files", keep_cp),
        ("tl_files", keep_tl),
        ("sub_files", keep_sub),
    ):
        r = await db.execute(text(f"SELECT name FROM {table}"))
        existing = [row[0] for row in r.fetchall()]
        drop = [n for n in existing if n not in keep]
        for n in drop:
            await db.execute(text(f"DELETE FROM {table} WHERE name = :n"), {"n": n})
        stat[table] = drop

    # 设备摘链（先于删班级，避免留下悬空 class_id）
    await db.execute(text("UPDATE client_profiles SET class_id = '' WHERE class_id <> ''"))
    await db.execute(text("DELETE FROM classes"))
    await db.execute(text("DELETE FROM class_resource_sets"))
    return stat


async def _create_class(db, imp, tl_name: str, sub_name: str) -> None:
    await _upsert(db, "cp_files", imp.class_plan_name, imp.class_plan_resource)
    await db.execute(
        text(
            """
            INSERT INTO classes (id, name, resource_set_id, sort_order, created_at, updated_at)
            VALUES (:cid, :name, :cid, :so, now(), now())
            ON CONFLICT (id) DO UPDATE
                SET name = EXCLUDED.name,
                    resource_set_id = EXCLUDED.resource_set_id,
                    sort_order = EXCLUDED.sort_order,
                    updated_at = now()
            """
        ),
        {"cid": imp.class_id, "name": imp.name, "so": imp.index},
    )
    await db.execute(
        text(
            """
            INSERT INTO class_resource_sets
                (resource_set_id, class_plan, time_layout, subjects,
                 default_settings, policy, components, credentials, updated_at)
            VALUES (:cid, :cp, :tl, :sub, 'default', 'default', 'default', 'default', now())
            ON CONFLICT (resource_set_id) DO UPDATE
                SET class_plan = EXCLUDED.class_plan,
                    time_layout = EXCLUDED.time_layout,
                    subjects = EXCLUDED.subjects,
                    updated_at = now()
            """
        ),
        {"cid": imp.class_id, "cp": imp.class_plan_name, "tl": tl_name, "sub": sub_name},
    )


async def _assign(db, client_id: str, index: int) -> bool:
    cid = f"class_{index:02d}"
    r = await db.execute(
        text(
            "UPDATE client_profiles SET class_id = :cid WHERE client_id = :c "
            "RETURNING client_id"
        ),
        {"cid": cid, "c": client_id},
    )
    return r.fetchone() is not None


async def _verify(db, tl_name: str, sub_name: str) -> None:
    """独立会话复核：班级数、资源行、悬空引用。"""
    print("\n" + "=" * 78)
    print("校验")
    print("=" * 78)
    r = await db.execute(
        text(
            """
            SELECT c.id, c.name, c.resource_set_id,
                   (SELECT count(*) FROM client_profiles p WHERE p.class_id = c.id) AS devs,
                   s.class_plan, s.time_layout, s.subjects
            FROM classes c LEFT JOIN class_resource_sets s ON s.resource_set_id = c.resource_set_id
            ORDER BY c.sort_order, c.id
            """
        )
    )
    rows = r.fetchall()
    print(f"班级数: {len(rows)}")
    dangling = 0
    for row in rows:
        cid, name, rsid, devs, cp, tl, sub = row
        flags = []
        for table, nm in (("cp_files", cp), ("tl_files", tl), ("sub_files", sub)):
            n = (
                await db.execute(
                    text(f"SELECT count(*) FROM {table} WHERE name = :n"), {"n": nm}
                )
            ).scalar()
            if not n:
                flags.append(f"{table}:{nm} 悬空")
                dangling += 1
        mark = "OK " if not flags else "BAD"
        print(
            f"  [{mark}] {cid:<10} {name:<6} 资源集={rsid:<10} 设备={devs} "
            f"cp={cp} tl={tl} sub={sub} {'; '.join(flags)}"
        )
    print(f"悬空引用: {dangling}")

    for table, nm in (("cp_files", None), ("tl_files", None), ("sub_files", None)):
        r = await db.execute(
            text(f"SELECT name, version, length(content) FROM {table} ORDER BY name")
        )
        items = [f"{a}(v{b},{c}B)" for a, b, c in r.fetchall()]
        print(f"  {table}: {len(items)} 行 -> {', '.join(items)}")

    r = await db.execute(
        text("SELECT client_id, class_id FROM client_profiles ORDER BY client_id")
    )
    for cid, cid2 in r.fetchall():
        print(f"  设备 {cid} -> 班级 {cid2 or '(未绑定)'}")


def _print_plan(parsed, imports) -> None:
    print("=" * 78)
    print(f"档案解析：{len(parsed.classes)} 个班 / 作息 {len(parsed.time_layouts)} 份 / "
          f"科目 {len(parsed.subjects)} 个")
    print(f"未挂载课表的空群: {len(parsed.unused_groups)} 个")
    print("=" * 78)
    by_index = {b.index: b for b in parsed.classes}
    for imp in imports:
        block = by_index[imp.index]
        print(f"\n{imp.name}  ({imp.class_id} / {imp.class_plan_name})")
        for wd in block.weekdays:
            guid, plan = block.plans[wd]
            subjects = []
            for ci in plan.get("Classes") or []:
                sid = str(ci.get("SubjectId") or "").lower()
                meta = parsed.subjects.get(sid) or parsed.subjects.get(sid.upper()) or {}
                subjects.append(meta.get("Name") or "·")
            print(f"   {WEEKDAY_CN[wd]}: {' '.join(subjects) or '(空)'}")


async def main() -> int:
    ap = argparse.ArgumentParser(description="从 ClassIsland 官方档案导入班级课表")
    ap.add_argument("--profile", default=DEFAULT_PROFILE, help="官方档案 JSON 路径")
    ap.add_argument("--slug", default="demo-class", help="租户 slug")
    ap.add_argument("--classes", default="", help="只导入指定班号，如 1,3,8（缺省全部）")
    ap.add_argument("--assign", action="append", default=[], help="设备绑定：CLIENT_UID=班号")
    ap.add_argument("--reset", action="store_true", help="先清空既有班级与残留测试资源")
    ap.add_argument("--dry-run", action="store_true", help="只解析与自检，不写库")
    args = ap.parse_args()

    profile = _load_profile(args.profile)
    parsed = parse_official_profile(profile)
    indices = (
        [int(x) for x in args.classes.split(",") if x.strip()] if args.classes else None
    )
    imports = plan_class_imports(parsed, indices)
    if not imports:
        print("[error] 没有可导入的班级"); return 2

    _print_plan(parsed, imports)

    issues = validate_import(parsed, imports)
    if issues:
        print("\n[自检] 发现问题：")
        for i in issues:
            print("  -", i)
        if args.dry_run:
            return 3
    else:
        print("\n[自检] 通过：作息/科目引用完整，星期覆盖完整。")

    if args.dry_run:
        print("[dry-run] 未写库。")
        return 0

    tl_resource = build_time_layout_resource(parsed.time_layouts)
    sub_resource = build_subjects_resource(parsed.subjects)

    keep_tl = {TIME_LAYOUT_RESOURCE_NAME, "default_timelayout"}
    keep_sub = {SUBJECTS_RESOURCE_NAME, "default"}
    keep_cp = {"default_classplan"} | {i.class_plan_name for i in imports}

    from app.models.engine import AsyncSessionLocal

    schema = f"tenant_{args.slug}"
    async with AsyncSessionLocal() as db:
        await db.execute(text(f'SET search_path TO "{schema}"'))

        if args.reset:
            stat = await _reset(db, keep_cp, keep_tl, keep_sub)
            print("\n[reset] 清理：")
            for t, dropped in stat.items():
                print(f"  {t}: 删除 {len(dropped)} 行 {dropped if dropped else ''}")
            print("  classes / class_resource_sets: 已清空")

        # 全校共享资源
        await _upsert(db, "tl_files", TIME_LAYOUT_RESOURCE_NAME, tl_resource)
        await _upsert(db, "sub_files", SUBJECTS_RESOURCE_NAME, sub_resource)

        # 兜底资源：合法空信封，避免未绑定设备拿到错课表或撞 404
        empty_cp = {
            "Name": "未绑定班级",
            "ClassPlans": {},
            "ClassPlanGroups": {
                DEFAULT_GROUP_GUID: {"Name": "默认", "IsGlobal": False},
                GLOBAL_GROUP_GUID: {"Name": "全局课表群", "IsGlobal": True},
            },
        }
        await _upsert(db, "cp_files", "default_classplan", empty_cp)
        await _upsert(db, "tl_files", "default_timelayout", tl_resource)
        await _upsert(db, "sub_files", "default", sub_resource)

        # 逐班
        for imp in imports:
            await _create_class(db, imp, TIME_LAYOUT_RESOURCE_NAME, SUBJECTS_RESOURCE_NAME)

        # 设备分派
        for spec in args.assign:
            if "=" not in spec:
                print(f"[warn] --assign 格式应为 CLIENT=班号，忽略 {spec}")
                continue
            client_id, idx = spec.split("=", 1)
            ok = await _assign(db, client_id.strip(), int(idx))
            print(f"[assign] {client_id.strip()} -> 班{idx.strip()} {'OK' if ok else '设备不存在'}")

        await db.commit()
        print(f"\n[ok] 已提交：{len(imports)} 个班 + 全校作息/科目 + 兜底资源")

    async with AsyncSessionLocal() as db:
        await db.execute(text(f'SET search_path TO "{schema}"'))
        await _verify(db, TIME_LAYOUT_RESOURCE_NAME, SUBJECTS_RESOURCE_NAME)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
