# CIMS 集控系统架构蓝图 v2

**班级化 · 双向通道 · 官方协议对齐**

| 项 | 值 |
|---|---|
| 文档状态 | 已评审（设计基线）；§8 路线 Phase 1/2/3 已实施验证（2026-09-14） |
| 版本 | v2.0.0 |
| 日期 | 2026-09-13 |
| 适用工程 | `CIMS-backend` + `school-multimedia-control/ext/stelarith-classisland-plugin` |
| 对齐标准 | ClassIsland 官方集控协议（gRPC 命令 + manifest 资源分发） |

---

## 1. 背景与问题陈述

CIMS 原为「ClassIsland 集控服务器」的古早实现。经现场摸查（逐文件核实），确认现状如下：

| 能力 | 现状 | 结论 |
|---|---|---|
| 账号体系 | management API 完备（user_auth / account_* / token_* / totp） | ✅ 可复用 |
| 设备注册 | `PairingCode` 配对码 + 审批放行 | ✅ 可复用，缺班级维度 |
| 班级聚合 | **无 `classes` 实体**，`ClientProfile` 仅按 `client_id` 一对一 | ❌ 核心缺口 |
| 课表标准 | `seed_resources/` 三件套已是官方 ClassIsland 原生 JSON | ✅ 方向正确 |
| 下行分发 | manifest 协议（`/v1/client/{uid}/manifest` → `*Source` → `/get`）已打通 | ✅ 可复用 |
| 上行回传 | gRPC 有 `ConfigUploadScReq` protobuf 骨架，**servicer 未实现** | ❌ 缺口 |
| 命令通道 | gRPC 实时订阅（`ClientCommandDeliverScReq`）+ HTTP poller 轮询兜底 | ✅ 双通道已建 |
| 远程控制 | lock / screenshot 插件直做；shell / 远程桌面转 AgentClient 代理 | ⚠️ 矩阵不完整 |
| 命令消费语义 | `command_poll.py` 为「GET 即置 done」 | ⚠️ 离线会丢命令 |
| 通知广播 | 官方 `AddNotificationProvider<T>` 注册 + UI 线程推送（cdb095f 已修） | ✅ 闭环打通 |

**核心洞见**：所有「多班级」问题的根源是**模型里没有班级这一层**。v2 的核心工作 = 增加班级聚合层 + 打通上行通道 + 修正命令消费语义 + 补齐控制矩阵。

---

## 2. 总体架构

### 2.1 分层模型

```
┌────────────────────────── 账号层 ──────────────────────────┐
│  管理员账号（管理端）  老师账号（管理端/查看）              │
└──────────────────────────────┬────────────────────────────┘
                               │ 管理
┌──────────────────────────────▼────────────────────────────┐
│  班级层（新增）  classes + class_resource_sets             │
│   初三1班 ──┬── 设备 lab-pc-001, lab-pc-002               │
│   初三2班 ──┴── 设备 lab-pc-003, lab-pc-004               │
│   班级 ──绑定──> 资源集（ClassPlan+TimeLayout+Subjects+…） │
└──────────────────────────────┬────────────────────────────┘
                               │ manifest / 命令
┌──────────────────────────────▼────────────────────────────┐
│  设备层   ClassIsland 终端 + Stelarith 插件                │
│   下行：manifest 拉取 / gRPC 命令 / HTTP poller 轮询       │
│   上行：ConfigUploadScReq 配置上报 / Audit 审计事件        │
└───────────────────────────────────────────────────────────┘
```

### 2.2 核心原则

1. **协议对齐官方**：命令类型、资源模型、上行通道一律用 ClassIsland 官方 protobuf/JSON 结构；不造私有协议。
2. **服务端只做编排**：课表渲染、提醒展示、课表编辑交互交给 ClassIsland 客户端/官方生态，服务端管数据与分发。
3. **双向通道**：下行（服务端→班级→设备）与上行（设备→班级→服务端）均为一等公民。
4. **向后兼容**：未绑定班级的设备回退到自身 `client_profiles`（老行为不变），平滑迁移。
5. **降级优先**：gRPC 断 → HTTP poller 兜底；通知不可用 → 托盘气泡；旧版本资源 → 跳过不阻塞。

---

## 3. 数据模型设计

### 3.1 现有基线（不动，仅加列/加表）

| 表 | 关键列 | 归属 |
|---|---|---|
| `clients` | uid PK, client_id, mac, registered_at | 租户 schema |
| `client_profiles` | client_id PK, class_plan, time_layout, subjects, default_settings, policy, components, credentials, updated_at | 租户 schema |
| `pairing_codes` | id PK, code(8), tenant_id, client_uid, client_id, client_mac, client_ip, approved, used, created_at | 租户 schema |
| `command_queue` | id, client_id, command_type, payload, status(pending/done), created_at, updated_at | 租户 schema |
| `cp_files` / `tl_files` / `sub_files` / `settings_files` / `policy_files` / `components_files` / `credentials_files` | name PK, content JSON, version, updated_at | 租户 schema |

### 3.2 新增 `classes`（班级）

```sql
CREATE TABLE classes (
    id          VARCHAR(64) PRIMARY KEY,          -- 'class_3p1' 风格，人可读
    tenant_id   VARCHAR(64) NOT NULL,             -- 冗余存租户，便于跨租户查询
    name        VARCHAR(128) NOT NULL,            -- '初三1班'
    resource_set_id VARCHAR(64) NOT NULL,         -- 绑定资源集
    sort_order  INT DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ix_classes_tenant ON classes (tenant_id);
```

### 3.3 新增 `class_resource_sets`（班级资源集）

复用 `client_profiles` 的 7 列结构，作为「班级版 profile」：

```sql
CREATE TABLE class_resource_sets (
    resource_set_id VARCHAR(64) PRIMARY KEY,      -- 与 classes.resource_set_id 对应
    class_plan      VARCHAR(64) NOT NULL DEFAULT 'default_classplan',
    time_layout     VARCHAR(64) NOT NULL DEFAULT 'default_timelayout',
    subjects        VARCHAR(64) NOT NULL DEFAULT 'default',
    default_settings VARCHAR(64) NOT NULL DEFAULT 'default',
    policy          VARCHAR(64) NOT NULL DEFAULT 'default',
    components      VARCHAR(64),
    credentials     VARCHAR(64),
    updated_at      TIMESTAMPTZ DEFAULT now()
);
```

> 设计理由：与 `client_profiles` 同构，manifest 生成函数可复用同一套 `_build_manifest`；未来可平滑演进为「班级 → 资源集」的 N:1 共享。

### 3.4 改造 `client_profiles`：加 `class_id` 外键

```sql
ALTER TABLE client_profiles ADD COLUMN class_id VARCHAR(64);
CREATE INDEX ix_client_profiles_class ON client_profiles (class_id);
```

**绑定规则（精准匹配）**：
- 设备注册（配对）时上报 `class_code` → 服务端解析出 `class_id` → 写入 `client_profiles.class_id`
- manifest 解析优先级：`class_id → class_resource_sets`（班级资源集）→ 未绑定则回退 `client_profiles` 自身 7 列（老行为）
- 一个班级 N 台设备共享同一份资源集；改班级课表 = 全班生效；设备级覆盖仍允许（class_id 为空时用自身列）

### 3.5 改造 `pairing_codes`：加班级码

```sql
ALTER TABLE pairing_codes ADD COLUMN class_code VARCHAR(16);  -- 'PAIR-3P1-XYZK' 或纯 '3P1'
ALTER TABLE pairing_codes ADD COLUMN class_id VARCHAR(64);
CREATE INDEX ix_pairing_class ON pairing_codes (class_id);
```

配对码格式升级为三段式：`PAIR-<班级码>-<8位码>`。设备首次注册时输班级码 → 服务端建/查班级 → 生成带 `class_id` 的配对记录 → 审批放行后 `client_profiles.class_id` 自动落库。

### 3.6 修正 `command_queue` 消费语义（关键变更）

现状（`command_poll.py`）：**GET queued 端点即把全部 pending 置 done**——离线/解析失败即丢命令。

v2 语义（引入 ack）：

```sql
ALTER TABLE command_queue ADD COLUMN ack_status VARCHAR(16) NOT NULL DEFAULT 'pending';
-- pending   : 未被取走
-- delivered : 已被 poller 取走，等待客户端执行确认
-- done      : 客户端已确认执行完成
-- failed    : 客户端确认执行失败（可重试）
ALTER TABLE command_queue ADD COLUMN delivered_at TIMESTAMPTZ;
ALTER TABLE command_queue ADD COLUMN ack_at TIMESTAMPTZ;
```

- `GET /queued`：只取 `ack_status='pending'` 的行，原子置 `delivered` 并返回（不再直接 done）
- 新增 `POST /{client_id}/command/ack`：客户端执行成功后上报 `{command_ids, status}` → 置 `done` / `failed`
- 超时（如 120s 未 ack）→ 回置 `pending` 补发；设备离线期间命令保持 `delivered`，重连后先拉 ack 状态再决定补发

---

## 4. API 设计

### 4.1 管理端（下行编排，8097）

> 路径即为**真实路径**（management app 监听 8097，路由直接挂在 `/class/*`）。
> 注意：`class_routes.py` 早期把 `class/` 写进了子路由，实际变成 `/class/class/create`，
> 已修正为下表。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/class/create` | 建班级 + 默认资源集 |
| GET | `/class/list` | 班级列表（含设备数、资源版本） |
| POST | `/class/device/assign` | 把设备划入班级 |
| POST | `/class/device/unassign` | 把设备移出班级（回退设备级） |
| POST/PUT | `/class/{class_id}/resource/{resource_type}/write` | 写班级某类资源（带 version 乐观锁 + 引用完整性校验） |
| POST | `/class/{class_id}/command/{command_type}` | 向班级全部设备广播命令 |
| POST | `/class/{class_id}/apply-week-template` | **手动添加**：给班级铺一张空周课表（6 天骨架） |
| GET | `/class/{class_id}/schedule` | 读回某班课表（按星期汇总科目名） |
| POST | `/class/import-from-profile` | **导入**：从 ClassIsland 官方档案一次切出 N 个班 |

命令广播落库逻辑：`target_class` 展开为设备列表 → 逐设备写 `command_queue`（`command_type` 保持官方 `CommandTypes` 枚举）。

### 4.2 客户端（下行拉取，8096）

| 方法 | 路径 | 现状 | v2 变更 |
|---|---|---|---|
| GET | `/api/v1/client/{uid}/manifest` | ✅ | 解析优先级加入班级资源集；**资源存在性兜底**（见下） |
| GET | `/api/v1/client/{ResourceType}?name=` → 302 `/get?token=` | ✅ | 不变 |
| GET | `/api/v1/client/{id}/command/queued` | ✅ | 语义改为 delivered（见 3.6） |
| POST | `/api/v1/client/{id}/command/ack` | ✅ | 执行确认 |

**资源存在性兜底（必须保留）**：`_build_manifest` 之前会把 `class_resource_sets` 里的名字
原样交给客户端。若该资源名在 `*_files` 表里不存在，客户端必然吃到 `/get` 的 404，
而 `CCProtectMiddleware` 计数 ≥400 响应——**同一 IP 60s 内累计 5 次就封禁 60s**，
连命令轮询一起打死（表现为插件「取不到命令 + 全接口 429」）。
现在 `resolve_resource_names` 末尾统一收敛：**请求名 → 该类型默认名 → 表内最近更新的一行**，
保证 manifest 永不指向不存在的资源。

### 4.3 上行回传（设备 → 服务端）

| 通道 | 协议 | 载荷 | v2 动作 |
|---|---|---|---|
| 配置上报 | gRPC `ConfigUploadScReq` | 客户端当前配置快照 | **实现 servicer**：接收 → 校验 → 落 `config_uploads` 审计表 → 触发「待审批」或「直接写回资源集」 |
| 审计事件 | gRPC `AuditScReq` | AppCrashed / ClassChangeCompleted / ProfileItemUpdated / PluginInstalled / AppSettingsUpdated / AuthorizeEvent | **实现 servicer**：落审计表，管理端状态面板可查 |
| 命令确认 | HTTP `POST /command/ack` | 执行结果 | 新增（见 3.6） |

### 4.4 配对

- `POST /api/v1/management/pairing/create`：管理端生成 `PAIR-<班级码>-<8位码>`
- 设备侧注册沿用官方 `ClientRegisterCsReq`，注册请求附 `class_code`
- `approve` 后自动完成 `client_profiles.class_id` 落库

---

## 5. 课表模型与编辑

### 5.1 三件套标准（官方格式是**唯一**基线）

| 资源 | 官方消费方式 | 载荷形状（**必须是 Profile 信封**） |
|---|---|---|
| ClassPlan | `GetJsonAsync<Profile>` → `MergeDictionary(ClassPlans, …)` | `{"ClassPlans": {"<guid>": {...}}, "ClassPlanGroups": {"<guid>": {...}}}` |
| TimeLayout | `GetJsonAsync<Profile>` → `MergeDictionary(TimeLayouts, …)` | `{"TimeLayouts": {"<guid>": {...}}}` |
| Subjects | `GetJsonAsync<Profile>` → `MergeDictionary(Subjects, …)` | `{"Subjects": {"<guid>": {...}}}` |

**这是踩过的坑，务必守住**：官方 `ClassIsland/Services/ProfileService.cs#MergeManagementProfileAsync`
把三类资源都反序列化成 `Profile`（档案）再合并字典。若服务端返回**单个** ClassPlan
或**单个** TimeLayout（早期 CIMS 种子的做法），客户端解析出的 `ClassPlans` 是空字典 →
「资源 200 拿到了、课表一节课都不显示」，属于**静默失效**，比 404 难查得多。

单份 ClassPlan 内部结构：`TimeLayoutId` + `Classes[]`（按 `SubjectId` 引用课时）+
`TimeRule`（`WeekDay` 0=周日…5=周五）+ `AssociatedGroup` + `IsOverlay`。
TimeLayout：`Layouts[]` = `StartTime`/`EndTime`/`TimeType`(0=上课,1=课间,2=动作组,3=动作组)/
`DefaultClassId`/`BreakName`。Subjects：`{GUID: {Name, Initial, TeacherName, IsOutDoor}}`
（**字典，不是数组**——旧校验按数组读会读空）。

**两条官方硬约束（决定「按班级下发」怎么做）**：

1. `ClassPlan.AssociatedGroup` 必须落在**默认课表群**（`ACAF4EF0-E261-4262-B941-34EA93CB4369`）
   或**全局课表群**（`00000000-…-000000000000`）才会被
   `LessonsService.CheckClassPlan` / `GetClassPlanByDate` 选中。
   `Profile.SelectedClassPlanGroupId` **只存在本地档案里，集控通道下发不了**
   （合并时只拷 `ClassPlans` / `ClassPlanGroups`）。所以按班级下发时必须把该班课表的
   `AssociatedGroup` 改写到默认群——每台设备一份档案、档案里只有自己班的 6 天课表，
   默认群即唯一有效群，语义自洽。
2. 时间表与科目**全校共享一份**即可：12 个班的 ClassPlan 都指向
   `tl_school` / `sub_school`，不必每班一份。

**不可拆分**：ClassPlan 的 `TimeLayoutId` 必须存在于该校作息资源，`Classes[].SubjectId`
必须在科目词典中（`SubjectId=Guid.Empty` 是官方「未填」合法写法，跳过）。
写班级课表时强制走 `app/api/command/timetable_validator.py` 做引用完整性校验。

### 5.2 编辑器方案（三选一，按工作量递增）

| 方案 | 说明 | 建议 |
|---|---|---|
| A. 表单生成三件套 | 管理端表单（课表网格：列=星期，行=节次）→ 生成 ClassPlan/TimeLayout/Subjects JSON → data_write | 先落地（够用） |
| B. 官方客户端导出导入 | 教师在 ClassIsland 桌面端编好课表 → 导出档案 JSON → **`POST /class/import-from-profile`** 一键切成 N 个班 | ✅ **已落地**（2026-09-14） |
| C. 网页可视化课表编辑器 | 拖拽式课表编辑组件（工作量大） | 后置 |



### 5.3 推送链（下行）

```
管理端改课表 → data_write（版本+1）→ 客户端 sync 拉 manifest
  → 版本变化 → 拉新资源 → 应用 → 上报 ack/audit（应用成功）
```

### 5.4 回传链（上行，v2 新增）

```
教师在教室端本地改了课表 → 插件监听官方配置变更事件
  → 打包 ClassPlan/TimeLayout/Subjects → gRPC ConfigUploadScReq 上报
  → 服务端校验（引用完整性）→ 策略判断：
      policy.allowClientEdit=true  → 直接写回班级资源集（版本+1，广播全班）
      policy.allowClientEdit=false → 落 config_uploads 待审，管理端审批后生效
```

---

### 5.5 官方档案导入与手动添加（✅ 已落地 2026-09-14）

**做法 B（导入真实档案）——批量把「一校十二班」搬进来**

```
Profiles/Default.json（官方档案）
  → parse_official_profile()      按 ClassPlan.AssociatedGroup 切出「一个班 = 一个课表群 = 6 天」
  → plan_class_imports()          生成 cp_class01…cp_class12 + class_01…class_12
  → validate_import()             自检：星期覆盖 / 作息引用 / 科目引用
  → write_shared_resources()      全校共享 tl_school（作息）+ sub_school（科目）
  → create_class_records()        每班写 cp_classNN（信封）+ classes + class_resource_sets
```

- 命令行：`scripts/import_classes_from_profile.py --reset --assign lab-pc-001=1`
- 接口：`POST /class/import-from-profile`（body 传档案 JSON，`classes="1,3,8"` 可只导部分班）
- 实现落在 `app/services/schedule_importer.py`（CLI 与 API 共用同一套编排，避免两份逻辑漂移）

**做法 A（手动添加）——没有档案也能起课表**

```
POST /class/create                     建班（自动绑默认资源集）
POST /class/{class_id}/apply-week-template   铺空周课表：6 天骨架 + 按作息算好的节数
POST /class/{class_id}/resource/ClassPlan/write   逐天写课时（引用完整性校验）
GET  /class/{class_id}/schedule        读回核对（按星期汇总科目名）
```

`apply-week-template` 的默认「哪天用哪份作息」是**按作息名里的「周X」提示推断**的
（`周1234` → 周一~周四、`周5` → 周五、`周日` → 周日），推断不到再按课时数兜底。
不要按 `TimeLayouts` 字典的插入序或字典序取作息——档案里那个字典是**乱序**的。

**未绑定班级的设备**：`default_classplan` 被写成「合法空信封」（只有两个课表群、无课表），
配合 §4.2 的资源存在性兜底，既不误发别班课表，也不会因 404 触发 CCProtect 自封 IP。

---

## 6. 插件事件表与命令矩阵

### 6.1 命令矩阵（`CommandTypes` 对齐）

| 命令类型 | 载荷 | 执行方 | 现状 |
|---|---|---|---|
| `SendNotification` | 标题+内容 | `StelarithNotificationProvider.Push` | ✅ 已闭环（cdb095f） |
| `stelarith_task: lock` | — | `OSActions` 锁屏 | ✅ |
| `stelarith_task: screenshot` | — | `OSActions` 截图 + 上传回传 | ✅（回传待补） |
| `stelarith_task: shell` | 命令串 | `AgentClient` 本地代理 | ✅ |
| `stelarith_task: remote_control_start/stop` | 会话参数 | `AgentClient` VNC/远程桌面 | ✅ |
| `stelarith_task: reboot` | — | `AgentClient` / 插件调系统 | ✅（转代理） |
| `stelarith_task: power_off` | — | 插件调系统 `shutdown` | ❌ **待补** |
| `stelarith_task: volume_set/mute` | 音量值 | 插件调系统音频 API | ❌ **待补** |
| `stelarith_task: media_play/pause/next` | — | 插件调系统媒体键 | ❌ **待补** |
| `DataUpdated` | — | 触发 sync 立即拉新资源 | ✅ |

> 新命令类型一律注册进 `StelarithCommandHandler.HandleRawAsync` 的路由表，命名走官方 `CommandTypes` 体系（自定义类型放 `stelarith_task` 命名空间下，避免与官方枚举冲突）。

### 6.2 插件事件/回调表（下行 → 设备）

| 事件 | 触发 | 处理器 |
|---|---|---|
| `CommandReceived`（gRPC） | 服务端下发命令 | `StelarithCommandHost` 静态守护订阅 → `HandleRawAsync` |
| HTTP poller 轮询命中 | 兜底通道 | `StelarithCommandPollerService.PollOnceAsync`（已修大小写/线程/keep-alive） |
| 资源版本变化 | sync 拉取发现 | `StelarithSyncService` → 应用 + 上报 ack |

### 6.3 上行事件表（设备 → 服务端，v2 新增）

| 事件 | 通道 | 载荷 | 服务端动作 |
|---|---|---|---|
| 配置快照上报 | gRPC `ConfigUploadScReq` | 7 类资源当前 JSON | 校验 + 落库 + 策略判定（直接生效/待审） |
| 命令执行确认 | HTTP `POST /command/ack` | command_id + status | 置 done/failed |
| 审计事件 | gRPC `AuditScReq` | 事件类型 + 详情 | 落审计表（状态面板） |

插件侧新增：`StelarithUploadService`（静态守护线程 + gRPC 上行 stub），与 poller/host 同范式（不依赖宿主 StartAsync）。

---

## 7. 降级与兼容矩阵

| 故障场景 | 降级路径 | 兜底行为 |
|---|---|---|
| gRPC 断连 | HTTP poller 轮询兜底 | 每 5s 轮询 `command/queued`；互斥锁防双通道重复消费 |
| 通知提供方注册失败 | `StelarithCommandHandler.Notify` 降级托盘气泡 | 已有实现 |
| 设备离线 | 命令保持 `delivered` | 重连后拉 ack 状态，未 ack 超时回 `pending` 补发 |
| 旧客户端拉到新资源 | 版本兼容 | 跳过不支持的资源类型，不阻塞整体同步 |
| 客户端本地无班级绑定 | manifest 回退自身 `client_profiles` | 老行为不变，平滑迁移 |
| 资源 JSON 引用不完整 | 编辑接口校验拦截 | 拒绝写入，返回具体缺项 |
| 后端限流（CCProtect 封禁） | 插件退避重试 | poller 429 时指数退避，勿硬顶触发封禁 |

---

## 8. 实施路线

### Phase 1 · 班级层（✅ 已实施 2026-09-14）

- **目标**：模型里出现「班级」，manifest 按班级聚合
- **改动**：`classes` + `class_resource_sets` 建表；`client_profiles.class_id`；`pairing_codes.class_code/class_id`；manifest 解析优先级改造；管理端班级 CRUD API
- **验证**：建两个班级 → 各配设备 → 各自 manifest 返回不同课表 → 未绑定设备回退正常（进程内 ASGI 探针通过）

### Phase 2 · 课表编辑与推送（✅ 已实施 2026-09-14）

- **目标**：管理端能改班级课表并广播生效
- **改动**：编辑器方案 A（表单生成三件套）+ 引用完整性校验 + 按班级广播命令（`target_class` 展开）+ `DataUpdated` 触发即时 sync
- **验证**：改初三1班课表 → lab-pc-001/002 同步生效，初三2班不受影响（引用校验拒/通 + 写课表落库 + 广播落 queue 探针通过）

### Phase 3 · 上行回传与命令确认（✅ 已实施 2026-09-14）

- **目标**：客户端能回传课表改动；命令不丢
- **改动**：`command_queue.ack_status` 语义迁移；`POST /command/ack`；`ConfigUploadScReq` servicer；插件 `StelarithUploadService` + 配置变更监听
- **说明**：`ConfigUploadScReq`/`AuditScReq` servicer 实测后端早已实现（`config_upload.py`/`audit.py`），非「未实现」；本 Phase 落地的是 ack 语义 + 插件自动确认
- **验证**：kill 客户端 → 注入命令 → 重连后补发执行；实时闭环 inject→poller process→ack 200→DB done 探针通过

### Phase 4 · 真实课表导入与官方格式兼容（✅ 已实施 2026-09-14）

- **目标**：清掉测试班级/资源，把 12 个班的真实课表按**官方格式**落库；同时给出「手动添加课表」能力
- **改动**：
  - `app/services/schedule_importer.py`：官方档案解析（切班）+ Profile 信封构造 + 导入自检 + 落库编排
  - `scripts/import_classes_from_profile.py`：CLI 导入（`--reset` 清空 + `--assign` 分派设备）
  - `POST /class/import-from-profile`、`POST /class/{id}/apply-week-template`、`GET /class/{id}/schedule`
  - **修正三类资源载荷为官方 Profile 信封**（ClassPlan/TimeLayout/Subjects）——此前是裸对象，客户端会静默不显示
  - `manifest.py` 资源存在性兜底；`timetable_validator.py` 适配 Subjects 字典格式 + 信封/裸对象双读
  - 资源模型补 `server_default`（裸 SQL 插入不再撞 NOT NULL）+ `scripts/migrate_notnull_defaults.py` 回填老 Schema
  - 修正 `class_routes.py` 路由重复前缀（`/class/class/*` → `/class/*`）
- **验证**：12 班 manifest 各自解析到 `cp_classNN`；资源内容断言为官方信封且课表全落在默认课表群；
  12 班周一课表逐班核对；悬空引用演练（`cp_不存在` → 自动兜底 `default_classplan` 且 200）；
  手动添加空周课表 = 6 天骨架且节数与作息一致；旁路租户 `rebuild_min_tenant` 全流程（12 班 + 7 类资源 + DataUpdated）通过

### Phase 5 · 控制矩阵与运维完善（建议 0.5 周）

- **目标**：控制矩阵补全，生产可运维
- **改动**：`power_off` / `volume_set` / `media_*` 命令落地；审计事件 servicer + 管理端状态面板；截图回传
- **验证**：控制台逐命令下发到真机执行；离线/限流场景全矩阵回归

---

## 9. 风险与开放问题

| # | 风险/问题 | 说明 | 缓解 |
|---|---|---|---|
| 1 | 班级资源集与设备覆盖的优先级冲突 | 设备级覆盖（老机制）与班级级（新机制）同时存在 | manifest 解析明确优先级：class_id 绑定后班级优先，设备列仅作无班级回退 |
| 2 | `ConfigUploadScReq` 官方载荷结构未完全确认 | protobuf 骨架在，字段含义需对照官方客户端上报实现 | Phase 3 前用官方客户端真机抓包验证 |
| 3 | 「GET 即 done」历史数据迁移 | 存量 done 命令无 ack 字段 | 迁移脚本：`done` → `ack_status='done'`，旧语义等效 |
| 4 | 双通道互斥的竞态 | gRPC 与 poller 同时命中同一条命令 | 服务端「delivered 原子抢占」+ 客户端静态锁；Phase 3 统一验证 |
| 5 | 编辑器方案 A 的课表网格 UX | 表单生成体验一般 | Phase 2 交付最小可用版，方案 C 后置评估 |

---

*本文档为设计基线，实现过程中的字段/路径微调以代码为准；涉及协议对齐的决策须对照 ClassIsland 官方源码/协议文档复核。*
