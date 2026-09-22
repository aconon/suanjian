# -*- coding: utf-8 -*-
"""web/ 前端契约测试 · 任务单 06（拍照上传 + 班组模式 + 历史/工票真数据）。

沿用 test_web_contract.py 的两层防线：node --check 保语法、源码断言锁行为。
契约来源：docs/任务单/05-持久化与OCR接线-后端.md（路由/字段名即最终契约，
前端只消费不定义）；金额=精确字符串，展示 Number()、回传原字符串。
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
# 任务 1：导入页「拍照/选图」→ POST /api/ocr → 预填表人工确认
# ---------------------------------------------------------------------

class TestOcrUploadPath:
    def test_node_check_app_js(self):
        ok, err = _node_check("app.js")
        assert ok is True, f"app.js 语法错误：{err}"

    def test_node_check_demo_data_js(self):
        ok, err = _node_check("demo-data.js")
        assert ok is True, f"demo-data.js 语法错误：{err}"

    def test_ocr_request_contract(self):
        src = _src("app.js")
        assert "api('/ocr'" in src            # POST /api/ocr（05 契约路由）
        assert "image_base64" in src           # 契约字段：base64 原文（不带 data: 前缀）
        assert "mime" in src                   # 契约字段：图片 MIME

    def test_ocr_timeout_widened(self):
        # 契约：识别超时 60s；api() 默认 3s 会掐死识别请求，必须按调用放宽
        src = _src("app.js")
        assert "timeoutMs" in src
        body = src[src.index("function startOcr"):src.index("function ocrReceived")]
        assert "65000" in body                 # 60s 契约 + 余量

    def test_image_size_guard_8mb(self):
        src = _src("app.js")                   # 契约：图片 ≤ 8MB，前端门口先拦
        assert "8 * 1024 * 1024" in src

    def test_image_picker_present(self):
        src = _src("app.js")
        assert 'id="imgInput"' in src          # 拍照/选图入口（accept image/*）
        assert "choose-image" in src
        assert "capture=" in src               # 手机上调后摄

    def test_errors_reject_whole_sheet(self):
        # errors 有值 → 整单打回（不进预填表）
        src = _src("app.js")
        body = src[src.index("function ocrReceived"):src.index("function ocrFailed")]
        assert "errors.length" in body
        assert "ocr-error" in body

    def test_needs_review_yellow_and_inline_edit(self):
        src = _src("app.js")
        css = _src("style.css")
        assert "needs_review" in src           # 契约字段驱动复核态
        assert "data-fld" in src               # 行内编辑锚点（票头+行字段）
        assert "ocrFieldBad" in src            # 可疑判定（[?]/空/数字不合法）
        assert ".suspect" in css               # 标黄样式
        assert "suspect" in src

    def test_confirm_gated_until_clean(self):
        # needs_review=true 时人工改完才可「确认入账」：按钮按待确认计数门禁
        src = _src("app.js")
        body = src[src.index("function confirmOcr"):src.index("function stateBlock")]
        assert "mode !== 'ocr-review'" in body
        assert "ocrPendingCount(imp) > 0" in body

    def test_confirm_saves_ticket_contract(self):
        # 确认入账 → POST /api/tickets/save（05 契约），票号字段与 core/db.py 对齐
        src = _src("app.js")
        body = src[src.index("function confirmOcr"):src.index("function stateBlock")]
        assert "api('/tickets/save'" in body
        assert "sheet_no" in body

    def test_inline_edit_updates_dom_in_place(self):
        # 行内编辑不整页重渲染（会丢输入焦点）：局部更新黄标/计数/按钮
        src = _src("app.js")
        body = src[src.index("function onDocInput"):src.index("function onHashChange")]
        assert "classList.toggle" in body
        assert "data-fld" in body


# ---------------------------------------------------------------------
# 任务 2：核算页模式切换（个人/班组）
# ---------------------------------------------------------------------

class TestTeamMode:
    def test_mode_switch_present(self):
        src = _src("app.js")
        assert 'data-act="calc-mode"' in src
        assert "team" in src and "individual" in src

    def test_team_total_sent_as_raw_string(self):
        # 契约：金额精确字符串——班组总额按输入框原字符串回传，不经 Number 往返
        src = _src("app.js")
        body = src[src.index("function runCalc"):src.index("var CSV_COLS")]
        assert "team_total: teamTotal" in body
        assert "teamTotal = String(" in body

    def test_settle_mode_follows_switch(self):
        src = _src("app.js")
        body = src[src.index("function runCalc"):src.index("var CSV_COLS")]
        assert "mode === 'team'" in body       # 班组/个人分支
        assert "teamMembersFromTickets()" in body

    def test_team_members_use_qualified_qty(self):
        # 分摊权重=合格数量（数量−废品）
        src = _src("app.js")
        body = src[src.index("function teamMembersFromTickets"):src.index("function teamToCalcView")]
        assert "Number(r.qty)" in body
        assert "Number(r.defect || 0)" in body

    def test_alloc_detail_with_residual_marking(self):
        # 结果展示分摊明细，含尾差调整行标注（remark=尾差调整）
        src = _src("app.js")
        body = src[src.index("function calcModeCard"):src.index("function renderPayslip")]
        assert "尾差调整" in body
        assert "remark" in body
        assert "base_amount" in body
        assert "adjust_amount" in body

    def test_demo_team_fallback(self):
        # 离线演示回落：班组分摊本地算（对齐个人模式的 DEMO.calc 机制）
        src = _src("demo-data.js")
        assert "teamCalc" in src
        app = _src("app.js")
        assert "teamCalc(" in app


# ---------------------------------------------------------------------
# 任务 3：历史（+工票）接真数据，回落机制沿用
# ---------------------------------------------------------------------

class TestHistoryRealData:
    def test_load_history_calls_api(self):
        src = _src("app.js")
        body = src[src.index("function loadHistory"):src.index("function normalizeTicketList")]
        assert "api('/history')" in body       # GET /api/history（05 契约）

    def test_offline_demo_fill_once_kept(self):
        # F20 机制沿用：服务不可用回落演示数据，且只在没有会话记录时填充
        src = _src("app.js")
        body = src[src.index("function loadHistory"):src.index("function normalizeSheets")]
        assert "if (!v.data || !v.data.length)" in body

    def test_history_amount_display_number(self):
        # 契约：金额精确字符串，展示才 Number()
        src = _src("app.js")
        body = src[src.index("function normalizeHistory"):src.index("function normalizeSheets")]
        assert "Number(" in body


class TestTicketsRealData:
    def test_load_tickets_calls_api(self):
        src = _src("app.js")
        body = src[src.index("function loadTickets"):src.index("function loadHistory")]
        assert "api('/tickets')" in body       # GET /api/tickets（05 契约）

    def test_ticket_sheet_no_alias_consumed(self):
        # core/db.py list_tickets 用 sheet_no 字段：前端读侧做别名容错
        src = _src("app.js")
        body = src[src.index("function normalizeTicketList"):src.index("function loadHistory")] \
            if "function normalizeTicketList" in src else ""
        assert "sheet_no" in src

    def test_history_view_tolerant_meta(self):
        # 结算底账没有工票数时不显示「0 张工票」，有 mode 展示计件模式
        src = _src("app.js")
        body = src[src.index("function renderHistory"):]
        assert "h.tickets != null" in body
