"""客户端清单（Manifest）生成。

为终端设备提供获取其动态配置集（课表等）的接口。

资源解析优先级（v2 班级架构）：
1. 若设备绑定了班级（client_profiles.class_id 非空）→ 取该班级的资源集
   class_resource_sets（同一班级 N 台设备共享一份课表）
2. 否则回退设备自身 client_profiles 7 列（老行为，平滑迁移）
"""

import time
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import logging

from app.models.database import get_db, ClientProfile, ClassResourceSet
from app.api.schemas.client import ClientManifest
from app.core.client_ip import get_client_ip_from_request

router = APIRouter()
logger = logging.getLogger(__name__)


async def resolve_resource_names(db: AsyncSession, client_uid: str):
    """解析某设备最终生效的 7 类资源引用名。

    优先级：班级资源集（若 class_id 非空）→ 设备自身 profile 列。
    无论走哪条路径，缺失项都回退到默认名，保证 manifest 永远能产出完整 7 源。
    """
    stmt = select(ClientProfile).where(ClientProfile.client_id == client_uid)
    result = await db.execute(stmt)
    p = result.scalar_one_or_none()

    names = {
        "class_plan": "default_classplan",
        "time_layout": "default_timelayout",
        "subjects": "default",
        "default_settings": "default",
        "policy": "default",
        "components": "default",
        "credentials": "default",
    }

    if p is not None:
        # 设备自身列兜底（老行为）
        for k, col in (
            ("class_plan", "class_plan"),
            ("time_layout", "time_layout"),
            ("subjects", "subjects"),
            ("default_settings", "default_settings"),
            ("policy", "policy"),
            ("components", "components"),
            ("credentials", "credentials"),
        ):
            v = getattr(p, col, None)
            if v:
                names[k] = v

        # 若绑定了班级，用班级资源集覆盖（v2 主路径）
        if p.class_id:
            cstmt = select(ClassResourceSet).where(
                ClassResourceSet.resource_set_id == p.class_id
            )
            cres = (await db.execute(cstmt)).scalar_one_or_none()
            if cres is not None:
                for k, col in (
                    ("class_plan", "class_plan"),
                    ("time_layout", "time_layout"),
                    ("subjects", "subjects"),
                    ("default_settings", "default_settings"),
                    ("policy", "policy"),
                    ("components", "components"),
                    ("credentials", "credentials"),
                ):
                    v = getattr(cres, col, None)
                    if v:
                        names[k] = v

    return names


@router.get("/v1/client/{client_uid}/manifest", response_model=ClientManifest)
async def get_client_manifest(
    request: Request, client_uid: str, db: AsyncSession = Depends(get_db)
):
    """为请求的 UID 构建完整的 Manifest JSON 数据。"""
    slug = getattr(request.state, "tenant_slug", "Unknown")
    client_ip = get_client_ip_from_request(request)
    short_uid = (
        f"{client_uid[:8]}...{client_uid[-5:]}" if len(client_uid) > 13 else client_uid
    )
    logger.info(
        "[%s][%s][%s] Client API 详细信息: 请求 Manifest", client_ip, slug, short_uid
    )

    names = await resolve_resource_names(db, client_uid)
    cur = int(time.time())

    return _build_manifest(
        request,
        names["class_plan"],
        names["time_layout"],
        names["subjects"],
        names["default_settings"],
        names["policy"],
        names["components"],
        names["credentials"],
        cur,
    )


def _build_manifest(request: Request, cp, tl, sub, ds, pol, comp, cred, ver):
    """组装各资源源的 Manifest 结构体。"""

    def _src(rt, n):
        # 使用 request.url_for 生成完整的绝对 URL，并附带 name 参数
        # 目标端点为 resource.py 中的 get_client_resource
        url = request.url_for("get_client_resource", resource_type=rt)
        full_url = str(url.include_query_params(name=n))
        return {"Value": full_url, "Version": ver}

    return ClientManifest(
        ClassPlanSource=_src("ClassPlan", cp),
        TimeLayoutSource=_src("TimeLayout", tl),
        SubjectsSource=_src("Subjects", sub),
        DefaultSettingsSource=_src("DefaultSettings", ds),
        PolicySource=_src("Policy", pol),
        ComponentsSource=_src("Components", comp),
        CredentialSource=_src("Credentials", cred),
        ServerKind=1,
        OrganizationName="CIMS Server",
        CoreVersion="2.0.0.0",
    )
