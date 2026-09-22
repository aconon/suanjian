# -*- coding: utf-8 -*-
"""任务：多月趋势报表（TDD 先行）——GET /api/trend。

契约 = docs/任务单/08-工资条导出与趋势报表.md：
- 按期聚合 settlements（已归期；未归期 "" 不进趋势），取近 6 期，**期升序**
  （折线图从左到右时间递增）；不足 6 期就返回已有期数
- 每期指标（金额 Decimal 精确、序列化字符串）：
  · total_amount  总产值 = Σ结算总额（individual=total_amount，team=allocated_total）
  · scrap_rate    报废率（%）= Σ报废 / Σ数量 × 100，**只统计个人计件**（班组无报废口径）；
    分母为 0 → null（宁缺勿编 0%）
  · workers       该期出现过的工人数（个人 by_person + 班组 lines 按名去重，跨结算也去重）
  · per_capita    人均产出 = 总产值 / 工人数；工人数 0 → null
- 取整：金额/比率一律 q2 银行家舍入；除法包 localcontext(prec=50)

红线：比率与人均的断言用 Decimal 精确相等（含银行家舍入边界）。
"""
import http.client
import json
import socket
import threading
from decimal import Decimal as D

import pytest

import server
from core import db

# ---------------------------------------------------------------------
# 基建：真服务器 + SUANJIAN_HOME→tmp（照 test_server_db.py 模式）
# ---------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SUANJIAN_HOME", str(tmp_path))
    yield tmp_path
    db.reset_thread_conn()


@pytest.fixture()
def srv(home, monkeypatch):
    monkeypatch.setattr(socket, "getfqdn", lambda h="": "localhost")
    httpd = server.make_server("127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield httpd.server_address
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=3)


def http_json(addr, method, path, obj=None, x_code="__unset__"):
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=5)
    try:
        headers = {}
        if obj is not None:
            headers["Content-Type"] = "application/json"
        if x_code != "__unset__":
            headers["X-Code"] = x_code
        conn.request(method, path,
                     body=json.dumps(obj, ensure_ascii=False).encode("utf-8")
                     if obj is not None else None,
                     headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, (json.loads(data.decode("utf-8")) if data else None)
    finally:
        conn.close()


def settle(addr, body):
    status, _ = http_json(addr, "POST", "/api/settle", body)
    assert status == 200, "造数失败"


ROWS_A = [
    {"process": "冲压", "name": "王建国", "qty": "100",
     "unit_price": "0.35", "defect": "1", "date": "2026-09-10"},
    {"process": "焊接", "name": "李秀英", "qty": "80",
     "unit_price": "0.5", "defect": "0", "date": "2026-09-11"},
]


def item(items, period):
    return [i for i in items if i["period"] == period][0]


# =====================================================================
# 基础聚合
# =====================================================================

class TestTrendBasics:
    def test_empty(self, srv):
        status, out = http_json(srv, "GET", "/api/trend")
        assert status == 200
        assert out == {"items": []}

    def test_single_period_exact_math(self, srv):
        # 个人：总额 74.65；数量 180、报废 1 → 报废率 1/180×100=0.555..→0.56；
        # 2 人 → 人均 74.65/2=37.325 → 银行家舍入 37.32（2 是偶数）
        settle(srv, {"mode": "individual", "rows": ROWS_A})
        status, out = http_json(srv, "GET", "/api/trend")
        assert status == 200
        assert len(out["items"]) == 1
        i = out["items"][0]
        assert i["period"] == "2026-09"
        assert D(i["total_amount"]) == D("74.65")
        assert i["scrap_rate"] == "0.56"
        assert i["workers"] == 2
        assert i["per_capita"] == "37.32"
        assert i["settlements"] == 1
        assert i["modes"] == ["individual"]

    def test_ascending_order_and_window(self, srv):
        # 8 个期 → 只留近 6 期（丢 2026-01..02），升序
        for m in range(1, 9):
            settle(srv, {"mode": "team", "team_total": "10",
                         "period": "2026-%02d" % m,
                         "rows": [{"name": "王建国", "qty": "1"}]})
        status, out = http_json(srv, "GET", "/api/trend")
        periods = [i["period"] for i in out["items"]]
        assert periods == sorted(periods)           # 升序（折线图从左到右）
        assert len(periods) == 6
        assert periods[0] == "2026-03"              # 01/02 被窗口丢掉
        assert periods[-1] == "2026-08"

    def test_unlabeled_excluded(self, srv):
        settle(srv, {"mode": "team", "team_total": "10",
                     "rows": [{"name": "x", "qty": "1"}]})   # 未归期
        status, out = http_json(srv, "GET", "/api/trend")
        assert out["items"] == []

    def test_multiple_settlements_same_period_aggregate(self, srv):
        settle(srv, {"mode": "individual", "rows": ROWS_A})
        settle(srv, {"mode": "individual", "rows": [
            {"process": "冲压", "name": "赵铁柱", "qty": "40",
             "unit_price": "0.25", "defect": "0", "date": "2026-09-20"}]})
        status, out = http_json(srv, "GET", "/api/trend")
        i = item(out["items"], "2026-09")
        assert i["settlements"] == 2
        assert D(i["total_amount"]) == D("74.65") + D("10.00")
        assert i["workers"] == 3                    # 王建国/李秀英/赵铁柱
        assert i["per_capita"] == "28.22"           # 84.65/3=28.2166..→28.22
        # 报废率合并两单：报废 1 / 数量 220 = 0.4545..% → 0.45
        assert i["scrap_rate"] == "0.45"
        assert set(i["modes"]) == {"individual"}


# =====================================================================
# 指标口径：班组/个人混合、null 语义
# =====================================================================

class TestTrendMetrics:
    def test_team_only_scrap_rate_null(self, srv):
        settle(srv, {"mode": "team", "team_total": "900", "period": "2026-09",
                     "rows": [{"name": "王建国", "qty": "2"},
                              {"name": "李秀英", "qty": "1"}]})
        status, out = http_json(srv, "GET", "/api/trend")
        i = item(out["items"], "2026-09")
        assert i["scrap_rate"] is None               # 班组无报废口径：宁缺勿编
        assert i["workers"] == 2
        assert D(i["per_capita"]) == D("450.00")     # 900/2

    def test_person_in_individual_and_team_counted_once(self, srv):
        settle(srv, {"mode": "individual", "rows": ROWS_A})
        settle(srv, {"mode": "team", "team_total": "100", "period": "2026-09",
                     "rows": [{"name": "王建国", "qty": "1"},
                              {"name": "钱二", "qty": "1"}]})
        status, out = http_json(srv, "GET", "/api/trend")
        i = item(out["items"], "2026-09")
        assert i["workers"] == 3                     # 王建国只算一次
        assert D(i["total_amount"]) == D("174.65")
        assert i["per_capita"] == "58.22"            # 174.65/3=58.2166..→58.22

    def test_workerless_result_per_capita_null(self, srv):
        # 底账被手工塞过脏 result（无 by_person/lines）：workers=0 → 人均 null 不炸
        conn = db.ensure()
        db.save_settlement(conn, "2026-09", "individual", {"total_amount": "10"})
        status, out = http_json(srv, "GET", "/api/trend")
        i = item(out["items"], "2026-09")
        assert i["workers"] == 0
        assert i["per_capita"] is None
        assert i["scrap_rate"] is None
        assert D(i["total_amount"]) == D("10")

    def test_amounts_are_strings_not_numbers(self, srv):
        settle(srv, {"mode": "individual", "rows": ROWS_A})
        _, out = http_json(srv, "GET", "/api/trend")
        i = out["items"][0]
        for k in ("total_amount", "scrap_rate", "per_capita"):
            assert i[k] is None or isinstance(i[k], str)   # 精确字符串，绝不 float


# =====================================================================
# 门禁
# =====================================================================

class TestTrendGate:
    def test_gate_401_without_header(self, srv, home):
        settle(srv, {"mode": "individual", "rows": ROWS_A})   # 先造数再上门
        (home / "config.json").write_text(
            json.dumps({"code": "9527ab"}), encoding="utf-8")
        status, out = http_json(srv, "GET", "/api/trend")
        assert status == 401
        assert "口令" in out["error"]

    def test_gate_passes_with_header(self, srv, home):
        settle(srv, {"mode": "individual", "rows": ROWS_A})   # 先造数再上门
        (home / "config.json").write_text(
            json.dumps({"code": "9527ab"}), encoding="utf-8")
        status, out = http_json(srv, "GET", "/api/trend", x_code="9527ab")
        assert status == 200
        assert len(out["items"]) == 1
