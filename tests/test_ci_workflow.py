# -*- coding: utf-8 -*-
"""GitHub Actions 打包工作流契约测试（收尾三小件之三）。

契约 = .github/workflows/build-exe.yml：
- push tag（v*）触发 + 手动触发（workflow_dispatch）
- windows runner 上直接调 PyInstaller 构建 suanjian.spec
  （不走 tools/build_exe.command：Windows runner 没有 bash .command 入口；
  spec 是与 .command 双入口维护的同一份资源清单，不再添第三份拷贝）
- 产物必须上传（dist/suanjian.exe，缺失即失败）

PyYAML 仅测试用（开发依赖）；运行时零依赖口径不受影响。
"""
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_PATH = os.path.join(ROOT, ".github", "workflows", "build-exe.yml")


def _load():
    with open(WF_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class TestWorkflowContract:
    def test_file_exists_and_parses(self):
        assert os.path.isfile(WF_PATH), "缺 .github/workflows/build-exe.yml"
        doc = _load()
        assert isinstance(doc, dict)

    def test_triggers(self):
        on = _load()["on"] if "on" in _load() else _load()[True]  # YAML 把 on 解析成 True
        assert "push" in on
        assert "v*" in on["push"]["tags"], "打 exe 必须由 v* tag 触发"
        assert "workflow_dispatch" in on

    def test_runs_on_windows(self):
        job = _load()["jobs"]["build"]
        assert job["runs-on"] == "windows-latest"

    def test_build_step_uses_spec(self):
        """打包走 suanjian.spec（单一资源清单入口），不复制 --add-data 清单。"""
        steps = _load()["jobs"]["build"]["steps"]
        build = [s for s in steps if s.get("name") == "打包 exe"][0]
        cmd = build["run"]
        assert "PyInstaller" in cmd
        assert "suanjian.spec" in cmd
        assert "--add-data" not in cmd, "资源清单不得出现第三份拷贝（R9-F11）"

    def test_artifact_upload(self):
        steps = _load()["jobs"]["build"]["steps"]
        up = [s for s in steps if "upload-artifact" in str(s.get("uses", ""))][0]
        with_d = up["with"]
        assert with_d["path"] == "dist/suanjian.exe"
        assert with_d["if-no-files-found"] == "error", "产物缺失必须显式失败"

    def test_utf8_env_for_chinese_output(self):
        """中文注释/输出在 Windows 控制台不炸：UTF-8 双开关（照参考模式）。"""
        steps = _load()["jobs"]["build"]["steps"]
        build = [s for s in steps if s.get("name") == "打包 exe"][0]
        env = build["env"]
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["PYTHONUTF8"] == "1"
