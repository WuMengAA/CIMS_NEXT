"""v2 架构变更迁移：为所有已存在租户 Schema 补列。

Phase 1+3 新增了多个列到已有租户表：
  - client_profiles.class_id        （班级绑定）
  - pairing_codes.class_code/class_id（配对班级码）
  - command_queue.ack_status / delivered_at / ack_at（命令确认语义）

新表 classes / class_resource_sets 由启动时 _reconcile_tenant_schemas 的
create_all(skip_existing) 自动补建，本脚本只负责给「已有表」补列（幂等）。

无条件自适应：遍历所有 tenant_* schema，逐表 ADD COLUMN IF NOT EXISTS。
"""
import re
from sqlalchemy import create_engine, text

from app.core.config import DATABASE_URL

# (表, 列, 类型, 默认)
_COLUMNS = [
    ("client_profiles", "class_id", "VARCHAR(64)", "''"),
    ("pairing_codes", "class_code", "VARCHAR(32)", "''"),
    ("pairing_codes", "class_id", "VARCHAR(64)", "''"),
    ("command_queue", "ack_status", "VARCHAR(16)", "'pending'"),
    ("command_queue", "delivered_at", "TIMESTAMPTZ", "NULL"),
    ("command_queue", "ack_at", "TIMESTAMPTZ", "NULL"),
]

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 租户 schema 名可能含连字符（如 tenant_demo-class），用较宽松但安全的匹配
_SCHEMA_IDENT = re.compile(r"^tenant_[A-Za-z0-9_-]+$")


def main() -> None:
    eng = create_engine(DATABASE_URL)
    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name LIKE 'tenant_%'"
        )).all()
        changed = 0
        for (sn,) in rows:
            if not _SCHEMA_IDENT.match(sn):
                continue
            for tbl, col, typ, default in _COLUMNS:
                if not all(_IDENT.match(x) for x in (tbl, col, typ.split("(")[0])):
                    continue
                sql = (
                    f'ALTER TABLE "{sn}"."{tbl}" '
                    f"ADD COLUMN IF NOT EXISTS {col} {typ} DEFAULT {default}"
                )
                conn.execute(text(sql))
                changed += 1
        conn.commit()
        print(f"已为 {len(rows)} 个租户 schema 补列（执行 {changed} 条 ALTER）")
    eng.dispose()


if __name__ == "__main__":
    main()
