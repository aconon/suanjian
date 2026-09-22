#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.ocr_pipe — 拍照识别管线（AI 预填 → 人工确认）。

设计口径：
- AI 只做「图 → 结构化预填」，**预填结果必须经人工确认才入账**（确认前不碰引擎）。
- 纯函数部分（response 解析、行校验、置信标注）本地可测零网络；
  网络调用（flash 视觉接口）单独隔离，BYOK 可选。
- 提示词要求模型「看不清标 [?]」，禁止编造；解析器把 [?] 记为需人工确认。
"""
import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple

# ---------- 提示词 ----------

PROMPT_TEMPLATE = """这是一张工厂计件工票的照片。请把表格逐行提取为 JSON，格式：
{{"sheet_no": "工票编号", "workshop": "车间", "date": "YYYY-MM-DD",
  "rows": [{{"process": "工序", "name": "姓名", "qty": 数量, "unit_price": 单价, "defect": 废品数}}]}}
规则：
1. 只提取看得清的内容；任何看不清的字符统一写成 [?]，禁止编造其他内容。
2. 数字保持原样，不要心算合计。
3. 只输出 JSON，不要解释。"""


# ---------- 纯函数：响应解析 ----------

@dataclass
class ParsedSheet:
    sheet_no: str = ""
    workshop: str = ""
    date: str = ""
    rows: List[dict] = field(default_factory=list)
    needs_review: bool = False      # 任一字段带 [?] = 需人工确认
    errors: List[str] = field(default_factory=list)


_NUM_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")


def _clean_number(raw) -> Optional[str]:
    """数字字段清洗（宁保守、不编造）。

    - 类型白名单 str/int/float/None；其余类型（dict/list/bool…）一律 None
      （调用方标 needs_review）——杜绝 str(raw) 把容器洗成数字的老坑；
    - 只剥千分位逗号（半角/全角）与货币符号（￥¥$元），剥完必须是纯数字；
    - 跨段数字（"4 4 2"、"第3行共5件"）不拼接不猜测 → None；
    - 科学计数法（"1e3"/"1E+30"）单列处理 → None 标人工复核；
    - 带 [?] 的原样返回（needs_review，转人工确认）。
    """
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return None
    s = str(raw).strip()  # 只去首尾空白：内部空格不剥（"4 4 2" 是跨段，禁拼接）
    if "[?]" in s or s in ("[?", "?]"):
        return s
    for ch in (",", "，", "￥", "¥", "$", "元"):
        s = s.replace(ch, "")
    s = s.strip()
    return s if _NUM_RE.match(s) else None


def _extract_json(text: str) -> Tuple[Optional[dict], bool]:
    """从模型回复里抠 JSON，返回 (解析结果或 None, 是否出现过候选)。

    先试整段贪婪（常见的「前后带话/``` 包裹」一次命中）；不成再逐块尝试：
    从每个 { 起、由短到长找 }，第一个 parse 成功的候选即用（嵌套对象
    天然优先取到最外层可解析段）。
    """
    m = re.search(r"\{.*\}", text, re.S)
    greedy_error = None
    if m:
        try:
            return json.loads(m.group(0)), True
        except json.JSONDecodeError as e:
            greedy_error = e  # 贪婪段失败不是终点：落逐块扫描（保留首错便于上层诊断）
    seen = False
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        end = text.find("}", start)
        while end != -1:
            try:
                return json.loads(text[start:end + 1]), True
            except json.JSONDecodeError:
                seen = True
                end = text.find("}", end + 1)
    return None, seen


def parse_response(text: str) -> ParsedSheet:
    """把模型返回解析为 ParsedSheet。任何异常都降级为 errors 标注，绝不抛出。"""
    out = ParsedSheet()
    if not text or not text.strip():
        out.errors.append("空响应")
        out.needs_review = True
        return out
    # 抠 JSON（容忍模型在前后加话/```包裹；贪婪不成逐块再试）
    data, had_candidate = _extract_json(text)
    if data is None:
        if had_candidate:
            out.errors.append("JSON 解析失败：抠出的候选都不是合法 JSON")
        else:
            out.errors.append("未找到 JSON")
        out.needs_review = True
        return out

    def mark(s):
        return "[?]" in str(s)

    out.sheet_no = str(data.get("sheet_no", ""))
    out.workshop = str(data.get("workshop", ""))
    out.date = str(data.get("date", ""))
    if mark(out.sheet_no) or mark(out.workshop) or mark(out.date):
        out.needs_review = True

    raw_rows = data.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        out.errors.append("无 rows 或格式错误")
        out.needs_review = True
        return out

    for i, r in enumerate(raw_rows, 1):
        if not isinstance(r, dict):
            out.errors.append(f"第{i}行格式错误")
            out.needs_review = True
            continue
        qty = _clean_number(r.get("qty"))
        price = _clean_number(r.get("unit_price"))
        defect_raw = _clean_number(r.get("defect"))
        defect = defect_raw if defect_raw is not None else "0"
        if defect_raw is None and r.get("defect") is not None:
            out.needs_review = True  # 废品给了但洗不出来：按 0 预填并标人工
        row = {
            "process": str(r.get("process", "")).strip(),
            "name": str(r.get("name", "")).strip(),
            "qty": qty,
            "unit_price": price,
            "defect": defect,
            "line_no": i,
        }
        if not row["process"] or not row["name"]:
            out.errors.append(f"第{i}行工序/姓名缺失")
            out.needs_review = True
        # [?] 可能出现在任何字段（提示词约定）：工序/姓名带 [?] 同样要人工确认，
        # 不能只查数字三件套（否则 needs_review=false 会让前端徽章误报「识别干净」）
        if any(mark(v) for v in (row["process"], row["name"],
                                 row["qty"], row["unit_price"], row["defect"])):
            out.needs_review = True
        # 数字可转的先转 Decimal 字符串（构造层最终校验）
        for k in ("qty", "unit_price", "defect"):
            v = row[k]
            if v is None:
                out.needs_review = True
            elif "[?]" not in str(v):
                try:
                    row[k] = str(Decimal(str(v)))
                except (InvalidOperation, ValueError):
                    row[k] = None
                    out.needs_review = True
        out.rows.append(row)
    return out


def merge_confirmed(parsed: ParsedSheet, confirmations: dict) -> List[dict]:
    """人工确认合并：confirmations = {"date": "...", "row_2.qty": "442", ...}。

    - "date" 为整单日期确认，注入每一行（引擎按行 date 归期，行里没日期
      settle_by_period 会拒）；
    - 其余 row_N.字段 只允许覆盖已有字段，不允许造结构。
    返回可直接喂引擎的 dict 列表（仍由引擎构造层二次校验）。"""
    rows = []
    for r in parsed.rows:
        row = dict(r)
        for k in ("process", "name", "qty", "unit_price", "defect"):
            key = f"row_{r.get('line_no')}.{k}"
            if key in confirmations:
                row[k] = confirmations[key]
        out = {k: row.get(k) for k in ("process", "name", "qty", "unit_price", "defect")}
        if "date" in confirmations:
            out["date"] = confirmations["date"]
        rows.append(out)
    return rows


# ---------- 网络调用（隔离；BYOK 可选，演示模式不依赖） ----------

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


def _mime_for(path: str) -> str:
    """data URL 的 MIME 按扩展名选（大小写不敏感；兜底 png）。"""
    return _MIME_BY_EXT.get(os.path.splitext(path)[1].lower(), "image/png")


def call_flash_vision(image_path: str, api_key: str, base_url: str,
                      model: str = "glm-5.3-flash", timeout: int = 120) -> str:
    """读图 → 模型 → 原始文本。网络/接口异常原样抛出（调用方兜底为人工录入模式）。
    注意：图片走 base64 本机直传，不经第三方 CDN；MIME 按扩展名选，
    jpg 样张不再被错标成 image/png。"""
    import base64
    import urllib.request

    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT_TEMPLATE},
            {"type": "image_url", "image_url": {
                "url": f"data:{_mime_for(image_path)};base64,{b64}"}},
        ]}],
        "max_tokens": 8000,
    }).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]
