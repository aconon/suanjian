# -*- coding: utf-8 -*-
"""server.py HTTP 服务层测试（TDD 先行）——curl 级用例：起真实 ThreadingHTTPServer 打真请求。

覆盖（任务单 docs/任务单/03-服务层接线）：
- POST /api/ingest/csv：正常导入（中文表头/英文表头/缺废品列/BOM+CRLF/JSON 信封）
  与校验失败（坏数字/负数/废品超数量/坏日期/空内容/只有表头/非 UTF-8），400 带行号
- POST /api/settle：individual/team 正常核算（数值与 core.engine 交叉对账 + Decimal 不变量）、
  mode 缺失/非法、team_total 缺失/负数、成员负数、字段错名、rows 空/非数组、坏 JSON
- GET /api/payslip：边界金额精确值、与引擎交叉对账、tax_note 标注演示口径、
  gross 缺失/垃圾/负数
- GET /api/health：{"ok": true}
- GET /：静态 web/（index.html / style.css / app.js），路径穿越一律 404
- 成熟模式回归：keep-alive 串包防护（GET 带 body 后复用 / 错误后复用 / 未知路由后复用）、
  413 超限收线、chunked 411、全响应带 Content-Length、日志净化（控制字符转义）、
  引擎炸掉 500 兜底且服务线程不死、_safe_rollback 永不抛
- R1-R4 修复单回归：Host 白名单 403 收线、负 Content-Length 收线、数值幅度上限 400、
  errorhandler 补 TimeoutError/socket.timeout/DecimalException、日志净化扩 DEL + gross 脱敏、
  sys.frozen 数据目录跟 exe 走

红线：金额断言用 Decimal 精确相等；异常路径一律断言状态码 + 报错关键词。
"""
import http.client
import json
import logging
import os
import socket
import sys
import threading
from decimal import Decimal as D

import pytest

import server
from core import engine

# ---------------------------------------------------------------------
# 测试基建：真服务器 + 轻客户端 + 裸 socket（穿越/keep-alive 用）
# ---------------------------------------------------------------------


@pytest.fixture()
def srv(monkeypatch, tmp_path):
    # 坑-HTTPServer首次bind卡35秒反向DNS：起真实 HTTPServer 必须桩掉 getfqdn
    # 坑-test_server.py 曾无 SUANJIAN_HOME 隔离（test_server_db/gate 有）：真 data/
    # config.json 一旦设了 code（口令门），全部 API 用例被 401 打挂；且 settle 用例
    # 会把测试底账写进真库 data/suanjian.db（09-20 实踩）。对齐姊妹测试：家安 tmp。
    monkeypatch.setenv("SUANJIAN_HOME", str(tmp_path))
    monkeypatch.setattr(socket, "getfqdn", lambda h="": "localhost")
    httpd = server.make_server("127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield httpd.server_address
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=3)
    from core import db
    db.reset_thread_conn()


class Client:
    """每个请求一条连接的轻封装（keep-alive 用例走裸 socket 或显式复用连接）。"""

    def __init__(self, addr):
        self.addr = addr

    def request(self, method, path, body=None, ctype=None):
        conn = http.client.HTTPConnection(self.addr[0], self.addr[1], timeout=5)
        try:
            headers = {"Content-Type": ctype} if ctype else {}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, data, resp.getheader("Content-Type")
        finally:
            conn.close()

    def json(self, method, path, obj=None, ctype="application/json"):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8") if obj is not None else None
        status, data, _ = self.request(method, path, body=body, ctype=ctype)
        return status, (json.loads(data.decode("utf-8")) if data else None)

    def text(self, method, path, text, ctype):
        status, data, _ = self.request(method, path, body=text.encode("utf-8"), ctype=ctype)
        parsed = json.loads(data.decode("utf-8")) if data else None
        return status, parsed


def read_response(sock):
    """读一条完整 HTTP 响应（按 Content-Length 定界，供 keep-alive 连接连续读取）。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    if not head:
        return head, rest
    clen = 0
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            clen = int(line.split(b":", 1)[1].strip())
    while len(rest) < clen:
        chunk = sock.recv(65536)
        if not chunk:
            break
        rest += chunk
    return head, rest


def send_raw(addr, payload, timeout=3.0):
    sock = socket.create_connection(addr, timeout=timeout)
    try:
        sock.sendall(payload)
        return read_response(sock)
    finally:
        sock.close()


# ---------------------------------------------------------------------
# 测试数据
# ---------------------------------------------------------------------

ROW_WANG1 = {"process": "冲压", "name": "王建国", "qty": 100,
             "unit_price": "0.35", "defect": 0, "date": "2026-09-10"}
ROW_WANG2 = {"process": "冲压", "name": "王建国", "qty": 50,
             "unit_price": "0.35", "defect": 2, "date": "2026-09-11"}
ROW_LI = {"process": "焊接", "name": "李秀英", "qty": 80,
          "unit_price": "0.5", "defect": 3, "date": "2026-09-12"}

CSV_OK = (
    "票号,车间,日期,工序,姓名,数量,单价,废品\r\n"
    "GP-001,一车间,2026-09-10,冲压,王建国,100,0.35,1\r\n"
    "GP-002,一车间,2026-09-11,焊接,李秀英,80,0.5,0\r\n"
)

CSV_BAD_NUM = (
    "工序,姓名,数量,单价\n"
    "冲压,王建国,100,0.35\n"
    "冲压,李秀英,abc,0.5\n"
)

CSV_NEG = "工序,姓名,数量,单价\n冲压,王建国,-5,0.35\n"
CSV_DEFECT_OVER = "工序,姓名,数量,单价,废品\n冲压,王建国,10,0.35,50\n"
CSV_BAD_DATE = "工序,姓名,数量,单价,日期\n冲压,王建国,10,0.35,2026/09/10\n"


# =====================================================================
# 引擎 to_dict（服务层序列化契约：Decimal 一律转字符串，绝不走 float）
# =====================================================================

class TestToDict:
    def test_piece_record_full(self):
        r = engine.PieceRecord(process="冲压", name="王建国", qty="100",
                               unit_price="0.35", defect="1", date="2026-09-10")
        d = r.to_dict()
        assert d == {"process": "冲压", "name": "王建国", "qty": "100",
                     "unit_price": "0.35", "defect": "1", "date": "2026-09-10"}
        assert all(not isinstance(v, D) for v in d.values())
        assert json.dumps(d, ensure_ascii=False)

    def test_piece_record_defaults(self):
        r = engine.PieceRecord(process="p", name="n", qty=1, unit_price=2)
        d = r.to_dict()
        assert d["defect"] == "0"
        assert d["date"] is None

    def test_individual_result_shape(self):
        res = engine.individual_piecepay(
            [engine.PieceRecord.from_dict(r) for r in (ROW_WANG1, ROW_WANG2, ROW_LI)])
        d = res.to_dict()
        assert set(d) == {"lines", "by_person", "total_amount", "total_defect"}
        assert d["total_amount"] == str(res.total_amount)
        assert d["total_defect"] == str(res.total_defect)
        assert d["lines"][0]["amount"] == str(res.lines[0].amount)
        assert d["by_person"][0]["amount"] == str(res.by_person[0].amount)
        assert json.dumps(d, ensure_ascii=False)

    def test_team_result_shape(self):
        res = engine.team_piecepay("100", [
            engine.TeamMember("赵一", 1), engine.TeamMember("钱二", 1), engine.TeamMember("孙三", 1)])
        d = res.to_dict()
        assert set(d) == {"team_total", "lines", "residual", "allocated_total"}
        assert d["team_total"] == str(res.team_total)
        assert d["residual"] == str(res.residual)
        assert d["allocated_total"] == str(res.allocated_total)
        assert set(d["lines"][0]) == {"name", "qty", "base_amount",
                                      "adjust_amount", "amount", "remark"}
        assert json.dumps(d, ensure_ascii=False)

    def test_payslip_shape(self):
        d = engine.payslip("8000").to_dict()
        assert d == {"gross": "8000.00", "tax": "90.00", "net": "7910.00"}

    def test_team_member_shape(self):
        d = engine.TeamMember("赵一", 3).to_dict()
        assert d == {"name": "赵一", "qty": "3"}


# =====================================================================
# GET /api/health
# =====================================================================

class TestHealth:
    def test_ok(self, srv):
        status, data = Client(srv).json("GET", "/api/health")
        assert status == 200
        assert data["ok"] is True

    def test_query_string_ignored(self, srv):
        status, data = Client(srv).json("GET", "/api/health?probe=1")
        assert status == 200
        assert data["ok"] is True

    def test_post_to_get_route_is_404(self, srv):
        status, data = Client(srv).json("POST", "/api/health", {"x": 1})
        assert status == 404
        assert "没有这个接口" in data["error"]


# =====================================================================
# POST /api/ingest/csv
# =====================================================================

class TestIngestCsv:
    def test_normal_chinese_headers(self, srv):
        status, data = Client(srv).text("POST", "/api/ingest/csv", CSV_OK, "text/csv")
        assert status == 200
        assert data["count"] == 2
        r0 = data["records"][0]
        assert r0 == {"process": "冲压", "name": "王建国", "qty": "100",
                      "unit_price": "0.35", "defect": "1", "date": "2026-09-10"}
        assert data["records"][1]["defect"] == "0"

    def test_english_headers(self, srv):
        csv_text = ("process,name,qty,unit_price,defect,date\n"
                    "punch,Wang,10,0.5,1,2026-09-01")
        status, data = Client(srv).text("POST", "/api/ingest/csv", csv_text, "text/csv")
        assert status == 200
        assert data["count"] == 1
        assert data["records"][0]["qty"] == "10"
        assert data["records"][0]["date"] == "2026-09-01"

    def test_defect_and_date_columns_optional(self, srv):
        csv_text = "工序,姓名,数量,单价\n冲压,王建国,100,0.35"
        status, data = Client(srv).text("POST", "/api/ingest/csv", csv_text, "text/csv")
        assert status == 200
        assert data["records"][0]["defect"] == "0"
        assert data["records"][0]["date"] is None

    def test_bom_and_crlf_tolerated(self, srv):
        csv_text = "\ufeff" + CSV_OK
        status, data = Client(srv).text("POST", "/api/ingest/csv", csv_text, "text/csv")
        assert status == 200
        assert data["count"] == 2

    def test_json_envelope(self, srv):
        status, data = Client(srv).json("POST", "/api/ingest/csv", {"csv": CSV_OK})
        assert status == 200
        assert data["count"] == 2

    def test_json_envelope_without_csv_field(self, srv):
        status, data = Client(srv).json("POST", "/api/ingest/csv", {"content": "工序,姓名"})
        assert status == 400
        assert "csv" in data["error"]

    def test_missing_required_column(self, srv):
        csv_text = "工序,姓名,数量\r\n冲压,王建国,100"
        status, data = Client(srv).text("POST", "/api/ingest/csv", csv_text, "text/csv")
        assert status == 400
        assert "单价" in data["error"]

    def test_bad_number_has_line_number(self, srv):
        status, data = Client(srv).text("POST", "/api/ingest/csv", CSV_BAD_NUM, "text/csv")
        assert status == 400
        assert "第 3 行" in data["error"]
        assert "数量" in data["error"]

    def test_negative_qty_has_line_number(self, srv):
        status, data = Client(srv).text("POST", "/api/ingest/csv", CSV_NEG, "text/csv")
        assert status == 400
        assert "第 2 行" in data["error"]
        assert "不能为负" in data["error"]

    def test_defect_over_qty(self, srv):
        status, data = Client(srv).text("POST", "/api/ingest/csv", CSV_DEFECT_OVER, "text/csv")
        assert status == 400
        assert "第 2 行" in data["error"]
        assert "超过" in data["error"]

    def test_bad_date_has_line_number(self, srv):
        status, data = Client(srv).text("POST", "/api/ingest/csv", CSV_BAD_DATE, "text/csv")
        assert status == 400
        assert "第 2 行" in data["error"]
        assert "日期" in data["error"]

    def test_header_only_rejected(self, srv):
        status, data = Client(srv).text("POST", "/api/ingest/csv", "工序,姓名,数量,单价\n", "text/csv")
        assert status == 400
        assert "数据行" in data["error"]

    def test_blank_content_rejected(self, srv):
        status, data = Client(srv).text("POST", "/api/ingest/csv", "   \n  ", "text/csv")
        assert status == 400
        assert "空" in data["error"]

    def test_empty_body_rejected(self, srv):
        client = Client(srv)
        status, raw, _ = client.request("POST", "/api/ingest/csv", body=None, ctype="text/csv")
        assert status == 400
        assert "空" in json.loads(raw.decode("utf-8"))["error"]

    def test_non_utf8_rejected_with_hint(self, srv):
        client = Client(srv)
        status, raw, _ = client.request("POST", "/api/ingest/csv",
                                        body="工序,姓名".encode("gbk"), ctype="text/csv")
        assert status == 400
        assert "UTF-8" in json.loads(raw.decode("utf-8"))["error"]


# =====================================================================
# POST /api/settle
# =====================================================================

class TestSettle:
    def test_individual_exact_values(self, srv):
        status, data = Client(srv).json("POST", "/api/settle",
                                        {"rows": [ROW_WANG1, ROW_WANG2, ROW_LI],
                                         "mode": "individual"})
        assert status == 200
        # 100×0.35=35.00；(50-2)×0.35=16.80；(80-3)×0.5=38.50 → 合计 90.30
        assert data["total_amount"] == "90.30"
        assert data["total_defect"] == "5"
        assert len(data["lines"]) == 3
        assert data["by_person"][0]["name"] == "王建国"
        assert data["by_person"][0]["qualified_qty"] == "148"
        assert data["by_person"][0]["amount"] == "51.80"
        assert data["by_person"][1]["amount"] == "38.50"

    def test_individual_matches_engine(self, srv):
        rows = [ROW_WANG1, ROW_WANG2, ROW_LI]
        status, data = Client(srv).json("POST", "/api/settle",
                                        {"rows": rows, "mode": "individual"})
        assert status == 200
        expected = engine.individual_piecepay(
            [engine.PieceRecord.from_dict(r) for r in rows]).to_dict()
        assert data == expected

    def test_team_residual_and_invariant(self, srv):
        rows = [{"name": "赵一", "qty": 1}, {"name": "钱二", "qty": 1}, {"name": "孙三", "qty": 1}]
        status, data = Client(srv).json("POST", "/api/settle",
                                        {"rows": rows, "mode": "team", "team_total": "100"})
        assert status == 200
        assert data["team_total"] == "100.00"
        # 100/3 → 33.33×3=99.99，尾差 0.01 挂数量并列第一的赵一
        assert data["residual"] == "0.01"
        assert data["allocated_total"] == "100.00"
        assert data["lines"][0]["name"] == "赵一"
        assert data["lines"][0]["adjust_amount"] == "0.01"
        assert data["lines"][0]["amount"] == "33.34"
        assert data["lines"][0]["remark"] == "尾差调整"
        assert data["lines"][1]["remark"] is None
        total = sum(D(line["amount"]) for line in data["lines"])
        assert total == D(data["team_total"]) == D("100.00")

    def test_team_proportional_invariant(self, srv):
        rows = [{"name": "赵一", "qty": 300}, {"name": "钱二", "qty": 200}, {"name": "孙三", "qty": 100}]
        status, data = Client(srv).json("POST", "/api/settle",
                                        {"rows": rows, "mode": "team", "team_total": "10000.00"})
        assert status == 200
        assert D(data["lines"][0]["base_amount"]) == D("5000.00")
        assert D(data["lines"][1]["base_amount"]) == D("3333.33")
        assert D(data["lines"][2]["base_amount"]) == D("1666.67")
        total = sum(D(line["amount"]) for line in data["lines"])
        assert total == D(data["team_total"]) == D("10000.00")
        assert data["allocated_total"] == data["team_total"]

    def test_mode_missing(self, srv):
        status, data = Client(srv).json("POST", "/api/settle", {"rows": [ROW_WANG1]})
        assert status == 400
        assert "mode" in data["error"]

    def test_mode_invalid(self, srv):
        status, data = Client(srv).json("POST", "/api/settle",
                                        {"rows": [ROW_WANG1], "mode": "monthly"})
        assert status == 400
        assert "individual" in data["error"] and "team" in data["error"]

    def test_team_without_team_total(self, srv):
        status, data = Client(srv).json("POST", "/api/settle",
                                        {"rows": [{"name": "赵一", "qty": 1}], "mode": "team"})
        assert status == 400
        assert "team_total" in data["error"]

    def test_team_negative_total(self, srv):
        body = {"rows": [{"name": "赵一", "qty": 1}], "mode": "team", "team_total": -1}
        status, data = Client(srv).json("POST", "/api/settle", body)
        assert status == 400
        assert "不能为负" in data["error"]

    def test_team_member_negative_qty(self, srv):
        body = {"rows": [{"name": "赵一", "qty": -1}], "mode": "team", "team_total": "100"}
        status, data = Client(srv).json("POST", "/api/settle", body)
        assert status == 400
        assert "不能为负" in data["error"]

    def test_individual_wrong_field_name(self, srv):
        rows = [{"工序": "冲压", "姓名": "王建国", "数量": 10, "单价": "0.5"}]
        status, data = Client(srv).json("POST", "/api/settle", {"rows": rows, "mode": "individual"})
        assert status == 400
        assert "未知字段" in data["error"]
        assert "字段名写错" in data["error"]

    def test_rows_empty(self, srv):
        status, data = Client(srv).json("POST", "/api/settle", {"rows": [], "mode": "individual"})
        assert status == 400
        assert "rows" in data["error"]

    def test_rows_not_list(self, srv):
        status, data = Client(srv).json("POST", "/api/settle",
                                        {"rows": {"name": "x"}, "mode": "individual"})
        assert status == 400
        assert "rows" in data["error"]

    def test_row_not_dict(self, srv):
        status, data = Client(srv).json("POST", "/api/settle",
                                        {"rows": [["冲压", "王建国"]], "mode": "individual"})
        assert status == 400
        assert "dict" in data["error"]

    def test_bad_json_body(self, srv):
        client = Client(srv)
        status, raw, _ = client.request("POST", "/api/settle", body=b"{oops",
                                        ctype="application/json")
        assert status == 400
        assert "JSON" in json.loads(raw.decode("utf-8"))["error"]


# =====================================================================
# GET /api/payslip
# =====================================================================

class TestPayslip:
    def test_boundary_8000_exact(self, srv):
        status, data = Client(srv).json("GET", "/api/payslip?gross=8000")
        assert status == 200
        # 8000-5000=3000 落在 3% 档边界 → 税 90.00
        assert data["gross"] == "8000.00"
        assert data["tax"] == "90.00"
        assert data["net"] == "7910.00"

    def test_matches_engine(self, srv):
        status, data = Client(srv).json("GET", "/api/payslip?gross=12345.67")
        assert status == 200
        expected = engine.payslip("12345.67")
        assert data["gross"] == str(expected.gross)
        assert data["tax"] == str(expected.tax)
        assert data["net"] == str(expected.net)

    def test_tax_note_marks_demo_caliber(self, srv):
        status, data = Client(srv).json("GET", "/api/payslip?gross=8000")
        assert status == 200
        assert "演示" in data["tax_note"]
        assert "不可用于真实申报" in data["tax_note"]

    def test_gross_missing(self, srv):
        status, data = Client(srv).json("GET", "/api/payslip")
        assert status == 400
        assert "gross" in data["error"]

    def test_gross_garbage(self, srv):
        status, data = Client(srv).json("GET", "/api/payslip?gross=abc")
        assert status == 400
        assert "数值" in data["error"]

    def test_gross_negative(self, srv):
        status, data = Client(srv).json("GET", "/api/payslip?gross=-1")
        assert status == 400
        assert "不能为负" in data["error"]


# =====================================================================
# GET / 静态服务与路径穿越
# =====================================================================

class TestStatic:
    def test_root_serves_index_html(self, srv):
        status, data, ctype = Client(srv).request("GET", "/")
        assert status == 200
        assert ctype is not None and ctype.startswith("text/html")
        assert len(data) > 0

    def test_style_css(self, srv):
        status, _, ctype = Client(srv).request("GET", "/style.css")
        assert status == 200
        assert ctype is not None and ctype.startswith("text/css")

    def test_app_js(self, srv):
        status, _, ctype = Client(srv).request("GET", "/app.js")
        assert status == 200
        assert ctype is not None and "javascript" in ctype

    def test_missing_file_404_json(self, srv):
        status, raw, ctype = Client(srv).request("GET", "/no-such.file")
        assert status == 404
        assert ctype is not None and ctype.startswith("application/json")
        assert "error" in json.loads(raw.decode("utf-8"))

    def test_unknown_api_404(self, srv):
        status, data = Client(srv).json("GET", "/api/nope")
        assert status == 404
        assert "没有这个接口" in data["error"]

    @pytest.mark.parametrize("bad_path", [
        b"/../server.py",
        b"/..%2fserver.py",
        b"/%2e%2e/core/engine.py",
        b"/web/../core/engine.py",
        b"/./../../etc/passwd",
    ])
    def test_path_traversal_blocked(self, srv, bad_path):
        head, body = send_raw(
            srv, b"GET " + bad_path + b" HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 404")
        assert b"error" in body


# =====================================================================
# 成熟模式回归：keep-alive 安全 / 限流 / 411 / 500 兜底 / 日志净化
# =====================================================================

class TestKeepAliveSafety:
    def test_get_with_body_then_reuse(self, srv):
        """GET 本不该带 body；带了必须被收掉，否则残留字节会被当下一请求的请求行（串包）。"""
        sock = socket.create_connection(srv, timeout=3)
        try:
            sock.sendall(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                         b"Content-Length: 5\r\n\r\nhello")
            head1, body1 = read_response(sock)
            assert head1.startswith(b"HTTP/1.1 200")
            assert b"Content-Length:" in head1
            assert b'"ok":true' in body1
            sock.sendall(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            head2, body2 = read_response(sock)
            assert head2.startswith(b"HTTP/1.1 200")
            assert b'"ok":true' in body2
        finally:
            sock.close()

    def test_bad_json_then_reuse_same_connection(self, srv):
        conn = http.client.HTTPConnection(srv[0], srv[1], timeout=5)
        try:
            conn.request("POST", "/api/settle", body=b"{oops",
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            assert resp.status == 400
            resp.read()
            conn.request("GET", "/api/health")
            resp2 = conn.getresponse()
            assert resp2.status == 200
            assert json.loads(resp2.read().decode("utf-8"))["ok"] is True
        finally:
            conn.close()

    def test_unknown_post_route_drains_and_reuses(self, srv):
        conn = http.client.HTTPConnection(srv[0], srv[1], timeout=5)
        try:
            conn.request("POST", "/api/nope", body=b"abc",
                         headers={"Content-Type": "text/plain"})
            resp = conn.getresponse()
            assert resp.status == 404
            resp.read()
            conn.request("GET", "/api/health")
            resp2 = conn.getresponse()
            assert resp2.status == 200
            resp2.read()  # 收干净再关，否则客户端 RST 会砸出服务端噪音
        finally:
            conn.close()

    def test_chunked_body_rejected_411(self, srv):
        head, body = send_raw(
            srv, b"POST /api/ingest/csv HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                 b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 411")
        assert b"error" in body

    def test_body_over_limit_413_and_close(self, srv, monkeypatch):
        monkeypatch.setattr(server, "MAX_BODY", 100)
        sock = socket.create_connection(srv, timeout=3)
        try:
            sock.sendall(b"POST /api/ingest/csv HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                         b"Content-Type: text/csv\r\nContent-Length: 101\r\n\r\n" + b"x" * 101)
            head, body = read_response(sock)
            assert head.startswith(b"HTTP/1.1 413")
            assert b"error" in body
            try:
                tail = sock.recv(64)
            except ConnectionResetError:
                tail = b""
            assert tail == b""  # 超限收线，连接必须关死
        finally:
            sock.close()


class TestRobustness:
    def test_engine_crash_maps_500_and_server_survives(self, srv, monkeypatch):
        def crash(records):
            raise RuntimeError("引擎内部炸了")

        monkeypatch.setattr(engine, "individual_piecepay", crash)
        status, data = Client(srv).json("POST", "/api/settle",
                                        {"rows": [ROW_WANG1], "mode": "individual"})
        assert status == 500
        assert "内部错误" in data["error"]
        # F2③：500 话术固定，不回显异常内容（防内部信息泄露）
        assert "引擎内部炸了" not in data["error"]
        status2, data2 = Client(srv).json("GET", "/api/health")
        assert status2 == 200
        assert data2["ok"] is True  # 服务线程没被砸死

    def test_access_log_control_chars_escaped(self, srv, caplog):
        with caplog.at_level(logging.INFO, logger="sj.access"):
            send_raw(srv, b"GET /a\x01b HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        assert "\\x01" in caplog.text      # 控制字符已转义成字面 \x01
        assert "\x01" not in caplog.text   # 日志里不许残留原始控制字符

    def test_safe_rollback_calls_rollback(self):
        class RecordingConn:
            def __init__(self):
                self.rolled = False

            def rollback(self):
                self.rolled = True

        conn = RecordingConn()
        server._TLS.conn = conn
        try:
            server._safe_rollback()
        finally:
            del server._TLS.conn
        assert conn.rolled is True

    def test_safe_rollback_swallows_conn_explosion(self):
        class ExplodingConn:
            def rollback(self):
                raise RuntimeError("连回滚都炸")

        server._TLS.conn = ExplodingConn()
        try:
            survived = True
            try:
                server._safe_rollback()
            except Exception:
                survived = False
        finally:
            del server._TLS.conn
        assert survived is True  # 回滚失败绝不遮原始异常、绝不炸调用方


# =====================================================================
# F2：数值幅度/位数上限 → 400 人话（不再是 InvalidOperation→500）
# =====================================================================

class TestNumericLimits:
    def test_payslip_gross_1e30_is_400(self, srv):
        status, data = Client(srv).json("GET", "/api/payslip?gross=1e30")
        assert status == 400
        assert "太大" in data["error"]

    def test_payslip_gross_1e999_is_400(self, srv):
        status, data = Client(srv).json("GET", "/api/payslip?gross=1e999")
        assert status == 400
        assert "太大" in data["error"]

    def test_settle_qty_1e30_string_is_400(self, srv):
        rows = [dict(ROW_WANG1, qty="1e30")]
        status, data = Client(srv).json("POST", "/api/settle",
                                        {"rows": rows, "mode": "individual"})
        assert status == 400
        assert "太大" in data["error"]

    def test_settle_qty_1e30_float_is_400(self, srv):
        rows = [dict(ROW_WANG1, qty=1e30)]
        status, data = Client(srv).json("POST", "/api/settle",
                                        {"rows": rows, "mode": "individual"})
        assert status == 400
        assert "太大" in data["error"]

    def test_settle_team_total_1e400_is_400(self, srv):
        body = {"rows": [{"name": "赵一", "qty": 1}], "mode": "team",
                "team_total": "1e400"}
        status, data = Client(srv).json("POST", "/api/settle", body)
        assert status == 400
        assert "太大" in data["error"]


# =====================================================================
# F10：errorhandler 补 TimeoutError / socket.timeout / DecimalException
# =====================================================================

class TestErrorhandlerEdges:
    class _FakeHandler:
        def __init__(self):
            self.captured = None

        def _error(self, status, msg, code=None):   # code：收尾三小件错误码（增量参数）
            self.captured = (status, msg, code)

    def test_socket_timeout_swallowed(self):
        f = self._FakeHandler()

        def boom():
            raise socket.timeout("读请求超时")

        assert server.errorhandler(f, boom) is None   # 静默返回，不写响应
        assert f.captured is None

    def test_timeout_error_swallowed(self):
        f = self._FakeHandler()

        def boom():
            raise TimeoutError("超时")

        assert server.errorhandler(f, boom) is None
        assert f.captured is None

    def test_decimal_exception_maps_400(self):
        import decimal
        f = self._FakeHandler()

        def boom():
            raise decimal.InvalidOperation("取整越界")

        server.errorhandler(f, boom)
        assert f.captured[0] == 400
        assert "数字" in f.captured[1]
        assert f.captured[2] == "NUMERIC_OVERFLOW"   # 收尾三小件：机器码同步带上


# =====================================================================
# F11：日志净化公共函数（扩 DEL）+ payslip gross 脱敏
# =====================================================================

class TestLogSanitize:
    def test_unit_control_and_del_escaped(self):
        assert server._sanitize_log("a\x01b\x7fc") == "a\\x01b\\x7fc"

    def test_unit_gross_redacted(self):
        s = server._sanitize_log("GET /api/payslip?gross=12345.67 HTTP/1.1")
        assert "gross=*" in s
        assert "12345.67" not in s

    def test_unit_gross_with_following_param(self):
        s = server._sanitize_log("/api/payslip?gross=99&x=1")
        assert s == "/api/payslip?gross=*&x=1"

    def test_access_log_escapes_del_and_redacts_gross(self, srv, caplog):
        # DEL 放在路径段（不在 gross 值里），两个净化动作各验各的
        with caplog.at_level(logging.INFO, logger="sj.access"):
            send_raw(srv, b"GET /x\x7fy?gross=8000 HTTP/1.1\r\n"
                          b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n")
        assert "\\x7f" in caplog.text          # DEL 也转义
        assert "gross=*" in caplog.text        # 收入参数脱敏
        assert "gross=8000" not in caplog.text


# =====================================================================
# F12：Host 白名单（只认本机），否则 403 收线
# =====================================================================

class TestHostAllowlist:
    def test_unit_host_allowed(self):
        assert server._host_allowed("127.0.0.1")
        assert server._host_allowed("127.0.0.1:8770")
        assert server._host_allowed("LOCALHOST:99")
        assert server._host_allowed("[::1]:1")
        assert not server._host_allowed("")
        assert not server._host_allowed(None)
        assert not server._host_allowed("evil.com")
        assert not server._host_allowed("127.0.0.1.evil.com")

    def test_foreign_host_get_403_and_closed(self, srv):
        sock = socket.create_connection(srv, timeout=3)
        try:
            sock.sendall(b"GET /api/health HTTP/1.1\r\nHost: evil.example.com\r\n\r\n")
            head, body = read_response(sock)
            assert head.startswith(b"HTTP/1.1 403")
            assert b"error" in body
            try:
                tail = sock.recv(64)
            except ConnectionResetError:
                tail = b""
            assert tail == b""   # 收线：不许残留连接
        finally:
            sock.close()

    def test_missing_host_403(self, srv):
        head, body = send_raw(srv, b"GET /api/health HTTP/1.1\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 403")

    def test_foreign_host_post_403(self, srv):
        head, body = send_raw(
            srv, b"POST /api/settle HTTP/1.1\r\nHost: evil.com\r\n"
                 b"Content-Length: 0\r\nConnection: close\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 403")

    def test_localhost_with_port_allowed(self, srv):
        head, body = send_raw(
            srv, b"GET /api/health HTTP/1.1\r\nHost: localhost:8770\r\nConnection: close\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200")
        assert b'"ok":true' in body

    def test_ipv6_loopback_literal_allowed(self, srv):
        head, _ = send_raw(
            srv, b"GET /api/health HTTP/1.1\r\nHost: [::1]:8770\r\nConnection: close\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200")


# =====================================================================
# F14：sys.frozen 时数据目录跟 exe 走，静态资源走解包目录
# =====================================================================

class TestFrozenPaths:
    def test_frozen_dirs_follow_exe_and_bundle(self, monkeypatch):
        import importlib
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", "/tmp/fakebin/suanjian")
        monkeypatch.setattr(sys, "_MEIPASS", "/tmp/fakebundle", raising=False)
        try:
            importlib.reload(server)
            assert server.BASE_DIR == "/tmp/fakebin"                       # 日志/数据跟 exe
            assert server.WEB_DIR == os.path.join("/tmp/fakebundle", "web")  # web/ 走解包目录
        finally:
            monkeypatch.undo()
            importlib.reload(server)
        assert server.BASE_DIR == os.path.dirname(os.path.abspath(server.__file__))


# =====================================================================
# F22：负 Content-Length → 400 并收线（防残留字节串包）
# =====================================================================

class TestNegativeContentLength:
    def test_negative_cl_400_and_connection_closed(self, srv):
        sock = socket.create_connection(srv, timeout=3)
        try:
            sock.sendall(b"POST /api/ingest/csv HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                         b"Content-Type: text/csv\r\nContent-Length: -5\r\n\r\n"
                         b"helloGET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            head, body = read_response(sock)
            assert head.startswith(b"HTTP/1.1 400")
            assert b"error" in body
            try:
                tail = sock.recv(256)
            except ConnectionResetError:
                tail = b""
            assert tail == b""   # 收线：后面的注入请求行不许被处理
        finally:
            sock.close()
