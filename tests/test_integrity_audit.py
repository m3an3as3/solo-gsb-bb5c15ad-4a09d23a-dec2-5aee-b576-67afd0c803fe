"""只读完整性巡检（python -m app.integrity_audit）测试。

SQLite 文件库 + 直接写库构造正常与损坏数据，覆盖：
  - 正常数据（含已结算事件）：报告 ok、无异常、main() 退出 0
  - 揭示关联承诺缺失 -> REVEAL_COMMITMENT_MISSING
  - 承诺属于其他事件 -> REVEAL_COMMITMENT_EVENT_MISMATCH
  - 重算值 != 揭示保存哈希 -> REVEAL_RECOMPUTED_HASH_MISMATCH
  - 重算值 != 承诺哈希 -> REVEAL_COMMITMENT_HASH_MISMATCH
  - 结算固化计数被篡改 -> SETTLEMENT_*_COUNT_MISMATCH 逐项报告
  - 有异常时 main() 退出 1 且 stdout 为可解析 JSON
  - 巡检不改写任何业务数据（前后逐行对比）
  - 子进程 python -m app.integrity_audit 端到端退出码与 JSON 输出
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

_tmpdir = tempfile.mkdtemp(prefix="sealed_predictions_integrity_audit_test_")
DB_PATH = f"{_tmpdir}/integrity_audit.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"

import pytest  # noqa: E402

import app.integrity_audit as audit  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.hashing import compute_commitment  # noqa: E402
from app.models import (  # noqa: E402
    Commitment,
    DeadlineExtension,
    Event,
    Reveal,
    Settlement,
)

T0 = datetime(2030, 8, 1, 0, 0, 0, tzinfo=timezone.utc)
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_tables():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        for model in (Reveal, Settlement, Commitment, DeadlineExtension, Event):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()
    yield


def make_event(db: SessionLocal, title: str = "巡检事件", offset_hours: int = 0) -> Event:
    event = Event(
        id=uuid4(),
        title=title,
        description=None,
        commit_deadline=T0 + timedelta(hours=1 + offset_hours),
        reveal_deadline=T0 + timedelta(hours=2 + offset_hours),
        created_at=T0,
    )
    db.add(event)
    return event


def make_commitment(db: SessionLocal, event: Event, text: str, salt: str) -> Commitment:
    commitment = Commitment(
        id=uuid4(),
        event_id=event.id,
        commitment_hash=compute_commitment(text, salt),
        created_at=T0,
    )
    db.add(commitment)
    return commitment


def make_reveal(
    db: SessionLocal,
    event: Event,
    commitment: Commitment,
    text: str,
    salt: str,
    recomputed_hash: str | None = None,
) -> Reveal:
    reveal = Reveal(
        id=uuid4(),
        event_id=event.id,
        commitment_id=commitment.id,
        original_text=text,
        salt=salt,
        recomputed_hash=(
            recomputed_hash
            if recomputed_hash is not None
            else compute_commitment(text, salt)
        ),
        created_at=T0 + timedelta(hours=1),
    )
    db.add(reveal)
    return reveal


def make_settlement(
    db: SessionLocal,
    event: Event,
    standard_result: str,
    correct: int,
    incorrect: int,
    unrevealed: int,
) -> Settlement:
    settlement = Settlement(
        id=uuid4(),
        event_id=event.id,
        standard_result=standard_result,
        correct_count=correct,
        incorrect_count=incorrect,
        unrevealed_count=unrevealed,
        created_at=T0 + timedelta(hours=2),
    )
    db.add(settlement)
    return settlement


def seed_consistent_event() -> dict:
    """构造一个完全一致的已结算事件：3 承诺、2 揭示（1 对 1 错）、1 未揭示。"""
    db = SessionLocal()
    try:
        event = make_event(db)
        c1 = make_commitment(db, event, "明天下雨", "salt-1")
        c2 = make_commitment(db, event, "明天晴天", "salt-2")
        make_commitment(db, event, "明天下雪", "salt-3")  # 不揭示
        r1 = make_reveal(db, event, c1, "明天下雨", "salt-1")
        r2 = make_reveal(db, event, c2, "明天晴天", "salt-2")
        settlement = make_settlement(db, event, "明天下雨", 1, 1, 1)
        db.commit()
        return {
            "event_id": str(event.id),
            "commitment_ids": [str(c1.id), str(c2.id)],
            "reveal_ids": [str(r1.id), str(r2.id)],
            "settlement_id": str(settlement.id),
        }
    finally:
        db.close()


def report() -> dict:
    return audit.run_audit()


def codes(rep: dict) -> list[str]:
    return [a["code"] for a in rep["anomalies"]]


# --------------------------------------------------------------------------- #
# 正常数据
# --------------------------------------------------------------------------- #

def test_empty_database_reports_ok():
    rep = report()
    assert rep["status"] == "ok"
    assert rep["anomaly_count"] == 0
    assert rep["anomalies"] == []
    assert rep["checked"] == {
        "events": 0,
        "commitments": 0,
        "reveals": 0,
        "settlements": 0,
    }


def test_consistent_settled_event_reports_ok():
    ids = seed_consistent_event()
    rep = report()
    assert rep["status"] == "ok"
    assert rep["anomaly_count"] == 0
    assert rep["anomalies"] == []
    assert rep["checked"] == {
        "events": 1,
        "commitments": 3,
        "reveals": 2,
        "settlements": 1,
    }
    assert rep["hash_scheme"] == audit.HASH_SCHEME
    # 报告只读且机器可读：事件 ID 出现在 checked 计数语义中，无异常列表为空
    assert ids["event_id"]  # 种子构造成功


def test_unsettled_event_without_settlement_is_fine():
    db = SessionLocal()
    try:
        event = make_event(db)
        c = make_commitment(db, event, "明天下雨", "salt-1")
        make_reveal(db, event, c, "明天下雨", "salt-1")
        db.commit()
    finally:
        db.close()
    rep = report()
    assert rep["status"] == "ok"
    assert rep["checked"]["settlements"] == 0


# --------------------------------------------------------------------------- #
# 揭示核对
# --------------------------------------------------------------------------- #

def test_reveal_commitment_missing():
    db = SessionLocal()
    try:
        event = make_event(db)
        ghost = uuid4()  # 不存在的承诺 ID
        reveal = Reveal(
            id=uuid4(),
            event_id=event.id,
            commitment_id=ghost,
            original_text="明天下雨",
            salt="salt-1",
            recomputed_hash=compute_commitment("明天下雨", "salt-1"),
            created_at=T0 + timedelta(hours=1),
        )
        db.add(reveal)
        db.commit()
        reveal_id = str(reveal.id)
    finally:
        db.close()

    rep = report()
    assert rep["status"] == "anomalies"
    # 承诺缺失时跳过承诺哈希比对；重算值与揭示保存哈希一致，仅报缺失
    assert codes(rep) == [audit.REVEAL_COMMITMENT_MISSING]
    anomaly = rep["anomalies"][0]
    assert anomaly["event_id"] == str(event.id)
    assert anomaly["reveal_id"] == reveal_id
    assert anomaly["commitment_id"] == str(ghost)


def test_reveal_commitment_event_mismatch():
    db = SessionLocal()
    try:
        event_a = make_event(db, title="事件A")
        event_b = make_event(db, title="事件B", offset_hours=10)
        commitment_b = make_commitment(db, event_b, "明天下雨", "salt-1")
        # 损坏：事件 A 的揭示引用了事件 B 的承诺（哈希本身一致）
        reveal = make_reveal(db, event_a, commitment_b, "明天下雨", "salt-1")
        db.commit()
        reveal_id = str(reveal.id)
    finally:
        db.close()

    rep = report()
    assert codes(rep) == [audit.REVEAL_COMMITMENT_EVENT_MISMATCH]
    anomaly = rep["anomalies"][0]
    assert anomaly["event_id"] == str(event_a.id)
    assert anomaly["reveal_id"] == reveal_id
    assert anomaly["commitment_id"] == str(commitment_b.id)
    assert anomaly["commitment_event_id"] == str(event_b.id)


def test_reveal_recomputed_hash_mismatch():
    db = SessionLocal()
    try:
        event = make_event(db)
        commitment = make_commitment(db, event, "明天下雨", "salt-1")
        # 损坏：揭示保存的重算哈希被篡改（承诺哈希仍与原文+盐一致）
        reveal = make_reveal(
            db, event, commitment, "明天下雨", "salt-1", recomputed_hash="0" * 64
        )
        db.commit()
        reveal_id = str(reveal.id)
    finally:
        db.close()

    rep = report()
    assert codes(rep) == [audit.REVEAL_RECOMPUTED_HASH_MISMATCH]
    anomaly = rep["anomalies"][0]
    assert anomaly["event_id"] == str(event.id)
    assert anomaly["reveal_id"] == reveal_id
    assert anomaly["stored_recomputed_hash"] == "0" * 64
    assert anomaly["actual_recomputed_hash"] == compute_commitment("明天下雨", "salt-1")


def test_reveal_commitment_hash_mismatch():
    db = SessionLocal()
    try:
        event = make_event(db)
        commitment = make_commitment(db, event, "明天下雨", "salt-1")
        make_reveal(db, event, commitment, "明天下雨", "salt-1")
        # 损坏：承诺哈希被改写，与原文+盐重算值不再相等
        commitment.commitment_hash = "f" * 64
        db.commit()
    finally:
        db.close()

    rep = report()
    assert codes(rep) == [audit.REVEAL_COMMITMENT_HASH_MISMATCH]
    anomaly = rep["anomalies"][0]
    assert anomaly["event_id"] == str(event.id)
    assert anomaly["commitment_id"] == str(commitment.id)
    assert anomaly["commitment_hash"] == "f" * 64
    assert anomaly["actual_recomputed_hash"] == compute_commitment("明天下雨", "salt-1")


# --------------------------------------------------------------------------- #
# 结算核对
# --------------------------------------------------------------------------- #

def test_settlement_counts_mismatch_reported_per_counter():
    ids = seed_consistent_event()
    db = SessionLocal()
    try:
        settlement = db.query(Settlement).one()
        # 损坏：三项固化计数均被篡改（真实重算为 1 / 1 / 1）
        settlement.correct_count = 3
        settlement.incorrect_count = 0
        settlement.unrevealed_count = 0
        db.commit()
    finally:
        db.close()

    rep = report()
    assert rep["status"] == "anomalies"
    assert codes(rep) == [
        audit.SETTLEMENT_CORRECT_COUNT_MISMATCH,
        audit.SETTLEMENT_INCORRECT_COUNT_MISMATCH,
        audit.SETTLEMENT_UNREVEALED_COUNT_MISMATCH,
    ]
    by_code = {a["code"]: a for a in rep["anomalies"]}
    correct = by_code[audit.SETTLEMENT_CORRECT_COUNT_MISMATCH]
    assert correct["event_id"] == ids["event_id"]
    assert correct["settlement_id"] == ids["settlement_id"]
    assert correct["stored_count"] == 3
    assert correct["recounted_count"] == 1
    unrevealed = by_code[audit.SETTLEMENT_UNREVEALED_COUNT_MISMATCH]
    assert unrevealed["stored_count"] == 0
    assert unrevealed["recounted_count"] == 1


def test_settlement_recount_uses_standard_result_exact_match():
    """重算口径与结算一致：逐字符精确匹配，区分大小写与空白。"""
    db = SessionLocal()
    try:
        event = make_event(db)
        c1 = make_commitment(db, event, "Yes", "s1")
        c2 = make_commitment(db, event, "yes", "s2")  # 大小写不同 -> 错误
        make_reveal(db, event, c1, "Yes", "s1")
        make_reveal(db, event, c2, "yes", "s2")
        make_settlement(db, event, "Yes", 1, 1, 0)
        db.commit()
    finally:
        db.close()
    rep = report()
    assert rep["status"] == "ok"
    assert rep["anomaly_count"] == 0


# --------------------------------------------------------------------------- #
# 退出码与输出
# --------------------------------------------------------------------------- #

def test_main_exit_zero_and_json_on_clean_data(capsys):
    seed_consistent_event()
    assert audit.main([]) == 0
    out = capsys.readouterr().out
    rep = json.loads(out)  # 单行机器可读 JSON
    assert rep["status"] == "ok"
    assert rep["anomaly_count"] == 0


def test_main_exit_one_and_json_on_anomalies(capsys):
    seed_consistent_event()
    db = SessionLocal()
    try:
        settlement = db.query(Settlement).one()
        settlement.unrevealed_count = 42
        db.commit()
    finally:
        db.close()

    assert audit.main([]) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["status"] == "anomalies"
    assert rep["anomaly_count"] == 1
    assert rep["anomalies"][0]["code"] == audit.SETTLEMENT_UNREVEALED_COUNT_MISMATCH


def test_main_pretty_prints_indented_json(capsys):
    assert audit.main(["--pretty"]) == 0
    out = capsys.readouterr().out
    assert "\n  " in out  # 缩进格式
    assert json.loads(out)["status"] == "ok"


# --------------------------------------------------------------------------- #
# 只读保证：巡检前后业务数据逐行一致
# --------------------------------------------------------------------------- #

def dump_business_data() -> dict:
    db = SessionLocal()
    try:
        return {
            "events": sorted(
                (str(e.id), e.title, e.description, e.commit_deadline, e.reveal_deadline, e.created_at)
                for e in db.query(Event).all()
            ),
            "commitments": sorted(
                (str(c.id), str(c.event_id), c.commitment_hash, c.created_at)
                for c in db.query(Commitment).all()
            ),
            "reveals": sorted(
                (str(r.id), str(r.event_id), str(r.commitment_id), r.original_text, r.salt, r.recomputed_hash, r.created_at)
                for r in db.query(Reveal).all()
            ),
            "settlements": sorted(
                (str(s.id), str(s.event_id), s.standard_result, s.correct_count, s.incorrect_count, s.unrevealed_count, s.created_at)
                for s in db.query(Settlement).all()
            ),
        }
    finally:
        db.close()


def test_audit_does_not_modify_business_data():
    seed_consistent_event()
    db = SessionLocal()
    try:
        settlement = db.query(Settlement).one()
        settlement.correct_count = 99  # 制造异常，确认巡检只报告不修复
        db.commit()
    finally:
        db.close()

    before = dump_business_data()
    rep = report()
    assert rep["status"] == "anomalies"
    after = dump_business_data()
    assert before == after


# --------------------------------------------------------------------------- #
# 子进程端到端：python -m app.integrity_audit
# --------------------------------------------------------------------------- #

def run_module_subprocess() -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # 子进程必须巡检与当前进程引擎相同的库：完整套件中 app.database 的引擎
    # 由首个导入它的测试模块绑定（本模块的 DATABASE_URL 不一定生效），
    # 因此直接取引擎实际使用的 URL 传给子进程
    env["DATABASE_URL"] = engine.url.render_as_string(hide_password=False)
    return subprocess.run(
        [sys.executable, "-m", "app.integrity_audit"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_module_subprocess_exit_zero_on_clean_data():
    seed_consistent_event()
    proc = run_module_subprocess()
    assert proc.returncode == 0, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["status"] == "ok"
    assert rep["anomaly_count"] == 0


def test_module_subprocess_exit_one_on_anomalies():
    seed_consistent_event()
    db = SessionLocal()
    try:
        settlement = db.query(Settlement).one()
        settlement.incorrect_count = 7
        db.commit()
    finally:
        db.close()

    proc = run_module_subprocess()
    assert proc.returncode == 1, proc.stderr
    rep = json.loads(proc.stdout)
    assert rep["status"] == "anomalies"
    assert codes(rep) == [audit.SETTLEMENT_INCORRECT_COUNT_MISMATCH]
