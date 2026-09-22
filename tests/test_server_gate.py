# -*- coding: utf-8 -*-
"""任务单 07 测试（TDD 先行）：口令门 + 绑定模式（SUANJIAN_LAN）。

契约 = docs/任务单/07-口令门与公网模式.md：
- data/config.json 顶层 {"code": "xxxxxx"} 启用口令门；所有 /api/*（除 /api/health）
  校验 header X-Code，错/缺 → 401；静态页照常放行；无 code 字段=不启口令门（向后兼容）
- SUANJIAN_LAN=1 → 绑 0.0.0.0（默认仍 127.0.0.1）；启动日志打印实际绑定与模式
- 红线：口令值不得出现在任何响应体里

所有测试走 SUANJIAN_HOME→tmp，绝不碰 data/ 真库与真 config.json。
"""
import http.client
import json
import socket
import threading

import pytest

import server
from core import db


# ---------------------------------------------------------------------
# 基建：真服务器（照 test_server_db.py 模式）+ SUANJIAN_HOME 重定向
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
    server._code_gate_reset()   # 口令失败计数是模块级共享状态：每用例从零开始（R9-F7 起有计数/锁定）
    httpd = server.make_server("127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield httpd.server_address
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=3)


def http_req(addr, method, path, body=None, x_code="__unset__", host=None):
    """打一个请求；x_code='__unset__' 表示不带 X-Code 头（区分空值）。"""
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=5)
    try:
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if x_code != "__unset__":
            headers["X-Code"] = x_code
        if host is not None:
            headers["Host"] = host  # 伪造 Host 头（Host 白名单用例）
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        parsed = None
        if data:
            try:
                parsed = json.loads(data.decode("utf-8"))
            except ValueError:
                parsed = data.decode("utf-8", "replace")
        return resp.status, parsed, resp.getheader("Content-Type")
    finally:
        conn.close()


def write_config(home, obj):
    (home / "config.json").write_text(
        json.dumps(obj, ensure_ascii=False), encoding="utf-8")


CODE = "9527ab"


# ---------------------------------------------------------------------
# 口令门：401 / 放行 / health 豁免 / 静态放行
# ---------------------------------------------------------------------

class TestGate:
    def test_no_config_gate_off(self, srv):
        """没有 config.json = 不启口令门（本机模式向后兼容）。"""
        status, body, _ = http_req(srv, "GET", "/api/tickets")
        assert status == 200
        assert body == {"tickets": []}

    def test_no_code_field_gate_off(self, srv, home):
        """config 只有别的字段（如 ocr 段）→ 口令门不启。"""
        write_config(home, {"ocr": {"key": "k", "base_url": "http://x"}})
        status, _, _ = http_req(srv, "GET", "/api/payslip?gross=8000")
        assert status == 200

    def test_blank_code_gate_off(self, srv, home):
        """code 为空串/纯空白 = 没设口令。"""
        for bad in ("", "   "):
            write_config(home, {"code": bad})
            status, _, _ = http_req(srv, "GET", "/api/tickets")
            assert status == 200, "code=%r 应视为未启用" % bad

    def test_nonstring_code_gate_off(self, srv, home):
        """code 不是字符串（如数字）→ 不启口令门，不炸。"""
        write_config(home, {"code": 123456})
        status, _, _ = http_req(srv, "GET", "/api/tickets")
        assert status == 200

    def test_missing_header_401(self, srv, home):
        write_config(home, {"code": CODE})
        status, body, _ = http_req(srv, "GET", "/api/tickets")
        assert status == 401
        assert "口令" in body["error"]

    def test_wrong_header_401(self, srv, home):
        write_config(home, {"code": CODE})
        status, body, _ = http_req(srv, "GET", "/api/tickets", x_code="wrong")
        assert status == 401
        assert "口令" in body["error"]

    def test_right_header_passes(self, srv, home):
        write_config(home, {"code": CODE})
        status, body, _ = http_req(srv, "GET", "/api/tickets", x_code=CODE)
        assert status == 200
        assert body == {"tickets": []}

    def test_post_gate_401(self, srv, home):
        """POST 路由同样进门。"""
        write_config(home, {"code": CODE})
        status, _, _ = http_req(srv, "POST", "/api/settle",
                                body=json.dumps({"rows": [], "mode": "individual"}).encode())
        assert status == 401

    def test_health_exempt(self, srv, home):
        """口令门开着，/api/health 也不问口令（前端可用性探测用）。"""
        write_config(home, {"code": CODE})
        status, body, _ = http_req(srv, "GET", "/api/health")
        assert status == 200
        assert body["ok"] is True

    def test_static_exempt(self, srv, home):
        """静态页照常放行（前端先进口令输入页）。"""
        write_config(home, {"code": CODE})
        status, body, ctype = http_req(srv, "GET", "/")
        assert status == 200
        assert "text/html" in ctype
        assert "算件" in body

    def test_gate_beats_404_for_unknown_api(self, srv, home):
        """未知 /api 路径也先进门：401 优先于 404（不泄露接口清单）。"""
        write_config(home, {"code": CODE})
        status, _, _ = http_req(srv, "GET", "/api/nope")
        assert status == 401

    def test_code_value_never_in_response(self, srv, home):
        """红线：口令值不出现在 401 响应体里。"""
        write_config(home, {"code": CODE})
        status, body, _ = http_req(srv, "GET", "/api/tickets")
        assert status == 401
        assert CODE not in json.dumps(body, ensure_ascii=False)

    def test_code_change_takes_effect_immediately(self, srv, home):
        """口令按请求即时读盘：改 config 不用重启服务。"""
        write_config(home, {"code": "aaa"})
        assert http_req(srv, "GET", "/api/tickets", x_code="aaa")[0] == 200
        write_config(home, {"code": "bbb"})
        assert http_req(srv, "GET", "/api/tickets", x_code="aaa")[0] == 401
        assert http_req(srv, "GET", "/api/tickets", x_code="bbb")[0] == 200

    def test_corrupt_config_does_not_crash(self, srv, home):
        """config.json 坏 JSON → 记日志按未启用处理，服务不炸。"""
        (home / "config.json").write_text("{not json", encoding="utf-8")
        status, _, _ = http_req(srv, "GET", "/api/tickets")
        assert status == 200

    def test_401_keeps_keepalive_usable(self, srv, home):
        """塔罗坑回归：401 早退必须先读掉 body，否则 keep-alive 下一请求串包。"""
        write_config(home, {"code": CODE})
        conn = http.client.HTTPConnection(srv[0], srv[1], timeout=5)
        try:
            body = json.dumps({"rows": [], "mode": "individual"}).encode()
            conn.request("POST", "/api/settle", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            assert resp.status == 401
            resp.read()
            # 同一条连接上跟一个 GET：能正常解析 = body 已被服务端收掉
            conn.request("GET", "/api/health")
            resp2 = conn.getresponse()
            data = resp2.read()
            assert resp2.status == 200
            assert b'"ok"' in data
        finally:
            conn.close()


# ---------------------------------------------------------------------
# 绑定模式：SUANJIAN_LAN 环境切换 + 启动横幅
# ---------------------------------------------------------------------

class TestLanBind:
    def test_default_localhost(self, home, monkeypatch):
        monkeypatch.delenv("SUANJIAN_LAN", raising=False)
        assert server.bind_host() == "127.0.0.1"

    def test_lan_one_binds_all_interfaces(self, home, monkeypatch):
        monkeypatch.setenv("SUANJIAN_LAN", "1")
        assert server.bind_host() == "0.0.0.0"

    def test_lan_truthy_variants(self, home, monkeypatch):
        for v in ("1", "true", "YES", "On"):
            monkeypatch.setenv("SUANJIAN_LAN", v)
            assert server.bind_host() == "0.0.0.0", "SUANJIAN_LAN=%r 应开局域网" % v

    def test_lan_falsy_or_garbage_stays_local(self, home, monkeypatch):
        for v in ("", "0", "false", "2", "随便"):
            monkeypatch.setenv("SUANJIAN_LAN", v)
            assert server.bind_host() == "127.0.0.1", "SUANJIAN_LAN=%r 不应开局域网" % v

    def test_banner_local_mode(self, home, monkeypatch):
        monkeypatch.delenv("SUANJIAN_LAN", raising=False)
        text = server.startup_banner("127.0.0.1", 8770)
        assert "127.0.0.1" in text and "8770" in text
        assert "本机" in text

    def test_banner_lan_mode_with_phone_url(self, home, monkeypatch):
        monkeypatch.setenv("SUANJIAN_LAN", "1")
        text = server.startup_banner("0.0.0.0", 8770, lan_ip="192.168.1.50")
        assert "0.0.0.0" in text
        assert "局域网" in text
        assert "http://192.168.1.50:8770/" in text

    def test_banner_lan_without_code_warns(self, home, monkeypatch):
        """局域网模式 + 未设口令 → 横幅必须提醒（安全兜底）。"""
        monkeypatch.setenv("SUANJIAN_LAN", "1")
        text = server.startup_banner("0.0.0.0", 8770)
        assert "口令" in text

    def test_banner_lan_with_code_no_warning(self, home, monkeypatch):
        monkeypatch.setenv("SUANJIAN_LAN", "1")
        write_config(home, {"code": CODE})
        text = server.startup_banner("0.0.0.0", 8770)
        assert CODE not in text          # 红线：口令值不进横幅/日志
        assert "还没设口令" not in text


# ---------------------------------------------------------------------
# Host 白名单 × LAN 模式：手机用局域网 IP 访问必须放行，域名仍拒
# ---------------------------------------------------------------------

class TestHostWhitelistLan:
    def test_local_mode_blocks_lan_ip_host(self, srv):
        """本机模式回归：非白名单 Host 仍 403（现状不许破坏）。"""
        status, body, _ = http_req(srv, "GET", "/api/health", host="192.168.1.50")
        assert status == 403

    def test_lan_mode_allows_ip_host(self, srv, home, monkeypatch):
        """LAN 模式：手机经局域网 IP 访问（Host=192.168.x.x）不再 403。"""
        monkeypatch.setenv("SUANJIAN_LAN", "1")
        status, body, _ = http_req(srv, "GET", "/api/health", host="192.168.1.50")
        assert status == 200
        assert body["ok"] is True

    def test_lan_mode_allows_ipv6_host(self, srv, home, monkeypatch):
        monkeypatch.setenv("SUANJIAN_LAN", "1")
        status, _, _ = http_req(srv, "GET", "/api/health", host="[fe80::1]")
        assert status == 200

    def test_lan_mode_still_blocks_domain_host(self, srv, home, monkeypatch):
        """LAN 模式也拒域名 Host（防 DNS 重绑定的防线不撤）。"""
        monkeypatch.setenv("SUANJIAN_LAN", "1")
        status, _, _ = http_req(srv, "GET", "/api/health", host="evil.example.com")
        assert status == 403

    def test_lan_mode_gate_still_enforced(self, srv, home, monkeypatch):
        """LAN 模式 + 口令门叠加：IP Host 放行 ≠ 免口令。"""
        monkeypatch.setenv("SUANJIAN_LAN", "1")
        write_config(home, {"code": CODE})
        status, _, _ = http_req(srv, "GET", "/api/tickets", host="192.168.1.50")
        assert status == 401
        status, _, _ = http_req(srv, "GET", "/api/tickets", host="192.168.1.50", x_code=CODE)
        assert status == 200
