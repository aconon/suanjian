# -*- coding: utf-8 -*-
"""任务单 05 第 2/3 件测试（TDD 先行）：持久化路由 + OCR 接线。

覆盖（契约 = 任务单 05，前端并行施工照此对接）：
- POST /api/tickets/save：存票+行（行经引擎校验，坏行 400 人话）；同票号重存=覆盖
- GET /api/tickets：列表含行（数值=精确字符串），空态 {"tickets": []}
- POST /api/settle：核算照旧 + 落 settlements（期间自动归期：全行同期→期标签，
  缺日期/跨期→""；team 可显式带 period）；写操作 audit 留痕
- GET /api/history：按期聚合（期降序、未归期殿后、金额 Decimal 精确求和）
- POST /api/ocr：{image_base64, mime} → 假 vision（monkeypatch，零网络）；
  未配置→400「未配置识别服务，请用 CSV 导入」；坏 base64/空图/坏 mime→400；
  图>8MB→413；上游失败→502 人话；临时文件用完即删；超时 60s 传参正确

所有 DB 测试走 SUANJIAN_HOME→tmp，绝不碰 data/suanjian.db 真库。
"""
import base64
import json
import os
import threading
import urllib.error
from decimal import Decimal

import pytest

import server
from core import db

# ---------------------------------------------------------------------
# 基建：真服务器（照 test_server.py 模式）+ SUANJIAN_HOME 重定向
# ---------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SUANJIAN_HOME", str(tmp_path))
    yield tmp_path
    db.reset_thread_conn()


@pytest.fixture()
def srv(home, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getfqdn", lambda h="": "localhost")
    httpd = server.make_server("127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield httpd.server_address
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=3)


def http_json(addr, method, path, obj=None):
    import http.client
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=10)
    try:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8") if obj is not None else None
        conn.request(method, path, body=body,
                     headers={"Content-Type": "application/json"} if obj is not None else {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, (json.loads(data.decode("utf-8")) if data else None)
    finally:
        conn.close()


def write_config(home, obj):
    (home / "config.json").write_text(
        json.dumps(obj, ensure_ascii=False), encoding="utf-8")


ROWS_A = [
    {"process": "冲压", "name": "王建国", "qty": "100",
     "unit_price": "0.35", "defect": "1", "date": "2026-09-10"},
    {"process": "焊接", "name": "李秀英", "qty": "80",
     "unit_price": "0.5", "defect": "0", "date": "2026-09-11"},
]

VISION_OK_TEXT = (
    '识别结果：\n{"sheet_no": "GP-101", "workshop": "一车间", "date": "2026-09-10", '
    '"rows": [{"process": "冲压", "name": "王建国", "qty": 100, '
    '"unit_price": 0.35, "defect": 1}]}')


# =====================================================================
# 工票存取
# =====================================================================

class TestTicketsSave:
    def test_save_ok(self, srv):
        status, out = http_json(srv, "POST", "/api/tickets/save", {
            "sheet_no": "GP-001", "workshop": "一车间", "date": "2026-09-10",
            "rows": ROWS_A, "csv": "原始CSV"})
        assert status == 200
        assert out["sheet_no"] == "GP-001"
        assert isinstance(out["id"], int) and out["created_at"]

    def test_save_then_list_roundtrip(self, srv):
        http_json(srv, "POST", "/api/tickets/save", {
            "sheet_no": "GP-001", "workshop": "一车间", "date": "2026-09-10",
            "rows": ROWS_A, "csv": "原始CSV"})
        status, out = http_json(srv, "GET", "/api/tickets")
        assert status == 200
        assert len(out["tickets"]) == 1
        t = out["tickets"][0]
        assert t["sheet_no"] == "GP-001" and t["workshop"] == "一车间"
        assert t["rows"] == ROWS_A                       # 行完整往返
        assert t["rows"][0]["qty"] == "100"              # 数值=精确字符串
        assert t["csv"] == "原始CSV"

    def test_resave_same_sheet_no_overwrites(self, srv):
        http_json(srv, "POST", "/api/tickets/save",
                  {"sheet_no": "GP-001", "rows": ROWS_A, "csv": "v1"})
        _, out2 = http_json(srv, "POST", "/api/tickets/save",
                            {"sheet_no": "GP-001", "rows": ROWS_A[:1], "csv": "v2"})
        _, listed = http_json(srv, "GET", "/api/tickets")
        assert len(listed["tickets"]) == 1               # 不造第二行
        assert listed["tickets"][0]["csv"] == "v2"
        assert listed["tickets"][0]["id"] == out2["id"]

    def test_missing_sheet_no_400(self, srv):
        status, out = http_json(srv, "POST", "/api/tickets/save",
                                {"rows": ROWS_A})
        assert status == 400 and "票号" in out["error"]

    def test_empty_sheet_no_400(self, srv):
        status, out = http_json(srv, "POST", "/api/tickets/save",
                                {"sheet_no": "  ", "rows": ROWS_A})
        assert status == 400 and "票号" in out["error"]

    def test_bad_date_400(self, srv):
        status, out = http_json(srv, "POST", "/api/tickets/save",
                                {"sheet_no": "GP-9", "date": "2026/09/10", "rows": ROWS_A})
        assert status == 400 and "日期" in out["error"]

    def test_rows_missing_400(self, srv):
        status, out = http_json(srv, "POST", "/api/tickets/save",
                                {"sheet_no": "GP-9"})
        assert status == 400 and "rows" in out["error"]

    def test_rows_bad_row_400_with_engine_msg(self, srv):
        bad = [{"process": "冲压", "name": "王建国", "qty": "-5", "unit_price": "0.35"}]
        status, out = http_json(srv, "POST", "/api/tickets/save",
                                {"sheet_no": "GP-9", "rows": bad})
        assert status == 400 and "负" in out["error"]

    def test_rows_unknown_field_400(self, srv):
        bad = [{"process": "冲压", "name": "王建国", "qty": "5",
                "unit_price": "0.35", "单价": "x"}]
        status, out = http_json(srv, "POST", "/api/tickets/save",
                                {"sheet_no": "GP-9", "rows": bad})
        assert status == 400 and "字段" in out["error"]

    def test_audit_trail_written(self, srv):
        http_json(srv, "POST", "/api/tickets/save",
                  {"sheet_no": "GP-001", "rows": ROWS_A})
        row = db.ensure().execute(
            "SELECT actor,action,target FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        assert row["action"] == "save_ticket" and row["target"] == "GP-001"


class TestTicketsList:
    def test_empty(self, srv):
        status, out = http_json(srv, "GET", "/api/tickets")
        assert status == 200 and out == {"tickets": []}

    def test_newest_first(self, srv):
        http_json(srv, "POST", "/api/tickets/save", {"sheet_no": "GP-1", "rows": ROWS_A})
        http_json(srv, "POST", "/api/tickets/save", {"sheet_no": "GP-2", "rows": ROWS_A})
        _, out = http_json(srv, "GET", "/api/tickets")
        assert [t["sheet_no"] for t in out["tickets"]] == ["GP-2", "GP-1"]

    def test_rows_and_csv_optional(self, srv):
        http_json(srv, "POST", "/api/tickets/save",
                  {"sheet_no": "GP-BARE", "rows": ROWS_A})
        _, out = http_json(srv, "GET", "/api/tickets")
        assert out["tickets"][0]["csv"] == ""            # 没传 csv → 空串


# =====================================================================
# 结算落库 + 按期历史
# =====================================================================

class TestSettlePersists:
    def test_individual_same_period_labeled(self, srv):
        status, out = http_json(srv, "POST", "/api/settle", {
            "mode": "individual", "rows": ROWS_A})
        assert status == 200
        rows = db.list_settlements(db.ensure())
        assert len(rows) == 1
        assert rows[0]["period"] == "2026-09"            # 两行日期同期 → 自动归期
        assert rows[0]["mode"] == "individual"
        assert rows[0]["result"] == out                  # 结果 JSON 原样落库

    def test_individual_missing_date_unlabeled(self, srv):
        rows = [{"process": "冲压", "name": "王建国", "qty": "10", "unit_price": "0.35"}]
        http_json(srv, "POST", "/api/settle", {"mode": "individual", "rows": rows})
        assert db.list_settlements(db.ensure())[0]["period"] == ""

    def test_individual_cross_period_unlabeled(self, srv):
        rows = ROWS_A + [{"process": "冲压", "name": "王建国", "qty": "10",
                          "unit_price": "0.35", "date": "2026-09-26"}]  # 归 2026-10 期
        http_json(srv, "POST", "/api/settle", {"mode": "individual", "rows": rows})
        assert db.list_settlements(db.ensure())[0]["period"] == ""

    def test_team_period_from_body(self, srv):
        members = [{"name": "王建国", "qty": "100"}, {"name": "李秀英", "qty": "80"}]
        http_json(srv, "POST", "/api/settle", {
            "mode": "team", "team_total": "500", "period": "2026-09",
            "rows": members})
        s = db.list_settlements(db.ensure())[0]
        assert s["period"] == "2026-09" and s["mode"] == "team"

    def test_team_no_period_unlabeled(self, srv):
        members = [{"name": "王建国", "qty": "100"}]
        http_json(srv, "POST", "/api/settle", {
            "mode": "team", "team_total": "500", "rows": members})
        assert db.list_settlements(db.ensure())[0]["period"] == ""

    def test_team_bad_period_400(self, srv):
        members = [{"name": "王建国", "qty": "100"}]
        status, out = http_json(srv, "POST", "/api/settle", {
            "mode": "team", "team_total": "500", "period": "2026-9",
            "rows": members})
        assert status == 400 and "核算期" in out["error"]

    def test_failed_settle_not_persisted(self, srv):
        http_json(srv, "POST", "/api/settle", {"mode": "individual", "rows": [
            {"process": "冲压", "name": "王建国", "qty": "abc", "unit_price": "0.35"}]})
        assert db.list_settlements(db.ensure()) == []    # 400 不落库

    def test_settle_audit_trail(self, srv):
        http_json(srv, "POST", "/api/settle",
                  {"mode": "individual", "rows": ROWS_A})
        row = db.ensure().execute(
            "SELECT action FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        assert row["action"] == "settle"


class TestHistory:
    def test_aggregates_by_period_desc(self, srv):
        http_json(srv, "POST", "/api/settle", {"mode": "individual", "rows": ROWS_A})
        http_json(srv, "POST", "/api/settle", {
            "mode": "individual", "rows": [
                {"process": "冲压", "name": "赵铁柱", "qty": "40",
                 "unit_price": "0.25", "date": "2026-08-20"}]})   # 2026-08 期
        status, out = http_json(srv, "GET", "/api/history")
        assert status == 200
        assert [i["period"] for i in out["items"]] == ["2026-09", "2026-08"]  # 新期在前

    def test_sums_amounts_across_settlements(self, srv):
        _, first = http_json(srv, "POST", "/api/settle",
                             {"mode": "individual", "rows": ROWS_A})
        _, second = http_json(srv, "POST", "/api/settle", {
            "mode": "individual", "rows": [
                {"process": "冲压", "name": "王建国", "qty": "10",
                 "unit_price": "0.35", "date": "2026-09-12"}]})
        _, out = http_json(srv, "GET", "/api/history")
        item = out["items"][0]
        assert item["period"] == "2026-09"
        assert item["count"] == 2
        assert Decimal(item["total_amount"]) == (
            Decimal(first["total_amount"]) + Decimal(second["total_amount"]))

    def test_team_amount_uses_allocated_total(self, srv):
        members = [{"name": "王建国", "qty": "100"}, {"name": "李秀英", "qty": "80"}]
        _, res = http_json(srv, "POST", "/api/settle", {
            "mode": "team", "team_total": "500.03", "period": "2026-09",
            "rows": members})
        _, out = http_json(srv, "GET", "/api/history")
        item = out["items"][0]
        assert item["total_amount"] == res["allocated_total"]  # Σ分摊==总额
        assert "team" in item["modes"]

    def test_unlabeled_period_last(self, srv):
        http_json(srv, "POST", "/api/settle", {"mode": "individual", "rows": ROWS_A})
        http_json(srv, "POST", "/api/settle", {"mode": "team", "team_total": "1",
                                               "rows": [{"name": "x", "qty": "1"}]})
        _, out = http_json(srv, "GET", "/api/history")
        assert [i["period"] for i in out["items"]] == ["2026-09", ""]

    def test_empty(self, srv):
        status, out = http_json(srv, "GET", "/api/history")
        assert status == 200 and out == {"items": []}


# =====================================================================
# OCR 接线
# =====================================================================

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64   # 假 PNG 字节（vision 是桩，不真解码）
PNG_1PX = base64.b64encode(PNG_BYTES).decode()


class TestOcrConfigMissing:
    def test_no_config_file_400_exact_msg(self, srv):
        status, out = http_json(srv, "POST", "/api/ocr", {
            "image_base64": PNG_1PX, "mime": "image/png"})
        assert status == 400
        assert out["error"] == "未配置识别服务，请用 CSV 导入"

    def test_config_without_ocr_section_400(self, srv, home):
        write_config(home, {"other": {}})
        status, out = http_json(srv, "POST", "/api/ocr", {
            "image_base64": PNG_1PX, "mime": "image/png"})
        assert status == 400
        assert out["error"] == "未配置识别服务，请用 CSV 导入"

    def test_config_empty_key_400(self, srv, home):
        write_config(home, {"ocr": {"base_url": "https://ai.example/v1"}})
        status, out = http_json(srv, "POST", "/api/ocr", {
            "image_base64": PNG_1PX, "mime": "image/png"})
        assert status == 400
        assert out["error"] == "未配置识别服务，请用 CSV 导入"

    def test_config_malformed_json_400(self, srv, home):
        (home / "config.json").write_text("{oops", encoding="utf-8")
        status, out = http_json(srv, "POST", "/api/ocr", {
            "image_base64": PNG_1PX, "mime": "image/png"})
        assert status == 400
        assert out["error"] == "未配置识别服务，请用 CSV 导入"


class TestOcrHappy:
    def fake_vision(self, monkeypatch, text=VISION_OK_TEXT):
        from core import ocr_pipe
        calls = {}

        def _fake(image_path, api_key, base_url, model="glm-5.3-flash", timeout=120):
            calls.update(path=image_path, key=api_key, base_url=base_url,
                         model=model, timeout=timeout,
                         blob=open(image_path, "rb").read())
            return text

        monkeypatch.setattr(ocr_pipe, "call_flash_vision", _fake)
        return calls

    def test_ok(self, srv, home, monkeypatch):
        write_config(home, {"ocr": {"key": "sk-test",
                                    "base_url": "https://ai.example/v1"}})
        calls = self.fake_vision(monkeypatch)
        status, out = http_json(srv, "POST", "/api/ocr", {
            "image_base64": PNG_1PX, "mime": "image/png"})
        assert status == 200
        assert out["sheet"]["sheet_no"] == "GP-101"
        assert out["sheet"]["workshop"] == "一车间"
        assert out["sheet"]["date"] == "2026-09-10"
        assert out["sheet"]["rows"][0]["qty"] == "100"
        assert out["sheet"]["rows"][0]["unit_price"] == "0.35"
        assert out["needs_review"] is False
        assert out["errors"] == []
        # 传参正确：key/base_url 来自 config，超时 60s，MIME 按扩展名
        assert calls["key"] == "sk-test"
        assert calls["base_url"] == "https://ai.example/v1"
        assert calls["timeout"] == 60
        assert calls["path"].endswith(".png")
        assert calls["blob"] == PNG_BYTES                  # 解码后的原始字节
        assert not os.path.exists(calls["path"])           # 临时文件用完即删

    def test_needs_review_propagates(self, srv, home, monkeypatch):
        write_config(home, {"ocr": {"key": "k", "base_url": "https://x/v1"}})
        text = ('{"sheet_no": "[?]", "workshop": "", "date": "", '
                '"rows": [{"process": "冲压", "name": "王建国", "qty": "[?]", '
                '"unit_price": 0.35, "defect": 0}]}')
        self.fake_vision(monkeypatch, text)
        status, out = http_json(srv, "POST", "/api/ocr", {
            "image_base64": PNG_1PX, "mime": "image/png"})
        assert status == 200
        assert out["needs_review"] is True

    def test_model_override_from_config(self, srv, home, monkeypatch):
        write_config(home, {"ocr": {"key": "k", "base_url": "https://x/v1",
                                    "model": "other-vision"}})
        calls = self.fake_vision(monkeypatch)
        http_json(srv, "POST", "/api/ocr",
                  {"image_base64": PNG_1PX, "mime": "image/png"})
        assert calls["model"] == "other-vision"

    def test_data_url_prefix_tolerated(self, srv, home, monkeypatch):
        write_config(home, {"ocr": {"key": "k", "base_url": "https://x/v1"}})
        calls = self.fake_vision(monkeypatch)
        status, out = http_json(srv, "POST", "/api/ocr", {
            "image_base64": "data:image/png;base64," + PNG_1PX,
            "mime": "image/png"})
        assert status == 200 and out["sheet"]["sheet_no"] == "GP-101"


class TestOcrValidation:
    def test_missing_image_400(self, srv, home):
        write_config(home, {"ocr": {"key": "k", "base_url": "https://x/v1"}})
        status, out = http_json(srv, "POST", "/api/ocr", {"mime": "image/png"})
        assert status == 400 and "image_base64" in out["error"]

    def test_bad_base64_400(self, srv, home):
        write_config(home, {"ocr": {"key": "k", "base_url": "https://x/v1"}})
        status, out = http_json(srv, "POST", "/api/ocr", {
            "image_base64": "!!!不是base64!!!", "mime": "image/png"})
        assert status == 400 and "base64" in out["error"]

    def test_empty_image_400(self, srv, home):
        write_config(home, {"ocr": {"key": "k", "base_url": "https://x/v1"}})
        # 空串在第一道必填校验就拦下（合法非空 base64 解不出 0 字节）
        status, out = http_json(srv, "POST", "/api/ocr", {
            "image_base64": "", "mime": "image/png"})
        assert status == 400 and "image_base64" in out["error"]

    def test_bad_mime_400(self, srv, home):
        write_config(home, {"ocr": {"key": "k", "base_url": "https://x/v1"}})
        status, out = http_json(srv, "POST", "/api/ocr", {
            "image_base64": PNG_1PX, "mime": "application/pdf"})
        assert status == 400 and "格式" in out["error"]

    def test_image_over_8mb_413(self, srv, home):
        write_config(home, {"ocr": {"key": "k", "base_url": "https://x/v1"}})
        big = base64.b64encode(b"x" * (8 * 1024 * 1024 + 1)).decode()
        status, out = http_json(srv, "POST", "/api/ocr", {
            "image_base64": big, "mime": "image/png"})
        assert status == 413 and "8MB" in out["error"]

    def test_body_over_ocr_cap_413(self, srv, home):
        write_config(home, {"ocr": {"key": "k", "base_url": "https://x/v1"}})
        big = base64.b64encode(b"x" * (9 * 1024 * 1024)).decode()  # b64≈12MB>11MB 上限
        # 服务端看 Content-Length 即回 413 收线；裸 socket 声明全长只发一小段，
        # 全量 sendall 会撞服务端收线后的 BrokenPipe（与既有 413 测试同法）
        import socket as _socket
        head = ("POST /api/ocr HTTP/1.1\r\nHost: localhost\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: %d\r\n\r\n" % (len(big) + 60)).encode()
        payload = head + b'{"image_base64":"' + big[:4096].encode()
        sock = _socket.create_connection(srv, timeout=5)
        try:
            sock.sendall(payload)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
            assert buf.startswith(b"HTTP/1.1 413")
        finally:
            sock.close()


class TestOcrUpstreamFailure:
    def test_network_error_502_human_msg(self, srv, home, monkeypatch):
        write_config(home, {"ocr": {"key": "k", "base_url": "https://x/v1"}})
        from core import ocr_pipe

        def _boom(path, key, base_url, model="m", timeout=60):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(ocr_pipe, "call_flash_vision", _boom)
        status, out = http_json(srv, "POST", "/api/ocr", {
            "image_base64": PNG_1PX, "mime": "image/png"})
        assert status == 502
        assert "CSV" in out["error"]                     # 人话：给得出路

    def test_http_error_502_mentions_code(self, srv, home, monkeypatch):
        write_config(home, {"ocr": {"key": "k", "base_url": "https://x/v1"}})
        from core import ocr_pipe

        def _auth_fail(path, key, base_url, model="m", timeout=60):
            raise urllib.error.HTTPError(
                "url", 401, "Unauthorized", hdrs=None, fp=None)

        monkeypatch.setattr(ocr_pipe, "call_flash_vision", _auth_fail)
        status, out = http_json(srv, "POST", "/api/ocr", {
            "image_base64": PNG_1PX, "mime": "image/png"})
        assert status == 502 and "401" in out["error"]
