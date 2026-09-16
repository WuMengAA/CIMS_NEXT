"""CC 追踪与 IP 单黑名单验证核心逻辑。"""

import time
from .state import set_cc_state, get_ip_failures_dict, get_global_requests

# 限频防御配置指引
IP_FAIL_MAX = 5  # 窗口内单一 IP 允许极度失败的最大阈值
CC_GLOBAL_MAX = 2000  # 触发 CC 拦截的综合请求频率阈值
WINDOW_SEC = 60  # 计算窗口时间（秒）

# 失败记录字典的容量上限。公网环境下扫描器会不断制造新 IP，
# 不设上限的话这个字典会随时间无限增长。
_IP_FAIL_ENTRY_MAX = 10000


def _clean_old(queue, now: float):
    """清理全局队列中超时的历史请求时间节点。"""
    while queue and queue[0] < now - WINDOW_SEC:
        queue.popleft()


def _prune_stale(now: float):
    """清理已过窗口的空/过期条目，并兜住字典容量。

    注意：**读路径绝不能写入字典**。旧实现用 `setdefault(ip, [])` 查询，
    等于每来一个陌生 IP 就给字典新增一个键 —— 公网下这就是一个缓慢的内存泄漏。
    """
    d = get_ip_failures_dict()
    if len(d) <= _IP_FAIL_ENTRY_MAX:
        return
    for k in list(d.keys()):
        fails = d.get(k)
        if not fails:
            d.pop(k, None)
            continue
        while fails and fails[0] < now - WINDOW_SEC:
            fails.pop(0)
        if not fails:
            d.pop(k, None)
        if len(d) <= _IP_FAIL_ENTRY_MAX // 2:
            break


def check_ip_blocked(ip: str) -> bool:
    """实时判定给定 IP 是否因为高频异常响应被列入管控封禁。"""
    now = time.time()
    fails = get_ip_failures_dict().get(ip)
    if not fails:
        return False
    while fails and fails[0] < now - WINDOW_SEC:
        fails.pop(0)
    if not fails:
        # 顺手回收空条目：这个 IP 已"洗白"，没必要继续占位
        get_ip_failures_dict().pop(ip, None)
        return False
    return len(fails) >= IP_FAIL_MAX


def record_ip_failure(ip: str):
    """于数据库登录失效或其余非标准路径出错时登记一次。"""
    if not ip:
        return
    now = time.time()
    get_ip_failures_dict().setdefault(ip, []).append(now)
    _prune_stale(now)


def monitor_global_frequency():
    """每次收到请求时测算整体水位线，并在暴增时激化全局保护态。"""
    now = time.time()
    reqs = get_global_requests()
    reqs.append(now)
    _clean_old(reqs, now)
    set_cc_state(len(reqs) > CC_GLOBAL_MAX)
