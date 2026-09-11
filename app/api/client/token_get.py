"""令牌下发的资源获取端点。

根据资源令牌加载对应的配置数据并返回 JSON 内容。
"""

import json
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from app.core.tenant.context import set_search_path
from app.core.client_ip import get_client_ip_from_request
from app.models.database import AsyncSessionLocal
from app.services.resource_token import resolve_token
from app.api.command.model_map import MODEL_MAP
from app.grpc.session.online_status import get_tenant_online_ips

router = APIRouter()


@router.get("/get")
async def get_resource_by_token(token: str, request: Request):
    """通过令牌获取配置内容，附带 IP 鉴权。"""
    result = await resolve_token(token)
    if result is None:
        raise HTTPException(status_code=403, detail="Invalid or expired token")

    tenant_id, resource_type, name, token_ip, schema = result
    model = MODEL_MAP.get(resource_type)
    if model is None:  # pragma: no cover
        raise HTTPException(status_code=400, detail="Invalid resource type")

    # IP 鉴权
    # 设计意图：token 绑定请求方 IP，防令牌被盗用后跨设备拉取。
    # 但以下两种合法场景必须放行，否则管理面板预览 / 冷启动无设备在线时会恒返回 {}：
    #  ① 请求来自本机回环地址（127.0.0.1/::1）——网站服务端代理走的就是这条管理通道；
    #  ② 租户当前没有任何在线设备（online_ips 为空）——无法校验时不做无意义拦截。
    # 真实教室端设备在线时，online_ips 非空且设备 IP 非回环，校验照常生效。
    client_ip = get_client_ip_from_request(request)
    if token_ip and client_ip not in ("127.0.0.1", "::1"):
        online_ips = await get_tenant_online_ips(tenant_id)
        if online_ips and client_ip not in online_ips:
            return {}

    # 按令牌记录的租户 Schema 设置 search_path（/get 在 TenantMiddleware
    # 白名单内、无 ContextVar 租户上下文，必须显式指定，否则读 public 与
    # 写入 schema 错位，返回空对象）。
    async with AsyncSessionLocal() as db:
        await set_search_path(db, schema)
        row = (
            await db.execute(select(model).where(model.name == name))
        ).scalar_one_or_none()

    if row is None:  # pragma: no cover
        raise HTTPException(status_code=404, detail="Resource not found")

    try:
        return json.loads(row.content)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=500, detail="Corrupted resource")
