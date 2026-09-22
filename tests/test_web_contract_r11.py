# -*- coding: utf-8 -*-
"""web/ 前端契约测试 · 修复单 R11（F3 注释口径 / F4 批内多票同败齐报 / F5 拆行残留）。

沿用 R9/R10 三层防线：node --check 保语法、源码断言锁契约、node 实跑抠出
落库段与 saveFail 真身（替身 api / store / render 观测行为）。

- F3：「每批 6 个＝浏览器并发上限」过度承诺 → 校准为「主流浏览器为 6，
       部分 WebKit 为 4，超出仅排队」
- F4：批内多票同败只报首票（Promise.all 抢跑）→ allSettled 语义收集全部
       失败票号一并报；重试幂等口径不变；批间串行、失败停后续批次不变
- F5：saveFail 里 store.import 拆行残留（全文同款语句均单行）→ 顺手规范
"""
import json
import subprocess
from pathlib import Path
from shutil import which

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"


def _src(name):
    return (WEB / name).read_text(encoding="utf-8")


def _node():
    if not (Path("/usr/bin/node").exists() or which("node")):
        pytest.skip("本机没有 node，跳过 JS 检查")
    return True


def _node_run(js):
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, "JS 跑不起来：%s" % r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _confirm_seg():
    """confirmImport 整段（saveFail + 分批落库都在其中）。"""
    src = _src("app.js")
    return src[src.index("function confirmImport"):src.index("function readFile")]


def _save_seg():
    """从 app.js 抠出「按票号聚合→分批落库」整段（锚点与 R10 同款）。"""
    src = _src("app.js")
    i = src.index("/* 按票号聚成整票再存")
    j = src.index("}, saveFail);", i) + len("}, saveFail);")
    return src[i:j]


def _run_saves(n_tickets, fail_map=None):
    """node 实跑落库段：替身 api 按 fail_map={票号: 报错文案} 定点失败。"""
    _node()
    head = (
        "var events = [], calls = [], timeouts = [];\n"
        "var active = 0, peak = 0;\n"
        "var FAIL_MAP = __FAIL_MAP__;\n"
        "var api = function (path, opts) {\n"
        "  var no = JSON.parse(opts.body).sheet_no;\n"
        "  if (path !== '/tickets/save') throw new Error('bad path: ' + path);\n"
        "  timeouts.push(opts.timeoutMs);\n"
        "  active += 1; if (active > peak) peak = active;\n"
        "  calls.push(no);\n"
        "  events.push(['start', no]);\n"
        "  return new Promise(function (resolve, reject) {\n"
        "    setTimeout(function () {\n"
        "      active -= 1; events.push(['end', no]);\n"
        "      if (FAIL_MAP[no]) {\n"
        "        var e = new Error(FAIL_MAP[no]); e.status = 500; reject(e);\n"
        "      } else { resolve({}); }\n"
        "    }, 5);\n"
        "  });\n"
        "};\n"
        "var saveRows = [];\n"
        "for (var i = 1; i <= __N__; i++) {\n"
        "  var no = 'SJ-' + (i < 10 ? '0' + i : '' + i);\n"
        "  saveRows.push({ no: no, workshop: '冲压', date: '2026-09-12',\n"
        "    process: '冲压', name: '张三', qty: '100', unit_price: '0.35',\n"
        "    defect: '0' });\n"
        "}\n"
        "var viewRows = saveRows.map(function (r) { return { no: r.no }; });\n"
        "var doneCalled = null, failCalled = null;\n"
        "var done = function (x) { doneCalled = x; };\n"
        "var saveFail = function (err) {\n"
        "  failCalled = { sheetNo: err.sheetNo, sheetNos: err.sheetNos,\n"
        "    status: err.status, msg: err.message };\n"
        "};\n"
        "var mergeImported = function (rows) { return { count: rows.length }; };\n"
        "var runSaves = function () {\n"
    ).replace("__FAIL_MAP__", json.dumps(fail_map or {})).replace("__N__", str(n_tickets))
    tail = (
        "\n};\n"
        "runSaves().then(function () {\n"
        "  setTimeout(function () {\n"
        "    console.log(JSON.stringify({ peak: peak, calls: calls,\n"
        "      timeouts: timeouts, events: events, doneCalled: doneCalled,\n"
        "      failCalled: failCalled }));\n"
        "  }, 40);\n"
        "}, function (e) { console.log('REJECTED ' + (e && e.message)); });\n"
    )
    return _node_run(head + _save_seg() + tail)


def _run_real_save_fail(err_json):
    """node 实跑 app.js 里的 saveFail 真身（验用户可见文案）。"""
    _node()
    src = _src("app.js")
    seg = src[src.index("var saveFail = function"):src.index("return api('/ingest/csv'")]
    js = (
        "var store = {}, rendered = 0, currentRoute = '#/tickets';\n"
        "var render = function () { rendered += 1; };\n"
        "var imp = { fileName: '车间9月.csv' };\n"
        + seg +
        "saveFail(__ERR__);\n"
        "console.log(JSON.stringify({ store: store, rendered: rendered }));\n"
    ).replace("__ERR__", json.dumps(err_json, ensure_ascii=False))
    return _node_run(js)


class TestR11Syntax:
    def test_app_js_passes_node_check(self):
        _node()
        r = subprocess.run(["node", "--check", str(WEB / "app.js")],
                           capture_output=True, text=True)
        assert r.returncode == 0, "app.js 语法挂了：%s" % r.stderr


# =====================================================================
# F3：注释口径校准（不再宣称「＝浏览器并发上限」）
# =====================================================================

class TestF3CommentCaliber:
    def test_batch_comment_states_calibrated_concurrency(self):
        """分批注释写明：主流浏览器为 6、部分 WebKit 为 4、超出仅排队。"""
        src = _src("app.js")
        assert "部分 WebKit 为 4" in src, "缺「部分 WebKit 为 4」校准说明"
        assert "主流浏览器" in src and "仅排队" in src, "缺「超出仅排队」口径"

    def test_overclaim_equals_browser_limit_gone(self):
        """旧口径「＝浏览器对同主机的 HTTP/1.1 并发上限」必须清掉。"""
        assert "=浏览器对同主机的 HTTP/1.1 并发上限" not in _src("app.js"), \
            "还在宣称批大小恒等于浏览器并发上限（WebKit 下不成立）"


# =====================================================================
# F4：批内多票同败 → 收集全部失败票号一并报（allSettled 语义）
# =====================================================================

class TestF4AllSettledContract:
    def test_batch_uses_allsettled_not_racing_all(self):
        """批内齐发改 allSettled：不再被 Promise.all 首个拒绝抢跑截胡。"""
        seg = _save_seg()
        assert "Promise.allSettled(" in seg, "批内没用 allSettled（多败只报首票）"
        assert "Promise.all(nos.map" not in seg, "旧 Promise.all 抢跑语义还在"

    def test_serial_between_batches_unchanged(self):
        """批间串行 + 失败停后续批次的地基不变（R10-1 契约保持）。"""
        seg = _save_seg()
        assert "SAVE_BATCH = 6" in seg
        assert "savedAll = savedAll.then" in seg, "批间 .then 链串行没了"


class TestF4AllSettledBehavior:
    def test_multi_fail_same_batch_reports_all_sheet_nos(self):
        """同批两票同败：失败票号一并带出（旧口径只报 Promise.all 抢跑的首票）。"""
        out = _run_saves(13, fail_map={"SJ-02": "存不进", "SJ-04": "存不进"})
        assert out["failCalled"] is not None, "失败被吞（假成功）"
        assert out["failCalled"]["sheetNos"] == ["SJ-02", "SJ-04"], \
            "没收集齐失败票号：%s" % out["failCalled"]
        assert out["failCalled"]["sheetNo"] == "SJ-02", "首失败票号兼容口径丢了"
        assert out["failCalled"]["msg"] == "存不进", \
            "同文案去重失败（%r）" % out["failCalled"]["msg"]
        assert "SJ-07" not in out["calls"], "有失败还继续发后续批次"

    def test_multi_fail_distinct_messages_joined(self):
        """同批两败且文案不同：合并成一条人话（「；」分隔）。"""
        out = _run_saves(6, fail_map={"SJ-01": "日期不对", "SJ-05": "票号已锁"})
        assert out["failCalled"]["msg"] == "日期不对；票号已锁"
        assert out["failCalled"]["sheetNos"] == ["SJ-01", "SJ-05"]

    def test_single_fail_keeps_legacy_behavior(self):
        """单票失败：口径不变（带票号、报原文案、停后续批次）。"""
        out = _run_saves(13, fail_map={"SJ-07": "存不进"})
        assert out["failCalled"]["sheetNo"] == "SJ-07"
        assert out["failCalled"]["sheetNos"] == ["SJ-07"]
        assert out["failCalled"]["msg"] == "存不进"
        assert "SJ-13" not in out["calls"]

    def test_success_path_unchanged(self):
        """全成功路径不受影响：13 张全落、峰值并发 6、批间等齐。"""
        out = _run_saves(13)
        assert out["doneCalled"] == {"count": 13}
        assert out["failCalled"] is None
        assert out["peak"] == 6 and len(out["calls"]) == 13

    def test_gate_status_wins_over_other_failures(self):
        """同批混 401 与普通失败：门口径优先透传（saveFail 让路收口用）。"""
        _node()
        seg = _save_seg()
        head = (
            "var calls = [];\n"
            "var api = function (path, opts) {\n"
            "  var no = JSON.parse(opts.body).sheet_no;\n"
            "  calls.push(no);\n"
            "  return new Promise(function (resolve, reject) {\n"
            "    setTimeout(function () {\n"
            "      if (no === 'SJ-01') { var g = new Error('HTTP 401'); g.status = 401; reject(g); }\n"
            "      else if (no === 'SJ-03') { reject(new Error('存不进')); }\n"
            "      else { resolve({}); }\n"
            "    }, 5);\n"
            "  });\n"
            "};\n"
            "var saveRows = [];\n"
            "for (var i = 1; i <= 6; i++) {\n"
            "  saveRows.push({ no: 'SJ-0' + i, workshop: 'w', date: 'd',\n"
            "    process: 'p', name: 'n', qty: '1', unit_price: '1', defect: '0' });\n"
            "}\n"
            "var viewRows = saveRows.map(function (r) { return { no: r.no }; });\n"
            "var doneCalled = null, failCalled = null;\n"
            "var done = function (x) { doneCalled = x; };\n"
            "var saveFail = function (err) {\n"
            "  failCalled = { status: err.status, sheetNos: err.sheetNos };\n"
            "};\n"
            "var mergeImported = function (rows) { return { count: rows.length }; };\n"
            "var runSaves = function () {\n"
        )
        tail = (
            "\n};\n"
            "runSaves().then(function () { setTimeout(function () {\n"
            "  console.log(JSON.stringify({ failCalled: failCalled,\n"
            "    doneCalled: doneCalled }));\n"
            "}, 40); }, function (e) { console.log('REJECTED ' + e.message); });\n"
        )
        out = _node_run(head + seg + tail)
        assert out["failCalled"] is not None
        assert out["failCalled"]["status"] == 401, \
            "混败时门口径没优先透传（%s）" % out["failCalled"]
        assert out["failCalled"]["sheetNos"] == ["SJ-01", "SJ-03"]


class TestF4SaveFailMessage:
    """saveFail 真身实跑：用户可见文案把失败票号报齐。"""

    def test_multi_sheet_message_lists_all(self):
        out = _run_real_save_fail({"sheetNos": ["SJ-02", "SJ-04"],
                                   "message": "存不进", "status": 500})
        msg = out["store"]["import"]["message"]
        assert "工票 SJ-02、SJ-04 没存进库" in msg, msg
        assert "重试一遍即可（同票号会覆盖，不会重复）" in msg, "重试幂等口径变了"
        assert out["rendered"] == 1

    def test_legacy_single_sheet_message(self):
        out = _run_real_save_fail({"sheetNo": "SJ-07",
                                   "message": "服务不通"})
        msg = out["store"]["import"]["message"]
        assert "工票 SJ-07 没存进库：服务不通" in msg, msg

    def test_gate_statuses_short_circuit(self):
        out = _run_real_save_fail({"status": 401, "message": "HTTP 401"})
        assert out["store"]["import"]["mode"] == "drop", "401 应让路口令门"
        assert out["store"]["import"].get("message") is None


# =====================================================================
# F5：saveFail 里 store.import 拆行残留规范（全文同款语句均单行）
# =====================================================================

class TestF5StoreImportFormat:
    def test_store_import_not_split_mid_object(self):
        """拆行残留清除：store.import 不再在 fileName 后断行（与全文同款单行一致）。"""
        b = _confirm_seg()
        assert "fileName: imp.fileName,\n" not in b, \
            "store.import 对象字面量仍被拆行（格式残留）"
        assert "store.import = { mode: 'error', fileName: imp.fileName, message:" in b, \
            "error 态 store.import 应与其余同款单行书写"
