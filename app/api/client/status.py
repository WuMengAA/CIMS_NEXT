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

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant.context import get_schema, safe_identifier
from app.models.database import get_db, ClientProfile, ClientStatus, ClientRecord
from app.models.class_model import (
    Class,
    REVIEW_APPROVED,
    REVIEW_PENDING,
    combine_device_label,
    normalize_os_family,
)

router = APIRouter()


async def resolve_device_uid(db: AsyncSession, token: str) -> str:
    """把「设备身份令牌」收敛成稳定的 uid（方案 B 的核心）。

    背景（票 #246「设备改名变成新建」）：

      设备上报 / 面板操作的 ``client_id`` 路径参数，历史上有两种含义混用——
        · 稳定的 uid（grpc ``ClientUid``），或
        · 可变的主机名 / 展示名（grpc ``ClientId``，老师改名后随之变化）。

      旧代码直接拿这个令牌当 ``ClientStatus`` / ``ClientProfile`` 的主键去查。
      一旦改名（主机名变），路径就变了 → 查不到旧行 → INSERT 一条新设备记录，
      旧的（还绑着班级）成了孤儿：绑定断裂、设备数虚增。

    本助手按 方案 B 收敛身份：无论传入的是 uid 还是主机名，都先查 ``ClientRecord``
    把它映射回稳定 ``uid``；映射不到（纯 HTTP 设备、尚未 grpc 注册）则原样当作 uid。
    这样「改名 → 路径变化」只触发展示名（host）的 UPDATE，绝不再 INSERT 新设备。

    返回：稳定 uid。上层用它作为 ``ClientStatus`` / ``ClientProfile`` 的键。
    """
    rec = (
        await db.execute(select(ClientRecord).where(ClientRecord.uid == token))
    ).scalar_one_or_none()
    if rec is not None:
        return rec.uid
    rec = (
        await db.execute(select(ClientRecord).where(ClientRecord.client_id == token))
    ).scalar_one_or_none()
    if rec is not None:
        return rec.uid
    return token


async def _get_or_create_by_uid(
    db: AsyncSession, model, uid: str, token: str
):
    """按稳定 uid 取/建设备行，并就地把旧行的「主机名主键」迁成 uid。

    兼容历史数据：旧行可能以原始令牌（主机名）作主键。命中时若主键仍是令牌，
    则改挂到 uid，避免改名后出现「旧主机名行 + 新 uid 行」两条记录。
    """
    row = (
        await db.execute(
            select(model).where(model.client_id.in_([uid, token]))
        )
    ).scalar_one_or_none()
    if row is None:
        row = model(client_id=uid)
        db.add(row)
    elif row.client_id != uid:
        row.client_id = uid
    return row


# 租户业务表（互动广播回执）：设备端回复与管理端拉取共用，按租户 schema 隔离。
_NOTICE_REPLY_DDL = (
    "id SERIAL PRIMARY KEY,"
    " client_id TEXT NOT NULL,"
    " notice_id TEXT NOT NULL DEFAULT '',"
    " text TEXT NOT NULL,"
    " created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
)

# 设备执行回执表（截图/锁屏/远控等动作执行结果的上报落点）。
# 与 notice_replies 同模式：设备端按 Host 头识别租户，落租户业务表，
# 操控端经管理端接口按 class/client 拉取。
_COMPLETION_DDL = (
    "id SERIAL PRIMARY KEY,"
    " client_id TEXT NOT NULL,"
    " action TEXT NOT NULL DEFAULT '',"
    " ok BOOLEAN NOT NULL DEFAULT TRUE,"
    " detail TEXT NOT NULL DEFAULT '',"
    " ts BIGINT NOT NULL DEFAULT 0,"
    " created_at TIMESTAMPTZ NOT NULL DEFAULT now()"
)


async def _ensure_completion_table(db: AsyncSession) -> str:
    """惰性建表：返回当前租户 schema 名（已带双引号，可直接拼 SQL）。"""
    schema = safe_identifier(get_schema())
    await db.execute(
        text(f'CREATE TABLE IF NOT EXISTS "{schema}".command_completions ({_COMPLETION_DDL})')
    )
    return schema


async def _ensure_notice_reply_table(db: AsyncSession) -> str:
    """惰性建表：返回当前租户 schema 名（已带双引号，可直接拼 SQL）。"""
    schema = safe_identifier(get_schema())
    await db.execute(
        text(f'CREATE TABLE IF NOT EXISTS "{schema}".notice_replies ({_NOTICE_REPLY_DDL})')
    )
    return schema

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

    # 方案 B：把传入令牌（可能是 uid 或主机名）收敛成稳定 uid，作为设备唯一身份键。
    # 改名只改 host/展示名，绝不新建设备行。
    uid = await resolve_device_uid(db, client_id)

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

    row = await _get_or_create_by_uid(db, ClientStatus, uid, client_id)

    row.host = str(body.get("host") or row.host or "")[:255]
    row.ip = str(ip or row.ip or "")[:64]
    row.version = str(body.get("version") or row.version or "")[:64]
    # 设备运行系统（Windows/macOS/Linux…）：设备级属性，用于拼接班级组合显示名
    # 「2025届3班_Windows」。
    #
    # ⚠️ 三种取法都要试，缺一不可：
    #   1. 顶层 `os_name` —— 目标契约（新客户端应报这个）；
    #   2. 顶层 `os`      —— 部分客户端的简写；
    #   3. `extra.os`     —— **已部署插件实际用的位置**（StelarithStatusReporter
    #      把 `os` 放进了 `extra` 字典，而 extra 是整体原样落库的，于是后端
    #      一直读不到，os_name 恒为空、组合显示名永远只有「2025届3班」）。
    #      在这里兜住它，可以让**存量插件不改包**就立刻生效。
    # 取到后统一归一成家族名，避免把「Microsoft Windows NT 10.0.26200.0」拼进去。
    _extra_dict = extra if isinstance(extra, dict) else {}
    _os_raw = (
        body.get("os_name")
        or body.get("os")
        or _extra_dict.get("os_name")
        or _extra_dict.get("os")
        or ""
    )
    row.os_name = (normalize_os_family(str(_os_raw)) or row.os_name or "")[:32]
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
             if k not in ("host", "version", "os_name", "os", "class_id",
                          "active_class_group", "modules", "plugins", "extra")},
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
    prof = await _get_or_create_by_uid(db, ClientProfile, uid, client_id)

    await db.commit()
    return {"client_id": uid, "reported": True, "server_time": row.reported_at.isoformat()}


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

    # 方案 B：传入令牌收敛成稳定 uid 再查，改名（路径变）也能命中同一条记录。
    uid = await resolve_device_uid(db, client_id)

    row = (
        await db.execute(
            select(ClientStatus).where(ClientStatus.client_id.in_([uid, client_id]))
        )
    ).scalar_one_or_none()

    profile = (
        await db.execute(
            select(ClientProfile).where(ClientProfile.client_id.in_([uid, client_id]))
        )
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
            # bound=false 让插件端立刻知道"还没绑定班级"，从而弹出 OOBE 引导
            # （而不是默默用本地默认档案的 1 班，造成"自动归到一班"的假象）。
            "bound": bool(class_id),
            "message": "该设备尚未上报过状态",
        }

    age = (_now() - row.reported_at).total_seconds() if row.reported_at else None
    # 可选班级清单：未绑定设备经此知道"能绑哪些班"，供插件 OOBE / 面板下拉使用。
    # 只在未绑定时查询，已绑定则不必再拉（省一次 SQL）。
    suggest = []
    if not class_id:
        try:
            _classes = (await db.execute(
                select(Class).order_by(Class.sort_order, Class.code, Class.name)
            )).scalars().all()
            suggest = [
                {
                    "class_id": c.id,
                    "name": c.name,
                    "code": c.code or c.name,
                    "review_status": c.review_status,
                    # 只有通过审核的班级可选（与 /class/device/assign 的门控一致），
                    # 前端据此把待审班级置灰，避免「选了却绑不上」。
                    "selectable": (c.review_status or "pending") == "approved",
                }
                for c in _classes
            ]
        except Exception:
            suggest = []

    # 班级组合显示名（编号_设备运行系统），让设备端一处拿到可直接展示的标签
    class_code = ""
    if class_id:
        _c = (await db.execute(select(Class).where(Class.id == class_id))).scalar_one_or_none()
        class_code = (_c.code or _c.name) if _c else ""
    return {
        "client_id": client_id,
        "reported": True,
        "online": age is not None and age <= FRESH_SECONDS,
        "age_seconds": int(age) if age is not None else None,
        "host": row.host,
        "ip": row.ip,
        "version": row.version,
        "os_name": row.os_name,
        # 管理端指派是权威归属；设备自报值放在 self_reported_class_id 供交叉校验
        "class_id": class_id,
        "class_code": class_code,
        # 组合显示名，如「2025届3班_Windows」——设备端可直接展示，无需自己拼
        "class_display": combine_device_label(class_code, row.os_name),
        "self_reported_class_id": row.class_id,
        # 是否已完成"班级绑定"：未绑定设备的插件端据此弹 OOBE 引导。
        "bound": bool(class_id),
        # 未绑定设备的可选班级清单（已绑定则为空数组）。
        "suggest": suggest,
        "active_class_group": row.active_class_group,
        "modules": _load(row.modules_json, {}),
        "plugins": _load(row.plugins_json, []),
        "extra": _load(row.extra_json, {}),
        "reported_at": row.reported_at.isoformat() if row.reported_at else None,
    }


@router.post("/v1/client/{client_id}/register")
async def register_device_class(
    request: Request,
    client_id: str,
    body: dict = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
):
    """设备自助注册班级（OOBE 收口）。

    与心跳、消息同一条信任链：TenantMiddleware 按 Host 头识别租户、按 client_id
    定向，无需会话凭证 —— 设备本来就持有该租户的上报身份。这条链路刻意与管理端
    ``/class/device/assign``（Bearer 鉴权、带审核人/属主门控）区分开：设备没有管理
    会话，只能自助把**自己**绑到「已通过审核」的班级，不能动别人的班。

    门控（与管理端 assign 的内容审核保持一致）：
      · 班级不存在 → 404（跨租户的 class_id 在此查不到，天然防越租户绑定）；
      · 班级未通过审核 → 409（杜绝未审内容经自注册落到教室大屏）；
      · 设备已绑定到**其它**班 → 409（必须先 unregister / 重新注册才能切换，
        与「一班一号、不静默抢占」硬约束同语义）；同班则幂等成功。
    """
    class_id = (body or {}).get("class_id") or ""
    if not class_id:
        raise HTTPException(400, "缺少 class_id")

    cls = (
        await db.execute(select(Class).where(Class.id == class_id))
    ).scalar_one_or_none()
    if not cls:
        raise HTTPException(404, f"班级 {class_id} 不存在")
    if (cls.review_status or REVIEW_PENDING) != REVIEW_APPROVED:
        raise HTTPException(
            409,
            f"班级「{cls.name or cls.id}」尚未通过审核，暂不能绑定。",
        )

    # 方案 B：令牌收敛为稳定 uid 再解析设备档案。
    uid = await resolve_device_uid(db, client_id)
    prof = (
        await db.execute(
            select(ClientProfile).where(ClientProfile.client_id.in_([uid, client_id]))
        )
    ).scalar_one_or_none()
    if prof is None:
        prof = ClientProfile(client_id=uid)
        db.add(prof)

    prev = prof.class_id or ""
    if prev and prev != class_id:
        raise HTTPException(
            409,
            f"本设备已属于「{prev}」，请先解除绑定（重新注册）后再切换到本班。",
        )

    prof.class_id = class_id
    await db.commit()

    class_code = cls.code or cls.name
    os_name = (await db.execute(
        select(ClientStatus.os_name).where(ClientStatus.client_id == client_id)
    )).scalar_one_or_none() or ""
    return {
        "status": "success",
        "client_id": client_id,
        "class_id": class_id,
        "class_code": class_code,
        "class_display": combine_device_label(class_code, os_name),
        "previous_class_id": prev,
        "registered": True,
        "bound": True,
    }


@router.post("/v1/client/{client_id}/unregister")
async def unregister_device_class(
    request: Request,
    client_id: str,
    db: AsyncSession = Depends(get_db),
):
    """设备自助解除班级绑定（重新注册入口）。

    与 register 同一信任链（租户 + client_id 定向）。解绑后下次心跳回读
    bound=false，插件端 OOBE 引导会重新弹出，引导教师重新选班。
    """
    # 方案 B：令牌收敛为稳定 uid 再解析设备档案。
    uid = await resolve_device_uid(db, client_id)
    prof = (
        await db.execute(
            select(ClientProfile).where(ClientProfile.client_id.in_([uid, client_id]))
        )
    ).scalar_one_or_none()
    if prof is None:
        raise HTTPException(404, f"设备 {client_id} 的配置档案不存在")
    prev = prof.class_id or ""
    prof.class_id = ""
    await db.commit()
    return {
        "status": "success",
        "client_id": client_id,
        "class_id": "",
        "previous_class_id": prev,
        "registered": False,
        "bound": False,
    }


@router.post("/v1/client/{client_id}/notice-reply")
async def post_notice_reply(
    request: Request,
    client_id: str,
    body: dict = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
):
    """教室端经「互动弹窗（确认/回复）」回复后回传的回执。

    与 register/unregister 同一信任链：租户经 Host 头识别、client_id 定向，无需会话凭证。
    回执落 tenant 业务表 notice_replies，供管理端经 GET /class/notice-replies 拉取。

    门控：空回复直接 400（防止轮询/误触把空行打进回执箱）；超长截断到 2000 字。
    """
    raw = body or {}
    reply_text = (raw.get("text") or "").strip()
    if not reply_text:
        raise HTTPException(400, "回复内容为空")
    if len(reply_text) > 2000:
        reply_text = reply_text[:2000]
    notice_id = (raw.get("notice_id") or "").strip()[:200]

    schema = await _ensure_notice_reply_table(db)
    await db.execute(
        text(f'INSERT INTO "{schema}".notice_replies (client_id, notice_id, text) '
             f"VALUES (:cid, :nid, :txt)"),
        {"cid": client_id, "nid": notice_id, "txt": reply_text},
    )
    await db.commit()
    return {"status": "success", "client_id": client_id, "notice_id": notice_id}


@router.post("/v1/client/{client_id}/completions")
async def post_command_completion(
    request: Request,
    client_id: str,
    body: dict = Body(default_factory=dict),
    db: AsyncSession = Depends(get_db),
):
    """教室端执行一条指令（截图/锁屏/远控/关机等）后的**执行回执**。

    与 notice-reply / status 同一信任链：租户经 Host 头识别、client_id 定向，无需会话凭证。
    回执落租户业务表 command_completions，供操控端/面板经管理端接口按 class/client 拉取，
    从而区分「指令已下发」与「设备已执行（成功/失败+原因）」—— 这是
    「被控端完成操作后给操控端上报响应」的服务端落点。

    门控：action 为空直接 400（防轮询误触）；detail 截断到 500 字。
    """
    raw = body or {}
    action = (raw.get("action") or "").strip()
    if not action:
        raise HTTPException(400, "缺少 action")
    ok = bool(raw.get("ok", True))
    detail = (raw.get("detail") or "").strip()[:500]
    ts = int(raw.get("ts") or 0)

    schema = await _ensure_completion_table(db)
    await db.execute(
        text(f'INSERT INTO "{schema}".command_completions '
             f"(client_id, action, ok, detail, ts) "
             f"VALUES (:cid, :act, :ok, :det, :ts)"),
        {"cid": client_id, "act": action, "ok": ok, "det": detail, "ts": ts},
    )
    await db.commit()
    return {"status": "success", "client_id": client_id, "action": action, "ok": ok}


@router.get("/v1/client/{client_id}/p2p")
async def client_p2p_credentials(request: Request, client_id: str):
    """下发本机 WebRTC 远控所需的信令凭据（**被控端**用）。

    与面板走 website `/api/console/ext/p2p-signal` 是**同一算法同一密钥**
    （``HMAC-SHA256(HMAC_KEY, "p2p:<uid>")`` → hex），因此控制器与被控端无需共享
    存储即天然拿到同一 secret。

    - 设备身份即租户内的 ``client_id``（与 manifest / status 同一身份，无需额外凭证）；
    - 顺手把令牌注入信令边车（幂等；边车未起则忽略，**不阻塞**设备取凭据）。
    """
    from fastapi.responses import JSONResponse

    try:
        from app.ext.p2p_signal import p2p_credentials, register_device
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"p2p 模块不可用：{exc}"}, status_code=503)
    try:
        cred = p2p_credentials(client_id)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    try:
        register_device(client_id, cred["secret"])
    except Exception:  # noqa: BLE001
        pass  # 边车没起不该妨碍设备取凭据（面板会按 signalUrl 自行重试）
    return {"client_id": client_id, **cred}
