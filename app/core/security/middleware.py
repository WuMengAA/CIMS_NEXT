"""FastAPI 提供防 CC 高频拦截与异常 IP 限流中间件支撑。"""

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from app.core.client_ip import get_real_client_ip
from .tracker import check_ip_blocked, record_ip_failure, monitor_global_frequency, WINDOW_SEC
from .state import get_cc_state
from .codes import ERR_CC_ACTIVE, ERR_IP_BLOCKED

# 只有这些路径上的 401/403 才算「疑似爆破」并计数。
# 正常业务路径上的 401/403 是鉴权/授权的正常结果（未登录、权限不够），
# 把它们算作失败会让「老师刷新几次面板」直接触发全站封禁。
_ATTACK_PATH_HINTS = ("login", "register", "auth", "token", "password")


def should_count_failure(path: str, status: int) -> bool:
    """判定这次 ≥400 响应该不该计入「单 IP 失败次数」。

    历史教训（2026-09-22，用户报「班级下拉切不动、显示演示班级」）：
    旧实现是 `if resp.status_code >= 400: record_ip_failure(ip)`，即**任何** 4xx 都计数。
    在本地/校园部署下（所有设备与所有老师共用同一个出口 IP），后果是：
      · 面板刚打开、还没登录 → `/api/me`、账号接口回 401 → 计 5 次 → 封 60 秒；
      · 任意一次点到不存在的资源 → 404 → 计 1 次；
      · 封禁期间 `/class/list` 全部 429 → 面板班级下拉拿不到数据 → 静默降级成演示数据。
    也就是说：**防护机制自己制造了故障**，且是"全校一起被封"。

    现在的口径（宁可漏挡扫描器，也不误封真实用户）：
      · 404            → 计数（就是这个 IP 在探不存在的资源，扫描器特征，保留防护）
      · 401 / 403      → 仅当路径含 login/register/auth/token/password 时计数（爆破防护）
      · 429            → 不计数。已经在封禁里，再计数会让封禁自我续期、永不解封
      · 400/405/409/422→ 不计数。这是客户端把请求写错了，属于正常业务错误
      · 5xx            → 不计数。服务端自己的问题，不能反过来惩罚客户端
    """
    if status >= 500 or status == 429:
        return False
    low = (path or "").lower()
    if status == 404:
        return True
    if status in (401, 403):
        return any(k in low for k in _ATTACK_PATH_HINTS)
    return False


class CCProtectMiddleware(BaseHTTPMiddleware):
    """用于应用侧的整体防护：阻断非法 IP 以及遭遇全域攻击时降级服务。"""

    async def dispatch(self, request, call_next):
        """挂载过滤清洗规则与异常频率溯源记录机制。"""
        # 必须用"真实客户端 IP"而不是传输层对端 IP：
        # 挂了反向代理 / Cloudflare Tunnel 之后，对端永远是那个代理，
        # 用对端 IP 会让所有公网客户端共用同一个失败计数 ——
        # 任意一人打满阈值，全网设备一起被封 60 秒。
        # 是否采信代理头由 CIMS_TRUSTED_PROXIES 决定（见 app/core/client_ip.py）。
        ip = get_real_client_ip(request)
        if check_ip_blocked(ip):
            # Retry-After 让调用侧知道"等多久"而不是盲目重试 ——
            # 面板的班级下拉据此显示「服务忙，N 秒后重试」，而不是静默换成演示数据。
            return JSONResponse(
                status_code=429,
                content={"code": ERR_IP_BLOCKED, "msg": "异常封禁"},
                headers={"Retry-After": str(WINDOW_SEC)},
            )

        monitor_global_frequency()

        # 判断全局遭受狂暴并发时，拦截敏感交互点
        path = request.url.path.lower()
        if get_cc_state() and any(k in path for k in ["login", "register"]):
            if "authorization" not in request.headers:
                return JSONResponse(
                    status_code=503, content={"code": ERR_CC_ACTIVE, "msg": "通道受阻"}
                )

        resp = await call_next(request)
        if should_count_failure(path, resp.status_code):
            record_ip_failure(ip)

        return resp
