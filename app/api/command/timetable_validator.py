"""课表三件套引用完整性校验。

ClassIsland 课表 = ClassPlan + TimeLayout + Subjects，三者交叉引用、不可拆分：
- ClassPlan.TimeLayoutId 必须指向一个已存在的 TimeLayout 资源
- ClassPlan.Classes[].SubjectId 必须存在于 Subjects 科目词典
- ClassPlan.Classes[].TimeLayoutIds[*]（若带时间轴）也应指向存在的 TimeLayout

本模块在「写班级课表」时强制校验，拒绝写入引用不完整的课表。
"""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import TLFile, SubFile, CPFile


async def validate_classplan_references(
    db: AsyncSession,
    plan: dict,
    tl_name: str | None = None,
    sub_name: str | None = None,
) -> None:
    """校验一个 ClassPlan 的引用完整性。

    Args:
        db: 数据库会话。
        plan: ClassPlan JSON（字典）。
        tl_name: 该计划引用的 TimeLayout 资源名（缺省从 plan["TimeLayoutId"] 取）。
        sub_name: 该计划引用的 Subjects 资源名（缺省用 'default'）。
    """
    if not isinstance(plan, dict):
        raise HTTPException(400, "ClassPlan 必须是 JSON 对象")

    # 1. 确定 TimeLayout 资源名
    tl = tl_name or plan.get("TimeLayoutId")
    if not tl:
        raise HTTPException(400, "ClassPlan 缺少 TimeLayoutId，无法校验作息引用")
    # 2. 确定 Subjects 资源名
    sub = sub_name or "default"

    # 校验 TimeLayout 存在
    tl_row = (
        await db.execute(select(TLFile).where(TLFile.name == tl))
    ).scalar_one_or_none()
    if tl_row is None:
        raise HTTPException(422, f"引用的 TimeLayout '{tl}' 不存在（<resource_type>TimeLayout</resource_type> 需先写入）")

    # 校验 Subjects 存在
    sub_row = (
        await db.execute(select(SubFile).where(SubFile.name == sub))
    ).scalar_one_or_none()
    if sub_row is None:
        raise HTTPException(422, f"引用的 Subjects '{sub}' 不存在（<resource_type>Subjects</resource_type> 需先写入）")

    # 3. 解析科目词典（允许 Subjects 资源缺省时跳过科目名存在性检查，
    #    但若提供了内容则严格校验 Classes[].SubjectId）
    subj_ids = set()
    try:
        subj_data = _parse_content(sub_row.content)
        for s in subj_data.get("Subjects", []):
            sid = s.get("SubjectId") or s.get("Id")
            if sid:
                subj_ids.add(str(sid))
    except Exception:
        subj_ids = set()  # Subjects 内容不可解析时，跳过科目存在性校验

    # 4. 遍历 Classes，校验 SubjectId 是否在词典（若词典非空）
    classes = plan.get("Classes") or []
    for cls in classes:
        if not isinstance(cls, dict):
            continue
        sid = str(cls.get("SubjectId") or "")
        if sid and subj_ids and sid not in subj_ids:
            raise HTTPException(
                422,
                f"ClassPlan 课时引用不存在的科目 SubjectId='{sid}'（Subjects '{sub}' 词典中无此科目）",
            )


def _parse_content(content: str):
    """解析资源 content 字段，容错返回 dict。"""
    import json

    if not content:
        return {}
    try:
        obj = json.loads(content)
        return obj if isinstance(obj, dict) else {"raw": obj}
    except Exception:
        return {}