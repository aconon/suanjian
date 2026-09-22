#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ocr_pipe 纯函数测试（零网络）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.ocr_pipe import _clean_number, _mime_for, _extract_json, merge_confirmed, parse_response


def test_parse_clean_json():
    r = parse_response('{"sheet_no":"SJ-1","workshop":"冲压","date":"2026-09-09",'
                       '"rows":[{"process":"打磨","name":"李秀兰","qty":360,'
                       '"unit_price":1.62,"defect":0}]}')
    assert r.errors == []
    assert r.needs_review is False
    assert r.rows[0]["qty"] == "360"
    assert r.rows[0]["unit_price"] == "1.62"
    assert r.rows[0]["line_no"] == 1


def test_parse_unclear_marked_for_review():
    r = parse_response('{"sheet_no":"SJ-[?]","rows":[{"process":"打磨","name":"李秀兰",'
                       '"qty":"[?]","unit_price":1.62,"defect":0}]}')
    assert r.needs_review is True
    assert r.rows[0]["qty"] == "[?]"


def test_parse_row_process_unclear_flagged():
    # 契约（ParsedSheet 文档）：任一字段带 [?] = 需人工确认。
    # 工序含 [?] 而数字全干净时，needs_review 也必须 True——否则 /api/ocr 的
    # needs_review=false 会让前端徽章误报「识别干净」（前端黄标兜底挡不住文案）。
    r = parse_response('{"sheet_no":"SJ-1","workshop":"冲压","date":"2026-09-09",'
                       '"rows":[{"process":"焊[?]","name":"李秀兰",'
                       '"qty":360,"unit_price":1.62,"defect":0}]}')
    assert r.needs_review is True


def test_parse_row_name_unclear_flagged():
    r = parse_response('{"sheet_no":"SJ-1","rows":[{"process":"打磨","name":"李[?]珍",'
                       '"qty":10,"unit_price":1,"defect":0}]}')
    assert r.needs_review is True


def test_parse_empty_response_degrades():
    r = parse_response("")
    assert r.needs_review is True
    assert "空响应" in r.errors[0]


def test_parse_no_json_degrades():
    r = parse_response("模型跑题说了一堆没有 JSON")
    assert r.needs_review is True
    assert "未找到 JSON" in r.errors[0]


def test_parse_broken_json_degrades():
    r = parse_response('{"sheet_no": SJ-1, "rows": []}')
    assert r.needs_review is True
    assert any("JSON 解析失败" in e for e in r.errors)


def test_parse_missing_rows_degrades():
    r = parse_response('{"sheet_no":"SJ-1"}')
    assert r.needs_review is True
    assert any("rows" in e for e in r.errors)


def test_parse_row_missing_name_flagged():
    r = parse_response('{"rows":[{"process":"打磨","name":"","qty":10,"unit_price":1,"defect":0}]}')
    assert r.needs_review is True
    assert any("姓名缺失" in e or "工序/姓名缺失" in e for e in r.errors)


def test_parse_dirty_number_cleaned():
    # F1：货币符号剥掉能用；跨段数字（"4 4 2"）禁拼接 → None 待人工
    r = parse_response('{"rows":[{"process":"打磨","name":"李","qty":"4 4 2","unit_price":"￥1.55","defect":0}]}')
    assert r.rows[0]["qty"] is None
    assert r.needs_review is True
    assert r.rows[0]["unit_price"] == "1.55"


def test_parse_thousand_separator_cleaned():
    # F1：千分位逗号（半角/全角）与货币符号剥掉后是纯数字 → 可用
    r = parse_response('{"rows":[{"process":"打磨","name":"李","qty":"1,234","unit_price":"¥2.50","defect":"２，０００"}]}')
    assert r.rows[0]["qty"] == "1234"
    assert r.rows[0]["unit_price"] == "2.50"
    # 全角逗号剥掉后露出的全角数字不是 [0-9] → 按 0 预填并标人工（不猜）
    assert r.rows[0]["defect"] == "0"
    assert r.needs_review is True


def test_parse_dirty_number_type_whitelist():
    # F1：类型白名单 str/int/float/None——dict 洗成 "1" 的老 bug 必须死
    r = parse_response('{"rows":[{"process":"打磨","name":"李","qty":{"a":1},"unit_price":[3,5],"defect":{"x":9}}]}')
    assert r.rows[0]["qty"] is None
    assert r.rows[0]["unit_price"] is None
    assert r.rows[0]["defect"] == "0"        # 废品洗不出 → 按 0 预填
    assert r.needs_review is True            # 但必须标人工


def test_parse_bool_not_washed_to_number():
    r = parse_response('{"rows":[{"process":"打磨","name":"李","qty":true,"unit_price":1,"defect":0}]}')
    assert r.rows[0]["qty"] is None
    assert r.needs_review is True


def test_parse_scientific_notation_flagged():
    # F1：科学计数法单列——不拼接不解析，标复核
    r = parse_response('{"rows":[{"process":"打磨","name":"李","qty":"1e3","unit_price":1,"defect":0}]}')
    assert r.rows[0]["qty"] is None
    assert r.needs_review is True
    assert _clean_number("1E+30") is None


def test_parse_cross_segment_digits_not_joined():
    # F1："第3行共5件" 老代码会拼成 "35" —— 必须是 None
    assert _clean_number("第3行共5件") is None
    assert _clean_number({"a": 1}) is None
    assert _clean_number([1, 2]) is None
    assert _clean_number(True) is None


def test_parse_all_dirty_number_marked():
    r = parse_response('{"rows":[{"process":"打磨","name":"李","qty":"看不清","unit_price":1,"defect":0}]}')
    assert r.needs_review is True
    assert r.rows[0]["qty"] is None


def test_merge_confirmed_overrides_only():
    base = parse_response('{"sheet_no":"SJ-1","rows":[{"process":"打磨","name":"李秀兰","qty":"[?]",'
                          '"unit_price":1.62,"defect":0},{"process":"攻丝","name":"赵国庆","qty":80,'
                          '"unit_price":1.59,"defect":0}]}')
    rows = merge_confirmed(base, {"row_1.qty": "360"})
    assert rows[0]["qty"] == "360"
    assert rows[0]["name"] == "李秀兰"
    assert rows[1]["qty"] == "80"
    assert len(rows) == 2


def test_merge_confirmed_cannot_inject_structure():
    base = parse_response('{"rows":[{"process":"打磨","name":"李","qty":1,"unit_price":1,"defect":0}]}')
    rows = merge_confirmed(base, {"row_1.evil": "drop table", "row_9.qty": "999"})
    assert "evil" not in rows[0]
    assert len(rows) == 1


# =====================================================================
# F6：确认的整单 date 注入每一行
# =====================================================================

def test_merge_confirmed_injects_date_to_all_rows():
    base = parse_response('{"rows":[{"process":"打磨","name":"李","qty":1,"unit_price":1,"defect":0},'
                          '{"process":"攻丝","name":"赵","qty":2,"unit_price":1,"defect":0}]}')
    rows = merge_confirmed(base, {"date": "2026-09-01", "row_1.qty": "360"})
    assert all(r["date"] == "2026-09-01" for r in rows)
    assert rows[0]["qty"] == "360"


def test_merge_confirmed_without_date_keeps_shape():
    base = parse_response('{"rows":[{"process":"打磨","name":"李","qty":1,"unit_price":1,"defect":0}]}')
    rows = merge_confirmed(base, {"row_1.qty": "360"})
    assert "date" not in rows[0]


# =====================================================================
# F16：恒 False 的死代码块已移除（此守卫防回潮）
# =====================================================================

def test_dead_placeholder_block_removed():
    src = Path(__file__).resolve().parent.parent / "core" / "ocr_pipe.py"
    text = src.read_text(encoding="utf-8")
    assert "for k in ()" not in text
    assert "for k in ()" not in text


# =====================================================================
# F18：data URL 的 MIME 按扩展名选
# =====================================================================

def test_mime_by_extension():
    assert _mime_for("/tmp/a.png") == "image/png"
    assert _mime_for("/tmp/a.JPG") == "image/jpeg"
    assert _mime_for("/tmp/a.jpeg") == "image/jpeg"
    assert _mime_for("/tmp/a.webp") == "image/webp"
    assert _mime_for("/tmp/a.gif") == "image/gif"
    assert _mime_for("/tmp/无扩展名") == "image/png"   # 兜底 png


# =====================================================================
# F19：JSON 抠取逐块尝试（贪婪失败不再整段放弃）
# =====================================================================

def test_extract_json_prefers_greedy_span():
    obj, _ = _extract_json('前置废话 {"rows": [{"qty": 1}]} 后置废话')
    assert obj == {"rows": [{"qty": 1}]}


def test_extract_json_falls_back_to_first_block():
    # 贪婪整段不成（两个 JSON 段）→ 逐块试，取第一个可解析的
    obj, _ = _extract_json('说明 {"a": 1} 更多说明 {"b": 2}')
    assert obj == {"a": 1}


def test_extract_json_trailing_garbage_with_brace():
    # 结尾的 } 在字符串里/残缺：逐块尝试要能跳过坏候选找到好候选
    obj, _ = _extract_json('```json\n{"sheet_no": "SJ-1", "rows": []}\n``` }')
    assert obj == {"sheet_no": "SJ-1", "rows": []}


def test_extract_json_none_when_no_braces():
    obj, seen = _extract_json("完全没有大括号")
    assert obj is None and seen is False


def test_extract_json_seen_when_only_broken_candidates():
    obj, seen = _extract_json('{"sheet_no": SJ-1}')
    assert obj is None and seen is True


def test_parse_broken_candidates_error_message():
    r = parse_response('前缀 {"a": b} 后缀 {"c": d}')
    assert r.needs_review is True
    assert any("JSON 解析失败" in e for e in r.errors)
