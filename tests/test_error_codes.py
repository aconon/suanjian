# -*- coding: utf-8 -*-
"""机器可读错误码契约测试（收尾三小件之一，照 DSH 模式）。

契约 = docs/错误码.md：
- 所有错误响应在人话 "error" 之外带稳定 "code" 字段：{"error":"人话","code":"CODE"}
- code 是增量字段：现有人话文案一字不改（红线）；成功响应不带 code 字段
- 未显式给 code 的 ApiError 按状态码查默认表；表外状态兜底 "HTTP_<status>"
- 前端可按 code 编程处理（如 GATE_LOCKED 倒计时、CSV_INVALID 高亮导入框）

所有测试走 SUANJIAN_HOME→tmp，绝不碰 data/ 真库与真 config.json。
"""
import base64
import datetime
import decimal
import http.client
import json
import socket
import threading
import urllib.error

import pytest

import server
from core import db


# ---------------------------------------------------------------------
# 基建：真服务器 + SUANJIAN_HOME 重定向（照 test_server_gate.py 模式）
# ---------------------------------------------------------------------

@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SUANJIAN_HOME", str(tmp_path))
    monkeypatch.delenv("SUANJIAN_LAN", raising=False)
    yield tmp_path
    db.reset_thread_conn()


@pytest.fixture()
def srv(home, monkeypatch):
    monkeypatch.setattr(socket, "getfqdn", lambda h="": "localhost")
    server._code_gate_reset()   # 口令失败计数是模块级共享状态：每用例从零开始
    httpd = server.make_server("127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield httpd.server_address
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=3)


def http_req(addr, method, path, body=None, ctype="application/json",
             x_code="__unset__", host=None):
    """打一个请求返回 (status, 解析后的 JSON 体)。"""
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=5)
    try:
        headers = {"Content-Type": ctype} if body is not None else {}
        if x_code != "__unset__":
            headers["X-Code"] = x_code
        if host is not None:
            headers["Host"] = host
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, json.loads(data.decode("utf-8"))
    finally:
        conn.close()


def send_raw(addr, raw: bytes) -> bytes:
    """裸 socket 发原始字节，收完整响应（411/413 等收线场景用）。"""
    s = socket.create_connection(addr, timeout=5)
    try:
        s.sendall(raw)
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
            if b"\r\n\r\n" in data:
                head, _, body = data.partition(b"\r\n\r\n")
                cl = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        cl = int(line.split(b":", 1)[1].strip())
                if len(body) >= cl:
                    break
        return data
    finally:
        s.close()


def raw_json(resp: bytes) -> dict:
    """从裸响应字节里抠 JSON 体。"""
    _, _, body = resp.partition(b"\r\n\r\n")
    return json.loads(body.decode("utf-8"))


def write_config(home, obj):
    (home / "config.json").write_text(
        json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def jpost(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


CODE = "9527ab"


# ---------------------------------------------------------------------
# 错误体形状：人话不变 + code 增量
# ---------------------------------------------------------------------

class TestErrorShape:
    def test_unknown_api_404(self, srv):
        status, body = http_req(srv, "GET", "/api/nope")
        assert status == 404
        assert "没有这个接口" in body["error"]       # 人话原样
        assert body["code"] == "NOT_FOUND"

    def test_bad_json(self, srv):
        status, body = http_req(srv, "POST", "/api/settle", b"not json{")
        assert status == 400
        assert "JSON" in body["error"]
        assert body["code"] == "INVALID_JSON"

    def test_json_array_not_object(self, srv):
        status, body = http_req(srv, "POST", "/api/settle", b"[1,2]")
        assert status == 400
        assert "JSON 对象" in body["error"]
        assert body["code"] == "INVALID_JSON"

    def test_generic_400_default(self, srv):
        """引擎校验类 400（ValueError 路径）拿默认 BAD_REQUEST。"""
        status, body = http_req(srv, "POST", "/api/settle",
                                jpost({"rows": [], "mode": "individual"}))
        assert status == 400
        assert "rows" in body["error"] or "数组" in body["error"]
        assert body["code"] == "BAD_REQUEST"

    def test_csv_header_missing(self, srv):
        csv_text = "工号,备注\n1,xx\n"
        status, body = http_req(srv, "POST", "/api/ingest/csv",
                                jpost({"csv": csv_text}))
        assert status == 400
        assert "表头" in body["error"]
        assert body["code"] == "CSV_INVALID"

    def test_csv_row_bad_number(self, srv):
        csv_text = "工序,姓名,数量,单价\n冲压,张三,abc,1.5\n"
        status, body = http_req(srv, "POST", "/api/ingest/csv",
                                jpost({"csv": csv_text}))
        assert status == 400
        assert "第 2 行" in body["error"]
        assert body["code"] == "CSV_INVALID"

    def test_csv_not_utf8(self, srv):
        status, body = http_req(srv, "POST", "/api/ingest/csv",
                                "工序,姓名\n冲压,张三\n".encode("gbk"),
                                ctype="text/csv")
        assert status == 400
        assert "UTF-8" in body["error"]
        assert body["code"] == "CSV_INVALID"

    def test_host_forbidden_403(self, srv):
        status, body = http_req(srv, "GET", "/api/tickets", host="evil.example.com")
        assert status == 403
        assert "本机" in body["error"]
        assert body["code"] == "FORBIDDEN"

    def test_body_too_large_413(self, srv):
        """声明超限 Content-Length → 413 + PAYLOAD_TOO_LARGE（服务端收线）。"""
        resp = send_raw(
            srv,
            b"POST /api/settle HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2097152\r\n\r\n{}")
        head = resp.split(b"\r\n", 1)[0]
        assert b" 413 " in head
        assert raw_json(resp)["code"] == "PAYLOAD_TOO_LARGE"

    def test_chunked_411(self, srv):
        resp = send_raw(
            srv,
            b"POST /api/settle HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
        head = resp.split(b"\r\n", 1)[0]
        assert b" 411 " in head
        assert raw_json(resp)["code"] == "LENGTH_REQUIRED"

    def test_internal_500(self, srv, monkeypatch):
        def boom():
            raise RuntimeError("炸了")
        monkeypatch.setattr(server, "api_list_tickets", boom)
        status, body = http_req(srv, "GET", "/api/tickets")
        assert status == 500
        assert body["error"] == "内部错误：请稍后重试（详情已记入服务端日志）"
        assert body["code"] == "INTERNAL_ERROR"

    def test_decimal_overflow_400(self, srv, monkeypatch):
        def boom(body):
            raise decimal.InvalidOperation("取整越界")
        monkeypatch.setattr(server, "api_settle", boom)
        status, resp = http_req(srv, "POST", "/api/settle",
                                jpost({"mode": "individual", "rows": []}))
        assert status == 400
        assert "数字" in resp["error"]
        assert resp["code"] == "NUMERIC_OVERFLOW"

    def test_success_has_no_code(self, srv):
        """成功响应不带 code 字段（code 只属于错误）。"""
        status, body = http_req(srv, "GET", "/api/health")
        assert status == 200
        assert "code" not in body


# ---------------------------------------------------------------------
# 口令门错误码（401 / 429）——文案红线：一字不改
# ---------------------------------------------------------------------

class TestGateCodes:
    def test_wrong_code_401(self, srv, home):
        write_config(home, {"code": CODE})
        status, body = http_req(srv, "GET", "/api/tickets", x_code="wrong")
        assert status == 401
        assert body["error"] == "口令不对，还剩 4 次机会"   # 人话原样（首次失败）
        assert body["code"] == "GATE_CODE_REQUIRED"

    def test_missing_code_401(self, srv, home):
        write_config(home, {"code": CODE})
        status, body = http_req(srv, "GET", "/api/tickets")
        assert status == 401
        assert "口令" in body["error"]
        assert body["code"] == "GATE_CODE_REQUIRED"

    def test_lock_429(self, srv, home):
        """连错 5 次锁定：第 5 次仍是 401，第 6 次起 429 + GATE_LOCKED。"""
        write_config(home, {"code": CODE})
        for i in range(5):
            status, body = http_req(srv, "GET", "/api/tickets", x_code="bad%d" % i)
            assert status == 401
            assert body["code"] == "GATE_CODE_REQUIRED"
        status, body = http_req(srv, "GET", "/api/tickets", x_code="bad5")
        assert status == 429
        assert "锁" in body["error"]
        assert body["code"] == "GATE_LOCKED"


# ---------------------------------------------------------------------
# OCR 错误码（未配置 400 / 上游 502）
# ---------------------------------------------------------------------

class TestOcrCodes:
    def _img_body(self):
        return jpost({"image_base64": base64.b64encode(b"fakedata").decode(),
                      "mime": "image/png"})

    def test_ocr_unconfigured(self, srv, home):
        write_config(home, {"ocr": {}})   # 有段没 key/base_url
        status, body = http_req(srv, "POST", "/api/ocr", self._img_body())
        assert status == 400
        assert body["error"] == server.OCR_UNCONFIGURED_MSG
        assert body["code"] == "OCR_UNCONFIGURED"

    def test_ocr_upstream_http_error(self, srv, home, monkeypatch):
        write_config(home, {"ocr": {"key": "k", "base_url": "http://ocr.invalid.test/v1"}})

        def upstream_fail(*a, **kw):
            raise urllib.error.HTTPError("u", 500, "Internal", None, None)
        monkeypatch.setattr(server.ocr_pipe, "call_flash_vision", upstream_fail)
        status, body = http_req(srv, "POST", "/api/ocr", self._img_body())
        assert status == 502
        assert "识别服务返回错误（HTTP 500）" in body["error"]
        assert body["code"] == "OCR_UPSTREAM_ERROR"

    def test_ocr_upstream_network_error(self, srv, home, monkeypatch):
        write_config(home, {"ocr": {"key": "k", "base_url": "http://ocr.invalid.test/v1"}})

        def upstream_fail(*a, **kw):
            raise ConnectionError("连不上")
        monkeypatch.setattr(server.ocr_pipe, "call_flash_vision", upstream_fail)
        status, body = http_req(srv, "POST", "/api/ocr", self._img_body())
        assert status == 502
        assert "识别服务没连上" in body["error"]
        assert body["code"] == "OCR_UPSTREAM_ERROR"


# ---------------------------------------------------------------------
# 默认码表（单元）：ApiError 不带 code 时的兜底口径
# ---------------------------------------------------------------------

class TestCodeMap:
    def test_default_by_status(self):
        assert server.ApiError(400, "x").code == "BAD_REQUEST"
        assert server.ApiError(401, "x").code == "GATE_CODE_REQUIRED"
        assert server.ApiError(403, "x").code == "FORBIDDEN"
        assert server.ApiError(404, "x").code == "NOT_FOUND"
        assert server.ApiError(411, "x").code == "LENGTH_REQUIRED"
        assert server.ApiError(413, "x").code == "PAYLOAD_TOO_LARGE"
        assert server.ApiError(429, "x").code == "RATE_LIMITED"
        assert server.ApiError(500, "x").code == "INTERNAL_ERROR"
        assert server.ApiError(502, "x").code == "BAD_GATEWAY"

    def test_explicit_code_wins(self):
        e = server.ApiError(400, "x", code="CSV_INVALID")
        assert e.status == 400
        assert e.msg == "x"
        assert e.code == "CSV_INVALID"

    def test_unknown_status_fallback(self):
        assert server._code_for_status(418) == "HTTP_418"
        assert server.ApiError(418, "x").code == "HTTP_418"

    def test_code_is_always_str(self):
        for status in (400, 401, 404, 429, 500):
            assert isinstance(server.ApiError(status, "x").code, str)
