# -*- coding: utf-8 -*-
"""算件 · 计件核算引擎（core/engine.py）——纯函数，零 IO。

口径声明（演示基准，净室重写，不含任何真实公司数据/表单样式）：
- 规则基准：江苏版制造业计件通行做法的演示实现；上海版口径**未覆盖**。
- 月切日：核算期 = 上月 26 日 → 本月 25 日，期标签 "YYYY-MM" 取期内的"本月"。
  例：2026-08-26 ~ 2026-09-25 记为 2026-09 期。
- 个人计件：合格数量 = 数量 − 报废；金额 = 合格数量 × 单价，**按工票行**
  四舍五入到分（Decimal 银行家舍入 ROUND_HALF_EVEN）后汇总。
- 班组计件：按个人数量占比分摊班组总额；每人分摊额按分取整（银行家舍入），
  尾差（总额 − 分摊合计）优先挂「尾差调整」行，落在**数量最大**的成员头上
  （并列取名单顺序第一个）；承接后金额为负时，该行扣到 0 并把负余额顺次
  转嫁给数量次大者（任何成员分摊额不为负）——演示口径，保证分摊合计恒等于总额。
- 报废对账：应产 = 实报 + 报废 + 下落不明；下落不明 = 应产 − 实报 − 报废，
  为正=缺口（有产出对不上），为负=超报（报的比应产还多）。
- 个税：**演示口径**——按月换算的综个所得税速算表（起征 5000/月），
  非居民工资真实申报用的累计预扣法；数值仅供演示，不可用于真实报税。

数值纪律：全部金额用 decimal.Decimal 精确十进制运算，取整一律
ROUND_HALF_EVEN（银行家舍入）；金额运算包 localcontext(prec=50)（高于默认
28 位，消除多有效位输入在半分边界上的理论风险）；输入幅度/有效位数有上限
（|值| ≤ 1e12、有效位数 ≤ 30），超限抛 ValueError——绝不让 decimal 的
InvalidOperation 漏成服务端 500。禁止 float 参与任何金额运算
（float 入参经 str() 取字面值转 Decimal，仅供演示数据导入用）。

纯函数纪律：本模块不 import os/sys、不开文件、不碰网络、不读全局可变状态；
所有校验失败抛 ValueError（中文报错，说人话）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import Optional, Tuple

__all__ = [
    "CENTS", "CUTOFF_DAY", "TAX_THRESHOLD", "MONTHLY_TAX_TABLE",
    "PieceRecord", "PieceLine", "PersonPay", "IndividualPayResult",
    "TeamMember", "TeamAllocLine", "TeamAllocResult",
    "ScrapReconciliation", "Payslip",
    "q2", "period_of", "period_bounds", "in_period", "settle_by_period",
    "individual_piecepay", "team_piecepay", "scrap_reconcile",
    "monthly_income_tax", "payslip",
]

# ------------------------------------------------------------------
# 常量
# ------------------------------------------------------------------

CENTS = Decimal("0.01")
"""金额最小单位：分。"""

CUTOFF_DAY = 26
"""月切日：每月 26 日（含）起归下一核算期。"""

TAX_THRESHOLD = Decimal("5000")
"""个税演示口径月减除费用（起征点）。"""

ZERO = Decimal("0.00")

MAX_ADJUSTED = 12
"""输入幅度上限：|数值| < 10^13（adjusted() ≤ 12，万亿级封顶）。"""

MAX_DIGITS = 30
"""输入有效位数上限：超过 30 位有效数字视为脏数据，拒收。"""

# 按月换算的综合所得税率表（演示口径）：级数 | 月应纳税所得额上限(含) | 税率 | 速算扣除数
MONTHLY_TAX_TABLE: Tuple[Tuple[Optional[Decimal], Decimal, Decimal], ...] = (
    (Decimal("3000"), Decimal("0.03"), Decimal("0")),
    (Decimal("12000"), Decimal("0.10"), Decimal("210")),
    (Decimal("25000"), Decimal("0.20"), Decimal("1410")),
    (Decimal("35000"), Decimal("0.25"), Decimal("2660")),
    (Decimal("55000"), Decimal("0.30"), Decimal("4410")),
    (Decimal("80000"), Decimal("0.35"), Decimal("7160")),
    (None, Decimal("0.45"), Decimal("15160")),
)

_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_PERIOD_RE = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")


# ------------------------------------------------------------------
# 数值与日期基础
# ------------------------------------------------------------------

def _to_decimal(value, field: str = "数值") -> Decimal:
    """把 int/str/Decimal/float 转成 Decimal；bool/NaN/Inf/垃圾值一律拒绝。

    float 经 str() 取其字面最短表示再转（演示导入路径的 CSV/JSON 数字），
    避免 float 二进制误差直接进入金额运算。
    幅度/有效位数超上限（|值| ≥ 10^13 或 > 30 位有效数字）也拒——
    超大数会让 quantize 抛 InvalidOperation（服务端会变 500），必须在门口拦。
    """
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, bool):  # bool 是 int 子类，金额字段必须挡住
        raise ValueError(f"{field}不能是布尔值")
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, float):
        d = Decimal(str(value))
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            raise ValueError(f"{field}不能为空")
        try:
            d = Decimal(s)
        except InvalidOperation:
            raise ValueError(f"{field}无法解析为数值：{value!r}") from None
    else:
        raise TypeError(f"{field}的类型不支持：{type(value).__name__}")
    if not d.is_finite():
        raise ValueError(f"{field}必须是有限数，不接受 NaN/Infinity")
    if d != 0 and (d.adjusted() > MAX_ADJUSTED or len(d.as_tuple().digits) > MAX_DIGITS):
        raise ValueError(
            f"{field}数字太大或位数太多（幅度上限 1 万亿、有效位数上限 {MAX_DIGITS} 位）：{value!r}")
    return d


def q2(value) -> Decimal:
    """金额取整到分，银行家舍入（ROUND_HALF_EVEN）。

    面向外部输入：经 _to_decimal 做类型/幅度/位数校验。
    InvalidOperation（取整结果超出上下文精度）兜成 ValueError——
    服务端对 ValueError 报 400 人话，对 decimal 异常会变 500。
    内部中间量（多有效位的商/积）请走 _q2_raw，别在这里被位数上限误伤。
    """
    try:
        return _to_decimal(value, "金额").quantize(CENTS, rounding=ROUND_HALF_EVEN)
    except InvalidOperation:
        raise ValueError("金额数字太大，取整到分时超出可表示范围") from None


def _q2_raw(d: Decimal) -> Decimal:
    """内部中间量取整到分（银行家舍入）。

    不做输入校验（中间量的幅度已受输入上限控制，只是有效位数可能
    超过 _to_decimal 的 30 位门——比如 prec=50 的除法商）。
    """
    return d.quantize(CENTS, rounding=ROUND_HALF_EVEN)


def _num_str(value: Decimal) -> str:
    """API 序列化出口：Decimal → 精确字符串（金额/数量绝不走 float）。"""
    return str(value)


def _parse_date(value) -> date:
    """严格解析 "YYYY-MM-DD"（补零、日历合法），其余一律 ValueError。"""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError(f"日期格式应为 YYYY-MM-DD：{value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"日期无效（日历上不存在）：{value!r}") from None


def _parse_period(value) -> str:
    """严格解析核算期标签 "YYYY-MM"（ASCII 数字，年份 0001~9999）。"""
    if not isinstance(value, str) or not _PERIOD_RE.match(value):
        raise ValueError(f"核算期格式应为 YYYY-MM：{value!r}")
    year = int(value[:4])
    if not 1 <= year <= 9999:
        raise ValueError(f"核算期年份应为 0001~9999：{value!r}")
    return value


# ------------------------------------------------------------------
# 月切日
# ------------------------------------------------------------------

def period_of(day: str) -> str:
    """日期 → 所属核算期 "YYYY-MM"（上月 26 日 ~ 本月 25 日为一期）。

    26 日（含）以后归下一期；25 日（含）以前归本期。跨年自动进位。
    生成的期标签再过一遍 _parse_period（拦住 9999-12-26 → 10000-01 这类
    越界标签）。
    """
    d = _parse_date(day)
    if d.day >= CUTOFF_DAY:
        year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    else:
        year, month = d.year, d.month
    return _parse_period(f"{year:04d}-{month:02d}")


def period_bounds(period: str) -> Tuple[str, str]:
    """核算期标签 → (起始日, 截止日)，均为闭端 "YYYY-MM-DD" 字符串。

    例："2026-09" → ("2026-08-26", "2026-09-25")；"2026-01" 跨年 →
    ("2025-12-26", "2026-01-25")。
    """
    _parse_period(period)
    year, month = int(period[:4]), int(period[5:7])
    pyear, pmonth = (year - 1, 12) if month == 1 else (year, month - 1)
    return f"{pyear:04d}-{pmonth:02d}-{CUTOFF_DAY:02d}", f"{year:04d}-{month:02d}-25"


def in_period(day: str, period: str) -> bool:
    """日期是否落在核算期内（两端含）。"""
    start, end = period_bounds(period)
    d = _parse_date(day)
    return _parse_date(start) <= d <= _parse_date(end)


# ------------------------------------------------------------------
# 计件记录（工票行）
# ------------------------------------------------------------------

@dataclass(frozen=True)
class PieceRecord:
    """一条工票行（结构化预填的最小单位），构造即校验。

    qty/unit_price/defect 接受 int/str/Decimal/float，内部统一 Decimal。
    """

    process: str
    name: str
    qty: Decimal
    unit_price: Decimal
    defect: Decimal = Decimal("0")
    date: Optional[str] = None

    _DICT_FIELDS = frozenset({"process", "name", "qty", "unit_price", "defect", "date"})

    def __post_init__(self) -> None:
        for field in ("process", "name"):
            v = getattr(self, field)
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"{('工序' if field == 'process' else '姓名')}不能为空")
        object.__setattr__(self, "qty", _to_decimal(self.qty, "数量"))
        object.__setattr__(self, "unit_price", _to_decimal(self.unit_price, "单价"))
        object.__setattr__(self, "defect", _to_decimal(self.defect, "报废数量"))
        if self.qty < 0:
            raise ValueError(f"数量不能为负：{self.qty}")
        if self.unit_price < 0:
            raise ValueError(f"单价不能为负：{self.unit_price}")
        if self.defect < 0:
            raise ValueError(f"报废数量不能为负：{self.defect}")
        if self.defect > self.qty:
            raise ValueError(f"报废数量 {self.defect} 超过数量 {self.qty}（{self.name}）")
        if self.date is not None:
            _parse_date(self.date)

    @classmethod
    def from_dict(cls, d: dict) -> "PieceRecord":
        """严格模式 dict → PieceRecord：字段名写错/缺失立即报错，不静默吞。"""
        if not isinstance(d, dict):
            raise ValueError(f"工票行应为 dict，收到 {type(d).__name__}")
        unknown = set(d) - cls._DICT_FIELDS
        if unknown:
            raise ValueError(f"工票行有未知字段（多半是字段名写错）：{sorted(unknown)}")
        missing = {"process", "name", "qty", "unit_price"} - set(d)
        if missing:
            raise ValueError(f"工票行缺少必填字段：{sorted(missing)}")
        return cls(
            process=d["process"], name=d["name"], qty=d["qty"],
            unit_price=d["unit_price"],
            defect=d.get("defect", 0), date=d.get("date"),
        )

    def to_dict(self) -> dict:
        """序列化（纯导出）：数值字段一律字符串，date 缺省为 None。"""
        return {
            "process": self.process, "name": self.name,
            "qty": _num_str(self.qty), "unit_price": _num_str(self.unit_price),
            "defect": _num_str(self.defect), "date": self.date,
        }


# ------------------------------------------------------------------
# 个人计件
# ------------------------------------------------------------------

@dataclass(frozen=True)
class PieceLine:
    """一条已核算的工票行。"""

    process: str
    name: str
    qty: Decimal
    defect: Decimal
    qualified_qty: Decimal
    unit_price: Decimal
    amount: Decimal

    def to_dict(self) -> dict:
        """序列化（纯导出）：数值字段一律字符串。"""
        return {
            "process": self.process, "name": self.name,
            "qty": _num_str(self.qty), "defect": _num_str(self.defect),
            "qualified_qty": _num_str(self.qualified_qty),
            "unit_price": _num_str(self.unit_price),
            "amount": _num_str(self.amount),
        }


@dataclass(frozen=True)
class PersonPay:
    """按人汇总的计件结果（首次出现顺序）。"""

    name: str
    qualified_qty: Decimal
    defect_qty: Decimal
    amount: Decimal

    def to_dict(self) -> dict:
        """序列化（纯导出）：数值字段一律字符串。"""
        return {
            "name": self.name,
            "qualified_qty": _num_str(self.qualified_qty),
            "defect_qty": _num_str(self.defect_qty),
            "amount": _num_str(self.amount),
        }


@dataclass(frozen=True)
class IndividualPayResult:
    lines: Tuple[PieceLine, ...]
    by_person: Tuple[PersonPay, ...]
    total_amount: Decimal
    total_defect: Decimal

    def to_dict(self) -> dict:
        """序列化（纯导出）：数值字段一律字符串。"""
        return {
            "lines": [line.to_dict() for line in self.lines],
            "by_person": [p.to_dict() for p in self.by_person],
            "total_amount": _num_str(self.total_amount),
            "total_defect": _num_str(self.total_defect),
        }


def individual_piecepay(records) -> IndividualPayResult:
    """个人计件：每行 金额 = (数量 − 报废) × 单价，按行取整到分后汇总。

    records 为 PieceRecord 列表（dict 请先走 PieceRecord.from_dict）。
    同名语义：**同名工票行按姓名合并**进 by_person（个人口径：同人=同一钱包），
    与班组口径不同（班组同名成员不合并，见 team_piecepay）。

    乘法/取整包 localcontext(prec=50, ROUND_HALF_EVEN)：默认 28 位精度会把
    多有效位输入的乘积先截断再取整，半分边界可能翻面（对齐班组做法）。
    """
    rows = _as_records(records)
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_HALF_EVEN
        lines = []
        persons: dict = {}  # name -> PersonPay（保序）
        for r in rows:
            qualified = r.qty - r.defect
            amount = _q2_raw(qualified * r.unit_price)
            lines.append(PieceLine(r.process, r.name, r.qty, r.defect,
                                   qualified, r.unit_price, amount))
            if r.name not in persons:
                persons[r.name] = PersonPay(r.name, qualified, r.defect, amount)
            else:
                p = persons[r.name]
                persons[r.name] = PersonPay(
                    p.name, p.qualified_qty + qualified, p.defect_qty + r.defect,
                    q2(p.amount + amount))
        total = q2(sum((l.amount for l in lines), Decimal(0)))
    total_defect = sum((l.defect for l in lines), Decimal(0))
    return IndividualPayResult(tuple(lines), tuple(persons.values()),
                               total, total_defect)


def settle_by_period(records) -> dict:
    """按月切日把工票归期，逐期算个人计件。返回 {期标签: IndividualPayResult}。

    期标签按时间升序；任何记录缺日期立即 ValueError。
    """
    rows = _as_records(records)
    groups: dict = {}
    for r in rows:
        if r.date is None:
            raise ValueError(f"{r.name} 的工票缺少日期，无法按月切日归期")
        groups.setdefault(period_of(r.date), []).append(r)
    return {p: individual_piecepay(groups[p]) for p in sorted(groups)}


def _as_records(records):
    if isinstance(records, (list, tuple)):
        return [r if isinstance(r, PieceRecord) else PieceRecord.from_dict(r)
                for r in records]
    if isinstance(records, PieceRecord):
        return [records]
    raise ValueError("records 应为 PieceRecord 列表（dict 请先走 PieceRecord.from_dict）")


# ------------------------------------------------------------------
# 班组计件
# ------------------------------------------------------------------

@dataclass(frozen=True)
class TeamMember:
    """班组成员：姓名 + 本期计件数量（占比分摊的权重）。"""

    name: str
    qty: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("班组成员姓名不能为空")
        object.__setattr__(self, "qty", _to_decimal(self.qty, "班组成员数量"))
        if self.qty < 0:
            raise ValueError(f"班组成员数量不能为负：{self.name}")

    @classmethod
    def from_dict(cls, d: dict) -> "TeamMember":
        if not isinstance(d, dict):
            raise ValueError(f"班组成员应为 dict，收到 {type(d).__name__}")
        unknown = set(d) - {"name", "qty"}
        if unknown:
            raise ValueError(f"班组成员有未知字段：{sorted(unknown)}")
        missing = {"name", "qty"} - set(d)
        if missing:
            raise ValueError(f"班组成员缺少字段：{sorted(missing)}")
        return cls(name=d["name"], qty=d["qty"])

    def to_dict(self) -> dict:
        """序列化（纯导出）：数值字段一律字符串。"""
        return {"name": self.name, "qty": _num_str(self.qty)}


@dataclass(frozen=True)
class TeamAllocLine:
    """班组计件单人分摊行。

    amount = base_amount + adjust_amount；remark="尾差调整" 表示该行承担尾差。
    """

    name: str
    qty: Decimal
    base_amount: Decimal
    adjust_amount: Decimal
    amount: Decimal
    remark: Optional[str] = None

    def to_dict(self) -> dict:
        """序列化（纯导出）：数值字段一律字符串。"""
        return {
            "name": self.name, "qty": _num_str(self.qty),
            "base_amount": _num_str(self.base_amount),
            "adjust_amount": _num_str(self.adjust_amount),
            "amount": _num_str(self.amount), "remark": self.remark,
        }


@dataclass(frozen=True)
class TeamAllocResult:
    team_total: Decimal      # 取整到分后的班组总额
    lines: Tuple[TeamAllocLine, ...]
    residual: Decimal        # 尾差 = 总额 − 分摊合计（可正可负）
    allocated_total: Decimal # 恒等于 team_total

    def to_dict(self) -> dict:
        """序列化（纯导出）：数值字段一律字符串。"""
        return {
            "team_total": _num_str(self.team_total),
            "lines": [line.to_dict() for line in self.lines],
            "residual": _num_str(self.residual),
            "allocated_total": _num_str(self.allocated_total),
        }


def team_piecepay(team_total, members) -> TeamAllocResult:
    """班组计件：按数量占比分摊总额，尾差挂「调整」行（演示口径）。

    同名语义：**同名成员不合并**——名单行=分摊行，重名会得到多行
    （与个人口径不同：个人计件按姓名合并，见 individual_piecepay）。

    规则：
    1. 总额先取整到分（银行家舍入）；
    2. 每人 base = 总额 × 个人数量 / 班组总数量，高精度除法后按分取整
       （银行家舍入；除法用 localcontext(prec=50)，消除默认 28 位精度
       在半分边界上的理论风险）；
    3. 尾差 = 总额 − Σbase，优先由**数量最大**者承接（并列取名单第一个）；
       承接后金额为负时该行扣到 0，负余额顺次转嫁给数量次大者，直到转正
       （任何成员分摊额不为负；总额 ≥ 0 时 Σbase ≥ |尾差|，必可转正）；
       remark="尾差调整" 标在所有实际承担调整的行上；
    4. 不变量：Σamount == 总额，恒成立。
    """
    if not isinstance(members, (list, tuple)) or len(members) == 0:
        raise ValueError("班组没有成员，无法分摊")
    team = [m if isinstance(m, TeamMember) else TeamMember.from_dict(m)
            for m in members]
    total = q2(team_total)
    if total < 0:
        raise ValueError(f"班组总额不能为负：{total}")
    team_qty = sum((m.qty for m in team), Decimal(0))
    if team_qty == 0:
        if total != 0:
            raise ValueError("班组数量全为 0，无法按比例分摊")
        return TeamAllocResult(
            total,
            tuple(TeamAllocLine(m.name, m.qty, ZERO, ZERO, ZERO) for m in team),
            ZERO, total)
    with localcontext() as ctx:
        ctx.prec = 50
        bases = [_q2_raw(total * m.qty / team_qty) for m in team]
    residual = q2(total - sum(bases, Decimal(0)))
    # 尾差承接链：按数量降序（并列取名单序）。承接后金额为负则该行清零，
    # 负余额继续下传；全员都转不正（理论上仅负总额会出现，构造层已拒）时
    # 按数量最大行兜底，保证 Σamount == total 不变量。
    order = sorted(range(len(team)), key=lambda i: (-team[i].qty, i))
    adjusts = [ZERO] * len(team)
    remaining = residual
    for i in order:
        if remaining == 0:
            break
        if bases[i] + remaining >= 0:
            adjusts[i] = remaining
            remaining = ZERO
        else:
            adjusts[i] = -bases[i]
            remaining = q2(bases[i] + remaining)
    if remaining != 0:
        adjusts[order[0]] = q2(adjusts[order[0]] + remaining)
    lines = []
    for i, m in enumerate(team):
        if adjusts[i] != 0:
            lines.append(TeamAllocLine(m.name, m.qty, bases[i], adjusts[i],
                                       q2(bases[i] + adjusts[i]), "尾差调整"))
        else:
            lines.append(TeamAllocLine(m.name, m.qty, bases[i], ZERO, bases[i]))
    allocated = q2(sum((l.amount for l in lines), Decimal(0)))
    return TeamAllocResult(total, tuple(lines), residual, allocated)


# ------------------------------------------------------------------
# 报废对账
# ------------------------------------------------------------------

@dataclass(frozen=True)
class ScrapReconciliation:
    """四列对平：应产 = 实报 + 报废 + 下落不明（恒等式由构造保证）。

    status：平 / 缺口（下落不明为正，超容差）/ 超报（为负，超容差）。
    """

    expected_qty: Decimal
    reported_qualified: Decimal
    scrapped_qty: Decimal
    missing_qty: Decimal
    status: str


def scrap_reconcile(expected_qty, records, tolerance="0") -> ScrapReconciliation:
    """报废对账：应产 vs 实报/报废/下落不明。

    实报 = Σ(数量 − 报废)，报废 = Σ报废，下落不明 = 应产 − 实报 − 报废。
    tolerance 为对平容差（默认 0，精确对平）；可传 "0.5" 之类的字符串。
    """
    expected = _to_decimal(expected_qty, "应产数量")
    if expected < 0:
        raise ValueError(f"应产数量不能为负：{expected}")
    tol = _to_decimal(tolerance, "对平容差")
    if tol < 0:
        raise ValueError(f"对平容差不能为负：{tol}")
    rows = _as_records(records)
    qualified = sum((r.qty - r.defect for r in rows), Decimal(0))
    scrapped = sum((r.defect for r in rows), Decimal(0))
    missing = expected - qualified - scrapped
    if missing > tol:
        status = "缺口"
    elif missing < -tol:
        status = "超报"
    else:
        status = "平"
    return ScrapReconciliation(expected, qualified, scrapped, missing, status)


# ------------------------------------------------------------------
# 个税（演示口径）
# ------------------------------------------------------------------

def _tax_bracket(taxable: Decimal) -> Tuple[Decimal, Decimal]:
    for upper, rate, quick in MONTHLY_TAX_TABLE:
        if upper is None or taxable <= upper:
            return rate, quick
    raise AssertionError("税率表缺兜底档（不可达）")


def monthly_income_tax(gross, threshold=TAX_THRESHOLD, deductions=0) -> Decimal:
    """月度个税（**演示口径**）：应税 = max(0, 收入 − 起征 − 扣除)，
    税 = 应税 × 税率 − 速算扣除数，取整到分（银行家舍入），不为负。

    注意：真实居民工资个税应按累计预扣法预扣，本函数是按月速算表的
    演示简化，数值仅供演示/对账走查，不可用于真实申报。

    减法/乘法/取整包 localcontext(prec=50, ROUND_HALF_EVEN)：默认 28 位
    精度会把多有效位输入先截断，半分边界可能翻面（对齐班组/个人做法）。
    """
    income = _to_decimal(gross, "收入")
    if income < 0:
        raise ValueError(f"收入不能为负：{income}")
    deduct = _to_decimal(deductions, "扣除项")
    if deduct < 0:
        raise ValueError(f"扣除项不能为负：{deduct}")
    base = _to_decimal(threshold, "起征点")
    if base < 0:
        raise ValueError(f"起征点不能为负：{base}")
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_HALF_EVEN
        taxable = income - base - deduct
        if taxable <= 0:
            return Decimal("0.00")
        rate, quick = _tax_bracket(taxable)
        tax = taxable * rate - quick
        if tax <= 0:
            return Decimal("0.00")
        return _q2_raw(tax)


# ------------------------------------------------------------------
# 工资单组装
# ------------------------------------------------------------------

@dataclass(frozen=True)
class Payslip:
    gross: Decimal
    tax: Decimal
    net: Decimal

    def to_dict(self) -> dict:
        """序列化（纯导出）：数值字段一律字符串。"""
        return {
            "gross": _num_str(self.gross),
            "tax": _num_str(self.tax),
            "net": _num_str(self.net),
        }


def payslip(gross, deductions=0, threshold=TAX_THRESHOLD) -> Payslip:
    """把计件收入组装成工资单：毛收入 → 个税（演示口径）→ 净额。

    计算路径与 individual/tax 一致包 localcontext(prec=50, ROUND_HALF_EVEN)。
    """
    with localcontext() as ctx:
        ctx.prec = 50
        ctx.rounding = ROUND_HALF_EVEN
        g = q2(gross)
        if g < 0:
            raise ValueError(f"收入不能为负：{g}")
        tax = monthly_income_tax(g, threshold=threshold, deductions=deductions)
        return Payslip(g, tax, q2(g - tax))
