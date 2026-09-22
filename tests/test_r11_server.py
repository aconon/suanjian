# -*- coding: utf-8 -*-
"""修复单 R11 服务端测试（TDD 先行）：docs/审查单/修复单-R11.md

- F1：限速 purge 边界——清理循环跳过当前 ip（表 >1024 且该 ip 锁刚过期时，
      条目不得被 purge 后计数从 1 重数；强口径「过期后再错 1 次即再锁」保持）
- F2：_cli_port 归一后加 1-65535 范围校验（越界中文 ValueError→调用方回落
      默认端口+人话警告）；docstring 与实现对齐；_log_dir/home_dir 补
      「frozen 态日志在 exe 目录另起一本」口径（R10-2 补漏）

全部走注入时钟/模块级函数，不碰 data/ 真库与真 config.json。
"""
from pathlib import Path

import pytest

import server
from core import db as core_db


@pytest.fixture(autouse=True)
def _clean_gate():
    """模块级失败/锁定表：每用例前后清空（防串扰，沿用 R9/R10 口径）。"""
    server._code_gate_reset()
    yield
    server._code_gate_reset()


def _fill(count, now, lock_until):
    """直填失败表铺量（测 purge 边界本身，不走 _code_fail_note 的入表路径）。

    条目 [1, lock_until]：lock_until 给远期=清理时「未过期」不可回收；
    给 0=「只错未锁」过期可回收。绕开逐条 note 的自清理干扰，可控铺表。
    """
    with server._CODE_FAILS_LOCK:
        for i in range(count):
            server._CODE_FAILS["172.16.%d.%d" % (i // 250, i % 250)] = [1, lock_until]


# =====================================================================
# F1：purge 跳过当前 ip——过期锁不被清掉重数
# =====================================================================

class TestF1PurgeKeepsCurrentIp:
    def test_expired_lock_survives_purge_then_one_fail_relocks(self):
        """表 >1024 且该 IP 锁刚过期：过期条目不得被本次清理吃掉。

        旧实现清理块在 get(ip) 之前跑：条目被 purge → 计数从 1 重数，
        穷举者每轮白得 4 次试错（R11-F1）。修法=清理跳过当前 ip。
        本例铺的全是「只错未锁」（st=[1,0]，0 < now 恒真=可回收）条目，
        当前 ip 的过期锁在候选集里却必须幸存。
        """
        ip = "10.20.0.1"
        t0 = 1000.0
        for _ in range(server._CODE_MAX_FAILS):
            server._code_fail_note(ip, now=t0)            # 锁到 t0+120
        assert server._code_lock_state(ip, now=t0) is True
        _fill(1030, now=t0, lock_until=0)                 # 铺 1030 条可回收噪声
        t1 = t0 + server._CODE_LOCK_SECS + 1              # ip 锁刚过期自解
        assert server._code_lock_state(ip, now=t1) is False
        assert len(server._CODE_FAILS) > 1024, "前置失败：表没铺过 1024"
        locked, left = server._code_fail_note(ip, now=t1)  # 过期后第 1 错
        assert locked is True, "过期条目被 purge，计数从 1 重数（强口径被撕破）"
        assert left == 0
        assert server._code_lock_state(
            ip, now=t1 + server._CODE_LOCK_SECS - 1) is True   # 立即再锁满 2 分钟

    def test_purge_still_cleans_other_expired_entries(self):
        """跳过当前 ip 不等于不清理：其余过期条目照常回收（防漏修成不清理）。"""
        ip = "10.21.0.1"
        for _ in range(server._CODE_MAX_FAILS):
            server._code_fail_note(ip, now=100.0)
        _fill(1030, now=100.0, lock_until=0)
        server._code_fail_note(ip, now=100.0 + server._CODE_LOCK_SECS + 1)
        assert len(server._CODE_FAILS) == 1, \
            "其余过期条目没被清走（表剩 %d 项）" % len(server._CODE_FAILS)
        assert ip in server._CODE_FAILS, "当前 ip 的条目反而被清掉了"

    def test_far_future_locked_noise_also_covers_case(self):
        """变体：铺表噪声全是未过期锁（不可回收）——当前 ip 的过期条目同样幸存。"""
        ip = "10.22.0.1"
        for _ in range(server._CODE_MAX_FAILS):
            server._code_fail_note(ip, now=200.0)
        _fill(1030, now=200.0, lock_until=99999.0)        # 未过期，谁都清不掉
        t1 = 200.0 + server._CODE_LOCK_SECS + 1
        locked, _left = server._code_fail_note(ip, now=t1)
        assert locked is True, "未过期噪声占满表时，当前 ip 过期锁被误清"


# =====================================================================
# F2：端口范围校验（1-65535）+ 回落警告 + docstring 对齐
# =====================================================================

class TestF2PortRange:
    def test_out_of_range_raises_chinese_valueerror(self):
        """越界数字（如 99999/65536/0）→ 中文 ValueError（不是静默 None）。

        旧实现裸返回 int → make_server bind 裸崩（R11-F2）。
        """
        table = str.maketrans("０１２３４５６７８９", "0123456789")
        for bad in ("99999", "65536", "０", "６５５３６", "100000"):
            normalized = str(int(bad.translate(table)))   # ０→0（1 是下界，0 越界）
            with pytest.raises(ValueError) as ei:
                server._cli_port(bad)
            msg = str(ei.value)
            assert "端口" in msg and "1-65535" in msg, \
                "报错不是中文人话（%r → %s）" % (bad, msg)
            assert normalized in msg, "报错里应带上具体越界值 %s：%s" % (normalized, msg)

    def test_boundary_values_accepted(self):
        """边界值 1 与 65535 合法（范围是闭区间）。"""
        assert server._cli_port("1") == 1
        assert server._cli_port("65535") == 65535

    def test_garbage_still_returns_none_not_raise(self):
        """非数字垃圾照旧 None（静默回落）；只有「数字但越界」才 ValueError。"""
        for bad in ("abc", "88a0", "-1", "٨٧٧٠", ""):
            assert server._cli_port(bad) is None, bad

    def test_env_port_out_of_range_falls_back_with_warning(self, monkeypatch, capsys):
        """SUANJIAN_PORT=99999：回落默认端口 + stderr 打人话警告（不裸崩）。"""
        monkeypatch.setenv("SUANJIAN_PORT", "99999")
        assert server._env_port() == server.DEFAULT_PORT
        err = capsys.readouterr().err
        assert "99999" in err and "默认端口" in err, "警告没带越界值/回落去向：%r" % err

    def test_env_port_boundary_ok(self, monkeypatch, capsys):
        """env 边界值照常用，且无越界警告。"""
        monkeypatch.setenv("SUANJIAN_PORT", "65535")
        assert server._env_port() == 65535
        assert "默认端口" not in capsys.readouterr().err

    def test_argv_entry_guards_valueerror_in_dunder_main(self):
        """__main__ 入口包 try/except ValueError：argv 越界也回落启动不裸崩。"""
        src = Path(server.__file__).read_text(encoding="utf-8")
        tail = src[src.index('if __name__ == "__main__"'):]
        assert "_cli_port" in tail
        assert "except ValueError" in tail, "argv 越界会带栈裸崩（没守卫）"

    def test_cli_port_docstring_states_range_rule(self):
        """docstring 与实现对齐：写明 1-65535 范围校验与越界 ValueError 口径。"""
        doc = server._cli_port.__doc__ or ""
        assert "1-65535" in doc, "_cli_port docstring 没写范围口径（过度承诺）"

    def test_env_port_docstring_states_fallback_on_range_error(self):
        """_env_port docstring 写明越界回落（与垃圾值回落并列）。"""
        doc = server._env_port.__doc__ or ""
        assert "越界" in doc or "范围" in doc, "_env_port docstring 没写越界回落口径"


class TestF2FrozenLogDocstring:
    def test_log_dir_and_home_dir_note_fresh_start(self):
        """R10-2 补漏：_log_dir 与 db.home_dir 两端都写明 frozen 态日志另起一本。"""
        assert "另起一本" in (server._log_dir.__doc__ or "")
        assert "另起一本" in (core_db.home_dir.__doc__ or ""), \
            "db.home_dir（实际算目录的一端）缺 frozen 日志另起一本口径"
