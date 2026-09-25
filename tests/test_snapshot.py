"""GET /events/{event_id}/snapshot?at=... 历史时刻事件视图重建测试。

SQLite 文件库 + 模拟时钟（与 test_extend.py 相同手法），覆盖：
  - 回放创建记录与按发生时间排序的截止时间延长：at 之前（含）的延长
    才生效，生效截止时间取最近一条已生效延长的新值
  - status 用回放后的截止时间判定 commit / reveal / closed
  - commitment_count / reveal_count 只统计 at 之前（含）已写入的记录
  - 边界含 at 本身；at == created_at 合法；at 早于创建时间 400
  - at 缺失 / 无法解析 / 无时区分别返回明确的 400；事件不存在 404
  - 非 UTC 时区偏移按同一时刻处理，snapshot_at 以 UTC 回显
  - 快照是纯只读视图：允许未来时刻，且与服务端当前时钟无关
"""

import os
import tempfile
from datetime import datetime, timedelta, timezone

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_snapshot_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/snapshot.db"

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
    # 补丁统一时钟：同时驱动阶段判定与所有记录的 created_at
    monkeypatch.setattr(clock_module, "now_utc", clock)
    with client:  # 触发 lifespan 建表（幂等）
        yield


def iso(dt: datetime) -> str:
    return dt.isoformat()


def dt(s: str) -> datetime:
    """解析响应中的时间字符串（兼容 Z 与 +00:00），按时刻比较。"""
    return datetime.fromisoformat(s)


def snapshot(event_id: str, at: datetime | str):
    raw = at if isinstance(at, str) else iso(at)
    return client.get(f"/events/{event_id}/snapshot", params={"at": raw})


def create_event(commit_dl: datetime = COMMIT_DL, reveal_dl: datetime = REVEAL_DL) -> str:
    r = client.post("/events", json={
        "title": "快照事件",
        "commit_deadline": iso(commit_dl),
        "reveal_deadline": iso(reveal_dl),
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def extend(event_id: str, commit_dl: datetime, reveal_dl: datetime, reason: str):
    r = client.post(f"/events/{event_id}/extend", json={
        "commit_deadline": iso(commit_dl),
        "reveal_deadline": iso(reveal_dl),
        "reason": reason,
    })
    assert r.status_code == 200, r.text
    return r.json()


def commit_hash(text: str, salt: str) -> str:
    return compute_commitment(text, salt)


def submit_commitment(event_id: str, h: str):
    r = client.post(f"/events/{event_id}/commitments", json={"commitment_hash": h})
    assert r.status_code == 201, r.text


def submit_reveal(event_id: str, text: str, salt: str):
    r = client.post(f"/events/{event_id}/reveals",
                    json={"original_text": text, "salt": salt})
    assert r.status_code == 201, r.text


@pytest.fixture
def replayed_event() -> str:
    """构造一条带两次延长、两个承诺与揭示（在揭示期）的完整历史：

    T0+0min    事件创建（commit=T0+1h, reveal=T0+2h）
    T0+5min    承诺 c1 写入
    T0+10min   延长 #1：commit=T0+3h, reveal=T0+4h
    T0+15min   承诺 c2 写入
    T0+20min   延长 #2：commit=T0+5h, reveal=T0+6h
    T0+5h10min 揭示 c1
    T0+5h20min 揭示 c2
    """
    event_id = create_event()
    h1 = commit_hash("预测一", "salt-1")
    h2 = commit_hash("预测二", "salt-2")

    clock.t = T0 + timedelta(minutes=5)
    submit_commitment(event_id, h1)

    clock.t = T0 + timedelta(minutes=10)
    extend(event_id, T0 + timedelta(hours=3), T0 + timedelta(hours=4), "第一次延长")

    clock.t = T0 + timedelta(minutes=15)
    submit_commitment(event_id, h2)

    clock.t = T0 + timedelta(minutes=20)
    extend(event_id, T0 + timedelta(hours=5), T0 + timedelta(hours=6), "第二次延长")

    clock.t = T0 + timedelta(hours=5, minutes=10)
    submit_reveal(event_id, "预测一", "salt-1")

    clock.t = T0 + timedelta(hours=5, minutes=20)
    submit_reveal(event_id, "预测二", "salt-2")

    return event_id


# --------------------------------------------------------------------------- #
# 截止时间回放
# --------------------------------------------------------------------------- #

def test_snapshot_at_creation_returns_original_view(replayed_event):
    r = snapshot(replayed_event, T0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_id"] == replayed_event
    assert body["title"] == "快照事件"
    assert dt(body["created_at"]) == T0
    assert dt(body["snapshot_at"]) == T0
    # 尚无任何延长生效：创建时的原始截止时间
    assert dt(body["commit_deadline"]) == COMMIT_DL
    assert dt(body["reveal_deadline"]) == REVEAL_DL
    assert body["status"] == "commit"
    assert body["commitment_count"] == 0
    assert body["reveal_count"] == 0


def test_snapshot_before_first_extension_keeps_original_deadlines(replayed_event):
    # 延长 #1 发生在 +10min：+9min59s 时原始截止时间仍生效，c1 已写入
    at = T0 + timedelta(minutes=9, seconds=59)
    body = snapshot(replayed_event, at).json()
    assert dt(body["commit_deadline"]) == COMMIT_DL
    assert dt(body["reveal_deadline"]) == REVEAL_DL
    assert body["status"] == "commit"
    assert body["commitment_count"] == 1
    assert body["reveal_count"] == 0


def test_snapshot_after_first_extension_uses_new_deadlines(replayed_event):
    # 恰好落在延长 #1 的发生时刻（边界含 at）：新截止时间生效
    at = T0 + timedelta(minutes=10)
    body = snapshot(replayed_event, at).json()
    assert dt(body["commit_deadline"]) == T0 + timedelta(hours=3)
    assert dt(body["reveal_deadline"]) == T0 + timedelta(hours=4)
    assert body["status"] == "commit"
    assert body["commitment_count"] == 1  # c2 在 +15min 才写入


def test_snapshot_between_two_extensions(replayed_event):
    at = T0 + timedelta(minutes=15)  # c2 恰在此时写入；延长 #2 尚未发生
    body = snapshot(replayed_event, at).json()
    assert dt(body["commit_deadline"]) == T0 + timedelta(hours=3)
    assert dt(body["reveal_deadline"]) == T0 + timedelta(hours=4)
    assert body["commitment_count"] == 2
    assert body["reveal_count"] == 0


def test_snapshot_after_second_extension_uses_latest_deadlines(replayed_event):
    at = T0 + timedelta(minutes=20)
    body = snapshot(replayed_event, at).json()
    assert dt(body["commit_deadline"]) == T0 + timedelta(hours=5)
    assert dt(body["reveal_deadline"]) == T0 + timedelta(hours=6)
    assert body["status"] == "commit"
    assert body["commitment_count"] == 2
    assert body["reveal_count"] == 0


def test_snapshot_replay_changes_phase_vs_original_windows(replayed_event):
    # 按原始窗口 T0+3h30 已是 reveal，但两次延长都已生效 -> 当时仍在 commit
    at = T0 + timedelta(hours=3, minutes=30)
    body = snapshot(replayed_event, at).json()
    assert dt(body["commit_deadline"]) == T0 + timedelta(hours=5)
    assert dt(body["reveal_deadline"]) == T0 + timedelta(hours=6)
    assert body["status"] == "commit"


# --------------------------------------------------------------------------- #
# 状态与计数
# --------------------------------------------------------------------------- #

def test_snapshot_reveal_phase_status(replayed_event):
    at = T0 + timedelta(hours=5, minutes=10)
    body = snapshot(replayed_event, at).json()
    assert body["status"] == "reveal"
    assert body["commitment_count"] == 2
    assert body["reveal_count"] == 1  # c2 的揭示在 +5h20 才写入


def test_snapshot_between_reveals_counts_only_first(replayed_event):
    at = T0 + timedelta(hours=5, minutes=15)
    body = snapshot(replayed_event, at).json()
    assert body["status"] == "reveal"
    assert body["reveal_count"] == 1


def test_snapshot_closed_phase(replayed_event):
    at = T0 + timedelta(hours=6)  # 恰好到达 reveal 截止：半开边界 -> closed
    body = snapshot(replayed_event, at).json()
    assert body["status"] == "closed"
    assert body["commitment_count"] == 2
    assert body["reveal_count"] == 2


def test_snapshot_plain_event_phase_progression():
    # 无延长事件：快照阶段随原始截止时间推进 commit -> reveal -> closed
    event_id = create_event()
    clock.t = T0 + timedelta(minutes=30)
    submit_commitment(event_id, commit_hash("x", "y"))

    body = snapshot(event_id, T0 + timedelta(minutes=30)).json()
    assert body["status"] == "commit"
    assert body["commitment_count"] == 1
    assert dt(body["commit_deadline"]) == COMMIT_DL

    body = snapshot(event_id, T0 + timedelta(hours=1)).json()
    assert body["status"] == "reveal"

    body = snapshot(event_id, T0 + timedelta(hours=1, minutes=30)).json()
    assert body["status"] == "reveal"

    body = snapshot(event_id, T0 + timedelta(hours=2)).json()
    assert body["status"] == "closed"


def test_snapshot_future_time_returns_latest_known_state(replayed_event):
    # 允许未来时刻：返回当前已知的最新截止时间与全部计数，状态按未来判定
    at = T0 + timedelta(days=100)
    body = snapshot(replayed_event, at).json()
    assert dt(body["snapshot_at"]) == at
    assert dt(body["commit_deadline"]) == T0 + timedelta(hours=5)
    assert dt(body["reveal_deadline"]) == T0 + timedelta(hours=6)
    assert body["status"] == "closed"
    assert body["commitment_count"] == 2
    assert body["reveal_count"] == 2


def test_snapshot_independent_of_server_clock(replayed_event):
    # 服务端时钟停在 closed 之后，查询的仍是指定历史时刻的视图
    clock.t = T0 + timedelta(days=100)
    at = T0 + timedelta(minutes=10)
    body = snapshot(replayed_event, at).json()
    assert body["status"] == "commit"
    assert dt(body["commit_deadline"]) == T0 + timedelta(hours=3)
    assert body["commitment_count"] == 1
    assert body["reveal_count"] == 0


def test_snapshot_is_read_only(replayed_event):
    before = client.get(f"/events/{replayed_event}").json()
    r = snapshot(replayed_event, T0 + timedelta(minutes=10))
    assert r.status_code == 200
    after = client.get(f"/events/{replayed_event}").json()
    assert before == after


# --------------------------------------------------------------------------- #
# 时区处理
# --------------------------------------------------------------------------- #

def test_snapshot_accepts_non_utc_offset_and_normalizes(replayed_event):
    plus8 = timezone(timedelta(hours=8))
    # T0+10min 的 +08:00 表示：08:10+08:00 == 00:10Z
    at_local = (T0 + timedelta(minutes=10)).astimezone(plus8)
    r = snapshot(replayed_event, at_local.isoformat())
    assert r.status_code == 200, r.text
    body = r.json()
    assert dt(body["snapshot_at"]) == T0 + timedelta(minutes=10)
    assert dt(body["commit_deadline"]) == T0 + timedelta(hours=3)
    assert body["commitment_count"] == 1


def test_snapshot_accepts_zulu_suffix(replayed_event):
    r = snapshot(replayed_event, "2030-06-01T00:20:00Z")
    assert r.status_code == 200, r.text
    body = r.json()
    assert dt(body["snapshot_at"]) == T0 + timedelta(minutes=20)
    assert dt(body["commit_deadline"]) == T0 + timedelta(hours=5)


# --------------------------------------------------------------------------- #
# 错误
# --------------------------------------------------------------------------- #

def test_snapshot_before_creation_returns_400(replayed_event):
    r = snapshot(replayed_event, T0 - timedelta(seconds=1))
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "SNAPSHOT_BEFORE_CREATION"


def test_snapshot_missing_at_returns_400(replayed_event):
    r = client.get(f"/events/{replayed_event}/snapshot")
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "MISSING_SNAPSHOT_TIME"


def test_snapshot_blank_at_returns_400(replayed_event):
    r = client.get(f"/events/{replayed_event}/snapshot", params={"at": "   "})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_SNAPSHOT_TIME"


def test_snapshot_unparseable_at_returns_400(replayed_event):
    for raw in ["not-a-time", "2030-13-40T99:99:99Z", "hello-world"]:
        r = snapshot(replayed_event, raw)
        assert r.status_code == 400, raw
        assert r.json()["detail"]["code"] == "INVALID_SNAPSHOT_TIME", raw


def test_snapshot_naive_at_returns_400(replayed_event):
    r = snapshot(replayed_event, "2030-06-01T00:10:00")  # 无时区
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "NAIVE_SNAPSHOT_TIME"


def test_snapshot_unknown_event_returns_404():
    missing = "00000000-0000-0000-0000-000000000000"
    r = snapshot(missing, T0 + timedelta(hours=1))
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "EVENT_NOT_FOUND"


def test_snapshot_non_uuid_returns_422():
    r = client.get("/events/not-a-uuid/snapshot", params={"at": iso(T0)})
    assert r.status_code == 422
