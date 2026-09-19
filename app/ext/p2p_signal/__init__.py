"""P2P 信令边车接缝包（方案2 · 接入真实 CIMS）。

暴露给 CIMS 启动期与路由层使用的薄封装：
  - register_device(device_uid, secret)  向边车注入按设备令牌
  - signal_health()                      探活（readiness 用）
  - ensure_sidecar()                     尽力拉起边车（失败不阻断 CIMS）

零新第三方依赖：标准库 urllib + subprocess。
"""

from .client import (
    ensure_sidecar,
    mint_secret,
    p2p_credentials,
    register_device,
    signal_health,
    signal_public_url,
)

__all__ = [
    "register_device",
    "signal_health",
    "ensure_sidecar",
    "mint_secret",
    "p2p_credentials",
    "signal_public_url",
]
