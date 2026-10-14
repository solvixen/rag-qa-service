"""批次 12 回归测试：run_sql 与数据库连接必须是真只读（评审实测可 DELETE 删数据）。

评审反馈原文：
  「run_sql 仍走可写连接：cleaning.py:98-102 的 open_readonly() 实际是普通读写连接，
    tools.py:76-81 的 run_sql 原样执行并 commit()，而它是暴露给模型的工具。
    我们在一份数据库副本上直接调用这个工具执行 DELETE，数据确实会被删掉。」

两层都要钉住：
  1) 连接层：open_readonly() 必须用 mode=ro 打开，写语句被 SQLite 直接拒绝；
  2) 语句层：run_sql() 只放行单条 SELECT / WITH，其它一律返回 error。

修复前预期：test_delete_is_rejected、test_readonly_connection_rejects_write 等为红
（DELETE 会成功执行并 commit，数据真的少掉）。
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kbqa.cleaning import open_readonly  # noqa: E402
from kbqa.config import Settings  # noqa: E402
from kbqa.service import Service  # noqa: E402
from kbqa.tools import DataTools  # noqa: E402

WORKSPACE = ROOT.parent


# -- 单元层：连接本身必须只读 ---------------------------------------------------


def _tiny_db(tmp_path: Path) -> Path:
    """一个 3 行的小库，专门用来验证写语句被拒。"""
    path = tmp_path / "tiny.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE t (a INTEGER); INSERT INTO t VALUES (1),(2),(3);")
    conn.commit()
    conn.close()
    return path


def test_readonly_connection_rejects_write(tmp_path):
    """连接层：open_readonly 打开的连接执行写语句必须抛 OperationalError。"""
    conn = open_readonly(_tiny_db(tmp_path))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM t")
    conn.close()


def test_readonly_connection_still_reads(tmp_path):
    """守卫：只读连接必须还能正常读。"""
    conn = open_readonly(_tiny_db(tmp_path))
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3
    conn.close()


# -- 语句层：run_sql 只放行单条 SELECT / WITH -----------------------------------


def _tools(tmp_path: Path) -> DataTools:
    return DataTools(_tiny_db(tmp_path))


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM t",
        "UPDATE t SET a = 0",
        "INSERT INTO t VALUES (99)",
        "DROP TABLE t",
        "ALTER TABLE t RENAME TO t2",
        "CREATE TABLE u (b INTEGER)",
        "PRAGMA journal_mode = WAL",
        "ATTACH DATABASE 'other.db' AS other",
        "VACUUM",
        "SELECT 1; DELETE FROM t",
    ],
)
def test_write_and_multi_statements_are_rejected(tmp_path, sql):
    """写语句、DDL、PRAGMA 写、ATTACH、多语句——全部必须被拒。"""
    got = _tools(tmp_path).run_sql(sql)
    assert isinstance(got, dict) and "error" in got, "run_sql 放行了非只读语句：%s" % sql


def test_select_still_works(tmp_path):
    """守卫：正常 SELECT 不许被这次改动影响。"""
    got = _tools(tmp_path).run_sql("SELECT a FROM t ORDER BY a")
    assert "error" not in got
    assert got["row_count"] == 3


def test_with_statement_still_works(tmp_path):
    """守卫：CTE（WITH）是只读查询，必须放行。"""
    got = _tools(tmp_path).run_sql("WITH x AS (SELECT a FROM t) SELECT COUNT(*) AS n FROM x")
    assert "error" not in got
    assert got["rows"][0]["n"] == 3


# -- 集成层：走 Service.run_tool，钉住"数据不能少" -------------------------------


def make_service(tmp_path: Path) -> Service:
    settings = Settings(
        data_dir=WORKSPACE / "data",
        kb_dir=WORKSPACE / "knowledge_base",
        var_dir=tmp_path / "var",
        today=date(2026, 9, 1),
        llm_base_url="",
        llm_api_key="",
        llm_model="",
        llm_timeout=120.0,
        chat_budget=150.0,
    )
    return Service(settings)


def test_delete_via_tool_leaves_data_intact(tmp_path):
    """评审复现的那一条：DELETE 必须被拒，且清洗表一行都不能少。"""
    svc = make_service(tmp_path)
    before = svc.tools.valid_sales_rows()
    assert before > 0

    got = svc.run_tool("run_sql", {"sql": "DELETE FROM sales_clean"})

    assert "error" in got, "run_sql 放行了 DELETE——这正是评审复现出来的那条"
    assert svc.tools.valid_sales_rows() == before, "DELETE 真的删掉了数据"


def test_write_attempt_survives_reconnect(tmp_path):
    """更狠一层：写失败后重开一条连接，行数仍然不变（没被静默写盘）。"""
    svc = make_service(tmp_path)
    before = svc.tools.valid_sales_rows()
    svc.run_tool("run_sql", {"sql": "DELETE FROM sales_clean"})

    svc.tools.close()
    assert svc.tools.valid_sales_rows() == before
