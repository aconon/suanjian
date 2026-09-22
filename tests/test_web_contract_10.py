# -*- coding: utf-8 -*-
"""web/ 前端契约测试 · 任务单 10（期间对比视图 /#/compare）。

沿用 test_web_contract.py 的两层防线：node --check 保语法、源码断言锁行为。
契约 = docs/任务单/10-期间对比视图.md + server.py api_compare（shape 由本单定死）：
- 新视图 /#/compare：tab「对比」+ 容器 + 路由 + loader（401 让路 / 404 探测
  health 保在线空态 / 不通 → 演示回落+徽章 / 400 人话上屏不进演示）
- 期选择器（两个 select，切换即重拉；选同一期前端就拦下说人话）
- 逐人对比：并排条 + 箭头涨跌色（涨=绿 --ok / 跌=暖橙 --warn，克制只在
  变化徽章上用色）；工人按 |实发变化| 降序（服务端排好，前端按序渲染）
- 新出现 / 这期没出现的工人单独分组
- 红线：打印 CSS（@media print 块）与口令门不许碰
"""
import subprocess
from pathlib import Path
from shutil import which

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"


def _src(name):
    return (WEB / name).read_text(encoding="utf-8")


def _node_check(name):
    if not (Path("/usr/bin/node").exists() or which("node")):
        pytest.skip("本机没有 node，跳过 JS 语法检查")
    r = subprocess.run(["node", "--check", str(WEB / name)],
                       capture_output=True, text=True)
    return r.returncode == 0, r.stderr


def _body(src, start, end):
    return src[src.index(start):src.index(end)]


# ---------------------------------------------------------------------
# 标记（index.html + 路由注册）
# ---------------------------------------------------------------------

class TestCompareMarkup:
    def test_node_check_app_js(self):
        ok, err = _node_check("app.js")
        assert ok is True, f"app.js 语法错误：{err}"

    def test_tab_in_index(self):
        html = _src("index.html")
        assert 'href="#/compare"' in html
        assert 'data-route="compare"' in html
        assert html.count('data-route="compare"') == 1      # 就一个对比入口
        assert ">对比</span>" in html

    def test_view_section_in_index(self):
        html = _src("index.html")
        # R8-F4：原断言 `... or True` 恒真（等于没断），改为真断言：
        # 容器必须是 section.view + id（与兄弟视图同构），写错立即红
        section = '<section class="view" id="view-compare" hidden></section>'
        assert section in html
        assert 'id="view-compare" hidden' in html

    def test_route_registered(self):
        src = _src("app.js")
        assert "'compare'" in _body(src, "var ROUTES", "var currentRoute")
        assert "compare: renderCompare" in src
        assert "compare: loadCompare" in src

    def test_store_has_compare(self):
        src = _src("app.js")
        assert "compare:" in _body(src, "var store", "var ui =")   # 视图状态注册


# ---------------------------------------------------------------------
# loader：loadCompare（机制沿用 loadTrend）
# ---------------------------------------------------------------------

class TestCompareLoader:
    def test_calls_api_with_periods(self):
        src = _src("app.js")
        body = _body(src, "function loadCompare", "function demoCompare")
        assert "api('/compare'" in body
        assert "encodeURIComponent" in body        # 期参数要编码（含中文演示期标签）

    def test_401_guard(self):
        src = _src("app.js")
        body = _body(src, "function loadCompare", "function demoCompare")
        assert "err.status === 401" in body        # 口令门让路，不进演示回落

    def test_404_health_probe(self):
        src = _src("app.js")
        body = _body(src, "function loadCompare", "function demoCompare")
        assert "err.status === 404" in body        # 老服务：health 探测保在线空态
        assert "api('/health')" in body

    def test_400_keeps_online_human_message(self):
        """在线但数据不够两期：服务端人话上屏，不进演示回落。"""
        src = _src("app.js")
        body = _body(src, "function loadCompare", "function demoCompare")
        assert "err.status === 400" in body
        branch = body[body.index("err.status === 400"):]
        assert "onlineEmpty" in branch             # 400 分支把人话存起来给空态
        assert "demoFill" not in branch.split("}")[0]  # 该分支不回落演示

    def test_demo_fallback(self):
        src = _src("app.js")
        body = _body(src, "function loadCompare", "function demoCompare")
        assert "demoFill" in body
        assert "markDemo()" in body                # 徽章亮（演示回落铁律）

    def test_demo_data_from_history(self):
        src = _src("app.js")
        body = _body(src, "function demoCompare", "function normalizeCompare")
        assert "DEMO.history" in body              # 演示来源与趋势一致


# ---------------------------------------------------------------------
# normalizeCompare：响应 → 视图模型
# ---------------------------------------------------------------------

class TestNormalizeCompare:
    def test_maps_contract_fields(self):
        src = _src("app.js")
        body = _body(src, "function normalizeCompare",
                     "/* ---------- 视图：期间对比")
        for snake, camel in (("new_workers", "newWorkers"),
                             ("gone_workers", "goneWorkers"),
                             ("totals", "totals"), ("periods", "periods")):
            assert snake in body and camel in body

    def test_strings_kept_numbers_for_bars(self):
        src = _src("app.js")
        body = _body(src, "function normalizeCompare",
                     "/* ---------- 视图：期间对比")
        assert "String(" in body                   # 服务端精确字符串原样保留
        assert "pieceNum1" in body and "pieceNum2" in body  # Number 只进条形比例

    def test_null_semantics(self):
        """rate 可为 null（p1 基数 0）——视图侧不编 0。"""
        src = _src("app.js")
        body = _body(src, "function normalizeCompare",
                     "/* ---------- 视图：期间对比")
        assert "!= null" in body


# ---------------------------------------------------------------------
# renderCompare：期选择器 + 并排条 + 涨跌色 + 新/消失分组
# ---------------------------------------------------------------------

class TestRenderCompare:
    def test_period_pickers(self):
        src = _src("app.js")
        body = _body(src, "function renderCompare",
                     "/* ---------- 视图：设置")
        assert "cmpPickerHtml(" in body                 # 渲染走选择器构造（R8-F1 抽公用）
        picker = _body(src, "function cmpPickerHtml",
                       "function renderCompare")
        assert 'id="cmpP1"' in picker and 'id="cmpP2"' in picker
        assert 'data-act="cmp-p1"' in picker and 'data-act="cmp-p2"' in picker

    def test_delta_arrow_colors(self):
        src = _src("app.js")
        body = _body(src, "function cmpDelta",
                     "/* ---------- 视图：设置")
        assert "up" in body and "down" in body     # 涨/跌两向
        assert "cmpArrow" in body                  # 箭头不是文字符号，统一小 SVG
        # 回归（无头实测抓到）：服务端 rate 自带符号，前端再前缀会出现「--71.43%」双负号
        assert "'（' + esc(b.rate) + '%）'" in body.replace('"', "'")
        css = _src("style.css")
        assert ".cmp-delta.up" in css and "var(--ok)" in css     # 涨=绿令牌
        assert ".cmp-delta.down" in css and "var(--warn)" in css  # 跌=暖橙令牌

    def test_side_by_side_bars(self):
        src = _src("app.js")
        body = _body(src, "function cmpArrow",
                     "/* ---------- 视图：设置")
        assert "cmp-bar-1" in body and "cmp-bar-2" in body   # 每工人两根条（p1/p2）
        assert "cmp-colbar" in body                          # 摘要两根并排柱
        css = _src("style.css")
        assert ".cmp-bars" in css and ".cmp-colwrap" in css

    def test_new_and_gone_groups(self):
        src = _src("app.js")
        body = _body(src, "function renderCompare",
                     "/* ---------- 视图：设置")
        assert "新出现的工人" in body
        assert "这期没出现的工人" in body

    def test_empty_state_with_goto_calc(self):
        src = _src("app.js")
        body = _body(src, "function renderCompare",
                     "/* ---------- 视图：设置")
        assert "goto-calc" in body                 # 凑不出两期 → 引导去核算

    def test_demo_hint(self):
        src = _src("app.js")
        body = _body(src, "function renderCompare",
                     "/* ---------- 视图：设置")
        assert "离线演示" in body                   # 演示态明示口径


# ---------------------------------------------------------------------
# 交互接线 + 重拉策略
# ---------------------------------------------------------------------

class TestCompareWiring:
    def test_select_change_wired(self):
        src = _src("app.js")
        body = _body(src, "function onDocChange",
                     "document.addEventListener('change'")
        assert "cmpP1" in body and "cmpP2" in body

    def test_same_period_blocked_client_side(self):
        src = _src("app.js")
        body = _body(src, "function onDocChange",
                     "document.addEventListener('change'")
        assert "两个期间不能选同一期" in body       # 前端先拦（人话 toast），不发请求

    def test_select_change_forces_reload(self):
        src = _src("app.js")
        body = _body(src, "function onDocChange",
                     "document.addEventListener('change'")
        assert "loadCompare(true)" in body          # 换期即重拉

    def test_loader_backfills_selected_periods(self):
        """回归（无头实测抓到）：缺省装载后必须用响应回填 p1/p2——
        否则用户只换一侧下拉时 (v.p1 && v.p2) 不成立，请求丢参数回落缺省。"""
        src = _src("app.js")
        body = _body(src, "function loadCompare", "function demoCompare")
        assert "v.p1 = v.data.p1" in body
        assert "v.p2 = v.data.p2" in body

    def test_force_reload_each_visit(self):
        src = _src("app.js")
        body = _body(src, "function showView",
                     "/* ---------- 事件委托")
        assert "route === 'compare'" in body        # 每次进对比页重拉（同历史/趋势）

    def test_submit_code_resets_compare(self):
        src = _src("app.js")
        body = _body(src, "function submitCode", "function api(")
        assert "store.compare.loaded = false" in body  # 输对口令后强制重拉真数据


# ---------------------------------------------------------------------
# 红线：打印 CSS 与口令门不许碰
# ---------------------------------------------------------------------

class TestRedLinesUntouched:
    def test_print_block_has_no_compare_rules(self):
        css = _src("style.css")
        print_blk = css[css.index("@media print"):]
        assert "cmp-" not in print_blk            # 对比样式全在屏幕区，打印块零改动
        assert "#view-payslip" in print_blk       # 打印选择器原样（工资条票面仍是主角）

    def test_gate_markup_untouched(self):
        html = _src("index.html")
        assert 'id="codeGate"' in html            # 口令门浮层原样
        assert 'id="codeInput"' in html
        src = _src("app.js")
        # 口令门收口原样：api() 统一带 X-Code、401 清口令重弹（对比页自动继承）
        assert "headers['X-Code']" in src
        assert "clearCode()" in src and "showGate()" in src
