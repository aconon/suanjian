# GitHub 发布清单（aconon/suanjian）

> 发 GitHub 当简历封面前的最后检查单。实测时间：2026-09-22（580 测试全绿版本）。
> 用法：逐项打勾，全部 ✅ 才推。命令只列不替你跑。

## 1. 真实数据零残留核验（已实测，结论+复验命令）

| 检查项 | 实测结论 | 复验命令 |
|---|---|---|
| 前雇主名「昇迅/升迅/shengxun」 | ⚠️ 仅 `AGENTS.md` 1 处（净室声明里提到）。代码/前端/文档/测试零出现 | `grep -rn -i -E "昇迅\|升迅\|shengxun" . --exclude-dir={dist,build,backups,data}` |
| 用户名「aco」 | ⚠️ 仅 `docs/会话台账-0919-算件冲刺.md` 3 处 | `grep -rn "aco" --include="*.md" .` |
| 真实姓名 | ✅ 跟踪面 93 文件未发现（git 作者=「Aco <aco@AcodeMac-Studio.local>」，不含真名） | `git log --format="%an %ae" \| sort -u` |
| OCR API key | ✅ 只在 `data/config.json`，已被 `.gitignore`（`data/` + `config*.json`）挡住，跟踪面零出现 | 见下方 §6 克隆复验 |
| 「江苏」 | ✅ 全部是「江苏版演示基准」地域口径（README/engine/前端文案），非公司名，正常保留 | `git grep "江苏"` |
| 截图素材 | ✅ `docs/images/mobile-*.png` 三屏全合成数据（人名=演示通用名池：冯玉珍/赵国庆等；车间=冲压/装配/裁剪车间；无公司名），已视觉模型逐屏核验 | 人工眼看一遍 |
| 样张 | ✅ `assets/samples/` 全部 `tools/gen_samples.py` 合成 | `head tools/gen_samples.py` |

## 2. 发布前必办（当前 ⚠️ 的处置）

- [ ] **AGENTS.md**：内部项目地图，含「昇迅」字样与内部工作流词。**建议整文件不入公开仓**：
      `git rm --cached AGENTS.md && echo "AGENTS.md" >> .gitignore`
      （若想保留净室声明，可把 README 顶部那句改写为「不含任何前雇主代码」——不点名）
- [ ] **内部工程文档不入公开仓**（任务单/审查单/精度门/会话台账，含 GLM/5.3/flash/知识库/派单等工作流内部词）：
      `git rm --cached -r docs/任务单 docs/审查单 && git rm --cached docs/会话台账-0919-算件冲刺.md docs/精度门-Day0.md`
      再把四个路径加进 `.gitignore`。
      保留发布：`docs/面试演示脚本.md`（已核干净，README 有链接）、`docs/GITHUB发布清单.md`（本文件，可留可删）、`docs/images/`。
- [ ] **README 链接检查**：删掉内部文档后 README 只剩 `docs/面试演示脚本.md` 与 `docs/images/` 两个链接，均保留，无死链。
- [ ] **截图入库**：`git add docs/images/`（当前还未跟踪；README 封面区引用相对路径 `docs/images/mobile-*.png`）。
- [ ] **git 身份**：推前把邮箱换成 aconon 的 GitHub noreply（否则提交不挂账号）：
      `git config user.email "<aconon的ID+aconon@users.noreply.github.com>"`
      历史提交邮箱是 `aco@AcodeMac-Studio.local`（不含真名，可接受；介意再 filter-repo 重写）。
- [ ] **首次提交人**：以上改动（git rm --cached + .gitignore + docs/images）打包成一次提交再推。

## 3. LICENSE（建议：Apache-2.0）

- [ ] 仓库根加 `LICENSE` 文件（GitHub 网页端 Add file → Choose license → Apache-2.0 最省事，或 `curl -O https://www.apache.org/licenses/LICENSE-2.0.txt` 后改名并补附录版权行：`Copyright 2026 aconon`）
- 选 Apache-2.0 的理由：
  1. 与 aco 其他公开发布仓对齐（任务书口径）
  2. 宽松 + 显式专利授权——作品含 AI 识别管线，展示给雇主时条款更「工程规范」
  3. 项目零第三方依赖（python3 标准库 + 原生 JS），无许可兼容问题，MIT/GPL 纠纷为零，选谁都不阻塞，纯偏好题
- 也可以选 MIT（更短更常见）；**不建议 GPL**（简历项目希望别人能放心看/抄/用）

## 4. 仓库描述（About 栏，≤350 字符）

推荐（中）：

```
计件工资 AI 核算台：拍照/CSV 导入工票 → AI 预填 → 人工确认 → 确定性 Decimal 引擎出工资条。零依赖（python3 标准库 + SQLite），离线可跑，580 测试。AI 只碰输入端，碰钱的一行代码都没有。
```

备选（英，投国际岗时用）：

```
Offline-first piece-rate payroll for small factories: photo/CSV ticket import, AI pre-fill with human confirmation, and a fully deterministic Decimal engine for the money math. Python 3 stdlib only + SQLite. 580 tests.
```

## 5. Topics 建议（全小写、无空格，挑 ≤12 个）

```
piece-rate-payroll  payroll  manufacturing  python  sqlite  decimal
deterministic-engine  ai-prefill  ocr  offline-first  stdlib-only  single-binary
```

（投国际岗可再加 `resume-project`；不要放中文 topic，搜索量低）

## 6. 发布动作序列（命令只列不代跑）

```bash
# ① 建仓（私有先推，检查完再 public 也行）
gh repo create aconon/suanjian --private --source . --push
# ② 网页检查（截图加载/LICENSE/描述/topics）后转公开
gh repo edit aconon/suanjian --public \
  --description "（粘贴 §4 文案）"
# ③ topics
gh repo edit aconon/suanjian --add-topic piece-rate-payroll --add-topic payroll …（逐个）
```

## 7. 发布后自检（克隆复验）

```bash
git clone https://github.com/aconon/suanjian /tmp/sj-verify && cd /tmp/sj-verify
ls data 2>&1            # 应报不存在（data/ 未入库）
grep -rn -i -E "昇迅|升迅|shengxun|aco" . ; echo "exit=$?"   # 应 exit=1（零命中）
python3 -m pytest tests/ -q   # 克隆仓裸环境应 580 passed
python3 server.py &     # 起服务，手机宽度开 http://127.0.0.1:8770 与截图比对
```

README 渲染检查：GitHub 网页端确认封面三图并排显示（相对路径 `docs/images/…`，仓内路径，无外链）。

## 8. 发布物料现状

| 物料 | 状态 |
|---|---|
| README 封面区（手机三屏截图） | ✅ 已加（`docs/images/mobile-import|calc|trend.png`，393×852@2x，全合成数据） |
| README 英文摘要段（Overview） | ✅ 已加（deterministic engine + AI-prefill + offline） |
| LICENSE | ⏳ 发布时按 §3 添加 |
| 仓库描述/topics | ⏳ 发布时按 §4/§5 粘贴 |
