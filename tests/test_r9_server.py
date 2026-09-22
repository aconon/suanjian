# -*- coding: utf-8 -*-
"""修复单 R9 服务端测试（TDD 先行）：F5 日志随 SUANJIAN_HOME / F6 中文口令编码缝
/ F7 X-Code 限速（军机处 5 错锁口径，时限 2 分钟）/ F12 端口参数全角洗白。

所有测试走 SUANJIAN_HOME→tmp，绝不碰 data/ 真库与真 config.json。
"""
import http.client
import json
import logging
import socket
import threading
import time
from urllib.parse import quote

import pytest

import server
from core import db


@pytest.fixture(autouse=True)
def _clean_gate():
    """口令失败计数/锁定表是模块级共享状态：每个用例前后都清（隔离串扰）。"""
    server._code_gate_reset()
    yield
    server._code_gate_reset()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SUANJIAN_HOME", str(tmp_path))
    monkeypatch.delenv("SUANJIAN_LAN", raising=False)
    yield tmp_path
    db.reset_thread_conn()


@pytest.fixture()
def srv(home, monkeypatch):
    monkeypatch.setattr(socket, "getfqdn", lambda h="": "localhost")
    httpd = server.make_server("127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield httpd.server_address
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=3)


def http_req(addr, method, path, body=None, x_code="__unset__"):
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=5)
    try:
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if x_code != "__unset__":
            headers["X-Code"] = x_code
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        parsed = None
        if data:
            try:
                parsed = json.loads(data.decode("utf-8"))
            except ValueError:
                parsed = data.decode("utf-8", "replace")
        return resp.status, parsed
    finally:
        conn.close()


def write_config(home, obj):
    (home / "config.json").write_text(
        json.dumps(obj, ensure_ascii=False), encoding="utf-8")


CODE = "9527ab"


# =====================================================================
# F6：中文口令编码缝——前端 encodeURIComponent + 服务端 unquote 还原
# （二选一里选了「编码对齐」：口令支持中文，不限制 ASCII——这台工具的
#   用户是家里管账的人，中文口令比「只准英文数字」更符合使用习惯）
# =====================================================================

class TestF6ChineseCode:
    def test_percent_encoded_chinese_code_passes(self, srv, home):
        """前端口径（encodeURIComponent 后发）的中文口令能进门。"""
        write_config(home, {"code": "中文口令"})
        status, _ = http_req(srv, "GET", "/api/tickets",
                             x_code=quote("中文口令"))
        assert status == 200

    def test_wrong_chinese_code_401(self, srv, home):
        write_config(home, {"code": "中文口令"})
        status, body = http_req(srv, "GET", "/api/tickets",
                                x_code=quote("中文口令x"))
        assert status == 401
        assert "口令" in body["error"]

    def test_raw_mojibake_still_401(self, srv, home):
        """未编码直发（latin-1 乱码形态）依旧进不来——不比修复前更松。"""
        write_config(home, {"code": "中文口令"})
        mojibake = "中文口令".encode("utf-8").decode("latin-1")
        status, _ = http_req(srv, "GET", "/api/tickets", x_code=mojibake)
        assert status == 401

    def test_ascii_code_plain_still_works(self, srv, home):
        """纯 ASCII 口令原样发（老客户端/curl）：unquote 无副作用，照常进门。"""
        write_config(home, {"code": CODE})
        status, _ = http_req(srv, "GET", "/api/tickets", x_code=CODE)
        assert status == 200

    def test_ascii_code_with_percent_edge(self, srv, home):
        """ASCII 口令含 % 的边角：编码发能对上；原样发（未经编码）也能对上。"""
        write_config(home, {"code": "a%20b"})
        status, _ = http_req(srv, "GET", "/api/tickets", x_code=quote("a%20b"))
        assert status == 200                       # 前端编码口径
        status, _ = http_req(srv, "GET", "/api/tickets", x_code="a%20b")
        assert status == 200                       # 老客户端原样口径


# =====================================================================
# F7：X-Code 限速——同 IP 连错 5 次锁 2 分钟（军机处 LAN 令牌口径）
# =====================================================================

class TestF7RateLimit:
    def test_four_wrong_get_remaining_hints(self, srv, home):
        write_config(home, {"code": CODE})
        for _ in range(4):
            status, body = http_req(srv, "GET", "/api/tickets", x_code="wrong")
            assert status == 401
            assert "还剩" in body["error"]

    def test_fifth_wrong_locks(self, srv, home):
        write_config(home, {"code": CODE})
        for _ in range(4):
            http_req(srv, "GET", "/api/tickets", x_code="wrong")
        status, body = http_req(srv, "GET", "/api/tickets", x_code="wrong")
        assert status == 401
        assert "锁" in body["error"]              # 第 5 错：本单即锁

    def test_locked_even_correct_code_gets_429(self, srv, home):
        """锁定期内连对口令也 429（锁定就是要让对错都进不来）。"""
        write_config(home, {"code": CODE})
        for _ in range(5):
            http_req(srv, "GET", "/api/tickets", x_code="wrong")
        status, body = http_req(srv, "GET", "/api/tickets", x_code=CODE)
        assert status == 429
        assert "锁" in body["error"]

    def test_lock_expires_automatically(self, srv, home, monkeypatch):
        monkeypatch.setattr(server, "_CODE_LOCK_SECS", 0.3)
        write_config(home, {"code": CODE})
        for _ in range(5):
            http_req(srv, "GET", "/api/tickets", x_code="wrong")
        assert http_req(srv, "GET", "/api/tickets", x_code=CODE)[0] == 429
        time.sleep(0.35)                          # 超期自动解
        assert http_req(srv, "GET", "/api/tickets", x_code=CODE)[0] == 200

    def test_success_resets_counter(self, srv, home):
        """输对一次清零：4 错 1 对，接着错会从「还剩 4 次」重新数。"""
        write_config(home, {"code": CODE})
        for _ in range(4):
            http_req(srv, "GET", "/api/tickets", x_code="wrong")
        assert http_req(srv, "GET", "/api/tickets", x_code=CODE)[0] == 200
        status, body = http_req(srv, "GET", "/api/tickets", x_code="wrong")
        assert status == 401
        assert "还剩 4" in body["error"]

    def test_gate_off_never_counts(self, srv, home):
        """门没开（无 code）不计数不受锁影响。"""
        write_config(home, {"ocr": {"key": "k"}})
        for _ in range(6):
            assert http_req(srv, "GET", "/api/tickets")[0] == 200

    def test_fail_note_semantics_with_injected_time(self):
        """模块级计数/锁定语义（注入时钟，确定性）：5 错置锁、到期自解。"""
        server._code_gate_reset()
        ip = "10.0.0.9"
        for _ in range(4):
            locked, left = server._code_fail_note(ip, now=100.0)
            assert not locked
        assert left == 1
        locked, _ = server._code_fail_note(ip, now=100.0)     # 第 5 错
        assert locked
        assert server._code_lock_state(ip, now=100.0 + 119) is True
        assert server._code_lock_state(ip, now=100.0 + 121) is False
        server._code_fail_clear(ip)                            # 输对清零
        assert server._code_lock_state(ip, now=100.0) is False


# =====================================================================
# F5：日志随 SUANJIAN_HOME——隔离实例不再把日志写进主 data/
# =====================================================================

class TestF5LoggingHome:
    def test_logs_follow_suanjian_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUANJIAN_HOME", str(tmp_path))
        names = ("sj.access", "sj.server")
        saved = [(logging.getLogger(n), logging.getLogger(n).handlers[:])
                 for n in names]
        try:
            server._setup_logging()
            for fname in ("access.log", "server.log"):
                assert (tmp_path / fname).exists(), \
                    "%s 必须落在 SUANJIAN_HOME 里" % fname
            # FileHandler 指向隔离目录，且真能接到日志
            paths = [h.baseFilename for n in names
                     for h in logging.getLogger(n).handlers
                     if isinstance(h, logging.FileHandler)]
            assert all(str(tmp_path) in p for p in paths), paths
            logging.getLogger("sj.server").info("r9-f5-probe")
            for h in logging.getLogger("sj.server").handlers:
                h.flush()
            assert "r9-f5-probe" in (tmp_path / "server.log").read_text(
                encoding="utf-8")
        finally:
            for lg, hs in saved:
                for h in lg.handlers[:]:
                    if h not in hs:
                        lg.removeHandler(h)
                        h.close()

    def test_default_home_unchanged(self, tmp_path, monkeypatch):
        """不设 SUANJIAN_HOME：日志仍落项目 data/（源码态口径不变）。"""
        import os
        monkeypatch.delenv("SUANJIAN_HOME", raising=False)
        assert server._log_dir() == os.path.join(server.BASE_DIR, "data")


# =====================================================================
# F12：命令行端口参数——全角转半角后再校验，非 ASCII 数字拒绝
# =====================================================================

class TestF12CliPort:
    def test_fullwidth_digits_normalized(self):
        """全角数字显式转半角（不是 isdigit 无声吞）。"""
        assert server._cli_port("８７７０") == 8770

    def test_ascii_digits(self):
        assert server._cli_port("8770") == 8770
        assert server._cli_port(" 8770 ") == 8770

    def test_non_ascii_or_bad_rejected(self):
        assert server._cli_port("٨٧٧٠") is None    # 阿拉伯-印度数字：isdigit 认，必须拒
        assert server._cli_port("88a0") is None
        assert server._cli_port("-1") is None
        assert server._cli_port("") is None
