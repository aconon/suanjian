# -*- coding: utf-8 -*-
"""web/ 前端契约测试 · 修复单 R10（中危 1：CSV 确认导入逐票 save 超时+分批）。

沿用 R9 的三层防线：node --check 保语法、源码断言锁契约、node 实跑抠出的
落库段对数（替身 api 观测：超时参数 / 峰值并发 / 批间串行 / 失败带票号）。

背景（R10-1）：旧实现把全部票的 /api/tickets/save 用 Promise.all 一次齐发，
队尾请求还在浏览器连接队列里排队就开始烧 api() 默认 3s 计时器——大单队尾
被假杀成「服务不通」。修复：逐票 save 放宽到 30s（与 ingest 同级）+ 改分批
（每批 6 个——主流浏览器对同主机的 HTTP/1.1 并发为 6，部分 WebKit 为 4，
超出并发上限仅排队不失败；R11-F3 口径校准、R11-F4 批内改 allSettled），
批间等齐再发下一批。
"""
import json
import subprocess
from pathlib import Path
from shutil import which

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"


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


def _save_seg():
    """从 app.js 抠出「按票号聚合→分批落库」整段（含分组，便于真路实验证）。"""
    src = _src("app.js")
    i = src.index("/* 按票号聚成整票再存")
    j = src.index("}, saveFail);", i) + len("}, saveFail);")
    return src[i:j]


def _run_saves(n_tickets, fail_no=None):
    """node 实跑落库段：替身 api 记录超时参数/并发峰值/起止事件，返回报告。"""
    _node()
    head = (
        "var events = [], calls = [], timeouts = [];\n"
        "var active = 0, peak = 0;\n"
        "var FAIL_NO = __FAIL_NO__;\n"
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
        "      if (FAIL_NO && no === FAIL_NO) {\n"
        "        var e = new Error('存不进'); e.status = 500; reject(e);\n"
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
        "  failCalled = { sheetNo: err.sheetNo, msg: err.message };\n"
        "};\n"
        "var mergeImported = function (rows) { return { count: rows.length }; };\n"
        "var runSaves = function () {\n"
    ).replace("__FAIL_NO__", json.dumps(fail_no)).replace("__N__", str(n_tickets))
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


class TestR10Item1Contract:
    """源码契约：逐票 save 的超时与分批结构锁死在文本层。"""

    def _fn(self):
        src = _src("app.js")
        return _body(src, "function confirmImport", "function readFile")

    def test_each_save_carries_30s_timeout(self):
        """逐票 save 带 timeoutMs:30000（与 ingest 同级）——默认 3s 假杀大单队尾。"""
        b = self._fn()
        i = b.index("api('/tickets/save'")
        call = b[i:b.index("throw err;", i)]   # api 调用（含 opts 与 catch 开头）
        assert "timeoutMs: 30000" in call, "逐票 save 缺 30s 超时"

    def test_no_parallel_fire_all(self):
        """并行齐发已废除：不再一次性 Promise.all(全量票)。"""
        b = self._fn()
        assert "Promise.all(saves)" not in b, \
            "还是全量齐发（Promise.all(saves)）——大单队尾会被排队期假杀"

    def test_batch_size_is_browser_concurrency_limit(self):
        """分批：批大小=6（主流浏览器同主机 HTTP/1.1 并发数），slice 切批。"""
        b = self._fn()
        assert "SAVE_BATCH = 6" in b, "批大小常量（6=主流浏览器同主机并发数）缺失"
        assert "slice(" in b, "没有按批切片"

    def test_batches_wait_via_then_chain(self):
        """批间等齐：批与批之间用 .then 链串行（不是 for 里同步全发）。

        R11-F4：批内齐发升级为 allSettled（多败齐报），串行地基不变。
        """
        b = self._fn()
        i = b.index("api('/tickets/save'")
        tail = b[i:b.index("}, saveFail);", i)] + "}, saveFail);"
        assert "Promise.allSettled(" in tail, "批内应齐发收齐（allSettled）"
        assert ".then(function () {" in tail, "批间应串行（链式 .then）"


class TestR10Item1Behavior:
    """node 实跑行为级验证（替身 api 观测真实调用模式）。"""

    def test_success_batches_of_six_serial(self):
        """13 张票：全部落库成功——峰值并发=6、批间等齐、每票 30s 超时、按序落库。"""
        out = _run_saves(13)
        assert out["doneCalled"] == {"count": 13}, "成功回调没拿到全部 13 张"
        assert out["failCalled"] is None
        assert out["calls"] == ["SJ-%02d" % i for i in range(1, 14)], \
            "落库顺序/次数不对：%s" % out["calls"]
        assert out["peak"] == 6, "峰值并发应恰为批大小 6（实际 %s）" % out["peak"]
        assert out["timeouts"] == [30000] * 13, \
            "逐票 save 超时应为 30000ms：%s" % sorted(set(out["timeouts"]))
        # 批间串行：第 7 张开始前，第 1 批 6 张必须全部结束；第 13 张同理
        seq = [(t, no) for t, no in out["events"]]
        pos = {(t, no): k for k, (t, no) in enumerate(seq)}

        def starts_after(no, priors):
            s = pos[("start", no)]
            return all(pos[("end", p)] < s for p in priors)

        batch1 = ["SJ-%02d" % i for i in range(1, 7)]
        batch2 = ["SJ-%02d" % i for i in range(7, 13)]
        assert starts_after("SJ-07", batch1), "第 2 批没等第 1 批全部落完"
        assert starts_after("SJ-13", batch2), "第 3 批没等第 2 批全部落完"

    def test_failure_reports_sheet_no_and_stops_later_batches(self):
        """第 7 张存失败：saveFail 带票号 SJ-07，后续批次不再发（不无限烧）。"""
        out = _run_saves(13, fail_no="SJ-07")
        assert out["failCalled"] is not None, "存库失败没走 saveFail（假成功）"
        assert out["failCalled"]["sheetNo"] == "SJ-07", "失败没带上具体票号"
        assert "SJ-13" not in out["calls"], "失败后不该继续发后面的批次"

    def test_small_import_single_batch(self):
        """3 张票：单批直落（分批不为小单增加额外等待轮次）。"""
        out = _run_saves(3)
        assert out["doneCalled"] == {"count": 3}
        assert out["peak"] == 3 and out["calls"] == ["SJ-01", "SJ-02", "SJ-03"]
