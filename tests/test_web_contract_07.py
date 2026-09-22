# -*- coding: utf-8 -*-
"""web/ 前端契约测试 · 任务单 07（口令门浮层）。

沿用 test_web_contract.py 的两层防线：node --check 保语法、源码断言锁行为。
契约 = docs/任务单/07-口令门与公网模式.md + 塔罗小屋同款机制：
- 口令输入浮层（输错提示+可重输），口令存 sessionStorage（输对后不再问）
- /api 401 时清口令重弹
- 红线：口令值不落 console/页面常驻（只进 sessionStorage 与请求头）
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
# 浮层结构（index.html）
# ---------------------------------------------------------------------

class TestGateMarkup:
    def test_node_check_app_js(self):
        ok, err = _node_check("app.js")
        assert ok is True, f"app.js 语法错误：{err}"

    def test_gate_overlay_in_index(self):
        html = _src("index.html")
        assert 'id="codeGate"' in html          # 浮层容器（默认 hidden，401 才弹）
        assert 'id="codeInput"' in html         # 口令输入框
        assert 'id="codeErr"' in html           # 输错提示位
        assert 'data-act="code-submit"' in html # 提交按钮走统一事件委托

    def test_gate_input_is_password(self):
        html = _src("index.html")
        idx = html.index("codeInput")
        seg = html[idx - 200:idx + 200]
        assert 'type="password"' in seg         # 口令掩码显示（防旁人偷看）

    def test_gate_hidden_by_default(self):
        html = _src("index.html")
        idx = html.index('id="codeGate"')
        seg = html[max(0, idx - 120):idx + 120]   # 整个开标签范围内找
        assert "hidden" in seg                  # 不问口令的本机模式零感知


# ---------------------------------------------------------------------
# 门逻辑（app.js）
# ---------------------------------------------------------------------

class TestGateLogic:
    def test_sessionstorage_remember(self):
        src = _src("app.js")
        assert "sessionStorage" in src
        assert "sj_code" in src                 # 固定键名：输对后本会话不再问

    def test_api_sends_x_code_header(self):
        src = _src("app.js")
        head = src[src.index("function api("):src.index("/* ---------- 视图状态")]
        assert "'X-Code'" in head               # 有存着的口令就随请求带上

    def test_401_clears_code_and_shows_gate(self):
        src = _src("app.js")
        head = src[src.index("function api("):src.index("/* ---------- 视图状态")]
        assert "401" in head                    # api() 统一收口：401 → 清口令+弹门
        assert "clearCode" in head
        assert "showGate" in head

    def test_gate_submit_probes_then_persists(self):
        src = _src("app.js")
        assert "function submitCode" in src
        seg = src[src.index("function submitCode"):src.index("function submitCode") + 2200]
        assert "api('/tickets')" in seg         # 拿真接口验口令（health 豁免验不了）
        assert "口令不对" in seg                 # 输错提示+可重输
        assert "setCode" in seg                 # 输对才记住

    def test_enter_key_submits(self):
        src = _src("app.js")
        assert "codeInput" in src and "Enter" in src  # 回车提交（手机键盘右下角）

    def test_all_api_catch_sites_guard_401(self):
        """401 不许掉进演示回落：每个 api() 的 catch 都先让路给口令门。"""
        src = _src("app.js")
        assert src.count("err.status === 401") + src.count("err2.status === 401") >= 6

    def test_gate_success_forces_data_reload(self):
        src = _src("app.js")
        seg = src[src.index("function submitCode"):src.index("function submitCode") + 2200]
        assert "loaded = false" in seg          # 输对后强制重拉真数据，不用演示残留


# ---------------------------------------------------------------------
# 浮层样式（style.css）
# ---------------------------------------------------------------------

class TestGateStyle:
    def test_gate_overlay_style(self):
        css = _src("style.css")
        assert ".code-gate" in css
        assert ".code-card" in css
        assert "z-index" in css                 # 盖在整页之上
