"""P2P 信令边车客户端（CIMS 侧接缝实现）。

零新依赖：标准库 urllib 调边车 REST，subprocess 拉起 node 边车。
边车本身是独立 Node 服务（见 _eval-billd-desk/signaling/server.mjs），
这里只负责「拉起 + 探活 + 注入按设备令牌」，不参与 WebRTC 编解码。
"""

import hashlib
import hmac
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
# 配置读取：**真实环境变量 → .env 文件** 两级
#
# 为什么不能只读 os.environ：CIMS 的配置走 pydantic-settings（app/core/config.py），
# 它只把 .env 的值填进 Settings 对象，**不会写进 os.environ**。若此处直接
# os.environ.get(...)，生产上会恒拿到空串 —— 表现为「令牌派生报未配置密钥」而
# 本地用 shell 导出变量测试时却一切正常（极其难排查）。
# 因此这里显式回退读 .env，且**只用局部字典**、不调用 load_dotenv() 污染全局
# os.environ（避免改变其他模块的既有行为）。
# --------------------------------------------------------------------------- #
try:
    from dotenv import dotenv_values as _dotenv_values

    _ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
    _DOTENV = dict(_dotenv_values(_ENV_FILE)) if _ENV_FILE.exists() else {}
except Exception:  # noqa: BLE001
    _DOTENV = {}


def _cfg(key: str) -> str:
    """取配置：真实环境变量优先，缺失则回退 .env 文件；统一 strip。"""
    val = os.environ.get(key)
    if val is None or not str(val).strip():
        val = _DOTENV.get(key)
    return "" if val is None else str(val).strip()


SIGNAL_URL = (_cfg("STELARITH_P2P_URL") or "http://127.0.0.1:18110").rstrip("/")
ADMIN_KEY = _cfg("STELARITH_P2P_ADMIN_KEY")
# 边车守护脚本（探活 + 拉起），例如：
#   "D:/Stelarith/_eval-billd-desk/signaling/guard-signaling.ps1"
# 脚本内部自带探活：边车已存活则直接退出，未存活才前台拉起 node 边车。
SIDECAR_GUARD = _cfg("STELARITH_P2P_SIDECAR_GUARD")
# 公网信令地址（供教室端/面板连接；未配置时回退内部地址 —— 仅同机可用）
PUBLIC_URL = _cfg("STELARITH_P2P_PUBLIC_URL")
# 设备令牌派生密钥：**必须与 website 侧 STELARITH_P2P_HMAC_KEY 完全一致**，
# 否则控制器（面板）与被控端（教室机）算出的 secret 不同 → join 被边车拒（401）。
HMAC_KEY = _cfg("STELARITH_P2P_HMAC_KEY") or _cfg("CIMS_ADMIN_SECRET")


def _post(path: str, payload: dict):
    req = urllib.request.Request(
        SIGNAL_URL + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.URLError as e:
        return getattr(e, "code", 0), {"error": str(e)}


def register_device(device_uid: str, secret: str):
    """把某设备的媒体令牌注入边车（受边车 ADMIN_KEY 保护）。"""
    return _post("/register-device", {"admin_key": ADMIN_KEY, "deviceUid": device_uid, "secret": secret})


def signal_health():
    try:
        with urllib.request.urlopen(SIGNAL_URL + "/health", timeout=3) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except Exception as e:  # noqa: BLE001
        return 0, {"error": str(e)}


def ensure_sidecar() -> bool:
    """尽力拉起边车（被控失败不抛，仅返回 False），避免阻断 CIMS 启动。

    调用方应在 CIMS 启动期调用一次；真正的常驻保活交给 Windows 计划任务
    （每分钟跑同一个 guard 脚本）。

    注意：Windows 上 ``shell=False`` 时**不能**把「node xxx.mjs」这种整串交给
    ``Popen`` —— 会被当成单个可执行文件名而直接失败。故这里统一走 PowerShell
    调用 guard 脚本（脚本自己拼命令行），并以 DETACHED_PROCESS 启动，
    使其脱离 CIMS 进程树独立存活。
    """
    if signal_health()[0] == 200:
        return True
    if not SIDECAR_GUARD:
        return False
    try:
        subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                SIDECAR_GUARD,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# 设备令牌派生与凭据下发
#   与 website 侧 src/routes/api/console/ext/p2p-signal/+server.ts 的 mintSecret
#   **同算法同密钥**：HMAC-SHA256(key, "p2p:<uid>") → hex（64 字符）。
#   确定性派生 ⇒ 控制器与被控端无需共享存储就能互认；换密钥失效所有房间。
# --------------------------------------------------------------------------- #


def signal_public_url() -> str:
    """设备/面板实际使用的信令地址：优先公网（STELARITH_P2P_PUBLIC_URL），否则内部地址。"""
    return (PUBLIC_URL or SIGNAL_URL).rstrip("/")


def mint_secret(device_uid: str) -> str:
    """派生某设备的信令令牌（与 website 侧同算法）。未配置密钥时抛 RuntimeError。"""
    if not HMAC_KEY:
        raise RuntimeError("STELARITH_P2P_HMAC_KEY 未配置")
    return hmac.new(
        HMAC_KEY.encode("utf-8"),
        f"p2p:{device_uid}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def p2p_credentials(device_uid: str) -> dict:
    """设备/面板连接信令边车所需的凭据（roomId 约定 `room-<uid>`）。"""
    return {
        "signalUrl": signal_public_url(),
        "roomId": f"room-{device_uid}",
        "secret": mint_secret(device_uid),
        # 确定性派生 ⇒ 长期有效（教室机重连即用同一令牌），无过期语义
        "expiresIn": 0,
    }
