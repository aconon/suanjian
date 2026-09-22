# -*- coding: utf-8 -*-
"""health 端点版本与运行信息契约测试（收尾三小件之二）。

契约：
- GET /api/health 在既有 {"ok":true,"service":"suanjian"} 之上增量返回：
  {"version": APP_VERSION（X.Y.Z）、"uptime_seconds": 进程已活秒数（int ≥ 0）、
   "started_at": 进程启动时刻 ISO（本机时区，秒精度）}
- version 单一事实源 = server.APP_VERSION（发版改这一处）
- 口令门豁免不受影响（开着门 health 也不问口令）
"""
import datetime
import json
import re
import socket
import threading
import time

import http.client

import pytest

import server
from core import db


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SUANJIAN_HOME", str(tmp_path))
    monkeypatch.delenv("SUANJIAN_LAN", raising=False)
    yield tmp_path
    db.reset_thread_conn()


@pytest.fixture()
def srv(home, monkeypatch):
    monkeypatch.setattr(socket, "getfqdn", lambda h="": "localhost")
    server._code_gate_reset()
    httpd = server.make_server("127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield httpd.server_address
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=3)


def get_health(addr):
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=5)
    try:
        conn.request("GET", "/api/health")
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode("utf-8"))
    finally:
        conn.close()


class TestHealthFields:
    def test_existing_fields_kept(self, srv):
        status, body = get_health(srv)
        assert status == 200
        assert body["ok"] is True
        assert body["service"] == "suanjian"

    def test_version_field(self, srv):
        _, body = get_health(srv)
        assert body["version"] == server.APP_VERSION
        assert re.fullmatch(r"\d+\.\d+\.\d+", body["version"])

    def test_version_constant_pinned(self):
        """版本常量钉死：改版本必须动这一处（防手滑漂移）。"""
        assert server.APP_VERSION == "1.5.0"

    def test_uptime_seconds_field(self, srv):
        _, body = get_health(srv)
        assert isinstance(body["uptime_seconds"], int)
        assert body["uptime_seconds"] >= 0

    def test_started_at_iso(self, srv):
        _, body = get_health(srv)
        started = datetime.datetime.fromisoformat(body["started_at"])  # 解析得了才是 ISO
        assert started.year >= 2026

    def test_uptime_consistent_with_started_at(self, srv):
        """uptime 与 started_at 同源同钟：now - started ≈ uptime（±2s 容差）。"""
        _, body = get_health(srv)
        started = datetime.datetime.fromisoformat(body["started_at"])
        delta = (datetime.datetime.now() - started).total_seconds()
        assert abs(delta - body["uptime_seconds"]) <= 2

    def test_uptime_ticks(self, srv):
        _, b1 = get_health(srv)
        time.sleep(1.05)
        _, b2 = get_health(srv)
        assert b2["uptime_seconds"] > b1["uptime_seconds"]   # 真的在走
        assert b2["uptime_seconds"] - b1["uptime_seconds"] >= 1

    def test_gate_exempt_with_new_fields(self, srv, home):
        """口令门开着，health 仍豁免且带全量字段（前端可用性探测依赖豁免）。"""
        (home / "config.json").write_text(
            json.dumps({"code": "9527ab"}), encoding="utf-8")
        status, body = get_health(srv)
        assert status == 200
        assert body["ok"] is True
        assert body["version"] == server.APP_VERSION
        assert "uptime_seconds" in body
        assert "started_at" in body

    def test_query_string_ignored(self, srv):
        conn = http.client.HTTPConnection(srv[0], srv[1], timeout=5)
        try:
            conn.request("GET", "/api/health?probe=1")
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            assert resp.status == 200
            assert body["version"] == server.APP_VERSION
        finally:
            conn.close()
