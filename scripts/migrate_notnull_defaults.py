"""给既有租户 Schema 补齐 NOT NULL 列的 DEFAULT（server_default 迁移）。

背景（一套踩过两次的坑）：模型里只写 Python 侧 `default=` 时，**裸 SQL INSERT**
（CLI 导入脚本、租户重建脚本、早期登记脚本）不会带上该列 → 直接 NOT NULL 违例。
新建的 Schema 会带上 server_default，但**已存在的 Schema 不会自动补**，
于是表现为「新租户能建、老租户一跑脚本就炸」。

本脚本幂等：对每个 `tenant_*` Schema 的指定列执行 `ALTER COLUMN ... SET DEFAULT ...`，
已经有的会静默重设（无副作用）。

用法（在 CIMS-backend 目录）：
    PYTHONPATH=. .venv/Scripts/python.exe scripts/migrate_notnull_defaults.py
    PYTHONPATH=. .venv/Scripts/python.exe scripts/migrate_notnull_defaults.py --schema tenant_demo-class
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine, text

DATABASE_URL = "postgresql+psycopg://postgresql:password@localhost:5432/cims"

# (表, 列, DEFAULT 表达式)
MIGRATIONS: list[tuple[str, str, str]] = [
    # 资源文件表（裸 SQL 插入时常常只给 name/content）
    *[(t, "content", "''") for t in ("cp_files", "tl_files", "sub_files", "policy_files",
                                     "settings_files", "components_files", "credentials_files")],
    *[(t, "version", "0") for t in ("cp_files", "tl_files", "sub_files", "policy_files",
                                    "settings_files", "components_files", "credentials_files")],
    *[(t, "updated_at", "now()") for t in ("cp_files", "tl_files", "sub_files", "policy_files",
                                           "settings_files", "components_files", "credentials_files")],
    # 设备登记
    ("clients", "client_id", "''"),
    ("clients", "mac", "''"),
    ("clients", "registered_at", "now()"),
    ("client_profiles", "class_id", "''"),
    ("client_profiles", "class_plan", "'default_classplan'"),
    ("client_profiles", "time_layout", "'default_timelayout'"),
    ("client_profiles", "subjects", "'default'"),
    ("client_profiles", "default_settings", "'default'"),
    ("client_profiles", "policy", "'default'"),
    ("client_profiles", "components", "'default'"),
    ("client_profiles", "credentials", "'default'"),
    ("client_profiles", "updated_at", "now()"),
    # 命令队列
    ("command_queue", "client_id", "''"),
    ("command_queue", "command_type", "''"),
    ("command_queue", "payload", "''"),
    ("command_queue", "ack_status", "'pending'"),
    ("command_queue", "status", "'pending'"),
    ("command_queue", "created_at", "now()"),
    # 班级层
    ("classes", "name", "''"),
    ("classes", "resource_set_id", "''"),
    ("classes", "sort_order", "0"),
    ("classes", "created_at", "now()"),
    ("classes", "updated_at", "now()"),
    ("class_resource_sets", "class_plan", "'default_classplan'"),
    ("class_resource_sets", "time_layout", "'default_timelayout'"),
    ("class_resource_sets", "subjects", "'default'"),
    ("class_resource_sets", "default_settings", "'default'"),
    ("class_resource_sets", "policy", "'default'"),
    ("class_resource_sets", "components", "'default'"),
    ("class_resource_sets", "credentials", "'default'"),
    ("class_resource_sets", "updated_at", "now()"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐租户 Schema 的列默认值")
    ap.add_argument("--schema", default="", help="只处理指定 schema（缺省处理全部 tenant_*）")
    ap.add_argument("--url", default=DATABASE_URL)
    args = ap.parse_args()

    eng = create_engine(args.url)
    applied = skipped = 0
    with eng.begin() as c:
        if args.schema:
            schemas = [args.schema]
        else:
            schemas = [
                r[0]
                for r in c.execute(
                    text(
                        "SELECT schema_name FROM information_schema.schemata "
                        "WHERE schema_name LIKE 'tenant\\_%' ORDER BY 1"
                    )
                ).all()
            ]
        print(f"目标 schema：{schemas}")
        for schema in schemas:
            for table, column, default in MIGRATIONS:
                exists = c.execute(
                    text(
                        """
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = :s AND table_name = :t AND column_name = :c
                        """
                    ),
                    {"s": schema, "t": table, "c": column},
                ).first()
                if not exists:
                    skipped += 1
                    continue
                c.execute(
                    text(
                        f'ALTER TABLE "{schema}"."{table}" '
                        f'ALTER COLUMN "{column}" SET DEFAULT {default}'
                    )
                )
                applied += 1
    print(f"[ok] 应用 {applied} 条 SET DEFAULT，跳过 {skipped} 条（列不存在）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
