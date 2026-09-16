"""客户端真实 IP 提取工具。

统一处理反向代理 / 隧道入口场景下的 IP 解析，
同时支持 FastAPI（HTTP）和 gRPC 两种传输方式。

安全前提（重要）：
    只有在请求的**对端**落在 `CIMS_TRUSTED_PROXIES` 指定的网段内时，才会采信
    `CF-Connecting-IP` / `X-Forwarded-For` / `X-Real-IP` 这类可由客户端伪造的头。
    否则一律回退到对端 IP。

    这一条不能省：若无条件采信 XFF，攻击者每次换一个伪造的 XFF 就能让
    CCProtectMiddleware 的单 IP 限流彻底失效（每个假 IP 都只有 1 次失败记录）。

代理链路对应关系：
  - Cloudflare Tunnel：对端是 127.0.0.1，真实 IP 在 `CF-Connecting-IP`
  - Nginx 反向代理： 对端是 Nginx 所在 IP，真实 IP 在 `X-Forwarded-For` / `X-Real-IP`
  - gRPC：            Nginx 以 metadata 形式传递同名头
"""

from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING, Iterable

from app.core.config import TRUSTED_PROXIES

if TYPE_CHECKING:
    from starlette.requests import Request
    import grpc


logger = logging.getLogger(__name__)

# 解析结果缓存：TRUSTED_PROXIES 是启动期常量，没必要每个请求都重新解析
_trusted_cache: dict[str, list] = {}


def _parse_forwarded_for(value: str) -> str:
    """从 X-Forwarded-For 头中提取第一个（最原始的）客户端 IP。

    X-Forwarded-For 格式: client, proxy1, proxy2
    """
    if not value:
        return ""
    return value.split(",")[0].strip()


def parse_trusted_proxies(raw: str) -> list:
    """把逗号分隔的 IP / CIDR 字符串解析成 ip_network 列表。

    解析失败的条目会被跳过并记一条 warning —— 不因为一个笔误让服务起不来，
    但也绝不静默放过（否则可能误以为限流生效）。
    """
    raw = (raw or "").strip()
    if raw in _trusted_cache:
        return _trusted_cache[raw]

    nets: list = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("CIMS_TRUSTED_PROXIES 中的条目无法解析，已忽略: %r", item)

    _trusted_cache[raw] = nets
    return nets


def is_trusted_proxy(peer_ip: str, trusted_raw: str | None = None) -> bool:
    """判断请求对端是否是可信任的代理入口。"""
    if not peer_ip:
        return False
    raw = TRUSTED_PROXIES if trusted_raw is None else trusted_raw
    nets = parse_trusted_proxies(raw)
    if not nets:
        return False
    try:
        addr = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    return any(addr in net for net in nets)


def _pick_from_headers(get_header, peer_ip: str) -> str:
    """按可信度从高到低挑一个头里的客户端 IP。"""
    # Cloudflare 设置的权威头：不可被客户端覆盖，优先采信
    cf = (get_header("cf-connecting-ip") or "").strip()
    if cf:
        return cf
    # 标准多级代理头
    forwarded = _parse_forwarded_for(get_header("x-forwarded-for") or "")
    if forwarded:
        return forwarded
    # Nginx 常用单值头
    real_ip = (get_header("x-real-ip") or "").strip()
    if real_ip:
        return real_ip
    return peer_ip


def get_real_client_ip(request: "Request") -> str:
    """取客户端真实 IP（HTTP）。对端不可信时直接返回对端 IP。"""
    peer_ip = request.client.host if request.client else ""
    if not is_trusted_proxy(peer_ip):
        return peer_ip
    return _pick_from_headers(request.headers.get, peer_ip)


def get_peer_ip(request: "Request") -> str:
    """取传输层对端 IP（不做任何头解析）。

    用途：判定"这个请求到底是从本机代理进来的，还是从公网直连进来的"。
    """
    return request.client.host if request.client else ""


def get_client_ip_from_request(request: "Request") -> str:
    """从 FastAPI/Starlette Request 中提取客户端真实 IP。

    这是给业务代码用的统一入口。是否采信代理头由 CIMS_TRUSTED_PROXIES 决定，
    详见模块文档。
    """
    return get_real_client_ip(request)


def get_client_ip_from_grpc(context: "grpc.aio.ServicerContext") -> str:
    """从 gRPC 上下文中提取客户端真实 IP。

    Nginx 在 gRPC 反代时会将 x-forwarded-for / x-real-ip
    作为 metadata 传递给后端。同样受 CIMS_TRUSTED_PROXIES 约束 ——
    不可信的对端只认 peer 地址。
    """
    metadata = dict(context.invocation_metadata())
    peer = context.peer() or ""

    peer_ip = ""
    if peer and ":" in peer:
        parts = peer.split(":")
        if len(parts) >= 2:
            peer_ip = parts[1]

    if not is_trusted_proxy(peer_ip):
        return peer_ip

    def _get(name: str) -> str:
        return metadata.get(name, "")

    return _pick_from_headers(_get, peer_ip)


def describe_trust_config() -> str:
    """给启动日志用的一句话摘要，便于确认限流到底认不认得真实 IP。"""
    nets: Iterable = parse_trusted_proxies(TRUSTED_PROXIES)
    rendered = ", ".join(str(n) for n in nets)
    return rendered if rendered else "（未配置：只认直连对端 IP）"
