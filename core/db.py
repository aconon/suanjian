#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.db — 算件主库（SQLite WAL，data/suanjian.db）。

- 主库：data/suanjian.db（相对本文件 ../data/；SUANJIAN_HOME 可重定向——测试/多实例用）
- 打包态（sys.frozen，PyInstaller）：数据跟 exe 走（与 server.py BASE_DIR 同口径，
  __file__ 在临时解包目录，退出即删，不能放库）
- WAL + busy_timeout=30000：ThreadingHTTPServer 每请求一线程，跨线程并发写不炸
  （防先前项目踩过的 SQLite 并发锁坑）
- 连接线程局部（同线程复用，跨线程各开各的，绝不跨线程共享 sqlite3.Connection）
- 建表幂等：CREATE TABLE IF NOT EXISTS，ensure() 每线程首跑一次，重复跑无害

表：
- tickets     工票：票号(UNIQUE，重存=覆盖)/车间/日期/行 JSON/原始 CSV/created_at
- settlements 结算底账：期间(YYYY-MM，空串=未归期)/模式/结果 JSON/created_at
- audit_log   审计留痕（成熟模式，配套 core/audit.py）
"""
import json
import os
import sqlite3
import sys
import threading

_LOCAL = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sheet_no TEXT NOT NULL UNIQUE,      -- 票号：同票号重存 = 覆盖（幂等）
  workshop TEXT NOT NULL DEFAULT '',  -- 车间
  date TEXT NOT NULL DEFAULT '',      -- 工票日期 YYYY-MM-DD（可空）
  rows_json TEXT NOT NULL DEFAULT '[]',  -- 工票行（PieceRecord.to_dict 列表）
  csv TEXT,                           -- 原始 CSV（导入来源留底；无则 NULL）
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS settlements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  period TEXT NOT NULL DEFAULT '',    -- 核算期 YYYY-MM（空=跨期/未归期）
  mode TEXT NOT NULL,                 -- individual / team
  result_json TEXT NOT NULL,          -- 引擎核算结果（Decimal 已转精确字符串）
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,                -- user / auto
  action TEXT NOT NULL,               -- save_ticket / settle / ...
  target TEXT NOT NULL DEFAULT '',
  detail_json TEXT NOT NULL DEFAULT '{}',
  at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_settlements_period ON settlements(period, created_at);
CREATE INDEX IF NOT EXISTS idx_tickets_created ON tickets(id);
"""


def home_dir() -> str:
    """数据目录：SUANJIAN_HOME 覆盖 > 打包态跟 exe > 源码态跟项目 data/。

    server._log_dir 也复用本函数当日志目录：frozen 态日志跟着在 exe
    目录**另起一本**（R10-2 口径，R11 补在此端写明）——exe 换目录运行
    就在新目录旁新建日志，旧目录里的日志历史不延续（不合并、不搬运）。
    """
    env = os.environ.get("SUANJIAN_HOME")
    if env:
        return env
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def db_path() -> str:
    return os.path.join(home_dir(), "suanjian.db")


def get() -> sqlite3.Connection:
    """线程局部连接；row_factory=Row；WAL + busy_timeout（并发写防锁死）。"""
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        os.makedirs(home_dir(), exist_ok=True)
        conn = sqlite3.connect(db_path(), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        _LOCAL.conn = conn
    return conn


def reset_thread_conn() -> None:
    """关掉并清空本线程连接与建表标记（测试收尾用；业务代码不需要）。"""
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception as e:  # 已关/已坏的连接：收尾动作绝不抛，但留痕
            sys.stderr.write("[db] 收尾关连接失败（忽略）：%s\n" % e)
    _LOCAL.conn = None
    _LOCAL.schema_ready = False


def ensure() -> sqlite3.Connection:
    """取本线程连接并保证表已建（幂等；每线程首跑一次 schema）。"""
    conn = get()
    if not getattr(_LOCAL, "schema_ready", False):
        conn.executescript(SCHEMA)
        conn.commit()
        _LOCAL.schema_ready = True
    return conn


# ------------------------------------------------------------------
# tickets：存票 + 行
# ------------------------------------------------------------------

def save_ticket(conn: sqlite3.Connection, sheet_no: str, workshop: str,
                date: str, rows: list, csv) -> int:
    """存一张工票（票号 UNIQUE：重存同票号 = 覆盖，返回行 id）。"""
    conn.execute(
        "INSERT INTO tickets(sheet_no,workshop,date,rows_json,csv) VALUES(?,?,?,?,?) "
        "ON CONFLICT(sheet_no) DO UPDATE SET "
        "workshop=excluded.workshop, date=excluded.date, "
        "rows_json=excluded.rows_json, csv=excluded.csv, "
        "created_at=datetime('now','localtime')",
        (sheet_no, workshop or "", date or "",
         json.dumps(rows or [], ensure_ascii=False), csv))
    conn.commit()
    return conn.execute("SELECT id FROM tickets WHERE sheet_no=?", (sheet_no,)).fetchone()["id"]


def list_tickets(conn: sqlite3.Connection) -> list:
    """全部工票（新票在前），rows_json/csv 还原为原始形态。"""
    out = []
    for r in conn.execute("SELECT * FROM tickets ORDER BY id DESC"):
        out.append({
            "id": r["id"], "sheet_no": r["sheet_no"], "workshop": r["workshop"],
            "date": r["date"], "rows": json.loads(r["rows_json"]),
            "csv": r["csv"] if r["csv"] is not None else "",
            "created_at": r["created_at"],
        })
    return out


# ------------------------------------------------------------------
# settlements：结算底账
# ------------------------------------------------------------------

def save_settlement(conn: sqlite3.Connection, period: str, mode: str,
                    result: dict) -> int:
    """落一条结算记录（period 空=未归期）。返回行 id。"""
    cur = conn.execute(
        "INSERT INTO settlements(period,mode,result_json) VALUES(?,?,?)",
        (period or "", mode, json.dumps(result, ensure_ascii=False)))
    conn.commit()
    return cur.lastrowid


def list_settlements(conn: sqlite3.Connection) -> list:
    """全部结算记录（新在前），result_json 还原为 dict。"""
    out = []
    for r in conn.execute("SELECT * FROM settlements ORDER BY id DESC"):
        out.append({
            "id": r["id"], "period": r["period"], "mode": r["mode"],
            "result": json.loads(r["result_json"]), "created_at": r["created_at"],
        })
    return out
