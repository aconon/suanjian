# -*- coding: utf-8 -*-
"""core/audit.py 兜底路径留痕测试（v1.4 清 delivery-report 已知 Minor）。

delivery-report 唯一 Minor（swallowed_exception，core/audit.py:25）：
fd 2 都关了的极端分支里 `except OSError: pass` 完全静默。验货器要求
「至少 logging.exception() 记下来」——修法：logging 自带 OSError 兜底
（Handler.handleError 不外抛），外再包 contextlib.suppress 保「审计失败
绝不砸业务」铁律；pass 从 except 下一行挪走，验货器规则不再命中。

test_audit_never_raises（test_db.py）已锁「不抛」；这里锁「留痕尝试」
与「验货器判定不再命中」两层。
"""
import re
from pathlib import Path

from core import audit as audit_mod

AUDIT_PY = Path(audit_mod.__file__).read_text(encoding="utf-8")


class TestAuditFallbackLogging:
    def test_oserror_branch_tries_logging(self):
        """极端分支至少尝试 logging.exception 留痕（不再纯静默）。"""
        assert "import logging" in AUDIT_PY or "from logging" in AUDIT_PY
        assert "logging.exception" in AUDIT_PY

    def test_logging_call_never_breaks_business(self):
        """logging 调用包在 contextlib.suppress 里：日志系统自身出事也不外抛。"""
        assert "contextlib.suppress" in AUDIT_PY

    def test_no_silent_pass_after_oserror(self):
        """镜像验货器判定：except OSError 的下一行不许是裸 pass（防回归）。"""
        lines = AUDIT_PY.split("\n")
        for i, line in enumerate(lines):
            if re.match(r"^\s*except\b[^:]*:\s*(#.*)?$", line):
                nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                clause = re.match(r"^\s*except\b([^:]*):", line).group(1).strip()
                if clause == "OSError":
                    assert not re.match(r"^(pass|continue|\.\.\.)\s*(#.*)?$", nxt), \
                        f"audit.py:{i + 2} except OSError 又退回静默 pass"

    def test_apocalypse_path_runs_and_never_raises(self, monkeypatch):
        """行为级：DB 炸 + stderr 炸 + fd 2 炸的三重兜底路径走到底：
        不抛业务异常，且途经 logging.exception（记录器直接验证调用发生）。"""
        import logging
        import os as os_mod
        import sqlite3

        class BoomConn:
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("db is toast")

        class DeadStderr:
            def write(self, *a, **k):
                raise OSError("stderr fd closed")

        calls = []
        monkeypatch.setattr("sys.stderr", DeadStderr())
        monkeypatch.setattr(os_mod, "write",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("fd 2 closed")))
        monkeypatch.setattr(logging, "exception",
                            lambda *a, **k: calls.append(a))
        audit_mod.audit(BoomConn(), "user", "settle", "2026-09")  # 不抛即契约
        assert calls, "fd 2 都关了的分支也应尝试 logging.exception 留痕"
