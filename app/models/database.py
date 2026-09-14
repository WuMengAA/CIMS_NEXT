"""数据库模型 Facade 索引。

该文件作为一个向后兼容层，将所有分散的模型及引擎实例归集至此。
新开发的功能建议直接从 app.models 子包导入各实体。
"""

from .base import Base
from .user import User
from .custom_role import CustomRole
from .account import Account
from .account_member import AccountMember
from .permission_def import PermissionDef
from .member_permission import MemberPermission
from .account_quota import AccountQuota
from .system_config import SystemConfig
from .reserved_name import ReservedName
from .resource_files import (
    CPFile,
    TLFile,
    SubFile,
    PolicyFile,
    SettingsFile,
    ComponentsFile,
    CredentialsFile,
)
from .client import ClientRecord, ClientProfile
from .class_model import Class, ClassResourceSet
from .command_queue import CommandQueueRecord
from .audit import AuditLog
from .config_upload import ConfigUploadRecord
from .engine import AsyncSessionLocal, init_db
from .session import get_db
from .schema_init import ensure_tenant_schema

__all__ = [
    "Base",
    "User",
    "CustomRole",
    "Account",
    "AccountMember",
    "PermissionDef",
    "MemberPermission",
    "AccountQuota",
    "SystemConfig",
    "ReservedName",
    "CPFile",
    "TLFile",
    "SubFile",
    "PolicyFile",
    "SettingsFile",
    "ComponentsFile",
    "CredentialsFile",
    "ClientRecord",
    "ClientProfile",
    "Class",
    "ClassResourceSet",
    "CommandQueueRecord",
    "AuditLog",
    "ConfigUploadRecord",
    "AsyncSessionLocal",
    "init_db",
    "get_db",
    "ensure_tenant_schema",
]
