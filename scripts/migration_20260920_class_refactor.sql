-- ============================================================================
-- 2026-09-20 班级模型重构迁移（文件夹式班级 + 内容审核 + 多用户隔离 + 预览图）
--
-- 背景：CIMS 无 Alembic，schema 由 ORM 的 Base.metadata.create_all 驱动。
--   create_all **只建缺失的表，不会给已存在的表补列**。因此已上线的租户
--   （例如 tenant_demo-class）必须用本脚本显式 ALTER，才能拿到新列。
--
-- 适用范围：所有 tenant_* schema（逐个遍历，自动跳过不存在的表）。
-- 幂等性：可安全重复执行；classes 的新增列只在「首次尚无 review_status 列」时执行，
--        避免重跑时把**新建的待审班级**误标为已通过。
--
-- 执行方式（示例）：
--   psql "$DATABASE_URL" -f scripts/migration_20260920_class_refactor.sql
-- 或在 pgsql 客户端里整体粘贴执行。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1) classes：新增 编号 / 届班号 / 属主 / 审核态 等列
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    s           text;
    col_exists  boolean;
BEGIN
    FOR s IN
        SELECT schema_name FROM information_schema.schemata WHERE schema_name ~ '^tenant_'
    LOOP
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = s AND table_name = 'classes' AND column_name = 'review_status'
        ) INTO col_exists;

        IF NOT col_exists THEN
            -- 仅当 classes 表存在时才补列（空 schema 交给应用 create_all 建整表）
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = s AND table_name = 'classes'
            ) THEN
                EXECUTE format('ALTER TABLE %I.classes ADD COLUMN code VARCHAR(128) NOT NULL DEFAULT %L', s, '');
                EXECUTE format('ALTER TABLE %I.classes ADD COLUMN graduation_year INTEGER', s);
                EXECUTE format('ALTER TABLE %I.classes ADD COLUMN class_number INTEGER', s);
                EXECUTE format('ALTER TABLE %I.classes ADD COLUMN owner_user_id VARCHAR NOT NULL DEFAULT %L', s, '');
                EXECUTE format('ALTER TABLE %I.classes ADD COLUMN review_status VARCHAR(16) NOT NULL DEFAULT ''pending''', s);
                EXECUTE format('ALTER TABLE %I.classes ADD COLUMN reviewed_by VARCHAR NOT NULL DEFAULT %L', s, '');
                EXECUTE format('ALTER TABLE %I.classes ADD COLUMN reviewed_at TIMESTAMP WITH TIME ZONE', s);
                EXECUTE format('ALTER TABLE %I.classes ADD COLUMN reject_reason VARCHAR(512) NOT NULL DEFAULT %L', s, '');

                -- 既有班级 grandfather 为「已审核」：历史/系统班级不应被新审核门控挡住
                EXECUTE format('UPDATE %I.classes SET review_status = ''approved'', reviewed_at = now()', s);
                -- 编号回落到班级名，保证 code 非空（组合显示名的前缀）
                EXECUTE format('UPDATE %I.classes SET code = name WHERE code = %L', s, '');

                EXECUTE format('CREATE INDEX IF NOT EXISTS ix_classes_owner_user_id ON %I.classes (owner_user_id)', s);
                EXECUTE format('CREATE INDEX IF NOT EXISTS ix_classes_review_status ON %I.classes (review_status)', s);
            END IF;
        END IF;
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 2) client_status：新增设备运行系统（设备级属性，用于拼接「xx届x班_Windows」）
-- ---------------------------------------------------------------------------
DO $$
DECLARE s text;
BEGIN
    FOR s IN
        SELECT schema_name FROM information_schema.schemata WHERE schema_name ~ '^tenant_'
    LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = s AND table_name = 'client_status'
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I.client_status ADD COLUMN IF NOT EXISTS os_name VARCHAR(32) NOT NULL DEFAULT %L',
                s, ''
            );
        END IF;
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 3) 新表：班级审计流水 + 班级预览图（160x90 JPEG）
--    应用 create_all 也会为**新租户**建这两张表；此处显式建是为了让老租户立即具备。
-- ---------------------------------------------------------------------------
DO $$
DECLARE s text;
BEGIN
    FOR s IN
        SELECT schema_name FROM information_schema.schemata WHERE schema_name ~ '^tenant_'
    LOOP
        EXECUTE format($f$
            CREATE TABLE IF NOT EXISTS %I.class_audit_log (
                id            VARCHAR(64)  NOT NULL,
                class_id      VARCHAR(64)  NOT NULL DEFAULT '',
                actor_user_id VARCHAR      NOT NULL DEFAULT '',
                action        VARCHAR(32)  NOT NULL DEFAULT '',
                detail        TEXT         NOT NULL DEFAULT '{}',
                created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                PRIMARY KEY (id)
            )$f$, s);
        EXECUTE format('CREATE INDEX IF NOT EXISTS ix_class_audit_log_class_id ON %I.class_audit_log (class_id)', s);
        EXECUTE format('CREATE INDEX IF NOT EXISTS ix_class_audit_log_created_at ON %I.class_audit_log (created_at)', s);

        EXECUTE format($f$
            CREATE TABLE IF NOT EXISTS %I.class_previews (
                class_id         VARCHAR(64) NOT NULL,
                content          BYTEA,
                width            INTEGER     NOT NULL DEFAULT 0,
                height           INTEGER     NOT NULL DEFAULT 0,
                source_client_id VARCHAR     NOT NULL DEFAULT '',
                updated_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
                PRIMARY KEY (class_id)
            )$f$, s);
        EXECUTE format('CREATE INDEX IF NOT EXISTS ix_class_previews_updated_at ON %I.class_previews (updated_at)', s);
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 校验（可选）：查看各租户 classes 的新列与班级审核分布
-- ---------------------------------------------------------------------------
-- SELECT table_schema, column_name, data_type, column_default
--   FROM information_schema.columns
--  WHERE table_name = 'classes' AND column_name IN ('code','review_status','owner_user_id')
--  ORDER BY table_schema, column_name;
