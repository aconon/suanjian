# -*- coding: utf-8 -*-
"""web/ 前端契约测试 · 任务 08（工资条导出 + 趋势视图）。

沿用 test_web_contract.py 的两层防线：node --check 保语法、源码断言锁行为。
契约 = docs/任务单/08-工资条导出与趋势报表.md：
- 工资条页「导出工资条」按钮：fetch+blob 下载（不能 <a href> 直链——口令门的
  X-Code 头只有 fetch 能带）；401 → 清口令重弹；文件名取 Content-Disposition
- 趋势视图 /#/trend：tab + 容器 + 路由 + loader（401 让路/404 探测/演示回落）；
  三指标小卡（总产值/报废率/人均产出）+ 精确值表格；SVG 折线零依赖
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


# ---------------------------------------------------------------------
# 导出按钮（工资条页）
# ---------------------------------------------------------------------

class TestExportMarkup:
    def test_node_check_app_js(self):
        ok, err = _node_check("app.js")
        assert ok is True, f"app.js 语法错误：{err}"

    def test_export_button_on_payslip_view(self):
        src = _src("app.js")
        body = src[src.index("function renderPayslip"):src.index("/* ---------- 视图：历史")]
        assert 'data-act="export-payslips"' in body     # 工资条就绪态有导出按钮
        assert "exportPayslipsBtn" in body              # 有 id（在途防抖找它）

    def test_download_uses_fetch_not_href(self):
        """红线：不许 <a href> 直链（口令门 X-Code 头带不上）。"""
        src = _src("app.js")
        body = src[src.index("function downloadExport"):src.index("function exportPayslips")]
        assert "fetch('/api' + path" in body
        assert "res.blob()" in body
        assert "URL.createObjectURL" in body
        assert "a.download = name" in body


class TestExportLogic:
    def test_sends_x_code_header(self):
        src = _src("app.js")
        body = src[src.index("function downloadExport"):src.index("function exportPayslips")]
        assert "headers['X-Code']" in body               # 导出也进门

    def test_401_clears_code_and_shows_gate(self):
        src = _src("app.js")
        body = src[src.index("function downloadExport"):src.index("function exportPayslips")]
        assert "res.status === 401" in body
        assert "clearCode" in body
        assert "showGate" in body

    def test_filename_from_content_disposition(self):
        src = _src("app.js")
        body = src[src.index("function downloadExport"):src.index("function exportPayslips")]
        assert "Content-Disposition" in body             # 中文真名优先
        assert "decodeURIComponent" in body

    def test_export_wired_in_click_delegate(self):
        src = _src("app.js")
        body = src[src.index("function onDocClick"):src.index("document.addEventListener('click'")]
        assert "export-payslips" in body                 # 走统一事件委托
        assert "exportPayslips()" in body

    def test_export_debounced_and_human_errors(self):
        src = _src("app.js")
        body = src[src.index("function exportPayslips"):src.index("/* ---------- 班组计件")]
        assert "btn.disabled = true" in body             # 在途防抖
        assert "导出没成功" in body                       # 失败说人话（toast）


class TestExportTimeout:
    """修复单 R5 F3：downloadExport 抄 api() 的 AbortController 模式——
    导出宽限 30s；超时按钮恢复 + 人话 toast（不许停在「正在生成…」）。"""

    def test_download_uses_abortcontroller_with_30s(self):
        src = _src("app.js")
        body = src[src.index("function downloadExport"):src.index("function exportPayslips")]
        assert "AbortController" in body                 # 抄 api() 同款模式
        assert "30000" in body                           # 导出宽限 30s
        assert "ctrl.abort()" in body                    # 到点掐断
        assert "clearTimeout" in body                    # 成功/失败都要撤定时器
        assert "signal" in body                          # 信号真接进 fetch

    def test_timeout_human_toast_and_button_restore(self):
        src = _src("app.js")
        body = src[src.index("function exportPayslips"):src.index("/* ---------- 班组计件")]
        assert "导出超时，再试一次" in body                 # 超时说人话（不甩 HTTP 术语）
        assert "err.timeout" in body                     # 超时单独分支，不进通用报错
        assert "btn.disabled = false" in body            # 超时路径按钮也恢复（防死锁）


# ---------------------------------------------------------------------
# 趋势视图（/#/trend）
# ---------------------------------------------------------------------

class TestTrendMarkup:
    def test_tab_in_index(self):
        html = _src("index.html")
        assert 'href="#/trend"' in html
        assert 'data-route="trend"' in html
        assert ">趋势</span>" in html

    def test_view_section_in_index(self):
        html = _src("index.html")
        assert 'id="view-trend"' in html

    def test_route_registered(self):
        src = _src("app.js")
        assert "'trend'" in src[src.index("var ROUTES"):src.index("var currentRoute")]
        assert "trend: renderTrend" in src               # 渲染表注册
        assert "trend: loadTrend" in src                 # 加载表注册

    def test_chart_css_tokens(self):
        css = _src("style.css")
        assert ".chart-card" in css
        assert ".chart-line" in css
        assert "non-scaling-stroke" not in css           # 该属性在 SVG 里，不在 CSS
        assert ".chart-stack" in css


class TestTrendLogic:
    def test_loader_401_guard(self):
        src = _src("app.js")
        body = src[src.index("function loadTrend"):src.index("function demoTrendItems")]
        assert "api('/trend')" in body
        assert "err.status === 401" in body              # 口令门让路，不进演示回落

    def test_loader_404_health_probe(self):
        src = _src("app.js")
        body = src[src.index("function loadTrend"):src.index("function demoTrendItems")]
        assert "err.status === 404" in body              # 老服务：health 探测保空态
        assert "api('/health')" in body

    def test_demo_fallback_from_history(self):
        src = _src("app.js")
        body = src[src.index("function demoTrendItems"):src.index("function normalizeTrend")]
        assert "DEMO.history" in body                    # 离线演示数据来源
        assert "reverse()" in body                       # 新期在前 → 升序

    def test_three_metric_cards(self):
        src = _src("app.js")
        body = src[src.index("function renderTrend"):src.index("/* ---------- 路由 ---------- */")]
        for metric in ("总产值", "报废率", "人均产出"):
            assert metric in body                        # 任务单点名的三个指标

    def test_svg_sparkline_no_library(self):
        src = _src("app.js")
        body = src[src.index("function sparkline"):src.index("function renderTrend")]
        assert "<polyline" in body                       # 手写 SVG 折线（零依赖）
        assert "non-scaling-stroke" in body              # 发丝线不被拉伸变粗
        assert "null" in body                            # 缺口径的期跳过，不编 0

    def test_table_shows_exact_strings(self):
        src = _src("app.js")
        body = src[src.index("function renderTrend"):src.index("/* ---------- 路由 ---------- */")]
        assert "totalStr" in body                        # 精确字符串进表格
        assert "scrapStr" in body
        assert "perCapitaStr" in body

    def test_trend_force_reload_each_visit(self):
        src = _src("app.js")
        body = src[src.index("function showView"):src.index("/* ---------- 事件委托 ---------- */")]
        assert "route === 'trend'" in body               # 每次进趋势页重拉
