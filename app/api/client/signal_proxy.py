"""P2P 信令边车的**公网入口透传**（挂在 client app / 8096）。

## 为什么需要这层
信令边车（socket.io，本机 :18110）只监听回环地址，而 Cloudflare 隧道的远端
ingress 只有两条：``panel.<zone>`` → 8090 与 ``*.<zone>`` → 8096。**新增独立子域
必须用 Cloudflare API token 改远端 ingress**（本地 config.yml 在 token 模式下不生效）。
复用已经公网可达的 8096（``<slug>.<zone>``）把 ``/socket.io/*`` 透传给边车，
即可**零新增 DNS、零改 ingress** 地让面板与教室机从公网接入信令。

## 边界（安全）
- 只透传 ``/socket.io/*``；**绝不放行**边车的管理接口 ``/register-device``。
  设备令牌注入一律走本机内部路径（``app.ext.p2p_signal.register_device``）。
- 边车自身对 join 做 fail-closed 鉴权：未注册设备一律 401 + 断开，因此
  即便有人猜到子域也无法加入房间（拿不到 HMAC 派生令牌）。
- WebSocket 连接**不经过** ``BaseHTTPMiddleware``（TenantMiddleware 只处理 http
  scope），故此处显式复核 Host 是否属于已注册租户，与 HTTP 路由保持一致；
  校验不通直接 1008 关闭，不进入透传。

## 依赖
``websockets``（随 ``uvicorn[standard]`` 一并安装，实测 16.0）；HTTP 侧取
socket.io 客户端脚本用标准库 urllib，不引 httpx。
"""

import asyncio
import logging
import urllib.error
import urllib.request

from fastapi import APIRouter, Response, WebSocket

from app.core.tenant.host_parser import extract_slug_from_host
from app.core.tenant.resolver import resolve_account
from app.ext.p2p_signal.client import SIGNAL_URL
from app.models.engine import AsyncSessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["P2P"])

# http(s):// → ws(s)://（边车走本机明文，公网 TLS 由 Cloudflare 终止）
_WS_BASE = SIGNAL_URL.replace("https://", "wss://").replace("http://", "ws://")

_JS_PATH = "/socket.io/socket.io.js"


def _fetch_client_js() -> bytes | None:
    """同步取 socket.io 客户端脚本（放到线程里跑，避免阻塞事件循环）。"""
    try:
        with urllib.request.urlopen(SIGNAL_URL + _JS_PATH, timeout=5) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None


@router.get(_JS_PATH)
async def socketio_client_js():
    """把 socket.io 客户端脚本从边车取回（面板通过 CDN 式引用，无需本地打包）。"""
    data = await asyncio.to_thread(_fetch_client_js)
    if data is None:
        # 边车没起：返回一段可执行的空桩，避免浏览器 404 触发 CCProtect 计数
        return Response(
            content=b"/* stelarith signaling sidecar unavailable */",
            media_type="application/javascript",
            status_code=200,
        )
    return Response(
        content=data,
        media_type="application/javascript",
        headers={"cache-control": "public, max-age=3600"},
    )


async def _host_is_known_tenant(websocket: WebSocket) -> bool:
    """复核 Host 属于已注册租户（WS 不经过 TenantMiddleware，需自查）。"""
    slug = extract_slug_from_host(websocket.headers.get("host", ""))
    if not slug:
        return False
    try:
        async with AsyncSessionLocal() as db:
            return bool(await resolve_account(slug, db))
    except Exception as exc:  # noqa: BLE001
        logger.warning("socket.io 透传：租户校验异常 slug=%s err=%s", slug, exc)
        return False


@router.websocket("/socket.io/")
@router.websocket("/socket.io")
async def socketio_ws(websocket: WebSocket):
    """WebRTC 信令（socket.io over websocket）双向原样透传到本机边车。

    socket.io 客户端以 ``transports:['websocket']`` 直连时只发 WebSocket 帧，
    因此这里不需要理解 Engine.IO 协议，**逐帧双向搬运**即可（含握手帧）。
    """
    if not await _host_is_known_tenant(websocket):
        await websocket.close(code=1008)  # policy violation：未知租户
        return

    try:
        import websockets  # 延迟导入：缺库时只影响本路由，不影响整个 app 启动
    except Exception as exc:  # noqa: BLE001
        logger.error("socket.io 透传不可用：websockets 未安装（%s）", exc)
        await websocket.close(code=1011)
        return

    await websocket.accept()

    qs = websocket.scope.get("query_string", b"").decode()
    upstream_url = _WS_BASE + "/socket.io/" + (f"?{qs}" if qs else "")

    try:
        async with websockets.connect(
            upstream_url, max_size=None, ping_interval=None, open_timeout=5
        ) as upstream:

            async def client_to_upstream() -> None:
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            return
                        if msg.get("text") is not None:
                            await upstream.send(msg["text"])
                        elif msg.get("bytes") is not None:
                            await upstream.send(msg["bytes"])
                except Exception:  # noqa: BLE001
                    return

            async def upstream_to_client() -> None:
                try:
                    async for chunk in upstream:
                        if isinstance(chunk, (bytes, bytearray)):
                            await websocket.send_bytes(bytes(chunk))
                        else:
                            await websocket.send_text(chunk)
                except Exception:  # noqa: BLE001
                    return

            tasks = {
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            }
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception as exc:  # noqa: BLE001
        # 边车未起 / 连接被拒：安静收尾（面板会自行重试），不当成服务端故障
        logger.info("socket.io 透传结束：%s", exc)
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
