#!/usr/bin/env bash
# 算件备份：data/（SQLite 用官方 .backup 出一致快照，WAL 活库禁裸拷）+ 导入的 CSV
#          → backups/suanjian-时间戳.tar.gz，保留 14 天
# 用法：bash tools/backup.command（macOS 双击本文件也可）
set -euo pipefail
cd "$(dirname "$0")/.."

KEEP_DAYS=14
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="backups/suanjian-${STAMP}.tar.gz"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/suanjian-backup.XXXXXX")"   # 暂存只进临时目录
trap 'rm -rf "$STAGE"' EXIT

# ---- 1) data/：日志原样拷；*.db 走 sqlite3 在线一致性快照（带 -wal/-shm 裸 tar 会丢最近事务）----
if [ -d data ]; then
  # 密钥不随备份走（与 /api/backup 同红线）：data/config.json（口令+识别密钥）排除
  find data -type f ! -name '*.db' ! -name '*.db-wal' ! -name '*.db-shm' ! -path 'data/config.json' | while IFS= read -r f; do
    mkdir -p "$STAGE/$(dirname "$f")"
    cp -p "$f" "$STAGE/$f"
  done
  find data -type f -name '*.db' | while IFS= read -r db; do
    mkdir -p "$STAGE/$(dirname "$db")"
    if command -v sqlite3 >/dev/null 2>&1; then
      sqlite3 "$db" ".backup '$STAGE/$db'"
      CHECK="$(sqlite3 "$STAGE/$db" 'PRAGMA integrity_check;')"
      [ "$CHECK" = "ok" ] || { echo "[备份] 快照校验失败：${db}（$CHECK），本次不产出备份" >&2; exit 1; }
      # integrity_check 打开 WAL 模式快照时会在旁边生成空的 -wal/-shm（快照本体已自洽），打包前清掉
      rm -f "$STAGE/$db-wal" "$STAGE/$db-shm"
    else
      # 没有 sqlite3 的兜底：主文件连旁挂文件一起打包（时序有竞态，恢复后务必 integrity_check）
      echo "[备份] 警告：系统没有 sqlite3，$db 连 -wal/-shm 一起打包，恢复后请先 integrity_check" >&2
      cp -p "$db" "$STAGE/$db"
      for x in "$db-wal" "$db-shm"; do [ -f "$x" ] && cp -p "$x" "$STAGE/$x"; done
    fi
  done
fi

# ---- 2) 导入的工票 CSV：项目根目录与 imports/ 下各收一份 ----
CSV_COUNT=0
for f in *.csv imports/*.csv; do
  [ -f "$f" ] || continue
  mkdir -p "$STAGE/csv"
  cp -p "$f" "$STAGE/csv/$(basename "$f")"
  CSV_COUNT=$((CSV_COUNT + 1))
done

# ---- 3) 打包（先验证 tar 可读，不产出坏备份）----
if ! find "$STAGE" -type f | grep -q .; then
  echo "[备份] 还没有可备份的数据（data/ 为空且没有 CSV），本次跳过"
  exit 0
fi
mkdir -p backups
tar -czf "$OUT" -C "$STAGE" .
tar -tzf "$OUT" >/dev/null   # 读得回来才算备份成功

# ---- 4) 轮换：删满 KEEP_DAYS 天的旧备份（find -mtime +(N-1) 才是「保留 N 天」，
#          直接写 +N 会多留一天）；删失败立即报错退出，不静默（NAS rm 半途而废的教训）----
DELETED=$(find backups -maxdepth 1 -type f -name 'suanjian-*.tar.gz' -mtime +$((KEEP_DAYS - 1)) -print -delete | wc -l | tr -d ' ')
REMAIN=$(ls -1 backups/suanjian-*.tar.gz 2>/dev/null | wc -l | tr -d ' ')

echo "[备份] 完成 → ${OUT}（$(du -h "$OUT" | cut -f1)，CSV ${CSV_COUNT} 份）"
echo "[备份] 清理 ${KEEP_DAYS} 天前旧备份 ${DELETED} 份，现共 ${REMAIN} 份"
echo "[备份] 恢复：先停掉算件，再 tar -xzf $OUT -C .（SQLite 已是快照，覆盖 data/ 即可）"
echo "[备份] data/config.json（口令/识别密钥）不进备份，密钥请单独保管"
