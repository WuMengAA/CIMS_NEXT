"""课表三件套引用完整性校验。

ClassIsland 课表 = ClassPlan + TimeLayout + Subjects，三者交叉引用、不可拆分：
- ClassPlan.TimeLayoutId 必须指向该班作息资源里确实存在的 TimeLayout
- ClassPlan.Classes[].SubjectId 必须存在于该校科目资源里确实存在的科目词典
- TimeLayout.Layouts[].DefaultClassId（若存在）也应能在科目词典里找到

**官方格式是本模块的兼容基线**（详见 app/services/schedule_importer.py 的模块说明）：
三类资源的载荷都是「档案（Profile）信封」，字典键为 GUID：

    ClassPlan  → {"ClassPlans": {...}, "ClassPlanGroups": {...}}
    TimeLayout → {"TimeLayouts": {...}}
    Subjects   → {"Subjects": {...}}

同时保留对「裸对象」历史写法的兼容（早期种子是单个 ClassPlan / 单个 TimeLayout），
避免老数据写不进来。
"""

import json

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import TLFile, SubFile


def _parse_content(content) -> dict:
    """解析资源 content 字段，容错返回 dict。"""
    if not content:
        return {}
    if isinstance(content, dict):
        return content
    try:
        obj = json.loads(content)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _layout_ids(tl_content: dict) -> set[str]:
    """取出作息资源里的全部 TimeLayout GUID。

    信封格式取 `TimeLayouts` 的键；裸对象格式（单份作息）没有 GUID 可指认，
    返回空集合表示「无法校验」——调用方据此跳过该层校验而不是误拒。
    """
    ids: set[str] = set()
    layouts = tl_content.get("TimeLayouts")
    if isinstance(layouts, dict):
        ids.update(str(k).lower() for k in layouts.keys())
    elif isinstance(layouts, list):
        for item in layouts:
            if isinstance(item, dict) and item.get("Id"):
                ids.add(str(item["Id"]).lower())
    return ids


def _subject_ids(sub_content: dict) -> set[str]:
    """取出科目资源里的全部科目 GUID。

    官方格式是 `{"Subjects": {"<guid>": {...}}}`（GUID 作键）；
    兼容历史数组写法 `{"Subjects": [{"SubjectId": ...}]}`。
    """
    ids: set[str] = set()
    subs = sub_content.get("Subjects")
    if isinstance(subs, dict):
        ids.update(str(k).lower() for k in subs.keys())
    elif isinstance(subs, list):
        for item in subs:
            if isinstance(item, dict):
                sid = item.get("SubjectId") or item.get("Id")
                if sid:
                    ids.add(str(sid).lower())
    return ids


def _iter_plans(plan: dict):
    """把「信封」或「裸 ClassPlan」统一成 (guid, ClassPlan) 迭代。"""
    plans = plan.get("ClassPlans")
    if isinstance(plans, dict) and plans:
        for guid, item in plans.items():
            if isinstance(item, dict):
                yield str(guid), item
        return
    # 裸 ClassPlan
    if "Classes" in plan or "TimeLayoutId" in plan:
        yield "(single)", plan


async def validate_classplan_references(
    db: AsyncSession,
    plan: dict,
    tl_name: str | None = None,
    sub_name: str | None = None,
) -> None:
    """校验一份 ClassPlan 资源（信封或裸对象）的引用完整性。

    Args:
        db: 数据库会话。
        plan: ClassPlan 资源内容（官方信封或裸 ClassPlan）。
        tl_name: 该校作息资源名（缺省取第一个 plan 的 TimeLayoutId，仅用于报错定位）。
        sub_name: 该校科目资源名（缺省 'default'）。
    """
    if not isinstance(plan, dict):
        raise HTTPException(400, "ClassPlan 必须是 JSON 对象")

    sub = sub_name or "default"

    # ---- 科目词典先加载（TimeLayout 的 DefaultClassId 也要用它兜底）----
    sub_row = (
        await db.execute(select(SubFile).where(SubFile.name == sub))
    ).scalar_one_or_none()
    if sub_row is None:
        raise HTTPException(
            422,
            f"引用的 Subjects '{sub}' 不存在（<resource_type>Subjects</resource_type> 需先写入）",
        )
    subject_ids = _subject_ids(_parse_content(sub_row.content))

    # ---- 作息：优先按资源名校验；资源名缺失时退化为「只看类里有没有这份作息」----
    tl = tl_name
    if not tl:
        for _, item in _iter_plans(plan):
            tl = item.get("TimeLayoutId")
            if tl:
                break
    layout_ids: set[str] = set()
    if tl:
        tl_row = (
            await db.execute(select(TLFile).where(TLFile.name == tl))
        ).scalar_one_or_none()
        if tl_row is None:
            raise HTTPException(
                422,
                f"引用的 TimeLayout '{tl}' 不存在（<resource_type>TimeLayout</resource_type> 需先写入）",
            )
        layout_ids = _layout_ids(_parse_content(tl_row.content))

    # ---- 逐份课表校验 ----
    for guid, item in _iter_plans(plan):
        tlid = str(item.get("TimeLayoutId") or "").lower()
        # layout_ids 非空才做存在性判定（裸作息资源没有 GUID 可指认时无法判定）
        if tlid and layout_ids and tlid not in layout_ids:
            raise HTTPException(
                422,
                f"ClassPlan({guid[:8]}) 引用了作息 '{tl}' 中不存在的 TimeLayoutId='{tlid}'",
            )

        for cls in item.get("Classes") or []:
            if not isinstance(cls, dict):
                continue
            sid = str(cls.get("SubjectId") or "").lower()
            # SubjectId 为 Guid.Empty 是官方「未填科目」的合法写法，跳过
            if not sid or sid == "00000000-0000-0000-0000-000000000000":
                continue
            if subject_ids and sid not in subject_ids:
                raise HTTPException(
                    422,
                    f"ClassPlan({guid[:8]}) 课时引用不存在的科目 SubjectId='{sid}'"
                    f"（Subjects '{sub}' 词典中无此科目）",
                )
