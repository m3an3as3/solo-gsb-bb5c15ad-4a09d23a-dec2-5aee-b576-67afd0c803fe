"""只读完整性巡检命令（``python -m app.integrity_audit``）。

随服务镜像交付的离线巡检工具：复用 ``app.database`` 的引擎（同一
``DATABASE_URL`` 连接配置）与 ``app.hashing`` 的承诺哈希算法，在同一
PostgreSQL 一致性只读快照（单条 ``REPEATABLE READ READ ONLY`` 事务）中
逐事件核对：

- **揭示核对**：揭示关联的承诺必须存在且属于该事件；用揭示保存的原文
  与盐按承诺哈希方案重算，重算值须同时等于揭示保存的
  ``recomputed_hash`` 与承诺的 ``commitment_hash``；
- **结算核对**：已结算事件按标准结果重算正确 / 错误 / 未揭示数（与
  结算写入时同一统计口径），与固化计数逐项比较。

全程只读：只执行 SELECT，事务显式回滚结束、绝不提交，不改写任何业务
数据。输出机器可读 JSON（含事件 ID、异常记录 ID 与稳定问题代码）；
无异常退出 0，有异常退出 1，巡检自身执行失败（如数据库不可达）退出 2。
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from . import clock
from .database import engine
from .hashing import HASH_SCHEME, compute_commitment
from .models import Commitment, Event, Reveal, Settlement

# 稳定问题代码（机器可读，语义与拼写保持稳定）
REVEAL_COMMITMENT_MISSING = "REVEAL_COMMITMENT_MISSING"
REVEAL_COMMITMENT_EVENT_MISMATCH = "REVEAL_COMMITMENT_EVENT_MISMATCH"
REVEAL_RECOMPUTED_HASH_MISMATCH = "REVEAL_RECOMPUTED_HASH_MISMATCH"
REVEAL_COMMITMENT_HASH_MISMATCH = "REVEAL_COMMITMENT_HASH_MISMATCH"
SETTLEMENT_CORRECT_COUNT_MISMATCH = "SETTLEMENT_CORRECT_COUNT_MISMATCH"
SETTLEMENT_INCORRECT_COUNT_MISMATCH = "SETTLEMENT_INCORRECT_COUNT_MISMATCH"
SETTLEMENT_UNREVEALED_COUNT_MISMATCH = "SETTLEMENT_UNREVEALED_COUNT_MISMATCH"

# 退出码：无异常 0；有异常 1；巡检自身执行失败（如数据库不可达）2
EXIT_OK = 0
EXIT_ANOMALIES = 1
EXIT_AUDIT_FAILED = 2


def _as_utc(dt: datetime) -> datetime:
    """把数据库读出的时间统一为带 UTC 时区的时间（兼容朴素时间）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sort_key(record) -> tuple:
    """事件/揭示的稳定排序键：写入时刻 + UUID 128 位整数（跨方言逐字节一致）。"""
    return (_as_utc(record.created_at), record.id.int)


def collect_report(session: Session) -> dict[str, Any]:
    """在调用方打开的（只读）事务内执行全部核对，返回报告字典。

    只执行 SELECT；事务边界（只读快照的开启与回滚）由调用方管理。
    结算重算口径与 ``main.tally_settlement`` 一致：正确 = 已揭示且原文与
    标准结果逐字符相等；错误 = 已揭示但不相等；未揭示 = 承诺数 - 揭示数。
    """
    events = session.query(Event).all()
    commitments = session.query(Commitment).all()
    reveals = session.query(Reveal).all()
    settlements = session.query(Settlement).all()

    commitments_by_id: dict[UUID, Commitment] = {c.id: c for c in commitments}
    commitments_by_event: dict[UUID, list[Commitment]] = {}
    for commitment in commitments:
        commitments_by_event.setdefault(commitment.event_id, []).append(commitment)
    reveals_by_event: dict[UUID, list[Reveal]] = {}
    for reveal in reveals:
        reveals_by_event.setdefault(reveal.event_id, []).append(reveal)
    settlements_by_event: dict[UUID, Settlement] = {s.event_id: s for s in settlements}

    anomalies: list[dict[str, Any]] = []
    for event in sorted(events, key=_sort_key):
        event_id = str(event.id)
        event_reveals = sorted(reveals_by_event.get(event.id, []), key=_sort_key)

        # ---- 揭示核对：承诺存在且属于该事件；重算值同时等于两处存库哈希 ----
        for reveal in event_reveals:
            commitment = commitments_by_id.get(reveal.commitment_id)
            if commitment is None:
                anomalies.append(
                    {
                        "code": REVEAL_COMMITMENT_MISSING,
                        "event_id": event_id,
                        "reveal_id": str(reveal.id),
                        "commitment_id": str(reveal.commitment_id),
                        "message": "揭示关联的承诺不存在",
                    }
                )
            elif commitment.event_id != reveal.event_id:
                anomalies.append(
                    {
                        "code": REVEAL_COMMITMENT_EVENT_MISMATCH,
                        "event_id": event_id,
                        "reveal_id": str(reveal.id),
                        "commitment_id": str(commitment.id),
                        "commitment_event_id": str(commitment.event_id),
                        "message": "揭示关联的承诺不属于该事件",
                    }
                )
            recomputed = compute_commitment(reveal.original_text, reveal.salt)
            if recomputed != reveal.recomputed_hash:
                anomalies.append(
                    {
                        "code": REVEAL_RECOMPUTED_HASH_MISMATCH,
                        "event_id": event_id,
                        "reveal_id": str(reveal.id),
                        "commitment_id": str(reveal.commitment_id),
                        "stored_recomputed_hash": reveal.recomputed_hash,
                        "actual_recomputed_hash": recomputed,
                        "message": "原文与盐重算值不等于揭示保存的哈希",
                    }
                )
            if commitment is not None and recomputed != commitment.commitment_hash:
                anomalies.append(
                    {
                        "code": REVEAL_COMMITMENT_HASH_MISMATCH,
                        "event_id": event_id,
                        "reveal_id": str(reveal.id),
                        "commitment_id": str(commitment.id),
                        "commitment_hash": commitment.commitment_hash,
                        "actual_recomputed_hash": recomputed,
                        "message": "原文与盐重算值不等于承诺哈希",
                    }
                )

        # ---- 结算核对：按标准结果重算三项计数，与固化值逐项比较 ----
        settlement = settlements_by_event.get(event.id)
        if settlement is not None:
            correct = sum(
                1 for r in event_reveals if r.original_text == settlement.standard_result
            )
            incorrect = len(event_reveals) - correct
            unrevealed = len(commitments_by_event.get(event.id, [])) - len(event_reveals)
            for code, stored, recounted, label in (
                (SETTLEMENT_CORRECT_COUNT_MISMATCH, settlement.correct_count, correct, "正确数"),
                (SETTLEMENT_INCORRECT_COUNT_MISMATCH, settlement.incorrect_count, incorrect, "错误数"),
                (SETTLEMENT_UNREVEALED_COUNT_MISMATCH, settlement.unrevealed_count, unrevealed, "未揭示数"),
            ):
                if stored != recounted:
                    anomalies.append(
                        {
                            "code": code,
                            "event_id": event_id,
                            "settlement_id": str(settlement.id),
                            "stored_count": stored,
                            "recounted_count": recounted,
                            "message": f"结算固化的{label}与按标准结果重算的值不一致",
                        }
                    )

    bind = session.get_bind()
    dialect_name = bind.dialect.name if bind is not None else engine.dialect.name
    snapshot = (
        "REPEATABLE READ READ ONLY"
        if dialect_name == "postgresql"
        else "single-transaction"
    )
    return {
        "status": "anomalies" if anomalies else "ok",
        "generated_at": _as_utc(clock.now_utc()).isoformat(),
        "hash_scheme": HASH_SCHEME,
        "snapshot": snapshot,
        "checked": {
            "events": len(events),
            "commitments": len(commitments),
            "reveals": len(reveals),
            "settlements": len(settlements),
        },
        "anomaly_count": len(anomalies),
        "anomalies": anomalies,
    }


def run_audit() -> dict[str, Any]:
    """在一致性只读快照中执行巡检并返回报告字典。

    PostgreSQL：单条 ``REPEATABLE READ READ ONLY`` 事务——全部核对读取
    同一一致性快照，事务级只读从机制上保证不改写业务数据；SQLite（本地
    测试）降级为普通事务（单连接串行读取，同样只执行 SELECT）。无论
    结果如何都以显式回滚结束事务，绝不提交。
    """
    connection = engine.connect()
    try:
        if engine.dialect.name == "postgresql":
            connection = connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
        session = Session(bind=connection)
        try:
            return collect_report(session)
        finally:
            session.close()
            connection.rollback()  # 只读巡检：显式回滚结束事务，绝不提交
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    """命令行入口：执行巡检，把机器可读 JSON 报告写到 stdout。"""
    parser = argparse.ArgumentParser(
        prog="python -m app.integrity_audit",
        description="只读完整性巡检：在同一一致性只读快照中逐事件核对揭示哈希与结算固化计数",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="以缩进格式输出 JSON（默认单行）",
    )
    args = parser.parse_args(argv)

    try:
        report = run_audit()
    except Exception as exc:  # 数据库不可达等执行失败：错误 JSON 打到 stderr
        error = {
            "status": "error",
            "code": "AUDIT_EXECUTION_FAILED",
            "message": f"巡检执行失败：{exc}",
        }
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return EXIT_AUDIT_FAILED

    indent = 2 if args.pretty else None
    print(json.dumps(report, ensure_ascii=False, indent=indent))
    return EXIT_OK if report["anomaly_count"] == 0 else EXIT_ANOMALIES


if __name__ == "__main__":
    sys.exit(main())
