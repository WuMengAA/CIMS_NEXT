"""从本机 ClassIsland 安装提取 7 类官方资源，作为 CIMS 租户初始化的真实种子。

对齐 ClassIsland 官方集控资源模型（manifest 的 7 个 *Source）：
    ClassPlan / TimeLayout / Subjects / DefaultSettings / Policy / Components / Credentials

**关键：ClassPlan / TimeLayout / Subjects 三类资源必须是「档案（Profile）信封」**。
官方客户端 `ProfileService.MergeManagementProfileAsync` 是这么消费它们的：

    GetJsonAsync<Profile>(Manifest.ClassPlanSource.Value)   → MergeDictionary(Profile.ClassPlans, ...)
    GetJsonAsync<Profile>(Manifest.TimeLayoutSource.Value)  → MergeDictionary(Profile.TimeLayouts, ...)
    GetJsonAsync<Profile>(Manifest.SubjectsSource.Value)    → MergeDictionary(Profile.Subjects, ...)

所以载荷形状只能是：

    ClassPlan  → {"ClassPlans": {...}, "ClassPlanGroups": {...}}
    TimeLayout → {"TimeLayouts": {...}}
    Subjects   → {"Subjects": {...}}

若直接落单个 ClassPlan / 单个 TimeLayout（早期做法），客户端解析出的字典为空 →
「资源拉到了但课表一节课都不显示」，静默失效。本脚本因此统一产出信封格式。
ClassPlan 种子刻意留空（`ClassPlans: {}`）——课表按班级下发，见
scripts/import_classes_from_profile.py；未绑定班级的设备就该没有课表。

数据来源（只读，不修改 ClassIsland 任何文件）：
    Profiles/Default.json            -> TimeLayouts / ClassPlans / Subjects
    data/Settings.json               -> DefaultSettings
    Config/Management/Policy.json    -> Policy
    Config/Management/Credentials.json -> Credentials
    Config/ComponentLayouts/Default.json -> Components

输出到 CIMS-backend/seed_resources/<Type>.json（UTF-8，缩进 2）。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.schedule_importer import (  # noqa: E402
    DEFAULT_GROUP_GUID,
    GLOBAL_GROUP_GUID,
    build_subjects_resource,
    build_time_layout_resource,
    parse_official_profile,
)

CI = r"D:\Classlsland\data"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_resources")
os.makedirs(OUT, exist_ok=True)


def _load(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _dump(name, obj):
    p = os.path.join(OUT, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    print(f"[ok] {name}  ({os.path.getsize(p)} bytes)")


def main():
    profile = _load(os.path.join(CI, "Profiles", "Default.json"))

    class_plans = profile.get("ClassPlans") or {}
    time_layouts = profile.get("TimeLayouts") or {}
    subjects = profile.get("Subjects") or {}
    assert class_plans, "ClassPlans 为空，无法提取"
    assert time_layouts, "TimeLayouts 为空，无法提取"

    # 用解析器切班，顺带确认档案可导入；种子只取全校共享的作息
    parsed = parse_official_profile(profile)
    print(f"[info] 档案切出 {len(parsed.classes)} 个班，"
          f"被引用的作息 {len(parsed.time_layouts)} 份，科目 {len(parsed.subjects)} 个")

    # ---- ClassPlan 种子：合法但留空的信封（课表按班级下发）----
    _dump(
        "ClassPlan.json",
        {
            "Name": "未绑定班级",
            "ClassPlans": {},
            "ClassPlanGroups": {
                DEFAULT_GROUP_GUID: {"Name": "默认", "IsGlobal": False},
                GLOBAL_GROUP_GUID: {"Name": "全局课表群", "IsGlobal": True},
            },
        },
    )
    # ---- TimeLayout：全校共享，信封 ----
    _dump("TimeLayout.json", build_time_layout_resource(parsed.time_layouts or time_layouts))
    # ---- Subjects：全校共享，信封 ----
    _dump("Subjects.json", build_subjects_resource(subjects))
    # ---- 其余四类官方就是裸模型对象，直接落 ----
    _dump("DefaultSettings.json", _load(os.path.join(CI, "Settings.json")))
    _dump("Policy.json", _load(os.path.join(CI, "Config", "Management", "Policy.json")))
    _dump("Credentials.json", _load(os.path.join(CI, "Config", "Management", "Credentials.json")))
    _dump("Components.json", _load(os.path.join(CI, "Config", "ComponentLayouts", "Default.json")))

    print(f"[done] 7 类资源已输出到 {OUT}（ClassPlan/TimeLayout/Subjects 为官方档案信封格式）")


if __name__ == "__main__":
    main()
