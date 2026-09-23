# -*- coding: utf-8 -*-
"""web/ 前端契约测试（F4/F5/F13/F20/F21 的回归锁）。

前端无 JS 测试框架，这里用两层防线：
1. node --check 保证语法可解析（改坏了立刻红）；
2. 源码契约断言锁住修复单要求的关键行为（防回退到旧的写法）。
真浏览器行为验收由人工在无头 Chrome 流程里执行（历史工单存档可查）。
"""
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"


def _src(name):
    return (WEB / name).read_text(encoding="utf-8")


def _node_check_ok(name):
    if not (Path("/usr/bin/node").exists() or _which_node()):
        pytest.skip("本机没有 node，跳过 JS 语法检查")
    r = subprocess.run(["node", "--check", str(WEB / name)],
                       capture_output=True, text=True)
    return r.returncode == 0, r.stderr


def _which_node():
    from shutil import which
    return which("node")


# ---------------------------------------------------------------------
# F4：原型键污染（byNo/detailsBy/demo-data.js map 用 Object.create(null)）
# ---------------------------------------------------------------------

class TestProtoKeyPollution:
    def test_app_js_maps_use_null_proto(self):
        src = _src("app.js")
        assert "var byNo = Object.create(null)" in src
        assert "var detailsBy = Object.create(null)" in src
        assert "var byNo = {}" not in src
        assert "var detailsBy = {}" not in src

    def test_demo_data_map_uses_null_proto(self):
        src = _src("demo-data.js")
        assert "var map = Object.create(null)" in src
        assert "var map = {}" not in src

    def test_app_js_node_check(self):
        ok, err = _node_check_ok("app.js")
        assert ok is True, f"app.js 语法错误：{err}"

    def test_demo_data_node_check(self):
        ok, err = _node_check_ok("demo-data.js")
        assert ok is True, f"demo-data.js 语法错误：{err}"


# ---------------------------------------------------------------------
# F5：confirmImport 在途防抖（入口同步置 parsing，先于网络请求）
# ---------------------------------------------------------------------

class TestConfirmImportDebounce:
    def test_mode_set_before_request(self):
        src = _src("app.js")
        body = src[src.index("function confirmImport"):src.index("function readFile")]
        assert "mode: 'parsing'" in body
        assert body.index("mode: 'parsing'") < body.index("api('/ingest/csv'")

    def test_preview_guard_still_first(self):
        src = _src("app.js")
        body = src[src.index("function confirmImport"):src.index("function readFile")]
        assert "imp.mode !== 'preview'" in body  # 防抖之外，终态校验仍在


# ---------------------------------------------------------------------
# F13：金额改用服务端返回值；amountStr 直取服务端原字符串
# ---------------------------------------------------------------------

class TestServerAmountsPreferred:
    def test_amount_str_is_server_string_verbatim(self):
        src = _src("app.js")
        assert "amountStr: p.amount" in src            # 直取服务端原字符串
        assert "String(p.amount)" not in src           # 不经 Number→String 往返

    def test_row_amount_prefers_server_value(self):
        src = _src("app.js")
        assert "function rowAmountCents" in src        # 行金额统一入口
        assert "r.amountStr != null" in src            # 服务端值优先

    def test_settle_result_written_back_to_tickets(self):
        src = _src("app.js")
        assert "srcRows[i].amountStr" in src           # settle 成功后回填工票行


# ---------------------------------------------------------------------
# F20：历史离线演示填充仅首次（不覆盖会话记录）
# ---------------------------------------------------------------------

class TestHistoryDemoFillOnce:
    def test_offline_fill_only_when_empty(self):
        src = _src("app.js")
        body = src[src.index("function loadHistory"):src.index("function normalizeSheets")]
        assert "if (!v.data || !v.data.length)" in body  # 有会话记录就不盖


# ---------------------------------------------------------------------
# F21：runCalc 400 时错误态上屏服务端人话报错
# ---------------------------------------------------------------------

class TestRunCalcSurfaces400:
    def test_catch_handles_400_with_message(self):
        src = _src("app.js")
        body = src[src.index("function runCalc"):src.index("var CSV_COLS")]
        assert "err.status === 400" in body
        assert "v.errorMsg = err.message" in body

    def test_error_view_uses_stored_message(self):
        src = _src("app.js")
        body = src[src.index("function renderCalc"):src.index("function renderPayslip")]
        assert "v.errorMsg" in body
