# Phase 4 验证报告 · 班级真实课表导入与官方格式兼容

- 时间：2026-09-14
- 后端提交：`62dd2de`（仓库 `D:/Stelarith/Stelarith-cims-eval/CIMS-backend`）
- 插件仓库：无改动（本轮全部在后端与数据层）
- 档案来源：`D:\Classlsland\data\Profiles\Default.json`（只读，未修改任何 ClassIsland 文件）

---

## 一、结论先行

1. **清空**：旧的测试班级（`class_3p1` / `class_3p2`）与残留测试资源（`plan_test` / `tl_test` / `sub_test`）已删除。
2. **导入**：12 个班的真实课表已按**官方格式**落库，每班一份 `cp_class01…cp_class12`，
   全校共享 `tl_school`（作息）/ `sub_school`（科目）。班级数 = 12，悬空引用 = 0。
3. **兼容**：修复了一个**静默失效级**的兼容缺陷（见下节），并给 manifest 加了资源存在性兜底。
4. **手动添加**：新增「铺空周课表 → 逐天写课时 → 读回核对」三个接口，产出同样是官方格式。

---

## 二、修掉的关键兼容缺陷（本轮最重要产出）

官方客户端 `ClassIsland/Services/ProfileService.cs#MergeManagementProfileAsync` 是这样消费资源的：

```csharp
var cpNew = await Connection.GetJsonAsync<Profile>(Manifest.ClassPlanSource.Value!);
MergeDictionary(Profile.ClassPlans, cpOld.ClassPlans, cpNew.ClassPlans);
MergeDictionary(Profile.ClassPlanGroups, cpOld.ClassPlanGroups, cpNew.ClassPlanGroups);
```

即 **ClassPlan / TimeLayout / Subjects 三类资源，服务端返回的必须是一个「档案（Profile）信封」**：

| 资源 | 之前（错） | 现在（对） |
|---|---|---|
| ClassPlan | 单个 ClassPlan 对象 | `{"ClassPlans":{…}, "ClassPlanGroups":{…}}` |
| TimeLayout | 单个 TimeLayout 对象 | `{"TimeLayouts":{…}}` |
| Subjects | 数组 | `{"Subjects":{…}}`（字典，键=科目 GUID） |

**不修的后果**：客户端解析出的 `Profile.ClassPlans` 是空字典 → 资源 HTTP 200 拿到了、
**课表一节课都不显示**，且不报任何错。静默失效，比 404 难查得多。

第二条硬约束：`ClassPlan.AssociatedGroup` 必须落在**默认课表群**
（`acaf4ef0-e261-4262-b941-34ea93cb4369`）或全局群，才会被 `LessonsService.CheckClassPlan` 选中。
而 `Profile.SelectedClassPlanGroupId` 只存在本地档案里、**集控通道下发不了**
（合并时只拷 `ClassPlans` / `ClassPlanGroups`）。因此按班级下发时，已把该班课表的
`AssociatedGroup` 统一改写到默认群——每台设备一份档案、档案里只有自己班的 6 天课表，
默认群即唯一有效群。

---

## 三、档案切班结果

`Default.json` 实测：`ClassPlans` 是**字典**（72 个）、`ClassPlanGroups` 24 个群。
按 `ClassPlan.AssociatedGroup` 反查，**12 个群挂了课表 = 12 个班 × 6 天**，另 12 个群是空壳。

- 作息：被引用的 3 份 —— `周1234(高二通用)`、`周5(高二通用)`、`周日(高二通用)`
  （档案里另有一份 `周1(高二通用)` 未被任何课表引用）
- 科目：全校共 23 个
- 班名：档案里 12 个群**全叫「新课表群」**，无从区分，故按出现次序命名 `1班…12班`（可在面板改名）

12 班「周一」课表（逐班核对，与档案一致）：

| 班 | 周一课表 |
|---|---|
| 1班 | 早读 数学 语文 化学 生物 数学 英语 物理 物理 班会 数学 化学 |
| 2班 | 早读 语文 数学 生物 化学 数学 物理 英语 物理 班会 化学 数学 |
| 3班 | 早读 数学 语文 化学 地理 数学 英语 物理 物理 班会 数学 化学 |
| 4班 | 早读 语文 数学 生物 化学 数学 物理 英语 物理 班会 化学 数学 |
| 5班 | 早读 数学 语文 化学 生物 数学 英语 物理 物理 班会 数学 生物 |
| 6班 | 早读 语文 数学 生物 化学 数学 物理 英语 物理 班会 化学 数学 |
| 7班 | 早读 数学 化学 语文 地理 数学 英语 物理 物理 班会 数学 化学 |
| 8班 | 早读 语文 数学 地理 化学 数学 物理 英语 物理 自习 化学 数学 |
| 9班 | 早读 数学 语文 地理 政治 数学 英语 历史 历史 班会 地理 数学 |
| 10班 | 早读 语文 数学 化学 物理 数学 政治 英语 物理 班会 数学 化学 |
| 11班 | 早读 数学 语文 化学 生物 数学 英语 物理 物理 班会 数学 化学 |
| 12班 | 早读 语文 数学 美术 化学 数学 物理 英语 物理 班会 化学 数学 |

> 2/4/6 班周一课表相同、1/5/11 班相近——这是档案里的真实排课（含同一教师带多班的合班课），
> 不是解析错误；全周 6 天合起来各班互不相同。

---

## 四、验证项与结果

| # | 验证项 | 方法 | 结果 |
|---|---|---|---|
| 1 | 12 班 manifest 各自解析到本班资源 | 进程内 ASGI 探针 `._tmp/cims_probe_p4.py` | ✅ `cp_classNN` / `tl_school` / `sub_school` |
| 2 | 资源内容是真信封 | 取回 ClassPlan 断言顶层键 | ✅ `Name`/`ClassPlans`/`ClassPlanGroups`，6 天，全部 `AssociatedGroup`=默认群 |
| 3 | 12 班课表各不相同且数据真实 | 逐班取回并翻译科目名 | ✅ 12 班周一课表与档案逐字一致 |
| 4 | 悬空引用演练 | 把 `class_01` 的 `class_plan` 改成不存在的名字 | ✅ manifest 自动兜底为 `default_classplan`，资源 200（不是 404） |
| 5 | 手动添加空周课表 | `._tmp/cims_probe_p5.py` | ✅ 6 天骨架，节数 周日3 / 周一~四12 / 周五8（与作息一致） |
| 6 | 选择性导入 | `plan_class_imports(parsed, [3,8])` | ✅ 只切出 3 班与 8 班，各 6 天 |
| 7 | 租户全流程重建 | `CIMS_TENANT_SLUG=cls-init-test rebuild_min_tenant.py` | ✅ 7 类资源 + 12 班 + 设备划入 1 班 + DataUpdated 命令 |
| 8 | 真 HTTP 链路 | 8096 manifest + 302 → `/get?token=` 取内容 | ✅ 信封形状与 DB 一致 |

---

## 五、新增/修改清单

**新增**

- `app/services/schedule_importer.py` — 档案解析（切班）+ 官方信封构造 + 导入自检 + 落库编排（CLI 与 API 共用）
- `app/api/management/class_import_routes.py` — `import-from-profile` / `apply-week-template` / `schedule`
- `scripts/import_classes_from_profile.py` — CLI 导入（`--reset` / `--assign` / `--classes` / `--dry-run`）
- `scripts/migrate_notnull_defaults.py` — 幂等回填既有租户 Schema 的列 DEFAULT
- `._tmp/cims_probe_p4.py`、`._tmp/cims_probe_p5.py` — 端到端验证探针

**修改**

- `app/api/client/manifest.py` — 资源存在性兜底（请求名 → 默认名 → 表内最近行）
- `app/api/command/timetable_validator.py` — 适配 Subjects **字典**格式，支持信封/裸对象双读，报错定位到课表 GUID
- `app/api/management/class_routes.py` — 班级专属资源名缺省生成 `cp_<class_id>`；路由去掉重复的 `class/` 前缀
- `app/models/{client,command_queue,class_model,resource_mixin}.py` — 补 `server_default`
- `rebuild_min_tenant.py` — 初始化即导入班级课表；search_path 只设一次不切回 public；支持 `CIMS_TENANT_SLUG`
- `extract_seed_resources.py` — 种子改为官方信封格式
- `seed_resources/{ClassPlan,TimeLayout,Subjects}.json` — 重新生成为信封格式
- `docs/architecture-v2.md` — §4.1 / 4.2 / 5.1 / 5.5 重写为**真实路径与真实格式**，新增 Phase 4 记录

---

## 六、手动添加课表怎么用（三段式）

```bash
# 1) 建班
curl -X POST "http://127.0.0.1:8097/class/create?class_id=class_13&name=13班" -H "Authorization: Bearer <token>"

# 2) 铺空周课表（6 天骨架，节数按作息自动算好）
curl -X POST "http://127.0.0.1:8097/class/class_13/apply-week-template?label=13班" -H "Authorization: Bearer <token>" -d '{}'

# 3) 逐天写课时（envelope 形态，带引用完整性校验）
curl -X POST "http://127.0.0.1:8097/class/class_13/resource/ClassPlan/write" \
     -H "Authorization: Bearer <token>" -H "Content-Type: application/json" -d @plan.json

# 4) 读回核对
curl "http://127.0.0.1:8097/class/class_13/schedule" -H "Authorization: Bearer <token>"
```

不想手工逐天填，也可以直接 `POST /class/import-from-profile` 传一份官方档案，
`classes="1,3,8"` 只取其中几个班。

---

## 七、遗留与注意

1. **插件目前只把同步到的资源「展示为快照」，并未写回 ClassIsland 本地档案**——
   「课表在教室大屏上真正按新表运行」需要插件侧再接一步（把资源合并进
   `Profiles/_management-profile.json` 并触发 profile 重载）。本轮未做。
2. 真机闭环验证（ClassIsland 进程侧）本轮未跑：运行验证时 ClassIsland 未在运行。
   后端与协议层已用进程内探针 + 真 HTTP 双路径验证。
3. 班名 `1班…12班` 是从档案推断的编号（档案里 12 个群同名），建议在面板上改成真实班名。
4. 老租户 Schema 若出现「NOT NULL 违例」，先跑
   `scripts/migrate_notnull_defaults.py` 补列默认值。
