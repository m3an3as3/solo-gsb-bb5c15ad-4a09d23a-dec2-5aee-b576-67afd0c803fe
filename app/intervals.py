"""数值区间预测报告：把揭示原文解析为「下界..上界」区间并做十进制精确覆盖判定。

纯函数模块：不依赖数据库与 HTTP，可独立单测。

区间文本语法（严格匹配，不允许空白、下划线、Infinity/NaN 等宽松形式）：

    interval       := finite_decimal ".." finite_decimal
    finite_decimal := [+-]? ( 数字+ ["." 数字*] | "." 数字+ ) [ (e|E) [+-]? 数字+ ]

解析出的两个数均为**有限十进制数**（decimal.Decimal 精确表示），且要求
下界 <= 上界；不满足任一条件即判为无效。覆盖判定（闭区间）与区间宽度
均用整数系数运算精确计算，全程不经过浮点，也不受默认 Decimal 上下文
28 位精度影响——宽度必须是精确值，任何舍入都可能改变命中排名。
"""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

# 有限十进制数的严格语法：显式排除 Decimal 构造器宽容接受的
# 空白、下划线、Infinity 与 NaN
FINITE_DECIMAL_PATTERN = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")

# 区间分隔符：恰好一个 ".."，把原文分成下界与上界两段
SEPARATOR = ".."

# 精确宽度允许的最大十进制有效数字位数：超过即无法在不舍入的前提下
# 给出宽度，报告整体返回 422 而不是静默截断或在物化超大整数时 500
MAX_WIDTH_DIGITS = 4096

# 无效原因
INVALID_FORMAT = "INVALID_FORMAT"  # 不是「有限十进制数..有限十进制数」
LOWER_BOUND_EXCEEDS_UPPER = "LOWER_BOUND_EXCEEDS_UPPER"  # 下界大于上界


class WidthTooLargeError(Exception):
    """精确区间宽度的有效数字位数超过 MAX_WIDTH_DIGITS。

    区间语法与端点数值均合法（下界 <= 上界），但精确宽度（上界 - 下界）
    的有效数字位数超过 MAX_WIDTH_DIGITS，无法在不舍入的前提下表示；
    指数跨度过大时甚至无法安全物化（如 1e-999999999999999999..
    1e999999999999999999），此时按位数上界提前抛出，绝不计算超大整数。
    """


def parse_finite_decimal(text: str) -> Decimal | None:
    """把字符串解析为有限十进制数；语法非法或非有限（Infinity/NaN）返回 None。"""
    if not FINITE_DECIMAL_PATTERN.fullmatch(text):
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        # 语法合法但指数超出 Decimal 可表示范围（如 1e999999999999999999999）
        return None
    if not value.is_finite():
        return None
    return value


def _signed_coefficient_exponent(value: Decimal) -> tuple[int, int]:
    """取有限 Decimal 的 (带符号整数系数, 指数)（负零归一为 0）。"""
    sign, digits, exponent = value.as_tuple()
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    return (-coefficient if sign else coefficient), exponent


def _digit_count(value: int) -> int:
    """非零整数的十进制位数（0 按 0 位处理，调用方需另行处理零项）。"""
    return len(str(abs(value)))


def exact_width(lower: Decimal, upper: Decimal) -> Decimal:
    """精确计算 width = upper - lower，不做任何舍入。

    用整数系数与公共指数直接对齐运算，等价于在精度足够大的 Decimal
    上下文中做减法：结果的指数取两端点指数的较小值（与 Decimal 精确
    减法的「首选指数」一致，故 ``2.0..2.00`` 的宽度为 ``0.00`` 而非
    ``0``），系数为对齐后的整数之差。

    结果有效数字位数超过 MAX_WIDTH_DIGITS（含指数跨度过大、无法安全
    物化的情形）时抛出 WidthTooLargeError。调用方已保证 lower、upper
    均为有限 Decimal 且 lower <= upper。
    """
    coefficient_upper, exponent_upper = _signed_coefficient_exponent(upper)
    coefficient_lower, exponent_lower = _signed_coefficient_exponent(lower)
    common_exponent = min(exponent_upper, exponent_lower)
    shift_upper = exponent_upper - common_exponent
    shift_lower = exponent_lower - common_exponent

    # 对齐到公共指数后，某一项需要乘 10**shift；shift 远大于 MAX_WIDTH_DIGITS
    # 时，若对端系数（位数受输入长度约束，通常很短）兜不住，结果必然超限，
    # 直接拒绝而不物化 10**shift——指数记法可把 shift 拉到 ~1e18，
    # 物化即拒绝服务。零系数项对齐后仍是 0，直接跳过，不参与移位。
    if coefficient_upper != 0 and shift_upper > MAX_WIDTH_DIGITS + 1:
        if coefficient_lower <= 0 or shift_upper > _digit_count(coefficient_lower):
            raise WidthTooLargeError
    if coefficient_lower != 0 and shift_lower > MAX_WIDTH_DIGITS + 1:
        if coefficient_upper <= 0 or shift_lower > _digit_count(coefficient_upper):
            raise WidthTooLargeError

    aligned_upper = coefficient_upper * 10**shift_upper if coefficient_upper else 0
    aligned_lower = coefficient_lower * 10**shift_lower if coefficient_lower else 0
    coefficient = aligned_upper - aligned_lower

    if coefficient != 0 and _digit_count(coefficient) > MAX_WIDTH_DIGITS:
        raise WidthTooLargeError

    try:
        # 结果指数取 min(端点指数)，与 Decimal 精确减法的首选指数一致；
        # 用 (符号, 数字元组, 指数) 三元组直接构造，绕过上下文精度
        # （scaleb 与算术运算都会按当前上下文精度舍入）
        if coefficient == 0:
            digits = (0,)
        else:
            digits = tuple(int(digit) for digit in str(abs(coefficient)))
        return Decimal((1 if coefficient < 0 else 0, digits, common_exponent))
    except InvalidOperation:
        # 极端情况下精确结果超出 Decimal 可表示的指数范围（如
        # -9.999…E+MAX_EMAX..9.999…E+MAX_EMAX 进位），同样拒绝而非 500
        raise WidthTooLargeError from None


@dataclass(frozen=True)
class ParsedInterval:
    """一条成功解析的区间：边界原文逐字保留，数值与宽度为精确十进制。"""

    lower_text: str
    upper_text: str
    lower: Decimal
    upper: Decimal
    width: Decimal  # upper - lower（>= 0，精确十进制）


def parse_interval(text: str) -> tuple[ParsedInterval | None, str | None]:
    """把揭示原文解析为「下界..上界」区间。

    返回 (区间, None) 或 (None, 原因)；原因取 INVALID_FORMAT（不是两个
    有限十进制数）或 LOWER_BOUND_EXCEEDS_UPPER（下界大于上界）。

    区间语法合法、但精确宽度位数超过 MAX_WIDTH_DIGITS 时抛出
    WidthTooLargeError（这不是无效区间，而是整份报告无法无舍入地生成）。
    """
    parts = text.split(SEPARATOR)
    if len(parts) != 2:
        return None, INVALID_FORMAT
    lower_text, upper_text = parts
    lower = parse_finite_decimal(lower_text)
    upper = parse_finite_decimal(upper_text)
    if lower is None or upper is None:
        return None, INVALID_FORMAT
    if lower > upper:
        return None, LOWER_BOUND_EXCEEDS_UPPER
    return ParsedInterval(
        lower_text, upper_text, lower, upper, exact_width(lower, upper)
    ), None


@dataclass(frozen=True)
class IntervalEntry:
    """一条合法区间的报告条目（命中或未命中）。"""

    commitment_id: UUID
    committed_at: datetime
    lower_text: str
    upper_text: str
    width: Decimal
    hit: bool


@dataclass(frozen=True)
class InvalidIntervalEntry:
    """一条无法解析为合法区间的揭示。"""

    commitment_id: UUID
    committed_at: datetime
    original_text: str
    reason: str  # INVALID_FORMAT | LOWER_BOUND_EXCEEDS_UPPER


@dataclass(frozen=True)
class IntervalReport:
    """区间报告的确定性结果：hits 已排名，misses / invalid 仅稳定排序。"""

    hits: list[IntervalEntry]  # 按 (宽度, 承诺提交时间, 承诺 ID) 升序，名次 = 下标 + 1
    misses: list[IntervalEntry]  # 按 (承诺提交时间, 承诺 ID) 升序，不排名
    invalid: list[InvalidIntervalEntry]  # 按 (承诺提交时间, 承诺 ID) 升序
    unrevealed_count: int  # 承诺存在但从未揭示的数量


def build_interval_report(
    standard: Decimal,
    revealed: list[tuple[UUID, datetime, str]],
    unrevealed_count: int,
) -> IntervalReport:
    """对全部已揭示原文做区间解析与覆盖判定，返回确定性排序的报告。

    - standard：结算标准结果（有限十进制数，由调用方解析）；
    - revealed：(承诺 ID, 承诺提交时间, 揭示原文) 三元组，输入顺序任意；
    - 覆盖判定为闭区间 lower <= standard <= upper，十进制精确比较；
    - 命中者按 (区间宽度, 承诺提交时间, 承诺 ID) 升序排名（名次 = 下标 + 1）；
    - 未命中者与无效条目分别按 (承诺提交时间, 承诺 ID) 稳定排序，不排名。

    某条合法区间的精确宽度位数超过 MAX_WIDTH_DIGITS 时抛出
    WidthTooLargeError（由调用方转为 422）。
    """
    hits: list[IntervalEntry] = []
    misses: list[IntervalEntry] = []
    invalid: list[InvalidIntervalEntry] = []
    for commitment_id, committed_at, original_text in revealed:
        parsed, reason = parse_interval(original_text)
        if parsed is None:
            invalid.append(
                InvalidIntervalEntry(commitment_id, committed_at, original_text, reason)
            )
            continue
        entry = IntervalEntry(
            commitment_id=commitment_id,
            committed_at=committed_at,
            lower_text=parsed.lower_text,
            upper_text=parsed.upper_text,
            width=parsed.width,
            hit=parsed.lower <= standard <= parsed.upper,
        )
        (hits if entry.hit else misses).append(entry)

    # 承诺 ID 取 UUID 128 位整数排序，与承诺集合证明的叶序一致；
    # 宽度为精确 Decimal，比较同样精确，不受任何上下文精度影响
    hits.sort(key=lambda e: (e.width, e.committed_at, e.commitment_id.int))
    misses.sort(key=lambda e: (e.committed_at, e.commitment_id.int))
    invalid.sort(key=lambda e: (e.committed_at, e.commitment_id.int))
    return IntervalReport(
        hits=hits, misses=misses, invalid=invalid, unrevealed_count=unrevealed_count
    )
