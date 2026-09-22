# -*- coding: utf-8 -*-
"""core/engine.py 计件核算引擎测试（TDD 先行，测试先于实现编写）。

口径（江苏版演示基准，全部数值已用 python3 + Decimal 实测核对）：
- 月切日：上月 26 日 → 本月 25 日为一个核算期，期标签 = 本月 "YYYY-MM"
- 个人计件：合格数量 = 数量 − 报废；金额 = 合格数量 × 单价，按工票行四舍五入到分
- 班组计件：按个人数量占比分摊班组总额；Decimal + 银行家舍入；尾差挂「调整」
- 报废对账：应产 = 实报 + 报废 + 下落不明，四列对平
- 个税：演示用月度速算表（非累计预扣法，演示口径）

红线：所有金额断言用 Decimal 精确相等，绝不与 float 比较近似值。
"""
import random
from decimal import Decimal as D

import pytest

from core.engine import (
    CUTOFF_DAY,
    MONTHLY_TAX_TABLE,
    TAX_THRESHOLD,
    PieceRecord,
    TeamMember,
    individual_piecepay,
    in_period,
    monthly_income_tax,
    payslip,
    period_bounds,
    period_of,
    q2,
    scrap_reconcile,
    settle_by_period,
    team_piecepay,
)


def is_cents(x):
    """金额必须是精确到分的 Decimal（指数 -2）。"""
    return x.as_tuple().exponent == -2


def rec(name="王建国", process="冲压", qty=100, price="0.35", defect=0, date=None):
    return PieceRecord.from_dict({
        "name": name, "process": process, "qty": qty,
        "unit_price": price, "defect": defect, "date": date,
    })


# =====================================================================
# 月切日：上月 26 日 → 本月 25 日
# =====================================================================

class TestPeriodOf:
    def test_cutoff_day_constant(self):
        assert CUTOFF_DAY == 26

    def test_25th_belongs_to_current_period(self):
        # 25 日是本期最后一天
        assert period_of("2026-09-25") == "2026-09"

    def test_26th_belongs_to_next_period(self):
        # 26 日是下期第一天（边界）
        assert period_of("2026-09-26") == "2026-10"

    def test_mid_month(self):
        assert period_of("2026-09-01") == "2026-09"
        assert period_of("2026-09-15") == "2026-09"

    def test_year_rollover(self):
        # 12 月 26 日跨年 → 次年 1 月期
        assert period_of("2026-12-26") == "2027-01"
        assert period_of("2026-12-25") == "2026-12"

    def test_jan_26_feeds_feb(self):
        assert period_of("2026-01-26") == "2026-02"

    @pytest.mark.parametrize("bad", [
        "2026-9-5",      # 未补零
        "2026/09/25",    # 分隔符错
        "2026-13-01",    # 月越界
        "2026-02-30",    # 日越界（非闰年）
        "20260925",      # 无分隔
        "", "not-a-date", None, 20260925,
    ])
    def test_invalid_date_raises(self, bad):
        with pytest.raises(ValueError) as ei:
            period_of(bad)
        assert "日期" in str(ei.value)  # 确认是日期校验在报错，不是别处


class TestPeriodBounds:
    def test_september(self):
        assert period_bounds("2026-09") == ("2026-08-26", "2026-09-25")

    def test_january_crosses_year(self):
        assert period_bounds("2026-01") == ("2025-12-26", "2026-01-25")

    def test_december_label(self):
        assert period_bounds("2026-12") == ("2026-11-26", "2026-12-25")

    def test_bounds_are_strings(self):
        start, end = period_bounds("2026-09")
        assert isinstance(start, str) and isinstance(end, str)

    @pytest.mark.parametrize("bad", ["2026-13", "2026-00", "26-09", "2026-9", "20269", "", None])
    def test_invalid_period_raises(self, bad):
        with pytest.raises(ValueError) as ei:
            period_bounds(bad)
        assert "核算期" in str(ei.value)


class TestInPeriod:
    def test_first_day_in(self):
        assert in_period("2026-08-26", "2026-09") is True

    def test_day_before_start_out(self):
        assert in_period("2026-08-25", "2026-09") is False

    def test_last_day_in(self):
        assert in_period("2026-09-25", "2026-09") is True

    def test_day_after_end_out(self):
        assert in_period("2026-09-26", "2026-09") is False

    def test_mid_month_in(self):
        assert in_period("2026-09-10", "2026-09") is True

    def test_invalid_inputs_raise(self):
        with pytest.raises(ValueError) as ei:
            in_period("2026-09-26", "2026-13")
        assert "核算期" in str(ei.value)
        with pytest.raises(ValueError) as ei:
            in_period("bad", "2026-09")
        assert "日期" in str(ei.value)


# =====================================================================
# 个人计件：合格数量 × 单价，按行取整到分
# =====================================================================

class TestIndividualPiecepay:
    def test_basic(self):
        result = individual_piecepay([rec(qty=100, price="0.35")])
        assert len(result.lines) == 1
        line = result.lines[0]
        assert line.qualified_qty == D(100)
        assert line.amount == D("35.00")
        assert is_cents(line.amount)

    def test_defect_deducted(self):
        result = individual_piecepay([rec(qty=100, price="0.35", defect=6)])
        line = result.lines[0]
        assert line.qualified_qty == D(94)
        assert line.amount == D("32.90")

    def test_zero_defect_full_pay(self):
        # 边界：0 废品
        result = individual_piecepay([rec(qty=480, price="0.35", defect=0)])
        assert result.lines[0].amount == D("168.00")
        assert result.total_defect == D(0)

    def test_all_defect_zero_pay(self):
        # 边界：全废品（报废 = 数量）
        result = individual_piecepay([rec(qty=50, price="1.20", defect=50)])
        assert result.lines[0].qualified_qty == D(0)
        assert result.lines[0].amount == D("0.00")
        assert result.total_amount == D("0.00")

    def test_zero_qty(self):
        result = individual_piecepay([rec(qty=0, price="1.20", defect=0)])
        assert result.lines[0].amount == D("0.00")

    def test_bankers_rounding_half_cent_down(self):
        # 0.005 恰在半分：银行家舍入取偶 → 0.00
        result = individual_piecepay([rec(qty=1, price="0.005")])
        assert result.lines[0].amount == D("0.00")

    def test_bankers_rounding_half_cent_up(self):
        # 0.015 恰在半分：取偶 → 0.02
        result = individual_piecepay([rec(qty=3, price="0.005")])
        assert result.lines[0].amount == D("0.02")

    def test_decimal_exactness_against_float_trap(self):
        # float 0.29*3 = 0.8699999…；Decimal 必须精确 0.87
        result = individual_piecepay([rec(qty=3, price="0.29")])
        assert result.lines[0].amount == D("0.87")
        assert result.lines[0].amount == q2(D("0.87"))

    def test_multi_row_multi_person_aggregation(self):
        result = individual_piecepay([
            rec(name="王建国", process="冲压", qty=100, price="0.35"),
            rec(name="王建国", process="折弯", qty=40, price="1.25"),
            rec(name="李秀兰", process="冲压", qty=200, price="0.35"),
        ])
        # 按人汇总：35.00 + 50.00 = 85.00 / 70.00
        assert [p.name for p in result.by_person] == ["王建国", "李秀兰"]  # 首次出现顺序
        assert result.by_person[0].amount == D("85.00")
        assert result.by_person[0].qualified_qty == D(140)
        assert result.by_person[0].defect_qty == D(0)
        assert result.by_person[1].amount == D("70.00")
        assert result.total_amount == D("155.00")
        assert is_cents(result.total_amount)

    def test_person_defect_aggregated(self):
        result = individual_piecepay([
            rec(name="王建国", qty=100, price="1.00", defect=3),
            rec(name="王建国", qty=50, price="1.00", defect=2),
        ])
        assert result.by_person[0].defect_qty == D(5)
        assert result.by_person[0].qualified_qty == D(145)
        assert result.total_defect == D(5)
        assert result.total_amount == D("145.00")

    def test_empty_records_ok(self):
        result = individual_piecepay([])
        assert result.lines == ()
        assert result.total_amount == D("0.00")

    def test_line_rounding_before_sum(self):
        # 每行独立取整到分再汇总（工票口径），两行 0.005 → 0.00 + 0.00
        result = individual_piecepay([
            rec(name="王建国", qty=1, price="0.005"),
            rec(name="李秀兰", qty=1, price="0.005"),
        ])
        assert result.total_amount == D("0.00")

    def test_gen_samples_shaped_rows(self):
        # 与 tools/gen_samples.py 真值同构的行（缺 defect/date 键也能解析）
        r = PieceRecord.from_dict({"process": "冲压", "name": "王建国",
                                   "qty": 120, "unit_price": 1.25})
        assert r.defect == D(0)
        assert r.date is None
        assert individual_piecepay([r]).total_amount == D("150.00")


class TestPieceRecordValidation:
    def test_defect_exceeds_qty_raises(self):
        with pytest.raises(ValueError) as ei:
            rec(qty=10, defect=11)
        assert "报废数量" in str(ei.value) and "超过" in str(ei.value)

    def test_negative_qty_raises(self):
        with pytest.raises(ValueError) as ei:
            rec(qty=-1)
        assert "数量不能为负" in str(ei.value)

    def test_negative_price_raises(self):
        with pytest.raises(ValueError) as ei:
            rec(price="-0.35")
        assert "单价不能为负" in str(ei.value)

    def test_negative_defect_raises(self):
        with pytest.raises(ValueError) as ei:
            rec(qty=10, defect=-1)
        assert "报废数量不能为负" in str(ei.value)

    @pytest.mark.parametrize("bad_name", ["", "  ", None])
    def test_bad_name_raises(self, bad_name):
        with pytest.raises(ValueError) as ei:
            rec(name=bad_name)
        assert "姓名" in str(ei.value)

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError) as ei:
            PieceRecord.from_dict({"name": "王建国", "process": "冲压",
                                   "qty": 1, "unit_price": "1", "unite_price": "2"})
        assert "未知字段" in str(ei.value)

    def test_missing_required_key_raises(self):
        with pytest.raises(ValueError) as ei:
            PieceRecord.from_dict({"name": "王建国", "qty": 1, "unit_price": "1"})
        assert "缺少必填字段" in str(ei.value)

    def test_qty_as_string_parses(self):
        r = rec(qty="100")
        assert r.qty == D(100)

    def test_qty_as_float_parses_via_literal(self):
        r = rec(qty=100.0)
        assert r.qty == D(100)

    def test_bool_qty_rejected(self):
        # bool 是 int 子类，必须在金额字段挡住
        with pytest.raises(ValueError) as ei:
            rec(qty=True)
        assert "布尔值" in str(ei.value)
        with pytest.raises(ValueError) as ei:
            rec(price=True)
        assert "布尔值" in str(ei.value)

    def test_nan_price_rejected(self):
        with pytest.raises(ValueError) as ei:
            rec(price="NaN")
        assert "有限数" in str(ei.value)
        with pytest.raises(ValueError) as ei:
            rec(price="Infinity")
        assert "有限数" in str(ei.value)

    def test_garbage_price_rejected(self):
        with pytest.raises(ValueError) as ei:
            rec(price="abc")
        assert "无法解析" in str(ei.value)
        with pytest.raises(ValueError) as ei:
            rec(price="")
        assert "不能为空" in str(ei.value)

    def test_bad_date_rejected(self):
        with pytest.raises(ValueError) as ei:
            rec(date="2026-9-5")
        assert "日期格式" in str(ei.value)

    def test_valid_date_kept(self):
        assert rec(date="2026-09-25").date == "2026-09-25"

    def test_frozen(self):
        r = rec()
        with pytest.raises(Exception) as ei:
            r.qty = 5
        # frozen dataclass 的 FrozenInstanceError 是 AttributeError 子类
        assert isinstance(ei.value, AttributeError)


# =====================================================================
# 班组计件：按数量占比分摊，尾差挂「调整」
# =====================================================================

class TestTeamPiecepay:
    def test_even_split_no_residual(self):
        result = team_piecepay("100.00", [
            TeamMember("王建国", 50), TeamMember("李秀兰", 50)])
        assert result.residual == D("0.00")
        assert [l.amount for l in result.lines] == [D("50.00"), D("50.00")]
        assert all(l.adjust_amount == D("0.00") for l in result.lines)
        assert result.allocated_total == D("100.00")

    def test_positive_residual_to_adjust_line(self):
        # 100.00 / 3 → 33.33×3 = 99.99，尾差 +0.01 挂调整（并列最大取第一个）
        result = team_piecepay("100.00", [
            TeamMember("甲", 1), TeamMember("乙", 1), TeamMember("丙", 1)])
        assert result.residual == D("0.01")
        assert [l.base_amount for l in result.lines] == [D("33.33")] * 3
        assert result.lines[0].adjust_amount == D("0.01")
        assert result.lines[0].amount == D("33.34")
        assert result.lines[1].amount == D("33.33")
        assert result.lines[2].amount == D("33.33")
        assert sum(l.amount for l in result.lines) == D("100.00")
        assert result.allocated_total == D("100.00")

    def test_negative_residual(self):
        # 100.00 / 6 → 16.67×6 = 100.02，尾差 −0.02
        members = [TeamMember(f"m{i}", 1) for i in range(6)]
        result = team_piecepay("100.00", members)
        assert result.residual == D("-0.02")
        assert result.lines[0].adjust_amount == D("-0.02")
        assert result.lines[0].amount == D("16.65")
        assert result.lines[0].remark == "尾差调整"
        assert all(l.remark is None for l in result.lines[1:])
        assert sum(l.amount for l in result.lines) == D("100.00")

    def test_bankers_tie_both_round_down(self):
        # 0.01 给 2 人：各 0.005，银行家舍入双双取偶 → 0.00，尾差 +0.01
        result = team_piecepay("0.01", [TeamMember("甲", 1), TeamMember("乙", 1)])
        assert [l.base_amount for l in result.lines] == [D("0.00"), D("0.00")]
        assert result.residual == D("0.01")
        assert sum(l.amount for l in result.lines) == D("0.01")

    def test_residual_goes_to_largest_qty(self):
        # 总额 1.00，数量 1/2/5（合计 8）：
        #   0.125→0.12（银行家取偶）、0.25、0.625→0.62（取偶），合计 0.99，尾差 +0.01 给最大数量者
        result = team_piecepay("1.00", [
            TeamMember("甲", 1), TeamMember("乙", 2), TeamMember("丙", 5)])
        assert [l.base_amount for l in result.lines] == [D("0.12"), D("0.25"), D("0.62")]
        assert result.lines[2].adjust_amount == D("0.01")
        assert result.lines[2].amount == D("0.63")
        assert sum(l.amount for l in result.lines) == D("1.00")

    def test_single_member_gets_all(self):
        result = team_piecepay("123.45", [TeamMember("甲", 7)])
        assert result.lines[0].amount == D("123.45")
        assert result.residual == D("0.00")
        assert result.lines[0].adjust_amount == D("0.00")

    def test_zero_total(self):
        result = team_piecepay("0", [TeamMember("甲", 5), TeamMember("乙", 3)])
        assert all(l.amount == D("0.00") for l in result.lines)
        assert result.residual == D("0.00")

    def test_total_quantized_first(self):
        # 班组总额超过 2 位小数：先按分取整（银行家：100.005 → 100.00）再分摊
        result = team_piecepay("100.005", [
            TeamMember("甲", 1), TeamMember("乙", 1), TeamMember("丙", 1)])
        assert result.team_total == D("100.00")
        assert sum(l.amount for l in result.lines) == D("100.00")

    def test_zero_qty_all_with_positive_total_raises(self):
        with pytest.raises(ValueError) as ei:
            team_piecepay("10.00", [TeamMember("甲", 0), TeamMember("乙", 0)])
        assert "无法按比例分摊" in str(ei.value)

    def test_zero_qty_all_with_zero_total_ok(self):
        result = team_piecepay("0", [TeamMember("甲", 0), TeamMember("乙", 0)])
        assert all(l.amount == D("0.00") for l in result.lines)

    def test_empty_members_raises(self):
        with pytest.raises(ValueError) as ei:
            team_piecepay("10.00", [])
        assert "没有成员" in str(ei.value)

    def test_negative_total_raises(self):
        with pytest.raises(ValueError) as ei:
            team_piecepay("-1.00", [TeamMember("甲", 1)])
        assert "总额不能为负" in str(ei.value)

    def test_negative_member_qty_raises(self):
        with pytest.raises(ValueError) as ei:
            TeamMember("甲", -1)
        assert "数量不能为负" in str(ei.value)

    def test_member_from_dict(self):
        m = TeamMember.from_dict({"name": "王建国", "qty": 5})
        assert m.name == "王建国" and m.qty == D(5)
        with pytest.raises(ValueError) as ei:
            TeamMember.from_dict({"name": "王建国"})
        assert "缺少字段" in str(ei.value)
        with pytest.raises(ValueError) as ei:
            TeamMember.from_dict({"name": "王建国", "qty": 1, "extra": 2})
        assert "未知字段" in str(ei.value)

    def test_all_amounts_are_cents(self):
        members = [TeamMember(f"m{i}", q) for i, q in enumerate([3, 7, 11, 13])]
        result = team_piecepay("99.99", members)
        assert all(is_cents(l.base_amount) and is_cents(l.amount) for l in result.lines)
        assert is_cents(result.residual)

    def test_property_sum_invariant_and_adjust_prefix(self):
        """随机参数下：分摊合计恒等于总额；所有金额非负；承担调整的行是
        数量降序前缀（每行数量 ≥ 任何未调整行，并列按名单序）。"""
        rng = random.Random(20260919)
        for _ in range(200):
            n = rng.randint(2, 9)
            qtys = [rng.randint(0, 500) for _ in range(n)]
            if sum(qtys) == 0:
                qtys[rng.randrange(n)] = 1
            total = D(rng.randint(1, 9999999)) / D(100)  # 0.01 ~ 99999.99
            members = [TeamMember(f"m{i}", q) for i, q in enumerate(qtys)]
            result = team_piecepay(total, members)
            # 不变量：合计恒等
            assert sum(l.amount for l in result.lines) == result.team_total == q2(total)
            # 每行金额精确到分且不为负（F3：负尾差逐级转嫁后不许出负数）
            assert all(is_cents(l.amount) for l in result.lines)
            assert all(l.amount >= 0 for l in result.lines)
            # 尾差幅度 ≤ 半分 × 人数
            assert abs(result.residual) <= D("0.005") * n
            # 承担调整的行必须是数量降序前缀（转嫁只往数量更大的行走）
            adjusted = [l for l in result.lines if l.adjust_amount != 0]
            for a in adjusted:
                for l in result.lines:
                    if l.adjust_amount == 0:
                        assert a.qty >= l.qty
            # 比例性：每人金额与精确占比之差在分以下
            team_qty = sum(qtys)
            for l in result.lines:
                exact = result.team_total * l.qty / team_qty
                assert abs(l.amount - exact) < D("0.01") * n


# =====================================================================
# 报废对账：应产 / 实报 / 报废 / 下落不明 四列对平
# =====================================================================

class TestScrapReconcile:
    def test_balanced(self):
        rows = [rec(name="王建国", qty=90, defect=3),
                rec(name="李秀兰", qty=10, defect=0)]
        r = scrap_reconcile("100", rows)
        assert r.expected_qty == D(100)
        assert r.reported_qualified == D(97)   # (90-3) + 10
        assert r.scrapped_qty == D(3)
        assert r.missing_qty == D(0)
        assert r.status == "平"
        # 四列恒等式
        assert r.reported_qualified + r.scrapped_qty + r.missing_qty == r.expected_qty

    def test_zero_defect(self):
        # 边界：0 废品
        rows = [rec(qty=60, defect=0), rec(qty=40, defect=0)]
        r = scrap_reconcile("100", rows)
        assert r.scrapped_qty == D(0)
        assert r.missing_qty == D(0)
        assert r.status == "平"

    def test_all_defect(self):
        # 边界：全废品
        rows = [rec(qty=50, defect=50)]
        r = scrap_reconcile("50", rows)
        assert r.reported_qualified == D(0)
        assert r.scrapped_qty == D(50)
        assert r.missing_qty == D(0)
        assert r.status == "平"

    def test_gap_reported(self):
        # 应产 120，实报+报废 = 100 → 下落不明 20
        rows = [rec(qty=90, defect=3), rec(qty=10, defect=0)]
        r = scrap_reconcile("120", rows)
        assert r.missing_qty == D(20)
        assert r.status == "缺口"

    def test_over_reported(self):
        rows = [rec(qty=90, defect=3), rec(qty=10, defect=0)]
        r = scrap_reconcile("90", rows)
        assert r.missing_qty == D(-10)
        assert r.status == "超报"

    def test_tolerance_absorbs_gap(self):
        rows = [rec(qty=90, defect=3), rec(qty=10, defect=0)]
        assert scrap_reconcile("102", rows, tolerance="2").status == "平"
        assert scrap_reconcile("102", rows, tolerance="1.99").status == "缺口"

    def test_empty_rows_full_gap(self):
        r = scrap_reconcile("30", [])
        assert r.reported_qualified == D(0)
        assert r.scrapped_qty == D(0)
        assert r.missing_qty == D(30)
        assert r.status == "缺口"

    def test_empty_rows_zero_expected(self):
        r = scrap_reconcile("0", [])
        assert r.missing_qty == D(0)
        assert r.status == "平"

    def test_negative_expected_raises(self):
        with pytest.raises(ValueError) as ei:
            scrap_reconcile("-1", [rec(qty=1)])
        assert "应产数量不能为负" in str(ei.value)

    def test_negative_tolerance_raises(self):
        with pytest.raises(ValueError) as ei:
            scrap_reconcile("10", [rec(qty=10)], tolerance="-0.01")
        assert "容差不能为负" in str(ei.value)

    def test_bad_rows_raise(self):
        with pytest.raises(ValueError) as ei:
            scrap_reconcile("10", [rec(qty=5, defect=6)])
        assert "超过" in str(ei.value)

    def test_decimal_types(self):
        r = scrap_reconcile("100.5", [rec(qty=100, defect=0)])
        assert r.missing_qty == D("0.5")
        assert isinstance(r.expected_qty, D)
        assert isinstance(r.reported_qualified, D)
        assert isinstance(r.scrapped_qty, D)


# =====================================================================
# 个税：演示用月度速算表（注明演示口径，非累计预扣法）
# =====================================================================

class TestMonthlyIncomeTax:
    def test_zero_gross(self):
        assert monthly_income_tax("0") == D("0.00")

    def test_below_threshold(self):
        assert monthly_income_tax("4999.99") == D("0.00")

    def test_exactly_threshold(self):
        # 起征点本身不计税
        assert monthly_income_tax("5000") == D("0.00")

    def test_bracket1(self):
        # 应税 3000 → 3% → 90.00（税率级边界）
        assert monthly_income_tax("8000") == D("90.00")

    def test_bracket2_just_above_3000(self):
        # 应税 3000.01 → 10% − 210 = 90.001 → 90.00（级边界连续）
        assert monthly_income_tax("8000.01") == D("90.00")

    def test_bracket2_at_12000(self):
        # 应税 12000 → 10% 档顶 → 990.00
        assert monthly_income_tax("17000") == D("990.00")

    def test_bracket3_just_above_12000(self):
        assert monthly_income_tax("17000.01") == D("990.00")

    def test_top_bracket(self):
        # 应税 100000 → 45% − 15160 = 29840.00
        assert monthly_income_tax("105000") == D("29840.00")

    def test_deductions_reduce_taxable(self):
        # 8000 − 5000 − 500 = 2500 → 3% → 75.00
        assert monthly_income_tax("8000", deductions="500") == D("75.00")

    def test_deductions_floor_at_zero(self):
        # 扣除把应税压到 0 以下 → 税 0
        assert monthly_income_tax("8000", deductions="9999") == D("0.00")

    def test_threshold_override(self):
        assert monthly_income_tax("4000", threshold="1000") == D("90.00")

    def test_bankers_tie_rounds_to_even_up(self):
        # 应税 3010.15 → 301.015 − 210 = 91.015 → 银行家舍入 → 91.02
        assert monthly_income_tax("8010.15") == D("91.02")

    def test_bankers_tie_rounds_to_even_down(self):
        # 应税 3010.05 → 91.005 → 银行家舍入 → 91.00
        assert monthly_income_tax("8010.05") == D("91.00")

    def test_returns_cents_decimal(self):
        t = monthly_income_tax("8000")
        assert isinstance(t, D)
        assert is_cents(t)

    def test_negative_gross_raises(self):
        with pytest.raises(ValueError) as ei:
            monthly_income_tax("-1")
        assert "收入不能为负" in str(ei.value)

    def test_negative_deductions_raises(self):
        with pytest.raises(ValueError) as ei:
            monthly_income_tax("8000", deductions="-1")
        assert "扣除项不能为负" in str(ei.value)

    def test_negative_threshold_raises(self):
        with pytest.raises(ValueError) as ei:
            monthly_income_tax("8000", threshold="-5000")
        assert "起征点不能为负" in str(ei.value)

    def test_table_is_monotonic_at_every_boundary(self):
        """税率表性质：每个级距边界上下 0.01 的税额不减（速算扣除数正确）。"""
        for upper, _rate, _qd in MONTHLY_TAX_TABLE:
            if upper is None:
                continue
            below = monthly_income_tax(upper + TAX_THRESHOLD)
            above = monthly_income_tax(upper + TAX_THRESHOLD + D("0.01"))
            assert above >= below, f"级距 {upper} 边界不单调"


# =====================================================================
# 工资单组装
# =====================================================================

class TestPayslip:
    def test_basic(self):
        p = payslip("8000")
        assert p.gross == D("8000.00")
        assert p.tax == D("90.00")
        assert p.net == D("7910.00")
        assert p.net + p.tax == p.gross

    def test_zero_tax(self):
        p = payslip("3000")
        assert p.tax == D("0.00")
        assert p.net == D("3000.00")

    def test_zero_gross(self):
        p = payslip("0")
        assert p.gross == D("0.00") and p.tax == D("0.00") and p.net == D("0.00")

    def test_deductions_passed_through(self):
        p = payslip("8000", deductions="500")
        assert p.tax == D("75.00")
        assert p.net == D("7925.00")

    def test_all_cents(self):
        p = payslip("8010.15")
        assert is_cents(p.gross) and is_cents(p.tax) and is_cents(p.net)


# =====================================================================
# 按月切日结算
# =====================================================================

class TestSettleByPeriod:
    def test_records_split_across_cutoff(self):
        # 边界：同在 9 月，24 日属 09 期、26 日属 10 期
        records = [
            rec(name="王建国", qty=100, price="0.35", date="2026-09-24"),
            rec(name="王建国", qty=100, price="0.35", date="2026-09-26"),
            rec(name="李秀兰", qty=200, price="0.50", date="2026-10-02"),
        ]
        settled = settle_by_period(records)
        assert set(settled) == {"2026-09", "2026-10"}
        assert settled["2026-09"].total_amount == D("35.00")
        assert settled["2026-10"].total_amount == D("135.00")

    def test_cutoff_boundary_days(self):
        settled = settle_by_period([
            rec(qty=1, price="1.00", date="2026-08-26"),  # 09 期首日
            rec(qty=1, price="1.00", date="2026-08-25"),  # 08 期最后一天
        ])
        assert set(settled) == {"2026-08", "2026-09"}

    def test_year_rollover_in_settle(self):
        settled = settle_by_period([
            rec(qty=1, price="1.00", date="2026-12-26"),
        ])
        assert set(settled) == {"2027-01"}

    def test_missing_date_raises(self):
        with pytest.raises(ValueError) as ei:
            settle_by_period([rec(qty=1, price="1.00", date=None)])
        assert "缺少日期" in str(ei.value)

    def test_empty(self):
        assert settle_by_period([]) == {}

    def test_keys_sorted(self):
        settled = settle_by_period([
            rec(qty=1, price="1.00", date="2026-11-26"),
            rec(qty=1, price="1.00", date="2026-09-01"),
            rec(qty=1, price="1.00", date="2026-10-01"),
        ])
        assert list(settled) == ["2026-09", "2026-10", "2026-12"]


# =====================================================================
# q2 助手
# =====================================================================

class TestQ2:
    def test_bankers_ties(self):
        assert q2(D("0.005")) == D("0.00")
        assert q2(D("0.015")) == D("0.02")
        assert q2(D("100.005")) == D("100.00")
        assert q2(D("100.015")) == D("100.02")

    def test_non_tie(self):
        assert q2(D("1.004")) == D("1.00")
        assert q2(D("1.006")) == D("1.01")

    def test_returns_cents(self):
        assert is_cents(q2(D("2")))
        assert q2(2) == D("2.00")


# =====================================================================
# F2：幅度/有效位数上限（超限 ValueError，绝不漏成 InvalidOperation→500）
# =====================================================================

class TestMagnitudeAndDigitCap:
    def test_gross_1e30_rejected_as_value_error(self):
        with pytest.raises(ValueError) as ei:
            payslip("1e30")
        assert "太大" in str(ei.value)

    def test_gross_1e999_rejected(self):
        with pytest.raises(ValueError) as ei:
            payslip("1e999")
        assert "太大" in str(ei.value)

    def test_qty_1e30_rejected(self):
        with pytest.raises(ValueError) as ei:
            rec(qty="1e30")
        assert "太大" in str(ei.value)

    def test_too_many_significant_digits_rejected(self):
        # 31 位有效数字（幅度正常）：位数超限也要拒
        with pytest.raises(ValueError) as ei:
            rec(qty="1." + "2" * 30)
        assert "位数" in str(ei.value)

    def test_1e12_boundary_allowed(self):
        # adjusted()==12（万亿级）仍在界内，能正常算
        result = individual_piecepay([rec(qty="1000000000000", price="0.01")])
        assert result.total_amount == D("10000000000.00")

    def test_30_digits_allowed_31_rejected(self):
        assert _to_decimal_ok("1234." + "5" * 26) is True      # 30 位
        assert _to_decimal_ok("1234." + "5" * 27) is False     # 31 位

    def test_q2_invalidoperation_becomes_value_error(self):
        # 兜底：即便有漏网的大数进到 quantize，也必须转 ValueError 而不是
        # 抛 InvalidOperation（服务端会变 500）
        with pytest.raises(ValueError) as ei:
            q2(D("1e30"))
        assert "太大" in str(ei.value)


def _to_decimal_ok(s) -> bool:
    from core.engine import _to_decimal
    try:
        _to_decimal(s, "数值")
        return True
    except ValueError:
        return False


# =====================================================================
# F3：班组负金额——承接行不许为负，负余额逐级转嫁数量次大者
# =====================================================================

class TestTeamNoNegativeAmounts:
    def test_003_over_5_members(self):
        # 0.03/5 人（各 1）：base 全 0.01，尾差 -0.02 → 前两行各扣到 0.00
        result = team_piecepay("0.03", [TeamMember(f"m{i}", 1) for i in range(5)])
        assert all(l.amount >= 0 for l in result.lines)
        assert [l.amount for l in result.lines] == \
            [D("0.00"), D("0.00"), D("0.01"), D("0.01"), D("0.01")]
        assert sum(l.amount for l in result.lines) == D("0.03")
        assert result.allocated_total == D("0.03")
        assert all(is_cents(l.amount) for l in result.lines)

    def test_004_over_7_members(self):
        # 0.04/7 人（各 1）：尾差 -0.03 → 级联三次
        result = team_piecepay("0.04", [TeamMember(f"m{i}", 1) for i in range(7)])
        assert all(l.amount >= 0 for l in result.lines)
        assert [l.amount for l in result.lines] == \
            [D("0.00")] * 3 + [D("0.01")] * 4
        assert sum(l.amount for l in result.lines) == D("0.04")
        assert result.allocated_total == D("0.04")

    def test_transfer_goes_to_next_largest_by_qty(self):
        # 数量不等时：承接链按数量降序走（转嫁只落在数量更大的行上）
        members = [TeamMember("大", 9), TeamMember("中", 5), TeamMember("小", 1)]
        result = team_piecepay("0.01", members)
        assert all(l.amount >= 0 for l in result.lines)
        assert sum(l.amount for l in result.lines) == D("0.01")
        # 承担调整的行必须是数量降序前缀（数量不小于任何未调整行）
        adjusted = [l for l in result.lines if l.adjust_amount != 0]
        for a in adjusted:
            for l in result.lines:
                if l.adjust_amount == 0:
                    assert a.qty >= l.qty


# =====================================================================
# F7：individual/个税路径 prec=50 对齐班组（29 位边界不因 prec=28 走样）
# =====================================================================

class TestPrecisionBoundary:
    def test_individual_29_digit_price_exact(self):
        # 单价 0.005 + 1e-31（29 位有效数字）：prec=28 会把乘积截成 0.005 平局
        # 银行家舍入得 0.00；prec=50 精确过半得 0.01（正确值）
        price = "0.005" + "0" * 27 + "1"
        assert len(D(price).as_tuple().digits) == 29
        result = individual_piecepay([rec(qty=1, price=price)])
        assert result.total_amount == D("0.01")

    def test_tax_29_digit_deduction_exact(self):
        # 扣除项 30 位：prec=28 会把应税的乘积舍到错误一侧（4.99），
        # 精确值 4.985-1e-28 不到半分 → 4.98
        ded = "833.833333333333333333333333334"
        assert len(D(ded).as_tuple().digits) == 30
        assert monthly_income_tax("6000", deductions=ded) == D("4.98")

    def test_payslip_path_inherits_precision(self):
        p = payslip("6000", deductions="833.833333333333333333333333334")
        assert p.tax == D("4.98")


# =====================================================================
# F8：全角数字（Unicode digits）不许混过正则
# =====================================================================

class TestFullwidthDigitsRejected:
    def test_fullwidth_period_rejected(self):
        # \d 会匹配全角数字，int() 还能把它无声换算成 2026 —— 必须拒
        with pytest.raises(ValueError) as ei:
            period_bounds("２０２６-09")
        assert "核算期" in str(ei.value)

    def test_fullwidth_date_rejected(self):
        with pytest.raises(ValueError) as ei:
            period_of("２０２６-０９-２５")
        assert "日期" in str(ei.value)

    def test_fullwidth_period_in_in_period_rejected(self):
        with pytest.raises(ValueError) as ei:
            in_period("2026-09-01", "２０２６-09")
        assert "核算期" in str(ei.value)

    def test_ascii_period_still_works(self):
        assert period_bounds("2026-09") == ("2026-08-26", "2026-09-25")


# =====================================================================
# F9：核算期年份 1..9999
# =====================================================================

class TestPeriodYearRange:
    def test_year_zero_rejected(self):
        with pytest.raises(ValueError) as ei:
            period_bounds("0000-01")
        assert "年份" in str(ei.value)

    def test_year_rollover_past_9999_rejected(self):
        # 9999-12-26 归期会生成 10000-01（非法标签）→ 必须报错而不是吐坏标签
        with pytest.raises(ValueError) as ei:
            period_of("9999-12-26")
        assert "核算期" in str(ei.value)  # 格式或年份检查任一拦截均可

    def test_year_9999_december_ok(self):
        assert period_of("9999-12-25") == "9999-12"
        assert period_bounds("9999-12") == ("9999-11-26", "9999-12-25")

    def test_year_0001_ok(self):
        assert period_bounds("0001-01") == ("0000-12-26", "0001-01-25")


# =====================================================================
# F23：同名语义（个人按名合并 / 班组不合并）——行为锁定
# =====================================================================

class TestSameNameSemantics:
    def test_individual_merges_same_name(self):
        result = individual_piecepay([
            rec(name="王建国", qty=100, price="0.35"),
            rec(name="王建国", qty=40, price="1.25"),
        ])
        assert len(result.by_person) == 1
        assert result.by_person[0].amount == D("85.00")
        assert len(result.lines) == 2  # 行级仍逐行

    def test_team_does_not_merge_same_name(self):
        result = team_piecepay("100.00", [
            TeamMember("王建国", 1), TeamMember("王建国", 3)])
        assert len(result.lines) == 2  # 班组：名单行=分摊行，重名不合并
        assert sum(l.amount for l in result.lines) == D("100.00")


# =====================================================================
# 红线：纯函数零 IO（对 engine 源码做静态检查，机械兜底）
# =====================================================================

class TestPurity:
    _BANNED_MODULES = {"os", "sys", "io", "pathlib", "socket", "sqlite3",
                       "requests", "urllib", "http", "subprocess", "shutil"}
    _BANNED_CALLS = {"open", "eval", "exec", "__import__"}

    def _source_tree(self):
        import ast
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent
               / "core" / "engine.py").read_text(encoding="utf-8")
        return ast.parse(src)

    def test_no_io_module_imports(self):
        import ast
        for node in ast.walk(self._source_tree()):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in self._BANNED_MODULES, \
                        f"engine 禁止 import {alias.name}（纯函数零 IO）"
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                assert root not in self._BANNED_MODULES, \
                    f"engine 禁止 from {node.module} import（纯函数零 IO）"

    def test_no_io_calls(self):
        import ast
        for node in ast.walk(self._source_tree()):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in self._BANNED_CALLS, \
                    f"engine 禁止调用 {node.func.id}()（纯函数零 IO）"
