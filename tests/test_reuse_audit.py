"""POST /events/commitments/reuse-audit 跨事件承诺复用审计测试。

用例通过直接写库并显式指定承诺 / 揭示的 created_at，避免依赖墙钟；
app.clock.now_utc 替换为固定时钟。审计判定只比较 UTC 时刻，不依赖
事件窗口与当前阶段。
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_reuse_audit_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/reuse_audit.db"

from fastapi.testclient import TestClient  # noqa: E402

import app.clock as clock_module  # noqa: E402
import app.main as main_module  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Commitment, Event, Reveal  # noqa: E402

BASE = datetime(2030, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
H1 = "a" * 64
H2 = "b" * 64
H3 = "c" * 64


def t(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class FixedClock:
    def __call__(self) -> datetime:
        return BASE


client = TestClient(main_module.app)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(clock_module, "now_utc", FixedClock())
    with client:  # 触发 lifespan 建表（幂等）
        yield


def make_event(event_id, created=t(0), commit_dl=t(100), reveal_dl=t(200), title="e"):
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


def make_commitment(cid, event_id, h, at):
    db = SessionLocal()
    try:
        db.add(
            Commitment(
                id=cid,
                event_id=event_id,
                commitment_hash=h,
                created_at=at,
            )
        )
        db.commit()
    finally:
        db.close()


def make_reveal(rid, cid, event_id, h, at):
    db = SessionLocal()
    try:
        db.add(
            Reveal(
                id=rid,
                event_id=event_id,
                commitment_id=cid,
                original_text=f"text-{h[:8]}",
                salt=f"salt-{rid}",
                recomputed_hash=h,
                created_at=at,
            )
        )
        db.commit()
    finally:
        db.close()


def audit(event_ids):
    return client.post(
        "/events/commitments/reuse-audit",
        json={"event_ids": [str(x) for x in event_ids]},
    )


def find_item(items, cid):
    return next(i for i in items if i["commitment_id"] == str(cid))


# --------------------------------------------------------------------------- #
# 请求校验
# --------------------------------------------------------------------------- #

def test_too_few_event_ids_returns_422():
    r = client.post("/events/commitments/reuse-audit", json={"event_ids": []})
    assert r.status_code == 422
    one = uuid4()
    make_event(one)
    r = audit([one])
    assert r.status_code == 422


def test_too_many_event_ids_returns_422():
    # 长度校验先于数据库查询：51 个（即使均不存在）也是 422
    r = client.post(
        "/events/commitments/reuse-audit",
        json={"event_ids": [str(uuid4()) for _ in range(51)]},
    )
    assert r.status_code == 422


def test_invalid_uuid_returns_422():
    r = client.post(
        "/events/commitments/reuse-audit", json={"event_ids": ["x", "y"]}
    )
    assert r.status_code == 422


def test_missing_event_returns_404():
    a = uuid4()
    make_event(a)
    missing = "00000000-0000-0000-0000-000000000000"
    r = audit([a, missing])
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "EVENT_NOT_FOUND"
    assert missing in detail["message"]


# --------------------------------------------------------------------------- #
# 重复识别
# --------------------------------------------------------------------------- #

def test_no_duplicates_returns_empty_items():
    a, b = uuid4(), uuid4()
    make_event(a), make_event(b)
    make_commitment(uuid4(), a, H1, t(0))
    make_commitment(uuid4(), b, H2, t(0))

    r = audit([a, b])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == []
    assert body["duplicate_hash_count"] == 0
    assert body["item_count"] == 0
    assert body["event_count"] == 2


def test_unrevealed_duplicates_still_listed_but_not_exposed():
    a, b = uuid4(), uuid4()
    make_event(a), make_event(b)
    ca, cb = uuid4(), uuid4()
    make_commitment(ca, a, H1, t(0))
    make_commitment(cb, b, H1, t(1))

    r = audit([a, b])
    body = r.json()
    assert body["duplicate_hash_count"] == 1
    assert body["item_count"] == 2
    for item in body["items"]:
        assert item["exposed_before_commit"] is False
        assert item["prior_reveal_sources"] == []


def test_prior_reveal_in_other_event_marks_exposed():
    # A: commit H1@t0, reveal@t2；B: commit H1@t5
    a, b = uuid4(), uuid4()
    make_event(a), make_event(b)
    ca, cb = uuid4(), uuid4()
    ra = uuid4()
    make_commitment(ca, a, H1, t(0))
    make_reveal(ra, ca, a, H1, t(2))
    make_commitment(cb, b, H1, t(5))

    body = audit([a, b]).json()
    item_a = find_item(body["items"], ca)
    item_b = find_item(body["items"], cb)

    # A 自己的揭示不计为 A 的事先公开来源；B 从未揭示
    assert item_a["exposed_before_commit"] is False
    assert item_a["prior_reveal_sources"] == []

    # B 提交时 A 已在 t2 揭示：严格早于 t5
    assert item_b["exposed_before_commit"] is True
    sources = item_b["prior_reveal_sources"]
    assert len(sources) == 1
    assert sources[0]["event_id"] == str(a)
    assert sources[0]["commitment_id"] == str(ca)
    assert sources[0]["reveal_id"] == str(ra)
    assert parse_iso(sources[0]["revealed_at"]) == t(2)


def test_same_instant_reveal_does_not_count_as_prior():
    # A reveal@t5 与 B commit@t5 同刻：严格早于不成立
    a, b = uuid4(), uuid4()
    make_event(a), make_event(b)
    ca, cb = uuid4(), uuid4()
    make_commitment(ca, a, H1, t(0))
    make_reveal(uuid4(), ca, a, H1, t(5))
    make_commitment(cb, b, H1, t(5))

    body = audit([a, b]).json()
    assert find_item(body["items"], cb)["exposed_before_commit"] is False
    assert find_item(body["items"], cb)["prior_reveal_sources"] == []


def test_later_reveal_does_not_count_as_prior():
    # B commit@t5 早于 A reveal@t10：揭示在提交之后，不算事先公开
    a, b = uuid4(), uuid4()
    make_event(a), make_event(b)
    ca, cb = uuid4(), uuid4()
    make_commitment(ca, a, H1, t(0))
    make_commitment(cb, b, H1, t(5))
    make_reveal(uuid4(), ca, a, H1, t(10))

    body = audit([a, b]).json()
    assert find_item(body["items"], cb)["exposed_before_commit"] is False


def test_three_events_multiple_sources_sorted():
    # A commit@t0 reveal@t3；B commit@t5 reveal@t8；C commit@t10
    a, b, c = uuid4(), uuid4(), uuid4()
    for e in (a, b, c):
        make_event(e)
    ca, cb, cc = uuid4(), uuid4(), uuid4()
    ra, rb = uuid4(), uuid4()
    make_commitment(ca, a, H1, t(0))
    make_reveal(ra, ca, a, H1, t(3))
    make_commitment(cb, b, H1, t(5))
    make_reveal(rb, cb, b, H1, t(8))
    make_commitment(cc, c, H1, t(10))

    body = audit([c, a, b]).json()  # 故意乱序请求
    assert body["event_count"] == 3
    item_c = find_item(body["items"], cc)
    assert item_c["exposed_before_commit"] is True
    # C 有两个来源，按揭示时间升序（A@t3 先于 B@t8）
    assert [(s["event_id"], s["reveal_id"]) for s in item_c["prior_reveal_sources"]] == [
        (str(a), str(ra)),
        (str(b), str(rb)),
    ]

    # B 只有 A 一个来源（C 从未揭示）
    item_b = find_item(body["items"], cb)
    assert [s["event_id"] for s in item_b["prior_reveal_sources"]] == [str(a)]


def test_duplicate_event_ids_are_deduplicated():
    a, b = uuid4(), uuid4()
    make_event(a), make_event(b)
    ca, cb = uuid4(), uuid4()
    make_commitment(ca, a, H1, t(0))
    make_commitment(cb, b, H1, t(1))

    r = audit([a, b, a, a, b])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_count"] == 2
    assert body["item_count"] == 2
    # 去重后条目不重复
    ids = [i["commitment_id"] for i in body["items"]]
    assert sorted(ids) == sorted([str(ca), str(cb)])


def test_case_insensitive_hash_grouping():
    # 直接写库混入大写哈希：审计按大小写归一归为同一组
    a, b = uuid4(), uuid4()
    make_event(a), make_event(b)
    ca, cb = uuid4(), uuid4()
    make_commitment(ca, a, H1.upper(), t(0))
    make_commitment(cb, b, H1, t(1))

    body = audit([a, b]).json()
    assert body["duplicate_hash_count"] == 1
    assert {i["commitment_hash"] for i in body["items"]} == {H1}


def test_multiple_duplicate_hashes_counts():
    a, b = uuid4(), uuid4()
    make_event(a), make_event(b)
    for h in (H1, H2, H3):
        make_commitment(uuid4(), a, h, t(0))
    make_commitment(uuid4(), b, H1, t(1))
    make_commitment(uuid4(), b, H2, t(1))
    # H3 仅 A 有，不算跨事件重复

    body = audit([a, b]).json()
    assert body["duplicate_hash_count"] == 2
    assert body["item_count"] == 4
    assert {i["commitment_hash"] for i in body["items"]} == {H1, H2}


# --------------------------------------------------------------------------- #
# 排序
# --------------------------------------------------------------------------- #

def test_items_sorted_by_hash_then_commit_time_then_id():
    a, b = uuid4(), uuid4()
    make_event(a), make_event(b)
    # H1 与 H2 均跨事件重复；H2 的最早提交时间早于 H1，但结果仍须先按哈希排序
    c_h1_a, c_h1_b = uuid4(), uuid4()
    make_commitment(c_h1_a, a, H1, t(9))
    make_commitment(c_h1_b, b, H1, t(9))
    # H2：B@t0 与 A@t5，组内按提交时间
    c_b_early = uuid4()
    c_a_h2 = uuid4()
    make_commitment(c_b_early, b, H2, t(0))
    make_commitment(c_a_h2, a, H2, t(5))

    body = audit([a, b]).json()
    items = body["items"]
    assert [i["commitment_hash"] for i in items] == [H1, H1, H2, H2]
    # H2 组内按提交时间：t0 先于 t5
    h2_items = [i for i in items if i["commitment_hash"] == H2]
    assert [parse_iso(i["committed_at"]) for i in h2_items] == [t(0), t(5)]


def test_same_commit_time_tie_breaks_by_commitment_id():
    a, b = uuid4(), uuid4()
    make_event(a), make_event(b)
    ca, cb = uuid4(), uuid4()
    make_commitment(ca, a, H1, t(7))
    make_commitment(cb, b, H1, t(7))

    body = audit([a, b]).json()
    ids = [i["commitment_id"] for i in body["items"]]
    assert ids == sorted(ids)
    assert ids == sorted([str(ca), str(cb)])


# --------------------------------------------------------------------------- #
# 只读与确定性
# --------------------------------------------------------------------------- #

def test_audit_is_read_only_and_deterministic():
    a, b = uuid4(), uuid4()
    make_event(a), make_event(b)
    ca, cb = uuid4(), uuid4()
    make_commitment(ca, a, H1, t(0))
    make_reveal(uuid4(), ca, a, H1, t(2))
    make_commitment(cb, b, H1, t(5))

    first = audit([a, b]).json()
    second = audit([b, a]).json()
    assert first == second
    assert parse_iso(first["generated_at"]) == BASE

    # 未新增任何承诺 / 揭示：列表端点计数不变
    la = client.get(f"/events/{a}/commitments").json()
    lb = client.get(f"/events/{b}/commitments").json()
    assert len(la) == 1 and la[0]["revealed"] is True
    assert len(lb) == 1 and lb[0]["revealed"] is False


def test_audit_scope_limited_to_requested_events():
    # 事件 C 不在请求中：即使存在同哈希的更早揭示也不作为来源
    a, b, c = uuid4(), uuid4(), uuid4()
    make_event(a), make_event(b), make_event(c)
    ca, cb, cc = uuid4(), uuid4(), uuid4()
    make_commitment(ca, a, H1, t(0))
    make_reveal(uuid4(), ca, a, H1, t(1))
    make_commitment(cb, b, H1, t(5))
    make_commitment(cc, c, H1, t(0))
    make_reveal(uuid4(), cc, c, H1, t(1))

    body = audit([a, b]).json()
    item_b = find_item(body["items"], cb)
    # 仅 A 的揭示列入来源；C 不在审计范围内，且 C 的承诺本身不出现在结果中
    assert [s["event_id"] for s in item_b["prior_reveal_sources"]] == [str(a)]
    assert {i["event_id"] for i in body["items"]} == {str(a), str(b)}
