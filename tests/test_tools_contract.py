# -*- coding: utf-8 -*-
"""tools/ shell 脚本契约测试（F15/F17 的回归锁）。

bash 脚本没法进 pytest 断言行为（打包/备份都是重活），这里锁：
1. bash -n 语法检查；
2. 修复单要求的关键行（探测回落 / -mtime 修正）不许回退。
"""
import subprocess
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"


def _bash_n_ok(name):
    r = subprocess.run(["bash", "-n", str(TOOLS / name)],
                       capture_output=True, text=True)
    return r.returncode == 0, r.stderr


# ---------------------------------------------------------------------
# F15：build_exe 命令名探测回落 python3 → python → py -3
# ---------------------------------------------------------------------

class TestBuildExeProbe:
    def test_probe_fallback_chain(self):
        src = (TOOLS / "build_exe.command").read_text(encoding="utf-8")
        assert "python3" in src and "python" in src and "py -3" in src
        assert "-m PyInstaller" in src
        # 探测必须发生在 PyInstaller 调用之前（先找解释器再装模块）
        assert src.index("py -3") < src.index("-m PyInstaller")

    def test_bash_n(self):
        ok, err = _bash_n_ok("build_exe.command")
        assert ok is True, f"build_exe.command 语法错误：{err}"


# ---------------------------------------------------------------------
# F17：备份轮换 -mtime +13（「保留 14 天」如实）
# ---------------------------------------------------------------------

class TestBackupRotation:
    def test_mtime_plus_13(self):
        src = (TOOLS / "backup.command").read_text(encoding="utf-8")
        # find -mtime +14 实际删的是第 15 天起（保留 15 天）；
        # 「保留 14 天」必须写 +（N-1），且 KEEP_DAYS=14
        assert "KEEP_DAYS=14" in src
        assert "-mtime +$((KEEP_DAYS - 1))" in src
        assert "-mtime +14" not in src
        assert "保留 14 天" in src

    def test_bash_n(self):
        ok, err = _bash_n_ok("backup.command")
        assert ok is True, f"backup.command 语法错误：{err}"
