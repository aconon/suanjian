#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""算件 · HTTP 服务层（server.py）——标准库 ThreadingHTTPServer，零 pip 依赖。

路由（以 docs/任务单/03、05 为准）：
- POST /api/ingest/csv    CSV 文本 → PieceRecord 列表（列名容错；校验失败 400 带行号）
- POST /api/settle        {rows, mode: individual|team, team_total?, period?} → 核算结果
                           （照旧返回；同时落 settlements 底账 + audit 留痕）
- POST /api/tickets/save  {sheet_no, workshop?, date?, rows, csv?} 存票+行（同票号重存=覆盖）
- GET  /api/tickets       工票列表（含行 JSON，新票在前）
- GET  /api/history       结算底账按期聚合（期降序、未归期殿后、金额精确求和）
- POST /api/ocr           {image_base64, mime} → {sheet, needs_review, errors}
                           （key/base_url 从 data/config.json [ocr] 段读，未配置→400 人话；
                           超时 60s；图片≤8MB；上游失败→502 人话，绝不裸抛）
- GET  /api/payslip?gross=  工资条（tax_note 字段标注个税为演示口径）
- GET  /api/export/payslips[?period=YYYY-MM]  工资条 Excel 导出
                           （SpreadsheetML 2003 XML，.xls 双 sheet：汇总=全员
                           工资条、明细=该期逐结算行；金额一律 String 精确字符串；
                           period 缺省取最近已归期；无归期记录 404 人话）
- GET  /api/trend          多月趋势（近 6 期升序：总产值/报废率/人均产出，
                           按期聚合 settlements，Decimal 精确）
- GET  /api/compare[?p1=&p2=]  期间对比（任务单 10）：两期逐人对比
                           （合格数量/计件工资/实发 + 变化额/变化率），
                           新出现/消失工人单独分组；缺省=最近两期；
                           不足两期/参数不齐 → 400 人话；audit 留痕
- GET  /api/backup         数据备份（任务单 09）：三表全量 + 全部 CSV 留底
                           打成一个 .json 附件下载；文件名带时间戳；
                           config.json（口令/密钥）绝不进备份；audit 留痕
- GET  /api/health        {"ok": true, "version", "uptime_seconds", "started_at"}
- GET  /                  静态服务 web/（realpath 包含校验防穿越）

服务层五类兜底（姊妹项目同款架构，实测过的坑都规避）：
- errorhandler 兜五类：ApiError（业务可控状态码）/ ValueError·TypeError（引擎校验→400 人话）
  / ConnectionError（客户端断线静默返回）/ Exception（500+堆栈落日志）/ 日志写入失败
  （stderr 兜底，绝不遮原始异常）
- 机器可读错误码（收尾三小件之一）：所有错误响应在人话 "error" 之外带稳定
  "code" 字段（{"error":"人话","code":"NOT_FOUND"}）——前端可编程处理；
  code 是增量字段，人话文案一字不改；清单与口径 = docs/错误码.md
- _safe_rollback：路由半途炸掉时回滚线程局部连接（core/db.py 接入后挂 _TLS.conn；
  当前引擎纯函数无事务，本函数只保证任何情况下不抛）
- keep-alive 安全：HTTP/1.1 全响应带 Content-Length；chunked 请求体 411；超上限
  body 413 并收线；每请求重置 body 消费标记
- _drain_body：没收的请求体读完丢弃，防残留字节被 keep-alive 下一请求当请求行解析
  （串包）；太大/读不动/chunked/负 Content-Length 一律收线
- 日志净化：访问日志把控制字符（含 DEL）转义成 \\xNN、gross 参数脱敏，防伪造/防泄露
  （X-Code 口令头只进内存比对，绝不落日志）
- Host 白名单：只认 127.0.0.1/localhost/[::1]（防 DNS 重绑定），否则 403 收线；
  局域网模式（SUANJIAN_LAN=1）额外放行 IP 字面量 Host（手机经局域网 IP 访问），
  域名 Host 仍拒
- 口令门（任务单 07）：data/config.json 顶层 {"code":"xxxxxx"} 启用——所有 /api/*
  （除 /api/health）校验 header X-Code，错/缺 → 401；静态页放行（前端自己弹口令层）；
  无 code 字段=不启（本机模式向后兼容）；按请求即时读盘（改口令不用重启）；
  口令支持中文：前端 encodeURIComponent 编码传输，服务端 unquote 还原比对（R9-F6）；
  限速（R9-F7，成熟口径）：同 IP 连错 5 次锁 2 分钟（锁定期对口令也 429），
  输对清零、超期自动解
- 绑定模式（任务单 07）：环境变量 SUANJIAN_LAN=1 → 绑 0.0.0.0（默认仍 127.0.0.1），
  启动横幅打印实际绑定与模式，局域网模式附手机可访问地址与口令提醒
- 500 固定话术不回显异常内容；数据目录：源码态跟 server.py，打包态（sys.frozen）跟 exe

数值纪律：引擎结果经 to_dict 导出，金额/数量一律精确字符串，绝不走 float。
"""
from __future__ import annotations

import base64
import csv
import datetime
import decimal
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit
from xml.sax.saxutils import escape as _xml_escape

if getattr(sys, "frozen", False):
    # PyInstaller 单文件：__file__ 在临时解包目录（退出即删），数据/日志必须
    # 跟 exe 走；web/ 静态资源在解包目录（_MEIPASS）里
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    WEB_DIR = os.path.join(getattr(sys, "_MEIPASS", BASE_DIR), "web")
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    WEB_DIR = os.path.join(BASE_DIR, "web")
sys.path.insert(0, BASE_DIR)

from core import db, engine, ocr_pipe  # noqa: E402
from core.audit import audit  # noqa: E402

DEFAULT_PORT = 8770

# 版本单一事实源（收尾三小件之二）：/api/health 带给前端/运维看，发版只改这一处。
APP_VERSION = "1.5.0"
# 进程启动时刻：uptime_seconds 与 started_at 同源同钟（都取 time.time()），
# 二者相减永远自洽；ISO 为本机时区秒精度（单机桌面工具，跟随用户视角）。
_PROCESS_STARTED_AT = time.time()
_STARTED_AT_ISO = datetime.datetime.fromtimestamp(
    _PROCESS_STARTED_AT).isoformat(timespec="seconds")


# ------------------------------------------------------------------
# 绑定模式（任务单 07）：SUANJIAN_LAN=1 → 绑 0.0.0.0（手机经局域网访问）
# ------------------------------------------------------------------

def _lan_mode() -> bool:
    """SUANJIAN_LAN 取 1/true/yes/on（大小写不敏感）才算开，其余一律本机。"""
    return os.environ.get("SUANJIAN_LAN", "").strip().lower() in ("1", "true", "yes", "on")


def bind_host() -> str:
    """实际绑定地址：局域网模式 0.0.0.0，默认 127.0.0.1。"""
    return "0.0.0.0" if _lan_mode() else "127.0.0.1"


def _lan_ip() -> str:
    """本机局域网 IP（给手机输网址用）：UDP connect 探路由，不真发包。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _gate_code() -> str:
    """口令门的口令：data/config.json 顶层 "code" 字段（非空字符串才启用）。

    按请求即时读盘——改口令不用重启服务；文件缺失/无 code 字段/空值 = 不启口令门
    （本机模式向后兼容）。坏 JSON/类型不对：记日志后按未启用处理，服务不炸
    （口径：能改这个文件的人已经在这台机器上，不是这层门要防的人）。
    红线：口令值只进内存比对，绝不写日志/响应。
    """
    path = os.path.join(db.home_dir(), "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        return ""
    except Exception as e:
        logging.getLogger("sj.server").error(
            "config.json 读不了（口令门按未启用处理）：%s", e)
        return ""
    if not isinstance(cfg, dict):
        return ""
    code = cfg.get("code")
    if code is None:
        return ""
    if not isinstance(code, str):
        logging.getLogger("sj.server").warning(
            "config.json 的 code 要是字符串（现在的类型：%s），口令门按未启用处理",
            type(code).__name__)
        return ""
    return code.strip() or ""


# ------------------------------------------------------------------
# 口令门限速（R9-F7，照 LAN 令牌成熟口径）：同 IP 连错 5 次锁一段时间。
# 桌面单机的口令门是隐私门不是加密门，但 SUANJIAN_LAN=1 时同网设备可在线
# 穷举 6 位口令（10^6 量级可行）——限速把在线暴力试错的空间掐掉。
# 先例口径是 5 错锁 10 分钟；本机场景用户自己手滑也计入，时限收窄到 2 分钟。
# ------------------------------------------------------------------

_CODE_MAX_FAILS = 5     # 连错 5 次锁
_CODE_LOCK_SECS = 120   # 锁 2 分钟（超期自动解）
_CODE_FAILS = {}        # ip -> [失败次数, 锁到时间戳]（内存表，重启即清）
_CODE_FAILS_LOCK = threading.Lock()   # 计数读改写要原子（先例仓审查 #7 同款）


def _code_fail_note(ip: str, now=None) -> tuple:
    """记一次口令失败。返回 (locked_now, left)——达到 5 次时置锁并 True/0。

    锁过期后再错的口径（R10-3 写明+固化）：**强口径**——过期只解锁不洗计数
    （st[0] 已 >= 5），过期后再错 1 次即再次置锁 2 分钟，不重新数满 5 次。
    穷举者拿不到「每 2 分钟白嫖 4 次试错」的窗口。
    now 可注入（测试用确定性时钟）。顺手清理：表只增不清会慢泄漏
    （先例仓 _purge_lan_state 的教训），超 1024 项时先清过期条目。
    R10-5：st[1]=0 的「只错未锁」条目也算过期回收（0 < now 恒真，
    旧写法 st[1] and ... 里 0 是 falsy，这类条目永远清不掉）。
    R11-F1：清理必须跳过当前 ip——清理块在 get(ip) 之前跑，若不跳过，
    表 >1024 且该 ip 锁刚过期时条目会先被清掉、计数从 1 重数，
    「过期后再错 1 次即再锁」的强口径被 purge 撕破（穷举者白得 4 次试错）。
    """
    if now is None:
        now = time.time()
    with _CODE_FAILS_LOCK:
        if len(_CODE_FAILS) > 1024:
            for cand_ip in [c for c, cst in _CODE_FAILS.items()
                            if cst[1] < now and c != ip]:
                _CODE_FAILS.pop(cand_ip, None)
        st = _CODE_FAILS.get(ip) or [0, 0]
        st[0] += 1
        if st[0] >= _CODE_MAX_FAILS:
            st[1] = now + _CODE_LOCK_SECS
            _CODE_FAILS[ip] = st
            return True, 0
        _CODE_FAILS[ip] = st
        return False, _CODE_MAX_FAILS - st[0]


def _code_lock_state(ip: str, now=None) -> bool:
    """该 IP 是否处于口令锁定期（超期自动视为未锁）。"""
    if now is None:
        now = time.time()
    st = _CODE_FAILS.get(ip)
    return bool(st and st[1] and now < st[1])


def _code_fail_clear(ip: str) -> None:
    """输对口令：计数清零（成熟口径——成功即清，不累计历史错误）。"""
    with _CODE_FAILS_LOCK:
        _CODE_FAILS.pop(ip, None)


def _code_gate_reset() -> None:
    """测试钩子：清空口令失败/锁定表。"""
    with _CODE_FAILS_LOCK:
        _CODE_FAILS.clear()


def _lock_desc() -> str:
    """锁定时长的中文说法（由 _CODE_LOCK_SECS 推导，避免文案与配置脱节）。"""
    s = _CODE_LOCK_SECS
    if s >= 60 and s % 60 == 0:
        return "%d 分钟" % (s // 60)
    return "%d 秒" % s


MAX_BODY = 1024 * 1024  # 请求体上限 1MB（OCR 路由单独放宽，见 OCR_MAX_BODY）
OCR_MAX_BODY = 11 * 1024 * 1024   # OCR 信封上限：8MB 图 base64 后 ≈10.7MB + 余量
OCR_MAX_IMAGE = 8 * 1024 * 1024   # 图片解码后 ≤ 8MB
OCR_TIMEOUT = 60                  # vision 接口超时（秒）

TAX_NOTE = (
    "个税为演示口径：按月换算的速算表（起征 5000/月）计算，"
    "并非居民工资累计预扣法，数值仅供演示对账，不可用于真实申报。"
)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


# ------------------------------------------------------------------
# 机器可读错误码（收尾三小件之一，清单=docs/错误码.md）：每个错误响应在人话
# "error" 之外带稳定 "code" 字段，前端可编程处理（增量字段，人话一字不改）。
# 未显式给 code 的 ApiError 按 status 查默认表；表外状态兜底 "HTTP_<status>"，
# 保证任何错误响应的 code 恒为非空字符串。
# ------------------------------------------------------------------

_CODE_BY_STATUS = {
    400: "BAD_REQUEST",
    401: "GATE_CODE_REQUIRED",   # 本服务的 401 只来自口令门
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    411: "LENGTH_REQUIRED",
    413: "PAYLOAD_TOO_LARGE",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    502: "BAD_GATEWAY",
}


def _code_for_status(status: int) -> str:
    """状态码 → 默认错误码（表外状态兜底 HTTP_<status>）。"""
    return _CODE_BY_STATUS.get(status, "HTTP_%d" % status)


class ApiError(Exception):
    """业务层可控错误：status + 说人话的中文 msg + 稳定机器码 code。

    code 可省略：省略时按 status 查 _code_for_status 默认表；需要前端
    区分语义的（CSV_INVALID / OCR_UNCONFIGURED / GATE_LOCKED 等）显式传。
    """

    def __init__(self, status, msg, code=None):
        super().__init__(msg)
        self.status = status
        self.msg = msg
        self.code = code or _code_for_status(status)


# F12：Host 白名单——本服务只给本机浏览器用，防 DNS 重绑定/网页跨源打本地口
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _host_allowed(host_header) -> bool:
    """Host 头去端口后必须命中白名单（大小写不敏感；IPv6 字面量保括号）。

    局域网模式（SUANJIAN_LAN=1）额外放行 IP 字面量 Host——手机用局域网 IP
    访问时 Host 就是那个 IP；域名 Host 仍拒（DNS 重绑定防线不撤）。
    """
    if not host_header:
        return False
    host = str(host_header).strip().lower()
    if host.startswith("["):  # [::1]:8770
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.split(":", 1)[0]
    if host in _ALLOWED_HOSTS:
        return True
    if _lan_mode():
        try:  # IP 字面量（v4/v6）放行，域名仍拒；v6 摘掉括号再验
            bare = host[1:-1] if host.startswith("[") else host
            ipaddress.ip_address(bare)
            return True
        except ValueError:
            return False
    return False


# F11：日志净化公共函数（访问日志/错误日志两处共用）
_CTRL_LOG_RE = re.compile(r"[\x00-\x1f\x7f]")  # 控制字符含 DEL


def _sanitize_log(text: str) -> str:
    """日志行净化：控制字符（含 DEL \\x7f）转义成 \\\\xNN（防伪造日志行）；
    payslip 的 gross 收入参数脱敏为 *（工资数据不落访问日志）。"""
    text = _CTRL_LOG_RE.sub(lambda m: "\\x%02x" % ord(m.group()), text)
    return re.sub(r"(gross=)[^&\s]*", r"\1*", text)


_TLS = threading.local()  # 线程局部：_db() 在此挂 core/db.py 的真连接（供回滚）


def _safe_rollback():
    """路由半途炸掉时回滚线程局部连接（隐式事务挂着会污染同连接的下一请求）。

    _TLS.conn 由 _db() 挂上 core/db.py 的线程局部真连接（任务单 05 接线）；
    本函数保证任何情况下不抛、不遮调用方的原始异常。
    """
    try:
        conn = getattr(_TLS, "conn", None)
        if conn is not None:
            conn.rollback()
    except Exception as e:
        sys.stderr.write("[rollback] 回滚线程连接失败（不遮原始异常）：%s\n" % e)


def _db():
    """取本线程数据库连接（建表幂等）并挂到 _TLS，让 _safe_rollback 真正生效。"""
    conn = db.ensure()
    _TLS.conn = conn
    return conn


def errorhandler(handler, route):
    """全局异常兜底（五类兜底）：业务错→人话 JSON；引擎校验错→400；
    断管/超时不炸线程；数值越界→400；系统错→500 固定话术+堆栈落日志；
    连日志写不进→stderr 兜底。
    """
    try:
        route()
    except ApiError as e:
        _safe_rollback()
        handler._error(e.status, e.msg, e.code)
    except (ValueError, TypeError) as e:
        # 引擎对非法输入抛 ValueError（中文报错）/ TypeError（类型不对）→ 400
        _safe_rollback()
        handler._error(400, str(e))
    except decimal.DecimalException:
        # 数值越界（如幅度超限导致取整失败）→ 400 人话，不给 500
        _safe_rollback()
        handler._error(400, "数字超出可算范围（金额/数量太大或位数太多）",
                       "NUMERIC_OVERFLOW")
    except (ConnectionError, TimeoutError, socket.timeout):
        _safe_rollback()
        return  # 客户端已断开/读超时（锁屏/切页属常态），响应无处可写
    except Exception as e:
        _safe_rollback()
        traceback.print_exc()
        try:
            logging.getLogger("sj.server").error(
                "未兜住的路由异常 %s", _sanitize_log(handler.path), exc_info=True)
        except Exception as log_err:
            sys.stderr.write("[errorhandler] 连日志都写不进（盘满/权限？）：%s\n" % log_err)
        # 固定话术：不回显异常内容（内部信息不外泄），细节只在服务端日志
        handler._error(500, "内部错误：请稍后重试（详情已记入服务端日志）",
                       "INTERNAL_ERROR")


# ------------------------------------------------------------------
# CSV 摄取：列名容错 + 带行号校验
# ------------------------------------------------------------------

CSV_ALIASES = {
    "process": ("工序", "工序名", "工序名称", "process"),
    "name": ("姓名", "工人", "员工", "人员", "name"),
    "qty": ("数量", "件数", "计件数量", "数量(件)", "qty", "quantity"),
    "unit_price": ("单价", "计件单价", "单价(元)", "price", "unit_price"),
    "defect": ("废品", "报废", "报废数量", "废品数", "不良数", "defect", "scrap"),
    "date": ("日期", "工票日期", "date"),
}
FIELD_CN = {"process": "工序", "name": "姓名", "qty": "数量", "unit_price": "单价"}

_HEADER_TRANS = str.maketrans({"（": "(", "）": ")", "　": " "})


def _norm_header(cell) -> str:
    """表头单元格归一：去 BOM/首尾空白、全角括号转半角、英文小写。"""
    return str(cell).replace("\ufeff", "").strip().translate(_HEADER_TRANS).lower()


def parse_csv_records(text: str):
    """CSV 全文 → PieceRecord 列表。

    列名容错：中英文别名任选，未知列（如票号/车间）忽略；废品、日期列可整列省略。
    校验失败抛 ApiError(400)，报错带 CSV 物理行号（第 N 行；引号内换行的行按
    csv 逻辑行计，与物理行号可能有偏差）。
    """
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "CSV 内容是空的", code="CSV_INVALID")
    try:
        parsed = list(csv.reader(io.StringIO(text)))
    except csv.Error as e:
        raise ApiError(400, "CSV 解析失败：%s" % e, code="CSV_INVALID") from None
    # (物理行号, 行)：全空白行跳过；表头 = 第一个非空行
    usable = [(n, row) for n, row in enumerate(parsed, start=1)
              if any(str(c).strip() for c in row)]
    if not usable:
        raise ApiError(400, "CSV 内容是空的", code="CSV_INVALID")
    if len(usable) == 1:
        raise ApiError(400, "CSV 里只有表头，没有数据行", code="CSV_INVALID")

    header = [_norm_header(c) for c in usable[0][1]]
    idx = {}
    alias_lut = {field: {a.lower() for a in aliases}
                 for field, aliases in CSV_ALIASES.items()}
    for col, cell in enumerate(header):
        for field, names in alias_lut.items():
            if cell in names and field not in idx:
                idx[field] = col  # 同名重复列取第一个
                break
    missing = [f for f in ("process", "name", "qty", "unit_price") if f not in idx]
    if missing:
        raise ApiError(400, "CSV 表头缺必要的列：%s（认这些列名：工序/姓名/数量/单价，"
                            "废品与日期列可省）" % "、".join(FIELD_CN[f] for f in missing),
                       code="CSV_INVALID")

    records = []
    for line_no, row in usable[1:]:
        def cell(field, _row=row):
            j = idx.get(field)
            return str(_row[j]).strip() if j is not None and j < len(_row) else ""
        try:
            records.append(engine.PieceRecord(
                process=cell("process"), name=cell("name"),
                qty=cell("qty"), unit_price=cell("unit_price"),
                defect=cell("defect") or 0, date=cell("date") or None))
        except (ValueError, TypeError) as e:
            raise ApiError(400, "第 %d 行导不进去：%s" % (line_no, e),
                           code="CSV_INVALID") from None
    return records


def api_ingest_csv(text: str) -> dict:
    records = parse_csv_records(text)
    return {"count": len(records), "records": [r.to_dict() for r in records]}


def _settle_period(mode: str, body: dict, records=None) -> str:
    """结算归期：body 显式 period 优先（校验 YYYY-MM）；
    individual 且全行有日期、同属一期 → 自动取该期；否则 ""（未归期）。"""
    p = body.get("period")
    if p is not None:
        if not isinstance(p, str):
            raise ApiError(400, "period 要是 \"YYYY-MM\" 格式的核算期文字")
        p = p.strip()
        if p:
            try:
                engine.period_bounds(p)
            except ValueError:
                raise ApiError(400, "核算期格式应为 YYYY-MM（如 2026-09）") from None
            return p
    if mode == "individual" and records:
        if all(r.date is not None for r in records):
            periods = {engine.period_of(r.date) for r in records}
            if len(periods) == 1:
                return periods.pop()
    return ""


def api_settle(body: dict) -> dict:
    """{rows, mode, team_total?, period?} → individual/team 核算结果的 dict。

    引擎对非法行抛 ValueError/TypeError，由 errorhandler 统一映射 400。
    任务单 05：核算成功后落 settlements 底账 + audit 留痕（400 的不落库）。
    """
    mode = body.get("mode")
    if mode not in ("individual", "team"):
        raise ApiError(400, "mode 必须是 individual（个人计件）或 team（班组计件）")
    rows = body.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ApiError(400, "rows 要是非空数组（个人计件=工票行，班组计件=成员行）")
    records = None
    if mode == "individual":
        records = [engine.PieceRecord.from_dict(r) for r in rows]
        result = engine.individual_piecepay(records).to_dict()
    else:
        team_total = body.get("team_total")
        if team_total is None:
            raise ApiError(400, "班组计件要带 team_total（班组总额）")
        members = [engine.TeamMember.from_dict(r) for r in rows]
        result = engine.team_piecepay(team_total, members).to_dict()
    period = _settle_period(mode, body, records)
    conn = _db()
    db.save_settlement(conn, period, mode, result)
    audit(conn, "user", "settle", period, mode=mode, rows=len(rows),
          total=result.get("total_amount") or result.get("allocated_total"))
    return result


# ------------------------------------------------------------------
# 工票存取 + 按期历史（任务单 05）
# ------------------------------------------------------------------

def api_save_ticket(body: dict) -> dict:
    """{sheet_no, workshop?, date?, rows, csv?} 存票+行。

    行经引擎 PieceRecord 校验（坏行 400 人话）；同票号重存=覆盖（幂等）。
    """
    sheet_no = body.get("sheet_no")
    if not isinstance(sheet_no, str) or not sheet_no.strip():
        raise ApiError(400, "票号不能为空（sheet_no）")
    sheet_no = sheet_no.strip()
    workshop = body.get("workshop", "")
    if workshop is None:
        workshop = ""
    if not isinstance(workshop, str):
        raise ApiError(400, "车间（workshop）要是文字")
    date = body.get("date", "")
    if date is None:
        date = ""
    if not isinstance(date, str):
        raise ApiError(400, "日期（date）要是 YYYY-MM-DD 文字")
    date = date.strip()
    if date:
        try:
            engine._parse_date(date)  # 同包复用引擎严格校验（日历合法/补零）
        except ValueError:
            raise ApiError(400, "工票日期格式应为 YYYY-MM-DD（如 2026-09-10）") from None
    rows = body.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ApiError(400, "rows 要是非空数组（工票行）")
    records = [engine.PieceRecord.from_dict(r) for r in rows]  # ValueError → 400 人话
    csv_text = body.get("csv")
    if csv_text is not None and not isinstance(csv_text, str):
        raise ApiError(400, "csv 要是文本（原始 CSV 留底，可不传）")
    conn = _db()
    tid = db.save_ticket(conn, sheet_no, workshop, date,
                         [r.to_dict() for r in records], csv_text)
    audit(conn, "user", "save_ticket", sheet_no,
          rows=len(records), workshop=workshop)
    created = conn.execute("SELECT created_at FROM tickets WHERE id=?",
                           (tid,)).fetchone()["created_at"]
    return {"id": tid, "sheet_no": sheet_no, "created_at": created}


def api_list_tickets() -> dict:
    return {"tickets": db.list_tickets(_db())}


def _settle_total(result: dict) -> decimal.Decimal:
    """结算结果里的总金额（individual=total_amount，team=allocated_total）。"""
    for k in ("total_amount", "allocated_total"):
        v = result.get(k) if isinstance(result, dict) else None
        if v is not None:
            try:
                return decimal.Decimal(str(v))
            except decimal.InvalidOperation:
                return decimal.Decimal(0)
    return decimal.Decimal(0)


def api_history() -> dict:
    """结算底账按期聚合：期降序、未归期（""）殿后；金额 Decimal 精确求和。"""
    groups: dict = {}
    for s in db.list_settlements(_db()):  # 新记录在前
        g = groups.get(s["period"])
        if g is None:
            g = groups[s["period"]] = {
                "period": s["period"], "count": 0, "modes": [],
                "total": decimal.Decimal(0),
                "first_at": s["created_at"], "last_at": s["created_at"]}
        g["count"] += 1
        if s["mode"] not in g["modes"]:
            g["modes"].append(s["mode"])
        g["total"] += _settle_total(s["result"])
        # created_at 是 datetime('now','localtime') 的 "YYYY-MM-DD HH:MM:SS"，文本可比
        g["first_at"] = min(g["first_at"], s["created_at"])
        g["last_at"] = max(g["last_at"], s["created_at"])
    items = [{"period": g["period"], "count": g["count"], "modes": g["modes"],
              "total_amount": str(engine.q2(g["total"])),
              "first_at": g["first_at"], "last_at": g["last_at"]}
             for g in groups.values()]
    items.sort(key=lambda i: (i["period"] != "", i["period"]), reverse=True)
    return {"items": items}


# ------------------------------------------------------------------
# 工资条 Excel 导出（SpreadsheetML 2003 XML：零依赖多 sheet，Excel/WPS 直开）
# ------------------------------------------------------------------

def _dec_or_zero(v) -> decimal.Decimal:
    """宽松 Decimal（导出聚合用）：坏值按 0，不砸导出（底账正常时走不到）。"""
    try:
        return decimal.Decimal(str(v))
    except (decimal.InvalidOperation, TypeError, ValueError):
        return decimal.Decimal(0)


def _period_param(qs) -> str:
    """?period= 校验：缺省空串（=最近一期）；给了就必须是合法 YYYY-MM。"""
    p = (qs.get("period") or [""])[0].strip()
    if not p:
        return ""
    try:
        engine.period_bounds(p)
    except ValueError:
        raise ApiError(400, "核算期格式应为 YYYY-MM（如 2026-09）") from None
    return p


_XML_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # XML 1.0 非法控制字符（保留 \t\n\r）


def _xml_row(cells, header=False) -> str:
    """一行单元格 → SpreadsheetML Row。空值空 Cell；金额一律 String 精确字符串。
    R5-F1：str 后先剥 XML 1.0 非法控制字符（\x00-\x08/\x0b/\x0c/\x0e-\x1f/\x7f，
    保留 \t\n\r）再转义——否则导出文件不是合法 XML，Excel 打开即报无法读取。"""
    out = ["<Row>"]
    for c in cells:
        if c is None or c == "":
            out.append("<Cell/>")
            continue
        style = ' ss:StyleID="h"' if header else ""
        text = _xml_escape(_XML_CTRL_RE.sub("", str(c)))
        out.append('<Cell%s><Data ss:Type="String">%s</Data></Cell>'
                   % (style, text))
    out.append("</Row>")
    return "".join(out)


def _sheet_xml(name: str, rows: list) -> str:
    """一张 Worksheet（首行表头加粗）。
    R5-F2：ss:Name 是双引号包裹的属性值，必须连英文双引号一起转义。"""
    body = "".join(_xml_row(r, header=(i == 0)) for i, r in enumerate(rows))
    return '<Worksheet ss:Name="%s"><Table>%s</Table></Worksheet>' % (
        _xml_escape(name, {'"': "&quot;"}), body)


def build_payslips_xls(settlements: list, period: str) -> bytes:
    """该期结算列表（时间升序）→ 工资条 .xls 字节（汇总 + 明细 两个 sheet）。

    - 汇总：姓名/计件工资/个税（演示口径）/实发工资 + 合计行 + 口径备注行；
      计件合计 = 个人 by_person 金额 + 班组分摊金额（同人跨结算合并），
      个税走 engine.payslip 演示口径（与页面工资条同一把尺子）。
    - 明细：模式/工序/姓名/数量/报废/合格数量/单价/金额/备注；个人行带全量
      引擎原值，班组行工序/报废/合格数量/单价留空、备注=尾差调整。
    - 不变量：明细合计金额 == 汇总合计计件（两本账对同一期必须对平）。
    - 全链 Decimal；单元格一律 ss:Type="String"（绝不 Number/float）。
    """
    persons: dict = {}          # name -> Decimal 计件合计（首次出现保序）
    detail_rows = []
    detail_total = decimal.Decimal(0)
    for s in settlements:
        result = s["result"] if isinstance(s["result"], dict) else {}
        lines = [l for l in (result.get("lines") or []) if isinstance(l, dict)]
        mode_cn = "班组计件" if s["mode"] == "team" else "个人计件"
        for line in lines:
            if s["mode"] == "team":
                detail_rows.append([mode_cn, None, line.get("name"), line.get("qty"),
                                    None, None, None, line.get("amount"),
                                    line.get("remark")])
            else:
                detail_rows.append([mode_cn, line.get("process"), line.get("name"),
                                    line.get("qty"), line.get("defect"),
                                    line.get("qualified_qty"), line.get("unit_price"),
                                    line.get("amount"), None])
            detail_total += _dec_or_zero(line.get("amount"))
        if s["mode"] == "team":
            for line in lines:  # 班组按人 = 分摊行（引擎不合并同名）
                name = line.get("name")
                if name is not None:
                    persons.setdefault(str(name), decimal.Decimal(0))
                    persons[str(name)] += _dec_or_zero(line.get("amount"))
        else:
            for p in (result.get("by_person") or []):  # 个人按人 = 引擎合并口径
                if isinstance(p, dict) and p.get("name") is not None:
                    name = str(p["name"])
                    persons.setdefault(name, decimal.Decimal(0))
                    persons[name] += _dec_or_zero(p.get("amount"))

    sum_rows = []
    piece_sum = tax_sum = net_sum = decimal.Decimal(0)
    for name, piece in persons.items():
        ps = engine.payslip(piece)  # 个税演示口径（tax_note 也在导出里标注）
        sum_rows.append([name, str(ps.gross), str(ps.tax), str(ps.net)])
        piece_sum += ps.gross
        tax_sum += ps.tax
        net_sum += ps.net
    sum_rows.append(["合计", str(engine.q2(piece_sum)), str(engine.q2(tax_sum)),
                     str(engine.q2(net_sum))])
    sum_rows.append(["备注：" + TAX_NOTE])

    detail_rows.append(["合计", None, None, None, None, None, None,
                        str(engine.q2(detail_total)), None])

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<?mso-application progid="Excel.Sheet"?>\n'
        '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"'
        ' xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
        '<Styles><Style ss:ID="h"><Font ss:Bold="1"/></Style></Styles>'
        + _sheet_xml("汇总", [["姓名", "计件工资", "个税（演示口径）", "实发工资"]] + sum_rows)
        + _sheet_xml("明细", [["模式", "工序", "姓名", "数量", "报废",
                              "合格数量", "单价", "金额", "备注"]] + detail_rows)
        + "</Workbook>")
    return xml.encode("utf-8")


def api_export_payslips(qs) -> tuple:
    """GET /api/export/payslips[?period=YYYY-MM] → (xls 字节, ASCII 名, UTF-8 名)。

    period 缺省 = 最近一个已归期（YYYY-MM 文本可比）；未归期（""）结算不进
    导出（归不了期发不了工资）；完全没有归期记录 → 404 人话。
    """
    want = _period_param(qs)
    conn = _db()
    labeled = [s for s in db.list_settlements(conn) if s["period"]]
    if not labeled:
        raise ApiError(404, "还没有归期的核算记录（工票带日期核算后才能归期导出），"
                            "先去「核算」跑一次")
    if not want:
        want = max(s["period"] for s in labeled)
    picked = [s for s in labeled if s["period"] == want]
    if not picked:
        raise ApiError(404, "%s 期还没有核算记录" % want)
    picked.reverse()  # db 新记录在前 → 时间升序（人物首次出场顺序稳定）
    body = build_payslips_xls(picked, want)
    audit(conn, "user", "export_payslips", want)
    return body, "suanjian-payslips-%s.xls" % want, "算件-工资条-%s.xls" % want


# ------------------------------------------------------------------
# 多月趋势（近 6 期：总产值 / 报废率 / 人均产出）
# ------------------------------------------------------------------

TREND_MONTHS = 6
"""趋势窗口：最近 6 个已归期；不足就返回已有期数（宁少勿编）。"""


def api_trend() -> dict:
    """按期聚合 settlements → 近 6 期（期升序，折线图友好）。

    指标口径：
    - total_amount 总产值 = Σ结算总额（与 /api/history 同一把 _settle_total 尺子）
    - scrap_rate   报废率（%，q2 银行家舍入）= Σ报废/Σ数量×100，只统计个人计件
      结算（班组无报废口径）；分母 0 → null（宁缺勿编 0%）
    - workers      该期工人数：个人 by_person + 班组 lines 按姓名去重（跨结算也去重）
    - per_capita   人均产出（q2）= 总产值/工人数；工人数 0 → null
    未归期（""）结算不进趋势。除法包 localcontext(prec=50)（对齐引擎纪律）。
    """
    groups: dict = {}
    for s in db.list_settlements(_db()):  # 新记录在前（顺序只影响 modes 首现）
        period = s["period"]
        if not period:
            continue
        result = s["result"] if isinstance(s["result"], dict) else {}
        g = groups.get(period)
        if g is None:
            g = groups[period] = {
                "period": period, "settlements": 0, "modes": [],
                "total": decimal.Decimal(0), "persons": set(),
                "qualified": decimal.Decimal(0), "defect": decimal.Decimal(0)}
        g["settlements"] += 1
        if s["mode"] not in g["modes"]:
            g["modes"].append(s["mode"])
        g["total"] += _settle_total(result)
        if s["mode"] == "team":
            for line in result.get("lines") or []:
                if isinstance(line, dict) and line.get("name") is not None:
                    g["persons"].add(str(line["name"]))
        else:
            for p in result.get("by_person") or []:
                if not isinstance(p, dict) or p.get("name") is None:
                    continue
                g["persons"].add(str(p["name"]))
                g["qualified"] += _dec_or_zero(p.get("qualified_qty"))
                g["defect"] += _dec_or_zero(p.get("defect_qty"))

    items = []
    for period in sorted(groups)[-TREND_MONTHS:]:  # 期升序 + 近 6 期窗口
        g = groups[period]
        qty_all = g["qualified"] + g["defect"]
        with decimal.localcontext() as ctx:
            ctx.prec = 50
            ctx.rounding = decimal.ROUND_HALF_EVEN
            # 比率/人均是内部中间量的商（prec=50 除法商 50 位有效数）——必须走
            # engine._q2_raw（同包复用，先例 _parse_date）：外部门 q2 的 30 位
            # 有效数上限会把多有效位商误判成脏数据（引擎 docstring 明示的坑）
            scrap = engine._q2_raw(g["defect"] / qty_all * 100) if qty_all > 0 else None
            per_capita = (engine._q2_raw(g["total"] / len(g["persons"]))
                          if g["persons"] else None)
        items.append({
            "period": period, "settlements": g["settlements"], "modes": g["modes"],
            "total_amount": str(engine.q2(g["total"])),
            "workers": len(g["persons"]),
            "scrap_rate": str(scrap) if scrap is not None else None,
            "per_capita": str(per_capita) if per_capita is not None else None,
        })
    return {"items": items}


# ------------------------------------------------------------------
# 期间对比（任务单 10：两期逐人对比，聚合好给前端）
# ------------------------------------------------------------------

def _cmp_period_param(qs, key: str) -> str:
    """取 p1/p2 查询参数：空串=没给；给了就校验 YYYY-MM（复用引擎期校验）。"""
    p = (qs.get(key) or [""])[0].strip()
    if not p:
        return ""
    try:
        engine.period_bounds(p)
    except ValueError:
        raise ApiError(400, "核算期格式应为 YYYY-MM（如 2026-09）") from None
    return p


def _period_persons(settlements: list) -> dict:
    """一期结算列表（时间升序）→ {姓名: {qualified, piece}}（Decimal）。

    口径与 /api/export/payslips 汇总同一把尺子：个人 by_person + 班组 lines
    按姓名合并、跨结算累加；班组无报废口径，成员数量直接计入合格数量。
    """
    persons: dict = {}
    for s in settlements:
        result = s["result"] if isinstance(s["result"], dict) else {}
        if s["mode"] == "team":
            rows = [(l.get("name"), l.get("qty"), l.get("amount"))
                    for l in (result.get("lines") or [])
                    if isinstance(l, dict)]
        else:
            rows = [(p.get("name"), p.get("qualified_qty"), p.get("amount"))
                    for p in (result.get("by_person") or [])
                    if isinstance(p, dict)]
        for name, qty, amount in rows:
            if name is None:
                continue
            per = persons.setdefault(
                str(name), {"qualified": decimal.Decimal(0),
                            "piece": decimal.Decimal(0)})
            per["qualified"] += _dec_or_zero(qty)
            per["piece"] += _dec_or_zero(amount)
    return persons


def _cmp_block(v1: decimal.Decimal, v2: decimal.Decimal, money: bool) -> dict:
    """一对数值 → {p1, p2, diff, rate}（金额/数量精确字符串）。

    diff = p2 − p1；rate = diff/p1×100（q2 银行家舍入；除法包
    localcontext(prec=50) 后走 engine._q2_raw——多有效位的商过外部门
    q2 会被 30 位上限误判，api_trend 踩过并注明的坑）；
    p1 基数为 0 → rate=null（宁缺勿编，同 scrap_rate 语义）。
    """
    diff = v2 - v1
    rate = None
    if v1 != 0:
        with decimal.localcontext() as ctx:
            ctx.prec = 50
            ctx.rounding = decimal.ROUND_HALF_EVEN
            rate = engine._q2_raw(diff / v1 * 100)
    if money:
        return {"p1": str(engine.q2(v1)), "p2": str(engine.q2(v2)),
                "diff": str(engine.q2(diff)),
                "rate": str(rate) if rate is not None else None}
    return {"p1": engine._num_str(v1), "p2": engine._num_str(v2),
            "diff": engine._num_str(diff),
            "rate": str(rate) if rate is not None else None}


def api_compare(qs) -> dict:
    """GET /api/compare[?p1=YYYY-MM&p2=YYYY-MM] → 两期逐人对比。

    契约（docs/任务单/10，响应 shape 本单定死、前端只消费）：
    - 数据源 = 已归期 settlements（未归期 "" 不算一期）
    - p1/p2 都不给 = 最近两期（p2=最新、p1=次新）；不足两期 → 400 人话；
      只给一个 → 400；p1==p2 → 400；p1 比 p2 晚（倒序）→ 400；
      显式给了但该期没有核算记录 → 400 人话（404 留给老服务路由探测）
    - 逐人：个人 by_person + 班组 lines 按姓名合并；piece=计件工资合计、
      qualified=合格数量（班组=成员数量）、net=engine.payslip(piece).net
      （个税演示口径，逐人独立算，与导出/工资条页同尺子）
    - workers=两期都在的人，按 |实发变化| 降序（并列按姓名）；
      new_workers（p2 新出现）/gone_workers（p2 没出现）单独分组，
      组内按该期计件降序；totals=两期全员合计（含新/消失）
    - 金额全链 Decimal 精确字符串；audit 留痕 action=compare（只在成功时）
    """
    p1 = _cmp_period_param(qs, "p1")
    p2 = _cmp_period_param(qs, "p2")
    if bool(p1) != bool(p2):
        raise ApiError(400, "要对比就得选两个期间（p1 和 p2 都要给，"
                            "或都不给=最近两期）")
    conn = _db()
    labeled: dict = {}   # 期 → 该期结算列表（新在前）
    for s in db.list_settlements(conn):
        if s["period"]:
            labeled.setdefault(s["period"], []).append(s)
    order = sorted(labeled, reverse=True)   # 全部已归期，降序（选择器用）
    if not p1:
        if len(order) < 2:
            raise ApiError(400, "还没有两期已归期的核算记录（对比要两个期间），"
                                "先去「核算」再跑一期")
        p2, p1 = order[0], order[1]
    else:
        if p1 == p2:
            raise ApiError(400, "两个期间不能是同一期：%s" % p1)
        if p1 > p2:
            raise ApiError(400, "前一期间（p1=%s）要比后一期间（p2=%s）早"
                                % (p1, p2))
        for p in (p1, p2):
            if p not in labeled:
                raise ApiError(400, "%s 期还没有核算记录（换一个有记录的期间）" % p)
    rows1, rows2 = labeled[p1], labeled[p2]
    for rows in (rows1, rows2):
        rows.reverse()   # 新在前 → 时间升序（人物首现顺序稳定）
    per1 = _period_persons(rows1)
    per2 = _period_persons(rows2)

    def person_row(per, name):
        piece = per[name]["piece"]
        return {"qualified": engine._num_str(per[name]["qualified"]),
                "piece": str(engine.q2(piece)),
                "net": str(engine.payslip(piece).net)}

    ranked = []          # (|实发变化|, 姓名, 条目)——排序用 Decimal 原值
    for name in per1.keys() & per2.keys():
        a, b = per1[name], per2[name]
        net1 = engine.payslip(a["piece"]).net
        net2 = engine.payslip(b["piece"]).net
        ranked.append((abs(net2 - net1), name, {
            "name": name,
            "qualified": _cmp_block(a["qualified"], b["qualified"], False),
            "piece": _cmp_block(a["piece"], b["piece"], True),
            "net": _cmp_block(net1, net2, True),
        }))
    ranked.sort(key=lambda t: (-t[0], t[1]))
    workers = [t[2] for t in ranked]

    def solo(per, other, side):
        rows = [{"name": n, side: person_row(per, n)}
                for n in per.keys() - other.keys()]
        rows.sort(key=lambda w: (-decimal.Decimal(w[side]["piece"]), w["name"]))
        return rows

    def sums(per):
        qualified = sum((v["qualified"] for v in per.values()),
                        decimal.Decimal(0))
        piece = sum((v["piece"] for v in per.values()), decimal.Decimal(0))
        net = sum((engine.payslip(v["piece"]).net for v in per.values()),
                  decimal.Decimal(0))
        return qualified, piece, net

    q1, piece1, net1s = sums(per1)
    q2v, piece2, net2s = sums(per2)
    audit(conn, "user", "compare", "%s~%s" % (p1, p2))
    return {
        "p1": p1, "p2": p2, "periods": order,
        "workers": workers,
        "new_workers": solo(per2, per1, "p2"),
        "gone_workers": solo(per1, per2, "p1"),
        "totals": {
            "qualified": _cmp_block(q1, q2v, False),
            "piece": _cmp_block(piece1, piece2, True),
            "net": _cmp_block(net1s, net2s, True),
        },
    }


# ------------------------------------------------------------------
# 数据备份（任务单 09：三表全量 + 全部 CSV 留底 → 一个 .json 附件）
# ------------------------------------------------------------------

def api_backup() -> tuple:
    """GET /api/backup → (json 字节, ASCII 文件名, UTF-8 中文文件名)。

    - 内容：tickets / settlements / audit_log 三表全量 + 每张工票的原始 CSV
      留底（sheet_no → csv 文本，无留底的票不进该表）+ counts 对账数；
      金额字段保持库内精确字符串，绝不走 float。
    - 红线：data/config.json（口令 + 识别密钥）绝不进备份——备份文件跟着
      人走（网盘/U 盘），密钥不能跟着走。
    - 文件名带时间戳（本地时间，与库内 created_at 同口径）；
    - 快照先取、留痕后写：本次的 backup 留痕进下一个备份，不进自己（确定性）。
    - 空库也照样备份（备份的是整份数据，不是数据量）。
    """
    now = datetime.datetime.now()   # 本地时间，与 datetime('now','localtime') 同口径
    stamp = now.strftime("%Y%m%d-%H%M%S")
    conn = _db()
    tickets = db.list_tickets(conn)
    settlements = db.list_settlements(conn)
    audit_rows = [dict(r) for r in conn.execute(
        "SELECT actor,action,target,detail_json,at FROM audit_log ORDER BY id")]
    csvs = {t["sheet_no"]: t["csv"] for t in tickets if t.get("csv")}
    payload = {
        "app": "suanjian",
        "kind": "backup",
        "version": 1,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "counts": {
            "tickets": len(tickets),
            "settlements": len(settlements),
            "audit_log": len(audit_rows),
            "csvs": len(csvs),
        },
        "db": {
            "tickets": tickets,
            "settlements": settlements,
            "audit_log": audit_rows,
        },
        "csvs": csvs,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    audit(conn, "user", "backup", stamp)
    return body, "suanjian-backup-%s.json" % stamp, "算件-备份-%s.json" % stamp


# ------------------------------------------------------------------
# OCR 接线（任务单 05：图 → 结构化预填，BYOK 可选）
# ------------------------------------------------------------------

# 与 core/ocr_pipe._MIME_BY_EXT 对应的 mime→扩展名（临时文件后缀决定上送 MIME）
_MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
             "image/gif": ".gif", "image/bmp": ".bmp"}

OCR_UNCONFIGURED_MSG = "未配置识别服务，请用 CSV 导入"


def _ocr_config():
    """data/config.json 的 [ocr] 段 → (key, base_url, model)。
    文件缺失/不是合法 JSON/没有 ocr 段/key 或 base_url 空 → 400 人话（任务单口径）。"""
    path = os.path.join(db.home_dir(), "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        raise ApiError(400, OCR_UNCONFIGURED_MSG, code="OCR_UNCONFIGURED") from None
    except Exception as e:
        logging.getLogger("sj.server").error("config.json 读不了：%s", e)
        raise ApiError(400, OCR_UNCONFIGURED_MSG, code="OCR_UNCONFIGURED") from None
    sec = cfg.get("ocr") if isinstance(cfg, dict) else None
    key = base_url = ""
    model = "glm-5.3-flash"
    if isinstance(sec, dict):
        key = str(sec.get("key") or sec.get("api_key") or "").strip()
        base_url = str(sec.get("base_url") or "").strip()
        model = str(sec.get("model") or "").strip() or model
    if not key or not base_url:
        raise ApiError(400, OCR_UNCONFIGURED_MSG, code="OCR_UNCONFIGURED")
    return key, base_url, model


def api_ocr(body: dict) -> dict:
    """{"image_base64", "mime"} → {sheet, needs_review, errors}。

    图片落临时文件（/tmp）复用 call_flash_vision（按扩展名定 MIME）；
    上游网络/接口失败兜成 502 人话（给出 CSV 出路），绝不裸抛 500。
    """
    b64 = body.get("image_base64")
    if not isinstance(b64, str) or not b64.strip():
        raise ApiError(400, "image_base64 必填（图片的 base64 文本）")
    mime = body.get("mime")
    if not isinstance(mime, str) or mime.strip().lower() not in _MIME_EXT:
        raise ApiError(400, "不支持的图片格式（认 PNG/JPG/WebP/GIF/BMP）")
    mime = mime.strip().lower()
    s = b64.strip()
    if s.startswith("data:") and "," in s:  # 容忍 data URL 前缀
        s = s.split(",", 1)[1]
    s = "".join(s.split())                  # 去折行空白
    s += "=" * (-len(s) % 4)                # 容忍缺 padding
    try:
        raw = base64.b64decode(s, validate=True)
    except Exception:
        raise ApiError(400, "图片数据不是合法的 base64") from None
    if len(raw) > OCR_MAX_IMAGE:
        raise ApiError(413, "图片太大（上限 8MB），压缩后再传")

    key, base_url, model = _ocr_config()
    fd, path = tempfile.mkstemp(suffix=_MIME_EXT[mime], prefix="sj_ocr_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        try:
            text = ocr_pipe.call_flash_vision(
                path, key, base_url, model=model, timeout=OCR_TIMEOUT)
        except urllib.error.HTTPError as e:
            raise ApiError(502, "识别服务返回错误（HTTP %d）：密钥或配置可能不对，"
                                "检查后再试，或先用 CSV 导入" % e.code,
                           code="OCR_UPSTREAM_ERROR") from None
        except Exception as e:
            raise ApiError(502, "识别服务没连上（%s）：检查网络后重试，"
                                "或先用 CSV 导入" % type(e).__name__,
                           code="OCR_UPSTREAM_ERROR") from None
    finally:
        try:
            os.unlink(path)                 # 临时图片用完即删
        except OSError as e:
            sys.stderr.write("[ocr] 临时图片没删掉（不影响识别结果）：%s\n" % e)
    parsed = ocr_pipe.parse_response(text)  # 任何解析异常都降级为 errors，不抛
    sheet = {"sheet_no": parsed.sheet_no, "workshop": parsed.workshop,
             "date": parsed.date, "rows": parsed.rows}
    return {"sheet": sheet, "needs_review": parsed.needs_review,
            "errors": parsed.errors}


# ------------------------------------------------------------------
# HTTP
# ------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SuanJian/1.0"
    timeout = 60  # 慢速/恶意连接不许无限占线程

    # ---------- 基础设施 ----------

    def log_message(self, fmt, *args):
        """访问日志：控制字符（含 DEL）转义成 \\xNN、gross 参数脱敏（共用
        _sanitize_log），经 logging 走文件。"""
        try:
            line = _sanitize_log(fmt % args)
            logging.getLogger("sj.access").info("%s %s", self.address_string(), line)
        except Exception as e:
            sys.stderr.write("[access-log] 写访问日志失败（不影响业务）：%s\n" % e)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, body: bytes, ctype: str, ascii_name: str, utf8_name: str):
        """附件下载响应：ASCII 回退 + filename* UTF-8 中文真名；
        Content-Length 必带（keep-alive 定界，与 _json 同纪律）。"""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"; filename*=UTF-8\'\'%s'
                         % (ascii_name, quote(utf8_name)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, msg, code=None):
        """错误响应：人话 error（原文案，一字不改）+ 增量稳定码 code。

        code 缺省按 status 查默认表（_code_for_status），保证恒非空。"""
        if not getattr(self, "_body_consumed", False):
            self._drain_body()  # 没收的 body 收掉，能收就保 keep-alive
        self._json({"error": msg, "code": code or _code_for_status(status)}, status)

    def _read_raw(self, max_body=None):
        """按 Content-Length 精确读整段请求体（keep-alive 定界）。
        max_body：本路由的请求体上限（OCR 路由放宽；默认全局 MAX_BODY）。"""
        limit = max_body if max_body is not None else MAX_BODY
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Content-Length 不合法")
        if length < 0:
            self.close_connection = True  # 负长度没法给 body 定界，只能收线防串包
            raise ApiError(400, "Content-Length 不能是负数")
        if length == 0:
            self._body_consumed = True
            raise ApiError(400, "请求体是空的")
        if length > limit:
            self.close_connection = True  # 超限 body 不读，直接断线防串包
            raise ApiError(413, "文件太大（上限 %dMB），拆小一点再传" % (limit // 1024 // 1024))
        data = self.rfile.read(length)
        self._body_consumed = True
        if len(data) < length:
            self.close_connection = True
            raise ApiError(400, "请求体没收全，连接好像断了")
        return data

    def _read_json(self, max_body=None):
        data = self._read_raw(max_body=max_body)
        try:
            obj = json.loads(data.decode("utf-8"))
        except Exception:
            raise ApiError(400, "请求体不是合法 JSON", code="INVALID_JSON") from None
        if not isinstance(obj, dict):
            raise ApiError(400, "请求体必须是 JSON 对象", code="INVALID_JSON")
        return obj

    def _read_csv_body(self):
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            body = self._read_json()
            text = body.get("csv", body.get("text"))
            if not isinstance(text, str):
                raise ApiError(400, "JSON 请求体要有 csv 字段（CSV 全文文本）",
                               code="CSV_INVALID")
            return text
        raw = self._read_raw()
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ApiError(400, "CSV 得是 UTF-8 编码（Excel 导出时选「CSV UTF-8」）",
                           code="CSV_INVALID") from None

    def _drain_body(self):
        """keep-alive 安全：把没收的 body 读完丢弃（太大/读不动/chunked 就收线）。"""
        self._body_consumed = True
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self.close_connection = True  # chunked 没法按长度收，只能断
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return
        if n < 0:
            self.close_connection = True  # 负长度没法定界（F22），收线
            return
        if n == 0:
            return
        if n > MAX_BODY:
            self.close_connection = True
            return
        try:
            remaining = n
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    self.close_connection = True
                    return
                remaining -= len(chunk)
        except OSError:
            self.close_connection = True

    # ---------- 口令门（任务单 07，塔罗小屋同款机制） ----------

    def _code_ok(self, given: str = None, code: str = None) -> bool:
        """口令比对（R9-F6 中文口令编码缝）。

        机理：HTTP 头按 latin-1 传输，中文口令原样放进 X-Code 到这里必是
        乱码、永远 401 且无提示。口径=前后端做百分号编码对齐：前端
        encodeURIComponent(code) 发（纯 ASCII 安全），这里 unquote 还原后
        compare_digest。还原值与原值双重精确比对：纯 ASCII 口令原样发
        （老客户端/curl）不受影响，含 % 的边角两种形态都能对上。
        hmac.compare_digest 防时序侧信道；口令值绝不落日志/响应。
        """
        if code is None:
            code = _gate_code()
        if not code:
            return True   # 门没开
        if given is None:
            given = (self.headers.get("X-Code") or "").strip()
        try:
            decoded = unquote(given)
        except Exception:
            decoded = given
        return (hmac.compare_digest(decoded.encode("utf-8"), code.encode("utf-8"))
                or hmac.compare_digest(given.encode("utf-8"), code.encode("utf-8")))

    def _gate_check(self, path: str):
        """/api/*（除 /api/health）进门，未知 /api 路径也先 401 后 404（不泄露接口清单）。

        401/429 走 ApiError → errorhandler → _error：没收的 body 会被 drain 掉，
        keep-alive 连接不被早退弄串包（塔罗踩过的坑）。
        R9-F7 限速（成熟口径）：同 IP 连错 5 次锁 _CODE_LOCK_SECS，锁定期内
        对口令也 429；输对清零。
        """
        if path in ("/api", "/api/") or path.startswith("/api/"):
            if path == "/api/health":
                return
            code = _gate_code()
            if not code:
                return   # 门没开（本机模式向后兼容）
            ip = self.client_address[0] if self.client_address else "?"
            if _code_lock_state(ip):
                raise ApiError(429, "试错太多次，锁 %s（防在线暴力试口令）"
                               % _lock_desc(), code="GATE_LOCKED")
            if not self._code_ok(code=code):
                locked, left = _code_fail_note(ip)
                if locked:
                    raise ApiError(401, "口令不对，已锁 %s" % _lock_desc())
                # R10-6：非锁定路径 left 恒 >= 1（st[0] >= 5 已走锁定分支），
                # 原三目里的兜底 else 分支不可达，删除
                raise ApiError(401, "口令不对，还剩 %d 次机会" % left)
            _code_fail_clear(ip)

    # ---------- GET ----------

    def do_GET(self):
        if not _host_allowed(self.headers.get("Host")):
            self.close_connection = True
            self._error(403, "本服务只允许本机访问（Host 不在白名单）")
            return
        self._body_consumed = False
        self._drain_body()  # GET 本不该带 body；带了就收掉，防串包
        errorhandler(self, self._route_get)

    def _route_get(self):
        path = urlsplit(self.path).path
        self._gate_check(path)
        if path == "/api/health":
            # 版本/运行信息（收尾三小件之二）：version=APP_VERSION 单一事实源；
            # uptime 与 started_at 同源同钟；口令门豁免不受影响（前端可用性探测依赖）
            return self._json({
                "ok": True,
                "service": "suanjian",
                "version": APP_VERSION,
                "uptime_seconds": int(time.time() - _PROCESS_STARTED_AT),
                "started_at": _STARTED_AT_ISO,
            })
        if path == "/api/tickets":
            return self._json(api_list_tickets())
        if path == "/api/history":
            return self._json(api_history())
        if path == "/api/trend":
            return self._json(api_trend())
        if path == "/api/compare":
            qs = parse_qs(urlsplit(self.path).query)
            return self._json(api_compare(qs))
        if path == "/api/payslip":
            qs = parse_qs(urlsplit(self.path).query)
            gross = (qs.get("gross") or [""])[0].strip()
            if not gross:
                raise ApiError(400, "gross 必填（月毛收入，如 8000）")
            out = engine.payslip(gross).to_dict()  # ValueError → 400（errorhandler）
            out["tax_note"] = TAX_NOTE
            return self._json(out)
        if path == "/api/export/payslips":
            qs = parse_qs(urlsplit(self.path).query)
            body, ascii_name, utf8_name = api_export_payslips(qs)
            return self._send_file(body, "application/vnd.ms-excel",
                                   ascii_name, utf8_name)
        if path == "/api/backup":
            body, ascii_name, utf8_name = api_backup()
            return self._send_file(body, "application/json; charset=utf-8",
                                   ascii_name, utf8_name)
        if path == "/api" or path.startswith("/api/"):
            raise ApiError(404, "没有这个接口：%s" % path)
        return self._serve_static(path)

    # ---------- POST ----------

    def do_POST(self):
        if not _host_allowed(self.headers.get("Host")):
            self.close_connection = True
            self._error(403, "本服务只允许本机访问（Host 不在白名单）")
            return
        self._body_consumed = False
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            return self._error(411, "本服务不支持 chunked 请求体（请带 Content-Length）")
        errorhandler(self, self._route_post)

    def _route_post(self):
        path = urlsplit(self.path).path
        self._gate_check(path)
        if path == "/api/ingest/csv":
            return self._json(api_ingest_csv(self._read_csv_body()))
        if path == "/api/settle":
            return self._json(api_settle(self._read_json()))
        if path == "/api/tickets/save":
            return self._json(api_save_ticket(self._read_json()))
        if path == "/api/ocr":
            return self._json(api_ocr(self._read_json(max_body=OCR_MAX_BODY)))
        raise ApiError(404, "没有这个接口：%s" % path)

    # ---------- 静态 ----------

    def _serve_static(self, path):
        """web/ 目录静态服务；realpath 包含校验防穿越（含百分号编码后的穿越）。"""
        name = "index.html" if path == "/" else unquote(path.lstrip("/"))
        full = os.path.realpath(os.path.join(WEB_DIR, name))
        root = os.path.realpath(WEB_DIR)
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            raise ApiError(404, "没有这个文件：%s" % name)
        ctype = CONTENT_TYPES.get(os.path.splitext(full)[1].lower(),
                                  "application/octet-stream")
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """客户端半途断线（ConnectionReset/超时）属常态：记日志不刷屏不炸线程。

        读请求行阶段的断线发生在路由之外，errorhandler 兜不到，在这里收口。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout)):
            logging.getLogger("sj.server").info(
                "客户端 %s 断开连接（锁屏/切页/关页属常态）：%s", client_address, exc)
            return
        super().handle_error(request, client_address)


def make_server(host: str, port: int) -> Server:
    """构造服务器实例（测试传 port=0 拿临时端口）。"""
    return Server((host, port), Handler)


def _log_dir() -> str:
    """日志目录：与数据库同家（core/db.home_dir()）——SUANJIAN_HOME 重定向库时
    日志跟着走（R9-F5：测试/并行实例不再把访问日志写进主 data/，日志与库不分家）；
    frozen 态=exe 旁、源码态=项目 data/，与原 BASE_DIR/data 口径一致。
    R10-2 口径写明：frozen 态日志在 exe 目录**另起一本**——打包后 exe 在哪个
    目录运行就在哪旁新建日志，旧目录（旧 data/ 或上次运行位置）里的日志历史
    **不延续**（不合并、不搬运），要看旧日志得回旧目录翻。"""
    return db.home_dir()


def _setup_logging():
    log_dir = _log_dir()
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("[%(asctime)s] %(name)s %(message)s")
    for name, fname in (("sj.access", "access.log"), ("sj.server", "server.log")):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        if not lg.handlers:
            fh = logging.FileHandler(os.path.join(log_dir, fname), encoding="utf-8")
            fh.setFormatter(fmt)
            lg.addHandler(fh)
    sys.excepthook = lambda t, v, tb: logging.getLogger("sj.server").error(
        "未捕获异常", exc_info=(t, v, tb))
    threading.excepthook = lambda a: logging.getLogger("sj.server").error(
        "线程未捕获异常", exc_info=a)


def startup_banner(host: str, port: int, lan_ip: str = "") -> str:
    """启动横幅：实际绑定地址 + 模式说明；局域网模式附手机网址与口令提醒
    （任务单 07：启动日志打印实际绑定与模式）。口令值绝不进横幅。"""
    lines = []
    if _lan_mode():
        lines.append("[算件] 模式：局域网开放（绑定 %s，同一网络的手机/电脑都能访问）" % host)
        if lan_ip and lan_ip != "127.0.0.1":
            lines.append("[算件] 手机浏览器打开：http://%s:%d/" % (lan_ip, port))
        if not _gate_code():
            lines.append("[算件] 提醒：还没设口令（data/config.json 的 \"code\" 字段），"
                         "同网任何设备都能直接看到数据")
    else:
        lines.append("[算件] 模式：本机（绑定 %s，只有这台电脑能访问；要给手机用设 "
                     "SUANJIAN_LAN=1）" % host)
    lines.append("[算件] 就绪 http://%s:%d/ （Ctrl+C 退出）" % (host, port))
    return "\n".join(lines)


def main(port=None):
    _setup_logging()
    port = port or _env_port()
    host = bind_host()
    lan_ip = _lan_ip() if _lan_mode() else ""
    httpd = make_server(host, port)

    def shutdown(sig, frame):
        print("[算件] 收到退出信号，正在收尾…")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
    except ValueError as e:
        sys.stderr.write("[server] 非主线程跳过信号注册（测试/嵌入场景正常现象）：%s\n" % e)

    print(startup_banner(host, port, lan_ip), flush=True)  # flush：重定向到文件/管道也立即可见
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        print("[算件] 已退出")


def _cli_port(arg) -> "int | None":
    """命令行端口参数（R9-F12）：全角数字显式转半角后，只认纯 ASCII 数字。

    旧写法 isdigit()+int() 会无声吞全角/阿拉伯-印度数字（Unicode 洗白），
    现在是「显式归一 + 严格校验」：全角照用（照顾输入法），其余数字文字
    返回 None 由调用方静默回落默认端口。数字本身还要过 1-65535 范围
    校验（R11-F2）：越界（如 99999）不是 None 而是 ValueError——
    「读起来是端口但用不了」和「根本不是端口」是两种错，前者若当垃圾
    静默吞掉会掩盖用户配置错误，必须抛中文 ValueError 让调用方
    警告回落（argv/env 两个入口都捕获后回落默认端口并打印人话提醒）。
    """
    s = str(arg).translate(str.maketrans("０１２３４５６７８９", "0123456789")).strip()
    if not s or not s.isascii() or not s.isdigit():
        return None
    v = int(s)
    if not 1 <= v <= 65535:
        raise ValueError("端口 %d 超出可用范围 1-65535" % v)
    return v


def _env_port() -> "int":
    """SUANJIAN_PORT 环境变量端口（R10-7）：与 argv 同口径复用 _cli_port。

    旧写法 int(os.environ.get(...)) 遇全角数字直接 ValueError 起不来，
    且 env/argv 两条入口归一口径不一致；现在全角显式归一照用，垃圾值
    拒收回落默认端口（与 argv 拒收语义一致：不用这个值，但不拦启动）。
    越界数字（R11-F2，如 99999）同样回落默认端口，但要多打一行人话
    警告——静默换端口会把「配置错了」伪装成「服务失踪」。
    """
    try:
        return _cli_port(os.environ.get("SUANJIAN_PORT")) or DEFAULT_PORT
    except ValueError as e:
        sys.stderr.write("[算件] SUANJIAN_PORT 不能用（%s），改用默认端口 %d。\n"
                         % (e, DEFAULT_PORT))
        return DEFAULT_PORT


if __name__ == "__main__":
    _argv_port = None
    if len(sys.argv) > 1:
        try:
            _argv_port = _cli_port(sys.argv[1])
        except ValueError as e:
            sys.stderr.write("[算件] 命令行端口不能用（%s），改用默认端口 %d。\n"
                             % (e, DEFAULT_PORT))
    main(_argv_port)
