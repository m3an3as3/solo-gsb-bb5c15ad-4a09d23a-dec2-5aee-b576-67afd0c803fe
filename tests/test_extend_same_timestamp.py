"""同一时刻连续两次延长的延长链排序回归测试。

场景（created_at 完全相同，且链条上第二条记录的 UUID 小于第一条）：
  T0+0min   事件创建（commit=T0+1h, reveal=T0+2h）
  T0+10min  延长 #1：commit=T0+3h, reveal=T0+4h（UUID 较大）
  T0+10min  延长 #2：commit=T0+5h, reveal=T0+6h（UUID 较小）

业务先后只能由延长链中新截止时间的严格递增确定，不能由 (created_at, id)
确定——后者会把 UUID 较小的延长 #2 误排到前面，导致快照回退、幂等重放
误报 409、时间线同刻记录倒置。
"""

import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_extend_chain_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/extend_chain.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.clock as clock_module  # noqa: E402
import app.main as main_module  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import DeadlineExtension, Event  # noqa: E402

T0 = datetime(2030, 9, 1, 0, 0, 0, tzinfo=timezone.utc)

COMMIT_DL = T0 + timedelta(hours=1)
REVEAL_DL = T0 + timedelta(hours=2)

FIRST_COMMIT = T0 + timedelta(hours=3)
FIRST_REVEAL = T0 + timedelta(hours=4)
SECOND_COMMIT = T0 + timedelta(hours=5)
SECOND_REVEAL = T0 + timedelta(hours=6)

EXTENDED_AT = T0 + timedelta(minutes=10)

# 链条上第二条延长的 UUID 必须更小（旧的 (created_at, id) 排序据此误排）；
# 具体值每次用例生成，避免模块级 SQLite 文件库跨用例的主键冲突
def _ordered_extension_ids() -> tuple[uuid.UUID, uuid.UUID]:
    while True:
        a, b = uuid.uuid4(), uuid.uuid4()
        if a != b:
            break
    # 第一条延长用较大 UUID，第二条延长用较小 UUID
    return (max(a, b), min(a, b))


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


def iso(dt: datetime) -> str:
    return dt.isoformat()


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


@pytest.fixture
def same_timestamp_event() -> str:
    r = client.post("/events", json={
        "title": "同刻双延长事件",
        "commit_deadline": iso(COMMIT_DL),
        "reveal_deadline": iso(REVEAL_DL),
    })
    assert r.status_code == 201, r.text
    event_id = uuid.UUID(r.json()["id"])

    first_id, second_id = _ordered_extension_ids()

    # 直接写入两条 created_at 完全相同的成功延长：第二条 UUID 更小，
    # 但新截止时间严格更大，业务顺序为 #1 -> #2；事件字段更新为链尾值
    db = SessionLocal()
    try:
        db.add(
            DeadlineExtension(
                id=first_id,
                event_id=event_id,
                old_commit_deadline=COMMIT_DL,
                old_reveal_deadline=REVEAL_DL,
                new_commit_deadline=FIRST_COMMIT,
                new_reveal_deadline=FIRST_REVEAL,
                reason="第一次延长",
                created_at=EXTENDED_AT,
            )
        )
        db.add(
            DeadlineExtension(
                id=second_id,
                event_id=event_id,
                old_commit_deadline=FIRST_COMMIT,
                old_reveal_deadline=FIRST_REVEAL,
                new_commit_deadline=SECOND_COMMIT,
                new_reveal_deadline=SECOND_REVEAL,
                reason="第二次延长",
                created_at=EXTENDED_AT,
            )
        )
        event = db.get(Event, event_id)
        event.commit_deadline = SECOND_COMMIT
        event.reveal_deadline = SECOND_REVEAL
        db.commit()
    finally:
        db.close()

    return str(event_id)


def test_snapshot_at_same_timestamp_applies_both_extensions(same_timestamp_event):
    # 恰好落在两条延长的同一发生时刻（边界含 at）：两次延长都应生效，
    # 生效截止时间是链尾（第二次延长）的新值，而不是回退到旧截止时间
    r = client.get(
        f"/events/{same_timestamp_event}/snapshot", params={"at": iso(EXTENDED_AT)}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert dt(body["commit_deadline"]) == SECOND_COMMIT
    assert dt(body["reveal_deadline"]) == SECOND_REVEAL
    assert body["status"] == "commit"

    # 该时刻之后同样取链尾值
    r = client.get(
        f"/events/{same_timestamp_event}/snapshot",
        params={"at": iso(T0 + timedelta(days=1))},
    )
    body = r.json()
    assert dt(body["commit_deadline"]) == SECOND_COMMIT
    assert dt(body["reveal_deadline"]) == SECOND_REVEAL


def test_snapshot_before_same_timestamp_keeps_original(same_timestamp_event):
    # 同刻双延长发生之前：原始截止时间仍生效
    at = EXTENDED_AT - timedelta(seconds=1)
    body = client.get(
        f"/events/{same_timestamp_event}/snapshot", params={"at": iso(at)}
    ).json()
    assert dt(body["commit_deadline"]) == COMMIT_DL
    assert dt(body["reveal_deadline"]) == REVEAL_DL


def test_latest_request_replays_first_response_idempotently(same_timestamp_event):
    # 重放链尾（第二次）延长的原始请求：仍幂等返回首次响应，不写入、不报 409
    payload = {
        "commit_deadline": iso(SECOND_COMMIT),
        "reveal_deadline": iso(SECOND_REVEAL),
        "reason": "第二次延长",
    }
    clock.t = T0 + timedelta(minutes=30)
    r = client.post(f"/events/{same_timestamp_event}/extend", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert dt(body["new_commit_deadline"]) == SECOND_COMMIT
    assert dt(body["new_reveal_deadline"]) == SECOND_REVEAL
    assert dt(body["old_commit_deadline"]) == FIRST_COMMIT
    assert dt(body["old_reveal_deadline"]) == FIRST_REVEAL
    assert body["reason"] == "第二次延长"
    assert dt(body["extended_at"]) == EXTENDED_AT

    db = SessionLocal()
    try:
        count = (
            db.query(DeadlineExtension)
            .filter(DeadlineExtension.event_id == uuid.UUID(same_timestamp_event))
            .count()
        )
    finally:
        db.close()
    assert count == 2


def test_timeline_orders_same_timestamp_records_like_chain(same_timestamp_event):
    r = client.get(f"/events/{same_timestamp_event}/timeline")
    assert r.status_code == 200, r.text
    extended = [e for e in r.json()["entries"] if e["type"] == "deadline_extended"]
    assert len(extended) == 2
    # 两条记录 at 完全相同，顺序必须与延长链一致：#1 -> #2
    assert dt(extended[0]["at"]) == dt(extended[1]["at"]) == EXTENDED_AT
    assert dt(extended[0]["detail"]["new_commit_deadline"]) == FIRST_COMMIT
    assert dt(extended[1]["detail"]["new_commit_deadline"]) == SECOND_COMMIT
    assert extended[0]["detail"]["reason"] == "第一次延长"
    assert extended[1]["detail"]["reason"] == "第二次延长"

    # event_created 仍展示创建时的原始截止时间（取链条首条的旧值）
    created = r.json()["entries"][0]["detail"]
    assert dt(created["commit_deadline"]) == COMMIT_DL
    assert dt(created["reveal_deadline"]) == REVEAL_DL
