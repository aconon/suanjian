# -*- coding: utf-8 -*-
"""任务单 10：期间对比视图（TDD 先行）——GET /api/compare?p1=YYYY-MM&p2=YYYY-MM。

契约（本单定死，前端只消费）：
- 数据源 = 已归期 settlements（未归期 "" 不进）；金额 Decimal 精确、序列化字符串
- 缺省（不给 p1/p2）= 最近两期：p2=最新已归期、p1=次新；不足两期 → 400 人话
- 参数规则：只给一个 → 400；格式坏 → 400；p1==p2 → 400；p1 比 p2 晚（倒序）→ 400；
  显式给了但该期没有核算记录 → 400 人话（404 留给「老服务没这个接口」，前端探测用）
- 逐人口径（与 /api/export/payslips 汇总同一把尺子）：
  · 个人 by_person + 班组 lines 按姓名合并、跨结算累加
  · piece 计件工资 = Σ个人 amount + Σ班组分摊 amount
  · qualified 合格数量 = Σ个人 qualified_qty + Σ班组 qty（班组无报废口径，数量即合格）
  · net 实发 = engine.payslip(piece).net（个税演示口径，逐人独立算）
- 变化：diff = p2 − p1（全字符串精确）；rate = diff/p1×100（q2 银行家舍入，
  除法包 localcontext(prec=50)）；p1 基数为 0 → rate=null（宁缺勿编）
- 排序：workers 按 |net.diff| 降序（并列按姓名，输出确定性）；
  new_workers（p2 有 p1 无）/gone_workers（p1 有 p2 无）按该期 piece 降序
- periods = 全部已归期降序（前端期选择器用）；audit 留痕 action=compare
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
# 基建：真服务器 + SUANJIAN_HOME→tmp（照 test_trend_api.py 模式）
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
    status, out = http_json(addr, "POST", "/api/settle", body)
    assert status == 200, "造数失败：%s" % out


def ind_row(name, qty, price, defect="0", date="2026-09-10"):
    return {"process": "冲压", "name": name, "qty": str(qty),
            "unit_price": str(price), "defect": str(defect), "date": date}


def seed_two_periods(addr):
    """p1=2026-08：王建国 34.65/合格99、李秀英 40.00/合格80；
    p2=2026-09：王建国 70.00/合格200、赵铁柱 10.00/合格40（日期<26 自动归期）。"""
    settle(addr, {"mode": "individual", "rows": [
        ind_row("王建国", 100, "0.35", "1", "2026-08-10"),
        ind_row("李秀英", 80, "0.5", "0", "2026-08-12")]})
    settle(addr, {"mode": "individual", "rows": [
        ind_row("王建国", 200, "0.35", "0", "2026-09-10"),
        ind_row("赵铁柱", 40, "0.25", "0", "2026-09-11")]})


def worker(out, name):
    return [w for w in out["workers"] if w["name"] == name][0]


# =====================================================================
# 参数校验：空库/单期/单参/同参/倒序/坏格式/期没记录
# =====================================================================

class TestCompareParams:
    def test_empty_db_400_human(self, srv):
        status, out = http_json(srv, "GET", "/api/compare")
        assert status == 400
        assert "两期" in out["error"]            # 人话说清差什么

    def test_single_period_400(self, srv):
        settle(srv, {"mode": "individual", "rows": [ind_row("王建国", 10, "1")]})
        status, out = http_json(srv, "GET", "/api/compare")
        assert status == 400
        assert "两期" in out["error"]

    def test_one_param_only_400(self, srv):
        seed_two_periods(srv)
        status, out = http_json(srv, "GET", "/api/compare?p1=2026-08")
        assert status == 400
        status, out = http_json(srv, "GET", "/api/compare?p2=2026-09")
        assert status == 400

    def test_same_period_400(self, srv):
        seed_two_periods(srv)
        status, out = http_json(srv, "GET",
                                "/api/compare?p1=2026-09&p2=2026-09")
        assert status == 400
        assert "同一期" in out["error"]

    def test_reversed_order_400(self, srv):
        seed_two_periods(srv)
        status, out = http_json(srv, "GET",
                                "/api/compare?p1=2026-09&p2=2026-08")
        assert status == 400
        assert "早" in out["error"]              # 人话：p1 要比 p2 早

    def test_bad_format_400(self, srv):
        seed_two_periods(srv)
        status, out = http_json(srv, "GET",
                                "/api/compare?p1=2026-13&p2=2026-09")
        assert status == 400
        assert "YYYY-MM" in out["error"]

    def test_missing_period_records_400(self, srv):
        seed_two_periods(srv)
        status, out = http_json(srv, "GET",
                                "/api/compare?p1=2026-01&p2=2026-09")
        assert status == 400
        assert "2026-01" in out["error"] and "核算记录" in out["error"]

    def test_unlabeled_not_a_period(self, srv):
        # 只有未归期（""）结算：不算一期，对比要 400
        settle(srv, {"mode": "team", "team_total": "10",
                     "rows": [{"name": "x", "qty": "1"}]})
        status, out = http_json(srv, "GET", "/api/compare")
        assert status == 400


# =====================================================================
# 聚合数值：双期逐人 + 新/消失分组 + 排序 + 合计
# =====================================================================

class TestCompareAggregation:
    def test_default_latest_two_periods(self, srv):
        seed_two_periods(srv)
        settle(srv, {"mode": "team", "team_total": "10", "period": "2026-05",
                     "rows": [{"name": "老张", "qty": "1"}]})   # 更早期不选
        status, out = http_json(srv, "GET", "/api/compare")
        assert status == 200
        assert out["p1"] == "2026-08"
        assert out["p2"] == "2026-09"
        assert out["periods"] == ["2026-09", "2026-08", "2026-05"]  # 降序全量

    def test_exact_math_both_present_worker(self, srv):
        seed_two_periods(srv)
        _, out = http_json(srv, "GET", "/api/compare")
        w = worker(out, "王建国")
        # 合格数量 99→200 差 101；率 101/99×100=102.0202..→102.02
        assert w["qualified"] == {"p1": "99", "p2": "200",
                                  "diff": "101", "rate": "102.02"}
        # 计件 34.65→70.00 差 35.35；率 35.35/34.65×100 同为 102.02
        assert w["piece"] == {"p1": "34.65", "p2": "70.00",
                              "diff": "35.35", "rate": "102.02"}
        # 5000 起征点下无税：实发=计件
        assert w["net"] == {"p1": "34.65", "p2": "70.00",
                            "diff": "35.35", "rate": "102.02"}

    def test_new_and_gone_groups(self, srv):
        seed_two_periods(srv)
        _, out = http_json(srv, "GET", "/api/compare")
        assert [w["name"] for w in out["new_workers"]] == ["赵铁柱"]
        assert out["new_workers"][0]["p2"] == {"qualified": "40",
                                               "piece": "10.00", "net": "10.00"}
        assert "p1" not in out["new_workers"][0]     # 新工人没有 p1 基数
        assert [w["name"] for w in out["gone_workers"]] == ["李秀英"]
        assert out["gone_workers"][0]["p1"] == {"qualified": "80",
                                                "piece": "40.00", "net": "40.00"}
        assert "p2" not in out["gone_workers"][0]
        names = [w["name"] for w in out["workers"]]
        assert "赵铁柱" not in names and "李秀英" not in names

    def test_sorted_by_abs_net_diff_desc(self, srv):
        # 甲 |+200| > 乙 |-100| > 丙 |-50|
        settle(srv, {"mode": "individual", "rows": [
            ind_row("甲", 100, "1", "0", "2026-08-01"),
            ind_row("乙", 200, "1", "0", "2026-08-01"),
            ind_row("丙", 300, "1", "0", "2026-08-01")]})
        settle(srv, {"mode": "individual", "rows": [
            ind_row("甲", 300, "1", "0", "2026-09-01"),
            ind_row("乙", 100, "1", "0", "2026-09-01"),
            ind_row("丙", 250, "1", "0", "2026-09-01")]})
        _, out = http_json(srv, "GET", "/api/compare")
        assert [w["name"] for w in out["workers"]] == ["甲", "乙", "丙"]
        assert D(out["workers"][0]["net"]["diff"]) == D("200.00")
        assert D(out["workers"][1]["net"]["diff"]) == D("-100.00")

    def test_totals_exact(self, srv):
        seed_two_periods(srv)
        _, out = http_json(srv, "GET", "/api/compare")
        t = out["totals"]
        # 合计含新/消失工人：p1 合格 179（99+80）、p2 合格 240（200+40）
        assert t["qualified"] == {"p1": "179", "p2": "240",
                                  "diff": "61", "rate": "34.08"}
        # 计件 74.65→80.00；率 5.35/74.65×100=7.1669..→7.17
        assert t["piece"] == {"p1": "74.65", "p2": "80.00",
                              "diff": "5.35", "rate": "7.17"}
        assert D(t["net"]["p1"]) == D("74.65") and D(t["net"]["p2"]) == D("80.00")

    def test_explicit_periods(self, srv):
        seed_two_periods(srv)
        settle(srv, {"mode": "team", "team_total": "10", "period": "2026-05",
                     "rows": [{"name": "老张", "qty": "1"}]})
        status, out = http_json(srv, "GET",
                                "/api/compare?p1=2026-05&p2=2026-08")
        assert status == 200
        assert out["p1"] == "2026-05" and out["p2"] == "2026-08"
        assert [w["name"] for w in out["gone_workers"]] == ["老张"]

    def test_tax_makes_net_differ_from_piece(self, srv):
        # p1 计件 4000（无税）→ p2 计件 6000：个税 (6000-5000)×3%=30 → 实发 5970
        settle(srv, {"mode": "individual", "rows": [
            ind_row("王建国", 4000, "1", "0", "2026-08-01")]})
        settle(srv, {"mode": "individual", "rows": [
            ind_row("王建国", 6000, "1", "0", "2026-09-01")]})
        _, out = http_json(srv, "GET", "/api/compare")
        w = worker(out, "王建国")
        assert w["piece"]["p2"] == "6000.00"
        assert w["net"]["p1"] == "4000.00"
        assert w["net"]["p2"] == "5970.00"
        assert w["net"]["diff"] == "1970.00"       # 4000→5970
        assert w["net"]["rate"] == "49.25"         # 1970/4000×100

    def test_team_and_individual_merged_by_name(self, srv):
        # 王建国 p1 同时出现在个人（34.65/合格99）与班组分摊（100.00/数量10）
        settle(srv, {"mode": "individual", "rows": [
            ind_row("王建国", 100, "0.35", "1", "2026-08-10")]})
        settle(srv, {"mode": "team", "team_total": "200", "period": "2026-08",
                     "rows": [{"name": "王建国", "qty": "10"},
                              {"name": "钱二", "qty": "10"}]})
        settle(srv, {"mode": "individual", "rows": [
            ind_row("王建国", 200, "0.35", "0", "2026-09-10")]})
        _, out = http_json(srv, "GET", "/api/compare")
        w = worker(out, "王建国")
        assert w["piece"]["p1"] == "134.65"        # 34.65 + 100.00
        assert w["qualified"]["p1"] == "109"       # 合格 99 + 班组数量 10
        assert w["net"]["p1"] == "134.65"
        assert [x["name"] for x in out["gone_workers"]] == ["钱二"]

    def test_rate_null_when_p1_base_zero(self, srv):
        # p1 全废（合格 0、计件 0）→ 率没有基数：null 宁缺勿编
        settle(srv, {"mode": "individual", "rows": [
            ind_row("王建国", 10, "5", "10", "2026-08-01")]})
        settle(srv, {"mode": "individual", "rows": [
            ind_row("王建国", 10, "5", "0", "2026-09-01")]})
        _, out = http_json(srv, "GET", "/api/compare")
        w = worker(out, "王建国")
        assert w["piece"]["p1"] == "0.00"
        assert w["piece"]["diff"] == "50.00"
        assert w["piece"]["rate"] is None
        assert w["qualified"]["rate"] is None

    def test_all_amounts_are_strings(self, srv):
        seed_two_periods(srv)
        _, out = http_json(srv, "GET", "/api/compare")
        for w in out["workers"]:
            for m in ("qualified", "piece", "net"):
                for k in ("p1", "p2", "diff"):
                    assert isinstance(w[m][k], str)   # 绝不 float
        for grp in ("new_workers", "gone_workers"):
            for w in out[grp]:
                for side in ("p1", "p2"):
                    if side in w:
                        for k, v in w[side].items():
                            assert isinstance(v, str)


# =====================================================================
# 门禁 + 审计
# =====================================================================

class TestCompareGateAndAudit:
    def test_gate_401_without_header(self, srv, home):
        seed_two_periods(srv)
        (home / "config.json").write_text(
            json.dumps({"code": "9527ab"}), encoding="utf-8")
        status, out = http_json(srv, "GET", "/api/compare")
        assert status == 401
        assert "口令" in out["error"]

    def test_gate_passes_with_header(self, srv, home):
        seed_two_periods(srv)
        (home / "config.json").write_text(
            json.dumps({"code": "9527ab"}), encoding="utf-8")
        status, out = http_json(srv, "GET", "/api/compare", x_code="9527ab")
        assert status == 200
        assert out["p2"] == "2026-09"

    def test_audit_logged(self, srv, home):
        seed_two_periods(srv)
        status, _ = http_json(srv, "GET", "/api/compare")
        assert status == 200
        rows = [dict(r) for r in db.ensure().execute(
            "SELECT actor,action,target FROM audit_log WHERE action='compare'")]
        assert len(rows) == 1                      # 对比一次留痕一行
        assert rows[0]["actor"] == "user"
        assert rows[0]["target"] == "2026-08~2026-09"

    def test_failed_compare_not_audited(self, srv):
        status, _ = http_json(srv, "GET", "/api/compare")   # 空库 400
        assert status == 400
        rows = db.ensure().execute(
            "SELECT id FROM audit_log WHERE action='compare'").fetchall()
        assert rows == []                          # 400 不留痕（只记成功的对比）
