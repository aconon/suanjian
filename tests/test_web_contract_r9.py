# -*- coding: utf-8 -*-
"""web/ 前端契约测试 · 修复单 R9（F1/F2/F3 + 前端侧 F4/F6/F7/F8）。

沿用 test_web_contract_v14.py 的三层防线：node --check 保语法、源码断言锁行为、
node 实跑 JS 函数对数（mergeImported 幂等 / periodKeyFromTickets 归期）。
背景见 docs/审查单/R9-全系统重扫.md：
- F1：CSV 确认导入只调 /api/ingest/csv（纯解析）从不落库 → 逐票 POST /api/tickets/save
- F2：mergeImported 同票号行级 push 追加 → 重复导入行翻倍 → 整票替换
- F3：team settle 从不传 period → 班组数据永不归期（趋势/对比/导出全瞎）→ 前端传期
- F6/F7：X-Code 百分号编码 + 429（口令锁定）让路口令门不进演示回落
- F8：settle / ingest/csv 吃 api() 默认 3s 超时 → 弱网被掐进演示回落换尺子 → 放宽
"""
import json
import subprocess
from pathlib import Path
from shutil import which

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
ROOT = Path(__file__).resolve().parent.parent


def _src(name):
    return (WEB / name).read_text(encoding="utf-8")


def _body(src, start, end):
    return src[src.index(start):src.index(end)]


def _node():
    if not (Path("/usr/bin/node").exists() or which("node")):
        pytest.skip("本机没有 node，跳过 JS 检查")
    return True


def _node_run(js):
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, "JS 跑不起来：%s" % r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestSyntax:
    def test_node_check_app_js(self):
        _node()
        r = subprocess.run(["node", "--check", str(WEB / "app.js")],
                           capture_output=True, text=True)
        assert r.returncode == 0, "app.js 语法错误：%s" % r.stderr


# =====================================================================
# F1：确认导入逐票落库（/api/tickets/save，与拍照入账同一条路）
# =====================================================================

class TestF1ConfirmImportSaves:
    def _fn(self):
        src = _src("app.js")
        return _body(src, "function confirmImport", "function readFile")

    def test_ingest_still_first(self):
        """解析仍先行（服务端权威校验/精确字符串），再落库。"""
        b = self._fn()
        assert "api('/ingest/csv'" in b

    def test_saves_each_ticket(self):
        """解析成功后逐票 POST /api/tickets/save（sheet_no 覆盖=幂等地基）。"""
        b = self._fn()
        assert "api('/tickets/save'" in b
        assert "sheet_no" in b

    def test_save_failure_is_error_not_fake_success(self):
        """存库失败：错误态如实上屏（可重试且不重复），不本地假成功、不演示回落。"""
        b = self._fn()
        assert "saveFail" in b and "mode: 'error'" in b   # 存库失败进错误态
        assert "不会重复" in b                             # 提示重试幂等（同票号覆盖）

    def test_offline_ingest_fallback_kept(self):
        """ingest 网络失败的演示回落保留（离线可用是产品底线）。"""
        b = self._fn()
        assert "markDemo()" in b

    def test_grouping_by_sheet_no(self):
        """落库前按票号分组（一批同票号行=一张完整工票），不是一行一存。"""
        b = self._fn()
        assert "byNo" in b and "sheet_no: r.no" in b


# =====================================================================
# F2：mergeImported 同票号=整票替换（node 实跑行为级验证）
# =====================================================================

class TestF2MergeIdempotent:
    def _merge_js(self, epilogue):
        src = _src("app.js")
        seg_a = _body(src, "function normalizeSheets", "/* ---------- 核算")
        seg_b = _body(src, "function mergeImported", "function startImportText")
        return ("var store = {tickets: {data: [], demoData: false}};\n"
                + seg_a + "\n" + seg_b + "\n" + epilogue)

    def test_double_import_no_row_duplication(self):
        """同一批行导两遍：票数不变、行数不翻倍（旧实现会 2 行变 4 行）。"""
        _node()
        rows = ("[{no:'SJ-1',workshop:'冲压',date:'2026-09-12',process:'冲压',"
                "name:'张三',qty:100,price:0.35,defect:1},"
                "{no:'SJ-1',workshop:'冲压',date:'2026-09-12',process:'裁切',"
                "name:'李四',qty:50,price:0.5,defect:0}]")
        out = _node_run(self._merge_js(
            "var r1 = mergeImported(%s);\n" % rows
            + "var r2 = mergeImported(%s);\n" % rows
            + "var dump = function (r) { return {added: r.added, replaced: r.replaced,"
              "sheets: r.sheets, rowsNow: store.tickets.data[0].rows.length}; };"
              "console.log(JSON.stringify({first: dump(r1), again: dump(r2)}));"))
        assert out["first"] == {"added": 1, "replaced": 0, "sheets": 1,
                                "rowsNow": 2}
        assert out["again"]["added"] == 0 and out["again"]["replaced"] == 1
        assert out["again"]["rowsNow"] == 2, "重复导入行翻倍（F2 回归）"

    def test_resave_same_no_replaces_whole_ticket(self):
        """同票号再导不同内容：整票替换（旧行不残留、车间/日期以新为准）。"""
        _node()
        first = ("[{no:'SJ-1',workshop:'冲压',date:'2026-09-12',process:'冲压',"
                 "name:'张三',qty:100,price:0.35,defect:1},"
                 "{no:'SJ-1',workshop:'冲压',date:'2026-09-12',process:'裁切',"
                 "name:'李四',qty:50,price:0.5,defect:0}]")
        second = ("[{no:'SJ-1',workshop:'装配',date:'2026-09-13',process:'装配',"
                  "name:'王五',qty:10,price:1,defect:0}]")
        out = _node_run(self._merge_js(
            "mergeImported(%s);\n" % first
            + "var r = mergeImported(%s);\n" % second
            + "var s = store.tickets.data[0];"
              "console.log(JSON.stringify({r: {added: r.added, replaced: r.replaced,"
              "sheets: r.sheets}, rowsNow: s.rows.length, ws: s.workshop, "
              "names: s.rows.map(function(x){return x.name;})}));"))
        assert out["r"]["replaced"] == 1 and out["r"]["added"] == 0
        assert out["rowsNow"] == 1, "同票号旧行必须被整票替换掉"
        assert out["ws"] == "装配"
        assert out["names"] == ["王五"]

    def test_new_ticket_appends_not_replaces(self):
        """不同票号照常新增，互不影响。"""
        _node()
        out = _node_run(self._merge_js(
            "mergeImported([{no:'A',workshop:'w',date:'2026-09-01',process:'p',"
            "name:'甲',qty:1,price:1,defect:0}]);\n"
            "var r = mergeImported([{no:'B',workshop:'w',date:'2026-09-02',"
            "process:'p',name:'乙',qty:2,price:1,defect:0}]);\n"
            "console.log(JSON.stringify({r: r, count: store.tickets.data.length}));"))
        assert out["r"]["added"] == 1 and out["r"]["sheets"] == 2


# =====================================================================
# F3：team settle 带 period（periodKeyFromTickets 前端归期）
# =====================================================================

class TestF3TeamPeriod:
    def test_run_calc_team_body_carries_period(self):
        src = _src("app.js")
        b = _body(src, "function runCalc", "/* ---------- CSV 解析与校验")
        assert "periodKeyFromTickets" in b
        assert "if (tp) body.period = tp;" in b   # 单期才传，跨期/无日期宁缺勿编

    def test_period_key_single_vs_mixed_vs_empty(self):
        """node 实跑：单期出 'YYYY-MM'、跨期/无日期出 ''（与显示口径一致）。"""
        _node()
        src = _src("app.js")
        # periodKeyFromTickets 与 periodFromTickets 相邻（同一段源码切出来直接可跑）
        seg = _body(src, "var CUTOFF_DAY", "/* /api/settle 结果")
        js = ("var store = {tickets:{data:["
              "{no:'A', date:'2026-09-02'},{no:'B', date:'2026-09-15'}]}};\n"
              + seg + "\n"
              + "var single = periodKeyFromTickets();\n"
              "store.tickets.data = [{no:'A', date:'2026-08-20'},{no:'B', date:'2026-09-10'}];\n"
              "var mixed = periodKeyFromTickets();\n"
              "store.tickets.data = [];\n"
              "var empty = periodKeyFromTickets();\n"
              "console.log(JSON.stringify({single: single, mixed: mixed, empty: empty}));")
        out = _node_run(js)
        assert out["single"] == "2026-09"
        assert out["mixed"] == ""
        assert out["empty"] == ""


# =====================================================================
# F6 前端：X-Code 百分号编码（中文口令能过 latin-1 header 缝）
# =====================================================================

class TestF6FrontEncode:
    def test_api_and_download_encode_code(self):
        src = _src("app.js")
        assert src.count("headers['X-Code'] = encodeURIComponent(code)") == 2, \
            "api() 与 downloadExport() 两处都要 encodeURIComponent（中文口令缝）"


# =====================================================================
# F7 前端：429（口令锁定）让路口令门，不进演示回落
# =====================================================================

class TestF7Front429:
    def test_api_treats_429_like_401(self):
        src = _src("app.js")
        head = _body(src, "function api(", "/* ---------- 视图状态")
        assert "res.status === 401 || res.status === 429" in head
        assert "clearCode" in head and "showGate" in head

    def test_catch_sites_let_429_through(self):
        """每个 api() 调用点的 catch：401/429 一并让路（锁定期不许掉进演示回落）。"""
        src = _src("app.js")
        assert src.count("err.status === 401 || err.status === 429") >= 6
        # 401 的旧让路写法不许再单独出现（要并上 429）
        import re
        lone = re.findall(r"err\.status === 401\)(?!\|\|)", src)
        assert not lone, "还有只让 401 不让 429 的 catch 点：%s" % lone

    def test_submit_code_shows_server_lock_message(self):
        src = _src("app.js")
        seg = src[src.index("function submitCode"):src.index("function submitCode") + 2400]
        assert "err2.status === 429" in seg       # 锁定提示用服务端人话（锁 2 分钟）


# =====================================================================
# F8：settle / ingest 超时分级（默认 3s 会把弱网大单掐进演示回落）
# =====================================================================

class TestF8Timeouts:
    def test_settle_gets_30s(self):
        src = _src("app.js")
        b = _body(src, "function runCalc", "/* ---------- CSV 解析与校验")
        i = b.index("api('/settle'")
        call = b[i:b.index("}).then", i)]
        assert "timeoutMs: 30000" in call

    def test_ingest_gets_30s(self):
        src = _src("app.js")
        b = _body(src, "function confirmImport", "function readFile")
        i = b.index("api('/ingest/csv'")
        call = b[i:b.index("}).then", i)]
        assert "timeoutMs: 30000" in call

    def test_fast_reads_stay_default(self):
        """tickets/history/trend 快读维持默认 3s（不放大等待）。"""
        src = _src("app.js")
        for fn in ("loadTickets", "loadHistory", "loadTrend"):
            start = src.index("function %s" % fn)
            end = src.index("\n  function ", start + 1)
            b = src[start:end]
            assert "timeoutMs" not in b, "%s 不该放宽超时" % fn
