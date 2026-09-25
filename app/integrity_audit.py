"""只读数据完整性巡检命令：``python -m app.integrity_audit``。

随服务镜像交付，供运维在容器内对持久库做一次**不改写任何业务数据**的
一致性巡检。复用现有数据库连接配置（``app.database`` 的 ``DATABASE_URL`` /
引擎）与承诺哈希算法（``app.hashing.compute_commitment``），在**同一个
只读事务**中逐事件核对：

1. 每条揭示关联的承诺必须存在且属于该事件；
2. 用揭示保存的原文与盐按约定方案重算，结果须**同时**等于揭示保存的
   ``recomputed_hash`` 与关联承诺的 ``commitment_hash``；
3. 已结算事件按标准结果重算 正确 / 错误 / 未揭示 三个计数，与结算时固化
   的计数逐项比较。

快照与只读保证：

- **PostgreSQL**（Compose 生产环境）：整个巡检在单个
  ``REPEATABLE READ`` + ``READ ONLY`` 事务中完成——自首个语句起看到同一
  份一致性快照，巡检期间其它事务的写入不会混入；事务级 ``READ ONLY``
  在数据库层面禁止本事务的任何写操作，从机制上保证不改写业务数据；
- **SQLite**（本地测试）：单条连接上的单个事务同样只执行 SELECT。

输出为机器可读的 JSON（stdout，``--pretty`` 可缩进），逐条列出异常所在的
事件 ID、异常记录 ID 与稳定的问题代码；退出码约定：

- ``0``：巡检完成，未发现异常；
- ``1``：巡检完成，发现至少一条异常（明细见 JSON 的 ``anomalies``）；
- ``2``：巡检本身未能完成（数据库不可达、库结构缺失等），错误摘要写
  stderr，不输出半截 JSON。

全程只读：不执行任何 INSERT / UPDATE / DELETE / DDL，不调用
``schema_bootstrap``，不启动 API 服务。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from . import clock
from .database import engine
from .hashing import HASH_SCHEME, compute_commitment
from .models import Commitment, Event, Reveal, Settlement

# --------------------------------------------------------------------------- #
# 稳定的问题代码（机器可读，对外契约，新增问题只追加不更名）
# --------------------------------------------------------------------------- #

# 揭示关联的承诺不存在（悬挂引用）
REVEAL_COMMITMENT_NOT_FOUND = "REVEAL_COMMITMENT_NOT_FOUND"
# 揭示关联的承诺存在，但属于另一个事件
REVEAL_COMMITMENT_EVENT_MISMATCH = "REVEAL_COMMITMENT_EVENT_MISMATCH"
# 原文 + 盐重算值 != 揭示保存的 recomputed_hash
REVEAL_HASH_MISMATCH = "REVEAL_HASH_MISMATCH"
# 原文 + 盐重算值 != 关联承诺的 commitment_hash
REVEAL_COMMITMENT_HASH_MISMATCH = "REVEAL_COMMITMENT_HASH_MISMATCH"
# 结算记录所属的事件不存在（悬挂引用）
SETTLEMENT_EVENT_NOT_FOUND = "SETTLEMENT_EVENT_NOT_FOUND"
# 重算计数与结算固化计数不一致（按计数项分开报告）
SETTLEMENT_CORRECT_COUNT_MISMATCH = "SETTLEMENT_CORRECT_COUNT_MISMATCH"
SETTLEMENT_INCORRECT_COUNT_MISMATCH = "SETTLEMENT_INCORRECT_COUNT_MISMATCH"
SETTLEMENT_UNREVEALED_COUNT_MISMATCH = "SETTLEMENT_UNREVEALED_COUNT_MISMATCH"

ALL_CODES = (
    REVEAL_COMMITMENT_NOT_FOUND,
    REVEAL_COMMITMENT_EVENT_MISMATCH,
    REVEAL_HASH_MISMATCH,
    REVEAL_COMMITMENT_HASH_MISMATCH,
    SETTLEMENT_EVENT_NOT_FOUND,
    SETTLEMENT_CORRECT_COUNT_MISMATCH,
    SETTLEMENT_INCORRECT_COUNT_MISMATCH,
    SETTLEMENT_UNREVEALED_COUNT_MISMATCH,
)

# 退出码
EXIT_OK = 0
EXIT_ANOMALIES = 1
EXIT_ERROR = 2


def snapshot_options(dialect_name: str) -> dict[str, Any]:
    """返回建立「同一一致性只读快照」所需的连接执行选项。

    PostgreSQL 上使用 ``REPEATABLE READ`` + ``READ ONLY``：快照自事务首个
    语句起固定，且数据库层面拒绝本事务的任何写入；其它方言（如测试用的
    SQLite）不加额外选项，单事务内的连续 SELECT 即构成一致视图。
    """
    if dialect_name == "postgresql":
        return {
            "isolation_level": "REPEATABLE READ",
            "postgresql_readonly": True,
        }
    return {}


def _anomaly(
    code: str,
    event_id: UUID | None,
    record_type: str,
    record_id: UUID,
    detail: dict[str, Any],
) -> dict[str, Any]:
    """构造一条异常记录（字段顺序固定，保证输出逐字节稳定）。"""
    return {
        "code": code,
        "event_id": str(event_id) if event_id is not None else None,
        "record_type": record_type,
        "record_id": str(record_id),
        "detail": detail,
    }


def audit_session(session: Session) -> dict[str, Any]:
    """在调用方建立的（只读）事务会话上执行全部核对，返回巡检报告。

    只执行 SELECT：揭示与承诺的哈希核对、结算计数的重算比较。异常按
    ``(code, event_id, record_id)`` 稳定排序，相同数据重复巡检输出一致。
    """
    events = session.scalars(select(Event)).all()
    commitments = session.scalars(select(Commitment)).all()
    reveals = session.scalars(select(Reveal)).all()
    settlements = session.scalars(select(Settlement)).all()

    commitments_by_id = {c.id: c for c in commitments}
    events_by_id = {e.id: e for e in events}
    commitment_count_by_event: dict[UUID, int] = {}
    for c in commitments:
        commitment_count_by_event[c.event_id] = (
            commitment_count_by_event.get(c.event_id, 0) + 1
        )
    reveals_by_event: dict[UUID, list[Reveal]] = {}
    for r in reveals:
        reveals_by_event.setdefault(r.event_id, []).append(r)

    anomalies: list[dict[str, Any]] = []

    # --- 揭示核对：关联承诺存在且属于该事件；重算值同时等于两处保存的哈希 ---
    for reveal in reveals:
        recomputed = compute_commitment(reveal.original_text, reveal.salt)
        commitment = commitments_by_id.get(reveal.commitment_id)
        if commitment is None:
            anomalies.append(
                _anomaly(
                    REVEAL_COMMITMENT_NOT_FOUND,
                    reveal.event_id,
                    "reveal",
                    reveal.id,
                    {"commitment_id": str(reveal.commitment_id)},
                )
            )
            # 承诺缺失时无法核对承诺哈希，但仍可核对揭示自身保存的重算值
        elif commitment.event_id != reveal.event_id:
            anomalies.append(
                _anomaly(
                    REVEAL_COMMITMENT_EVENT_MISMATCH,
                    reveal.event_id,
                    "reveal",
                    reveal.id,
                    {
                        "commitment_id": str(commitment.id),
                        "commitment_event_id": str(commitment.event_id),
                    },
                )
            )
        if recomputed != reveal.recomputed_hash:
            anomalies.append(
                _anomaly(
                    REVEAL_HASH_MISMATCH,
                    reveal.event_id,
                    "reveal",
                    reveal.id,
                    {
                        "commitment_id": str(reveal.commitment_id),
                        "stored_hash": reveal.recomputed_hash,
                        "recomputed_hash": recomputed,
                    },
                )
            )
        if commitment is not None and recomputed != commitment.commitment_hash:
            anomalies.append(
                _anomaly(
                    REVEAL_COMMITMENT_HASH_MISMATCH,
                    reveal.event_id,
                    "reveal",
                    reveal.id,
                    {
                        "commitment_id": str(commitment.id),
                        "commitment_hash": commitment.commitment_hash,
                        "recomputed_hash": recomputed,
                    },
                )
            )

    # --- 结算核对：按标准结果重算三计数，与固化计数逐项比较 ---
    for settlement in settlements:
        if settlement.event_id not in events_by_id:
            anomalies.append(
                _anomaly(
                    SETTLEMENT_EVENT_NOT_FOUND,
                    settlement.event_id,
                    "settlement",
                    settlement.id,
                    {},
                )
            )
            continue
        event_reveals = reveals_by_event.get(settlement.event_id, [])
        correct = sum(
            1 for r in event_reveals if r.original_text == settlement.standard_result
        )
        revealed_count = len(event_reveals)
        incorrect = revealed_count - correct
        unrevealed = (
            commitment_count_by_event.get(settlement.event_id, 0) - revealed_count
        )
        expected = {
            SETTLEMENT_CORRECT_COUNT_MISMATCH: ("correct_count", settlement.correct_count, correct),
            SETTLEMENT_INCORRECT_COUNT_MISMATCH: ("incorrect_count", settlement.incorrect_count, incorrect),
            SETTLEMENT_UNREVEALED_COUNT_MISMATCH: ("unrevealed_count", settlement.unrevealed_count, unrevealed),
        }
        for code, (field, stored, recomputed_count) in expected.items():
            if stored != recomputed_count:
                anomalies.append(
                    _anomaly(
                        code,
                        settlement.event_id,
                        "settlement",
                        settlement.id,
                        {
                            "field": field,
                            "stored_count": stored,
                            "recomputed_count": recomputed_count,
                        },
                    )
                )

    # 稳定排序：问题代码 -> 事件 ID -> 记录 ID（None 事件排在最后）
    anomalies.sort(
        key=lambda a: (
            a["code"],
            a["event_id"] if a["event_id"] is not None else "f" * 36,
            a["record_id"],
        )
    )

    return {
        "status": "ok" if not anomalies else "anomalies_found",
        "generated_at": clock.now_utc().isoformat(),
        "hash_scheme": HASH_SCHEME,
        "snapshot": snapshot_options(session.get_bind().dialect.name) or None,
        "summary": {
            "event_count": len(events),
            "commitment_count": len(commitments),
            "reveal_count": len(reveals),
            "settlement_count": len(settlements),
            "anomaly_count": len(anomalies),
        },
        "anomalies": anomalies,
    }


def run_audit(eng: Engine) -> dict[str, Any]:
    """建立单个只读事务并在其中完成全部核对（不改写任何业务数据）。

    会话绑定到外部连接的事务上（join external transaction）：事务边界完全
    由本函数控制，会话关闭不会提交或回滚；全部核对在同一个事务快照内完成。
    """
    options = snapshot_options(eng.dialect.name)
    with eng.connect() as conn:
        if options:
            # 须在事务开始前设置：下一条语句即进入只读一致性快照
            conn = conn.execution_options(**options)
        with conn.begin():
            with Session(bind=conn) as session:
                return audit_session(session)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.integrity_audit",
        description="只读数据完整性巡检：同一一致性只读快照内逐事件核对揭示哈希与结算计数，"
        "输出机器可读 JSON；无异常退出 0，有异常退出 1，巡检失败退出 2。",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="以缩进格式输出 JSON（默认单行紧凑输出）",
    )
    args = parser.parse_args(argv)

    try:
        report = run_audit(engine)
    except Exception as exc:  # 数据库不可达 / 缺表缺列等：非零退出，不输出半截 JSON
        print(f"integrity audit failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    indent = 2 if args.pretty else None
    json.dump(report, sys.stdout, ensure_ascii=False, indent=indent)
    sys.stdout.write("\n")
    return EXIT_OK if not report["anomalies"] else EXIT_ANOMALIES


if __name__ == "__main__":
    sys.exit(main())
