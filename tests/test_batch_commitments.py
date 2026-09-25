"""POST /events/{event_id}/commitments/batch 原子批量提交测试。

SQLite 文件库 + 模拟时钟（与 test_api.py 相同手法），覆盖：
  - 按输入顺序批量创建并返回 count
  - 请求内重复 / 库中已有 / 格式非法 的逐项 409 冲突清单
  - 混合冲突一次报全，且整体回滚不留部分承诺
  - 数量边界（0、101）422
  - commit_deadline 关闭、事件不存在 404 仍生效
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_batch_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/batch.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.clock as clock_module  # noqa: E402
import app.main as main_module  # noqa: E402
from app.hashing import compute_commitment  # noqa: E402

T0 = datetime(2030, 3, 1, 0, 0, 0, tzinfo=timezone.utc)


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


def h(i: int) -> str:
    return compute_commitment(f"prediction-{i}", f"salt-{i}")


@pytest.fixture
def event_id() -> str:
    with client:
        r = client.post("/events", json={
            "title": "批量事件",
            "commit_deadline": (T0 + timedelta(hours=1)).isoformat(),
            "reveal_deadline": (T0 + timedelta(hours=2)).isoformat(),
        })
        assert r.status_code == 201, r.text
        return r.json()["id"]


def batch(event_id: str, hashes):
    return client.post(
        f"/events/{event_id}/commitments/batch",
        json={"commitment_hashes": hashes},
    )


def test_batch_creates_in_input_order(event_id):
    hashes = [h(1), h(2).upper(), h(3)]  # 第二个故意大写，存储须归一化
    r = batch(event_id, hashes)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["count"] == 3
    out = body["commitments"]
    assert [c["commitment_hash"] for c in out] == [h(1), h(2), h(3)]
    assert len({c["id"] for c in out}) == 3
    assert all(c["event_id"] == event_id for c in out)

    r = client.get(f"/events/{event_id}")
    assert r.json()["commitment_count"] == 3


def test_batch_single_item_ok(event_id):
    r = batch(event_id, [h(9)])
    assert r.status_code == 201
    assert r.json()["count"] == 1
    assert r.json()["commitments"][0]["commitment_hash"] == h(9)


def test_batch_duplicate_within_request_all_conflict_indices(event_id):
    hashes = [h(1), h(2), h(1), h(2), h(1)]
    r = batch(event_id, hashes)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "BATCH_COMMITMENT_CONFLICT"
    conflicts = detail["conflicts"]
    assert [(c["index"], c["type"]) for c in conflicts] == [
        (2, "DUPLICATE_IN_REQUEST"),
        (3, "DUPLICATE_IN_REQUEST"),
        (4, "DUPLICATE_IN_REQUEST"),
    ]
    assert conflicts[0]["commitment_hash"] == h(1)
    # 回滚：零写入
    assert client.get(f"/events/{event_id}").json()["commitment_count"] == 0


def test_batch_duplicate_in_database(event_id):
    assert batch(event_id, [h(1), h(2)]).status_code == 201

    r = batch(event_id, [h(3), h(1), h(2)])
    assert r.status_code == 409
    conflicts = r.json()["detail"]["conflicts"]
    assert [(c["index"], c["type"]) for c in conflicts] == [
        (1, "DUPLICATE_COMMITMENT"),
        (2, "DUPLICATE_COMMITMENT"),
    ]
    assert conflicts[0]["commitment_hash"] == h(1)
    # 未冲突的新哈希也不得写入
    assert client.get(f"/events/{event_id}").json()["commitment_count"] == 2


def test_batch_case_insensitive_duplicate(event_id):
    assert batch(event_id, [h(1)]).status_code == 201
    r = batch(event_id, [h(1).upper()])
    assert r.status_code == 409
    conflicts = r.json()["detail"]["conflicts"]
    assert conflicts == [{
        "index": 0,
        "type": "DUPLICATE_COMMITMENT",
        "commitment_hash": h(1).upper(),  # 保留原始大小写
    }]


def test_batch_invalid_format(event_id):
    r = batch(event_id, [h(1), "xyz", "g" * 64, h(2), h(2).upper()[:63]])
    assert r.status_code == 409
    conflicts = r.json()["detail"]["conflicts"]
    assert [(c["index"], c["type"]) for c in conflicts] == [
        (1, "INVALID_FORMAT"),
        (2, "INVALID_FORMAT"),
        (4, "INVALID_FORMAT"),
    ]
    assert conflicts[0]["commitment_hash"] == "xyz"
    assert conflicts[1]["commitment_hash"] == "g" * 64
    assert client.get(f"/events/{event_id}").json()["commitment_count"] == 0


def test_batch_mixed_conflicts_all_reported_and_rolled_back(event_id):
    # 库中先有 h2
    assert batch(event_id, [h(2)]).status_code == 201

    # 下标: 0 h1(合法新) 1 bad(格式非法) 2 h3(合法新)
    #       3 h1(请求内重复) 4 h2(库中已有) 5 bad2(格式非法)
    r = batch(event_id, [h(1), "not-hex", h(3), h(1), h(2), "Z" * 64])
    assert r.status_code == 409
    conflicts = r.json()["detail"]["conflicts"]
    assert [(c["index"], c["type"]) for c in conflicts] == [
        (1, "INVALID_FORMAT"),
        (3, "DUPLICATE_IN_REQUEST"),
        (4, "DUPLICATE_COMMITMENT"),
        (5, "INVALID_FORMAT"),
    ]
    # h1/h3 虽是新哈希，但整批回滚，不得落库；仅先前 h2 存在
    assert client.get(f"/events/{event_id}").json()["commitment_count"] == 1

    # 修正冲突项后可以整批成功
    r = batch(event_id, [h(1), h(3)])
    assert r.status_code == 201, r.text
    assert r.json()["count"] == 2


def test_batch_first_occurrence_reported_as_db_duplicate(event_id):
    assert batch(event_id, [h(1)]).status_code == 201
    # h1 在库中且请求内重复三次：首个下标记库冲突，其余记请求内重复
    r = batch(event_id, [h(1), h(1), h(1)])
    assert r.status_code == 409
    assert [(c["index"], c["type"]) for c in r.json()["detail"]["conflicts"]] == [
        (0, "DUPLICATE_COMMITMENT"),
        (1, "DUPLICATE_IN_REQUEST"),
        (2, "DUPLICATE_IN_REQUEST"),
    ]


def test_batch_size_limits(event_id):
    r = batch(event_id, [])
    assert r.status_code == 422

    r = batch(event_id, [h(i) for i in range(101)])
    assert r.status_code == 422

    # 恰好 100 个允许
    r = batch(event_id, [h(1000 + i) for i in range(100)])
    assert r.status_code == 201, r.text
    assert r.json()["count"] == 100


def test_batch_wrong_body_shape_returns_422(event_id):
    r = client.post(
        f"/events/{event_id}/commitments/batch",
        json={"commitment_hashes": "not-a-list"},
    )
    assert r.status_code == 422
    r = client.post(f"/events/{event_id}/commitments/batch", json={})
    assert r.status_code == 422
    assert client.get(f"/events/{event_id}").json()["commitment_count"] == 0


def test_batch_after_commit_deadline(event_id):
    clock.t = T0 + timedelta(hours=1)
    r = batch(event_id, [h(1)])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "COMMIT_PHASE_CLOSED"
    assert client.get(f"/events/{event_id}").json()["commitment_count"] == 0


def test_batch_unknown_event_returns_404():
    missing = "00000000-0000-0000-0000-000000000000"
    r = batch(missing, [h(1)])
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "EVENT_NOT_FOUND"


def test_batch_and_single_endpoint_share_storage(event_id):
    assert batch(event_id, [h(1), h(2)]).status_code == 201
    # 单个端点再提交批内已存在的哈希 -> 原有的单项重复语义
    r = client.post(
        f"/events/{event_id}/commitments",
        json={"commitment_hash": h(1)},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "DUPLICATE_COMMITMENT"

    # 批量端点也能看到单个端点的写入
    r = client.post(
        f"/events/{event_id}/commitments",
        json={"commitment_hash": h(7)},
    )
    assert r.status_code == 201
    r = batch(event_id, [h(7)])
    assert r.status_code == 409
    assert r.json()["detail"]["conflicts"][0]["type"] == "DUPLICATE_COMMITMENT"
