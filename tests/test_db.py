# -*- coding: utf-8 -*-
"""core.db + core.audit 单元测试（任务单 05 第 1 件，TDD 先行）。

覆盖：
- SQLite 主库落在 data/suanjian.db（SUANJIAN_HOME 可重定向，测试不碰真库）
- WAL 日志模式；幂等建表（ensure 两次不炸、表齐）
- tickets：save（upsert 幂等：同票号重存=覆盖不造第二行）、list（含行 JSON 往返）
- settlements：save + 按期取回
- audit：一行 audit() 留痕（actor/action/target/detail_json）；audit 失败绝不砸业务
- 线程局部连接：同线程复用、跨线程不串
"""
import sqlite3
import threading

import pytest

from core import audit as audit_mod
from core import db


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """SUANJIAN_HOME 重定向到临时目录：测试绝不碰 data/suanjian.db 真库。
    收尾必 reset 线程局部连接——否则上一条测试的连接跨测试残留（同线程缓存），
    带着旧 tmp 目录的句柄污染下一条。"""
    monkeypatch.setenv("SUANJIAN_HOME", str(tmp_path))
    yield tmp_path
    db.reset_thread_conn()


@pytest.fixture()
def conn(home):
    c = db.ensure()
    yield c
    c.close()
    db.reset_thread_conn()


# ---------------------------------------------------------------------
# 路径与初始化
# ---------------------------------------------------------------------

class TestInit:
    def test_db_path_under_home(self, home):
        assert db.db_path() == str(home / "suanjian.db")

    def test_ensure_idempotent(self, home):
        c1 = db.ensure()
        c2 = db.ensure()  # 幂等建表：跑两次不炸、不重复建
        assert c1 is c2
        names = {r["name"] for r in c1.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"tickets", "settlements", "audit_log"} <= names

    def test_wal_mode(self, home):
        c = db.ensure()
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_tables_recreated_after_file_deleted(self, home):
        db.ensure().close()
        db.reset_thread_conn()  # close 后线程局部仍缓存已关连接：配对重置
        (home / "suanjian.db").unlink()
        c = db.ensure()  # 新文件重新建表（连接是线程局部的，重开走新文件）
        names = {r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"tickets", "settlements", "audit_log"} <= names

    def test_thread_local_connections(self, home):
        main_conn = db.ensure()
        got = {}

        def other():
            got["conn"] = db.ensure()
            got["conn"].close()  # sqlite3 禁跨线程操作对象：子线程自己关
            db.reset_thread_conn()

        t = threading.Thread(target=other)
        t.start()
        t.join()
        assert got["conn"] is not main_conn  # 跨线程不串连接
        # 子线程退出不影响主线程连接
        assert db.get() is main_conn


# ---------------------------------------------------------------------
# tickets：存票 + 行
# ---------------------------------------------------------------------

ROWS_A = [
    {"process": "冲压", "name": "王建国", "qty": "100",
     "unit_price": "0.35", "defect": "1", "date": "2026-09-10"},
    {"process": "焊接", "name": "李秀英", "qty": "80",
     "unit_price": "0.5", "defect": "0", "date": "2026-09-11"},
]


class TestTickets:
    def test_save_and_list_roundtrip(self, conn):
        tid = db.save_ticket(conn, sheet_no="GP-001", workshop="一车间",
                             date="2026-09-10", rows=ROWS_A, csv="原始CSV文本")
        assert isinstance(tid, int)
        tickets = db.list_tickets(conn)
        assert len(tickets) == 1
        t = tickets[0]
        assert t["sheet_no"] == "GP-001"
        assert t["workshop"] == "一车间"
        assert t["date"] == "2026-09-10"
        assert t["rows"] == ROWS_A            # 行 JSON 完整往返
        assert t["csv"] == "原始CSV文本"
        assert t["created_at"]                # 建档时间非空
        assert t["id"] == tid

    def test_resave_same_sheet_no_upserts(self, conn):
        db.save_ticket(conn, "GP-001", "一车间", "2026-09-10", ROWS_A, "v1")
        tid2 = db.save_ticket(conn, "GP-001", "二车间", "2026-09-12",
                              ROWS_A[:1], "v2")  # 同票号重存=覆盖
        tickets = db.list_tickets(conn)
        assert len(tickets) == 1               # 不造第二行
        assert tickets[0]["workshop"] == "二车间"
        assert tickets[0]["csv"] == "v2"
        assert tickets[0]["id"] == tid2

    def test_list_newest_first(self, conn):
        db.save_ticket(conn, "GP-001", "", "", ROWS_A, "")
        db.save_ticket(conn, "GP-002", "", "", ROWS_A, "")
        assert [t["sheet_no"] for t in db.list_tickets(conn)] == ["GP-002", "GP-001"]

    def test_rows_and_csv_optional_default(self, conn):
        tid = db.save_ticket(conn, "GP-X", "", "", [], None)
        t = db.list_tickets(conn)[0]
        assert t["rows"] == [] and t["csv"] == ""


# ---------------------------------------------------------------------
# settlements：期间 + 模式 + 结果 JSON
# ---------------------------------------------------------------------

class TestSettlements:
    def test_save_and_fetch(self, conn):
        result = {"total_amount": "3270.81", "by_person": []}
        sid = db.save_settlement(conn, period="2026-09",
                                 mode="individual", result=result)
        rows = db.list_settlements(conn)
        assert len(rows) == 1
        s = rows[0]
        assert s["id"] == sid
        assert s["period"] == "2026-09"
        assert s["mode"] == "individual"
        assert s["result"] == result          # 结果 JSON 完整往返
        assert s["created_at"]

    def test_list_settlements_ordered(self, conn):
        db.save_settlement(conn, "2026-08", "individual", {"a": 1})
        db.save_settlement(conn, "2026-09", "team", {"b": 2})
        got = [(s["period"], s["mode"]) for s in db.list_settlements(conn)]
        assert got == [("2026-09", "team"), ("2026-08", "individual")]


# ---------------------------------------------------------------------
# audit：写操作留痕（军机处模式）
# ---------------------------------------------------------------------

class TestAudit:
    def test_audit_writes_row(self, conn):
        audit_mod.audit(conn, actor="user", action="save_ticket",
                        target="GP-001", rows=2)
        row = conn.execute(
            "SELECT actor,action,target,detail_json FROM audit_log "
            "ORDER BY id DESC LIMIT 1").fetchone()
        assert row["actor"] == "user"
        assert row["action"] == "save_ticket"
        assert row["target"] == "GP-001"
        assert '"rows": 2' in row["detail_json"] or '"rows":2' in row["detail_json"]

    def test_audit_never_raises(self, conn):
        # 审计失败绝不砸业务：连接坏掉也只吞错（军机处铁律）。
        # sqlite3.Connection 是 C 类型不许实例级 patch，用假连接对象模拟坏库
        class BoomConn:
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("boom")

            def commit(self):
                raise sqlite3.OperationalError("boom")

        audit_mod.audit(BoomConn(), "user", "x", "y")  # 不抛即过
        audit_mod.audit(conn, "user", "after", "ok")   # 真库照常能继续写
        assert conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"] == 1
