# 后端测试基线（P2-124）

> 记录时间：2026-09-18 · 环境：本机 Windows + PostgreSQL 17 + Memurai(Redis)
> 目的：把「当前已知的失败」固定下来。后续改动只需比对是否引入**新**失败，
> 而不是被既有的红点淹没（也避免把既有失败误判成自己刚改出来的回归）。

## 怎么跑（务必带端口覆盖）

生产后端在跑时已占用 8096/8097/8098/8100，直接 `pytest` 会以
`RuntimeError: Failed to bind to address [::]:8100` 全军覆没（表现为一大片 ERROR）。
用脚本把测试端口挪开即可，**不必停生产后端**：

```bash
bash scripts/run-tests-baseline.sh                      # 全部（逐文件，200s 上限/文件）
bash scripts/run-tests-baseline.sh tests/test_api.py    # 单个文件
```

脚本等价于：

```bash
CIMS_CLIENT_PORT=18096 CIMS_MANAGEMENT_PORT=18097 CIMS_ADMIN_PORT=18098 CIMS_GRPC_PORT=18100 \
  .venv/Scripts/python.exe -m pytest tests/test_xxx.py -q --no-header -p no:cacheprovider
```

> 端口来自 `CIMSSettings`（字段 `cims_grpc_port` 等，无 `env_prefix` →
> 环境变量 `CIMS_GRPC_PORT` 生效），优先级高于 `.env` 里写死的 8096-8100。

> 为什么要逐文件跑：本机全量一次 **>15 分钟**，且单个慢文件会把整体拖垮
> （用整跑 + 全局 `timeout` 时，超时会连汇总都拿不到，只剩半截进度点）。

## 基线（2026-09-18）

| 文件 | 结果 |
|---|---|
| tests/test_api.py | 4 failed, 11 passed |
| tests/test_coverage.py | 7 failed, 1 passed |
| tests/test_device_auto_profile.py | 4 passed |
| tests/test_grpc.py | 24 passed |
| tests/test_mock_client_lifecycle.py | 6 passed |
| tests/test_multi_tenant_isolation.py | 5 passed |
| tests/test_oobe.py | 1 failed, 31 passed |
| tests/test_permission_matrix.py | 5 passed |
| tests/test_permissions.py | 5 passed |
| tests/test_quota_enforcement.py | 6 passed |
| tests/test_quotas.py | 5 passed |
| tests/test_roles.py | 6 passed |
| tests/test_security_attacks.py | 1 failed, 12 passed |
| tests/test_totp_2fa.py | 12 passed |
| tests/test_user_auth.py | 7 passed |
| **合计** | **13 failed, 140 passed** |

## 已知失败（13）——均为既有问题，非本次功能改动引入

1. **test_api.py + test_coverage.py（11 条）：管理端鉴权期望漂移。**
   典型是 `assert 401 == 403` / `assert 401 == 200`：
   `AdminAuthMiddleware` 对「无令牌 / 令牌无效」一律返回 401
   （日志：`认证失败：ip=... path=/account/<id>/ClassPlan/list`），
   而测试期望 403（无权限）或 200（带 command 令牌放行）。
   属**测试期望与现行中间件语义不一致**，与业务功能无关。
   涉及：`test_get_client_manifest(_with_profile)`、`test_get_ip_auth_fail`、
   `test_command_with_invalid_token`、`test_command_edge_cases`、
   `test_client_details_endpoint`、`test_client_status_with_session_manager`、
   `test_update_data_command`、`test_send_notification_command`、
   `test_get_config_command`、`test_command_endpoints_without_servicer`。

2. **test_security_attacks.py（1 条）：测试对 DB 脏数据敏感。**
   `IntegrityError: 重复键违反唯一约束 "ix_users_username"` ——
   反复跑会累积同名测试用户，第二次起即撞唯一约束。清库或改用随机用户名可解。

3. **test_oobe.py（1 条）：OOBE 相关断言（非本次改动引入）。**

## 历史修复（本基线建立时一并处理）

- **`asyncio` 未导入 → 23 个 ERROR**：`app/apps/lifespan_shutdown.py` 新增的
  「停止定时广播调度器」用了 `asyncio.wait_for`，但该文件只 `import logging`，
  未 `import asyncio`。凡触发应用 shutdown 的测试（test_api / test_coverage）
  在**teardown 阶段**抛 `NameError: name 'asyncio' is not defined`，
  表现为 15 + 8 = 23 个 ERROR。补 `import asyncio` 后 ERROR 全消，
  真实结果露出（11 failed, 12 passed）。**教训：新增 `asyncio` 用法务必核对 import。**
