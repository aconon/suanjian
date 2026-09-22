# -*- coding: utf-8 -*-
"""web/ 前端契约测试 · 修复单 R8（对比页状态机死胡同三连 F1/F2/F3 + F5 小件）。

【根因】store.compare 残留非法 p1/p2 → 每次进页带参重发 400 → 空态无选择器
→ 只能整页刷新。三个入口同一症状：
- F2 入口（onDocChange）：同选一期时先写 store 再拦 → 残留 p1===p2
- F3 入口（demoFill）：演示期标签（如「08-25 期」）回填 v.p1/v.p2 → 服务恢复
  后带演示标签请求 → 服务端期格式校验 400 → 残留
- F1 兜底（loadCompare 400 分支）：不清参、空态不渲染期选择器 → 死胡同无自救
回归口径（修复单）：①倒序参数 400 后清参→回默认可自恢复（本文件锁源码形状，
  活体验证见修复单验收）；②同期拦截后 store 无残留；③离线回落不污染 store；
④恒真断言已死（改在 test_web_contract_10.py 里）。
"""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


def _src(name):
    return (WEB / name).read_text(encoding="utf-8")


def _body(src, start, end):
    return src[src.index(start):src.index(end)]


# ---------------------------------------------------------------------
# F2：onDocChange 入口校验前置——非法（同选一期）不写 store
# ---------------------------------------------------------------------

class TestF2EntryGuard:
    def _branch(self):
        src = _src("app.js")
        return _body(src, "function onDocChange",
                     "document.addEventListener('change'")

    def test_old_unconditional_write_gone(self):
        """旧写法（先写 store 再校验）必须消失——那是残留的源头。"""
        assert "c[ev.target.id" not in self._branch()

    def test_validate_before_write(self):
        """校验在写 store 之前：toast 拦截分支必须先于 c[key] 赋值出现。"""
        b = self._branch()
        i_toast = b.index("两个期间不能选同一期")
        i_write = b.index("c[key] = ev.target.value")
        assert i_toast < i_write

    def test_illegal_branch_rolls_back_control_and_returns(self):
        """非法分支：回滚该控件的旧选中（不残留 UI 假象）+ return（不发请求）。"""
        b = self._branch()
        seg = b[b.index("两个期间不能选同一期") - 400:b.index("c[key] = ev.target.value")]
        assert "ev.target.value = key" in seg          # 回滚控件到旧值
        assert "return;" in seg                        # 拦下：不发请求、不写 store

    def test_legal_write_gated_on_both_periods(self):
        """合法写入后：两期都选齐才重发（单边空=等另一边，避免半参请求）。"""
        b = self._branch()
        assert "if (c.p1 && c.p2) loadCompare(true)" in b


# ---------------------------------------------------------------------
# F3：demoFill 不回填演示标签——store 期参只装真服务的期
# ---------------------------------------------------------------------

class TestF3DemoNoPollution:
    def _loader(self):
        src = _src("app.js")
        return _body(src, "function loadCompare", "function demoCompare")

    def test_demo_labels_never_backfilled(self):
        """演示期标签（08-25 期 等）绝不进 store——服务恢复后请求不再带毒参。"""
        b = self._loader()
        assert "v.p1 = d.p1" not in b
        assert "v.p2 = d.p2" not in b

    def test_demo_fill_empties_period_params(self):
        """回落演示时把期参置空串 → 下次装载走缺省（最近两期）。"""
        b = self._loader()
        assert "v.p1 = ''" in b and "v.p2 = ''" in b


# ---------------------------------------------------------------------
# F1：loadCompare 400 分支自救——清参 + 空态保留期选择器
# ---------------------------------------------------------------------

class TestF1SelfRescue:
    def _loader(self):
        src = _src("app.js")
        return _body(src, "function loadCompare", "function demoCompare")

    def test_400_branch_clears_period_params(self):
        """400 = 参数有毒（倒序/期没记录/同参）→ 清 v.p1/v.p2，
        下次进页走缺省最近两期，自恢复。"""
        b = self._loader()
        seg = b[b.index("err.status === 400"):b.index("return demoFill")]
        assert "v.p1 = ''" in seg and "v.p2 = ''" in seg

    def test_store_remembers_period_list(self):
        """store.compare 记住最近一次成功装载的期表（空态选择器的选项来源）。"""
        src = _src("app.js")
        store = _body(src, "var store", "var ui =")
        assert "periods: []" in store
        loader = self._loader()
        assert "v.periods = " in loader            # 成功装载时更新期表

    def test_empty_state_keeps_period_picker(self):
        """空态渲染保留期选择器：用户改完下拉直接重发，
        不再是无选择器的死胡同（期表已知 ≥2 期才摆）。"""
        src = _src("app.js")
        body = _body(src, "function renderCompare", "/* ---------- 视图：设置")
        seg = body[body.index("if (!d) {"):body.index("var picker =")]
        assert "cmpPickerHtml(" in seg
        assert "v.periods.length >= 2" in seg

    def test_picker_html_shared_by_ready_and_empty(self):
        """期选择器构造收敛为一个函数（就绪态与空态同一份，不复制两份走样）。"""
        src = _src("app.js")
        assert src.count("function cmpPickerHtml") == 1
        body = _body(src, "function renderCompare", "/* ---------- 视图：设置")
        assert body.count("cmpPickerHtml(") >= 2   # 空态 + 就绪态共用


# ---------------------------------------------------------------------
# F5：normalizeTrend 函数体首行格式残留
# ---------------------------------------------------------------------

class TestF5Format:
    def test_function_brace_on_its_own_line(self):
        """函数声明行只到 { 为止，函数体首行另起（格式残留清掉）。"""
        src = _src("app.js")
        assert re.search(r"function normalizeTrend\(out\) \{\s*$", src, re.M), \
            "normalizeTrend 声明行仍拖着语句（格式残留未清）"
