"""跨事件概率校准报告测试：POST /events/probability-calibration-report。

SQLite 文件库 + 模拟时钟，覆盖：
  - 事件 ID 数量 1～100（空 / 101 个为 422）、非 UUID 为 422
  - 重复 ID 保序去重；任一事件不存在 404、未结算 409、
    标准结果非精确 "0"/"1" 为 422（按去重后顺序报第一个违规事件）
  - 概率原文严格解析：无空白 / 无符号 / 无指数且在 [0,1]；
    无效原文逐条列出（INVALID_FORMAT / OUT_OF_RANGE），未揭示承诺单独计数
  - Decimal 精确平方误差与总体 Brier 均分（无有效预测时为 null）
  - 十分箱边界归属确定（k/10 归右侧箱，1 归最后一箱）；空样本均值为 null
  - 四舍六入五成双至 6 位小数；固定字段顺序与排序
  - 纯只读，重复调用一致；纯函数解析与分箱
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from fractions import Fraction

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_calibration_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/calibration.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.clock as clock_module  # noqa: E402
import app.main as main_module  # noqa: E402
from app import calibration  # noqa: E402
from app.hashing import compute_commitment  # noqa: E402

T0 = datetime(2030, 8, 1, 0, 0, 0, tzinfo=timezone.utc)
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


def create_event(title: str = "概率事件") -> str:
    r = client.post("/events", json={
        "title": title,
        "commit_deadline": COMMIT_DL.isoformat(),
        "reveal_deadline": REVEAL_DL.isoformat(),
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def commit(event_id: str, text: str, salt: str) -> str:
    h = compute_commitment(text, salt)
    r = client.post(f"/events/{event_id}/commitments", json={"commitment_hash": h})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def reveal(event_id: str, text: str, salt: str) -> None:
    r = client.post(f"/events/{event_id}/reveals",
                    json={"original_text": text, "salt": salt})
    assert r.status_code == 201, r.text


def settle(event_id: str, standard_result: str) -> None:
    clock.t = REVEAL_DL
    r = client.post(f"/events/{event_id}/settlement",
                    json={"standard_result": standard_result})
    assert r.status_code == 200, r.text
    clock.t = T0


def report(event_ids, expected: int = 200) -> dict:
    r = client.post("/events/probability-calibration-report",
                    json={"event_ids": event_ids})
    assert r.status_code == expected, r.text
    return r.json()


def settled_event(pairs, standard_result, unrevealed_texts=()):
    """建一个事件：pairs 为 (原文, 盐) 已揭示列表；unrevealed_texts 只承诺不揭示。"""
    event_id = create_event()
    for text, salt in pairs:
        commit(event_id, text, salt)
    for text in unrevealed_texts:
        commit(event_id, text, f"unrevealed-{text}-{event_id}")
    clock.t = COMMIT_DL
    for text, salt in pairs:
        reveal(event_id, text, salt)
    settle(event_id, standard_result)
    return event_id


# --------------------------------------------------------------------------- #
# 请求校验
# --------------------------------------------------------------------------- #

def test_empty_and_too_many_event_ids_are_422():
    r = client.post("/events/probability-calibration-report", json={"event_ids": []})
    assert r.status_code == 422

    too_many = ["00000000-0000-0000-0000-000000000000"] * 101
    r = client.post("/events/probability-calibration-report",
                    json={"event_ids": too_many})
    assert r.status_code == 422


def test_non_uuid_is_422():
    r = client.post("/events/probability-calibration-report",
                    json={"event_ids": ["not-a-uuid"]})
    assert r.status_code == 422


def test_missing_event_is_404():
    existing = settled_event([("0.5", "s")], "1")
    body = report([existing, "00000000-0000-0000-0000-000000000000"], expected=404)
    assert body["detail"]["code"] == "EVENT_NOT_FOUND"


def test_unsettled_event_is_409():
    event_id = create_event()
    body = report([event_id], expected=409)
    assert body["detail"]["code"] == "CALIBRATION_EVENT_NOT_SETTLED"

    # closed 但未结算同样拒绝
    clock.t = REVEAL_DL
    body = report([event_id], expected=409)
    assert body["detail"]["code"] == "CALIBRATION_EVENT_NOT_SETTLED"


def test_non_binary_standard_result_is_422():
    # 空串无法通过结算端点的非空校验，直接写库构造该结算记录；
    # 其余非 "0"/"1" 的标准结果均走正常结算流程
    for bad in ["明天下雨", "00", "01", "1.0", "0.0", " 1", "1 ", "2", "-0", "true"]:
        event_id = settled_event([("0.5", f"s-{bad}")], bad)
        body = report([event_id], expected=422)
        assert body["detail"]["code"] == "CALIBRATION_STANDARD_RESULT_NOT_BINARY"

    event_id = _settle_raw("")
    body = report([event_id], expected=422)
    assert body["detail"]["code"] == "CALIBRATION_STANDARD_RESULT_NOT_BINARY"


def _settle_raw(standard_result: str) -> str:
    """空串等无法走正常流程的值：直接建事件、过截止时间后也无法结算（结算要求非空）。

    因此空标准结果不会存在于结算记录中，这里通过直接写库构造。
    """
    from uuid import UUID

    from app.database import SessionLocal
    from app.models import Settlement

    event_id = create_event()
    clock.t = REVEAL_DL
    db = SessionLocal()
    try:
        db.add(Settlement(
            event_id=UUID(event_id),
            standard_result=standard_result,
            correct_count=0, incorrect_count=0, unrevealed_count=0,
        ))
        db.commit()
    finally:
        db.close()
    clock.t = T0
    return event_id


def test_validation_order_missing_before_unsettled_before_non_binary():
    missing = "00000000-0000-0000-0000-000000000000"
    unsettled = create_event()
    non_binary = settled_event([("0.5", "s")], "yes")

    # 第一个不存在 -> 404（即使后面还有其他问题）
    body = report([missing, unsettled, non_binary], expected=404)
    assert body["detail"]["code"] == "EVENT_NOT_FOUND"
    # 去掉不存在后，第一个未结算 -> 409
    body = report([unsettled, non_binary], expected=409)
    assert body["detail"]["code"] == "CALIBRATION_EVENT_NOT_SETTLED"
    # 只剩标准结果非二元 -> 422
    body = report([non_binary], expected=422)
    assert body["detail"]["code"] == "CALIBRATION_STANDARD_RESULT_NOT_BINARY"


# --------------------------------------------------------------------------- #
# 解析、计分与分箱
# --------------------------------------------------------------------------- #

def test_full_report_brier_and_predictions():
    # 事件 A 标准结果 1
    a = settled_event([
        ("1", "a-perfect"),     # SE = 0
        ("0.75", "a-good"),     # SE = 0.0625
        ("0.25", "a-bad"),      # SE = 0.5625
        ("nope", "a-invalid"),  # 非法格式
        ("1.5", "a-oor"),       # 超范围
    ], "1", unrevealed_texts=["secret-a"])
    # 事件 B 标准结果 0
    b = settled_event([
        ("0", "b-perfect"),     # SE = 0
        ("0.5", "b-half"),      # SE = 0.25
    ], "0", unrevealed_texts=["secret-b-1", "secret-b-2"])

    # 重复 ID 去重，且保序
    body = report([a, b, a, b])
    assert body["event_count"] == 2
    assert body["event_ids"] == [a, b]

    assert body["valid_prediction_count"] == 5
    assert body["invalid_count"] == 2
    assert body["unrevealed_commitment_count"] == 3

    # 平方误差（四舍六入五成双至 6 位）
    se = {(p["original_text"], p["outcome"]): p["squared_error"]
          for p in body["predictions"]}
    assert se == {
        ("1", 1): "0.000000",
        ("0.75", 1): "0.062500",
        ("0.25", 1): "0.562500",
        ("0", 0): "0.000000",
        ("0.5", 0): "0.250000",
    }

    # Brier 均分 = (0 + 0.0625 + 0.5625 + 0 + 0.25) / 5 = 0.875 / 5 = 0.175
    assert body["brier_mean"] == "0.175000"

    invalid = {(i["original_text"], i["reason"]) for i in body["invalid"]}
    assert invalid == {
        ("nope", "INVALID_FORMAT"),
        ("1.5", "OUT_OF_RANGE"),
    }
    for item in body["invalid"]:
        assert item["event_id"] == a
        assert item["commitment_id"]

    # 排序：按 (事件 UUID, 提交时间, 承诺 ID) 全局稳定排序，与请求顺序无关。
    # 事件 ID 不同时预测必然按事件成组；组的先后由事件 UUID 大小决定，
    # 组的大小分别为 3（A）与 2（B）
    event_seq = [p["event_id"] for p in body["predictions"]]
    a_predictions = [p for p in body["predictions"] if p["event_id"] == a]
    b_predictions = [p for p in body["predictions"] if p["event_id"] == b]
    assert len(a_predictions) == 3 and len(b_predictions) == 2
    from uuid import UUID as _UUID
    ordered_ids = sorted([a, b], key=lambda x: _UUID(x).int)
    group_size = {a: 3, b: 2}
    assert event_seq == [ordered_ids[0]] * group_size[ordered_ids[0]] + \
        [ordered_ids[1]] * group_size[ordered_ids[1]]
    # 同一时钟时刻提交，事件内同 committed_at 按承诺 ID（UUID 整数）排序
    a_expected = sorted(
        [p for p in body["predictions"] if p["event_id"] == a],
        key=lambda p: _UUID(p["commitment_id"]).int,
    )
    assert [p["original_text"] for p in a_predictions] == [
        p["original_text"] for p in a_expected
    ]
    b_expected = sorted(
        [p for p in body["predictions"] if p["event_id"] == b],
        key=lambda p: _UUID(p["commitment_id"]).int,
    )
    assert [p["original_text"] for p in b_predictions] == [
        p["original_text"] for p in b_expected
    ]
    assert {p["original_text"] for p in a_predictions} == {"1", "0.75", "0.25"}
    assert {p["original_text"] for p in b_predictions} == {"0", "0.5"}
    # invalid 条目同样按事件 ID 归组（两条非法原文均属事件 A）
    assert [i["event_id"] for i in body["invalid"]] == [a, a]


def test_brier_mean_null_when_no_valid_predictions():
    event_id = settled_event([("garbage", "s1"), ("2", "s2")], "1")
    body = report([event_id])
    assert body["valid_prediction_count"] == 0
    assert body["invalid_count"] == 2
    assert body["brier_mean"] is None
    assert body["predictions"] == []
    for b in body["bins"]:
        assert b["count"] == 0
        assert b["mean"] is None
        assert b["positive_rate"] is None


def test_bins_boundaries_and_aggregates():
    # 概率覆盖各边界：0、0.1、0.5、0.9、1；全部属于标准结果 1 的事件，
    # 另加一个标准结果 0 事件提供反例
    pos = settled_event([
        ("0", "p-0"),
        ("0.1", "p-01"),
        ("0.5", "p-05"),
        ("0.9", "p-09"),
        ("1", "p-1"),
    ], "1")
    neg = settled_event([("0.1", "n-01"), ("0.9", "n-09")], "0")
    body = report([pos, neg])

    bins = body["bins"]
    assert len(bins) == 10
    assert [b["index"] for b in bins] == list(range(10))
    assert bins[0]["range"] == "[0.0,0.1)"
    assert bins[9]["range"] == "[0.9,1.0]"
    assert bins[0]["lower_bound"] == "0.0" and bins[0]["upper_bound"] == "0.1"
    assert bins[9]["lower_bound"] == "0.9" and bins[9]["upper_bound"] == "1.0"

    by_index = {b["index"]: b for b in bins}
    # 0 在第 0 箱
    assert by_index[0]["count"] == 1
    assert by_index[0]["mean"] == "0.000000"
    assert by_index[0]["positive_rate"] == "1.000000"
    # 0.1 归入第 1 箱（边界属于右侧）：样本 0.1(正) 与 0.1(负)
    assert by_index[1]["count"] == 2
    assert by_index[1]["mean"] == "0.100000"
    assert by_index[1]["positive_rate"] == "0.500000"
    # 0.5 在第 5 箱
    assert by_index[5]["count"] == 1
    assert by_index[5]["mean"] == "0.500000"
    assert by_index[5]["positive_rate"] == "1.000000"
    # 0.9 归入第 9 箱：0.9(正)、0.9(负)、1(正)
    assert by_index[9]["count"] == 3
    # 精确均值 (0.9+0.9+1)/3 = 2.8/3 ≈ 0.933333（第 7 位为 3，不进位）
    assert by_index[9]["mean"] == "0.933333"
    # 正例频率 2/3 ≈ 0.666667（第 7 位为 6，进位）
    assert by_index[9]["positive_rate"] == "0.666667"
    # 空箱
    for i in [2, 3, 4, 6, 7, 8]:
        assert by_index[i]["count"] == 0
        assert by_index[i]["mean"] is None
        assert by_index[i]["positive_rate"] is None


def test_bin_edge_standard_boundaries():
    # k/10 边界全部归入右侧箱：0.2、0.8 不在 [0.1,0.2)/[0.7,0.8)
    event_id = settled_event([
        ("0.2", "s02"), ("0.3", "s03"), ("0.8", "s08"),
    ], "0")
    bins = {b["index"]: b for b in report([event_id])["bins"]}
    assert bins[1]["count"] == 0
    assert bins[2]["count"] == 1
    assert bins[7]["count"] == 0
    assert bins[8]["count"] == 1
    assert bins[3]["count"] == 1
    # 全部反例：任意非空箱 positive_rate = 0
    assert bins[2]["positive_rate"] == "0.000000"


def test_half_even_rounding():
    # 平方误差恰落在第 7 位为 5 的情形：p=0.1234565, y=0
    # SE = 0.0152414...? 改用可验证的简单分数：p 使 p^2 在第 7 位为 5 较难，
    # 直接验证分箱频率 1/16 = 0.0625 不涉及五成双；这里构造 1/8 箱频率
    # = 0.125000，以及用纯函数验证 0.0000005 的五成双去向。
    assert f"{calibration.quantize6(Decimal('0.0000005')):f}" == "0.000000"  # 偶数
    assert f"{calibration.quantize6(Decimal('0.0000015')):f}" == "0.000002"  # 向偶数
    assert f"{calibration.quantize6(Decimal('0.0000025')):f}" == "0.000002"
    assert f"{calibration.quantize6(Decimal('0.1234565')):f}" == "0.123456"  # 6 为偶
    assert f"{calibration.quantize6(Decimal('0.1234575')):f}" == "0.123458"  # 8 为偶


# --------------------------------------------------------------------------- #
# 高精度概率：单条平方误差、跨事件均分与分箱均值在最终 6 位量化前
# 不得被默认 Decimal 上下文的 28 位有效数字提前舍入
# --------------------------------------------------------------------------- #

# 50 位小数的高精度揭示原文
HP1 = "0.50000049999975000024999968750043749934375103124833"
HP2 = "0.50000099999900000000000000000000000000000000000002"
HP3 = "0.50000050000000000000000000000000000000000000000001"
HP4 = "0.50000050000000000000000000000000000000000000000002"


def _exact_quantize6(value: Fraction) -> str:
    """对非负有理数做 6 位小数五成双量化（纯整数运算，与 Decimal 上下文无关）。"""
    scaled = value * 10**6
    q, r = divmod(scaled.numerator, scaled.denominator)
    twice = 2 * r
    if twice > scaled.denominator or (twice == scaled.denominator and q % 2 == 1):
        q += 1
    return f"{q // 10**6}.{q % 10**6:06d}"


def _frac_prob(text: str) -> Fraction:
    """把概率原文转为精确有理数（Fraction 不接受 ``.25`` 这类开头为点的形式，
    先经 Decimal 精确解析）。"""
    return Fraction(Decimal(text))


def _exact_squared_error(text: str, outcome: int) -> Decimal:
    """在足够精度的局部上下文中计算 (p-y)^2，作为被测实现的独立参照。"""
    with localcontext() as ctx:
        ctx.prec = 200
        return (Decimal(text) - Decimal(outcome)) ** 2


def test_high_precision_squared_error_not_pre_rounded():
    # 回归：(HP1-0)^2 = 0.2500005000000000000000000000000000000000000000000057784…
    # 精确值严格大于五成双分界 0.2500005，6 位量化应为 0.250001；
    # 默认 28 位上下文把平方误差提前舍入成恰好 0.2500005，五成双误为 0.250000。
    event_id = settled_event([(HP1, "hp-se")], "0")
    body = report([event_id])
    assert body["valid_prediction_count"] == 1
    assert body["predictions"][0]["squared_error"] == "0.250001"
    # 单条预测时 Brier 均分退化为该平方误差
    assert body["brier_mean"] == "0.250001"
    assert f"{calibration.quantize6(_exact_squared_error(HP1, 0)):f}" == "0.250001"
    # 概率本身（分箱均值）不受影响：HP1 = 0.5000004999… 量化为 0.500000
    bin5 = body["bins"][5]
    assert bin5["count"] == 1
    assert bin5["mean"] == _exact_quantize6(_frac_prob(HP1)) == "0.500000"
    assert bin5["positive_rate"] == "0.000000"


def test_high_precision_brier_mean_across_events_order_independent():
    # 两个事件各一条高精度预测：Brier 均分 = (HP1^2 + HP2^2) / 2，
    # 结果与精确十进制计算一致，且与请求中的事件顺序无关。
    a = settled_event([(HP1, "hp-a")], "0")
    b = settled_event([(HP2, "hp-b")], "0")
    expected_se = {
        HP1: _exact_quantize6(_frac_prob(HP1) ** 2),
        HP2: _exact_quantize6(_frac_prob(HP2) ** 2),
    }
    expected_brier = _exact_quantize6(
        (_frac_prob(HP1) ** 2 + _frac_prob(HP2) ** 2) / 2
    )
    expected_bin_mean = _exact_quantize6(
        (_frac_prob(HP1) + _frac_prob(HP2)) / 2
    )
    assert expected_se[HP1] == "0.250001"
    assert expected_brier == "0.250001"

    forward = report([a, b])
    backward = report([b, a])
    for body in (forward, backward):
        assert body["valid_prediction_count"] == 2
        assert body["brier_mean"] == expected_brier
        bin5 = body["bins"][5]
        assert bin5["count"] == 2
        assert bin5["mean"] == expected_bin_mean
        assert bin5["positive_rate"] == "0.000000"
        se = {p["original_text"]: p["squared_error"] for p in body["predictions"]}
        assert se == expected_se
    # 分箱汇总不随输入顺序改变
    assert forward["bins"] == backward["bins"]
    # 逐条计分不随输入顺序改变（条目分组顺序由事件 ID 决定）
    se_forward = {p["original_text"]: p["squared_error"] for p in forward["predictions"]}
    se_backward = {p["original_text"]: p["squared_error"]
                   for p in backward["predictions"]}
    assert se_forward == se_backward


def test_high_precision_brier_mean_of_two_identical_predictions():
    # 两条相同的高精度预测：均分仍为 HP1^2，默认 28 位精度会在求和/均分后
    # 塌缩成 0.2500005 而误报 0.250000。
    a = settled_event([(HP1, "hp-dup-a")], "0")
    b = settled_event([(HP1, "hp-dup-b")], "0")
    body = report([a, b])
    assert body["valid_prediction_count"] == 2
    assert body["brier_mean"] == "0.250001"
    assert [p["squared_error"] for p in body["predictions"]] == [
        "0.250001", "0.250001",
    ]


def test_high_precision_bin_mean_not_pre_rounded():
    # 箱内均值 (HP3+HP4)/2 = 0.5000005000000000000000000000000000000000000000000015
    # 严格大于五成双分界 0.5000005；默认 28 位精度会把和提前舍入到恰好
    # 1.000001000000000000000000000，均值落在分界上而误为 0.500000。
    event_id = settled_event([(HP3, "hp3"), (HP4, "hp4")], "1")
    body = report([event_id])
    bin5 = body["bins"][5]
    assert bin5["count"] == 2
    assert bin5["mean"] == "0.500001"
    assert bin5["positive_rate"] == "1.000000"


def test_probability_format_variants():
    event_id = settled_event([
        ("0", "v0"), ("1", "v1"), ("0.0", "v00"), ("1.00", "v100"),
        (".25", "vdot"), ("000.5", "vlead"), ("1.", "vdottail"),
    ], "1")
    body = report([event_id])
    assert body["valid_prediction_count"] == 7
    assert body["invalid_count"] == 0
    # 端点预测的平方误差
    se = {p["original_text"]: p["squared_error"] for p in body["predictions"]}
    assert se["0"] == "1.000000"   # (0-1)^2
    assert se["1"] == "0.000000"
    assert se[".25"] == "0.562500"

    bad_event = settled_event([
        ("+0.5", "b-plus"),       # 符号
        ("-0", "b-minus"),
        ("1e-1", "b-exp"),        # 指数
        ("5E0", "b-exp2"),
        (" 0.5", "b-ws1"),        # 空白
        ("0.5 ", "b-ws2"),
        ("0.5\n", "b-ws3"),
        ("\t0.5", "b-ws4"),
        ("", "b-empty"),          # 空串
        ("abc", "b-abc"),
        ("0.5.6", "b-dot2"),
        ("1_000", "b-under"),
        ("NaN", "b-nan"),
        ("Infinity", "b-inf"),
        ("0x1", "b-hex"),
        ("1.0000000001", "b-over1"),  # 超范围
        ("-0.0", "b-negzero"),
        ("9", "b-nine"),
    ], "0")
    body = report([bad_event])
    assert body["valid_prediction_count"] == 0
    assert body["invalid_count"] == 18
    reasons = {i["original_text"]: i["reason"] for i in body["invalid"]}
    assert reasons["+0.5"] == "INVALID_FORMAT"
    assert reasons["-0"] == "INVALID_FORMAT"
    assert reasons["1e-1"] == "INVALID_FORMAT"
    assert reasons[" 0.5"] == "INVALID_FORMAT"
    assert reasons[""] == "INVALID_FORMAT"
    assert reasons["1_000"] == "INVALID_FORMAT"
    assert reasons["NaN"] == "INVALID_FORMAT"
    assert reasons["Infinity"] == "INVALID_FORMAT"
    assert reasons["1.0000000001"] == "OUT_OF_RANGE"
    assert reasons["9"] == "OUT_OF_RANGE"
    assert reasons["-0.0"] == "INVALID_FORMAT"


def test_report_is_read_only_and_repeatable():
    event_id = settled_event([("0.3", "s1"), ("bad", "s2")], "1",
                             unrevealed_texts=["secret"])
    first = report([event_id])
    second = report([event_id])
    # generated_at 在冻结时钟下相同；整体逐字节一致
    assert first == second

    # 既有结算与计数不变
    r = client.get(f"/events/{event_id}/settlement")
    assert r.json()["status"] == "settled"
    assert r.json()["standard_result"] == "1"
    r = client.get(f"/events/{event_id}")
    assert r.json()["commitment_count"] == 3
    assert r.json()["reveal_count"] == 2


def test_response_top_level_field_order_fixed():
    event_id = settled_event([("0.5", "s")], "1")
    r = client.post("/events/probability-calibration-report",
                    json={"event_ids": [event_id]})
    assert list(r.json().keys()) == [
        "generated_at", "event_count", "event_ids",
        "valid_prediction_count", "invalid_count",
        "unrevealed_commitment_count", "brier_mean",
        "predictions", "invalid", "bins",
    ]
    assert list(r.json()["bins"][0].keys()) == [
        "index", "range", "lower_bound", "upper_bound",
        "count", "mean", "positive_rate",
    ]


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #

def test_parse_probability_pure():
    ok = {
        "0": Decimal("0"), "1": Decimal("1"), "0.5": Decimal("0.5"),
        ".25": Decimal(".25"), "1.": Decimal("1"), "00.10": Decimal("00.10"),
    }
    for text, expected in ok.items():
        value, reason = calibration.parse_probability(text)
        assert reason is None, text
        assert value == expected

    invalid_format = [
        "", "+", "-", ".", "+0.5", "-0", "1e-1", "5E0", " 0.5", "0.5 ",
        "0.5\n", "\t.1", "abc", "0.5.6", "1_000", "NaN", "Infinity",
        "-Infinity", "0x1", "１",  # 全角数字
    ]
    for text in invalid_format:
        value, reason = calibration.parse_probability(text)
        assert value is None and reason == calibration.INVALID_FORMAT, repr(text)

    out_of_range = ["1.0000000001", "9", "1.0001", "10"]
    for text in out_of_range:
        value, reason = calibration.parse_probability(text)
        assert value is None and reason == calibration.OUT_OF_RANGE, repr(text)


def test_bin_index_pure():
    def idx(text):
        value, _ = calibration.parse_probability(text)
        return calibration.bin_index_for(value)

    assert idx("0") == 0
    assert idx("0.099999") == 0
    assert idx("0.1") == 1       # 边界归右侧
    assert idx("0.2") == 2
    assert idx("0.5") == 5
    assert idx("0.899999") == 8
    assert idx("0.9") == 9
    assert idx("1") == 9         # 右端点归最后一箱


def test_build_report_pure_decimal_exact():
    sources = [
        calibration.CalibrationSource(
            event_id=_uuid(1), commitment_id=_uuid(101),
            committed_at=T0, original_text="0.1", outcome=0,
        ),
        calibration.CalibrationSource(
            event_id=_uuid(1), commitment_id=_uuid(102),
            committed_at=T0, original_text="0.1", outcome=1,
        ),
        calibration.CalibrationSource(
            event_id=_uuid(2), commitment_id=_uuid(201),
            committed_at=T0, original_text="1", outcome=1,
        ),
    ]
    result = calibration.build_calibration_report(sources, unrevealed_count=4)
    assert result.unrevealed_count == 4
    # (0.1-0)^2 + (0.1-1)^2 + (1-1)^2 = 0.01 + 0.81 + 0 = 0.82，均分 0.82/3
    assert result.brier_mean == Decimal("0.82") / Decimal("3")
    assert result.predictions[0].squared_error == Decimal("0.01")
    assert result.predictions[1].squared_error == Decimal("0.81")
    assert result.predictions[2].squared_error == Decimal("0")
    assert result.bins[1].count == 2
    assert result.bins[1].mean == Decimal("0.1")
    assert result.bins[1].positive_rate == Decimal("0.5")
    assert result.bins[9].count == 1
    # 排序：事件 1 的两条（按 commitment_id）在事件 2 之前
    assert [p.commitment_id for p in result.predictions] == [
        _uuid(101), _uuid(102), _uuid(201)
    ]


def test_build_report_high_precision_squared_error_exact_pure():
    # 50 位小数概率的平方误差有 100 位小数，必须逐位保留（默认上下文 28 位
    # 会提前舍入），且单条预测的 Brier 均分等于平方误差本身。
    source = calibration.CalibrationSource(
        event_id=_uuid(1), commitment_id=_uuid(11),
        committed_at=T0, original_text=HP1, outcome=0,
    )
    result = calibration.build_calibration_report([source], 0)
    with localcontext() as ctx:
        ctx.prec = 200
        expected_se = Decimal(HP1) ** 2
    assert result.predictions[0].squared_error == expected_se
    assert result.brier_mean == expected_se
    # 最终量化为 0.250001（精确值严格大于 0.2500005）
    assert f"{calibration.quantize6(expected_se):f}" == "0.250001"
    # 分箱均值即概率本身（同一箱单样本）
    assert result.bins[5].mean == Decimal(HP1)
    assert result.bins[5].count == 1


def test_build_report_order_independent_pure():
    # 多个高精度预测时，打乱输入顺序不改变任何汇总结果
    sources = [
        calibration.CalibrationSource(
            event_id=_uuid(2), commitment_id=_uuid(202),
            committed_at=T0, original_text=HP2, outcome=0,
        ),
        calibration.CalibrationSource(
            event_id=_uuid(1), commitment_id=_uuid(101),
            committed_at=T0, original_text=HP1, outcome=0,
        ),
        calibration.CalibrationSource(
            event_id=_uuid(1), commitment_id=_uuid(102),
            committed_at=T0, original_text=HP3, outcome=1,
        ),
        calibration.CalibrationSource(
            event_id=_uuid(2), commitment_id=_uuid(201),
            committed_at=T0, original_text=HP4, outcome=1,
        ),
    ]
    forward = calibration.build_calibration_report(sources, unrevealed_count=2)
    backward = calibration.build_calibration_report(list(reversed(sources)), 2)
    assert forward == backward

    # 与精确十进制计算一致（独立有理数参照）
    with localcontext() as ctx:
        ctx.prec = 200
        expected_brier = (
            Decimal(HP1) ** 2 + Decimal(HP2) ** 2
            + (Decimal(HP3) - 1) ** 2 + (Decimal(HP4) - 1) ** 2
        ) / 4
    assert f"{calibration.quantize6(forward.brier_mean):f}" == \
        _exact_quantize6(
            (
                _frac_prob(HP1) ** 2 + _frac_prob(HP2) ** 2
                + (_frac_prob(HP3) - 1) ** 2 + (_frac_prob(HP4) - 1) ** 2
            ) / 4
        )
    assert forward.brier_mean == expected_brier
    # HP1、HP3、HP4 都在第 5 箱，HP2 也在第 5 箱（均 >= 0.5 且 < 0.6）
    assert forward.bins[5].count == 4
    assert f"{calibration.quantize6(forward.bins[5].mean):f}" == \
        _exact_quantize6(
            (_frac_prob(HP1) + _frac_prob(HP2) + _frac_prob(HP3) + _frac_prob(HP4)) / 4
        )
    assert f"{calibration.quantize6(forward.bins[5].positive_rate):f}" == "0.500000"


def test_build_report_leaves_global_decimal_context_untouched():
    from decimal import getcontext

    before = getcontext().prec
    source = calibration.CalibrationSource(
        event_id=_uuid(1), commitment_id=_uuid(11),
        committed_at=T0, original_text=HP1, outcome=0,
    )
    result = calibration.build_calibration_report([source], 0)
    calibration.quantize6(result.predictions[0].squared_error)
    assert getcontext().prec == before == 28


def _uuid(suffix: int):
    import uuid
    return uuid.UUID(int=suffix)
