"""POST /events/{event_id}/reveals/batch 原子批量揭示测试。

SQLite 文件库 + 模拟时钟（与 test_batch_commitments.py 相同手法），覆盖：
  - 揭示期内按输入顺序批量写入并返回 count / 逐项 verified 结果
  - 格式非法（盐为空/超长）/ 请求内重复承诺 / 已揭示 / 无匹配 的逐项 409 清单
  - 混合冲突一次报全，且整体回滚不留部分揭示
  - 数量边界（0、101）422、请求体结构错误 422
  - 非揭示阶段（提前 / 逾期）拒绝、事件不存在 404
  - 与单个揭示端点共享存储；并发竞争由唯一约束兜底，仅首次揭示生效
"""

import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_batch_reveal_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/batch_reveal.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.clock as clock_module  # noqa: E402
import app.main as main_module  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.hashing import compute_commitment  # noqa: E402
from app.models import Reveal  # noqa: E402

T0 = datetime(2030, 4, 1, 0, 0, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t


clock = MutableClock(T0)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    clock.t = T0
    monkeypatch.setattr(clock_module, "now_utc", clock)
    yield


client = TestClient(main_module.app)


def text(i: int) -> str:
    return f"prediction-{i}"


def salt(i: int) -> str:
    return f"salt-{i}"


def h(i: int) -> str:
    return compute_commitment(text(i), salt(i))


def item(i: int) -> dict:
    return {"original_text": text(i), "salt": salt(i)}


def batch_reveal(event_id: str, items):
    return client.post(
        f"/events/{event_id}/reveals/batch",
        json={"reveals": items},
    )


@pytest.fixture
def seeded_event():
    """创建事件并批量提交 5 个承诺（h1..h5），时间停在 commit 阶段。"""
    with client:
        r = client.post("/events", json={
            "title": "批量揭示事件",
            "commit_deadline": (T0 + timedelta(hours=1)).isoformat(),
            "reveal_deadline": (T0 + timedelta(hours=2)).isoformat(),
        })
        assert r.status_code == 201, r.text
        event_id = r.json()["id"]
        r = client.post(
            f"/events/{event_id}/commitments/batch",
            json={"commitment_hashes": [h(i) for i in range(1, 6)]},
        )
        assert r.status_code == 201, r.text
    return event_id


def test_batch_reveals_creates_in_input_order(seeded_event):
    event_id = seeded_event
    clock.t = T0 + timedelta(hours=1, minutes=30)

    r = batch_reveal(event_id, [item(3), item(1), item(2)])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["count"] == 3
    reveals = body["reveals"]
    # 严格按输入顺序返回
    assert [rv["recomputed_hash"] for rv in reveals] == [h(3), h(1), h(2)]
    assert [rv["commitment_hash"] for rv in reveals] == [h(3), h(1), h(2)]
    assert len({rv["id"] for rv in reveals}) == 3
    assert all(rv["event_id"] == event_id for rv in reveals)
    assert all(rv["verified"] is True for rv in reveals)
    assert all(rv["hash_scheme"] == main_module.HASH_SCHEME for rv in reveals)

    r = client.get(f"/events/{event_id}")
    assert r.json()["reveal_count"] == 3
    assert r.json()["commitment_count"] == 5

    # 时间线中按承诺顺序可见全部 3 条揭示，原文可重新核验
    tl = client.get(f"/events/{event_id}/timeline").json()
    accepted = [e for e in tl["entries"] if e["type"] == "reveal_accepted"]
    assert len(accepted) == 3
    assert {e["detail"]["original_text"] for e in accepted} == {
        text(1), text(2), text(3)
    }
    for e in accepted:
        d = e["detail"]
        assert compute_commitment(d["original_text"], d["salt"]) == d["commitment_hash"]


def test_batch_reveals_single_item_ok(seeded_event):
    event_id = seeded_event
    clock.t = T0 + timedelta(hours=1)  # 揭示期起点（含）
    r = batch_reveal(event_id, [item(4)])
    assert r.status_code == 201, r.text
    assert r.json()["count"] == 1
    assert r.json()["reveals"][0]["commitment_hash"] == h(4)


def test_batch_reveals_duplicate_within_request_all_conflict_indices(seeded_event):
    event_id = seeded_event
    clock.t = T0 + timedelta(hours=1, minutes=30)

    r = batch_reveal(event_id, [item(1), item(2), item(1), item(2), item(1)])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "BATCH_REVEAL_CONFLICT"
    conflicts = detail["conflicts"]
    assert [(c["index"], c["type"]) for c in conflicts] == [
        (2, "DUPLICATE_IN_REQUEST"),
        (3, "DUPLICATE_IN_REQUEST"),
        (4, "DUPLICATE_IN_REQUEST"),
    ]
    assert conflicts[0]["original_text"] == text(1)
    assert conflicts[0]["salt"] == salt(1)
    assert conflicts[0]["recomputed_hash"] == h(1)
    # 回滚：零写入
    assert client.get(f"/events/{event_id}").json()["reveal_count"] == 0


def test_batch_reveals_no_matching_commitment(seeded_event):
    event_id = seeded_event
    clock.t = T0 + timedelta(hours=1, minutes=30)

    r = batch_reveal(event_id, [item(1), {"original_text": "不存在的预测", "salt": "xxx"}])
    assert r.status_code == 409
    conflicts = r.json()["detail"]["conflicts"]
    assert [(c["index"], c["type"]) for c in conflicts] == [
        (1, "NO_MATCHING_COMMITMENT"),
    ]
    assert conflicts[0]["recomputed_hash"] == compute_commitment("不存在的预测", "xxx")
    # 未冲突项也不得写入
    assert client.get(f"/events/{event_id}").json()["reveal_count"] == 0


def test_batch_reveals_already_revealed_conflict(seeded_event):
    event_id = seeded_event
    clock.t = T0 + timedelta(hours=1, minutes=30)

    # 单个端点先揭示 h2
    r = client.post(f"/events/{event_id}/reveals", json=item(2))
    assert r.status_code == 201, r.text

    r = batch_reveal(event_id, [item(1), item(2), item(3)])
    assert r.status_code == 409
    conflicts = r.json()["detail"]["conflicts"]
    assert [(c["index"], c["type"]) for c in conflicts] == [(1, "DUPLICATE_REVEAL")]
    assert conflicts[0]["recomputed_hash"] == h(2)
    # 整批回滚：仅先前的 1 条揭示
    assert client.get(f"/events/{event_id}").json()["reveal_count"] == 1

    # 去掉冲突项后整批成功
    r = batch_reveal(event_id, [item(1), item(3)])
    assert r.status_code == 201, r.text
    assert r.json()["count"] == 2


def test_batch_reveals_first_occurrence_reported_as_already_revealed(seeded_event):
    event_id = seeded_event
    clock.t = T0 + timedelta(hours=1, minutes=30)

    r = client.post(f"/events/{event_id}/reveals", json=item(1))
    assert r.status_code == 201
    # h1 已被揭示且请求内重复三次：首个下标记已揭示，其余记请求内重复
    r = batch_reveal(event_id, [item(1), item(1), item(1)])
    assert r.status_code == 409
    assert [(c["index"], c["type"]) for c in r.json()["detail"]["conflicts"]] == [
        (0, "DUPLICATE_REVEAL"),
        (1, "DUPLICATE_IN_REQUEST"),
        (2, "DUPLICATE_IN_REQUEST"),
    ]


def test_batch_reveals_mixed_conflicts_all_reported_and_rolled_back(seeded_event):
    event_id = seeded_event
    clock.t = T0 + timedelta(hours=1, minutes=30)

    # h2 已被揭示
    assert client.post(f"/events/{event_id}/reveals", json=item(2)).status_code == 201

    payload = [
        item(1),                                   # 0 合法新
        {"original_text": "随便", "salt": ""},      # 1 格式非法（空盐）
        item(3),                                   # 2 合法新
        item(1),                                   # 3 请求内重复
        item(2),                                   # 4 已揭示
        {"original_text": "幽灵预测", "salt": "s9"},  # 5 无匹配
        {"original_text": "长盐", "salt": "s" * 257},  # 6 格式非法（超长盐）
    ]
    r = batch_reveal(event_id, payload)
    assert r.status_code == 409
    conflicts = r.json()["detail"]["conflicts"]
    assert [(c["index"], c["type"]) for c in conflicts] == [
        (1, "INVALID_FORMAT"),
        (3, "DUPLICATE_IN_REQUEST"),
        (4, "DUPLICATE_REVEAL"),
        (5, "NO_MATCHING_COMMITMENT"),
        (6, "INVALID_FORMAT"),
    ]
    # INVALID_FORMAT 项不携带重算哈希；其余项携带
    assert "recomputed_hash" not in conflicts[0]
    assert conflicts[1]["recomputed_hash"] == h(1)
    assert conflicts[2]["recomputed_hash"] == h(2)
    assert conflicts[3]["recomputed_hash"] == compute_commitment("幽灵预测", "s9")
    # h1/h3 虽有效，但整批回滚不得落库；仅先前 h2 一条揭示
    assert client.get(f"/events/{event_id}").json()["reveal_count"] == 1

    # 修正全部冲突后可以整批成功
    r = batch_reveal(event_id, [item(1), item(3)])
    assert r.status_code == 201, r.text
    assert r.json()["count"] == 2


def test_batch_reveals_salt_boundaries(seeded_event):
    event_id = seeded_event
    clock.t = T0 + timedelta(hours=1, minutes=30)

    # 盐恰好 256 字符：格式合法（无匹配 -> NO_MATCHING_COMMITMENT，而非格式错误）
    r = batch_reveal(event_id, [{"original_text": "x", "salt": "s" * 256}])
    assert r.status_code == 409
    assert r.json()["detail"]["conflicts"][0]["type"] == "NO_MATCHING_COMMITMENT"

    # 空盐 / 超长盐：INVALID_FORMAT
    r = batch_reveal(event_id, [
        {"original_text": "a", "salt": ""},
        {"original_text": "b", "salt": "s" * 257},
    ])
    assert r.status_code == 409
    assert [(c["index"], c["type"]) for c in r.json()["detail"]["conflicts"]] == [
        (0, "INVALID_FORMAT"),
        (1, "INVALID_FORMAT"),
    ]
    assert client.get(f"/events/{event_id}").json()["reveal_count"] == 0


def test_batch_reveals_size_limits(seeded_event):
    event_id = seeded_event
    clock.t = T0 + timedelta(hours=1, minutes=30)

    r = batch_reveal(event_id, [])
    assert r.status_code == 422

    r = batch_reveal(event_id, [item(1)])  # 先腾一个？不——需要 101 个不同哈希
    assert r.status_code == 201
    r = batch_reveal(event_id, [
        {"original_text": f"bulk-{i}", "salt": f"bulk-salt-{i}"} for i in range(101)
    ])
    assert r.status_code == 422

    # 恰好 100 组（与已有 5 个承诺均不匹配）：全部无匹配 -> 409，逐项报全且回滚
    r = batch_reveal(event_id, [
        {"original_text": f"other-{i}", "salt": f"other-salt-{i}"} for i in range(100)
    ])
    assert r.status_code == 409
    conflicts = r.json()["detail"]["conflicts"]
    assert len(conflicts) == 100
    assert all(c["type"] == "NO_MATCHING_COMMITMENT" for c in conflicts)
    assert client.get(f"/events/{event_id}").json()["reveal_count"] == 1


def test_batch_reveals_wrong_body_shape_returns_422(seeded_event):
    event_id = seeded_event
    clock.t = T0 + timedelta(hours=1, minutes=30)

    r = client.post(f"/events/{event_id}/reveals/batch", json={"reveals": "not-a-list"})
    assert r.status_code == 422
    r = client.post(f"/events/{event_id}/reveals/batch", json={})
    assert r.status_code == 422
    # 字段缺失 / 字段类型错误：请求体 422（不是逐项 409）
    r = batch_reveal(event_id, [{"original_text": "x"}])
    assert r.status_code == 422
    r = batch_reveal(event_id, [{"original_text": "x", "salt": 123}])
    assert r.status_code == 422
    r = batch_reveal(event_id, ["not-an-object"])
    assert r.status_code == 422
    assert client.get(f"/events/{event_id}").json()["reveal_count"] == 0


def test_batch_reveals_before_commit_deadline(seeded_event):
    event_id = seeded_event
    # T0 处于 commit 阶段
    r = batch_reveal(event_id, [item(1)])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "REVEAL_PHASE_NOT_STARTED"
    assert client.get(f"/events/{event_id}").json()["reveal_count"] == 0


def test_batch_reveals_after_reveal_deadline(seeded_event):
    event_id = seeded_event
    clock.t = T0 + timedelta(hours=2)  # 到达揭示截止时刻（不含）-> closed
    r = batch_reveal(event_id, [item(1)])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "REVEAL_PHASE_CLOSED"
    assert client.get(f"/events/{event_id}").json()["reveal_count"] == 0


def test_batch_reveals_unknown_event_returns_404():
    missing = "00000000-0000-0000-0000-000000000000"
    r = batch_reveal(missing, [item(1)])
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "EVENT_NOT_FOUND"


def test_batch_reveals_share_storage_with_single_endpoint(seeded_event):
    event_id = seeded_event
    clock.t = T0 + timedelta(hours=1, minutes=30)

    # 批量端点揭示 h1/h2 后，单个端点再揭示 h1 -> DUPLICATE_REVEAL
    assert batch_reveal(event_id, [item(1), item(2)]).status_code == 201
    r = client.post(f"/events/{event_id}/reveals", json=item(1))
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "DUPLICATE_REVEAL"

    # 单个端点揭示 h3 后，批量端点可见并判冲突，整批回滚
    assert client.post(f"/events/{event_id}/reveals", json=item(3)).status_code == 201
    r = batch_reveal(event_id, [item(4), item(3)])
    assert r.status_code == 409
    conflicts = r.json()["detail"]["conflicts"]
    assert conflicts == [{
        "index": 1,
        "type": "DUPLICATE_REVEAL",
        "original_text": text(3),
        "salt": salt(3),
        "recomputed_hash": h(3),
    }]
    # h4 未随批写入
    assert client.get(f"/events/{event_id}").json()["reveal_count"] == 3
    commitments = client.get(f"/events/{event_id}/commitments").json()
    h4_row = next(c for c in commitments if c["commitment_hash"] == h(4))
    assert h4_row["revealed"] is False


def test_batch_reveals_concurrent_race_only_first_wins(seeded_event, monkeypatch):
    """预检通过后、本事务提交前，另一事务抢先揭示同一承诺：唯一约束兜底。

    在 Session.flush 上注入一次性「竞争者」——用独立会话直接插入揭示并
    提交，随后让原请求继续 flush，必然撞 reveals.commitment_id 唯一约束，
    端点须整体回滚并返回逐项 DUPLICATE_REVEAL 清单。
    """
    event_id = seeded_event
    clock.t = T0 + timedelta(hours=1, minutes=30)

    commitments = client.get(f"/events/{event_id}/commitments").json()
    id_by_hash = {c["commitment_hash"]: c["id"] for c in commitments}
    c1_id, c2_id = id_by_hash[h(1)], id_by_hash[h(2)]

    real_flush = main_module.Session.flush
    fired = {"done": False}

    def racing_flush(self, *args, **kwargs):
        if not fired["done"] and any(
            isinstance(obj, Reveal) for obj in getattr(self, "new", [])
        ):
            fired["done"] = True
            racer = SessionLocal()
            try:
                racer.add(Reveal(
                    id=uuid.uuid4(),
                    event_id=uuid.UUID(event_id),
                    commitment_id=uuid.UUID(c1_id),
                    original_text=text(1),
                    salt=salt(1),
                    recomputed_hash=h(1),
                ))
                racer.commit()
            finally:
                racer.close()
        return real_flush(self, *args, **kwargs)

    monkeypatch.setattr(main_module.Session, "flush", racing_flush)

    r = batch_reveal(event_id, [item(1), item(2)])
    assert fired["done"] is True
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "BATCH_REVEAL_CONFLICT"
    assert [(c["index"], c["type"]) for c in r.json()["detail"]["conflicts"]] == [
        (0, "DUPLICATE_REVEAL"),
    ]

    # 仅竞争者那一条揭示落库；落败批次的 h2 仍可随后成功揭示
    # （monkeypatch 在测试结束时自动撤销，这里不能提前 undo——
    #  同一 monkeypatch 实例还承载 autouse 的时钟补丁）
    detail = client.get(f"/events/{event_id}").json()
    assert detail["reveal_count"] == 1
    r = batch_reveal(event_id, [item(2)])
    assert r.status_code == 201, r.text
    assert r.json()["reveals"][0]["commitment_id"] == c2_id
    assert r.json()["reveals"][0]["commitment_id"] != c1_id
