# -*- coding: utf-8 -*-
"""任务 09：设置页数据备份（TDD 先行）——GET /api/backup。

契约 = docs/任务单/09-打印工资条与数据备份.md：
- GET /api/backup：把 data/suanjian.db 的三表全量（tickets/settlements/audit_log）
  + 全部工票的原始 CSV 留底打成一个 .json 附件，浏览器直接下载
- 文件名带时间戳（suanjian-backup-YYYYMMDD-HHMMSS.json；中文真名 算件-备份-…）
- 口令门照进（/api/* 除 health 全拦，含 backup）
- audit 留痕（action=backup）
- 内容往返：下载的 JSON 解析回来，票/结算/留痕与库内一致，CSV 留底可取回

红线：config.json（口令+识别密钥）绝不进备份——备份文件跟着人走，密钥不能跟着走。
"""
import http.client
import json
import re
import socket
import threading
from urllib.parse import unquote

import pytest

import server
from core import db


# ---------------------------------------------------------------------
# 基建：真服务器 + SUANJIAN_HOME→tmp（照 test_export.py 模式）
# ---------------------------------------------------------------------

@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SUANJIAN_HOME", str(tmp_path))
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


def http_get(addr, path, x_code="__unset__"):
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=5)
    try:
        headers = {}
        if x_code != "__unset__":
            headers["X-Code"] = x_code
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp, data
    finally:
        conn.close()


def http_post_json(addr, path, body):
    conn = http.client.HTTPConnection(addr[0], addr[1], timeout=5)
    try:
        conn.request("POST", path,
                     body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, data
    finally:
        conn.close()


CSV_TEXT = ("票号,车间,日期,工序,姓名,数量,单价,废品\n"
            "SJ-B001,冲压车间,2026-09-12,冲压,王建国,100,0.35,1")


def seed_data(addr):
    """造一套有代表性的数据：带 CSV 留底的票 + 个人/班组各一笔结算。"""
    status, data = http_post_json(addr, "/api/tickets/save", {
        "sheet_no": "SJ-B001", "workshop": "冲压车间", "date": "2026-09-12",
        "rows": [{"process": "冲压", "name": "王建国", "qty": "100",
                  "unit_price": "0.35", "defect": "1"}],
        "csv": CSV_TEXT})
    assert status == 200, data
    assert http_post_json(addr, "/api/settle", {
        "mode": "individual", "rows": [
            {"process": "冲压", "name": "王建国", "qty": "100",
             "unit_price": "0.35", "defect": "1", "date": "2026-09-12"}]})[0] == 200
    assert http_post_json(addr, "/api/settle", {
        "mode": "team", "team_total": "90", "period": "2026-09",
        "rows": [{"name": "李秀英", "qty": "2"}, {"name": "钱二", "qty": "1"}]})[0] == 200


def write_config(home, obj):
    (home / "config.json").write_text(
        json.dumps(obj, ensure_ascii=False), encoding="utf-8")


# =====================================================================
# 基本响应：附件头 + 时间戳文件名
# =====================================================================

class TestBackupBasics:
    def test_ok_and_content_headers(self, srv):
        resp, data = http_get(srv, "/api/backup")
        assert resp.status == 200
        ctype = resp.getheader("Content-Type") or ""
        assert "application/json" in ctype
        disp = resp.getheader("Content-Disposition") or ""
        assert "attachment" in disp
        assert resp.getheader("Content-Length") == str(len(data))  # keep-alive 定界

    def test_ascii_filename_with_timestamp(self, srv):
        _, data = http_get(srv, "/api/backup")
        disp = http_get(srv, "/api/backup")[0].getheader("Content-Disposition")
        m = re.search(r'filename="(suanjian-backup-(\d{8}-\d{6})\.json)"', disp)
        assert m, "ASCII 名要带时间戳：suanjian-backup-YYYYMMDD-HHMMSS.json"
        stamp = m.group(2)
        assert re.fullmatch(r"\d{8}-\d{6}", stamp)

    def test_utf8_filename_chinese(self, srv):
        resp, _ = http_get(srv, "/api/backup")
        disp = resp.getheader("Content-Disposition") or ""
        assert "filename*=UTF-8''" in disp
        star = disp.split("filename*=UTF-8''", 1)[1]
        assert unquote(star) == "算件-备份-%s.json" % re.search(
            r"(\d{8}-\d{6})", disp).group(1)

    def test_empty_db_still_backs_up(self, srv):
        """空库也能备份（备份的是文件不是数据量），返回空三表。"""
        resp, data = http_get(srv, "/api/backup")
        assert resp.status == 200
        pkg = json.loads(data.decode("utf-8"))
        assert pkg["db"]["tickets"] == []
        assert pkg["db"]["settlements"] == []
        assert pkg["db"]["audit_log"] == []
        assert pkg["csvs"] == {}

    def test_body_is_valid_utf8_json(self, srv):
        seed_data(srv)
        _, data = http_get(srv, "/api/backup")
        pkg = json.loads(data.decode("utf-8"))  # 坏 JSON / 非 UTF-8 → 抛（红）
        assert pkg["app"] == "suanjian"
        assert pkg["kind"] == "backup"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
                            pkg["generated_at"])


# =====================================================================
# 内容往返：下载的 JSON 与库内一致
# =====================================================================

class TestBackupRoundTrip:
    def test_tickets_round_trip(self, srv):
        seed_data(srv)
        _, data = http_get(srv, "/api/backup")
        pkg = json.loads(data.decode("utf-8"))
        tickets = pkg["db"]["tickets"]
        assert len(tickets) == 1
        t = tickets[0]
        assert t["sheet_no"] == "SJ-B001"
        assert t["workshop"] == "冲压车间"
        assert t["date"] == "2026-09-12"
        assert t["rows"][0]["name"] == "王建国"
        assert t["rows"][0]["qty"] == "100"          # 精确字符串原样
        assert t["rows"][0]["unit_price"] == "0.35"
        assert t["created_at"]

    def test_settlements_round_trip(self, srv):
        seed_data(srv)
        _, data = http_get(srv, "/api/backup")
        pkg = json.loads(data.decode("utf-8"))
        sts = pkg["db"]["settlements"]
        assert len(sts) == 2
        by_mode = {s["mode"]: s for s in sts}
        ind = by_mode["individual"]
        assert ind["period"] == "2026-09"             # 自动归期口径与库一致
        assert ind["result"]["total_amount"] == "34.65"
        team = by_mode["team"]
        assert team["period"] == "2026-09"
        assert team["result"]["allocated_total"] == "90.00"

    def test_audit_log_included(self, srv):
        seed_data(srv)
        _, data = http_get(srv, "/api/backup")
        pkg = json.loads(data.decode("utf-8"))
        actions = [a["action"] for a in pkg["db"]["audit_log"]]
        assert "save_ticket" in actions
        assert "settle" in actions

    def test_csvs_carried(self, srv):
        """全部 CSV 留底按票号可取回（任务单：打包 data/suanjian.db + 全部 CSV）。"""
        seed_data(srv)
        _, data = http_get(srv, "/api/backup")
        pkg = json.loads(data.decode("utf-8"))
        assert pkg["csvs"]["SJ-B001"] == CSV_TEXT

    def test_counts_match(self, srv):
        seed_data(srv)
        _, data = http_get(srv, "/api/backup")
        pkg = json.loads(data.decode("utf-8"))
        c = pkg["counts"]
        assert c["tickets"] == len(pkg["db"]["tickets"]) == 1
        assert c["settlements"] == len(pkg["db"]["settlements"]) == 2
        assert c["audit_log"] == len(pkg["db"]["audit_log"]) >= 3
        assert c["csvs"] == 1

    def test_config_secrets_never_in_backup(self, srv, home):
        """红线：config.json（口令+识别密钥）绝不进备份。"""
        seed_data(srv)                     # 先造数据（还没设口令，POST 不用带码）
        write_config(home, {"code": "9527ab",
                            "ocr": {"key": "sk-SECRET-KEY", "base_url": "http://x"}})
        _, data = http_get(srv, "/api/backup", x_code="9527ab")
        assert b"sk-SECRET-KEY" not in data
        assert b"9527ab" not in data
        pkg = json.loads(data.decode("utf-8"))
        # 顶层结构就是白名单本身：多余的自由文本段（config/code）根本不存在
        assert set(pkg.keys()) == {"app", "kind", "version", "generated_at",
                                   "counts", "db", "csvs"}


# =====================================================================
# 口令门 + 留痕 + keep-alive
# =====================================================================

class TestBackupGateAndAudit:
    def test_gate_401_without_code(self, srv, home):
        seed_data(srv)
        write_config(home, {"code": "9527ab"})
        resp, data = http_get(srv, "/api/backup")
        assert resp.status == 401
        assert "口令" in json.loads(data.decode("utf-8"))["error"]

    def test_gate_passes_with_code(self, srv, home):
        seed_data(srv)
        write_config(home, {"code": "9527ab"})
        resp, _ = http_get(srv, "/api/backup", x_code="9527ab")
        assert resp.status == 200

    def test_audit_trail_written(self, srv):
        seed_data(srv)
        http_get(srv, "/api/backup")
        row = db.ensure().execute(
            "SELECT actor,action FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["action"] == "backup"
        assert row["actor"] == "user"

    def test_backup_audit_row_not_in_its_own_file(self, srv):
        """快照先取、留痕后写：这条 backup 留痕进下一个备份，不进自己（确定性）。"""
        seed_data(srv)
        _, first = http_get(srv, "/api/backup")
        n1 = json.loads(first.decode("utf-8"))["counts"]["audit_log"]
        _, second = http_get(srv, "/api/backup")
        pkg2 = json.loads(second.decode("utf-8"))
        assert pkg2["counts"]["audit_log"] == n1 + 1
        assert pkg2["db"]["audit_log"][-1]["action"] == "backup"

    def test_keepalive_usable_after_download(self, srv):
        """附件响应也必须带准 Content-Length：同连接下一请求不串包。"""
        seed_data(srv)
        conn = http.client.HTTPConnection(srv[0], srv[1], timeout=5)
        try:
            conn.request("GET", "/api/backup")
            resp = conn.getresponse()
            body = resp.read()
            assert resp.status == 200
            assert int(resp.getheader("Content-Length")) == len(body)
            conn.request("GET", "/api/health")
            resp2 = conn.getresponse()
            assert resp2.status == 200
            assert b'"ok"' in resp2.read()
        finally:
            conn.close()
