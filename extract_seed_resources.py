"""从本机 ClassIsland 安装提取 7 类官方资源，作为 CIMS 租户初始化的真实种子。

对齐 ClassIsland 官方集控资源模型（manifest 的 7 个 *Source）：
    ClassPlan / TimeLayout / Subjects / DefaultSettings / Policy / Components / Credentials

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

    # 选一个代表课表：优先 IsEnabled/IsActive，其次首个
    cp_id, cp = next(iter(class_plans.items()))
    for k, v in class_plans.items():
        if isinstance(v, dict) and (v.get("IsEnabled") or v.get("IsActive")):
            cp_id, cp = k, v
            break

    # 选与课表关联的时间布局
    tl_id = cp.get("TimeLayoutId") if isinstance(cp, dict) else None
    tl = time_layouts.get(tl_id) if tl_id else None
    if tl is None:
        tl_id, tl = next(iter(time_layouts.items()))

    print(f"[info] 选用 ClassPlan id={cp_id}  TimeLayout id={tl_id}")

    _dump("ClassPlan.json", cp)
    _dump("TimeLayout.json", tl)
    _dump("Subjects.json", subjects)
    _dump("DefaultSettings.json", _load(os.path.join(CI, "Settings.json")))
    _dump("Policy.json", _load(os.path.join(CI, "Config", "Management", "Policy.json")))
    _dump("Credentials.json", _load(os.path.join(CI, "Config", "Management", "Credentials.json")))
    _dump("Components.json", _load(os.path.join(CI, "Config", "ComponentLayouts", "Default.json")))

    print(f"[done] 7 类资源已输出到 {OUT}")


if __name__ == "__main__":
    main()
