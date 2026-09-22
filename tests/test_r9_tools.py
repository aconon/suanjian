# -*- coding: utf-8 -*-
"""修复单 R9 工具/打包面测试（TDD 先行）：
- F4：tools/backup.command 的 tar 不带 data/config.json（口令+密钥绝不进备份）
- F9：deploy/suanjian.service 注释「LAN=1 须配口令」
- F10：web/design.html 移出打包目录（两条打包路径都自然不再带上）
- F11：suanjian.spec 与 tools/build_exe.command 双份维护互注（单一事实源注释）
"""
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# =====================================================================
# F4：命令行备份排除密钥——真跑一遍脚本（沙箱迷你项目，不碰真 data/）
# =====================================================================

class TestF4BackupExcludesConfig:
    def test_tar_has_no_config_json(self, tmp_path):
        if not (shutil.which("bash") and shutil.which("tar")):
            pytest.skip("本机没有 bash/tar，跳过备份脚本实测")
        proj = tmp_path
        (proj / "tools").mkdir()
        shutil.copy(ROOT / "tools" / "backup.command",
                    proj / "tools" / "backup.command")
        (proj / "data").mkdir()
        (proj / "data" / "config.json").write_text(
            '{"code": "9527ab", "ocr": {"key": "sk-勿外传"}}',
            encoding="utf-8")
        sqlite3.connect(str(proj / "data" / "suanjian.db")).close()
        (proj / "工票.csv").write_text("票号,车间\nSJ-1,冲压\n",
                                       encoding="utf-8")
        r = subprocess.run(["bash", str(proj / "tools" / "backup.command")],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        tars = list((proj / "backups").glob("suanjian-*.tar.gz"))
        assert len(tars) == 1, "沙箱里应产出一枚备份"
        listing = subprocess.run(["tar", "-tzf", str(tars[0])],
                                 capture_output=True, text=True).stdout
        assert "config.json" not in listing, \
            "data/config.json（口令+识别密钥）绝不能进备份 tar"
        assert "suanjian.db" in listing                  # 业务库照常进
        assert "csv/工票.csv" in listing                  # CSV 留底照常进

    def test_backup_output_reminds_key_safety(self, tmp_path):
        """脚本完成输出要提醒「密钥不进备份/单独保管」（与设置页同口径）。"""
        if not (shutil.which("bash") and shutil.which("tar")):
            pytest.skip("本机没有 bash/tar，跳过备份脚本实测")
        proj = tmp_path
        (proj / "tools").mkdir()
        shutil.copy(ROOT / "tools" / "backup.command",
                    proj / "tools" / "backup.command")
        (proj / "data").mkdir()
        sqlite3.connect(str(proj / "data" / "suanjian.db")).close()
        r = subprocess.run(["bash", str(proj / "tools" / "backup.command")],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert "密钥" in r.stdout

    def test_settings_copy_and_readme_aligned(self):
        """设置页文案与 README 同口径：备份不含密钥、密钥单独保管。"""
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for text in (app, readme):
            assert "密钥请单独保管" in text
        # 设置页必须把「界面下载」和「命令行 backup.command」说成同一个口径
        settings = app[app.index("function renderSettings"):app.index("/* ---------- 路由")]
        assert "backup.command" in settings
        assert "不装密钥" in settings


# =====================================================================
# F9：service 样例带安全注释（LAN=1 须配口令）
# =====================================================================

class TestF9ServiceComment:
    def test_service_warns_code_required_for_lan(self):
        unit = (ROOT / "deploy" / "suanjian.service").read_text(encoding="utf-8")
        assert "Environment=SUANJIAN_LAN=1" in unit
        # LAN 行上方要有口令提醒注释（含 config.json 的设法规程）
        i_lan = unit.index("Environment=SUANJIAN_LAN=1")
        comment = unit[:i_lan]
        assert "口令" in comment
        assert "config.json" in comment


# =====================================================================
# F10：design.html 移出 web/（不再被打包、不再经静态路由对外提供）
# =====================================================================

class TestF10DesignHtmlOutOfBundle:
    def test_design_html_moved_out_of_web(self):
        assert not (ROOT / "web" / "design.html").exists(), \
            "web/design.html 还在打包目录里（会进 exe 并被静态路由对外提供）"
        assert (ROOT / "docs" / "design.html").exists(), \
            "设计稿应移到 docs/ 留档（仓库里保留，打包不带上）"


# =====================================================================
# F11：两条打包路径互注（资源清单单一事实源注释）
# =====================================================================

class TestF11SpecCrossRef:
    def test_spec_mentions_cli_counterpart(self):
        spec = (ROOT / "suanjian.spec").read_text(encoding="utf-8")
        assert "build_exe.command" in spec, \
            "spec 要注明与 tools/build_exe.command 是同一份资源清单的两处入口"

    def test_cli_script_mentions_spec(self):
        cmd = (ROOT / "tools" / "build_exe.command").read_text(encoding="utf-8")
        assert "suanjian.spec" in cmd, \
            "build_exe.command 要注明与 suanjian.spec 同步维护"
