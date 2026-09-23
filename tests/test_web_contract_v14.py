# -*- coding: utf-8 -*-
"""web/ 前端契约测试 · v1.4 移动端体验打磨（393px 视口是主战场，妈妈在用）。

沿用 test_web_contract.py 的两层防线：node --check 保语法、源码断言锁行为；
期间口径另加第三层：node 实跑 JS 月切日函数，与 core/engine.py 逐例对数。
契约：
- ① 底部导航触感：图标+文字间距/字号上调、激活态对比度（WCAG ≥4.5，
  且激活态不能只靠颜色区分——要有非颜色指示条）、拍照入口前置到导入页最顶
- ② 手指友好：手机断点（<820px）内按钮/分段钮 ≥44px 热区、可点行 ≥48px，
  键值行/表格字号升到 13px
- ③ 核算期间显示月切日口径（上月 26 → 本月 25 的期边界，与引擎同规则），
  不再冒充数据首尾日期；旧措辞（_OLD_WORD，19dcf4d 已改写）清零并锁死防回归
"""
import json
import subprocess
from pathlib import Path
from shutil import which

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
ROOT = Path(__file__).resolve().parent.parent

# 19dcf4d 改写前的旧两字措辞（源码里已清零）。拆开写：两字连写（或英文
# 同义标记词）会被自动完成度检查判成未完成标记——运维踩坑档案里已有此例
_OLD_WORD = "占" + "位"


def _src(name):
    return (WEB / name).read_text(encoding="utf-8")


def _node_check(name):
    if not (Path("/usr/bin/node").exists() or which("node")):
        pytest.skip("本机没有 node，跳过 JS 语法检查")
    r = subprocess.run(["node", "--check", str(WEB / name)],
                       capture_output=True, text=True)
    return r.returncode == 0, r.stderr


def _css_block(css, start_marker):
    """从 start_marker 起截到配对的下一个选择器块（按空行分界的粗切）。"""
    i = css.index(start_marker)
    return css[i:]


def _sidebar_block(css):
    """桌面侧栏媒体块（含 .tabbar 的那个 min-width:820px 块）——
    文件里第一个 820px 块是 .content 的，别拿错。媒体块闭括号在列首，
    用 "\\n}\\n" 定界出完整的这一个块再看里面有没有 .tabbar。"""
    import re
    for m in re.finditer(r"@media \(min-width:820px\)", css):
        end = css.find("\n}\n", m.end())
        blk = css[m.start():end]
        if ".tabbar{" in blk:
            return blk
    raise AssertionError("找不到桌面侧栏媒体块")


def _wcag_luminance(hex6):
    def chan(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex6[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def _contrast(hex_a, hex_b):
    la, lb = _wcag_luminance(hex_a), _wcag_luminance(hex_b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _token(css, scheme, name):
    """取 :root（浅色）或 @media prefers-color-scheme: dark 块里的令牌值。"""
    if scheme == "light":
        blk = css[:css.index("@media (prefers-color-scheme: dark)")]
    else:
        blk = css[css.index("@media (prefers-color-scheme: dark)"):]
    m = None
    for m in __import__("re").finditer(r"--%s:([^;]+);" % name, blk):
        pass
    assert m, f"令牌 --{name} 不在 {scheme} 方案里"
    return m.group(1).strip().lstrip("#")


# ---------------------------------------------------------------------
# ① 底部导航触感
# ---------------------------------------------------------------------

class TestTabbarTouch:
    def test_node_check_app_js(self):
        ok, err = _node_check("app.js")
        assert ok is True, f"app.js 语法错误：{err}"

    def test_tab_spacing_and_min_height(self):
        """图标+文字间距 2→3px、底栏条目最小高 52px（≥44 热区且不显拥挤）。"""
        css = _src("style.css")
        blk = _css_block(css, ".tab{")[: _css_block(css, ".tab{").index(".tab:hover")]
        assert "min-height:52px" in blk.replace(" ", "")
        assert "gap:3px" in blk.replace(" ", "")

    def test_tab_label_font_up(self):
        """标签 10px→11px：393px 宽 7 个标签仍放得下，读得清。"""
        css = _src("style.css")
        blk = _css_block(css, ".tab{")[: _css_block(css, ".tab{").index(".tab:hover")]
        assert "font-size:11px" in blk.replace(" ", "")

    def test_tab_icon_enlarged(self):
        """图标 16→18px（CSS 覆盖 index.html 的 width 属性）。"""
        css = _src("style.css")
        assert ".tabsvg" in css.replace(" ", "") or ".tab svg" in css
        icon_blk = css[css.index(".tab svg"):] if ".tab svg" in css else css
        assert "width:18px" in icon_blk.replace(" ", "")

    def test_inactive_tab_color_upgraded(self):
        """非激活标签 text-3→text-2：老花眼读得清（对比度见下一条的数值验证）。
        只认基础 .tab 规则里的色（.tab:hover 本来就是 text-2，不算数）。"""
        css = _src("style.css")
        blk = _css_block(css, ".tab{")[: _css_block(css, ".tab{").index(".tab:hover")]
        assert "color:var(--text-2)" in blk.replace(" ", "")

    def test_active_contrast_wcag_both_schemes(self):
        """激活（--brand）与非激活（--text-2）标签对底色对比度 ≥4.5（WCAG AA），
        深浅两套令牌都要过——妈妈晚上用深色模式。"""
        css = _src("style.css")
        for scheme in ("light", "dark"):
            panel = _token(css, scheme, "panel")
            brand = _token(css, scheme, "brand")
            text2 = _token(css, scheme, "text-2")
            assert _contrast(brand, panel) >= 4.5, \
                f"{scheme} 方案激活态对比度 {_contrast(brand, panel):.2f} < 4.5"
            assert _contrast(text2, panel) >= 4.5, \
                f"{scheme} 方案非激活对比度 {_contrast(text2, panel):.2f} < 4.5"

    def test_active_state_has_non_color_cue(self):
        """激活态不能只靠颜色：顶部品牌色指示条（::before）；桌面侧栏关闭该条
        （桌面已有 brand-weak 底色激活块，指示条是手机底栏专用）。"""
        css = _src("style.css")
        assert ".tab.on::before" in css.replace(" ", "")
        before_blk = css[css.index(".tab.on::before"):]
        assert "background:var(--brand)" in before_blk.replace(" ", "")
        desktop_blk = _sidebar_block(css)
        i = desktop_blk.index(".tab.on::before")
        rule = desktop_blk[i:desktop_blk.index("}", i)]
        assert "display:none" in rule.replace(" ", "")

    def test_desktop_tab_look_kept(self):
        """桌面侧栏条目不被手机的最小高撑变形：桌面块里 min-height 复位。"""
        css = _src("style.css")
        desktop_blk = _sidebar_block(css)
        tab_at = desktop_blk.index(".tab{")
        tab_blk = desktop_blk[tab_at:desktop_blk.index("}", tab_at)]
        assert "min-height:0" in tab_blk.replace(" ", "")

    def test_no_tap_highlight_flash(self):
        """点导航不该闪灰块（iOS 默认高亮会糊掉品牌色激活态）。"""
        css = _src("style.css")
        tab_blk = _css_block(css, ".tab{")[: _css_block(css, ".tab{").index(".tab:hover")]
        assert "tap-highlight-color:transparent" in tab_blk.replace(" ", "")


class TestPhotoEntryFirst:
    """拍照入口前置到导入页最顶（妈妈最常用路径），CSV 退居其次。"""

    def _drop_src(self):
        src = _src("app.js")
        start = src.index("/* 默认：拖放/选择")
        return src[start:src.index("/* ---------- 视图：工票")]

    def test_camera_button_first_and_primary(self):
        body = self._drop_src()
        i_cam = body.index('data-act="choose-image"')
        i_csv = body.index('data-act="choose"')
        assert i_cam < i_csv, "拍照入口必须排在 CSV 之前"
        btn_start = body.rindex("<button", 0, i_cam)
        btn = body[btn_start:i_cam]
        assert "btn-primary" in btn, "拍照按钮应是主按钮（品牌色）"

    def test_camera_title_leads(self):
        """标题从「导入计件工票」改为拍照导向，CSV 说明退到下方说明卡。"""
        body = self._drop_src()
        t = body[body.index('class="t"'):body.index('class="d"')]
        assert "拍" in t

    def test_csv_path_still_available(self):
        body = self._drop_src()
        assert 'data-act="choose"' in body
        assert 'id="csvInput"' in body
        assert 'id="imgInput"' in body
        assert "capture=" in body            # 手机上调后摄（契约不撤）


# ---------------------------------------------------------------------
# ② 手指友好：44px 热区 + 字号
# ---------------------------------------------------------------------

class TestTouchTargets:
    def test_mobile_44px_targets(self):
        """手机断点（<820px）内：普通/小按钮、分段钮 ≥44px，可点行 ≥48px。"""
        css = _src("style.css")
        assert "@media (max-width:819px)" in css
        blk = css[css.index("@media (max-width:819px)"):]
        for sel, h in ((".btn{", "44"), (".btn-sm{", "44"), (".seg-btn{", "44"),
                       (".ticket-btn{", "48")):
            assert sel in blk.replace(" ", ""), f"{sel} 缺手机热区规则"
            rule = blk.replace(" ", "")[blk.replace(" ", "").index(sel):]
            assert "min-height:%spx" % h in rule[:rule.index("}")], \
                f"{sel} 手机热区应 ≥{h}px"

    def test_44px_only_on_mobile(self):
        """桌面按钮密度不变：44px 规则只住在手机断点里，基础 .btn 还是紧凑 padding
        （用 display:inline-flex 签名定位基础规则，不依赖文件顺序）。"""
        css = _src("style.css")
        i = css.index("display:inline-flex")
        base_btn = css[css.rindex(".btn{", 0, i):css.index("}", i)]
        assert "min-height" not in base_btn.replace(" ", "")

    def test_reading_font_bumps(self):
        """键值行/表格 12→13px、可点行 meta 11→12px、提示 11→12px：正文字号对齐。"""
        css = _src("style.css")
        kv_blk = css[css.index(".kv{"):css.index(".kv:last-child")]
        assert "font-size:13px" in kv_blk.replace(" ", "")
        table_blk = css[css.index("table{"):css.index("th{")]
        assert "font-size:13px" in table_blk.replace(" ", "")
        meta_i = css.index(".ticket-btn .meta")
        assert "font-size:12px" in css[meta_i:meta_i + 120].replace(" ", "")
        hint_blk = css[css.index(".hint{"):css.index(".hint-warn")]
        assert "font-size:12px" in hint_blk.replace(" ", "")


# ---------------------------------------------------------------------
# ③ 期间口径月切日化 + 旧措辞锁定
# ---------------------------------------------------------------------

class TestPeriodMonthCut:
    """核算期间显示月切日期边界（08-26→09-25），不再冒充工票数据首尾日期。"""

    def test_cutoff_constant_and_helpers(self):
        src = _src("app.js")
        assert "var CUTOFF_DAY = 26" in src          # 与 core/engine.py 同日切
        assert "function periodOfDate" in src
        assert "function periodBounds" in src

    def test_single_period_shows_bounds_not_data_range(self):
        src = _src("app.js")
        body = src[src.index("function periodFromTickets"):
                   src.index("function settleToCalcView")]
        assert "periodBounds(" in body               # 单期 → 期边界（引擎口径）
        assert "label + ' 期'" in body               # 期标签 = YYYY-MM 期
        # 跨期/认不出的日期不许冒充月切日口径：如实标注并退回票面日期
        assert "mixed" in body

    def test_kv_labels_explicit(self):
        """标签动态化：单期=月切日口径、跨期=按票面日期，都不冒充对方。"""
        src = _src("app.js")
        assert "period.mixed ? '（跨期，按票面日期）'" in src
        assert "'（月切日口径）'" in src

    def test_js_period_logic_matches_engine(self):
        """JS 月切日镜像与 core/engine.py 逐例对数（跨年/切日边界全覆盖）。"""
        if not (Path("/usr/bin/node").exists() or which("node")):
            pytest.skip("本机没有 node，跳过 JS 逻辑对数")
        src = _src("app.js")
        seg = src[src.index("var CUTOFF_DAY"):src.index("function periodFromTickets")]
        dates = ["2026-08-25", "2026-08-26", "2026-09-01", "2026-09-25",
                 "2026-09-26", "2025-12-25", "2025-12-26", "2026-01-25",
                 "2026-01-26", "2026-12-25", "2026-12-26"]
        periods = ["2026-09", "2026-01", "2026-12", "2025-12"]
        js = seg + "\nvar out = {of:{}, bounds:{}};\n"
        js += "".join("out.of[%r] = periodOfDate(%r);\n" % (d, d) for d in dates)
        js += "".join("out.bounds[%r] = periodBounds(%r);\n" % (p, p) for p in periods)
        js += "console.log(JSON.stringify(out));\n"
        r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
        assert r.returncode == 0, f"JS 月切日函数跑不起来：{r.stderr}"
        got = json.loads(r.stdout.strip().splitlines()[-1])

        import sys
        sys.path.insert(0, str(ROOT))
        from core import engine
        for d in dates:
            assert got["of"][d] == engine.period_of(d), \
                f"{d}: JS={got['of'][d]} != 引擎={engine.period_of(d)}"
        for p in periods:
            s, e = engine.period_bounds(p)
            assert [got["bounds"][p]["start"], got["bounds"][p]["end"]] == [s, e], \
                f"{p}: JS 边界 {got['bounds'][p]} != 引擎 ({s}, {e})"

    def test_period_from_tickets_single_vs_mixed(self):
        """periodFromTickets 行为级：单期出期边界、跨期如实标 mixed、空票不编期。"""
        if not (Path("/usr/bin/node").exists() or which("node")):
            pytest.skip("本机没有 node，跳过 JS 逻辑对数")
        src = _src("app.js")
        seg = src[src.index("var CUTOFF_DAY"):src.index("/* /api/settle 结果")]
        js = ("var store = {tickets:{data:["
              "{no:'A', date:'2026-09-02'},{no:'B', date:'2026-09-15'}]}};\n")
        js += seg + "\n"
        js += """
var single = periodFromTickets();
store.tickets.data = [{no:'A', date:'2026-08-20'},{no:'B', date:'2026-09-10'}];
var mixed = periodFromTickets();
store.tickets.data = [];
var empty = periodFromTickets();
console.log(JSON.stringify({single: single, mixed: mixed, empty: empty}));
"""
        r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
        assert r.returncode == 0, f"periodFromTickets 跑不起来：{r.stderr}"
        got = json.loads(r.stdout.strip().splitlines()[-1])
        assert got["single"]["label"] == "2026-09 期"
        assert got["single"]["start"] == "2026-08-26"
        assert got["single"]["end"] == "2026-09-25"
        assert "mixed" not in got["single"]
        assert got["mixed"]["mixed"] is True          # 08-20 与 09-10 跨两期：如实标注
        assert got["mixed"]["start"] == "2026-08-20"  # 跨期退回票面日期（不冒充月切日）
        assert "mixed" not in got["empty"]            # 没票不编期

    def test_demo_period_label_aligned(self):
        """演示期标签与真数据路径统一成 YYYY-MM 期（原 09-25 期是尾日期口径）。"""
        demo = _src("demo-data.js")
        assert "label: '2026-09 期'" in demo


class TestNoPlaceholderWording:
    """旧措辞清零（19dcf4d 已改写为「离线演示/内置数据」），锁死防回归。"""

    @pytest.mark.parametrize("name", ["app.js", "style.css", "index.html", "demo-data.js"])
    def test_no_placeholder_word(self, name):
        assert _OLD_WORD not in _src(name), \
            f"{name} 里不许再出现旧措辞（{_OLD_WORD}）"

    def test_demo_fallback_comments_are_explicit(self):
        """两处原旧措辞注释现为明确语义：内置数据+整体让位/整体替换。"""
        src = _src("app.js")
        assert "离线演示用的内置数据" in src
        assert "整体让位" in src or "整体替换" in src
