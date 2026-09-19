"""P2P 信令管理路由（可挂载到 CIMS 任意 app，如 management_app）。

端点：
  GET  /p2p-signal/health   探活（readiness 探针可聚合它）
  POST /p2p-signal/register 把设备令牌注入边车（body 带 admin_key，与边车一致）

挂载方式（示例，按需加到对应 *_app.py）：
    from app.ext.p2p_signal.router import router as p2p_router
    app.include_router(p2p_router)
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .client import register_device, signal_health

router = APIRouter(prefix="/p2p-signal", tags=["p2p-signal"])


@router.get("/health")
async def health():
    status, body = signal_health()
    return JSONResponse(
        {"signal_status": status, "detail": body},
        status_code=200 if status == 200 else 503,
    )


@router.post("/register")
async def register(request: Request):
    body = await request.json()
    device_uid = str(body.get("device_uid") or "").strip()
    secret = str(body.get("secret") or "").strip()
    if not device_uid or not secret:
        return JSONResponse({"code": 400, "msg": "device_uid & secret required"}, status_code=400)
    status, resp = register_device(device_uid, secret)
    return JSONResponse(resp, status_code=status or 502)
