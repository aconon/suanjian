# -*- coding: utf-8 -*-
"""任务：工资条 Excel 导出（TDD 先行）——GET /api/export/payslips。

契约 = docs/任务单/08-工资条导出与趋势报表.md：
- GET /api/export/payslips[?period=YYYY-MM]：SpreadsheetML 2003 XML（.xls，
  Excel/WPS 直接打开，零 pip 依赖），两个 sheet：
  · 「汇总」= 全员工资条（姓名/计件工资/个税（演示口径）/实发工资 + 合计行 + 口径备注）
  · 「明细」= 该期每条结算行（模式/工序/姓名/数量/报废/合格数量/单价/金额/备注 + 合计行）
- 金额一律 ss:Type="String" 精确字符串（Decimal 全链，绝不走 float/Number）
- period 缺省 = 最近一个已归期；期里没有核算记录 → 404 人话；格式坏 → 400
- 响应带 Content-Type/Content-Disposition（attachment；ASCII 回退 + filename* UTF-8）
- 口令门照进（/api/*）；导出留痕 audit（action=export_payslips，target=期）

红线：金额断言用 Decimal 精确相等；XML 用 ElementTree 解析回来对（不裸 grep 字符串）。
"""
import http.client
import json
import socket
import threading
import xml.etree.ElementTree as ET
from decimal import Decimal as D
from urllib.parse import unquote

import pytest

import server
from core import db

SS = "{urn:schemas-microsoft-com:office:spreadsheet}"


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


def http_get(addr, path, x_code="__unset__"):
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=5)
    try:
        headers = {}
        if x_code != "__unset__":
            headers["X-Code"] = x_code
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp, data
    finally:
        conn.close()


def settle(addr, body):
    """打 /api/settle（辅助造底账）。"""
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=5)
    try:
        conn.request("POST", "/api/settle",
                     body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def sheet_rows(xml_bytes, name):
    """SpreadsheetML → 某 sheet 的行列文本（None = 空单元格）。"""
    root = ET.fromstring(xml_bytes)
    for ws in root.findall(SS + "Worksheet"):
        if ws.get(SS + "Name") == name:
            table = ws.find(SS + "Table")
            rows = []
            for row in table.findall(SS + "Row"):
                cells = []
                for cell in row.findall(SS + "Cell"):
                    data = cell.find(SS + "Data")
                    cells.append(data.text if data is not None else None)
                rows.append(cells)
            return rows
    raise AssertionError(f"没有名为 {name} 的 sheet")


def write_config(home, obj):
    (home / "config.json").write_text(
        json.dumps(obj, ensure_ascii=False), encoding="utf-8")


# 个人计件底账：王建国 (100-1)×0.35=34.65；李秀英 80×0.5=40.00 → 合计 74.65
ROWS_A = [
    {"process": "冲压", "name": "王建国", "qty": "100",
     "unit_price": "0.35", "defect": "1", "date": "2026-09-10"},
    {"process": "焊接", "name": "李秀英", "qty": "80",
     "unit_price": "0.5", "defect": "0", "date": "2026-09-11"},
]

# 大额行：赵大富 8000×1 → 8000.00（个税演示口径 90.00）
ROWS_BIG = [
    {"process": "装配", "name": "赵大富", "qty": "8000",
     "unit_price": "1", "defect": "0", "date": "2026-09-12"},
]


def make_period_data(addr):
    """两期底账：2026-08（早）与 2026-09（晚，个人+班组混合）。"""
    settle(addr, {"mode": "individual", "rows": [
        {"process": "冲压", "name": "王建国", "qty": "10",
         "unit_price": "0.35", "defect": "0", "date": "2026-08-20"}]})
    settle(addr, {"mode": "individual", "rows": ROWS_A})
    settle(addr, {"mode": "individual", "rows": ROWS_BIG})
    settle(addr, {"mode": "team", "team_total": "100",
                  "period": "2026-09",
                  "rows": [{"name": "王建国", "qty": "1"},
                           {"name": "钱二", "qty": "1"},
                           {"name": "孙三", "qty": "1"}]})


# =====================================================================
# 基本响应与错误路径
# =====================================================================

class TestExportBasics:
    def test_no_settlements_404(self, srv):
        resp, data = http_get(srv, "/api/export/payslips")
        assert resp.status == 404
        assert "核算" in json.loads(data.decode("utf-8"))["error"]

    def test_bad_period_format_400(self, srv):
        make_period_data(srv)
        resp, data = http_get(srv, "/api/export/payslips?period=2026-9")
        assert resp.status == 400
        assert "YYYY-MM" in json.loads(data.decode("utf-8"))["error"]

    def test_unknown_period_404(self, srv):
        make_period_data(srv)
        resp, data = http_get(srv, "/api/export/payslips?period=2025-01")
        assert resp.status == 404
        assert "2025-01" in json.loads(data.decode("utf-8"))["error"]

    def test_unlabeled_only_404(self, srv):
        # 未归期（缺日期）结算不进导出：只有未归期 → 404 人话
        settle(srv, {"mode": "team", "team_total": "10",
                     "rows": [{"name": "x", "qty": "1"}]})
        resp, data = http_get(srv, "/api/export/payslips")
        assert resp.status == 404
        assert "归期" in json.loads(data.decode("utf-8"))["error"]

    def test_default_period_is_latest(self, srv):
        make_period_data(srv)
        resp, data = http_get(srv, "/api/export/payslips")
        assert resp.status == 200
        disp = resp.getheader("Content-Disposition") or ""
        assert "2026-09" in disp   # 缺省取最近一期（2026-09 晚于 2026-08）

    def test_explicit_period_earlier(self, srv):
        make_period_data(srv)
        resp, data = http_get(srv, "/api/export/payslips?period=2026-08")
        assert resp.status == 200
        rows = sheet_rows(data, "汇总")
        assert [r[0] for r in rows[1:2]] == ["王建国"]   # 08 期只有王建国
        assert rows[1][1] == "3.50"                      # 10×0.35

    def test_content_headers(self, srv):
        make_period_data(srv)
        resp, data = http_get(srv, "/api/export/payslips")
        assert resp.status == 200
        ctype = resp.getheader("Content-Type") or ""
        assert "excel" in ctype
        disp = resp.getheader("Content-Disposition") or ""
        assert "attachment" in disp
        assert 'filename="suanjian-payslips-2026-09.xls"' in disp   # ASCII 回退
        assert "filename*=UTF-8''" in disp                          # 中文真名
        # 中文文件名可还原：算件-工资条-2026-09.xls
        star = disp.split("filename*=UTF-8''", 1)[1]
        assert unquote(star) == "算件-工资条-2026-09.xls"
        assert resp.getheader("Content-Length") == str(len(data))   # keep-alive 定界

    def test_two_worksheets_named(self, srv):
        make_period_data(srv)
        resp, data = http_get(srv, "/api/export/payslips")
        root = ET.fromstring(data)
        names = [ws.get(SS + "Name") for ws in root.findall(SS + "Worksheet")]
        assert names == ["汇总", "明细"]
        assert b"mso-application" in data[:300]   # Excel 直开的 PI 在头部


# =====================================================================
# 汇总 sheet：按人合计 + 个税（演示口径）+ 不变量
# =====================================================================

class TestExportSummary:
    def test_person_sums_across_individual_and_team(self, srv):
        make_period_data(srv)
        _, data = http_get(srv, "/api/export/payslips?period=2026-09")
        rows = sheet_rows(data, "汇总")
        assert rows[0][:4] == ["姓名", "计件工资", "个税（演示口径）", "实发工资"]
        by_name = {r[0]: r for r in rows[1:] if r[0] and r[0] != "合计"}
        # 王建国 = 个人 34.65 + 班组分摊（尾差承接）33.34 = 67.99
        assert D(by_name["王建国"][1]) == D("67.99")
        assert by_name["钱二"][1] == "33.33"
        assert by_name["孙三"][1] == "33.33"
        assert by_name["李秀英"][1] == "40.00"

    def test_tax_demo_caliber_exact(self, srv):
        make_period_data(srv)
        _, data = http_get(srv, "/api/export/payslips?period=2026-09")
        rows = sheet_rows(data, "汇总")
        by_name = {r[0]: r for r in rows[1:] if r[0] and r[0] != "合计"}
        # 赵大富 8000.00 → 演示个税 90.00，实发 7910.00
        assert by_name["赵大富"][1] == "8000.00"
        assert by_name["赵大富"][2] == "90.00"
        assert by_name["赵大富"][3] == "7910.00"
        # 低收入三人个税为 0
        assert by_name["李秀英"][2] == "0.00"
        assert D(by_name["李秀英"][3]) == D(by_name["李秀英"][1])

    def test_total_row_and_invariants(self, srv):
        make_period_data(srv)
        _, data = http_get(srv, "/api/export/payslips?period=2026-09")
        rows = sheet_rows(data, "汇总")
        total = [r for r in rows if r and r[0] == "合计"][0]
        persons = [r for r in rows[1:] if r[0] not in ("合计", None)
                   and not (r[0] or "").startswith("备注")]
        assert D(total[1]) == sum(D(r[1]) for r in persons)
        assert D(total[2]) == sum(D(r[2]) for r in persons)
        assert D(total[3]) == sum(D(r[3]) for r in persons)
        # 恒等式：Σ实发 == Σ计件 − Σ个税（Decimal 精确）
        assert D(total[3]) == D(total[1]) - D(total[2])

    def test_tax_note_row_present(self, srv):
        make_period_data(srv)
        _, data = http_get(srv, "/api/export/payslips?period=2026-09")
        rows = sheet_rows(data, "汇总")
        note = [r for r in rows if r and (r[0] or "").startswith("备注")]
        assert note, "汇总 sheet 末尾要有演示口径备注行"
        assert "不可用于真实申报" in note[0][0]


# =====================================================================
# 明细 sheet：逐行原值 + 不变量
# =====================================================================

class TestExportDetail:
    def test_detail_rows_match_engine_lines(self, srv):
        make_period_data(srv)
        _, data = http_get(srv, "/api/export/payslips?period=2026-09")
        rows = sheet_rows(data, "明细")
        assert rows[0] == ["模式", "工序", "姓名", "数量", "报废",
                           "合格数量", "单价", "金额", "备注"]
        body = [r for r in rows[1:] if r[0] not in ("合计", None)]
        # 个人行：金额/数量与引擎 to_dict 字符串逐字相等
        wang = [r for r in body if r[2] == "王建国" and r[0] == "个人计件"][0]
        assert wang[1] == "冲压" and wang[3] == "100" and wang[4] == "1"
        assert wang[5] == "99" and wang[6] == "0.35" and wang[7] == "34.65"
        # 班组行：工序/报废/合格数量/单价留空，备注带尾差调整
        team = [r for r in body if r[0] == "班组计件"]
        assert len(team) == 3
        adj = [r for r in team if r[8] == "尾差调整"][0]
        assert adj[2] == "王建国" and adj[3] == "1" and D(adj[7]) == D("33.34")
        assert adj[4] is None and adj[5] is None and adj[6] is None

    def test_detail_total_equals_summary_total(self, srv):
        make_period_data(srv)
        _, data = http_get(srv, "/api/export/payslips?period=2026-09")
        detail = [r for r in sheet_rows(data, "明细") if r and r[0] == "合计"][0]
        summary = [r for r in sheet_rows(data, "汇总") if r and r[0] == "合计"][0]
        # Σ明细金额 == Σ汇总计件（同一期两本账必须对上）
        assert D(detail[7]) == D(summary[1])

    def test_money_cells_are_string_type(self, srv):
        make_period_data(srv)
        _, data = http_get(srv, "/api/export/payslips")
        root = ET.fromstring(data)
        types = set()
        for data_el in root.iter(SS + "Data"):
            types.add(data_el.get(SS + "Type"))
        assert types == {"String"}   # 全 String：金额绝不走 Number/float

    def test_xml_escapes_special_chars(self, srv):
        settle(srv, {"mode": "individual", "rows": [
            {"process": "冲压&下料", "name": "A<B>&C", "qty": "10",
             "unit_price": "0.35", "defect": "0", "date": "2026-09-10"}]})
        _, data = http_get(srv, "/api/export/payslips")
        rows = sheet_rows(data, "明细")
        special = [r for r in rows if r and r[2] == "A<B>&C"]
        assert special and special[0][1] == "冲压&下料"   # 解析回来原样


# =====================================================================
# 毒字符 / 公式注入（修复单 R5：F1 + F2 + 附）
# =====================================================================

class TestExportPoisonChars:
    """F1：XML 1.0 非法控制字符（\x00-\x08 \x0b \x0c \x0e-\x1f \x7f）必须在
    _xml_row 里剥掉（保留 \t\n\r），否则导出的 .xls 不是合法 XML，Excel
    打开即报「无法读取」。经真路径：settle 存毒名 → export 取文件。"""

    def test_control_chars_stripped_and_xml_parses(self, srv):
        settle(srv, {"mode": "individual", "rows": [
            {"process": "冲压", "name": "A\x0bB\x0cC\x1fD", "qty": "10",
             "unit_price": "0.35", "defect": "0", "date": "2026-09-10"},
            {"process": "焊接", "name": "E\x00F\x08G", "qty": "5",
             "unit_price": "1", "defect": "0", "date": "2026-09-11"},
        ]})
        _, data = http_get(srv, "/api/export/payslips?period=2026-09")
        root = ET.fromstring(data)          # 未剥毒字符 → ParseError（红）
        rows = sheet_rows(data, "明细")
        names = {r[2] for r in rows[1:] if r[2]}
        assert "ABCD" in names              # \x0b \x0c \x1f 已剥，正文原样
        assert "EFG" in names               # \x00 \x08 已剥
        for poison in ("\x0b", "\x0c", "\x1f", "\x00", "\x08", "\x7f"):
            assert poison.encode() not in data   # 全文件无残留毒字节

    def test_tab_newline_cr_preserved(self, srv):
        """正则只剥 XML 1.0 非法字符；\t \n \r 是合法字符，必须保留。
        注意：\r 在 XML 解析侧按规范归一为 \n，所以 \r 只断字节级保留
        （导出文件里有），\t \n 断 round-trip 原样。"""
        settle(srv, {"mode": "individual", "rows": [
            {"process": "冲\t压", "name": "王\t小\n明", "qty": "10",
             "unit_price": "0.35", "defect": "0", "date": "2026-09-10"},
            {"process": "焊接", "name": "李\r四", "qty": "5",
             "unit_price": "1", "defect": "0", "date": "2026-09-11"},
        ]})
        _, data = http_get(srv, "/api/export/payslips?period=2026-09")
        rows = sheet_rows(data, "明细")
        row = [r for r in rows if r and r[2] == "王\t小\n明"]
        assert row                          # \t\n 原样回到单元格
        assert row[0][1] == "冲\t压"
        assert b"\r" in data                # \r 字节仍在文件里（解析侧归一成 \n 是 XML 规范行为）
        assert "李\n四" in {r[2] for r in rows if r[2]}   # \r 解析后呈现为换行，不是被剥空

    def test_formula_injection_neutralized(self, srv):
        """附：名字带 =/+/-/@ 开头的公式载荷 → 全文档 0 处 ss:Formula，
        Data Type 恒 String，载荷按字面文本回到单元格（SpreadsheetML 里
        String 型 Data 不是公式，Excel 不会求值）。"""
        poison_names = [
            '=HYPERLINK("http://evil.example","点我")',
            "+1+1",
            "@SUM(A1:A9)*9",
            "-2+3+cmd|' /C calc'!A0",
        ]
        rows_payload = [
            {"process": "冲压", "name": n, "qty": "1",
             "unit_price": "1", "defect": "0", "date": "2026-09-10"}
            for n in poison_names
        ]
        settle(srv, {"mode": "individual", "rows": rows_payload})
        _, data = http_get(srv, "/api/export/payslips?period=2026-09")
        root = ET.fromstring(data)
        # 全文档 0 处公式：既无 ss:Formula 元素，也无 Cell 上的 ss:Formula 属性
        assert not root.findall(".//" + SS + "Formula")
        for cell in root.iter(SS + "Cell"):
            assert SS + "Formula" not in cell.attrib
        # Type 恒 String（公式注入的根治面：金额/文本一律字符串单元格）
        types = {d.get(SS + "Type") for d in root.iter(SS + "Data")}
        assert types == {"String"}
        # 载荷按字面文本回来（明细 sheet 第 3 列=姓名）
        detail_names = {r[2] for r in sheet_rows(data, "明细")[1:] if r[2]}
        for n in poison_names:
            assert n in detail_names
        # 汇总 sheet 同样按字面文本（by_person 合并口径）
        sum_names = {r[0] for r in sheet_rows(data, "汇总")[1:] if r[0]}
        for n in poison_names:
            assert n in sum_names


class TestSheetXmlHardening:
    """F2：ss:Name 过 _xml_escape（含双引号实体）——属性值裹在双引号里，
    名字里的英文双引号不转义即破 XML。单测直打 _sheet_xml。"""

    def _wrap(self, fragment):
        return ('<?xml version="1.0" encoding="UTF-8"?>'
                '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"'
                ' xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
                + fragment + "</Workbook>").encode("utf-8")

    def test_sheet_name_with_quote_escapes(self):
        bad = 'My"Sheet&<1>'
        doc = self._wrap(server._sheet_xml(bad, [["a", "b"]]))
        root = ET.fromstring(doc)           # 引号未转义 → ParseError（红）
        ws = root.find(SS + "Worksheet")
        assert ws.get(SS + "Name") == bad   # 转义后解析回原名字
        rows = [c for row in ws.find(SS + "Table") for c in row]
        texts = [(c.find(SS + "Data").text if c.find(SS + "Data") is not None
                  else None) for c in rows]
        assert texts == ["a", "b"]

    def test_sheet_name_normal_unchanged(self):
        doc = self._wrap(server._sheet_xml("汇总", [["a"]]))
        root = ET.fromstring(doc)
        assert root.find(SS + "Worksheet").get(SS + "Name") == "汇总"


# =====================================================================
# 门禁 + 留痕 + keep-alive
# =====================================================================

class TestExportGateAndAudit:
    def test_gate_401_without_code_header(self, srv, home):
        make_period_data(srv)
        write_config(home, {"code": "9527ab"})
        resp, data = http_get(srv, "/api/export/payslips")
        assert resp.status == 401
        assert "口令" in json.loads(data.decode("utf-8"))["error"]

    def test_gate_passes_with_code_header(self, srv, home):
        make_period_data(srv)
        write_config(home, {"code": "9527ab"})
        resp, data = http_get(srv, "/api/export/payslips", x_code="9527ab")
        assert resp.status == 200
        assert resp.getheader("Content-Disposition")

    def test_audit_trail_written(self, srv):
        make_period_data(srv)
        http_get(srv, "/api/export/payslips?period=2026-09")
        row = db.ensure().execute(
            "SELECT actor,action,target FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["action"] == "export_payslips"
        assert row["target"] == "2026-09"
        assert row["actor"] == "user"

    def test_keepalive_usable_after_download(self, srv):
        """二进制/附件响应也必须带准 Content-Length：同连接下一请求不串包。"""
        make_period_data(srv)
        conn = http.client.HTTPConnection(srv[0], srv[1], timeout=5)
        try:
            conn.request("GET", "/api/export/payslips")
            resp = conn.getresponse()
            body = resp.read()
            assert resp.status == 200
            assert int(resp.getheader("Content-Length")) == len(body)
            conn.request("GET", "/api/health")
            resp2 = conn.getresponse()
            assert resp2.status == 200
            assert b'"ok"' in resp2.read()
        finally:
            conn.close()
