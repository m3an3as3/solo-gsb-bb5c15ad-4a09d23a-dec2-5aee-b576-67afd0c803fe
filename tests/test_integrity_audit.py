"""只读完整性巡检命令测试：python -m app.integrity_audit。

SQLite 文件库 + 模拟时钟，覆盖：
  - 干净库巡检退出 0，JSON 结构完整（summary / anomalies / snapshot）
  - 揭示关联承诺缺失 / 属于其他事件 -> REVEAL_COMMITMENT_NOT_FOUND /
    REVEAL_COMMITMENT_EVENT_MISMATCH
  - 原文+盐重算值与揭示保存哈希不一致 -> REVEAL_HASH_MISMATCH
  - 重算值与承诺哈希不一致 -> REVEAL_COMMITMENT_HASH_MISMATCH
  - 结算固化计数与重算值逐项不一致 -> SETTLEMENT_*_COUNT_MISMATCH
  - 结算悬挂到不存在的事件 -> SETTLEMENT_EVENT_NOT_FOUND
  - 巡检全程只读：业务数据逐字节不变
  - 巡检失败（数据库不可用）退出 2 且不输出半截 JSON
"""

import json
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_audit_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmpdir}/audit.db"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.clock as clock_module  # noqa: E402
import app.integrity_audit as audit  # noqa: E402
import app.main as main_module  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.hashing import compute_commitment  # noqa: E402
from app.models import Commitment, DeadlineExtension, Event, Reveal, Settlement  # noqa: E402

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
        # 巡检面向整库：每个用例从空库开始，避免上一用例的篡改残留干扰
        db = SessionLocal()
        try:
            for model in (Reveal, Settlement, Commitment, DeadlineExtension, Event):
                db.query(model).delete()
            db.commit()
        finally:
            db.close()
        yield


def create_event() -> str:
    r = client.post("/events", json={
        "title": "巡检事件",
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


def settle(event_id: str, standard_result: str) -> None:
    r = client.post(f"/events/{event_id}/settlement",
                    json={"standard_result": standard_result})
    assert r.status_code == 200, r.text


def run_main(capsys, argv=None) -> tuple[int, dict]:
    """运行巡检入口并解析 stdout 中的 JSON 报告。"""
    code = audit.main(argv or [])
    out = capsys.readouterr()
    assert out.err == ""
    return code, json.loads(out.out)


def make_settled_event() -> str:
    """1 正确 + 1 错误 + 1 未揭示的已结算事件。"""
    event_id = create_event()
    commit(event_id, "明天下雨", "salt-a")
    commit(event_id, "明年晴天", "salt-b")
    commit(event_id, "不揭示", "salt-c")
    clock.t = COMMIT_DL
    reveal(event_id, "明天下雨", "salt-a")
    reveal(event_id, "明年晴天", "salt-b")
    clock.t = REVEAL_DL
    settle(event_id, "明天下雨")
    return event_id


def test_clean_database_exits_zero(capsys):
    event_id = make_settled_event()
    code, report = run_main(capsys)
    assert code == 0
    assert report["status"] == "ok"
    assert report["hash_scheme"].startswith("sha256(")
    assert report["generated_at"]
    assert report["anomalies"] == []
    summary = report["summary"]
    assert summary["event_count"] >= 1
    assert summary["commitment_count"] >= 3
    assert summary["reveal_count"] >= 2
    assert summary["settlement_count"] >= 1
    assert summary["anomaly_count"] == 0
    # SQLite（测试方言）不附加快照选项；PostgreSQL 上为只读可重复读
    assert report["snapshot"] is None
    assert event_id  # 事件 ID 参与流程即足够，干净库无任何异常


def test_snapshot_options_by_dialect():
    pg = audit.snapshot_options("postgresql")
    assert pg == {"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
    assert audit.snapshot_options("sqlite") == {}


def test_run_audit_applies_postgres_snapshot_options(monkeypatch):
    """PostgreSQL 方言下 run_audit 须在事务开始前设置只读一致性快照选项。"""
    calls: dict = {}

    class FakeTransaction:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execution_options(self, **opts):
            calls["options"] = opts
            return self

        def begin(self):
            calls["began_with_options"] = "options" in calls
            return FakeTransaction()

    class FakeEngine:
        class dialect:
            name = "postgresql"

        def connect(self):
            return FakeConnection()

    class FakeSession:
        def __init__(self, bind):
            calls["bind"] = bind

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(audit, "Session", FakeSession)
    monkeypatch.setattr(audit, "audit_session", lambda session: {"ok": True})
    result = audit.run_audit(FakeEngine())
    assert result == {"ok": True}
    assert calls["options"] == {
        "isolation_level": "REPEATABLE READ",
        "postgresql_readonly": True,
    }
    # 快照选项必须先于事务开启设置
    assert calls["began_with_options"] is True


def test_pretty_output_is_valid_json(capsys):
    make_settled_event()
    code = audit.main(["--pretty"])
    out = capsys.readouterr()
    assert code == 0
    assert "\n  " in out.out  # 缩进格式
    assert json.loads(out.out)["status"] == "ok"


def test_detects_reveal_text_tampering(capsys):
    event_id = make_settled_event()
    # 篡改「正确」揭示保存的原文：重算值与两处保存的哈希同时不符；
    # 同时结算重算计数随之变化（正确 1->0、错误 1->2），巡检应一并报告
    db = SessionLocal()
    try:
        reveal_row = (
            db.query(Reveal)
            .filter(
                Reveal.event_id == uuid.UUID(event_id),
                Reveal.original_text == "明天下雨",
            )
            .one()
        )
        reveal_id = str(reveal_row.id)
        commitment_id = str(reveal_row.commitment_id)
        reveal_row.original_text = "被篡改的原文"
        db.commit()
    finally:
        db.close()

    code, report = run_main(capsys)
    assert code == 1
    assert report["status"] == "anomalies_found"
    codes = {a["code"] for a in report["anomalies"]}
    assert codes == {
        audit.REVEAL_HASH_MISMATCH,
        audit.REVEAL_COMMITMENT_HASH_MISMATCH,
        audit.SETTLEMENT_CORRECT_COUNT_MISMATCH,
        audit.SETTLEMENT_INCORRECT_COUNT_MISMATCH,
    }
    reveal_anomalies = [
        a for a in report["anomalies"] if a["record_type"] == "reveal"
    ]
    assert len(reveal_anomalies) == 2
    for a in reveal_anomalies:
        assert a["event_id"] == event_id
        assert a["record_id"] == reveal_id
        assert a["detail"]["commitment_id"] == commitment_id
        assert a["detail"]["recomputed_hash"] != a["detail"].get(
            "stored_hash", a["detail"].get("commitment_hash")
        )
    settlement_anomalies = {
        a["code"]: a for a in report["anomalies"] if a["record_type"] == "settlement"
    }
    assert settlement_anomalies[audit.SETTLEMENT_CORRECT_COUNT_MISMATCH]["detail"] == {
        "field": "correct_count",
        "stored_count": 1,
        "recomputed_count": 0,
    }
    assert settlement_anomalies[audit.SETTLEMENT_INCORRECT_COUNT_MISMATCH]["detail"] == {
        "field": "incorrect_count",
        "stored_count": 1,
        "recomputed_count": 2,
    }
    assert report["summary"]["anomaly_count"] == len(report["anomalies"]) == 4


def test_detects_stored_recomputed_hash_tampering(capsys):
    event_id = make_settled_event()
    # 仅篡改揭示保存的 recomputed_hash：只触发 REVEAL_HASH_MISMATCH
    db = SessionLocal()
    try:
        reveal_row = db.query(Reveal).filter(Reveal.event_id == uuid.UUID(event_id)).first()
        reveal_id = str(reveal_row.id)
        reveal_row.recomputed_hash = "0" * 64
        db.commit()
    finally:
        db.close()

    code, report = run_main(capsys)
    assert code == 1
    assert [a["code"] for a in report["anomalies"]] == [audit.REVEAL_HASH_MISMATCH]
    anomaly = report["anomalies"][0]
    assert anomaly["event_id"] == event_id
    assert anomaly["record_id"] == reveal_id
    assert anomaly["detail"]["stored_hash"] == "0" * 64
    assert anomaly["detail"]["recomputed_hash"] != "0" * 64


def test_detects_commitment_hash_tampering(capsys):
    event_id = make_settled_event()
    # 仅篡改承诺保存的哈希：只触发 REVEAL_COMMITMENT_HASH_MISMATCH
    db = SessionLocal()
    try:
        commitment_row = (
            db.query(Commitment)
            .join(Reveal, Reveal.commitment_id == Commitment.id)
            .filter(Commitment.event_id == uuid.UUID(event_id))
            .first()
        )
        commitment_row.commitment_hash = "f" * 64
        db.commit()
    finally:
        db.close()

    code, report = run_main(capsys)
    assert code == 1
    assert [a["code"] for a in report["anomalies"]] == [
        audit.REVEAL_COMMITMENT_HASH_MISMATCH
    ]
    anomaly = report["anomalies"][0]
    assert anomaly["event_id"] == event_id
    assert anomaly["detail"]["commitment_hash"] == "f" * 64


def test_detects_orphan_reveal(capsys):
    event_id = make_settled_event()
    # 悬挂揭示：关联的承诺 ID 不存在（SQLite 不强制外键，可直接构造）
    ghost_commitment_id = uuid.uuid4()
    text, salt = "悬挂揭示", "ghost-salt"
    db = SessionLocal()
    try:
        ghost = Reveal(
            event_id=uuid.UUID(event_id),
            commitment_id=ghost_commitment_id,
            original_text=text,
            salt=salt,
            recomputed_hash=compute_commitment(text, salt),
            created_at=clock(),
        )
        db.add(ghost)
        db.commit()
        ghost_id = str(ghost.id)
    finally:
        db.close()

    code, report = run_main(capsys)
    assert code == 1
    # 悬挂揭示同时使结算重算计数变化（错误 1->2、未揭示 1->0），一并报告
    codes = [a["code"] for a in report["anomalies"]]
    assert set(codes) == {
        audit.REVEAL_COMMITMENT_NOT_FOUND,
        audit.SETTLEMENT_INCORRECT_COUNT_MISMATCH,
        audit.SETTLEMENT_UNREVEALED_COUNT_MISMATCH,
    }
    anomaly = next(a for a in report["anomalies"] if a["record_type"] == "reveal")
    assert anomaly["code"] == audit.REVEAL_COMMITMENT_NOT_FOUND
    assert anomaly["event_id"] == event_id
    assert anomaly["record_id"] == ghost_id
    assert anomaly["detail"]["commitment_id"] == str(ghost_commitment_id)


def test_detects_reveal_commitment_in_other_event(capsys):
    # 事件 A 的揭示挂到事件 B 的承诺上
    event_a = create_event()
    event_b = create_event()
    text, salt = "跨事件引用", "salt-x"
    commit(event_b, text, salt)
    clock.t = COMMIT_DL

    db = SessionLocal()
    try:
        commitment_b = (
            db.query(Commitment).filter(Commitment.event_id == uuid.UUID(event_b)).one()
        )
        misplaced = Reveal(
            event_id=uuid.UUID(event_a),
            commitment_id=commitment_b.id,
            original_text=text,
            salt=salt,
            recomputed_hash=compute_commitment(text, salt),
            created_at=clock(),
        )
        db.add(misplaced)
        db.commit()
        misplaced_id = str(misplaced.id)
    finally:
        db.close()

    code, report = run_main(capsys)
    assert code == 1
    assert [a["code"] for a in report["anomalies"]] == [
        audit.REVEAL_COMMITMENT_EVENT_MISMATCH
    ]
    anomaly = report["anomalies"][0]
    assert anomaly["event_id"] == event_a
    assert anomaly["record_id"] == misplaced_id
    assert anomaly["detail"]["commitment_event_id"] == event_b


def test_detects_settlement_count_tampering(capsys):
    event_id = make_settled_event()
    # 篡改固化的正确数与未揭示数：逐项报告，错误数不动
    db = SessionLocal()
    try:
        settlement_row = (
            db.query(Settlement).filter(Settlement.event_id == uuid.UUID(event_id)).one()
        )
        settlement_id = str(settlement_row.id)
        settlement_row.correct_count = 5
        settlement_row.unrevealed_count = 9
        db.commit()
    finally:
        db.close()

    code, report = run_main(capsys)
    assert code == 1
    by_code = {a["code"]: a for a in report["anomalies"]}
    assert set(by_code) == {
        audit.SETTLEMENT_CORRECT_COUNT_MISMATCH,
        audit.SETTLEMENT_UNREVEALED_COUNT_MISMATCH,
    }
    correct = by_code[audit.SETTLEMENT_CORRECT_COUNT_MISMATCH]
    assert correct["event_id"] == event_id
    assert correct["record_type"] == "settlement"
    assert correct["record_id"] == settlement_id
    assert correct["detail"] == {
        "field": "correct_count",
        "stored_count": 5,
        "recomputed_count": 1,
    }
    unrevealed = by_code[audit.SETTLEMENT_UNREVEALED_COUNT_MISMATCH]
    assert unrevealed["detail"] == {
        "field": "unrevealed_count",
        "stored_count": 9,
        "recomputed_count": 1,
    }


def test_detects_orphan_settlement(capsys):
    make_settled_event()
    # 悬挂结算：所属事件不存在
    ghost_event_id = uuid.uuid4()
    db = SessionLocal()
    try:
        ghost = Settlement(
            event_id=ghost_event_id,
            standard_result="x",
            correct_count=0,
            incorrect_count=0,
            unrevealed_count=0,
            created_at=clock(),
        )
        db.add(ghost)
        db.commit()
        ghost_id = str(ghost.id)
    finally:
        db.close()

    code, report = run_main(capsys)
    assert code == 1
    assert [a["code"] for a in report["anomalies"]] == [audit.SETTLEMENT_EVENT_NOT_FOUND]
    anomaly = report["anomalies"][0]
    assert anomaly["event_id"] == str(ghost_event_id)
    assert anomaly["record_type"] == "settlement"
    assert anomaly["record_id"] == ghost_id


def test_audit_is_readonly(capsys):
    make_settled_event()
    db = SessionLocal()
    try:
        before = {
            "events": sorted((str(e.id), e.title) for e in db.query(Event).all()),
            "commitments": sorted(
                (str(c.id), c.commitment_hash) for c in db.query(Commitment).all()
            ),
            "reveals": sorted(
                (str(r.id), r.original_text, r.salt, r.recomputed_hash)
                for r in db.query(Reveal).all()
            ),
            "settlements": sorted(
                (str(s.id), s.standard_result, s.correct_count,
                 s.incorrect_count, s.unrevealed_count)
                for s in db.query(Settlement).all()
            ),
            "deadline_extensions": sorted(
                str(e.id) for e in db.query(DeadlineExtension).all()
            ),
        }
    finally:
        db.close()

    code, _ = run_main(capsys)
    assert code == 0

    db = SessionLocal()
    try:
        after = {
            "events": sorted((str(e.id), e.title) for e in db.query(Event).all()),
            "commitments": sorted(
                (str(c.id), c.commitment_hash) for c in db.query(Commitment).all()
            ),
            "reveals": sorted(
                (str(r.id), r.original_text, r.salt, r.recomputed_hash)
                for r in db.query(Reveal).all()
            ),
            "settlements": sorted(
                (str(s.id), s.standard_result, s.correct_count,
                 s.incorrect_count, s.unrevealed_count)
                for s in db.query(Settlement).all()
            ),
            "deadline_extensions": sorted(
                str(e.id) for e in db.query(DeadlineExtension).all()
            ),
        }
    finally:
        db.close()
    assert before == after


def test_audit_failure_exits_two_without_json(capsys, monkeypatch):
    def boom(_engine):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(audit, "run_audit", boom)
    code = audit.main([])
    out = capsys.readouterr()
    assert code == 2
    assert out.out == ""  # 失败时不输出半截 JSON
    assert "database unreachable" in out.err
