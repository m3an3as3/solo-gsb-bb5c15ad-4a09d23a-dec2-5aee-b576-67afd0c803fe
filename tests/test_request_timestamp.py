"""准入判定与 created_at 必须共用同一 UTC 时刻的回归测试。

背景：路由原先在入口读取一次时钟做阶段判定，模型 created_at 的默认值在
flush 时再读一次。若两次读数跨过截止点，会出现：
  - 接受「创建时刻已晚于提交截止」的事件；
  - 获准提交 / 揭示的记录被记到下一阶段，快照与时间线自相矛盾。

这里的 StraddleClock 对被测试的请求返回一个预设序列：第 1 次读数在截止点
之前（准入应通过），第 2 次读数在截止点之后（修复前会被模型默认值当作
created_at）。修复后路由只读取一次并把该时刻显式写入记录，因此：
  - 响应中的 created_at / revealed_at 与准入时刻完全相同；
  - 在截止点取快照时该记录已计入（created_at <= at）；
  - 批量各项时间戳相同，且冲突仍整体回滚；
  - 被拒请求不落库。
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_request_ts_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/request_ts.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.clock as clock_module  # noqa: E402
import app.main as main_module  # noqa: E402
from app.hashing import compute_commitment  # noqa: E402

T0 = datetime(2030, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
COMMIT_DL = T0 + timedelta(hours=1)
REVEAL_DL = T0 + timedelta(hours=2)
EPS = timedelta(microseconds=1)


class StraddleClock:
    """可武装的时钟：未武装时恒返回 fixed；武装后按顺序返回读数，
    序列耗尽后钳制在最后一个读数，并统计调用次数。"""

    def __init__(self, fixed: datetime):
        self.fixed = fixed
        self.sequence: list[datetime] | None = None
        self.calls = 0

    def set_fixed(self, t: datetime) -> None:
        self.sequence = None
        self.fixed = t

    def arm(self, readings: list[datetime]) -> None:
        self.calls = 0
        self.sequence = list(readings)

    def __call__(self) -> datetime:
        if self.sequence is None:
            return self.fixed
        t = self.sequence[min(self.calls, len(self.sequence) - 1)]
        self.calls += 1
        return t


clock = StraddleClock(T0)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    clock.set_fixed(T0)
    monkeypatch.setattr(clock_module, "now_utc", clock)
    with client:  # 触发 lifespan 建表（幂等）
        yield


client = TestClient(main_module.app)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def create_event() -> str:
    r = client.post("/events", json={
        "title": "跨点事件",
        "commit_deadline": iso(COMMIT_DL),
        "reveal_deadline": iso(REVEAL_DL),
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_create_event_uses_single_instant_across_commit_deadline():
    # 第 1 次读数（准入）在截止前 1us，第 2 次读数（修复前 created_at
    # 默认值）在截止后 1us：修复后只允许第 1 次读数生效
    before, after = COMMIT_DL - EPS, COMMIT_DL + EPS
    clock.arm([before, after])

    r = client.post("/events", json={
        "title": "跨点创建",
        "commit_deadline": iso(COMMIT_DL),
        "reveal_deadline": iso(REVEAL_DL),
    })
    assert r.status_code == 201, r.text
    body = r.json()
    # created_at 必须是准入时刻，而不是模型默认值读出的截止后时刻
    assert dt(body["created_at"]) == before
    event_id = body["id"]

    # 在准入时刻（= created_at，快照边界含 at）取快照必须存在；
    # 修复前 created_at 落在截止后，这里会 400 SNAPSHOT_BEFORE_CREATION
    r = client.get(f"/events/{event_id}/snapshot", params={"at": iso(before)})
    assert r.status_code == 200, r.text
    snap = r.json()
    assert dt(snap["created_at"]) == before
    assert snap["status"] == "commit"


def test_event_created_after_commit_deadline_is_rejected_and_not_persisted():
    # 准入时刻本身已晚于提交截止：400，且事件不落库
    clock.set_fixed(COMMIT_DL)
    r = client.post("/events", json={
        "title": "迟到事件",
        "commit_deadline": iso(COMMIT_DL),
        "reveal_deadline": iso(REVEAL_DL),
    })
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_DEADLINES"

    clock.set_fixed(T0)
    existing = client.get("/events").json()
    assert all(e["title"] != "迟到事件" for e in existing)


def test_single_commitment_recorded_at_admission_instant():
    event_id = create_event()
    h = compute_commitment("prediction-1", "salt-1")
    before, after = COMMIT_DL - EPS, COMMIT_DL + EPS
    clock.arm([before, after])

    r = client.post(
        f"/events/{event_id}/commitments", json={"commitment_hash": h}
    )
    assert r.status_code == 201, r.text
    assert dt(r.json()["created_at"]) == before

    # 截止点快照（at=commit_dl，边界含 at）必须已计入该承诺；
    # 修复前记录时间为截止后，计数为 0 且该承诺看似属于 reveal 阶段
    snap = client.get(
        f"/events/{event_id}/snapshot", params={"at": iso(COMMIT_DL)}
    ).json()
    assert snap["status"] == "reveal"
    assert snap["commitment_count"] == 1


def test_single_commitment_at_exact_deadline_rejected_without_write():
    event_id = create_event()
    clock.set_fixed(COMMIT_DL)
    r = client.post(
        f"/events/{event_id}/commitments",
        json={"commitment_hash": compute_commitment("x", "y")},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "COMMIT_PHASE_CLOSED"
    assert client.get(f"/events/{event_id}").json()["commitment_count"] == 0


def test_batch_commitments_share_single_admission_instant():
    event_id = create_event()
    hashes = [
        compute_commitment("prediction-1", "salt-1"),
        compute_commitment("prediction-2", "salt-2"),
    ]
    before, after = COMMIT_DL - EPS, COMMIT_DL + EPS
    # 修复前：1 次准入读数 + 每条记录 flush 时各读一次（共 3 次）
    clock.arm([before, after])

    r = client.post(
        f"/events/{event_id}/commitments/batch",
        json={"commitment_hashes": hashes},
    )
    assert r.status_code == 201, r.text
    out = r.json()["commitments"]
    # 整批共用准入时刻
    assert {dt(c["created_at"]) for c in out} == {before}

    snap = client.get(
        f"/events/{event_id}/snapshot", params={"at": iso(COMMIT_DL)}
    ).json()
    assert snap["commitment_count"] == 2

    # 截止点之后整批被拒，不落库任何一条
    clock.set_fixed(COMMIT_DL)
    r = client.post(
        f"/events/{event_id}/commitments/batch",
        json={"commitment_hashes": [compute_commitment("late", "late")]},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "COMMIT_PHASE_CLOSED"
    assert client.get(f"/events/{event_id}").json()["commitment_count"] == 2


def test_single_reveal_recorded_at_admission_instant():
    event_id = create_event()
    text, salt = "明天下雨", "salt-1"
    h = compute_commitment(text, salt)
    assert client.post(
        f"/events/{event_id}/commitments", json={"commitment_hash": h}
    ).status_code == 201

    # 准入读数在揭示截止前 1us（reveal 阶段内），第 2 次读数已 closed
    before, after = REVEAL_DL - EPS, REVEAL_DL + EPS
    clock.arm([before, after])

    r = client.post(
        f"/events/{event_id}/reveals",
        json={"original_text": text, "salt": salt},
    )
    assert r.status_code == 201, r.text
    assert dt(r.json()["revealed_at"]) == before

    # 截止点快照必须已计入该揭示；修复前记录时间落入 closed，计数为 0
    snap = client.get(
        f"/events/{event_id}/snapshot", params={"at": iso(REVEAL_DL)}
    ).json()
    assert snap["status"] == "closed"
    assert snap["commitment_count"] == 1
    assert snap["reveal_count"] == 1


def test_single_reveal_at_exact_reveal_deadline_rejected_without_write():
    event_id = create_event()
    text, salt = "明天下雨", "salt-1"
    h = compute_commitment(text, salt)
    assert client.post(
        f"/events/{event_id}/commitments", json={"commitment_hash": h}
    ).status_code == 201

    clock.set_fixed(REVEAL_DL)
    r = client.post(
        f"/events/{event_id}/reveals",
        json={"original_text": text, "salt": salt},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "REVEAL_PHASE_CLOSED"
    assert client.get(f"/events/{event_id}").json()["reveal_count"] == 0


def test_batch_reveals_share_single_admission_instant():
    event_id = create_event()
    items = [
        ("明天下雨", "salt-1"),
        ("明天晴天", "salt-2"),
    ]
    hashes = [compute_commitment(t, s) for t, s in items]
    assert client.post(
        f"/events/{event_id}/commitments/batch",
        json={"commitment_hashes": hashes},
    ).status_code == 201

    before, after = REVEAL_DL - EPS, REVEAL_DL + EPS
    # 修复前：1 次准入读数 + 每条揭示各读一次（共 3 次）
    clock.arm([before, after])

    r = client.post(
        f"/events/{event_id}/reveals/batch",
        json={"reveals": [
            {"original_text": t, "salt": s} for t, s in items
        ]},
    )
    assert r.status_code == 201, r.text
    out = r.json()["reveals"]
    assert {dt(x["revealed_at"]) for x in out} == {before}

    snap = client.get(
        f"/events/{event_id}/snapshot", params={"at": iso(REVEAL_DL)}
    ).json()
    assert snap["commitment_count"] == 2
    assert snap["reveal_count"] == 2

    # 截止点之后整批被拒，整体回滚不落库任何一条
    clock.set_fixed(REVEAL_DL)
    r = client.post(
        f"/events/{event_id}/reveals/batch",
        json={"reveals": [
            {"original_text": t, "salt": s} for t, s in items
        ]},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "REVEAL_PHASE_CLOSED"
    assert client.get(f"/events/{event_id}").json()["reveal_count"] == 2
