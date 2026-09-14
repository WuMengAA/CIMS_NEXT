"""班级课表「手动添加」与「从官方档案导入」路由（挂在 /class/* 下）。

为什么单独成文件：这两组接口的共同职责是**产出官方 ClassIsland 集控格式的资源**，
和 class_routes.py 里的 CRUD / 命令广播关注点不同。官方格式的细节集中在
app/services/schedule_importer.py，这里只做编排与落库。

接口：
    POST /class/{class_id}/apply-week-template   铺一张空周课表（6 天骨架，课时待填）
    GET  /class/{class_id}/schedule              读回某班课表（按星期汇总科目名）
    POST /class/import-from-profile              从 ClassIsland 官方档案一次导入 N 个班

三者产出的 ClassPlan / TimeLayout / Subjects 一律是官方 Profile 信封：

    ClassPlan  → {"ClassPlans": {...}, "ClassPlanGroups": {...}}
    TimeLayout → {"TimeLayouts": {...}}
    Subjects   → {"Subjects": {...}}

若返回裸对象，客户端 `Profile.ClassPlans` 会解析为空 → 课表静默不显示（比 404 更难查）。
"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.command.model_map import MODEL_MAP
from app.api.command.timetable_validator import validate_classplan_references
from app.models.class_model import Class, ClassResourceSet
from app.models.session import get_db
from app.services.schedule_importer import (
    SUBJECTS_RESOURCE_NAME,
    TIME_LAYOUT_RESOURCE_NAME,
    assign_device_to_class,
    build_empty_week_class_plan_resource,
    class_resource_name,
    create_class_records,
    infer_weekday_layout_map,
    parse_official_profile,
    plan_class_imports,
    upsert_resource,
    validate_import,
    write_shared_resources,
)

router = APIRouter()

WEEKDAY_CN = {0: "周日", 1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六"}


def _now():
    return datetime.now(timezone.utc)


def _read_content(content) -> dict:
    if not content:
        return {}
    if isinstance(content, dict):
        return content
    try:
        obj = json.loads(content)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


async def _read_class(db: AsyncSession, class_id: str) -> tuple[Class, ClassResourceSet]:
    cls = (await db.execute(select(Class).where(Class.id == class_id))).scalar_one_or_none()
    if cls is None:
        raise HTTPException(404, f"班级 {class_id} 不存在")
    crs = (
        await db.execute(
            select(ClassResourceSet).where(ClassResourceSet.resource_set_id == cls.resource_set_id)
        )
    ).scalar_one_or_none()
    if crs is None:
        raise HTTPException(404, f"班级 {class_id} 的资源集不存在")
    return cls, crs


async def _put_resource(db: AsyncSession, resource_type: str, name: str, payload: dict) -> int:
    """写一份资源（薄封装，落到 schedule_importer.upsert_resource）。"""
    return await upsert_resource(db, resource_type, name, payload)


def _guess_class_plan_name(class_id: str) -> str:
    """由班级 id 推资源名：class_03 -> cp_class03；其它自定义 id 用 cp_<id>。"""
    if class_id.startswith("class_"):
        try:
            return class_resource_name(int(class_id.split("_")[-1]))
        except ValueError:
            pass
    return f"cp_{class_id}"


# --------------------------------------------------------------------------- #
# 手动添加
# --------------------------------------------------------------------------- #


@router.post("/{class_id}/apply-week-template")
async def apply_week_template(
    class_id: str,
    label: str = "",
    layout_by_weekday: dict = Body(default={}),
    db: AsyncSession = Depends(get_db),
):
    """给班级铺一张**空周课表**（6 天骨架，课时留空待填）。

    「手动添加课表」的入口：不依赖任何档案导入，纯手工起一张周课表。
    - `label`：课表名（缺省用班级名）
    - `layout_by_weekday`：可选，指定每天用哪份作息，如 {"1": "<guid>", "5": "<guid>"}；
      缺省按班级作息资源里 TimeLayouts 的声明次序映射（第 1 份给周一~周四、
      第 2 份给周五、第 3 份给周日），不足以覆盖时统一用第 1 份。

    产出官方 Profile 信封，随后即可用
    `POST /class/{class_id}/resource/ClassPlan/write` 逐天填课时。
    """
    cls, crs = await _read_class(db, class_id)
    label = label or cls.name or class_id

    tl_model = MODEL_MAP["TimeLayout"]
    tl_row = (await db.execute(select(tl_model).where(tl_model.name == crs.time_layout))).scalar_one_or_none()
    layouts = _read_content(tl_row.content if tl_row else "").get("TimeLayouts") or {}
    if not layouts:
        raise HTTPException(
            422,
            f"班级作息资源 '{crs.time_layout}' 里没有 TimeLayouts（需先写入作息资源，"
            f"可直接复用全校资源 '{TIME_LAYOUT_RESOURCE_NAME}'）",
        )

    if layout_by_weekday:
        mapping = {int(k): str(v) for k, v in layout_by_weekday.items()}
        unknown = [v for v in mapping.values() if v.lower() not in {k.lower() for k in layouts}]
        if unknown:
            raise HTTPException(422, f"指定的作息不存在于 '{crs.time_layout}'：{unknown}")
    else:
        # 按作息名的「周X」提示推断（周1234 / 周5 / 周日 之类），推断不到的按课时数兜底。
        # 注意别用字典插入序——TimeLayouts 在档案里是乱序字典。
        mapping = infer_weekday_layout_map(layouts)

    layout_classes = {
        tl_id: sum(1 for i in (tl or {}).get("Layouts") or [] if i.get("TimeType") == 0)
        for tl_id, tl in layouts.items()
    }

    resource = build_empty_week_class_plan_resource(
        label, mapping, layout_classes=layout_classes
    )
    res_name = _guess_class_plan_name(class_id)
    await validate_classplan_references(db, resource, crs.time_layout, crs.subjects)

    version = await _put_resource(db, "ClassPlan", res_name, resource)
    crs.class_plan = res_name
    crs.updated_at = _now()
    db.add(crs)
    await db.commit()

    return {
        "status": "success",
        "class_id": class_id,
        "class_plan": res_name,
        "version": version,
        "weekdays": [WEEKDAY_CN[k] for k in sorted(mapping)],
        "classes_per_day": {WEEKDAY_CN[k]: layout_classes.get(v, 0) for k, v in mapping.items()},
        "message": f"已为 {label} 铺好空周课表（{res_name}），接下来逐天写入课时即可",
    }


@router.get("/{class_id}/schedule")
async def get_class_schedule(class_id: str, db: AsyncSession = Depends(get_db)):
    """读回某班课表（按星期汇总科目名），供面板展示、手工编辑前对账。"""
    cls, crs = await _read_class(db, class_id)

    cp_model = MODEL_MAP["ClassPlan"]
    cp_row = (await db.execute(select(cp_model).where(cp_model.name == crs.class_plan))).scalar_one_or_none()
    content = _read_content(cp_row.content if cp_row else "")
    plans = content.get("ClassPlans") or {}

    sub_model = MODEL_MAP["Subjects"]
    sub_row = (await db.execute(select(sub_model).where(sub_model.name == crs.subjects))).scalar_one_or_none()
    subjects = _read_content(sub_row.content if sub_row else "").get("Subjects") or {}
    # 科目 guid 大小写归一（官方是小写键；历史数据可能是大写）
    subj_by_id = {str(k).lower(): v for k, v in subjects.items() if isinstance(v, dict)}

    days = []
    for guid, plan in plans.items():
        if not isinstance(plan, dict):
            continue
        names = []
        for ci in plan.get("Classes") or []:
            meta = subj_by_id.get(str(ci.get("SubjectId") or "").lower()) or {}
            names.append(meta.get("Name") or "")
        wd = int((plan.get("TimeRule") or {}).get("WeekDay", 0))
        days.append(
            {
                "weekday": wd,
                "weekday_name": WEEKDAY_CN.get(wd, str(wd)),
                "plan_id": guid,
                "name": plan.get("Name", ""),
                "time_layout_id": str(plan.get("TimeLayoutId") or ""),
                "subjects": names,
            }
        )
    days.sort(key=lambda d: d["weekday"])

    return {
        "class_id": class_id,
        "class_name": cls.name,
        "class_plan": crs.class_plan,
        "time_layout": crs.time_layout,
        "subjects": crs.subjects,
        "format": "official-profile-envelope" if plans else (
            "single-classplan" if content.get("Classes") else "empty"
        ),
        "days": days,
    }


# --------------------------------------------------------------------------- #
# 从官方档案导入
# --------------------------------------------------------------------------- #


@router.post("/import-from-profile")
async def import_from_profile(
    profile: dict = Body(...),
    classes: str = "",
    assign: str = "",
    reset: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """从一份 ClassIsland 官方档案（Profile）导入 N 个班级的真实课表。

    - `profile`：官方档案 JSON（如 Profiles/Default.json 的内容）
    - `classes`：只导入指定班号，如 "1,3,8"（缺省全部）
    - `assign`：设备绑定，如 "lab-pc-001=1,lab-pc-002=2"
    - `reset`：先清空本租户既有班级与班级资源集，并解除设备归属

    产出：全校共享 `tl_school` / `sub_school` + 每班一份 `cp_classNN`，全为官方信封格式。
    每个班的课表 AssociatedGroup 会被改写为官方「默认课表群」——集控通道下发不了
    `SelectedClassPlanGroupId`，只有落在默认群才会被客户端真正激活。
    """
    try:
        parsed = parse_official_profile(profile)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    wanted = [int(x) for x in classes.split(",") if x.strip()] if classes else None
    imports = plan_class_imports(parsed, wanted)
    if not imports:
        raise HTTPException(400, "档案里没有匹配的班级可导入")

    issues = validate_import(parsed, imports)
    if issues:
        raise HTTPException(422, "；".join(issues))

    if reset:
        from app.models.database import CPFile, SubFile, TLFile

        keeps = {
            CPFile: {"default_classplan"} | {i.class_plan_name for i in imports},
            TLFile: {TIME_LAYOUT_RESOURCE_NAME, "default_timelayout"},
            SubFile: {SUBJECTS_RESOURCE_NAME, "default"},
        }
        for model, keep in keeps.items():
            rows = (await db.execute(select(model))).scalars().all()
            for row in rows:
                if row.name not in keep:
                    await db.delete(row)
        await db.execute(ClientProfile.__table__.update().values(class_id=""))
        for row in (await db.execute(select(Class))).scalars().all():
            await db.delete(row)
        for row in (await db.execute(select(ClassResourceSet))).scalars().all():
            await db.delete(row)
        await db.flush()

    await write_shared_resources(db, parsed)

    for imp in imports:
        await create_class_records(db, imp)

    assigned = []
    for pair in [p for p in assign.split(",") if p.strip()]:
        if "=" not in pair:
            continue
        client_id, idx = pair.split("=", 1)
        ok = await assign_device_to_class(db, client_id.strip(), int(idx))
        if ok:
            assigned.append({"client_id": client_id.strip(), "class_id": f"class_{int(idx):02d}"})

    await db.commit()
    return {
        "status": "success",
        "classes_imported": [
            {"index": i.index, "class_id": i.class_id, "name": i.name, "class_plan": i.class_plan_name}
            for i in imports
        ],
        "time_layout_resource": TIME_LAYOUT_RESOURCE_NAME,
        "subjects_resource": SUBJECTS_RESOURCE_NAME,
        "assigned": assigned,
        "groups_total": len(parsed.classes),
        "unused_groups": len(parsed.unused_groups),
        "message": f"已导入 {len(imports)} 个班（官方 Profile 信封格式）",
    }
