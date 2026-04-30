from __future__ import annotations

import unittest
import sys
import types

sys.modules.setdefault("requests", types.ModuleType("requests"))
from auto_test_pr import _validate_bvt_sql_safety


class BvtSqlSafetyTest(unittest.TestCase):
    def test_allows_table_ddl_with_dedicated_prefix(self) -> None:
        _validate_bvt_sql_safety(
            """
            create table bvt_sample_t1(a int);
            insert into bvt_sample_t1 values (1);
            select * from bvt_sample_t1;
            drop table bvt_sample_t1;
            """,
            "sample",
        )

    def test_rejects_database_level_ddl(self) -> None:
        for sql in (
            "use test_nightly;",
            "create database mo_test;",
            "create or replace database mo_test;",
            "drop database mo_test;",
            "alter database mo_test charset utf8;",
            "create schema mo_test;",
            "drop schema mo_test;",
            "alter schema mo_test charset utf8;",
        ):
            with self.subTest(sql=sql):
                with self.assertRaisesRegex(ValueError, "database-level statement"):
                    _validate_bvt_sql_safety(sql, "sample")

    def test_rejects_database_qualified_tables(self) -> None:
        with self.assertRaisesRegex(ValueError, "database-qualified table names"):
            _validate_bvt_sql_safety(
                "select * from test_nightly.some_table;",
                "sample",
            )

    def test_rejects_tables_without_prefix(self) -> None:
        with self.assertRaisesRegex(ValueError, "dedicated prefix"):
            _validate_bvt_sql_safety(
                "create table t1(a int); insert into t1 values (1); drop table t1;",
                "sample",
            )


if __name__ == "__main__":
    unittest.main()
