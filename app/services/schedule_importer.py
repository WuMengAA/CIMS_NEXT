"""ClassIsland 官方档案（Profile）→ CIMS 班级课表资源 导入器。

对齐 ClassIsland 官方集控（CIMS）协议的资源格式（**这是本模块存在的根本原因**）：

官方客户端 `ClassIsland/Services/ProfileService.cs#MergeManagementProfileAsync` 拉取资源时是
这样反序列化的：

    var cpNew = await Connection.GetJsonAsync<Profile>(Manifest.ClassPlanSource.Value!);
    MergeDictionary(Profile.ClassPlans,       cpOld.ClassPlans,       cpNew.ClassPlans);
    MergeDictionary(Profile.ClassPlanGroups,  cpOld.ClassPlanGroups,  cpNew.ClassPlanGroups);

    var tlNew = await Connection.GetJsonAsync<Profile>(Manifest.TimeLayoutSource.Value!);
    MergeDictionary(Profile.TimeLayouts, tlOld.TimeLayouts, tlNew.TimeLayouts);

    var subNew = await Connection.GetJsonAsync<Profile>(Manifest.SubjectsSource.Value!);
    MergeDictionary(Profile.Subjects, subjectOld.Subjects, subjectNew.Subjects);

即：**ClassPlan / TimeLayout / Subjects 三类资源，服务端返回的必须是一个「档案（Profile）
信封对象」**——顶层是 PascalCase 的字典属性：

    ClassPlan   → { "ClassPlans": { "<guid>": {...} }, "ClassPlanGroups": { "<guid>": {...} } }
    TimeLayout  → { "TimeLayouts": { "<guid>": {...} } }
    Subjects    → { "Subjects": { "<guid>": {...} } }

若直接返回单个 ClassPlan / 单个 TimeLayout 对象（早期 CIMS 种子的做法），客户端
`Profile.ClassPlans` 会解析为空字典，结果「拉到了资源但一节课都不显示」——
静默失效，比 404 更难排查。

另外两处官方硬约束（决定了本模块的产出形状）：

1. `ClassPlan.AssociatedGroup` 必须落在「默认课表群」或「全局课表群」才会被
   `LessonsService.CheckClassPlan` / `GetClassPlanByDate` 选中：

       if (plan.AssociatedGroup != ClassPlanGroup.GlobalGroupGuid &&
           plan.AssociatedGroup != Profile.SelectedClassPlanGroupId &&
           plan.AssociatedGroup != Profile.TempClassPlanGroupId) return false;

   而 `SelectedClassPlanGroupId` 存在于本地档案里，**集控通道无法下发**（合并时只拷
   ClassPlans / ClassPlanGroups）。所以「按班级下发」时，必须把该班课表的
   AssociatedGroup 改写到默认群（ACAF4EF0-…），否则设备拉到课表也不会激活。
   每个班级一份档案、档案里只有自己班的 6 天课表，默认群即唯一有效群，语义自洽。

2. `ClassPlanGroup.DefaultGroupGuid = ACAF4EF0-E261-4262-B941-34EA93CB4369`，
   `ClassPlanGroup.GlobalGroupGuid = 00000000-0000-0000-0000-000000000000`。

时间表与科目则相反：**全校共享一份**即可（同一作息 + 同一科目词典），
12 个班级的 ClassPlan 都指向同一个 TimeLayout 资源名 / Subjects 资源名。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

# ---- 官方常量（禁止改动，来自 ClassIsland.Shared/Models/Profile/ClassPlanGroup.cs）----
DEFAULT_GROUP_GUID = "acaf4ef0-e261-4262-b941-34ea93cb4369"
GLOBAL_GROUP_GUID = "00000000-0000-0000-0000-000000000000"
DEFAULT_GROUP_NAME = "默认"
GLOBAL_GROUP_NAME = "全局课表群"

# WeekDay: 0=周日 … 5=周五（与 C# DayOfWeek 对齐）
WEEKDAY_CN = {0: "周日", 1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六"}

# 资源命名约定（manifest 依此下发）
TIME_LAYOUT_RESOURCE_NAME = "tl_school"
SUBJECTS_RESOURCE_NAME = "sub_school"


def class_resource_name(index: int) -> str:
    """第 index 个班的 ClassPlan 资源名（1 基）。"""
    return f"cp_class{index:02d}"


def class_id_of(index: int) -> str:
    """第 index 个班的班级 id（1 基）。"""
    return f"class_{index:02d}"


# 班级课表群的 UUIDv5 命名空间（固定值，绝不能改——改了会让所有设备认不出既有群）
CLASS_GROUP_NAMESPACE = "6f2d8a41-0c93-4b7e-9d15-8a3c5e7f0b26"


def deterministic_group_guid(index: int) -> str:
    """由班号确定性地派生课表群 GUID（同一班永远得同一个群）。

    用 UUIDv5（基于名字的散列）而非 uuid4：重复导入同一个班时落在同一个群，
    设备端表现为「更新这个群」，而不是每导一次就多出一个空群。
    """
    import uuid

    return str(uuid.uuid5(uuid.UUID(CLASS_GROUP_NAMESPACE), f"stelarith-class-group-{index}"))


def class_group_name(index: int, label: str | None = None) -> str:
    """第 index 个班的课表群显示名（与 build_class_plan_resource 里写的保持一致）。"""
    return f"{label or default_class_label(index)}课表群"


def default_class_label(index: int) -> str:
    """第 index 个班的默认班名（1 基）。官方档案里 12 个群同名「新课表群」，无从区分，
    故按档案中的出现次序编号；导入后可在面板改名。"""
    return f"{index}班"


# 作息名里的「周X」提示，用于推断「哪天用哪份作息」
_DAY_CHAR = {"日": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}


def _layout_day_hint(name: str) -> set[int]:
    """从作息名推断它覆盖哪些星期，如「周1234(高二通用)」→ {1,2,3,4}。"""
    if not name:
        return set()
    m = re.search(r"周([0-9一二三四五六日、,，]+)", name)
    if not m:
        return set()
    days: set[int] = set()
    for ch in m.group(1):
        if ch.isdigit():
            v = int(ch)
            if 0 <= v <= 6:
                days.add(v)
        elif ch in _DAY_CHAR:
            days.add(_DAY_CHAR[ch])
    return days


def _layout_class_count(tl: dict | None) -> int:
    """该作息里有几节「上课」（TimeType==0）。"""
    return sum(1 for i in (tl or {}).get("Layouts") or [] if i.get("TimeType") == 0)


def infer_weekday_layout_map(layouts: dict[str, dict]) -> dict[int, str]:
    """推断「周日/周一…周五」各该用哪份作息。

    优先按作息名的「周X」提示（真实档案基本都是这么命名的，如 周1234 / 周5 / 周日）；
    提示覆盖不到的星期再按课时数从多到少兜底（周一~周四用最多课时的那份、
    周五用次多、周日用第三多）。纯靠字典序或字典插入序都会错——`TimeLayouts`
    在档案里是乱序字典，别依赖顺序。
    """
    if not layouts:
        return {}

    hints = {tid: _layout_day_hint((tl or {}).get("Name") or "") for tid, tl in layouts.items()}

    mapping: dict[int, str] = {}
    for tid, days in hints.items():
        for d in days:
            cur = mapping.get(d)
            # 同时命中时，覆盖天数更多的作息优先（周1234 胜过 周1）
            if cur is None or len(days) >= len(hints.get(cur, set())):
                mapping[d] = tid

    remaining = [d for d in (1, 2, 3, 4, 5, 0) if d not in mapping]
    if remaining:
        by_size = sorted(layouts.keys(), key=lambda t: -_layout_class_count(layouts[t]))
        for d in remaining:
            idx = 0 if d in (1, 2, 3, 4) else (1 if d == 5 else 2)
            mapping[d] = by_size[min(idx, len(by_size) - 1)]

    return mapping


@dataclass
class ClassBlock:
    """从官方档案中切出的「一个班」的课表集合。"""
    index: int  # 1 基序号
    group_id: str  # 原始课表群 GUID
    group_name: str
    # 星期(0-5) -> (原始 plan GUID, ClassPlan 字典)
    plans: dict[int, tuple[str, dict]] = field(default_factory=dict)

    @property
    def weekdays(self) -> list[int]:
        return sorted(self.plans.keys())

    def summary(self) -> str:
        names = {0: "日", 1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六"}
        return " | ".join(
            f"周{names[wd]}:{self.plans[wd][1].get('Name', '')}"
            f"({len(self.plans[wd][1].get('Classes') or [])}节)"
            for wd in self.weekdays
        )


@dataclass
class ProfileParseResult:
    """官方档案解析结果。"""

    classes: list[ClassBlock]
    time_layouts: dict[str, dict]  # 被课表引用到的作息（全校共享）
    subjects: dict[str, dict]  # 全量科目词典
    groups: dict[str, dict]  # 档案中的课表群（原始）
    layout_ids_used: set[str] = field(default_factory=set)
    subject_ids_used: set[str] = field(default_factory=set)
    unused_groups: list[str] = field(default_factory=list)


def parse_official_profile(profile: dict) -> ProfileParseResult:
    """把一个 ClassIsland 档案（Profiles/Default.json 之类）解析成「N 个班 + 共享作息/科目」。

    切班依据：ClassPlan.AssociatedGroup 反向指向 ClassPlanGroups 中的群；
    一个群里 6 个 plan（TimeRule.WeekDay 各不相同）= 该班一周。
    """
    if not isinstance(profile, dict):
        raise ValueError("档案必须是 JSON 对象")

    raw_plans = profile.get("ClassPlans") or {}
    raw_groups = profile.get("ClassPlanGroups") or {}
    raw_layouts = profile.get("TimeLayouts") or {}
    raw_subjects = profile.get("Subjects") or {}

    if not raw_plans:
        raise ValueError("档案缺少 ClassPlans，无法切分班级")

    by_group: dict[str, dict[int, tuple[str, dict]]] = {}
    for guid, plan in raw_plans.items():
        if not isinstance(plan, dict):
            continue
        group = str(plan.get("AssociatedGroup") or DEFAULT_GROUP_GUID).lower()
        weekday = int((plan.get("TimeRule") or {}).get("WeekDay", 0))
        by_group.setdefault(group, {})[weekday] = (guid, plan)

    # 保持档案中 ClassPlanGroups 的声明顺序，只保留真正挂载了课表的群
    order = [g for g in raw_groups.keys() if g.lower() in by_group]
    # 兜底：有课表但群未声明，追加到末尾，保证不漏班
    for g in by_group:
        if g not in [x.lower() for x in order]:
            order.append(g)

    classes: list[ClassBlock] = []
    for i, gid in enumerate(order, start=1):
        meta = raw_groups.get(gid) or {}
        classes.append(
            ClassBlock(
                index=i,
                group_id=gid,
                group_name=str(meta.get("Name") or ""),
                plans=by_group[gid.lower()],
            )
        )

    layout_ids_used: set[str] = set()
    subject_ids_used: set[str] = set()
    for block in classes:
        for _, plan in block.plans.values():
            tl = plan.get("TimeLayoutId")
            if tl:
                layout_ids_used.add(str(tl).lower())
            for ci in plan.get("Classes") or []:
                sid = ci.get("SubjectId")
                if sid and str(sid).lower() != GLOBAL_GROUP_GUID:
                    subject_ids_used.add(str(sid).lower())

    used_lower = {k.lower() for k in raw_layouts}
    time_layouts = {k: v for k, v in raw_layouts.items() if k.lower() in layout_ids_used}
    # 若课表引用的作息在档案里缺失，宁可全量下发（客户端见不到也不至于 404）
    missing = layout_ids_used - used_lower
    if missing:
        time_layouts = dict(raw_layouts)

    unused_groups = [g for g in raw_groups.keys() if g.lower() not in by_group]

    return ProfileParseResult(
        classes=classes,
        time_layouts=time_layouts,
        subjects=dict(raw_subjects),
        groups=dict(raw_groups),
        layout_ids_used=layout_ids_used,
        subject_ids_used=subject_ids_used,
        unused_groups=unused_groups,
    )


# --------------------------------------------------------------------------- #
# 资源构造（全部产出「官方 Profile 信封」形状）
# --------------------------------------------------------------------------- #


def build_class_plan_resource(
    block: ClassBlock, label: str | None = None, group_guid: str | None = None
) -> dict:
    """把一个班的 6 天课表打成一份 ClassPlan 资源（Profile 信封）。

    - 保留原始 plan GUID（同 GUID 在设备端是「更新」而非「新增」，不会堆出重复课表）
    - **每个班使用自己的独立课表群**（`group_guid`，缺省由班号稳定派生）：
      集控通道下发不了 `SelectedClassPlanGroupId`（合并时只拷 ClassPlans/ClassPlanGroups），
      所以设备端一次性会累积到所有班的课表。若各班的课表都塞进「默认群」，
      同一天会有 12 张课表互相覆盖，"这台机器显示哪一班"就无从控制。
      改为每班一群后，由插件按集控指令把 `SelectedClassPlanGroupId` 指向对应群
      （见 `set_active_class` 动作 / `POST /class/{id}/activate`），切班语义才成立。

      `group_guid` 缺省用「班号确定性地哈希成 GUID」，保证同一班多次导入落在同一群，
      不会因为重新导入而堆出新群。
    """
    label = label or default_class_label(block.index)
    group_guid = (group_guid or deterministic_group_guid(block.index)).lower()
    group_name = f"{label}课表群"

    plans: dict[str, dict] = {}
    for weekday in block.weekdays:
        guid, plan = block.plans[weekday]
        item = copy.deepcopy(plan)
        item["AssociatedGroup"] = group_guid
        item["IsEnabled"] = True
        item["IsOverlay"] = False
        item["OverlaySourceId"] = None
        item.setdefault("Name", WEEKDAY_CN.get(weekday, f"周{weekday}"))
        plans[guid] = item

    return {
        "Name": f"{label}课表",
        "ClassPlans": plans,
        "ClassPlanGroups": {
            group_guid: {"Name": group_name, "IsGlobal": False},
            # 默认群 / 全局群一并带上：官方客户端在合并 ClassPlanGroups 时按 GUID 逐键并，
            # 带上它们可保证设备端始终存在这两个「永远有效」的群，避免用户手工切走后再无回退群。
            DEFAULT_GROUP_GUID: {"Name": DEFAULT_GROUP_NAME, "IsGlobal": False},
            GLOBAL_GROUP_GUID: {"Name": GLOBAL_GROUP_NAME, "IsGlobal": True},
        },
        # 非官方字段，仅供 CIMS 侧读取「本班课表群是哪个」，客户端会忽略它。
        "StelarithActiveGroup": group_guid,
    }


def build_time_layout_resource(layouts: dict[str, dict], name: str = "全校作息") -> dict:
    """作息资源（Profile 信封），全校共享一份。"""
    return {"Name": name, "TimeLayouts": copy.deepcopy(layouts)}


def build_subjects_resource(subjects: dict[str, dict], name: str = "全校科目") -> dict:
    """科目资源（Profile 信封），全校共享一份。"""
    return {"Name": name, "Subjects": copy.deepcopy(subjects)}


def build_empty_week_class_plan_resource(
    label: str,
    layout_by_weekday: dict[int, str],
    plan_guids: dict[int, str] | None = None,
    layout_classes: dict[str, int] | None = None,
    group_guid: str | None = None,
) -> dict:
    """手工新建一个班的「空周课表」资源（6 天骨架，课时留空待填）。

    用于面板「手动添加课表」：不依赖任何档案导入，只要给出每天的作息 GUID 即可。
    Classes 里 SubjectId 留 Guid.Empty——官方 `ClassPlan.RefreshClassesList()` 会用
    作息时间点的 `DefaultClassId` 兜底填充，是官方推荐的空课表形态。

    `group_guid`：本班课表群。缺省时用「由 label 确定性派生」的群，保证手工新建的班
    与导入的班一样有独立课表群，可被 `POST /class/{id}/activate` 切到。
    """
    import uuid

    group_guid = (group_guid or deterministic_group_guid(_label_to_index(label))).lower()
    group_name = f"{label}课表群"

    plans: dict[str, dict] = {}
    for weekday, tl_id in sorted(layout_by_weekday.items()):
        guid = (plan_guids or {}).get(weekday) or str(uuid.uuid4())
        n = (layout_classes or {}).get(tl_id, 0)
        plans[guid] = {
            "TimeLayoutId": tl_id,
            "TimeRule": {
                "WeekDay": weekday,
                "WeekCountDiv": 0,
                "WeekCountDivTotal": 2,
                "IsActive": False,
            },
            "Classes": [
                {
                    "SubjectId": GLOBAL_GROUP_GUID,
                    "IsChangedClass": False,
                    "IsEnabled": True,
                    "AttachedObjects": {},
                    "IsActive": False,
                }
                for _ in range(n)
            ],
            "Name": WEEKDAY_CN.get(weekday, f"周{weekday}"),
            "IsOverlay": False,
            "OverlaySourceId": None,
            "IsEnabled": True,
            "AssociatedGroup": group_guid,
            "AttachedObjects": {},
            "IsActive": False,
        }
    return {
        "Name": f"{label}课表",
        "ClassPlans": plans,
        "ClassPlanGroups": {
            group_guid: {"Name": group_name, "IsGlobal": False},
            DEFAULT_GROUP_GUID: {"Name": DEFAULT_GROUP_NAME, "IsGlobal": False},
            GLOBAL_GROUP_GUID: {"Name": GLOBAL_GROUP_NAME, "IsGlobal": True},
        },
        "StelarithActiveGroup": group_guid,
    }


# 中文数字 → 阿拉伯数字（用于从「3班」这类标签反推班号；也接受「初三1班」）
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _label_to_index(label: str) -> int:
    """从班级标签里抽出班号；抽不到时用字符串的稳定散列映射到一个正整数。

    目的只是让「同一个标签永远得到同一个群」，不参与任何业务判断，
    所以抽不到班号时退化为散列完全可接受（不会与其它班冲突到同一群的概率极高）。
    """
    if label:
        m = re.search(r"(\d+)", label)
        if m:
            return int(m.group(1))
        # 「三班」这种中文数字写法
        for ch, val in _CN_DIGITS.items():
            if ch in label:
                return val
        # 纯散列兜底（保证稳定）
        h = 0
        for ch in label:
            h = (h * 131 + ord(ch)) & 0x7FFFFFFF
        return h % 100000 + 1000
    return 0


@dataclass
class ClassImport:
    """一个班要落库的全部内容。"""

    index: int
    class_id: str
    name: str
    class_plan_name: str
    class_plan_resource: dict


def plan_class_imports(
    parsed: ProfileParseResult,
    indices: Iterable[int] | None = None,
    label_fn: Callable[[int], str] = default_class_label,
) -> list[ClassImport]:
    """按班级序号挑选要导入的班，并生成资源名/资源内容。"""
    wanted = set(indices) if indices else {b.index for b in parsed.classes}
    out: list[ClassImport] = []
    for block in parsed.classes:
        if block.index not in wanted:
            continue
        label = label_fn(block.index)
        out.append(
            ClassImport(
                index=block.index,
                class_id=class_id_of(block.index),
                name=label,
                class_plan_name=class_resource_name(block.index),
                class_plan_resource=build_class_plan_resource(block, label),
            )
        )
    return out


# --------------------------------------------------------------------------- #
# 校验（导入前自检，防悬空引用）
# --------------------------------------------------------------------------- #


def validate_import(parsed: ProfileParseResult, imports: list[ClassImport]) -> list[str]:
    """返回问题列表（空列表 = 全部通过）。

    检查项：
    - 每个班是否恰好覆盖周日/周一…周五 6 天（缺天会让该天在设备端无课表）
    - ClassPlan.TimeLayoutId 是否都能在共享作息里找到
    - ClassPlan.Classes[].SubjectId 是否都在科目词典里
    """
    issues: list[str] = []
    expected = set(WEEKDAY_CN) - {6}  # 周日 + 周一~周五

    for block in parsed.classes:
        got = set(block.weekdays)
        if got != expected:
            issues.append(
                f"班{block.index}: 星期覆盖不全，缺少 {sorted(expected - got)}"
            )

    layout_ids = {k.lower() for k in parsed.time_layouts}
    subject_ids = {k.lower() for k in parsed.subjects}
    # 作息里时间点的 DefaultClassId 是合法科目源，一并纳入白名单
    for tl in parsed.time_layouts.values():
        for item in (tl or {}).get("Layouts") or []:
            did = item.get("DefaultClassId")
            if did:
                subject_ids.add(str(did).lower())

    for imp in imports:
        plans = imp.class_plan_resource.get("ClassPlans") or {}
        for guid, plan in plans.items():
            tl = str(plan.get("TimeLayoutId") or "").lower()
            if tl and tl not in layout_ids:
                issues.append(f"{imp.name}({guid[:8]}): 引用了不存在的作息 {tl}")
            for ci in plan.get("Classes") or []:
                sid = str(ci.get("SubjectId") or "").lower()
                if sid and sid != GLOBAL_GROUP_GUID and sid not in subject_ids:
                    issues.append(f"{imp.name}({guid[:8]}): 引用了不存在的科目 {sid}")
    return issues


# --------------------------------------------------------------------------- #
# 落库编排（供 CLI 脚本与管理 API 共用，避免两边各写一份）
# --------------------------------------------------------------------------- #


async def upsert_resource(db, resource_type: str, name: str, payload: dict) -> int:
    """把一份资源写进对应 *_files 表（存在则就地更新 + version+1），返回新版本号。"""
    import json
    from datetime import datetime, timezone
    from sqlalchemy import select

    from app.api.command.model_map import MODEL_MAP

    model = MODEL_MAP[resource_type]
    rec = (await db.execute(select(model).where(model.name == name))).scalar_one_or_none()
    if rec is None:
        rec = model(name=name)
    rec.content = json.dumps(payload, ensure_ascii=False)
    rec.version = (rec.version or 0) + 1
    rec.updated_at = datetime.now(timezone.utc)
    db.add(rec)
    return rec.version


async def write_shared_resources(db, parsed: ProfileParseResult) -> dict[str, int]:
    """写入全校共享的作息与科目资源（信封格式），返回 {资源类型: 版本}。"""
    return {
        "TimeLayout": await upsert_resource(
            db, "TimeLayout", TIME_LAYOUT_RESOURCE_NAME, build_time_layout_resource(parsed.time_layouts)
        ),
        "Subjects": await upsert_resource(
            db, "Subjects", SUBJECTS_RESOURCE_NAME, build_subjects_resource(parsed.subjects)
        ),
    }


async def create_class_records(db, imp: ClassImport) -> None:
    """写一份班级课表资源 + classes 行 + class_resource_sets 行（幂等）。

    ⚠️ 重构后语义（2026-09-20）：班级**不再由课表派生**。此处是「从官方档案显式导入」
    这条受控链路，仅负责把班级记录补齐，以便承载导入的课表资源；这样产生的班级
    一律视为**系统班级**（`owner_user_id=""`）并直接标记 `approved`，从而不会被
    新增的审核门控挡住。真正的班级创建入口是管理端「文件夹式」新建（带属主+审核）。
    """
    from sqlalchemy import select

    from app.models.class_model import Class, ClassResourceSet, REVIEW_APPROVED

    await upsert_resource(db, "ClassPlan", imp.class_plan_name, imp.class_plan_resource)

    cls = (await db.execute(select(Class).where(Class.id == imp.class_id))).scalar_one_or_none()
    if cls is None:
        cls = Class(
            id=imp.class_id,
            name=imp.name,
            code=imp.name,
            resource_set_id=imp.class_id,
            sort_order=imp.index,
            owner_user_id="",
            review_status=REVIEW_APPROVED,
        )
    cls.name = imp.name
    cls.sort_order = imp.index
    db.add(cls)

    crs = (
        await db.execute(
            select(ClassResourceSet).where(ClassResourceSet.resource_set_id == imp.class_id)
        )
    ).scalar_one_or_none()
    if crs is None:
        crs = ClassResourceSet(resource_set_id=imp.class_id)
    crs.class_plan = imp.class_plan_name
    crs.time_layout = TIME_LAYOUT_RESOURCE_NAME
    crs.subjects = SUBJECTS_RESOURCE_NAME
    db.add(crs)


async def assign_device_to_class(db, client_id: str, index: int) -> bool:
    """把设备划到第 index 个班；设备不存在返回 False。"""
    from sqlalchemy import select

    from app.models.client import ClientProfile

    prof = (
        await db.execute(select(ClientProfile).where(ClientProfile.client_id == client_id))
    ).scalar_one_or_none()
    if prof is None:
        return False
    prof.class_id = class_id_of(index)
    db.add(prof)
    return True
