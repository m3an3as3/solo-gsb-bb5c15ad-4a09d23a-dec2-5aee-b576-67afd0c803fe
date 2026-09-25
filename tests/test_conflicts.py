"""POST /events/conflicts 的冲突分析测试。

sweep-line 几何用例通过直接写库、显式指定 created_at / 两个截止时间，
避免依赖墙钟；app.clock.now_utc 同样替换为固定时钟。另对纯函数
app.conflicts.sweep_conflicts 做单元级边界验证。
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_conflicts_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/conflicts.db"

from fastapi.testclient import TestClient  # noqa: E402

import app.clock as clock_module  # noqa: E402
import app.main as main_module  # noqa: E402
from app.conflicts import COMMIT, REVEAL, ConflictSegment, PhaseWindow, sweep_conflicts  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Event  # noqa: E402

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
    # 每个用例独立打补丁，避免模块导入顺序覆盖 test_api.py 的模拟时钟
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


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# 纯函数：sweep_conflicts
# --------------------------------------------------------------------------- #

def test_pure_half_open_touching_does_not_conflict():
    # A [0,10) 与 B [10,20) 首尾相接：半开边界下不冲突
    a = uuid4()
    b = uuid4()
    windows = [
        PhaseWindow(a, COMMIT, t(0), t(10)),
        PhaseWindow(b, COMMIT, t(10), t(20)),
    ]
    assert sweep_conflicts(windows) == []


def test_pure_different_phases_never_conflict():
    # 同一时段但分属 commit / reveal：不算冲突
    a, b = uuid4(), uuid4()
    windows = [
        PhaseWindow(a, COMMIT, t(0), t(10)),
        PhaseWindow(b, REVEAL, t(0), t(10)),
    ]
    assert sweep_conflicts(windows) == []


def test_pure_triple_overlap_and_sorted_ids():
    small = UUID("00000000-0000-0000-0000-000000000001")
    middle = UUID("00000000-0000-0000-0000-000000000002")
    large = UUID("00000000-0000-0000-0000-000000000003")
    # 故意乱序传入，结果内的 event_ids 仍须升序
    windows = [
        PhaseWindow(large, COMMIT, t(0), t(10)),
        PhaseWindow(small, COMMIT, t(0), t(10)),
        PhaseWindow(middle, COMMIT, t(0), t(10)),
    ]
    result = sweep_conflicts(windows)
    assert result == [
        ConflictSegment(t(0), t(10), COMMIT, (small, middle, large))
    ]


def test_pure_segments_sorted_with_phase_tiebreak():
    # 同一对起止时间上 commit / reveal 各有一个冲突片段：commit 在前
    a, b, c, d = (uuid4() for _ in range(4))
    windows = [
        PhaseWindow(a, REVEAL, t(0), t(10)),
        PhaseWindow(b, REVEAL, t(0), t(10)),
        PhaseWindow(c, COMMIT, t(0), t(10)),
        PhaseWindow(d, COMMIT, t(0), t(10)),
    ]
    result = sweep_conflicts(windows)
    assert [s.phase for s in result] == [COMMIT, REVEAL]
    assert result[0].start == t(0) and result[0].end == t(10)


def test_pure_zero_length_window_ignored():
    a, b = uuid4(), uuid4()
    windows = [
        PhaseWindow(a, COMMIT, t(5), t(5)),
        PhaseWindow(b, COMMIT, t(0), t(10)),
    ]
    assert sweep_conflicts(windows) == []


def test_pure_partial_overlap_chains():
    # A [0,10) / B [5,15) / C [10,20)：无三者同时重叠
    a, b, c = uuid4(), uuid4(), uuid4()
    windows = [
        PhaseWindow(a, COMMIT, t(0), t(10)),
        PhaseWindow(b, COMMIT, t(5), t(15)),
        PhaseWindow(c, COMMIT, t(10), t(20)),
    ]
    result = sweep_conflicts(windows)
    assert [(s.start, s.end, s.event_ids) for s in result] == [
        (t(5), t(10), tuple(sorted((a, b)))),
        (t(10), t(15), tuple(sorted((b, c)))),
    ]


# --------------------------------------------------------------------------- #
# API：POST /events/conflicts
# --------------------------------------------------------------------------- #

def test_empty_request_returns_stable_empty_result():
    r = client.post("/events/conflicts", json={"event_ids": []})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["segments"] == []
    assert body["count"] == 0
    assert body["phases"] == ["commit", "reveal"]
    assert parse_iso(body["generated_at"]) == BASE


def test_missing_field_treated_as_empty():
    r = client.post("/events/conflicts", json={})
    assert r.status_code == 200
    assert r.json()["segments"] == []


def test_unknown_event_returns_404():
    known = uuid4()
    make_event(known, t(0), t(10), t(20), title="known")
    missing = "00000000-0000-0000-0000-000000000000"
    r = client.post(
        "/events/conflicts",
        json={"event_ids": [str(known), missing]},
    )
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "EVENT_NOT_FOUND"
    assert missing in detail["message"]


def test_invalid_uuid_returns_422():
    r = client.post("/events/conflicts", json={"event_ids": ["not-a-uuid"]})
    assert r.status_code == 422


def test_conflict_geometry_across_three_events():
    #   created  commit_dl  reveal_dl
    # A    0        10          20
    # B    5        15          25
    # C   10        20          30
    a, b, c = uuid4(), uuid4(), uuid4()
    make_event(a, t(0), t(10), t(20), title="A")
    make_event(b, t(5), t(15), t(25), title="B")
    make_event(c, t(10), t(20), t(30), title="C")

    r = client.post(
        "/events/conflicts",
        json={"event_ids": [str(c), str(a), str(b)]},  # 故意乱序请求
    )
    assert r.status_code == 200, r.text
    body = r.json()
    segments = body["segments"]
    assert body["count"] == len(segments) == 4

    expected = [
        (t(5), t(10), COMMIT, {a, b}),
        (t(10), t(15), COMMIT, {b, c}),
        (t(15), t(20), REVEAL, {a, b}),
        (t(20), t(25), REVEAL, {b, c}),
    ]
    for seg, (start, end, phase, ids) in zip(segments, expected):
        assert parse_iso(seg["start"]) == start
        assert parse_iso(seg["end"]) == end
        assert seg["phase"] == phase
        assert {UUID(x) for x in seg["event_ids"]} == ids
        assert seg["event_ids"] == sorted(seg["event_ids"])  # 片段内稳定排序

    # 整体按起始时间排序
    starts = [parse_iso(s["start"]) for s in segments]
    assert starts == sorted(starts)

    # 半开边界：A 与 C 首尾相接，任何片段都不应同时包含两者
    for seg in segments:
        assert not ({str(a), str(c)} <= set(seg["event_ids"]))


def test_half_open_boundaries_via_api():
    # X: commit [100,110) reveal [110,120)；Y: commit [110,120) reveal [120,130)
    x, y = uuid4(), uuid4()
    make_event(x, t(100), t(110), t(120), title="X")
    make_event(y, t(110), t(120), t(130), title="Y")
    r = client.post(
        "/events/conflicts", json={"event_ids": [str(x), str(y)]}
    )
    assert r.status_code == 200
    assert r.json()["segments"] == []
    assert r.json()["count"] == 0


def test_disjoint_events_no_conflicts():
    x, y = uuid4(), uuid4()
    make_event(x, t(100), t(110), t(120), title="X")
    make_event(y, t(200), t(210), t(220), title="Y")
    r = client.post(
        "/events/conflicts", json={"event_ids": [str(x), str(y)]}
    )
    assert r.status_code == 200
    assert r.json()["segments"] == []


def test_duplicate_event_ids_are_deduplicated():
    x = uuid4()
    make_event(x, t(100), t(110), t(120), title="X")
    r = client.post(
        "/events/conflicts", json={"event_ids": [str(x), str(x), str(x)]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # 单个事件不可能与自身冲突，且片段内不出现重复 ID
    assert body["segments"] == []
    assert body["count"] == 0


def test_single_event_has_no_conflicts():
    x = uuid4()
    make_event(x, t(0), t(10), t(20), title="solo")
    r = client.post("/events/conflicts", json={"event_ids": [str(x)]})
    assert r.status_code == 200
    assert r.json()["segments"] == []
