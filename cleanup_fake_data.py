# 清理 CIMS 评估环境中的"不实数据"，重新初始化
# 前置要求：已在 backups/ 留有全库备份 cims_pre_cleanup_backup_*.sql
import psycopg

DSN = "postgresql://postgresql:password@localhost:5432/cims"

def main():
    with psycopg.connect(DSN) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            # 1. 删除测试租户 schema
            for s in ["tenant_test-school", "tenant_e2e-school"]:
                cur.execute(f'DROP SCHEMA IF EXISTS "{s}" CASCADE')
                print(f"[ok] DROP SCHEMA {s}")

            # 2. 删除测试账号及其配额/成员(先删无外键约束的依赖表)
            test_accounts = ["cf0dcb10-fa53-496d-bb40-87f699366a7a", "test-account-00000000"]
            for a in test_accounts:
                cur.execute('DELETE FROM account_quotas WHERE account_id = %s', (a,))
                print(f"[ok] DEL account_quotas for {a}: {cur.rowcount}")
                cur.execute('DELETE FROM account_members WHERE account_id = %s', (a,))
                print(f"[ok] DEL account_members for {a}: {cur.rowcount}")
                cur.execute('DELETE FROM accounts WHERE id = %s', (a,))
                print(f"[ok] DEL account {a}: {cur.rowcount}")
            # 删除孤儿配额组(c2664a08, 5db3c92c 不在 accounts 中)
            orphan_quota_accounts = ["c2664a08-7107-4743-9cd3-44338730ba11",
                                     "5db3c92c-79f8-494a-be2f-08f58d934fa3"]
            for a in orphan_quota_accounts:
                cur.execute('DELETE FROM account_quotas WHERE account_id = %s', (a,))
                print(f"[ok] DEL orphan account_quotas for {a}: {cur.rowcount}")

            # 3. 删除测试用户
            test_users = ["bf22fba3-baf7-4de8-8146-bb3058412704",  # techrep01
                          "53d62767-903e-426e-8592-2f20ecd54134",  # e2e_test
                          "9b300928-9970-46d1-9ddf-1afe53faaf87"]   # e2e_test2
            for u in test_users:
                cur.execute('DELETE FROM users WHERE id = %s', (u,))
                print(f"[ok] DEL user {u}: {cur.rowcount}")

            # 4. 删除伪造设备 lab-pc-001(不代表任何真实机器,MAC 为默认假地址)
            cur.execute("DELETE FROM clients WHERE client_id = %s", ("lab-pc-001",))
            print(f"[ok] DEL client lab-pc-001: {cur.rowcount}")

            # 5. 清理测试期共享内容文件(点歌看板/课表模板均为评估产物)
            cur.execute("DELETE FROM components_files WHERE name IN ('songboard','default_components')")
            print(f"[ok] DEL components_files: {cur.rowcount}")
            cur.execute("DELETE FROM cp_files WHERE name IN ('default_classplan','probe_cp_0911','面板课表_v2')")
            print(f"[ok] DEL cp_files: {cur.rowcount}")

            # 6. 清理测试期审计/配置上传遗留
            cur.execute("DELETE FROM audit_logs")
            print(f"[ok] DEL audit_logs: {cur.rowcount}")
            cur.execute("DELETE FROM config_uploads")
            print(f"[ok] DEL config_uploads: {cur.rowcount}")

        # 校验
        with conn.cursor() as cur:
            cur.execute("SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'tenant_%'")
            print("\n=== remaining tenant schemas ===", cur.fetchall())
            cur.execute("SELECT id, slug, name FROM accounts")
            print("=== remaining accounts ===", cur.fetchall())
            cur.execute("SELECT id, username, email FROM users")
            print("=== remaining users ===", cur.fetchall())
            cur.execute("SELECT client_id, mac FROM clients")
            print("=== remaining clients ===", cur.fetchall())

    print("\nCLEANUP DONE")

if __name__ == "__main__":
    main()
