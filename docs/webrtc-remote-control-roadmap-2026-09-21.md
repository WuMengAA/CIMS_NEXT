# 票 #247 — WebRTC 远控交底与路线决策

- 文档日期：2026-09-21（夜班修复窗口·并行 agent 交底）
- 范围：被控端（桌面端 Windows / 安卓）的 WebRTC 远控能力现状、缺口与路线决策
- 结论性质：**诚实交底**。所有结论均有代码证据（见下方「代码证据」一节，含文件路径与行号），不臆造已实现能力。

---

## 0. 一句话结论

**被控端（Windows 代理 + 安卓）当前没有 WebRTC 实现。** 远程屏幕接管目前由被控端启动 **VNC 服务** 实现；媒体（抓拍/录像）由 ffmpeg + 局域网令牌直连实现，且被控端代码自证「这是局域网直连，不是 WebRTC」。后端的 WebRTC 信令链路（边车 + 公网透传 + 设备令牌派生）已具备，但**没有对端（被控端）会说 WebRTC**——管控端那套 WebRTC 前端也只是未接入主控制台的孤立原型。WebRTC 远控在当前代码里是「骨架已搭、肌肉为零」。

---

## 1. 现状（代码证据）

> 取证范围：`Stelarith-cims-eval/school-multimedia-control/ext/stelarith-agent`（被控端 Rust 代理）、`Stelarith-cims-eval/CIMS-backend/app/ext/p2p_signal` 与 `app/api/client/signal_proxy.py`（信令）、`Stelarith-cims-eval/school-multimedia-control/static/console`（管控端前端原型）。

### 1.1 被控端（Windows Rust 代理）完全没有 WebRTC

- 被控端源码仅有 2 个文件：`ext/stelarith-agent/src/main.rs`、`ext/stelarith-agent/src/media.rs`。
- 在这两个文件及整个 `ext/stelarith-agent/src` 内检索 `webrtc|p2p|peer|datachannel|rtc|screen_capture|behavior`，**零命中 WebRTC/RTCPeerConnection/DataChannel**；仅命中 VNC 与媒体直连。
- `media.rs:14-19` 自带诚实声明（原文）：

  > 上面第 3 点是**局域网直连**，不是 WebRTC。校园网里教室机与运维终端本来就在同一三层网络内，直连可达……但**真正的 NAT 穿越（STUN/TURN/ICE、DataChannel）没有实现** —— 跨网段或公网访问时当前方案连不上。这一点必须在文档与面板上写明，不能拿 `media_mode=p2p` 这样一个配置值去充当能力。

- `main.rs:7` 描述被控端职责：「执行 OS 动作：锁屏 / 重启；按需启动 VNC 服务实现远程屏幕控制」。远控 = **VNC**，不是 WebRTC。
- `main.rs:399-553`：`VncSession`，通过环境变量 `STELARITH_VNC_CMD` 拉起 VNC 服务，**仅绑 localhost + 令牌**，并经扩展网关 `/vnc-session` 回报面板（`media.rs:665` `report_session("vnc", …)`）。即：屏幕接管走 VNC，且**必须同网段/经网关可达**。

### 1.2 后端信令链路已存在，但只做「传输」，不做「编解码」

- `CIMS-backend/app/ext/p2p_signal/client.py:1-6` 自述：边车客户端「只负责『拉起 + 探活 + 注入按设备令牌』，**不参与 WebRTC 编解码**」。
- `client.py:131-150` 提供 `mint_secret` / `p2p_credentials`：用 HMAC-SHA256 确定性派生设备信令令牌（与 website 侧同算法）。这是**信令鉴权就绪**，不是 WebRTC 媒体就绪。
- `CIMS-backend/app/api/client/signal_proxy.py`：把 `/socket.io/*` **原样透传**到本机信令边车（:18110）的 WebSocket 代理，作为公网信令入口。仅转发信令帧，不理解 WebRTC 协议。
- 结论：信令「通道」已通，但 WebRTC 的**两端（尤其被控端）缺实现**，通道是空管。

### 1.3 管控端（面板）有 WebRTC 前端原型，但是孤立的、且对端不存在

- `school-multimedia-control/static/console/p2p-connector.js`：封装 `RTCPeerConnection`（`_newPc()` 第 ~98 行）、`startController`（发 offer）、`startControlled`（被控：收 offer 回 answer 并发送桌面流）、`_wireDataChannel`（鼠标/键盘行为 `BEHAVIOR` 经 `stelarith-ctrl` DataChannel 下发）。这是**控制端**代码。
- `school-multimedia-control/static/console/remote-webrtc.js`：控制端远控视图，调用 `p2p.connect()`、`p2p.startController(null)`、`onTrack` 接收屏幕、`sendBehavior` 下发鼠标/键盘。
- **关键缺口**：在 `static/console/app.js`（主控制台）和 `admin-console/src/app.js` 中检索 `remote-webrtc|p2p-connector|StelarithP2P|startController|sendBehavior`，**零命中**。即 `remote-webrtc.html` 仅孤立引用 `remote-webrtc.js`，**未被主控制台接入**，是实验性原型而非在用品。
- 即使人为把该原型接入：控制端 `startController` 发出 offer，需被控端 `startControlled` 回桌面流并接收行为 DataChannel——而被控端 Rust 代理无 WebRTC、只有 VNC，因此**连接永远等不到对端应答**（控制端停在「发起远控…」，实际无流、无控制回路）。

### 1.4 安卓被控端

- 本工作区未检出安卓被控端源码（检索 `*android*`、`Stelarith-*app*` 及 `Stelarith-cl`/`Stelarith-Portal` 内 `RTCPeerConnection|webrtc` 均零命中）。
- 因此：**不能断言安卓被控端有 WebRTC**；当前可验证的安卓能力状态为「未知/未在本仓库落地」。若安卓被控端另存于他处，须独立核验后再更新本文。

### 1.5 现状小结（诚实版）

| 能力 | 被控端（Windows 代理） | 后端信令 | 管控端（面板） |
|------|------|------|------|
| WebRTC 媒体流 | ❌ 无 | —（不编解码） | ⚠️ 原型存在、未接入 |
| WebRTC DataChannel（鼠标/键盘控制） | ❌ 无 | — | ⚠️ 原型存在、未接入 |
| 信令通道（边车+透传+令牌） | — | ✅ 已就绪 | ✅ 可连 |
| 实际生效的远控 | ✅ VNC（仅同网段/网关可达） | 透传 VNC 会话地址 | 经 VNC 接入 |
| 跨网段/公网远控 | ❌ 不可达（无 NAT 穿透） | — | — |

---

## 2. 远控能力缺口与风险

1. **跨网段/公网不可达**：被控端远控依赖 VNC 且 LAN 直连，无 STUN/TURN/ICE。运维终端若不在同一三层网（如居家/外网），无法接管教室机。这是当前最硬的缺口。
2. **WebRTC 实为「空管」**：信令全链路就绪，但两端无 WebRTC 实现，存在「看起来支持 WebRTC、其实连不上」的误导风险（与 `media.rs:19` 自警一致）。
3. **原型未接入**：管控端 WebRTC 前端若被误当成已上线能力对外承诺，会造成交付预期错位。
4. **安卓被控端未核验**：能力状态不明，若对外承诺「安卓远控」则无依据。

---

## 3. 路线决策（短期 / 中期）

### 3.1 推荐路线（综合研判）

> **短期：复用现有信令 + VNC 中转通道，把「跨网段远控」先做通，并诚实标注「非 WebRTC」；暂缓 WebRTC。中期：排期实现被控端 WebRTC（先 Windows 代理，后安卓），补齐 NAT 穿透与 DataChannel 控制回路。**

### 3.2 短期方案（0–数周，低风险）

- **A. 复用现有信令 + 中转的 VNC 远控（推荐）**
  - 做法：VNC（被控端已支持，`main.rs:540` 拉起）经后端 8096/网关中转（类似 `signal_proxy.py` 的透传模式）暴露给管控端，复用已就绪的令牌鉴权（`client.py` 的 HMAC 派生）。
  - 收益：被控端**零改动**即可获得跨网段远控；复用既有安全模型（localhost+令牌、网关 fail-closed）。
  - 成本/风险：VNC 走中转带宽高于 P2P；需确保 VNC 连接也经 TLS/令牌，不能裸暴露。风险低、可逆。
- **B. 诚实提示（必须同步做）**：在管控端远控入口，凡涉及「P2P/WebRTC」措辞处，明确标注「当前为 VNC/中转，非 WebRTC，跨网段需经中转」。见第 4 节最小改动建议。
- **C. 冻结 WebRTC 远控承诺**：`remote-webrtc.html` 原型保留但不对外承诺；在文档与面板显式标注「实验性、对端未实现」。

### 3.3 中期方案（排期实现 WebRTC）

- **被控端 WebRTC（先 Windows 代理）**：在 Rust 代理中引入 WebRTC 栈（如 `webrtc` crate 或本地 WebView/Agent 桥接），实现 `startControlled` 对侧逻辑：采集桌面帧→`addTrack`、接收 `stelarith-ctrl` DataChannel 的鼠标/键盘行为→注入 OS 输入。这是真正补齐「WebRTC 远控」的必须项。
  - 工作量评估：被控端是整个链路最重的一块（桌面采集 + 输入注入 + ICE/TURN）。建议独立排期，不与短期方案并行混淆。
- **安卓被控端**：先核验是否存在、能力状态（见 1.4），再决定是否排期；不要在未核验情况下纳入承诺。
- **NAT 穿透**：被控端补齐 STUN/TURN/ICE（含自建 TURN 兜底），否则跨网段仍依赖中转。

### 3.4 时间 / 风险权衡

| 路线 | 时间 | 风险 | 收益 | 建议 |
|------|------|------|------|------|
| 短期 A：VNC 经信令中转 | 低（被控端零改） | 低 | 立得跨网段远控 | ✅ 推荐立即做 |
| 短期 B：诚实标注 | 极低（仅 UI 文案） | 极低 | 消除误导 | ✅ 必须同步 |
| 中期：被控端 WebRTC | 高 | 中（输入注入/采集稳定性） | 真·P2P 低带宽远控 | 排期，不与短期抢优先级 |
| 暂缓 WebRTC（仅做 A/B） | — | — | 满足多数现场需求 | ⚠️ 可作为折中 |

> 决策建议：**不假装已有 WebRTC**。先用短期 A 解决「能不能跨网段接管」的刚需，用 B 消除误导；WebRTC 作为中期排期，待被控端实现后再把 `remote-webrtc` 原型真正接入。

---

## 4. 最小 UI 改动建议（仅建议，写入本文；默认不改代码）

> 铁律：优先只出文档。以下为「若决定加诚实提示」的最小改动清单，需由对应仓库负责人确认后实施，不在本票跨仓库实际落地。

- **管控端远控入口**：在远程控制页/按钮旁增加一行静态说明，文案示例：「当前远控基于 VNC/中转通道（非 WebRTC），需被控端与运维端网络可达；跨网段经服务端中转。」
- **涉及「P2P/WebRTC」的文案**：将 `static/console/app.js:1490 / 1553`、`admin-console/src/app.js:1879 / 1942` 中把画面直连描述为「WebRTC P2P」的段落，补一句「被控端当前未实现 WebRTC，实际为局域网/中转直连」。
- **`remote-webrtc.html` 原型**：页眉加实验性标注「本页为 WebRTC 远控原型；被控端尚未实现 WebRTC，当前无法连通」。避免被误当在用品。
- 改动范围均限本文所述仓库内、纯文案/标注，不触碰信令或被控端逻辑；实施前需负责人复核。

---

## 5. 后续动作清单（建议）

1. 落地短期 A：VNC 经信令中转的跨网段远控（被控端零改，后端加一路透传/令牌校验）。
2. 落地短期 B：按第 4 节加诚实标注。
3. 冻结 WebRTC 远控对外承诺，直到被控端实现。
4. 立项中期：被控端（Windows 代理）WebRTC 实现 + TURN 兜底；安卓被控端先核验后排期。
5. 本交底文档与记忆文件（见下）同步归档，作为后续不重复踩坑的依据。

---

## 附：取证命令与证据索引

- 被控端无 WebRTC：`grep -rinE "webrtc|p2p|peer|datachannel|rtc|screen_capture|behavior" ext/stelarith-agent/src` → 零命中（仅 vnc/media）。
- 自证非 WebRTC：`media.rs:14-19`。
- 远控=VNC：`main.rs:7, 399-553`；`media.rs:665, 697`。
- 信令通道已就绪：`./app/ext/p2p_signal/client.py:1-6, 131-150`；`./app/api/client/signal_proxy.py`。
- 控制端 WebRTC 原型存在但未接入：`static/console/p2p-connector.js`、`remote-webrtc.js`、`remote-webrtc.html`；`grep` 主控制台 `app.js` 零引用。
