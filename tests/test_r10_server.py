# -*- coding: utf-8 -*-
"""修复单 R10 服务端测试（TDD 先行）：docs/审查单/修复单-R10.md

- 2：_log_dir docstring 写明 frozen 态日志在 exe 目录另起、旧日志历史不延续
- 3：_code_fail_note 锁过期后再错 1 次立即再锁（强口径）——docstring 写明 + 用例固化
- 5：失败表清理候选 0 锁条目也回收（st[1]=0 不再因 truthy 判断漏网）
- 6：不可达的「即将锁定」分支删除（非锁定路径 left 恒 >= 1）
- 7：SUANJIAN_PORT env 与 argv 同口径（复用 _cli_port 的全角归一+严格校验）

全部走注入时钟/模块级函数，不碰 data/ 真库与真 config.json。
"""
from pathlib import Path

import pytest

import server


@pytest.fixture(autouse=True)
def _clean_gate():
    """模块级失败/锁定表：每用例前后清空（防串扰，沿用 R9 口径）。"""
    server._code_gate_reset()
    yield
    server._code_gate_reset()


# =====================================================================
# 3：锁过期后再错 1 次立即再锁（强口径固化）
# =====================================================================

class TestItem3RelockAfterExpiry:
    def test_one_fail_after_expiry_relocks_immediately(self):
        """锁过期后再错 1 次：立即再锁 2 分钟（不重新数满 5 次）。

        强口径理由：过期只解锁不洗计数（st[0] 已 >= 5），再 +1 仍 >= 5，
        穷举者拿不到「每 2 分钟白嫖 4 次试错」的窗口。
        """
        ip = "10.1.0.1"
        t0 = 1000.0
        for _ in range(server._CODE_MAX_FAILS):
            server._code_fail_note(ip, now=t0)            # 第 5 错：锁到 t0+120
        assert server._code_lock_state(ip, now=t0) is True
        t1 = t0 + server._CODE_LOCK_SECS + 1              # 过期自解
        assert server._code_lock_state(ip, now=t1) is False
        locked, left = server._code_fail_note(ip, now=t1)  # 过期后第 1 错
        assert locked is True, "过期后 1 错应立即再锁（强口径）"
        assert left == 0
        assert server._code_lock_state(
            ip, now=t1 + server._CODE_LOCK_SECS - 1) is True   # 再锁满 2 分钟
        assert server._code_lock_state(
            ip, now=t1 + server._CODE_LOCK_SECS + 1) is False  # 到期再自解

    def test_relock_docstring_states_strong_rule(self):
        """docstring 把「过期后再错 1 次即再次置锁」的口径写明（不是只活在测试里）。"""
        doc = server._code_fail_note.__doc__ or ""
        assert "过期" in doc and "置锁" in doc, \
            "_code_fail_note docstring 缺过期后再错口径说明"


# =====================================================================
# 5：失败表清理——0 锁条目（只错未锁 st=[n,0]）也回收
# =====================================================================

class TestItem5PurgeZeroLockEntries:
    def test_never_locked_entries_also_purged(self):
        """超 1024 项触发清理时，「只错过、从未置锁」的条目（st[1]=0）也回收。

        旧写法 `st[1] and st[1] < now` 里 0 是 falsy：这类条目永远清不掉，
        表只增不减（慢泄漏）。每 IP 只错 1 次铺 1030 个 IP 验证。
        """
        now = 100.0
        for i in range(1030):
            ip = "10.%d.%d.1" % (i // 250, i % 250)   # 1030 个互不相同的 IP
            server._code_fail_note(ip, now=now)       # 每个都只错 1 次（st=[1,0]）
        assert len(server._CODE_FAILS) <= 1024, \
            "0 锁条目没被回收，失败表只增（泄漏）：%d 项" % len(server._CODE_FAILS)

    def test_unexpired_locked_entries_survive_purge(self):
        """仍在锁定期内的条目不能被清理误伤（清理只回收过期的）。"""
        ip_locked = "10.2.0.1"
        t0 = 100.0
        for _ in range(server._CODE_MAX_FAILS):
            server._code_fail_note(ip_locked, now=t0)      # 锁到 t0+120
        for i in range(1030):                              # 铺满触发清理
            server._code_fail_note("10.3.%d.%d" % (i // 250, i % 250), now=t0 + 1)
        assert server._code_lock_state(ip_locked, now=t0 + 1) is True, \
            "清理误伤了仍在锁定期的条目"


# =====================================================================
# 6：不可达的「即将锁定」分支删除
# =====================================================================

class TestItem6UnreachableBranch:
    def test_dead_branch_removed(self):
        """非锁定路径 left 恒 >= 1（st[0]>=5 已走锁定分支），「即将锁定」永不可达。

        修复单 R10-6：死人代码删除。活分支「还剩 %d 次机会」保留。
        """
        src = Path(server.__file__).read_text(encoding="utf-8")
        assert "即将锁定" not in src, "不可达分支还在"
        assert "还剩 %d 次机会" in src, "活分支（剩余次数提示）被误删"

    def test_nonlocked_left_always_positive(self):
        """模块级语义佐证：非锁定返回值 left 恒 >= 1（证「即将锁定」确不可达）。"""
        ip = "10.4.0.1"
        for expect_left in (4, 3, 2, 1):        # 第 1~4 错
            locked, left = server._code_fail_note(ip, now=100.0)
            assert not locked
            assert left == expect_left and left > 0
        locked, _ = server._code_fail_note(ip, now=100.0)   # 第 5 错走锁定分支
        assert locked


# =====================================================================
# 7：SUANJIAN_PORT env 与 argv 同口径
# =====================================================================

class TestItem7EnvPort:
    def test_env_fullwidth_normalized(self, monkeypatch):
        """全角端口与 argv 同口径：复用 _cli_port 显式归一（旧 int() 直接 ValueError）。"""
        monkeypatch.setenv("SUANJIAN_PORT", "８７７０")
        assert server._env_port() == 8770

    def test_env_fullwidth_400(self, monkeypatch):
        """修复单原例：全角「４００」→ 400（只测解析，不真绑端口）。"""
        monkeypatch.setenv("SUANJIAN_PORT", "４００")
        assert server._env_port() == 400

    def test_env_ascii_and_blank(self, monkeypatch):
        monkeypatch.setenv("SUANJIAN_PORT", "9000")
        assert server._env_port() == 9000
        monkeypatch.setenv("SUANJIAN_PORT", " 9000 ")
        assert server._env_port() == 9000

    def test_env_garbage_falls_back_to_default(self, monkeypatch):
        """垃圾值不炸启动：回落默认端口（与 argv 拒收口径一致）。"""
        for bad in ("abc", "88a0", "-1", "٨٧٧٠", ""):
            monkeypatch.setenv("SUANJIAN_PORT", bad)
            assert server._env_port() == server.DEFAULT_PORT, bad

    def test_env_unset_uses_default(self, monkeypatch):
        monkeypatch.delenv("SUANJIAN_PORT", raising=False)
        assert server._env_port() == server.DEFAULT_PORT

    def test_env_argv_same_normalization(self, monkeypatch):
        """一致性：同一输入，env 路径与 argv 路径归一结果一致（复用同一函数）。"""
        for v in ("８７７０", "9000", " 9000 ", "abc", "٨٧٧٠", ""):
            monkeypatch.setenv("SUANJIAN_PORT", v)
            assert server._env_port() == (server._cli_port(v) or server.DEFAULT_PORT)


# =====================================================================
# 2：_log_dir docstring 写明 frozen 态日志另起
# =====================================================================

class TestItem2LogDirDocstring:
    def test_docstring_documents_frozen_fresh_start(self):
        """frozen 态日志在 exe 目录另起一本、旧日志历史不延续——口径写进 docstring。"""
        doc = server._log_dir.__doc__ or ""
        assert "frozen" in doc
        assert "不延续" in doc, "必须写明旧日志历史不延续（换目录=另起一本）"
