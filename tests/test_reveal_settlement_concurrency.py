"""揭示（单条 / 批量）与永久结算在 reveal 截止边界上的并发串行化回归测试。

背景缺陷：单条 / 批量揭示原先用普通 SELECT 读事件（无行锁），并在读到
截止前时钟、通过阶段检查后才去匹配承诺 / 准备写入；截止后到达的结算
请求虽然对事件行加了 ``SELECT ... FOR UPDATE``，却不会与揭示事务互斥，
于是可能：

  1. 揭示在截止前通过阶段检查，尚未提交（挂起在提交前）；
  2. 截止后到达的结算先完成：统计时该承诺仍被计为「未揭示」并固化计数；
  3. 揭示随后成功提交落库——固化计数与存库揭示自相矛盾。

修复后两种揭示入口与结算共用同一把事件行锁、且都在**取得锁之后**才读
唯一一次服务端 UTC：

  - 揭示先持锁：它在揭示期内获准、提交后才放锁，随后取得锁的结算在同一
    事务内统计得到这条揭示（计数与存库一致）；
  - 结算先持锁：截止后完成固化，随后取得锁的揭示读到的唯一次时钟必已
    closed，按 REVEAL_PHASE_CLOSED 拒绝、回滚且不落库。

SQLite 不支持行锁，这里用一个 ``EventLockEmulator`` 猴子补丁替换路由层
共享的 ``lock_event_for_update`` 帮助函数，精确模拟 PostgreSQL 行锁的
「持锁 / 等待 / 提交或回滚释放」语义；并在 Session.commit / rollback
上补丁以在真实提交 / 回滚完成时释放锁（因此后取得锁的事务必然读到前一
事务已提交的数据）。两个真实请求通过 TestClient 在线程中并发执行，
阶段门控由可推进的模拟时钟控制，交错顺序完全确定。
"""

import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_reveal_settle_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/reveal_settle.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.clock as clock_module  # noqa: E402
import app.main as main_module  # noqa: E402
from app.hashing import compute_commitment  # noqa: E402
from app.models import Reveal  # noqa: E402

T0 = datetime(2030, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
COMMIT_DL = T0 + timedelta(hours=1)
REVEAL_DL = T0 + timedelta(hours=2)
PRE = REVEAL_DL - timedelta(seconds=1)  # 截止前 1 秒：仍在 reveal 阶段
LOCK_WAIT_TIMEOUT = 30


class MutableClock:
    def __init__(self, start: datetime):
        self.t = start
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self.t

    def set(self, t: datetime) -> None:
        with self._lock:
            self.t = t


clock = MutableClock(T0)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    clock.set(T0)
    monkeypatch.setattr(clock_module, "now_utc", clock)
    with client:  # 触发 lifespan 建表（幂等）
        yield


client = TestClient(main_module.app)


class EventLockEmulator:
    """模拟 PostgreSQL 事件行锁：同事件的 lock_event_for_update 互斥。

    - 每个数据库会话（Session 身份）取得事件锁后，其它会话的同事件加锁
      请求阻塞等待，直到持锁会话的 commit / rollback 完成（补丁在真实
      提交 / 回滚后才释放，保证等待方读到已提交数据）；
    - ``role_for_next`` 标记下一次成功加锁请求的角色，供测试门控识别；
    - ``on_acquire`` 在持锁状态下、返回路由代码前执行（可推进时钟并
      阻塞，制造确定性交错）；
    - ``gate_commit_roles`` + ``should_gate_commit`` 让指定角色在真正
      commit 前挂起（模拟「已通过阶段检查、尚未提交」的窗口）。
    """

    def __init__(self, monkeypatch):
        self.real_lock = main_module.lock_event_for_update
        self._cond = threading.Condition(threading.Lock())
        self._holders: dict[object, int] = {}       # event_id -> session id
        self._waiters: dict[object, set[int]] = {}  # event_id -> session ids
        self._session_roles: dict[int, str | None] = {}

        self.role_for_next: str | None = None
        self.on_acquire = None
        self.gate_commit_roles: set[str] = set()
        self.should_gate_commit = lambda db: False  # noqa: E731
        self._gate_fired_roles: set[str] = set()
        self.commit_reached = threading.Event()
        self.allow_commit = threading.Event()  # 初始为「不放行」，由测试显式 .set()

        monkeypatch.setattr(main_module, "lock_event_for_update", self.acquire)
        real_commit = main_module.Session.commit
        real_rollback = main_module.Session.rollback

        def patched_commit(db, *args, **kwargs):
            sid = id(db)
            role = self._session_roles.get(sid)
            if (
                role in self.gate_commit_roles
                and role not in self._gate_fired_roles
                and self.should_gate_commit(db)
            ):
                self._gate_fired_roles.add(role)
                self.commit_reached.set()
                assert self.allow_commit.wait(timeout=LOCK_WAIT_TIMEOUT), (
                    "超时等待测试放行提交"
                )
            try:
                return real_commit(db, *args, **kwargs)
            finally:
                self.release(db)

        def patched_rollback(db, *args, **kwargs):
            try:
                return real_rollback(db, *args, **kwargs)
            finally:
                self.release(db)

        monkeypatch.setattr(main_module.Session, "commit", patched_commit)
        monkeypatch.setattr(main_module.Session, "rollback", patched_rollback)

    def acquire(self, db, event_id):
        sid = id(db)
        deadline = time.monotonic() + LOCK_WAIT_TIMEOUT
        with self._cond:
            registered_wait = False
            while self._holders.get(event_id) not in (None, sid):
                if not registered_wait:
                    self._waiters.setdefault(event_id, set()).add(sid)
                    registered_wait = True
                self._cond.wait(timeout=0.05)
                assert time.monotonic() < deadline, "等待事件行锁超时（疑似死锁）"
            if registered_wait:
                self._waiters[event_id].discard(sid)
            self._holders[event_id] = sid
            self._session_roles[sid] = self.role_for_next
            role = self.role_for_next
        # 行锁已取得：执行测试回调（可在持锁状态下推进时钟并阻塞），
        # 随后路由代码才开始它那唯一一次服务端 UTC 读数
        event = self.real_lock(db, event_id)
        if self.on_acquire is not None:
            self.on_acquire(role)
        return event

    def release(self, db) -> None:
        sid = id(db)
        with self._cond:
            event_id = next((e for e, s in self._holders.items() if s == sid), None)
            if event_id is not None:
                del self._holders[event_id]
                self._cond.notify_all()

    def waiter_count(self, event_id) -> int:
        with self._cond:
            return len(self._waiters.get(UUID(str(event_id)), ()))

    def holder_role(self, event_id) -> str | None:
        with self._cond:
            sid = self._holders.get(UUID(str(event_id)))
            return self._session_roles.get(sid) if sid is not None else None

    def wait_until(self, predicate, description: str) -> None:
        deadline = time.monotonic() + LOCK_WAIT_TIMEOUT
        while not predicate():
            assert time.monotonic() < deadline, f"超时等待：{description}"
            time.sleep(0.005)


def wait_event(event: threading.Event, description: str) -> None:
    assert event.wait(timeout=LOCK_WAIT_TIMEOUT), f"超时等待：{description}"


def run_in_thread(target) -> tuple[threading.Thread, dict]:
    result: dict = {}

    def runner():
        try:
            result["response"] = target()
        except Exception as exc:  # pragma: no cover - 让 join 后断言暴露问题
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return thread, result


def seed_event(num_commitments: int = 1) -> tuple[str, list[tuple[str, str]]]:
    """创建事件并提交 num_commitments 个承诺；时钟停在 commit 阶段。返回事件 ID 与 (原文, 盐) 列表。"""
    r = client.post("/events", json={
        "title": "截止边界并发事件",
        "commit_deadline": COMMIT_DL.isoformat(),
        "reveal_deadline": REVEAL_DL.isoformat(),
    })
    assert r.status_code == 201, r.text
    event_id = r.json()["id"]

    pairs = [(f"prediction-{i}", f"salt-{i}") for i in range(1, num_commitments + 1)]
    hashes = [compute_commitment(text, salt) for text, salt in pairs]
    r = client.post(
        f"/events/{event_id}/commitments/batch",
        json={"commitment_hashes": hashes},
    )
    assert r.status_code == 201, r.text
    return event_id, pairs


def single_reveal_request(event_id: str, text: str, salt: str):
    return lambda: client.post(
        f"/events/{event_id}/reveals",
        json={"original_text": text, "salt": salt},
    )


def batch_reveal_request(event_id: str, pairs):
    return lambda: client.post(
        f"/events/{event_id}/reveals/batch",
        json={"reveals": [{"original_text": t, "salt": s} for t, s in pairs]},
    )


def settle_request(event_id: str, standard_result: str):
    return lambda: client.post(
        f"/events/{event_id}/settlement",
        json={"standard_result": standard_result},
    )


# --------------------------------------------------------------------------- #
# 场景 A：揭示先取得事件锁（截止前获准、提交前挂起），结算在截止后排队等待
# --------------------------------------------------------------------------- #

def test_single_reveal_committed_before_settlement_is_counted(monkeypatch):
    event_id, pairs = seed_event(num_commitments=1)
    text, salt = pairs[0]
    emu = EventLockEmulator(monkeypatch)
    emu.gate_commit_roles = {"reveal"}
    emu.should_gate_commit = lambda db: any(isinstance(o, Reveal) for o in db.new)

    # 揭示请求在截止前到达：取得事件锁、按截止前时钟通过阶段检查，
    # 随后在真正提交前挂起（持有事件锁，INSERT 尚未提交）
    clock.set(PRE)
    emu.role_for_next = "reveal"
    reveal_thread, reveal_out = run_in_thread(single_reveal_request(event_id, text, salt))
    wait_event(emu.commit_reached, "揭示到达提交前门控")
    assert emu.holder_role(event_id) == "reveal"

    # 结算请求在截止后到达：被同一把事件锁阻塞，尚不能读时钟 / 统计
    clock.set(REVEAL_DL)
    emu.role_for_next = "settlement"
    settle_thread, settle_out = run_in_thread(settle_request(event_id, text))
    emu.wait_until(lambda: emu.waiter_count(event_id) == 1, "结算排队等待事件锁")

    # 放行揭示提交：锁在真实提交完成后才释放，结算随后取得锁
    emu.allow_commit.set()
    reveal_thread.join(timeout=LOCK_WAIT_TIMEOUT)
    settle_thread.join(timeout=LOCK_WAIT_TIMEOUT)
    assert not reveal_thread.is_alive()
    assert not settle_thread.is_alive()

    reveal_resp = reveal_out["response"]
    settle_resp = settle_out["response"]
    assert reveal_resp.status_code == 201, reveal_resp.text
    # 揭示的 created_at 取自截止前的唯一一次读数，而非提交时刻
    assert datetime.fromisoformat(reveal_resp.json()["revealed_at"]) == PRE

    assert settle_resp.status_code == 200, settle_resp.text
    body = settle_resp.json()
    # 先提交的合法揭示必须被结算统计：固化计数与存库记录一致
    assert body["status"] == "settled"
    assert body["correct_count"] == 1
    assert body["incorrect_count"] == 0
    assert body["unrevealed_count"] == 0

    detail = client.get(f"/events/{event_id}").json()
    assert detail["reveal_count"] == 1
    assert detail["commitment_count"] == 1
    # 结算查询永久返回同一结果
    assert client.get(f"/events/{event_id}/settlement").json() == body


def test_batch_reveal_committed_before_settlement_is_counted(monkeypatch):
    event_id, pairs = seed_event(num_commitments=2)
    emu = EventLockEmulator(monkeypatch)
    emu.gate_commit_roles = {"reveal"}
    emu.should_gate_commit = lambda db: any(isinstance(o, Reveal) for o in db.new)

    clock.set(PRE)
    emu.role_for_next = "reveal"
    reveal_thread, reveal_out = run_in_thread(batch_reveal_request(event_id, pairs))
    wait_event(emu.commit_reached, "批量揭示到达提交前门控")
    assert emu.holder_role(event_id) == "reveal"

    clock.set(REVEAL_DL)
    emu.role_for_next = "settlement"
    settle_thread, settle_out = run_in_thread(settle_request(event_id, pairs[0][0]))
    emu.wait_until(lambda: emu.waiter_count(event_id) == 1, "结算排队等待事件锁")

    emu.allow_commit.set()
    reveal_thread.join(timeout=LOCK_WAIT_TIMEOUT)
    settle_thread.join(timeout=LOCK_WAIT_TIMEOUT)
    assert not reveal_thread.is_alive() and not settle_thread.is_alive()

    reveal_resp = reveal_out["response"]
    settle_resp = settle_out["response"]
    assert reveal_resp.status_code == 201, reveal_resp.text
    assert reveal_resp.json()["count"] == 2
    assert {
        datetime.fromisoformat(r["revealed_at"]) for r in reveal_resp.json()["reveals"]
    } == {PRE}

    assert settle_resp.status_code == 200, settle_resp.text
    body = settle_resp.json()
    # 两条揭示均在结算统计内：一条精确匹配、一条不匹配，未揭示为 0
    assert body["correct_count"] == 1
    assert body["incorrect_count"] == 1
    assert body["unrevealed_count"] == 0
    assert client.get(f"/events/{event_id}").json()["reveal_count"] == 2


# --------------------------------------------------------------------------- #
# 场景 B：结算先取得事件锁（截止后固化），揭示在锁外排队；放锁后揭示按
#         closed 阶段拒绝且不落库
# --------------------------------------------------------------------------- #

def test_late_single_reveal_after_settlement_rejected_without_write(monkeypatch):
    event_id, pairs = seed_event(num_commitments=1)
    text, salt = pairs[0]
    emu = EventLockEmulator(monkeypatch)

    settlement_acquired = threading.Event()
    allow_settlement = threading.Event()

    def after_acquire(role):
        if role != "settlement":
            return
        # 结算取得锁的瞬间真实时钟跨过揭示截止：推进模拟时钟后持锁挂起，
        # 让揭示请求先排队到这把锁上
        clock.set(REVEAL_DL)
        settlement_acquired.set()
        assert allow_settlement.wait(timeout=LOCK_WAIT_TIMEOUT), "超时等待放行结算"

    emu.on_acquire = after_acquire

    clock.set(PRE)
    emu.role_for_next = "settlement"
    settle_thread, settle_out = run_in_thread(settle_request(event_id, text))
    wait_event(settlement_acquired, "结算取得事件锁并持锁挂起")
    assert emu.holder_role(event_id) == "settlement"

    # 揭示请求在截止点之后排队等待事件锁（取得锁前不读时钟、不写库）
    emu.role_for_next = "reveal"
    reveal_thread, reveal_out = run_in_thread(single_reveal_request(event_id, text, salt))
    emu.wait_until(lambda: emu.waiter_count(event_id) == 1, "揭示排队等待事件锁")

    # 放行结算：固化时揭示尚未提交，承诺计为未揭示
    allow_settlement.set()
    settle_thread.join(timeout=LOCK_WAIT_TIMEOUT)
    # 结算提交并放锁后，揭示取得锁，读到的唯一次时钟已 closed
    reveal_thread.join(timeout=LOCK_WAIT_TIMEOUT)
    assert not settle_thread.is_alive() and not reveal_thread.is_alive()

    settle_resp = settle_out["response"]
    reveal_resp = reveal_out["response"]
    assert settle_resp.status_code == 200, settle_resp.text
    settled = settle_resp.json()
    assert settled["correct_count"] == 0
    assert settled["incorrect_count"] == 0
    assert settled["unrevealed_count"] == 1

    # 后到揭示按阶段拒绝且不落库
    assert reveal_resp.status_code == 409, reveal_resp.text
    assert reveal_resp.json()["detail"]["code"] == "REVEAL_PHASE_CLOSED"
    assert client.get(f"/events/{event_id}").json()["reveal_count"] == 0

    # 固化计数永久不变，与存库记录一致（仍为 0 条揭示）
    stored = client.get(f"/events/{event_id}/settlement").json()
    assert stored == settled
    assert stored["unrevealed_count"] == 1


def test_late_batch_reveal_after_settlement_rejected_and_fully_rolled_back(monkeypatch):
    event_id, pairs = seed_event(num_commitments=2)
    emu = EventLockEmulator(monkeypatch)

    settlement_acquired = threading.Event()
    allow_settlement = threading.Event()

    def after_acquire(role):
        if role != "settlement":
            return
        clock.set(REVEAL_DL)
        settlement_acquired.set()
        assert allow_settlement.wait(timeout=LOCK_WAIT_TIMEOUT), "超时等待放行结算"

    emu.on_acquire = after_acquire

    clock.set(PRE)
    emu.role_for_next = "settlement"
    settle_thread, settle_out = run_in_thread(
        settle_request(event_id, "anything-not-revealed")
    )
    wait_event(settlement_acquired, "结算取得事件锁并持锁挂起")

    emu.role_for_next = "reveal"
    reveal_thread, reveal_out = run_in_thread(batch_reveal_request(event_id, pairs))
    emu.wait_until(lambda: emu.waiter_count(event_id) == 1, "批量揭示排队等待事件锁")

    allow_settlement.set()
    settle_thread.join(timeout=LOCK_WAIT_TIMEOUT)
    reveal_thread.join(timeout=LOCK_WAIT_TIMEOUT)
    assert not settle_thread.is_alive() and not reveal_thread.is_alive()

    settle_resp = settle_out["response"]
    reveal_resp = reveal_out["response"]
    assert settle_resp.status_code == 200, settle_resp.text
    settled = settle_resp.json()
    # 两个承诺均未揭示
    assert settled["correct_count"] == 0
    assert settled["incorrect_count"] == 0
    assert settled["unrevealed_count"] == 2

    # 整批按 closed 阶段拒绝：全批回滚，不落库任何一条揭示
    assert reveal_resp.status_code == 409, reveal_resp.text
    assert reveal_resp.json()["detail"]["code"] == "REVEAL_PHASE_CLOSED"
    assert client.get(f"/events/{event_id}").json()["reveal_count"] == 0
    commitments = client.get(f"/events/{event_id}/commitments").json()
    assert all(c["revealed"] is False for c in commitments)

    stored = client.get(f"/events/{event_id}/settlement").json()
    assert stored == settled


def test_settlement_remains_single_confirmation_under_contention(monkeypatch):
    """结算只确认一次：揭示落败后再次结算仍返回首次固化结果，绝不改写。"""
    event_id, pairs = seed_event(num_commitments=1)
    emu = EventLockEmulator(monkeypatch)

    settlement_acquired = threading.Event()
    allow_settlement = threading.Event()

    def after_acquire(role):
        if role != "settlement":
            return
        clock.set(REVEAL_DL)
        settlement_acquired.set()
        assert allow_settlement.wait(timeout=LOCK_WAIT_TIMEOUT)

    emu.on_acquire = after_acquire
    clock.set(PRE)
    emu.role_for_next = "settlement"
    settle_thread, settle_out = run_in_thread(settle_request(event_id, pairs[0][0]))
    wait_event(settlement_acquired, "结算取得事件锁")

    emu.role_for_next = "reveal"
    reveal_thread, reveal_out = run_in_thread(
        single_reveal_request(event_id, pairs[0][0], pairs[0][1])
    )
    emu.wait_until(lambda: emu.waiter_count(event_id) == 1, "揭示排队等待事件锁")

    allow_settlement.set()
    settle_thread.join(timeout=LOCK_WAIT_TIMEOUT)
    reveal_thread.join(timeout=LOCK_WAIT_TIMEOUT)
    assert reveal_out["response"].status_code == 409
    assert reveal_out["response"].json()["detail"]["code"] == "REVEAL_PHASE_CLOSED"
    first = settle_out["response"].json()
    assert first["unrevealed_count"] == 1

    # 再次结算（不同标准结果）：唯一确认语义不变，返回首次结果
    r = client.post(
        f"/events/{event_id}/settlement",
        json={"standard_result": "完全不同的结果"},
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "SETTLEMENT_ALREADY_CONFIRMED"
    assert detail["conflicts"][0]["existing"] == first
    assert client.get(f"/events/{event_id}/settlement").json() == first
