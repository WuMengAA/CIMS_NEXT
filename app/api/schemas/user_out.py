"""用户信息响应、登录请求与管理员更新模型。"""

from typing import Optional

from pydantic import BaseModel, Field


class UserLoginRequest(BaseModel):
    """用户登录请求体。

    `email` 用宽松 `str` 而非 `EmailStr`：本系统允许 `.local` 等内部域名邮箱
    （如 `techrep01@stelarith.local`），而 pydantic 的 EmailStr 会以
    "special-use domain" 为由直接拒绝 `.local`，导致 422「参数失范」。
    这与 `/user/sync-create`（同样是 `str`）保持一致——账号权威在 website 侧，
    CIMS 只做镜像，不应在此处引入比同步端更严的校验。
    """

    email: str = Field(..., min_length=3, max_length=254, description="用户邮箱")
    password: str = Field(..., max_length=128)


class UserOut(BaseModel):
    """用户信息响应模型。"""

    id: str
    username: str
    email: str
    display_name: str
    role_code: str
    is_active: bool
    can_create_account: bool = False
    created_at: str


class UserUpdateRequest(BaseModel):
    """用户信息更新请求体（管理员用）。"""

    display_name: Optional[str] = Field(None, max_length=128)
    role_code: Optional[str] = Field(None, max_length=32)
    is_active: Optional[bool] = None
    can_create_account: Optional[bool] = None
