"""结算模块测试：POST/GET /events/{event_id}/settlement。

SQLite 文件库 + 模拟时钟，覆盖：
  - 未结算查询返回 200 + status=unsettled（计数字段为 null）
  - commit / reveal 阶段结算返回 409 SETTLEMENT_NOT_IN_CLOSED_PHASE
  - closed 阶段结算：正确 / 错误 / 未揭示按揭示原文精确匹配统计
  - 精确匹配区分大小写与空白，不做任何归一化
  - 重复结算（相同或不同标准结果）返回 409 SETTLEMENT_ALREADY_CONFIRMED，
    detail.existing 回显首次结果，且既有结果不被改写
  - 空 / 缺失 standard_result 返回 422
  - 未知事件 404；结算记录出现在时间线中
  - 零承诺事件可结算（三个计数均为 0）
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_settlement_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/settlement.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.clock as clock_module  # noqa: E402
import app.main as main_module  # noqa: E402
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
        "title": "待结算事件",
        "commit_deadline": COMMIT_DL.isoformat(),
        "reveal_deadline": REVEAL_DL.isoformat(),
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def commit(event_id: str, text: str, salt: str) -> str:
    h = compute_commitment(text, salt)
    r = client.post(f"/events/{event_id}/commitments", json={"commitment_hash": h})
    assert r.status_code == 201, r.text
    return h


def reveal(event_id: str, text: str, salt: str) -> None:
    r = client.post(f"/events/{event_id}/reveals",
                    json={"original_text": text, "salt": salt})
    assert r.status_code == 201, r.text


def settle(event_id: str, standard_result: str, expected: int = 200) -> dict:
    r = client.post(f"/events/{event_id}/settlement",
                    json={"standard_result": standard_result})
    assert r.status_code == expected, r.text
    return r.json()


def test_unsettled_query_is_explicit():
    event_id = create_event()
    r = client.get(f"/events/{event_id}/settlement")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "event_id": event_id,
        "status": "unsettled",
        "standard_result": None,
        "correct_count": None,
        "incorrect_count": None,
        "unrevealed_count": None,
        "settled_at": None,
    }


def test_settlement_rejected_before_closed():
    event_id = create_event()
    # commit 阶段
    r = client.post(f"/events/{event_id}/settlement",
                    json={"standard_result": "x"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SETTLEMENT_NOT_IN_CLOSED_PHASE"

    # reveal 阶段同样拒绝
    clock.t = COMMIT_DL
    r = client.post(f"/events/{event_id}/settlement",
                    json={"standard_result": "x"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SETTLEMENT_NOT_IN_CLOSED_PHASE"

    # 拒绝后仍为未结算，查询状态明确
    r = client.get(f"/events/{event_id}/settlement")
    assert r.status_code == 200 and r.json()["status"] == "unsettled"


def test_settlement_tallies_exact_match():
    event_id = create_event()
    text_a, salt_a = "明天下雨", "salt-a"
    text_b, salt_b = "明天 下雨", "salt-b"   # 原文多一个空格
    text_c, salt_c = "明年晴天", "salt-c"   # 只提交承诺，不揭示
    commit(event_id, text_a, salt_a)
    commit(event_id, text_b, salt_b)
    commit(event_id, text_c, salt_c)

    clock.t = COMMIT_DL
    reveal(event_id, text_a, salt_a)
    reveal(event_id, text_b, salt_b)

    clock.t = REVEAL_DL
    body = settle(event_id, "明天下雨")
    assert body["status"] == "settled"
    assert body["standard_result"] == "明天下雨"
    # text_a 精确相等 -> 正确；带空格的 text_b 不做空白归一化 -> 错误；
    # text_c 从未揭示 -> 未揭示
    assert body["correct_count"] == 1
    assert body["incorrect_count"] == 1
    assert body["unrevealed_count"] == 1
    assert body["settled_at"] is not None

    # 查询接口返回同一永久结果
    r = client.get(f"/events/{event_id}/settlement")
    assert r.status_code == 200
    assert r.json() == body


def test_settlement_exact_match_is_case_sensitive():
    event_id = create_event()
    commit(event_id, "YES", "s")
    clock.t = COMMIT_DL
    reveal(event_id, "YES", "s")
    clock.t = REVEAL_DL

    body = settle(event_id, "yes")
    assert body["correct_count"] == 0
    assert body["incorrect_count"] == 1
    assert body["unrevealed_count"] == 0


def test_duplicate_settlement_conflicts_and_never_overwrites():
    event_id = create_event()
    commit(event_id, "A", "s")
    clock.t = COMMIT_DL
    reveal(event_id, "A", "s")
    clock.t = REVEAL_DL

    first = settle(event_id, "A")
    assert first["correct_count"] == 1

    # 相同标准结果重复结算 -> 409
    r = client.post(f"/events/{event_id}/settlement",
                    json={"standard_result": "A"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "SETTLEMENT_ALREADY_CONFIRMED"
    assert detail["conflicts"][0]["existing"]["standard_result"] == "A"
    assert detail["conflicts"][0]["existing"]["correct_count"] == 1

    # 不同标准结果同样 409，既有结果不得改写
    r = client.post(f"/events/{event_id}/settlement",
                    json={"standard_result": "B"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SETTLEMENT_ALREADY_CONFIRMED"

    r = client.get(f"/events/{event_id}/settlement")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "settled"
    assert body["standard_result"] == "A"
    assert body["correct_count"] == 1
    assert body["incorrect_count"] == 0
    assert body["settled_at"] == first["settled_at"]


def test_empty_standard_result_is_422():
    event_id = create_event()
    clock.t = REVEAL_DL
    r = client.post(f"/events/{event_id}/settlement",
                    json={"standard_result": ""})
    assert r.status_code == 422, r.text
    r = client.post(f"/events/{event_id}/settlement", json={})
    assert r.status_code == 422, r.text
    # 校验失败不留结算
    r = client.get(f"/events/{event_id}/settlement")
    assert r.json()["status"] == "unsettled"


def test_settlement_404_for_missing_event():
    missing = "00000000-0000-0000-0000-000000000000"
    r = client.post(f"/events/{missing}/settlement", json={"standard_result": "x"})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "EVENT_NOT_FOUND"
    r = client.get(f"/events/{missing}/settlement")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "EVENT_NOT_FOUND"


def test_settlement_appears_in_timeline():
    event_id = create_event()
    commit(event_id, "A", "s")
    clock.t = COMMIT_DL
    reveal(event_id, "A", "s")
    clock.t = REVEAL_DL
    settle(event_id, "A")

    r = client.get(f"/events/{event_id}/timeline")
    assert r.status_code == 200, r.text
    entries = r.json()["entries"]
    last = entries[-1]
    assert last["type"] == "settlement_confirmed"
    assert last["detail"] == {
        "standard_result": "A",
        "correct_count": 1,
        "incorrect_count": 0,
        "unrevealed_count": 0,
    }


def test_settlement_with_zero_commitments():
    event_id = create_event()
    clock.t = REVEAL_DL
    body = settle(event_id, "无人参与的结果")
    assert body["correct_count"] == 0
    assert body["incorrect_count"] == 0
    assert body["unrevealed_count"] == 0
