# -*- coding: utf-8 -*-
"""web/ 前端契约测试 · 任务 09（打印版工资条 + 设置页数据备份）。

沿用 test_web_contract.py 的两层防线：node --check 保语法、源码断言锁行为。
契约 = docs/任务单/09-打印工资条与数据备份.md：
- 工资条页「打印」按钮 + @media print 专用 CSS：每工人一张卡、A5 竖排两联、
  隐导航/按钮/徽章、只留票头+明细+签字线，Ctrl+P 直接出可裁剪的工牌条
- 设置页（/#/settings）「下载数据备份」：GET /api/backup 经 fetch+blob 下载
  （口令门 X-Code 只有 fetch 能带，禁 <a href> 直链）、401 清口令重弹、在途防抖
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
# 打印版工资条：按钮 + 打印版标记
# ---------------------------------------------------------------------

class TestPrintButton:
    def test_node_check_app_js(self):
        ok, err = _node_check("app.js")
        assert ok is True, f"app.js 语法错误：{err}"

    def test_print_button_on_payslip_view(self):
        src = _src("app.js")
        body = src[src.index("function renderPayslip"):src.index("/* ---------- 视图：历史")]
        assert 'data-act="print-payslips"' in body     # 工资条就绪态有打印按钮
        assert "printPayslipsBtn" in body              # 有 id

    def test_print_wired_to_window_print(self):
        src = _src("app.js")
        body = src[src.index("function onDocClick"):src.index("document.addEventListener('click'")]
        assert "print-payslips" in body                # 走统一事件委托
        assert "window.print()" in body                # 唤起浏览器打印


class TestPrintSheets:
    """打印版结构：每工人一张卡、一卡两联（存根联/员工联）、裁剪线、签字线。"""

    def _sheets_src(self):
        src = _src("app.js")
        return src[src.index("function payslipPrintCopy"):src.index("/* ---------- 视图：历史")]

    def test_two_copies_per_card(self):
        body = self._sheets_src()
        assert "存根联" in body                        # 第一联（老板留底）
        assert "员工联" in body                        # 第二联（发车间）

    def test_cut_line_between_copies(self):
        body = self._sheets_src()
        assert "print-cut" in body                     # 裁剪线元素
        assert "剪开" in body                          # 说人话的裁剪提示

    def test_signature_line_present(self):
        body = self._sheets_src()
        assert "print-sign" in body                    # 签字线
        assert "签字" in body

    def test_head_and_details_present(self):
        body = self._sheets_src()
        assert "print-head" in body                    # 票头
        assert "print-table" in body                   # 明细表
        assert "实发" in body                          # 实发金额（签字前必须看见）

    def test_print_sheets_rendered_into_payslip(self):
        src = _src("app.js")
        body = src[src.index("function renderPayslip"):src.index("/* ---------- 视图：历史")]
        assert "print-sheets" in body                  # 打印版容器挂在工资条视图里

    def test_names_escaped_in_print_html(self):
        body = self._sheets_src()
        assert "esc(p.name)" in body                   # 姓名过 esc（注入防线不撤）


class TestPrintCss:
    """print 专用 CSS 契约：A5 竖排、隐导航/按钮/徽章、只留打印版。"""

    def test_media_print_block_exists(self):
        css = _src("style.css")
        assert "@media print" in css

    def test_page_a5_portrait(self):
        css = _src("style.css")
        print_blk = css[css.index("@media print"):]
        assert "@page" in print_blk
        assert "A5 portrait" in print_blk              # A5 竖排

    def test_hides_chrome_on_print(self):
        css = _src("style.css")
        print_blk = css[css.index("@media print"):]
        for sel in (".appbar", ".tabbar", ".btn", ".badge"):
            assert sel in print_blk, f"{sel} 必须在打印时隐藏"
        assert "display:none" in print_blk.replace(" ", "")  # 屏面元素真被藏掉

    def test_payslip_view_only_print_sheets(self):
        css = _src("style.css")
        print_blk = css[css.index("@media print"):]
        assert "#view-payslip" in print_blk            # 只留工资条视图
        assert ":not(.print-sheets)" in print_blk      # 屏面卡片全藏，只留打印版

    def test_print_sheets_hidden_on_screen(self):
        css = _src("style.css")
        screen_part = css[:css.index("@media print")]
        assert ".print-sheets" in screen_part
        assert "display:none" in screen_part[screen_part.index(".print-sheets"):]

    def test_card_not_split_across_pages(self):
        css = _src("style.css")
        print_blk = css[css.index("@media print"):]
        assert "break-inside:avoid" in print_blk       # 一工人一卡不被劈两页

    def test_cut_line_dashed(self):
        css = _src("style.css")
        print_blk = css[css.index("@media print"):]
        assert "dashed" in print_blk                   # 裁剪线=虚线


# ---------------------------------------------------------------------
# 设置页 + 数据备份
# ---------------------------------------------------------------------

class TestSettingsMarkup:
    def test_tab_in_index(self):
        html = _src("index.html")
        assert 'href="#/settings"' in html
        assert 'data-route="settings"' in html
        assert ">设置</span>" in html

    def test_view_section_in_index(self):
        html = _src("index.html")
        assert 'id="view-settings"' in html

    def test_route_registered(self):
        src = _src("app.js")
        assert "'settings'" in src[src.index("var ROUTES"):src.index("var currentRoute")]
        assert "settings: renderSettings" in src       # 渲染表注册

    def test_backup_button_in_settings(self):
        src = _src("app.js")
        body = src[src.index("function renderSettings"):src.index("/* ---------- 路由 ---------- */")]
        assert 'data-act="download-backup"' in body    # 下载数据备份按钮
        assert "下载数据备份" in body
        assert "备份" in body


class TestBackupLogic:
    def test_download_via_fetch_not_href(self):
        """红线：不许 <a href> 直链（口令门 X-Code 头只有 fetch 能带）——
        复用任务 08 的 downloadExport（自带 401 收口+文件名解析+30s 宽限）。"""
        src = _src("app.js")
        body = src[src.index("function downloadBackup"):src.index("/* ---------- 班组计件")]
        assert "downloadExport('/backup'" in body
        assert "a.href" not in body                     # 不自造直链

    def test_401_handled_by_shared_helper(self):
        src = _src("app.js")
        body = src[src.index("function downloadBackup"):src.index("/* ---------- 班组计件")]
        assert "err.status === 401" in body             # 门已弹则让路

    def test_debounced_and_human_errors(self):
        src = _src("app.js")
        body = src[src.index("function downloadBackup"):src.index("/* ---------- 班组计件")]
        assert "btn.disabled = true" in body            # 在途防抖
        assert "btn.disabled = false" in body           # 成败一律恢复
        assert "备份没成功" in body                      # 失败说人话

    def test_backup_wired_in_click_delegate(self):
        src = _src("app.js")
        body = src[src.index("function onDocClick"):src.index("document.addEventListener('click'")]
        assert "download-backup" in body
        assert "downloadBackup()" in body
