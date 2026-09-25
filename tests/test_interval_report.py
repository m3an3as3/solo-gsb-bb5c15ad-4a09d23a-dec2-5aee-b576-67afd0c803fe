"""数值区间预测报告测试：GET /events/{event_id}/interval-report。

SQLite 文件库 + 模拟时钟，覆盖：
  - 未结算事件（含 closed 但未结算）返回 409 INTERVAL_REPORT_NOT_SETTLED
  - 标准结果不是有限十进制数返回 422 STANDARD_RESULT_NOT_FINITE_DECIMAL
  - 区间解析：合法 / 非法格式 / 下界大于上界；未揭示承诺单独计数
  - 十进制精确覆盖判定：闭区间边界命中、数值相等（1.10 == 1.1）、
    宽度精确（0.2 而非浮点误差）
  - 宽度不受默认 Decimal 28 位精度影响：30 位端点的末位差异保留，
    更宽的命中区间排在更窄者之后
  - 精确宽度位数超过 4096（含指数跨度过大）返回 422
    INTERVAL_WIDTH_TOO_LARGE，不静默舍入也不 500
  - 命中者按宽度升序排名，同宽按承诺提交时间、承诺 ID 排序
  - 未命中者列出但不排名（rank 为 null）
  - 既有结算计数不变；接口只读，重复调用结果一致
  - 未知事件 404；零承诺事件；区间解析纯函数
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_interval_report_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/interval_report.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.clock as clock_module  # noqa: E402
import app.main as main_module  # noqa: E402
from app import intervals  # noqa: E402
from app.hashing import compute_commitment  # noqa: E402

T0 = datetime(2030, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
COMMIT_DL = T0 + timedelta(hours=1)
REVEAL_DL = T0 + timedelta(hours=2)


class MutableClock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t


clock = MutableClock(T0)

client = TestClient(main_module.app)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    clock.t = T0
    monkeypatch.setattr(clock_module, "now_utc", clock)
    with client:  # 触发 lifespan 建表（幂等）
        yield


def create_event() -> str:
    r = client.post("/events", json={
        "title": "数值区间预测",
        "commit_deadline": COMMIT_DL.isoformat(),
        "reveal_deadline": REVEAL_DL.isoformat(),
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def commit(event_id: str, text: str, salt: str) -> str:
    """提交承诺并返回承诺 ID。"""
    h = compute_commitment(text, salt)
    r = client.post(f"/events/{event_id}/commitments", json={"commitment_hash": h})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def reveal(event_id: str, text: str, salt: str) -> None:
    r = client.post(f"/events/{event_id}/reveals",
                    json={"original_text": text, "salt": salt})
    assert r.status_code == 201, r.text


def settle(event_id: str, standard_result: str) -> dict:
    r = client.post(f"/events/{event_id}/settlement",
                    json={"standard_result": standard_result})
    assert r.status_code == 200, r.text
    return r.json()


def report(event_id: str, expected: int = 200) -> dict:
    r = client.get(f"/events/{event_id}/interval-report")
    assert r.status_code == expected, r.text
    return r.json()


def test_unsettled_event_is_409():
    event_id = create_event()
    # commit 阶段未结算
    body = report(event_id, expected=409)
    assert body["detail"]["code"] == "INTERVAL_REPORT_NOT_SETTLED"

    # closed 阶段但仍未结算，同样拒绝
    clock.t = REVEAL_DL
    body = report(event_id, expected=409)
    assert body["detail"]["code"] == "INTERVAL_REPORT_NOT_SETTLED"


def test_report_404_for_missing_event():
    missing = "00000000-0000-0000-0000-000000000000"
    body = report(missing, expected=404)
    assert body["detail"]["code"] == "EVENT_NOT_FOUND"


def test_non_finite_decimal_standard_result_is_422():
    # 空标准结果无法通过结算校验，不会出现在结算记录中，不在此列
    for bad in ["明天下雨", "NaN", "Infinity", "-Infinity", "1..2", "1.5 ", " 1.5",
                "abc", "1_000", "1e999999999999999999999"]:
        clock.t = T0  # 每个用例从创建事件开始，重置时钟
        event_id = create_event()
        clock.t = REVEAL_DL
        settle(event_id, bad)
        body = report(event_id, expected=422)
        assert body["detail"]["code"] == "STANDARD_RESULT_NOT_FINITE_DECIMAL"


def test_report_ranks_hits_by_width_and_lists_misses_and_invalid():
    event_id = create_event()
    commit(event_id, "1..2", "s-hit-wide")          # 命中，宽 1
    commit(event_id, "1.4..1.6", "s-hit-narrow")    # 命中，宽 0.2（精确十进制）
    commit(event_id, "0..10", "s-hit-widest")       # 命中，宽 10
    commit(event_id, "3..4", "s-miss")              # 合法但未覆盖
    commit(event_id, "not-an-interval", "s-bad-1")  # 非法格式
    commit(event_id, "5..4", "s-bad-2")             # 下界大于上界
    commit(event_id, "9..99", "s-unrevealed")       # 只承诺不揭示

    clock.t = COMMIT_DL
    reveal(event_id, "1..2", "s-hit-wide")
    reveal(event_id, "1.4..1.6", "s-hit-narrow")
    reveal(event_id, "0..10", "s-hit-widest")
    reveal(event_id, "3..4", "s-miss")
    reveal(event_id, "not-an-interval", "s-bad-1")
    reveal(event_id, "5..4", "s-bad-2")

    clock.t = REVEAL_DL
    settled = settle(event_id, "1.5")

    body = report(event_id)
    assert body["event_id"] == event_id
    assert body["standard_result"] == "1.5"
    assert body["settled_at"] == settled["settled_at"]

    # 既有结算计数原样回显（逐字符精确匹配：无揭示等于 "1.5"）
    assert body["correct_count"] == 0
    assert body["incorrect_count"] == 6
    assert body["unrevealed_count"] == 1

    # 报告计数
    assert body["commitment_count"] == 7
    assert body["revealed_count"] == 6
    assert body["unrevealed_commitment_count"] == 1
    assert body["valid_interval_count"] == 4
    assert body["hit_count"] == 3
    assert body["miss_count"] == 1
    assert body["invalid_count"] == 2

    # 命中者按宽度升序排名：0.2 < 1 < 10；宽度为精确十进制（非浮点误差）
    hits = body["hits"]
    assert [h["rank"] for h in hits] == [1, 2, 3]
    assert [(h["lower_bound"], h["upper_bound"]) for h in hits] == [
        ("1.4", "1.6"), ("1", "2"), ("0", "10"),
    ]
    assert [h["width"] for h in hits] == ["0.2", "1", "10"]
    assert all(h["hit"] is True for h in hits)

    # 未命中者列出但不排名
    misses = body["misses"]
    assert len(misses) == 1
    assert misses[0]["lower_bound"] == "3" and misses[0]["upper_bound"] == "4"
    assert misses[0]["hit"] is False
    assert misses[0]["rank"] is None

    # 非法格式单独列出并注明原因
    invalid = {(i["original_text"], i["reason"]) for i in body["invalid"]}
    assert invalid == {
        ("not-an-interval", "INVALID_FORMAT"),
        ("5..4", "LOWER_BOUND_EXCEEDS_UPPER"),
    }
    assert all(i["commitment_id"] for i in body["invalid"])


def test_same_width_tie_breaks_by_commit_time_then_commitment_id():
    event_id = create_event()
    # 四个区间均覆盖标准结果 1.5 且宽度均为 2
    # 不同时刻提交：同宽时先提交者名次在前
    id_early = commit(event_id, "1..3", "s-early")
    clock.t = T0 + timedelta(seconds=5)
    id_late = commit(event_id, "0..2", "s-late")

    # 同一时刻提交两个承诺：committed_at 相同，按承诺 ID（UUID 整数）升序
    clock.t = T0 + timedelta(seconds=10)
    id_same_a = commit(event_id, "0.5..2.5", "s-same-a")
    id_same_b = commit(event_id, "1.4..3.4", "s-same-b")

    clock.t = COMMIT_DL
    reveal(event_id, "1..3", "s-early")
    reveal(event_id, "0..2", "s-late")
    reveal(event_id, "0.5..2.5", "s-same-a")
    reveal(event_id, "1.4..3.4", "s-same-b")

    clock.t = REVEAL_DL
    settle(event_id, "1.5")

    body = report(event_id)
    hits = body["hits"]
    # 四个区间宽度均为 2：先按提交时间，再按承诺 ID
    same_time_sorted = sorted([id_same_a, id_same_b], key=lambda i: UUID(i).int)
    assert [h["commitment_id"] for h in hits] == [id_early, id_late] + same_time_sorted
    assert [h["rank"] for h in hits] == [1, 2, 3, 4]


def test_coverage_is_closed_interval_and_decimal_exact():
    event_id = create_event()
    commit(event_id, "1..2", "s-upper-edge")    # 上界恰为标准结果
    commit(event_id, "2..3", "s-lower-edge")    # 下界恰为标准结果
    commit(event_id, "2.0..2.00", "s-equal")    # 数值相等（2.0 == 2 == 2.00）
    commit(event_id, "0.1..0.2", "s-miss")      # 不覆盖

    clock.t = COMMIT_DL
    reveal(event_id, "1..2", "s-upper-edge")
    reveal(event_id, "2..3", "s-lower-edge")
    reveal(event_id, "2.0..2.00", "s-equal")
    reveal(event_id, "0.1..0.2", "s-miss")

    clock.t = REVEAL_DL
    settle(event_id, "2")

    body = report(event_id)
    assert body["hit_count"] == 3
    assert body["miss_count"] == 1
    widths = sorted(h["width"] for h in body["hits"])
    # 2.00 - 2.0 = 0.00：精确十进制，保留操作数精度，不经过浮点
    assert widths == ["0.00", "1", "1"]
    zero_width = next(h for h in body["hits"] if h["lower_bound"] == "2.0")
    assert zero_width["rank"] == 1  # 零宽区间最窄，排名第一
    assert body["misses"][0]["lower_bound"] == "0.1"


def test_widths_preserve_last_digit_beyond_default_decimal_precision():
    # 默认 Decimal 精度为 28 位：两个 30 位端点若经默认上下文相减，
    # 都会被舍入为 0.1234567890123456789012345679 而丢掉末位差异。
    # 精确宽度必须保留末位，且较宽者排在较窄者之后。
    wide_text = "0..0.123456789012345678901234567891"
    narrow_text = "0..0.123456789012345678901234567890"
    event_id = create_event()
    # 先提交较宽者，确保排名只取决于真实宽度而非提交顺序
    commit(event_id, wide_text, "s-wide")
    commit(event_id, narrow_text, "s-narrow")

    clock.t = COMMIT_DL
    reveal(event_id, wide_text, "s-wide")
    reveal(event_id, narrow_text, "s-narrow")

    clock.t = REVEAL_DL
    settle(event_id, "0.123456789012345678901234567890")

    body = report(event_id)
    hits = body["hits"]
    assert [h["rank"] for h in hits] == [1, 2]
    # 末位完整保留：两者宽度不再相等，窄者（末位 0）名次在前
    assert hits[0]["width"] == "0.123456789012345678901234567890"
    assert hits[1]["width"] == "0.123456789012345678901234567891"
    assert hits[0]["upper_bound"] == "0.123456789012345678901234567890"
    assert hits[1]["upper_bound"] == "0.123456789012345678901234567891"


def test_width_exact_across_default_precision_boundary_for_misses_too():
    # 未命中时宽度同样精确：宽/窄两个区间均不覆盖标准结果，
    # misses 按提交时间排序，但 width 字段仍须保留末位差异
    wide_text = "1..1.123456789012345678901234567891"
    narrow_text = "1..1.123456789012345678901234567890"
    event_id = create_event()
    commit(event_id, wide_text, "s-wide")
    commit(event_id, narrow_text, "s-narrow")

    clock.t = COMMIT_DL
    reveal(event_id, wide_text, "s-wide")
    reveal(event_id, narrow_text, "s-narrow")

    clock.t = REVEAL_DL
    settle(event_id, "0")

    body = report(event_id)
    assert body["hit_count"] == 0
    assert {m["width"] for m in body["misses"]} == {
        "0.123456789012345678901234567891",
        "0.123456789012345678901234567890",
    }


def test_width_at_4096_digit_limit_is_accepted():
    # 0..1E+4095 的精确宽度 = 1 后面 4095 个 0，恰好 4096 位：允许
    text_ok = "0..1E+4095"
    event_id = create_event()
    commit(event_id, text_ok, "s-ok")

    clock.t = COMMIT_DL
    reveal(event_id, text_ok, "s-ok")

    clock.t = REVEAL_DL
    settle(event_id, "0")

    body = report(event_id)
    assert body["hit_count"] == 1
    assert body["hits"][0]["width"] == "1" + "0" * 4095


def test_width_over_4096_digits_is_422():
    # 0..1E+4096 的精确宽度为 4097 位：明确 422，不静默舍入
    text_too_wide = "0..1E+4096"
    event_id = create_event()
    commit(event_id, text_too_wide, "s-too-wide")

    clock.t = COMMIT_DL
    reveal(event_id, text_too_wide, "s-too-wide")

    clock.t = REVEAL_DL
    settle(event_id, "0")

    body = report(event_id, expected=422)
    assert body["detail"]["code"] == "INTERVAL_WIDTH_TOO_LARGE"

    # 只读：既有结算结果不受影响，重复请求仍为同一 422
    r = client.get(f"/events/{event_id}/settlement")
    assert r.status_code == 200
    assert r.json()["standard_result"] == "0"
    body = report(event_id, expected=422)
    assert body["detail"]["code"] == "INTERVAL_WIDTH_TOO_LARGE"


def test_extreme_exponent_spread_is_fast_422_not_500():
    # 指数跨度 ~2e18：精确宽度需要不可物化的超大整数。
    # 区间语法合法（端点均可解析），必须快速返回 422 而不是挂起或 500。
    text_huge = "1E-999999999999999999..1E+999999999999999999"
    event_id = create_event()
    commit(event_id, text_huge, "s-huge")

    clock.t = COMMIT_DL
    reveal(event_id, text_huge, "s-huge")

    clock.t = REVEAL_DL
    settle(event_id, "0")

    body = report(event_id, expected=422)
    assert body["detail"]["code"] == "INTERVAL_WIDTH_TOO_LARGE"


def test_long_input_cancellation_still_exact():
    # 两个 5000 位端点（系数本身很长）之差仅为 1：精确宽度只有 1 位，
    # 不应因端点位数超过 4096 就拒绝（限制针对的是结果宽度，不是输入长度）
    upper = "1" + "0" * 4998 + "1"  # 5000 位
    lower = "1" + "0" * 4998 + "0"  # 5000 位，恰比 upper 小 1
    text = f"{lower}..{upper}"
    event_id = create_event()
    commit(event_id, text, "s-long")

    clock.t = COMMIT_DL
    reveal(event_id, text, "s-long")

    clock.t = REVEAL_DL
    settle(event_id, lower)

    body = report(event_id)
    assert body["hit_count"] == 1
    assert body["hits"][0]["width"] == "1"
    assert body["hits"][0]["rank"] == 1


def test_scientific_notation_bounds_are_finite_decimals():
    event_id = create_event()
    commit(event_id, "1e2..2e2", "s-sci")   # [100, 200]
    commit(event_id, "-1.5e1..0", "s-neg")  # [-15, 0]，不覆盖

    clock.t = COMMIT_DL
    reveal(event_id, "1e2..2e2", "s-sci")
    reveal(event_id, "-1.5e1..0", "s-neg")

    clock.t = REVEAL_DL
    settle(event_id, "150")

    body = report(event_id)
    assert body["hit_count"] == 1
    assert body["hits"][0]["width"] == "1E+2"
    assert body["miss_count"] == 1


def test_invalid_format_variants():
    event_id = create_event()
    texts = [
        "1..2..3",      # 分隔符多于一个
        "1 .. 2",       # 含空白
        "1..",          # 缺上界
        "..2",          # 缺下界
        "NaN..2",       # NaN 非有限十进制数
        "1..Infinity",  # Infinity 非有限十进制数
        "",             # 空原文
        "1e999999999999999999999..2",  # 指数超出 Decimal 可表示范围
    ]
    for i, text in enumerate(texts):
        commit(event_id, text, f"s-{i}")

    clock.t = COMMIT_DL
    for i, text in enumerate(texts):
        reveal(event_id, text, f"s-{i}")

    clock.t = REVEAL_DL
    settle(event_id, "1.5")

    body = report(event_id)
    assert body["valid_interval_count"] == 0
    assert body["invalid_count"] == len(texts)
    assert {i["reason"] for i in body["invalid"]} == {"INVALID_FORMAT"}
    assert {i["original_text"] for i in body["invalid"]} == set(texts)


def test_report_is_read_only_and_settlement_counts_unchanged():
    event_id = create_event()
    commit(event_id, "1..2", "s-a")
    clock.t = COMMIT_DL
    reveal(event_id, "1..2", "s-a")
    clock.t = REVEAL_DL
    settled = settle(event_id, "1.5")

    first = report(event_id)
    second = report(event_id)
    assert first == second  # 时钟冻结下重复调用结果逐字节一致

    # 既有结算结果不变
    r = client.get(f"/events/{event_id}/settlement")
    assert r.status_code == 200
    assert r.json() == settled

    # 承诺 / 揭示计数不变
    r = client.get(f"/events/{event_id}")
    assert r.json()["commitment_count"] == 1
    assert r.json()["reveal_count"] == 1


def test_report_with_zero_commitments():
    event_id = create_event()
    clock.t = REVEAL_DL
    settle(event_id, "1.5")

    body = report(event_id)
    assert body["commitment_count"] == 0
    assert body["revealed_count"] == 0
    assert body["unrevealed_commitment_count"] == 0
    assert body["valid_interval_count"] == 0
    assert body["hit_count"] == 0
    assert body["miss_count"] == 0
    assert body["invalid_count"] == 0
    assert body["hits"] == [] and body["misses"] == [] and body["invalid"] == []


# --------------------------------------------------------------------------- #
# 纯函数：有限十进制数与区间解析
# --------------------------------------------------------------------------- #

def test_parse_finite_decimal_strict():
    assert intervals.parse_finite_decimal("1.5") == Decimal("1.5")
    assert intervals.parse_finite_decimal("+1.") == Decimal("1")
    assert intervals.parse_finite_decimal("-.5") == Decimal("-0.5")
    assert intervals.parse_finite_decimal("1e3") == Decimal("1E+3")
    assert intervals.parse_finite_decimal("01.50") == Decimal("1.50")
    for bad in ["", ".", "+", "1.2.3", "1_000", " 1", "1 ", "Infinity", "-Infinity",
                "NaN", "0x10", "1e", "e3", "1e999999999999999999999"]:
        assert intervals.parse_finite_decimal(bad) is None, bad


def test_parse_interval_bounds_and_reasons():
    parsed, reason = intervals.parse_interval("1.4..1.6")
    assert reason is None
    assert parsed.lower == Decimal("1.4") and parsed.upper == Decimal("1.6")
    assert parsed.width == Decimal("0.2")  # 精确十进制，无浮点误差
    assert parsed.lower_text == "1.4" and parsed.upper_text == "1.6"

    parsed, reason = intervals.parse_interval("5..4")
    assert parsed is None and reason == intervals.LOWER_BOUND_EXCEEDS_UPPER

    for bad in ["", "1", "1..2..3", "a..2", "1...2.5.6", "..", "1 .. 2"]:
        parsed, reason = intervals.parse_interval(bad)
        assert parsed is None and reason == intervals.INVALID_FORMAT, bad


# --------------------------------------------------------------------------- #
# 纯函数：精确宽度（整数系数运算，不受默认 Decimal 28 位精度影响）
# --------------------------------------------------------------------------- #

def test_exact_width_keeps_last_digit_beyond_default_precision():
    w91 = intervals.exact_width(
        Decimal("0"), Decimal("0.123456789012345678901234567891")
    )
    w90 = intervals.exact_width(
        Decimal("0"), Decimal("0.123456789012345678901234567890")
    )
    # 末位保留，两者不再像默认上下文相减那样塌缩为同一个 28 位值
    assert w91 == Decimal("0.123456789012345678901234567891")
    assert w90 == Decimal("0.123456789012345678901234567890")
    assert w91 > w90


def test_exact_width_matches_unbounded_decimal_subtraction():
    # 与精度 10000 的 Decimal 上下文减法逐位（含首选指数）一致
    from decimal import Context, localcontext

    pairs = [
        ("0", "1"), ("1.4", "1.6"), ("2.0", "2.00"), ("0.0", "0.00"),
        ("1e2", "2e2"), ("-1.5e1", "0"), ("-5", "5"), ("-1.5", "-1.4"),
        ("0", "0." + "1234567890" * 400), ("1E+4095", "1E+4095"),
        ("-1", "0E+5000"),  # 零系数携带巨大指数，宽度为 1，不应物化 10**5000
    ]
    for lower_text, upper_text in pairs:
        lower, upper = Decimal(lower_text), Decimal(upper_text)
        exact = intervals.exact_width(lower, upper)
        with localcontext(Context(prec=10000)):
            reference = upper - lower
        assert exact.as_tuple() == reference.as_tuple(), (lower_text, upper_text)


def test_exact_width_limit_boundary():
    # 恰好 4096 位：允许；4097 位：拒绝
    assert intervals.exact_width(Decimal("0"), Decimal("1E+4095")) == Decimal(
        "1" + "0" * 4095
    )
    for lower, upper in [
        (Decimal("0"), Decimal("1E+4096")),                       # 4097 位
        (Decimal("-1E+4096"), Decimal("1")),                     # 指数跨度
        (Decimal("1E-999999999999999999"), Decimal("1E+999999999999999999")),
    ]:
        with pytest.raises(intervals.WidthTooLargeError):
            intervals.exact_width(lower, upper)


def test_exact_width_allows_cancelled_long_inputs():
    # 端点系数各 5000 位但只差 1：结果宽度 1 位，不触发 4096 限制
    upper = Decimal("1" + "0" * 4998 + "1")
    lower = Decimal("1" + "0" * 4998 + "0")
    assert intervals.exact_width(lower, upper) == Decimal("1")


def test_parse_interval_raises_width_too_large():
    # 语法合法、下界 <= 上界，仅因精确宽度超限而抛出（区别于无效区间的元组返回）
    with pytest.raises(intervals.WidthTooLargeError):
        intervals.parse_interval("0..1E+4096")
    # 无效语法仍按原契约返回 (None, INVALID_FORMAT)，不抛宽度异常
    parsed, reason = intervals.parse_interval("1e999999999999999999999..2")
    assert parsed is None and reason == intervals.INVALID_FORMAT
