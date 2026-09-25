"""POST /events/schedule 的资源排程测试。

几何用例通过直接写库、显式指定 created_at / 两个截止时间，避免依赖墙钟；
app.clock.now_utc 同样替换为固定时钟。另对纯函数 app.scheduling.color_phase /
overload_pairs / schedule 做单元级边界验证。
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_schedule_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/schedule.db"

from fastapi.testclient import TestClient  # noqa: E402

import app.clock as clock_module  # noqa: E402
import app.main as main_module  # noqa: E402
from app.conflicts import COMMIT, REVEAL, PhaseWindow  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Event  # noqa: E402
from app.scheduling import color_phase, overload_pairs, schedule  # noqa: E402

BASE = datetime(2030, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def t(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


class FixedClock:
    def __call__(self) -> datetime:
        return BASE


_client_clock = FixedClock()
client = TestClient(main_module.app)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    # 每个用例独立打补丁，避免模块导入顺序覆盖其他测试模块的模拟时钟
    monkeypatch.setattr(clock_module, "now_utc", _client_clock)
    with client:  # 触发 lifespan 建表（幂等）
        yield


def make_event(event_id: UUID, created: datetime, commit_dl: datetime,
               reveal_dl: datetime, title: str = "event") -> None:
    db = SessionLocal()
    try:
        db.add(
            Event(
                id=event_id,
                title=title,
                created_at=created,
                commit_deadline=commit_dl,
                reveal_deadline=reveal_dl,
            )
        )
        db.commit()
    finally:
        db.close()


def w(event_id: UUID, phase: str, start: int, end: int) -> PhaseWindow:
    return PhaseWindow(event_id, phase, t(start), t(end))


# --------------------------------------------------------------------------- #
# 纯函数：color_phase
# --------------------------------------------------------------------------- #

def test_pure_touching_intervals_share_track():
    # 半开边界：A [0,10) 与 B [10,20) 首尾相接，可复用同一轨道
    a, b = uuid4(), uuid4()
    tracks = color_phase([w(a, COMMIT, 0, 10), w(b, COMMIT, 10, 20)])
    assert tracks == {a: 1, b: 1}


def test_pure_overlapping_intervals_use_distinct_tracks():
    a, b = uuid4(), uuid4()
    tracks = color_phase([w(a, COMMIT, 0, 10), w(b, COMMIT, 5, 15)])
    assert tracks == {a: 1, b: 2}


def test_pure_chain_reuses_closed_track():
    # A [0,10) / B [5,15) / C [10,20)：C 开始时轨道 1 已释放，应复用
    a, b, c = uuid4(), uuid4(), uuid4()
    tracks = color_phase(
        [w(a, COMMIT, 0, 10), w(b, COMMIT, 5, 15), w(c, COMMIT, 10, 20)]
    )
    assert tracks == {a: 1, b: 2, c: 1}


def test_pure_assignment_independent_of_input_order():
    a, b, c, d = uuid4(), uuid4(), uuid4(), uuid4()
    windows = [
        w(a, COMMIT, 0, 10),
        w(b, COMMIT, 0, 10),
        w(c, COMMIT, 0, 10),
        w(d, COMMIT, 10, 20),
    ]
    first = color_phase(windows)
    # 任意打乱输入，分配完全一致；同起点区间按 event_id 升序分得 1/2/3
    assert color_phase(list(reversed(windows))) == first
    simultaneous = sorted((a, b, c))
    assert first == {
        simultaneous[0]: 1,
        simultaneous[1]: 2,
        simultaneous[2]: 3,
        # D 在 10 时刻所有轨道都已释放，复用编号最小的空闲轨道 1
        d: 1,
    }


def test_pure_same_start_orders_by_end_then_event_id():
    small = UUID("00000000-0000-0000-0000-000000000001")
    large = UUID("00000000-0000-0000-0000-000000000002")
    # 同起点：更短的区间（结束更早）先分配；结束也相同则 event_id 小者先
    tracks = color_phase(
        [w(large, COMMIT, 0, 5), w(small, COMMIT, 0, 10)]
    )
    assert tracks == {large: 1, small: 2}
    tracks = color_phase(
        [w(large, COMMIT, 0, 10), w(small, COMMIT, 0, 10)]
    )
    assert tracks == {small: 1, large: 2}


def test_pure_zero_length_window_gets_no_track():
    a, b = uuid4(), uuid4()
    tracks = color_phase([w(a, COMMIT, 5, 5), w(b, COMMIT, 0, 10)])
    assert tracks == {b: 1}


def test_pure_minimum_tracks_equals_peak_depth():
    # 轨道 1/2/3 各被占用后，新区间总是复用编号最小的空闲轨道
    a, b, c, d, e = (uuid4() for _ in range(5))
    windows = [
        w(a, COMMIT, 0, 10),
        w(b, COMMIT, 1, 11),
        w(c, COMMIT, 2, 3),    # 最短，最先释放
        w(d, COMMIT, 4, 12),   # 开始时仅轨道（c 的）空闲 -> 复用
        w(e, COMMIT, 4, 12),   # 需要第 4 条轨道（峰值深度 4）
    ]
    tracks = color_phase(windows)
    assert tracks[c] == 3
    # d、e 同起点，按 event_id 升序依次取空闲轨道 3 与新开轨道 4
    earlier, later = sorted((d, e))
    assert tracks[earlier] == 3
    assert tracks[later] == 4
    assert max(tracks.values()) == 4


# --------------------------------------------------------------------------- #
# 纯函数：overload_pairs
# --------------------------------------------------------------------------- #

def test_pure_overload_pairs_empty_when_within_limit():
    a, b = uuid4(), uuid4()
    assert overload_pairs([w(a, COMMIT, 0, 10), w(b, COMMIT, 5, 15)], 2) == []


def test_pure_overload_pairs_touching_is_free():
    a, b = uuid4(), uuid4()
    assert overload_pairs([w(a, COMMIT, 0, 10), w(b, COMMIT, 10, 20)], 1) == []


def test_pure_overload_pairs_triple_depth_with_limit_one():
    a, b, c = uuid4(), uuid4(), uuid4()
    pairs = overload_pairs(
        [w(a, COMMIT, 0, 10), w(b, COMMIT, 0, 10), w(c, COMMIT, 0, 10)], 1
    )
    assert pairs == sorted(pairs)
    assert set(pairs) == {tuple(sorted((a, b))), tuple(sorted((a, c))),
                          tuple(sorted((b, c)))}


def test_pure_overload_pairs_only_above_limit_segments():
    # A [0,10) B [5,15) C [10,20)：深度 2 的片段 [5,10) 与 [10,15)
    # 上限 2 -> 无冲突对；上限 1 -> 两对（A,B）（B,C），跨片段去重
    a, b, c = uuid4(), uuid4(), uuid4()
    windows = [w(a, COMMIT, 0, 10), w(b, COMMIT, 5, 15), w(c, COMMIT, 10, 20)]
    assert overload_pairs(windows, 2) == []
    pairs = overload_pairs(windows, 1)
    assert pairs == sorted([tuple(sorted((a, b))), tuple(sorted((b, c)))])


def test_pure_schedule_required_tracks_is_max_over_phases():
    # commit 需要 2 条、reveal 仅需 1 条 -> required = 2；两阶段编号独立
    a = UUID("00000000-0000-0000-0000-000000000001")
    b = UUID("00000000-0000-0000-0000-000000000002")
    windows_by_phase = {
        COMMIT: [w(a, COMMIT, 0, 10), w(b, COMMIT, 0, 10)],
        REVEAL: [w(a, REVEAL, 10, 20), w(b, REVEAL, 20, 30)],
    }
    result = schedule(windows_by_phase, [b, a], 2)
    assert result.required_tracks == 2
    assert result.feasible is True
    assert result.conflicts == []
    by_id = {x.event_id: x for x in result.assignments}
    # assignments 按 event_id 升序，与输入顺序无关
    assert [x.event_id for x in result.assignments] == [a, b]
    # commit 同起点同终点，event_id 小者分得轨道 1
    assert by_id[a].commit_track == 1 and by_id[b].commit_track == 2
    # reveal 首尾相接：两者都在轨道 1（编号与 commit 独立）
    assert by_id[a].reveal_track == 1 and by_id[b].reveal_track == 1


def test_pure_schedule_empty_events():
    result = schedule({COMMIT: [], REVEAL: []}, [], 1)
    assert result.required_tracks == 0
    assert result.feasible is True
    assert result.assignments == []
    assert result.conflicts == []


def test_pure_schedule_conflicts_both_phases_sorted():
    small = UUID("00000000-0000-0000-0000-000000000001")
    middle = UUID("00000000-0000-0000-0000-000000000002")
    large = UUID("00000000-0000-0000-0000-000000000003")
    windows_by_phase = {
        COMMIT: [w(x, COMMIT, 0, 10) for x in (small, middle, large)],
        REVEAL: [w(x, REVEAL, 10, 20) for x in (small, middle, large)],
    }
    result = schedule(windows_by_phase, [large, small, middle], 2)
    assert result.required_tracks == 3
    assert result.feasible is False
    phases = [c.phase for c in result.conflicts]
    assert phases == [COMMIT] * 3 + [REVEAL] * 3  # 阶段顺序确定
    # assignments 按 event_id 升序，与输入顺序无关
    assert [a.event_id for a in result.assignments] == [small, middle, large]
    # 冲突对内 event_id 升序
    for c in result.conflicts:
        assert c.event_id_1 < c.event_id_2


# --------------------------------------------------------------------------- #
# API：POST /events/schedule
# --------------------------------------------------------------------------- #

def test_api_empty_request_feasible_with_zero_tracks():
    r = client.post("/events/schedule", json={"event_ids": [], "track_limit": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "track_limit": 1,
        "required_tracks": 0,
        "feasible": True,
        "assignments": [],
        "conflicts": [],
    }


def test_api_missing_event_ids_field_treated_as_empty():
    r = client.post("/events/schedule", json={"track_limit": 3})
    assert r.status_code == 200
    assert r.json()["assignments"] == []
    assert r.json()["required_tracks"] == 0


@pytest.mark.parametrize("value", [None, 0, -1, 2.0, "2", True, False, [1]])
def test_api_invalid_track_limit_returns_unified_400(value):
    x = uuid4()
    make_event(x, t(0), t(10), t(20), title="x")
    r = client.post(
        "/events/schedule", json={"event_ids": [str(x)], "track_limit": value}
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "INVALID_TRACK_LIMIT"
    assert "track_limit" in detail["message"]


def test_api_missing_track_limit_returns_unified_400():
    r = client.post("/events/schedule", json={"event_ids": []})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_TRACK_LIMIT"


def test_api_unknown_event_returns_404():
    known = uuid4()
    make_event(known, t(0), t(10), t(20), title="known")
    missing = "00000000-0000-0000-0000-000000000000"
    r = client.post(
        "/events/schedule",
        json={"event_ids": [str(known), missing], "track_limit": 2},
    )
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "EVENT_NOT_FOUND"
    assert missing in detail["message"]


def test_api_invalid_uuid_returns_422():
    r = client.post(
        "/events/schedule", json={"event_ids": ["not-a-uuid"], "track_limit": 1}
    )
    assert r.status_code == 422


def test_api_chain_geometry_minimum_coloring():
    #   created  commit_dl  reveal_dl
    # A    0        10          20
    # B    5        15          25
    # C   10        20          30
    a, b, c = uuid4(), uuid4(), uuid4()
    make_event(a, t(0), t(10), t(20), title="A")
    make_event(b, t(5), t(15), t(25), title="B")
    make_event(c, t(10), t(20), t(30), title="C")

    r = client.post(
        "/events/schedule",
        json={"event_ids": [str(c), str(a), str(b)], "track_limit": 2},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["track_limit"] == 2
    assert body["required_tracks"] == 2
    assert body["feasible"] is True
    assert body["conflicts"] == []

    # assignments 按 event_id 升序（与请求顺序无关）
    ids = [x["event_id"] for x in body["assignments"]]
    assert ids == sorted(ids) == sorted(str(x) for x in (a, b, c))

    tracks = {UUID(x["event_id"]): x for x in body["assignments"]}
    # commit: A->1 B->2 C->1；reveal: A->1 B->2 C->1
    assert tracks[a]["commit_track"] == 1
    assert tracks[b]["commit_track"] == 2
    assert tracks[c]["commit_track"] == 1
    assert tracks[a]["reveal_track"] == 1
    assert tracks[b]["reveal_track"] == 2
    assert tracks[c]["reveal_track"] == 1


def test_api_half_open_touching_fits_single_track():
    # X: commit [100,110) reveal [110,120)；Y: commit [110,120) reveal [120,130)
    x, y = uuid4(), uuid4()
    make_event(x, t(100), t(110), t(120), title="X")
    make_event(y, t(110), t(120), t(130), title="Y")
    r = client.post(
        "/events/schedule",
        json={"event_ids": [str(y), str(x)], "track_limit": 1},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["required_tracks"] == 1
    assert body["feasible"] is True
    assert body["conflicts"] == []
    for item in body["assignments"]:
        assert item["commit_track"] == 1
        assert item["reveal_track"] == 1


def test_api_infeasible_returns_conflict_pairs():
    # 三个事件窗口完全重叠：commit [0,10)、reveal [10,20)，各自深度 3
    a, b, c = uuid4(), uuid4(), uuid4()
    make_event(a, t(0), t(10), t(20), title="A")
    make_event(b, t(0), t(10), t(20), title="B")
    make_event(c, t(0), t(10), t(20), title="C")
    r = client.post(
        "/events/schedule",
        json={"event_ids": [str(a), str(b), str(c)], "track_limit": 2},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["required_tracks"] == 3
    assert body["feasible"] is False
    conflicts = body["conflicts"]
    assert len(conflicts) == 6  # 每阶段 C(3,2)=3 对
    assert [x["phase"] for x in conflicts] == [COMMIT] * 3 + [REVEAL] * 3
    expected_pairs = {
        (COMMIT,) + tuple(sorted(str(x) for x in pair))
        for pair in ((a, b), (a, c), (b, c))
    }
    expected_pairs |= {
        (REVEAL,) + tuple(sorted(str(x) for x in pair))
        for pair in ((a, b), (a, c), (b, c))
    }
    actual_pairs = {
        (x["phase"], x["event_id_1"], x["event_id_2"]) for x in conflicts
    }
    assert actual_pairs == expected_pairs
    for x in conflicts:
        assert x["event_id_1"] < x["event_id_2"]

    # 即便不可行，仍返回完整的最小轨道分配
    by_id = {UUID(x["event_id"]): x for x in body["assignments"]}
    assert {
        by_id[a]["commit_track"],
        by_id[b]["commit_track"],
        by_id[c]["commit_track"],
    } == {1, 2, 3}


def test_api_feasible_response_has_no_conflicts_even_with_overlaps():
    # 两两重叠但峰值深度 2：上限 2 可行，conflicts 必须为空
    a, b, c = uuid4(), uuid4(), uuid4()
    make_event(a, t(0), t(10), t(20), title="A")
    make_event(b, t(5), t(15), t(25), title="B")
    make_event(c, t(10), t(20), t(30), title="C")
    r = client.post(
        "/events/schedule",
        json={"event_ids": [str(a), str(b), str(c)], "track_limit": 2},
    )
    assert r.status_code == 200
    assert r.json()["feasible"] is True
    assert r.json()["conflicts"] == []


def test_api_duplicate_ids_deduplicated():
    x = uuid4()
    make_event(x, t(0), t(10), t(20), title="x")
    r = client.post(
        "/events/schedule",
        json={"event_ids": [str(x), str(x), str(x)], "track_limit": 1},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["assignments"]) == 1
    assert body["required_tracks"] == 1


def test_api_deterministic_across_repeats_and_input_order():
    a, b, c = uuid4(), uuid4(), uuid4()
    make_event(a, t(0), t(10), t(20), title="A")
    make_event(b, t(5), t(15), t(25), title="B")
    make_event(c, t(10), t(20), t(30), title="C")
    payload = {"event_ids": [str(a), str(b), str(c)], "track_limit": 1}

    r1 = client.post("/events/schedule", json=payload)
    r2 = client.post("/events/schedule", json=payload)
    r3 = client.post(
        "/events/schedule",
        json={"event_ids": [str(c), str(a), str(b), str(a)], "track_limit": 1},
    )
    assert r1.status_code == r2.status_code == r3.status_code == 200
    assert r1.json() == r2.json() == r3.json()


def test_api_schedule_does_not_modify_events():
    x = uuid4()
    make_event(x, t(0), t(10), t(20), title="x")
    before = client.get(f"/events/{x}").json()
    for i in range(2):
        r = client.post(
            "/events/schedule",
            json={"event_ids": [str(x)], "track_limit": 1},
        )
        assert r.status_code == 200
    after = client.get(f"/events/{x}").json()
    assert before == after
