# ClassIsland 集控激活 · 运维/部署联调清单

> 编制时间：2026-09-12 21:xx（GMT+8）
> 面向：让一台装了 `StelarithControlPlugin` 的 ClassIsland 教室端，真正能被 CIMS 集控后台下发 `stelarith_task` 命令（lock/screenshot/refresh/restart 等轻动作 + 转发 StelarithAgent 17999 的重动作）。
>
> __重要前提（已实测确认）__：设备控制能否真生效，**根因不在 CIMS 后端**，而在 **ClassIsland 侧未激活集控**。CIMS 下端点多完整，设备侧命令通道是关的，命令就永远不达。本清单解决的就是这条"设备端通道"。

---

## 一、现状快照（已验证的事实）

| 项 | 状态 | 证据 |
|---|---|---|
| ClassIsland 进程 | ✅ 运行中 | `ClassIsland.Desktop.exe`（PID 37248，09-12 时） |
| StelarithControlPlugin | ✅ 已安装 | `D:\Classlsland\data\Plugins\StelarithControlPlugin\`（dll 09-12 13:55 构建） |
| 插件命令通道 | ⚠️ 依赖宿主 | 订阅 ClassIsland 的 `IManagementServerConnection.CommandReceived` 事件（收到带 `stelarith_task` 载荷的命令后本地执行） |
| 集控凭证 | ❌ 未激活 | `D:\Classlsland\data\Config\Management\Credentials.json` → `IsActive:false`，User/Admin 凭证为空 |
| 集控策略 | ❌ 未激活 | `D:\Classlsland\data\Config\Management\Policy.json` → `IsActive:false` |
| 集控服务端地址 | ⚠️ 指向远端 | `Settings.json` → `https://www.245959623.xyz/api/classisland/announcements`（voicehub 同域，非本机 CIMS） |
| CIMS 四端口 | ✅ 全开 | client 8096 / management 8097 / admin 8098 / gRPC 8100 |
| CIMS 在线设备 | ⚠️ 0 个 | Redis db1 `online:*` 键数量 = 0 |

**核心结论**：这台 ClassIsland **未激活集控、未连接任何集控服务端**，因此插件的 `CommandReceived` 事件永不触发，CIMS 下发必然"静默落地"。

---

## 二、两类部署形态（先决定你的目标场景）

### 形态 A：接入现成远端集控服（最快，复用 voicehub/245959623.xyz）
若远端 245959623.xyz 已提供 ClassIsland 集控服务端，且 admin 端能下发 stelarith_task：

```
ClassIsland(教室端) —— 集控连接 ——> 245959623.xyz 集控服务端
        │                                      │  CIMS 后端下发
        ▼                                      ▼
  插件 CommandReceived —— 执行 stelarith_task    管理后台/API
```

- __优点__：无需改 CIMS，跨校园统一管理。
- __动作__：在 ClassIsland UI "设置 → 集控管理" 填入远端服务端地址 + 连接码/凭证，确认 Credentials/Policy 从 `IsActive:false` 变 `true`。

### 形态 B：接入本机 CIMS（自管，开发/单机组网验证）
让本机 CIMS 充当教室端的集控服务端来源之一，或把 CIMS 的设备指令桥接到某集控通道：

```
ClassIsland(教室端) —— (通过集控连接/桥) ——> CIMS 后端
        │                                        ▲  send_command → client_queues
        ▼                                        │
  插件 CommandReceived —— 执行 stelarith_task      AccountContext(account_id)
```

- __前提__：需确认 CIMS（节点）是否实现了 ClassIsland 的 `IManagementServerConnection` 服务端协议；若当前没有，则形态 B 需要额外的"集控桥接/中继"，非 ClassIsland 直连 CIMS gRPC 8100。
- __动作__：把集控服务端指到本机 CIMS（含对应 connect code），并确认 Redis `online:*` 开始出现键。

> ⚠️ **务必澄清一个易踩的坑**：插件命令通道**不是**"ClassIsland 直连 CIMS 的 gRPC 8100"。gRPC 8100 是 CIMS 原生 client 注册通道，与插件收单的 `IManagementServerConnection` 是两套。**别把 CIMS gRPC 端口塞给 ClassIsland 的集控地址**。

---

## 三、Step-by-Step 联调清单

### Step 0 · 前置检查（每项都过，缺一不可）
- [ ] CIMS 四端口 `8096/8097/8098/8100` 探活（`Test-NetConnection 127.0.0.1 -Port 8xxx`）
- [ ] CIMS 用 .venv 启动无误、Redis(db1) 可达
- [ ] ClassIsland 进程正常，无插件加载红字报错
- [ ] 确认远端/本机服务的**集控 connect code / 连接码**存在可用

### Step 1 · 决定形态并配置集控服务端地址
- [ ] 形态A：填远端 `245959623.xyz` 集控地址；形态B：填本机 CIMS 集控地址
- [ ] ClassIsland UI：`设置 → 集控管理 → 服务器/连接码` 逐项填写

### Step 2 · 激活集控（关键，当前为 false）
- [ ] 重新核对 `D:\Classlsland\data\Config\Management\Credentials.json` → `IsActive` 应变 `true` 且 User/Admin 凭证非空
- [ ] `Policy.json` → `IsActive` 变 `true`，存在可下发的策略条目
- [ ] 若仍 false：确认连接码正确、服务端可达、ClassIsland 版本与服务端协议匹配

### Step 3 · 验证插件命令通道打开
- [ ] ClassIsland 日志（`D:\Classlsland\logs\`）出现 `IManagementServerConnection` 已连接/已订阅 **CommandReceived** 的记录
- [ ] 从 ClassIsland 侧手动发一条测试命令，确认插件本地执行（轻动作如弹窗/锁屏）

### Step 4 · 打通 CIMS 端点（需 account_id + 在线设备）
- [ ] 准备一个真实账户：该 user 在某 Account 中有 `account_members` 记录（角色 owner/admin/member）且具备设备控制权限
- [ ] 确认该 Account 下的 client 已注册（`GET /account/{account_id}/client/list` 非空）
- [ ] 确认目标 client 在线：Redis db1 `online:{tid}:{cuid}` 有键
- [ ] 下发验证：`POST /account/{account_id}/client/{cid}/command/restart` → 预期设备重启

### Step 5 · 端到端验收（最终判据）
- [ ] 教室端收到 restart/update-data 并执行（设备行为变化）
- [ ] 设备状态在 CIMS 侧可见（online 状态、最近下发时间）
- [ ] 记录验收时间与结果，回写验证报告

---

## 四、常见阻塞排查表

| 现象 | 最可能原因 | 动作 |
|---|---|---|
| `Credentials/Policy IsActive` 恒为 false | 连接码错误 / 服务端不可达 / 版本不匹配 | 核对连接码、探活地址、升级匹配 |
| 插件加载但收不到命令 | 集控未激活，`CommandReceived` 未订阅 | 先过 Step 2/3 |
| 端点报 401/403 | user 无该 Account 成员关系或权限 | 建 `account_member` 记录并授权限 |
| `client_queues` 无 key 静默丢弃 | 目标 client 未在线 | 先确认设备在线（Step 4） |
| `.local` 邮箱登录 422 | sync 宽松 vs 登录 `EmailStr` 严格(已知缺口) | 用真实域名邮箱，或待后端统一校验 |

---

## 五、涉及环境/文件（只读核对清单）
```
D:\Classlsland\data\Config\Management\Credentials.json    # 集控凭证（IsActive）
D:\Classlsland\data\Config\Management\Policy.json          # 集控策略（IsActive）
D:\Classlsland\data\Settings.json                          # 集控/公告地址(245959623.xyz)
D:\Classlsland\data\Plugins\StelarithControlPlugin\        # 插件(dll 09-12)
D:\Classlsland\logs\                                       # ClassIsland 日志
```
CIMS 相关（后端已就绪，无需改即能验的方向）：
```
POST /account/{account_id}/client/list                      # 设备列表/在线
POST /account/{account_id}/client/{cid}/command/restart     # restart 下发→RestartApp
POST /account/{account_id}/client/{cid}/command/update-data # update-data→DataUpdated
```

---

## 六、结论与建议

- ✅ **CIMS 后端侧已就绪**：设备端点完整、下行机制正确、四端口全开，可以作为集控"控制面"直接使用。
- ❌ **设备端命令通道未开**：本机 ClassIsland 未激活集控（Credentials/Policy `IsActive:false`）、未连服务端——这是当前唯一硬阻塞，且**不在 CIMS 代码**，需在 ClassIsland 侧完成激活。
- ⏭ **建议**：若目标是"自管教室端"，优先让本机 CIMS 提供集控服务端能力并对接（形态 B），完成 Step 0→4；若目标是"快速让一台设备可控"，走现成远端（形态 A）最快。本清单即为两端联调的操作依据。
