#!/usr/bin/env bash
# 算件打包脚本：PyInstaller 单文件（宿主 OS；Win 包需在 Windows 上跑本脚本）
set -euo pipefail
cd "$(dirname "$0")/.."

# 解释器探测回落：python3 → python → py -3（Windows 常没有 python3 这个名字）
PY_CMD=""
for c in python3 python "py -3"; do
  if $c -c 'import sys' >/dev/null 2>&1; then
    PY_CMD="$c"
    break
  fi
done
if [ -z "$PY_CMD" ]; then
  echo "[打包] 找不到可用的 python（试过 python3 / python / py -3），先装好再跑" >&2
  exit 1
fi

PYI="$PY_CMD -m PyInstaller"
if ! $PYI --version >/dev/null 2>&1; then
  "$PY_CMD" -m pip install --quiet pyinstaller   # 模块缺失才装；装不上让脚本如实报错退出
fi

# --add-data 分隔符：macOS/Linux 用冒号，Windows（git-bash/MSYS）用分号
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) SEP=";" ;;
  *) SEP=":" ;;
esac

# 资源清单与 suanjian.spec 双份维护（R9-F11）：下面两条 --add-data 改动时，
# suanjian.spec 的 datas 必须同步改（同一份清单的两处入口）
$PYI --noconfirm --clean \
  --name suanjian \
  --onefile \
  --add-data "web${SEP}web" \
  --add-data "assets/samples${SEP}assets/samples" \
  server.py

echo "打包完成 → dist/suanjian"
echo "运行: ./dist/suanjian  (或 SUANJIAN_PORT=8770 ./dist/suanjian)"
