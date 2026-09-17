"""教室端设备**运行时状态**上报（心跳）与回读端点。

用途：让面板显示**真实的**设备状态，而不是演示数据或"最后配置时间"这类间接信号。

    POST /v1/client/{client_id}/status   设备上报心跳（覆盖写，幂等）
    GET  /v1/client/{client_id}/status   回读本机状态 + 管理端权威班级归属

鉴权与 manifest / command_poll / messages 完全一致：TenantMiddleware 按
Host 头 `<slug>.<BASE_DOMAIN>` 识别租户，按 client_id 定向，无需会话凭证 ——
设备本来就持有该租户的上报身份。

关于「宽松解析」：请求体刻意不做严格 pydantic 校验（除 client_id 路径参数外全部
从原始 JSON 里取），因为上报 schema 会随插件版本演进。一旦因为多出一个字段就返回
422，心跳就会整条断掉、面板上设备集体变"离线"，而根因只是字段兼容问题 ——
这类故障排查成本极高，收益为零。故此处**只提取已知字段，其余原样存入 extra**。
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db, ClientProfile, ClientStatus

router = APIRouter()

# 心跳间隔的上游约定（秒）。面板以 REPORTED_FRESH_SECONDS 为在线判定阈值：
# 超过这个时长没有任何心跳，即认为设备离线。取 3 倍于插件默认上报间隔（20s），
# 容忍一次丢包/一次进程抖动而不误报离线。
FRESH_SECONDS = 90


def _now():
    return datetime.now(timezone.utc)


@router.post("/v1/client/{client_id}/status")
async def report_client_status(
    request: Request,
    client_id: str,
    body: dict = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
):
    """设备上报心跳。幂等覆盖写：同一 client_id 只保留最近一次状态。

    请求体（全部可选，缺失即留空，不报错）::

        {
          "host": "LAB-PC-001",           # 机器名
          "version": "1.2.0.0",          # 插件版本
          "class_id": "class03",         # 设备自报所属班级
          "active_class_group": "3班课表群",
          "modules": {"sync": true, "command_poll": true, ...},
          "plugins": [{"id":"...", "name":"...", "version":"...",
                       "enabled":true, "status":"Loaded", "isStelarith":false}],
          "extra": {"sync_ok": true, "messages": 3, ...}
        }
    """
    slug = getattr(request.state, "tenant_slug", "Unknown")
    body = body or {}

    # 客户端 IP：优先 X-Forwarded-For 首段（经反代时才是真实来源）
    ip = ""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        ip = fwd.split(",")[0].strip()
    if not ip and request.client:
        ip = request.client.host or ""

    import json as _json

    def _dump(value, fallback: str) -> str:
        """把上报里的结构化字段序列化；非法结构一律退化为 fallback，绝不让心跳 500。"""
        try:
            return _json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return fallback

    modules = body.get("modules")
    plugins = body.get("plugins")
    extra = body.get("extra")

    row = (
        await db.execute(select(ClientStatus).where(ClientStatus.client_id == client_id))
    ).scalar_one_or_none()
    if row is None:
        row = ClientStatus(client_id=client_id)
        db.add(row)

    row.host = str(body.get("host") or row.host or "")[:255]
    row.ip = str(ip or row.ip or "")[:64]
    row.version = str(body.get("version") or row.version or "")[:64]
    row.class_id = str(body.get("class_id") or "")[:128]
    row.active_class_group = str(body.get("active_class_group") or "")[:255]
    row.modules_json = _dump(modules, "{}")
    row.plugins_json = _dump(plugins, "[]")
    # extra 里除已知字段外的一切都留着：新增遥测项不必改后端
    if isinstance(extra, dict):
        row.extra_json = _dump(extra, "{}")
    else:
        row.extra_json = _dump(
            {k: v for k, v in body.items()
             if k not in ("host", "version", "class_id", "active_class_group",
                          "modules", "plugins", "extra")},
            "{}",
        )
    row.reported_at = _now()

    # 设备首次上报即建档（client_profiles）—— 让面板能把它指派进班级。
    #
    # 背景：`/device/assign`（设备划入班级）要求档案存在，而此前**没有任何生产
    # 代码路径**会创建档案（只有租户初始化脚本会建），于是「新装的教室机」会陷入
    # 一个极难自查的状态：面板上看得见它（心跳进了 client_status）、却绑不了班
    # （assign 返回 404「配置档案不存在」）—— 界面有这台机器，点了就是失败。
    #
    # 这里按最小侵入自愈：档案已存在则**一个字段都不动**（绝不覆盖已指派的班级），
    # 不存在才按默认资源名建一条空档（class_id 留空，等管理端指派）。
    prof = (
        await db.execute(
            select(ClientProfile).where(ClientProfile.client_id == client_id)
        )
    ).scalar_one_or_none()
    if prof is None:
        db.add(ClientProfile(client_id=client_id))

    await db.commit()
    return {"client_id": client_id, "reported": True, "server_time": row.reported_at.isoformat()}


@router.get("/v1/client/{client_id}/status")
async def read_client_status(
    request: Request,
    client_id: str,
    db: AsyncSession = Depends(get_db),
):
    """回读本机状态。

    设备端用它对账（"管理端认为我属于哪个班" vs "我以为我属于哪个班"），
    排查"本地切班成功但面板显示没变"这类跨端不一致。

    返回 200 且 `reported: false` 表示**没有心跳记录**（设备从未上报），
    而不是接口错误 —— 让设备端能区分"离线"与"路径写错"。
    """
    slug = getattr(request.state, "tenant_slug", "Unknown")

    row = (
        await db.execute(select(ClientStatus).where(ClientStatus.client_id == client_id))
    ).scalar_one_or_none()

    profile = (
        await db.execute(select(ClientProfile).where(ClientProfile.client_id == client_id))
    ).scalar_one_or_none()

    import json as _json

    def _load(text_value, fallback):
        try:
            return _json.loads(text_value) if text_value else fallback
        except (TypeError, ValueError):
            return fallback

    class_id = getattr(profile, "class_id", "") if profile else ""

    if row is None:
        return {
            "client_id": client_id,
            "reported": False,
            "class_id": class_id,
            "message": "该设备尚未上报过状态",
        }

    age = (_now() - row.reported_at).total_seconds() if row.reported_at else None
    return {
        "client_id": client_id,
        "reported": True,
        "online": age is not None and age <= FRESH_SECONDS,
        "age_seconds": int(age) if age is not None else None,
        "host": row.host,
        "ip": row.ip,
        "version": row.version,
        # 管理端指派是权威归属；设备自报值放在 self_reported_class_id 供交叉校验
        "class_id": class_id,
        "self_reported_class_id": row.class_id,
        "active_class_group": row.active_class_group,
        "modules": _load(row.modules_json, {}),
        "plugins": _load(row.plugins_json, []),
        "extra": _load(row.extra_json, {}),
        "reported_at": row.reported_at.isoformat() if row.reported_at else None,
    }
