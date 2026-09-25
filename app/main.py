"""封存并按时公开预测内容的 Commit-Reveal 服务。

生命周期（所有时间点均按服务端 UTC 判定）：
  [创建, commit_deadline)   commit 阶段：可提交承诺
  [commit_deadline, reveal_deadline)  reveal 阶段：可揭示
  [reveal_deadline, ∞)      closed：只读
"""

import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .conflicts import COMMIT, REVEAL, PhaseWindow, sweep_conflicts
from . import calibration, clock, intervals, merkle
from .database import engine, get_db
from .hashing import HASH_SCHEME, compute_commitment
from .models import Commitment, DeadlineExtension, Event, Reveal, Settlement
from .schema_bootstrap import SchemaState, ensure_schema
from .scheduling import PHASES as SCHEDULE_PHASES, schedule
from .schemas import (
    COMMITMENT_HASH_PATTERN,
    CommitmentBatchCreate,
    CommitmentBatchOut,
    CommitmentCreate,
    CommitmentOut,
    CommitmentReuseAuditOut,
    CommitmentReuseAuditRequest,
    CommitmentReuseItemOut,
    ConflictRequest,
    ConflictsOut,
    DeadlineExtensionCreate,
    DeadlineExtensionOut,
    EventCreate,
    EventMerkleRootOut,
    EventOut,
    EventSnapshotOut,
    HashOut,
    IntervalReportEntryOut,
    IntervalReportInvalidOut,
    IntervalReportOut,
    MerkleProofOut,
    MerkleProofStep,
    MerkleProofVerifyRequest,
    MerkleProofVerifyOut,
    PriorRevealSourceOut,
    ProbabilityCalibrationBinOut,
    ProbabilityCalibrationInvalidOut,
    ProbabilityCalibrationOut,
    ProbabilityCalibrationPredictionOut,
    ProbabilityCalibrationRequest,
    RevealBatchCreate,
    RevealBatchOut,
    RevealCreate,
    RevealOut,
    ScheduleConflictPairOut,
    ScheduleOut,
    ScheduleRequest,
    SettlementCreate,
    SettlementOut,
    TimelineOut,
)

# 批量提交逐项冲突类型
INVALID_FORMAT = "INVALID_FORMAT"
DUPLICATE_IN_REQUEST = "DUPLICATE_IN_REQUEST"
DUPLICATE_COMMITMENT = "DUPLICATE_COMMITMENT"
# 批量揭示逐项冲突类型
NO_MATCHING_COMMITMENT = "NO_MATCHING_COMMITMENT"
DUPLICATE_REVEAL = "DUPLICATE_REVEAL"

logger = logging.getLogger("sealed-predictions")
# uvicorn 默认只配置根 logger；为本服务 logger 补一个 INFO 级控制台输出，
# 保证启动期结构核对结果（空库初始化 / 旧库通过 / 半旧库阻止）在容器日志可见
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)


def now_utc() -> datetime:
    """服务端当前 UTC 时间。

    委托给统一时钟模块 ``app.clock``。每个写请求只在入口读取一次：该时刻
    同时用于阶段准入判定与本次记录 ``created_at`` 的显式赋值，模型默认值
    不再二次读数，避免两次读数跨过截止点导致「准入阶段与记录时间分属两个
    阶段」。测试中替换 ``app.clock.now_utc`` 即可控制全部时间来源。
    """
    return clock.now_utc()


def as_utc(dt: datetime) -> datetime:
    """把数据库读出的时间统一为带 UTC 时区的时间（兼容朴素时间）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def phase_for_deadlines(
    at: datetime, commit_deadline: datetime, reveal_deadline: datetime
) -> str:
    """按半开区间判定阶段：[创建, commit_dl) commit；[commit_dl, reveal_dl) reveal；其后 closed。"""
    if at < commit_deadline:
        return "commit"
    if at < reveal_deadline:
        return "reveal"
    return "closed"


def event_status(event: Event, now: datetime) -> str:
    return phase_for_deadlines(
        now, as_utc(event.commit_deadline), as_utc(event.reveal_deadline)
    )


def parse_snapshot_at(raw: str | None) -> datetime:
    """解析快照查询参数 at：必须是携带时区偏移的 ISO 8601 时间。

    与请求体不同，查询参数由路由自行解析，使「缺失 / 无法解析 / 无时区」
    都返回统一的业务错误 400，而不是 FastAPI 的 422。
    """
    if not raw:
        raise api_error(
            400,
            "MISSING_SNAPSHOT_TIME",
            "缺少必填查询参数 at（携带时区偏移的 ISO 8601 时间，如 2030-01-01T00:00:00Z）",
        )
    text = raw.strip()
    # datetime.fromisoformat 在 3.11 之前不接受结尾的 Z，统一改写为显式 UTC 偏移
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise api_error(
            400,
            "INVALID_SNAPSHOT_TIME",
            f"at={raw!r} 不是合法的 ISO 8601 时间（如 2030-01-01T00:00:00Z）",
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise api_error(
            400,
            "NAIVE_SNAPSHOT_TIME",
            f"at={raw!r} 缺少时区偏移，必须携带时区（如 2030-01-01T00:00:00Z 或 +08:00）",
        )
    return as_utc(parsed)


def api_error(
    status_code: int,
    code: str,
    message: str,
    conflicts: list[dict] | None = None,
) -> HTTPException:
    detail: dict = {"code": code, "message": message}
    if conflicts is not None:
        detail["conflicts"] = conflicts
    return HTTPException(status_code=status_code, detail=detail)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时核对持久库结构；数据库尚未就绪时重试，等待 Compose 中的 Postgres 起来。
    # 空库直接建表；已有应用表按模型核对全部表与必需列，缺项时不补建，
    # 仅把状态置为未就绪（见 /health 与 readiness 中间件），避免在半旧库上
    # 误报就绪、业务接口随后因缺列报 500。
    global schema_state
    attempts = 30
    for attempt in range(1, attempts + 1):
        try:
            schema_state = ensure_schema(engine)
            break
        except OperationalError:
            if attempt == attempts:
                raise
            logger.warning("database not ready, retrying (%s/%s)", attempt, attempts)
            time.sleep(1)

    if schema_state.empty_initialized:
        logger.info("database was empty: schema initialized, ready")
    elif schema_state.ready:
        logger.info("existing database schema matches models, ready (data preserved)")
    else:
        logger.error(
            "database schema is incomplete, API will not serve traffic: %s",
            schema_state.describe(),
        )
    # 就绪状态为启动核对的闩锁结果：关闭阶段不再改变它（关闭后也不会再有请求）
    yield


app = FastAPI(
    title="Sealed Predictions",
    description="封存并按时公开预测内容的 Commit-Reveal 服务",
    version="1.0.0",
    lifespan=lifespan,
)

# 启动核对完成前保持 None（视为未就绪），lifespan 中由 ensure_schema 更新
schema_state: SchemaState | None = None


def not_ready_response() -> JSONResponse:
    """构造结构核对未就绪时的统一 503 响应（含缺失表/列清单）。"""
    if schema_state is None:
        body = {
            "status": "not_ready",
            "code": "SCHEMA_NOT_READY",
            "message": "服务尚未完成启动期数据库结构核对",
            "missing_tables": [],
            "missing_columns": {},
        }
    else:
        body = {
            "status": "not_ready",
            "code": "SCHEMA_NOT_READY",
            "message": "持久库结构与当前模型不一致，API 拒绝在半旧库上就绪："
            + schema_state.describe(),
            "missing_tables": list(schema_state.missing_tables),
            "missing_columns": {
                table: list(columns)
                for table, columns in schema_state.missing_columns.items()
            },
        }
    return JSONResponse(status_code=503, content=body)


@app.middleware("http")
async def require_ready_schema(request: Request, call_next):
    """结构未就绪时阻止除 /health 之外的一切请求，返回明确的 503 而非后续 500。"""
    if schema_state is not None and schema_state.ready:
        return await call_next(request)
    if request.url.path == "/health":
        return await call_next(request)
    return not_ready_response()


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #

def get_event_or_404(db: Session, event_id: UUID) -> Event:
    event = db.get(Event, event_id)
    if event is None:
        raise api_error(404, "EVENT_NOT_FOUND", f"事件 {event_id} 不存在")
    return event


def lock_event_for_update(db: Session, event_id: UUID) -> Event:
    """取事件行级写锁并返回事件（不存在 -> 404 EVENT_NOT_FOUND）。

    单条揭示、批量揭示与永久结算共用这**同一把事件行锁**串行化：调用方
    必须在**取得锁之后**才读取那一次服务端 UTC 时刻并据此做阶段判定与写
    ``created_at``。由此消除「揭示在截止前通过阶段检查、尚未提交，截止后
    结算先把该承诺计为未揭示，随后揭示仍成功落库」的交错：

      - 揭示先取得锁：它的单次读数尚在揭示期内，正常提交后才放锁；随后
        取得锁的结算在同一事务内统计得到这条已提交的合法揭示；
      - 结算先取得锁：截止后完成固化并提交；随后取得锁的揭示读到的唯一
        一次时钟必已不早于 reveal_deadline，按 REVEAL_PHASE_CLOSED 拒绝，
        回滚且不落库任何揭示。

    锁顺序恒为「事件行 -> 承诺行」（批量揭示在事件锁内再锁承诺行），
    不存在反向加锁路径，因此不会死锁。SQLite 不支持行锁，``FOR UPDATE``
    被方言降级为普通查询（本地测试环境）；PostgreSQL（Compose 生产环境）
    上由本事件行锁与 reveals/settlements 唯一约束共同保证并发正确性。
    """
    event = db.query(Event).filter(Event.id == event_id).with_for_update().first()
    if event is None:
        raise api_error(404, "EVENT_NOT_FOUND", f"事件 {event_id} 不存在")
    return event


def event_out(event: Event, db: Session, now: datetime) -> EventOut:
    commitment_count = db.query(Commitment).filter(Commitment.event_id == event.id).count()
    reveal_count = db.query(Reveal).filter(Reveal.event_id == event.id).count()
    return EventOut(
        id=event.id,
        title=event.title,
        description=event.description,
        commit_deadline=as_utc(event.commit_deadline),
        reveal_deadline=as_utc(event.reveal_deadline),
        created_at=as_utc(event.created_at),
        status=event_status(event, now),
        commitment_count=commitment_count,
        reveal_count=reveal_count,
    )


def commitment_out(commitment: Commitment) -> CommitmentOut:
    return CommitmentOut(
        id=commitment.id,
        event_id=commitment.event_id,
        commitment_hash=commitment.commitment_hash,
        created_at=as_utc(commitment.created_at),
        revealed=commitment.reveal is not None,
    )


def extension_out(ext: DeadlineExtension) -> DeadlineExtensionOut:
    return DeadlineExtensionOut(
        event_id=ext.event_id,
        old_commit_deadline=as_utc(ext.old_commit_deadline),
        old_reveal_deadline=as_utc(ext.old_reveal_deadline),
        new_commit_deadline=as_utc(ext.new_commit_deadline),
        new_reveal_deadline=as_utc(ext.new_reveal_deadline),
        reason=ext.reason,
        extended_at=as_utc(ext.created_at),
    )


def extensions_in_chain_order(db: Session, event_id: UUID) -> list[DeadlineExtension]:
    """读取事件的全部延长审计记录，按延长链业务顺序返回。

    排序依据是延长链中**严格递增的新截止时间**
    ``(new_commit_deadline, new_reveal_deadline)``，而非记录的写入时刻与
    UUID：同一次成功延长要求两个截止时间都严格晚于当前值，因此同一事件
    的成功延长在新截止时间上构成严格递增链，新截止时间严格递增即业务上
    的发生先后。

    这避免了同一时刻（``created_at`` 相同）连续两次延长、且后一条记录的
    UUID 恰好更小时，按 ``(created_at, id)`` 排序导致的链条倒置——该倒置
    会让历史快照回退到旧截止时间，并让最新请求的幂等重放误报 409。
    在 Python 侧排序（而非数据库 ORDER BY），保证 SQLite（测试）与
    PostgreSQL（Compose）对带时区时间的比较逐字节一致。``/extend``、
    ``/snapshot`` 与 ``/timeline`` 共用本排序依据。
    """
    rows = (
        db.query(DeadlineExtension)
        .filter(DeadlineExtension.event_id == event_id)
        .all()
    )
    rows.sort(
        key=lambda e: (as_utc(e.new_commit_deadline), as_utc(e.new_reveal_deadline))
    )
    return rows


def settlement_out(event_id: UUID, settlement: Settlement | None) -> SettlementOut:
    if settlement is None:
        # 未结算时返回明确状态，其余字段均为 null
        return SettlementOut(event_id=event_id, status="unsettled")
    return SettlementOut(
        event_id=event_id,
        status="settled",
        standard_result=settlement.standard_result,
        correct_count=settlement.correct_count,
        incorrect_count=settlement.incorrect_count,
        unrevealed_count=settlement.unrevealed_count,
        settled_at=as_utc(settlement.created_at),
    )


def tally_settlement(db: Session, event_id: UUID, standard_result: str) -> tuple[int, int, int]:
    """按揭示原文与标准结果精确匹配统计 (正确, 错误, 未揭示)。

    正确 = 已揭示且 reveal.original_text 与标准结果逐字符完全相等；
    错误 = 已揭示但原文不等于标准结果；
    未揭示 = 承诺存在但从未揭示（每个承诺至多一条揭示）。
    """
    reveals = db.query(Reveal.original_text).filter(Reveal.event_id == event_id).all()
    revealed_count = len(reveals)
    correct_count = sum(1 for (text,) in reveals if text == standard_result)
    commitment_count = (
        db.query(Commitment).filter(Commitment.event_id == event_id).count()
    )
    incorrect_count = revealed_count - correct_count
    unrevealed_count = commitment_count - revealed_count
    return correct_count, incorrect_count, unrevealed_count


# --------------------------------------------------------------------------- #
# 基础端点
# --------------------------------------------------------------------------- #

@app.get("/")
def root():
    return {
        "service": "sealed-predictions",
        "docs": "/docs",
        "hash_scheme": HASH_SCHEME,
    }


@app.get("/health")
def health():
    """就绪探针：仅当启动期结构核对通过时返回 200。

    半旧库（缺表/缺列）时返回 503，并在响应中明确列出缺失的数据表名与
    每张表缺失的必需列名——Compose 的 healthcheck 据此把 API 标记为
    unhealthy，不会再出现结构不完整却报告 ok、事件接口随后 500 的情况。
    """
    if schema_state is not None and schema_state.ready:
        return {"status": "ok"}
    return not_ready_response()


@app.post("/utils/commitment", response_model=HashOut)
def compute_hash(payload: RevealCreate):
    """按约定方案在服务端计算承诺哈希，便于客户端核对本地实现。"""
    return HashOut(
        commitment_hash=compute_commitment(payload.original_text, payload.salt),
        hash_scheme=HASH_SCHEME,
    )


# --------------------------------------------------------------------------- #
# 事件
# --------------------------------------------------------------------------- #

@app.post("/events", status_code=201, response_model=EventOut)
def create_event(payload: EventCreate, db: Session = Depends(get_db)):
    commit_dl = as_utc(payload.commit_deadline)
    reveal_dl = as_utc(payload.reveal_deadline)
    # 单次读数：该时刻同时用于截止判定与事件 created_at，模型默认值不再
    # 二次读数，避免判定后时钟跨过 commit_deadline 却仍创建事件
    now = now_utc()
    if reveal_dl <= commit_dl:
        raise api_error(400, "INVALID_DEADLINES", "reveal_deadline 必须晚于 commit_deadline")
    if commit_dl <= now:
        raise api_error(400, "INVALID_DEADLINES", "commit_deadline 必须晚于当前服务端 UTC 时间")
    event = Event(
        title=payload.title,
        description=payload.description,
        commit_deadline=commit_dl,
        reveal_deadline=reveal_dl,
        created_at=now,
    )
    db.add(event)
    db.commit()
    return event_out(event, db, now)


@app.get("/events", response_model=list[EventOut])
def list_events(db: Session = Depends(get_db)):
    now = now_utc()
    events = db.query(Event).order_by(Event.created_at, Event.id).all()
    return [event_out(e, db, now) for e in events]


@app.get("/events/{event_id}", response_model=EventOut)
def get_event(event_id: UUID, db: Session = Depends(get_db)):
    return event_out(get_event_or_404(db, event_id), db, now_utc())


@app.get("/events/{event_id}/snapshot", response_model=EventSnapshotOut)
def get_event_snapshot(event_id: UUID, at: str | None = None, db: Session = Depends(get_db)):
    """按指定历史时刻重建事件视图（纯只读，不依赖服务端当前时钟）。

    回放规则（所有时间按 UTC 时刻比较，边界含 at 本身）：
      - 起点为事件创建记录；按延长链业务顺序（新截止时间严格递增，而非
        created_at 与 UUID）回放 deadline_extensions，应用其中
        created_at <= at 的延长——最近一条已生效延长的新截止时间即当时
        生效的截止时间；at 早于首次延长时，取首次延长记录中的旧值（即
        创建时的原始截止时间）；
      - status 用回放后的截止时间按常规半开语义判定：
        at < commit_deadline 为 commit，at < reveal_deadline 为 reveal，
        其后为 closed；
      - commitment_count / reveal_count 只统计 created_at <= at 的记录
        （at 之后才写入的承诺/揭示不计入）。

    错误：
      - at 缺失、无法解析为 ISO 8601 或未携带时区 -> 400
        （MISSING_SNAPSHOT_TIME / INVALID_SNAPSHOT_TIME / NAIVE_SNAPSHOT_TIME）；
      - at 早于事件创建时间 -> 400 SNAPSHOT_BEFORE_CREATION；
      - 事件不存在 -> 404 EVENT_NOT_FOUND。
    """
    snapshot_at = parse_snapshot_at(at)
    event = get_event_or_404(db, event_id)
    created_at = as_utc(event.created_at)
    if snapshot_at < created_at:
        raise api_error(
            400,
            "SNAPSHOT_BEFORE_CREATION",
            f"快照时刻 {snapshot_at.isoformat()} 早于事件创建时间 {created_at.isoformat()}，"
            "事件在该时刻尚不存在",
        )

    # 按延长链业务顺序排序的延长审计（新截止时间严格递增）：
    # 只应用 at（含）之前已写入的延长
    extensions = extensions_in_chain_order(db, event.id)
    applied = [e for e in extensions if as_utc(e.created_at) <= snapshot_at]
    if applied:
        commit_dl = as_utc(applied[-1].new_commit_deadline)
        reveal_dl = as_utc(applied[-1].new_reveal_deadline)
    else:
        # 尚未发生任何延长：首次延长记录的旧值即创建时的原始截止时间；
        # 无延长记录时回退到事件当前字段（快照场景下二者相同）
        first = extensions[0] if extensions else None
        commit_dl = as_utc(first.old_commit_deadline if first else event.commit_deadline)
        reveal_dl = as_utc(first.old_reveal_deadline if first else event.reveal_deadline)

    # 计数只含 at（含）之前已写入的记录；在 Python 侧按 UTC 时刻比较，
    # 避免不同数据库方言对带时区时间字符串比较的差异
    commitment_count = sum(
        1
        for c in db.query(Commitment.created_at)
        .filter(Commitment.event_id == event.id)
        .all()
        if as_utc(c[0]) <= snapshot_at
    )
    reveal_count = sum(
        1
        for r in db.query(Reveal.created_at)
        .filter(Reveal.event_id == event.id)
        .all()
        if as_utc(r[0]) <= snapshot_at
    )

    return EventSnapshotOut(
        event_id=event.id,
        title=event.title,
        description=event.description,
        created_at=created_at,
        snapshot_at=snapshot_at,
        commit_deadline=commit_dl,
        reveal_deadline=reveal_dl,
        status=phase_for_deadlines(snapshot_at, commit_dl, reveal_dl),
        commitment_count=commitment_count,
        reveal_count=reveal_count,
    )


@app.post("/events/{event_id}/extend", response_model=DeadlineExtensionOut)
def extend_event_deadlines(
    event_id: UUID, payload: DeadlineExtensionCreate, db: Session = Depends(get_db)
):
    """在单个事务中受约束地延长事件截止时间（仅 commit 阶段）。

    约束（全部校验通过才写入，任何失败不改变截止时间）：
      - 仅当前处于 commit 阶段可调用；进入 reveal / closed 阶段返回 409；
      - 新 commit_deadline 必须严格晚于当前值；
      - 新 reveal_deadline 必须严格晚于新 commit_deadline，且不得早于当前
        reveal_deadline —— 两个截止时间只能延长，不能缩短；
      - 与最近一次成功延长完全相同的请求（新截止时间与 reason 均相同）幂等
        返回首次延长的结果，不重复写入。
    每次成功延长都会记录一条审计（旧值/新值/原因），并出现在事件时间线中。
    """
    # 行锁串行化并发延长：校验与更新在同一事务内完成
    event = db.query(Event).filter(Event.id == event_id).with_for_update().first()
    if event is None:
        raise api_error(404, "EVENT_NOT_FOUND", f"事件 {event_id} 不存在")

    new_commit_dl = as_utc(payload.commit_deadline)
    new_reveal_dl = as_utc(payload.reveal_deadline)

    # 幂等重放：与最近一次成功延长完全相同 -> 直接返回首次结果，不再写入。
    # 「最近一次」按延长链业务顺序（新截止时间严格递增）取链尾，而非按
    # created_at/UUID 排序，保证同一时刻连续两次延长时不会认错最近记录。
    extension_chain = extensions_in_chain_order(db, event.id)
    last_extension = extension_chain[-1] if extension_chain else None
    if (
        last_extension is not None
        and as_utc(last_extension.new_commit_deadline) == new_commit_dl
        and as_utc(last_extension.new_reveal_deadline) == new_reveal_dl
        and last_extension.reason == payload.reason
    ):
        # 先在事务内把字段读到响应对象，再回滚释放行锁：状态不变
        replay = extension_out(last_extension)
        db.rollback()
        return replay

    now = now_utc()
    old_commit_dl = as_utc(event.commit_deadline)
    old_reveal_dl = as_utc(event.reveal_deadline)

    if now >= old_commit_dl:
        db.rollback()
        raise api_error(
            409,
            "EXTENSION_NOT_IN_COMMIT_PHASE",
            f"仅允许在 commit 阶段延长截止时间：提交截止时间为 {old_commit_dl.isoformat()}，"
            f"当前服务端时间 {now.isoformat()}，事件已进入 reveal/closed 阶段",
        )
    if new_commit_dl <= old_commit_dl:
        db.rollback()
        raise api_error(
            409,
            "INVALID_EXTENSION",
            f"新 commit_deadline 必须晚于当前值 {old_commit_dl.isoformat()}（只能延长，不能缩短或不变）",
        )
    if new_reveal_dl <= new_commit_dl:
        db.rollback()
        raise api_error(
            409,
            "INVALID_EXTENSION",
            "新 reveal_deadline 必须晚于新 commit_deadline",
        )
    if new_reveal_dl <= old_reveal_dl:
        db.rollback()
        raise api_error(
            409,
            "INVALID_EXTENSION",
            f"新 reveal_deadline 必须晚于当前值 {old_reveal_dl.isoformat()}（只能延长，不能缩短或不变）",
        )

    extension = DeadlineExtension(
        event_id=event.id,
        old_commit_deadline=old_commit_dl,
        old_reveal_deadline=old_reveal_dl,
        new_commit_deadline=new_commit_dl,
        new_reveal_deadline=new_reveal_dl,
        reason=payload.reason,
        created_at=now,
    )
    event.commit_deadline = new_commit_dl
    event.reveal_deadline = new_reveal_dl
    db.add(extension)
    db.commit()
    return extension_out(extension)


# --------------------------------------------------------------------------- #
# 事件窗口冲突分析
# --------------------------------------------------------------------------- #

@app.post("/events/conflicts", response_model=ConflictsOut)
def analyze_event_conflicts(payload: ConflictRequest, db: Session = Depends(get_db)):
    """用 sweep-line 计算多个事件的阶段窗口冲突。

    每个事件按服务端 UTC 视为两段半开区间：
      commit: [created_at, commit_deadline)
      reveal: [commit_deadline, reveal_deadline)

    返回任意时刻同时处于**同一**阶段（commit 或 reveal）的事件集合所
    覆盖的时间片段；片段按起止时间与阶段排序。请求中的重复 ID 去重；
    任何 ID 不存在均返回 404；空列表稳定返回空片段集。
    """
    now = now_utc()
    # 保序去重：同一事件只参与一次分析
    event_ids = list(dict.fromkeys(payload.event_ids))

    events_by_id: dict[UUID, Event] = {}
    if event_ids:
        events = db.query(Event).filter(Event.id.in_(event_ids)).all()
        events_by_id = {e.id: e for e in events}
    missing = next((eid for eid in event_ids if eid not in events_by_id), None)
    if missing is not None:
        raise api_error(404, "EVENT_NOT_FOUND", f"事件 {missing} 不存在")

    windows: list[PhaseWindow] = []
    for event in (events_by_id[eid] for eid in event_ids):
        created = as_utc(event.created_at)
        commit_dl = as_utc(event.commit_deadline)
        reveal_dl = as_utc(event.reveal_deadline)
        windows.append(PhaseWindow(event.id, COMMIT, created, commit_dl))
        windows.append(PhaseWindow(event.id, REVEAL, commit_dl, reveal_dl))

    segments = sweep_conflicts(windows)
    return ConflictsOut(
        generated_at=now,
        phases=[COMMIT, REVEAL],
        count=len(segments),
        segments=[
            {
                "start": s.start,
                "end": s.end,
                "phase": s.phase,
                "event_ids": list(s.event_ids),
            }
            for s in segments
        ],
    )


# --------------------------------------------------------------------------- #
# 事件窗口资源排程
# --------------------------------------------------------------------------- #

@app.post("/events/schedule", response_model=ScheduleOut)
def schedule_event_tracks(payload: ScheduleRequest, db: Session = Depends(get_db)):
    """为多个事件的 commit / reveal 半开窗口做最少轨道的确定性区间着色。

    每个事件按服务端 UTC 读取两段半开区间：
      commit: [created_at, commit_deadline)
      reveal: [commit_deadline, reveal_deadline)

    两个阶段各自独立分配轨道（轨道编号相互独立，均从 1 开始）：同一轨道、
    同一阶段的区间不得重叠；半开边界下首尾相接（前一区间右端点恰为后一区间
    左端点）可复用同一轨道。按 (起点, 终点, event_id) 排序的贪心着色给出最
    小轨道数（等于该阶段最大并发深度）；required_tracks 取两阶段的较大值。

    - 请求中的重复 ID 去重；任何 ID 不存在返回 404 EVENT_NOT_FOUND；
    - track_limit 必须是 >= 1 的整数，否则返回 400 INVALID_TRACK_LIMIT；
    - required_tracks > track_limit 时 feasible=false（HTTP 仍为 200），
      conflicts 给出各阶段超出上限的并发峰值中涉及的事件对（同阶段、半开
      重叠，去重并按 phase / event_id 排序）；
    - 纯只读：不修改任何事件数据；结果只取决于窗口与上限，相同输入重复
      请求结果一致。
    """
    track_limit = payload.track_limit
    # bool 是 int 的子类，须先排除；上限非法使用与其他业务错误一致的结构
    if isinstance(track_limit, bool) or not isinstance(track_limit, int):
        raise api_error(
            400,
            "INVALID_TRACK_LIMIT",
            "track_limit 必须是 >= 1 的整数",
        )
    if track_limit < 1:
        raise api_error(
            400,
            "INVALID_TRACK_LIMIT",
            f"track_limit 必须 >= 1，收到 {track_limit}",
        )

    # 保序去重：同一事件只参与一次排程
    event_ids = list(dict.fromkeys(payload.event_ids))

    events_by_id: dict[UUID, Event] = {}
    if event_ids:
        events = db.query(Event).filter(Event.id.in_(event_ids)).all()
        events_by_id = {e.id: e for e in events}
    missing = next((eid for eid in event_ids if eid not in events_by_id), None)
    if missing is not None:
        raise api_error(404, "EVENT_NOT_FOUND", f"事件 {missing} 不存在")

    windows_by_phase: dict[str, list[PhaseWindow]] = {phase: [] for phase in SCHEDULE_PHASES}
    for event in (events_by_id[eid] for eid in event_ids):
        created = as_utc(event.created_at)
        commit_dl = as_utc(event.commit_deadline)
        reveal_dl = as_utc(event.reveal_deadline)
        windows_by_phase[COMMIT].append(PhaseWindow(event.id, COMMIT, created, commit_dl))
        windows_by_phase[REVEAL].append(PhaseWindow(event.id, REVEAL, commit_dl, reveal_dl))

    result = schedule(windows_by_phase, event_ids, track_limit)
    return ScheduleOut(
        track_limit=track_limit,
        required_tracks=result.required_tracks,
        feasible=result.feasible,
        assignments=[
            {
                "event_id": a.event_id,
                "commit_track": a.commit_track,
                "reveal_track": a.reveal_track,
            }
            for a in result.assignments
        ],
        conflicts=[
            ScheduleConflictPairOut(
                phase=c.phase,
                event_id_1=c.event_id_1,
                event_id_2=c.event_id_2,
            )
            for c in result.conflicts
        ],
    )


# --------------------------------------------------------------------------- #
# 跨事件承诺复用审计
# --------------------------------------------------------------------------- #

@app.post("/events/commitments/reuse-audit", response_model=CommitmentReuseAuditOut)
def audit_commitment_reuse(payload: CommitmentReuseAuditRequest, db: Session = Depends(get_db)):
    """按大小写归一的承诺哈希审计跨事件重复承诺（纯只读）。

    接收 2～50 个事件 ID（重复 ID 去重；任一事件不存在返回 404），在这些
    事件的承诺中找出**同一归一化哈希出现在 >= 2 个不同事件**的承诺条目。
    每个条目返回所属事件、承诺 ID 与提交时间；对每个条目，仅当**其他事件**
    中同哈希承诺的揭示时间**严格早于**该条目提交时间时，标记
    ``exposed_before_commit=true`` 并在 ``prior_reveal_sources`` 中列出这些
    揭示来源（同一时刻揭示不算事先公开，同一事件内也不存在跨事件来源）。

    结果按 (commitment_hash, committed_at, commitment_id) 稳定排序；接口
    只读，不改变任何提交、揭示与结算数据。
    """
    now = now_utc()
    # 保序去重：同一事件只参与一次审计
    event_ids = list(dict.fromkeys(payload.event_ids))

    events_by_id: dict[UUID, Event] = {}
    if event_ids:
        events = db.query(Event).filter(Event.id.in_(event_ids)).all()
        events_by_id = {e.id: e for e in events}
    missing = next((eid for eid in event_ids if eid not in events_by_id), None)
    if missing is not None:
        raise api_error(404, "EVENT_NOT_FOUND", f"事件 {missing} 不存在")

    # 一次性取出这些事件的全部承诺及其揭示（每个承诺至多一条揭示）；
    # 存储时哈希已归一化为小写，这里再做一次 lower() 防御
    rows = (
        db.query(Commitment, Reveal)
        .outerjoin(Reveal, Reveal.commitment_id == Commitment.id)
        .filter(Commitment.event_id.in_(event_ids))
        .all()
    )

    # 归一化哈希 -> 该哈希在不同事件中的承诺列表
    by_hash: dict[str, list[tuple[Commitment, Reveal | None]]] = {}
    for commitment, reveal in rows:
        by_hash.setdefault(commitment.commitment_hash.lower(), []).append(
            (commitment, reveal)
        )

    items: list[CommitmentReuseItemOut] = []
    duplicate_hashes: set[str] = set()
    for norm_hash, entries in by_hash.items():
        # 仅跨事件重复才算：同一事件内该哈希至多一条（唯一约束），
        # 这里仍按事件集合判定，确保只有 >= 2 个不同事件才入列
        distinct_events = {c.event_id for c, _ in entries}
        if len(distinct_events) < 2:
            continue
        duplicate_hashes.add(norm_hash)
        for commitment, reveal in entries:
            committed_at = as_utc(commitment.created_at)
            # 其他事件中已揭示、且揭示时间严格早于本承诺提交时间的来源；
            # 等号（同刻揭示）不算事先公开
            sources = [
                PriorRevealSourceOut(
                    event_id=other.event_id,
                    commitment_id=other.id,
                    reveal_id=other_reveal.id,
                    revealed_at=as_utc(other_reveal.created_at),
                )
                for other, other_reveal in entries
                if other.event_id != commitment.event_id
                and other_reveal is not None
                and as_utc(other_reveal.created_at) < committed_at
            ]
            sources.sort(
                key=lambda s: (s.revealed_at, s.event_id, s.reveal_id)
            )
            items.append(
                CommitmentReuseItemOut(
                    commitment_hash=norm_hash,
                    event_id=commitment.event_id,
                    commitment_id=commitment.id,
                    committed_at=committed_at,
                    exposed_before_commit=bool(sources),
                    prior_reveal_sources=sources,
                )
            )

    # 稳定排序：哈希 -> 提交时间 -> 承诺 ID
    items.sort(key=lambda it: (it.commitment_hash, it.committed_at, it.commitment_id))

    return CommitmentReuseAuditOut(
        generated_at=now,
        event_count=len(event_ids),
        duplicate_hash_count=len(duplicate_hashes),
        item_count=len(items),
        items=items,
    )


# --------------------------------------------------------------------------- #
# 承诺
# --------------------------------------------------------------------------- #

@app.post("/events/{event_id}/commitments", status_code=201, response_model=CommitmentOut)
def submit_commitment(event_id: UUID, payload: CommitmentCreate, db: Session = Depends(get_db)):
    event = get_event_or_404(db, event_id)
    # 单次读数：准入判定与本条承诺 created_at 共用同一时刻，模型默认值
    # 不再二次读数，避免获准提交的记录被记到 reveal 阶段
    now = now_utc()
    commit_dl = as_utc(event.commit_deadline)
    if now >= commit_dl:
        raise api_error(
            409,
            "COMMIT_PHASE_CLOSED",
            f"提交截止时间为 {commit_dl.isoformat()}，当前服务端时间 {now.isoformat()}，迟到提交被拒绝",
        )
    commitment = Commitment(
        event_id=event.id,
        commitment_hash=payload.commitment_hash.lower(),
        created_at=now,
    )
    db.add(commitment)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise api_error(409, "DUPLICATE_COMMITMENT", "该承诺哈希已在此事件中提交过")
    return commitment_out(commitment)


@app.post(
    "/events/{event_id}/commitments/batch",
    status_code=201,
    response_model=CommitmentBatchOut,
)
def submit_commitments_batch(
    event_id: UUID, payload: CommitmentBatchCreate, db: Session = Depends(get_db)
):
    """在单个数据库事务中原子批量提交 1～100 个承诺。

    所有写入前的校验按输入顺序完成，任何一项冲突即整体回滚（不留下
    部分承诺），并返回 409 与逐项冲突清单：
      - INVALID_FORMAT：不是 64 位十六进制哈希
      - DUPLICATE_IN_REQUEST：在本次请求内部重复（第二次及以后出现的下标）
      - DUPLICATE_COMMITMENT：数据库中该事件已有此哈希
    格式非法优先于重复判定；冲突清单按下标升序，每个下标至多一项。
    全部通过时按输入顺序创建，并以相同顺序返回记录。
    """
    event = get_event_or_404(db, event_id)
    # 单次读数：准入判定与整批承诺的 created_at 共用同一时刻——批内各条
    # 记录时间戳相同，模型默认值不再逐项二次读数，避免获准提交的记录被
    # 记到 reveal 阶段；冲突依旧整体回滚
    now = now_utc()
    commit_dl = as_utc(event.commit_deadline)
    if now >= commit_dl:
        raise api_error(
            409,
            "COMMIT_PHASE_CLOSED",
            f"提交截止时间为 {commit_dl.isoformat()}，当前服务端时间 {now.isoformat()}，迟到提交被拒绝",
        )

    raw_hashes = payload.commitment_hashes
    normalized = [h.lower() for h in raw_hashes]
    conflicts: list[dict] = []

    # 1) 按输入顺序校验格式
    valid_format: list[bool] = []
    for i, h in enumerate(raw_hashes):
        ok = re.fullmatch(COMMITMENT_HASH_PATTERN, h) is not None
        valid_format.append(ok)
        if not ok:
            conflicts.append({"index": i, "type": INVALID_FORMAT, "commitment_hash": h})

    # 2) 按输入顺序检查请求内重复（仅对格式合法的项；保留首次出现）
    seen_in_request: set[str] = set()
    first_index: dict[str, int] = {}  # 归一化哈希 -> 请求内首次出现下标
    for i, norm in enumerate(normalized):
        if not valid_format[i]:
            continue
        if norm in seen_in_request:
            conflicts.append(
                {"index": i, "type": DUPLICATE_IN_REQUEST, "commitment_hash": raw_hashes[i]}
            )
            continue
        seen_in_request.add(norm)
        first_index[norm] = i

    # 3) 同事务内一次性查询数据库中已存在的哈希；仅在请求内首次出现的
    #    下标上报库冲突，后续重复已归为 DUPLICATE_IN_REQUEST
    if first_index:
        existing_rows = (
            db.query(Commitment.commitment_hash)
            .filter(
                Commitment.event_id == event.id,
                Commitment.commitment_hash.in_(list(first_index)),
            )
            .all()
        )
        existing = {row[0] for row in existing_rows}
        for norm, i in first_index.items():
            if norm in existing:
                conflicts.append(
                    {"index": i, "type": DUPLICATE_COMMITMENT, "commitment_hash": raw_hashes[i]}
                )

    if conflicts:
        # 尚未执行任何 INSERT：显式回滚以结束事务，保证不留任何写入
        db.rollback()
        conflicts.sort(key=lambda c: c["index"])
        raise api_error(
            409,
            "BATCH_COMMITMENT_CONFLICT",
            f"批量提交包含 {len(conflicts)} 个冲突项，已整体回滚，未创建任何承诺",
            conflicts=conflicts,
        )

    # 4) 全部通过：按输入顺序在同一事务中创建，created_at 共用准入时刻 now
    commitments = [
        Commitment(event_id=event.id, commitment_hash=norm, created_at=now)
        for norm in normalized
    ]
    db.add_all(commitments)
    try:
        db.commit()
    except IntegrityError:
        # 并发竞争：预检后、提交前另一事务写入了相同哈希。回滚并重新
        # 读取冲突项，依旧给出逐项清单，保证不留下部分承诺
        db.rollback()
        existing_rows = (
            db.query(Commitment.commitment_hash)
            .filter(
                Commitment.event_id == event.id,
                Commitment.commitment_hash.in_(normalized),
            )
            .all()
        )
        existing = {row[0] for row in existing_rows}
        race_conflicts = [
            {"index": i, "type": DUPLICATE_COMMITMENT, "commitment_hash": raw_hashes[i]}
            for i, norm in enumerate(normalized)
            if norm in existing
        ]
        race_conflicts.sort(key=lambda c: c["index"])
        raise api_error(
            409,
            "BATCH_COMMITMENT_CONFLICT",
            f"批量提交包含 {len(race_conflicts)} 个冲突项，已整体回滚，未创建任何承诺",
            conflicts=race_conflicts,
        )

    return CommitmentBatchOut(
        count=len(commitments),
        commitments=[commitment_out(c) for c in commitments],
    )


@app.get("/events/{event_id}/commitments", response_model=list[CommitmentOut])
def list_commitments(event_id: UUID, db: Session = Depends(get_db)):
    event = get_event_or_404(db, event_id)
    commitments = (
        db.query(Commitment)
        .filter(Commitment.event_id == event.id)
        .order_by(Commitment.created_at, Commitment.id)
        .all()
    )
    return [commitment_out(c) for c in commitments]


# --------------------------------------------------------------------------- #
# 揭示
# --------------------------------------------------------------------------- #

@app.post("/events/{event_id}/reveals", status_code=201, response_model=RevealOut)
def submit_reveal(event_id: UUID, payload: RevealCreate, db: Session = Depends(get_db)):
    # 与批量揭示、永久结算共用同一把事件行锁串行化：先取得事件行锁，再读
    # 那唯一一次服务端 UTC——若结算先持锁并在截止后完成，本请求放锁后读到
    # 的时刻必已 closed，将在下方按 REVEAL_PHASE_CLOSED 拒绝且不落库。
    event = lock_event_for_update(db, event_id)
    # 单次读数：取得事件锁后的此刻同时用于准入判定与本条揭示 created_at，
    # 模型默认值不再二次读数，避免获准揭示的记录被记到 closed 阶段
    now = now_utc()
    commit_dl = as_utc(event.commit_deadline)
    reveal_dl = as_utc(event.reveal_deadline)
    if now < commit_dl:
        db.rollback()
        raise api_error(
            409,
            "REVEAL_PHASE_NOT_STARTED",
            f"揭示期自 {commit_dl.isoformat()} 开始，当前服务端时间 {now.isoformat()}，提前揭示被拒绝",
        )
    if now >= reveal_dl:
        db.rollback()
        raise api_error(
            409,
            "REVEAL_PHASE_CLOSED",
            f"揭示截止时间为 {reveal_dl.isoformat()}，当前服务端时间 {now.isoformat()}，逾期揭示被拒绝",
        )

    recomputed = compute_commitment(payload.original_text, payload.salt)
    commitment = (
        db.query(Commitment)
        .filter(Commitment.event_id == event.id, Commitment.commitment_hash == recomputed)
        .first()
    )
    if commitment is None:
        db.rollback()
        raise api_error(
            409,
            NO_MATCHING_COMMITMENT,
            "原文与盐重算的哈希不匹配本事件中的任何承诺",
        )
    if commitment.reveal is not None:
        db.rollback()
        raise api_error(
            409,
            DUPLICATE_REVEAL,
            "该承诺已被揭示，仅首次匹配的揭示生效",
        )

    reveal = Reveal(
        event_id=event.id,
        commitment_id=commitment.id,
        original_text=payload.original_text,
        salt=payload.salt,
        recomputed_hash=recomputed,
        created_at=now,
    )
    db.add(reveal)
    try:
        db.commit()
    except IntegrityError:
        # 并发下另一请求已抢先揭示同一承诺：回滚，状态不变
        db.rollback()
        raise api_error(409, DUPLICATE_REVEAL, "该承诺已被揭示，仅首次匹配的揭示生效")

    return RevealOut(
        id=reveal.id,
        event_id=event.id,
        commitment_id=commitment.id,
        commitment_hash=commitment.commitment_hash,
        recomputed_hash=recomputed,
        hash_scheme=HASH_SCHEME,
        revealed_at=as_utc(reveal.created_at),
        verified=True,
    )


@app.post(
    "/events/{event_id}/reveals/batch",
    status_code=201,
    response_model=RevealBatchOut,
)
def submit_reveals_batch(
    event_id: UUID, payload: RevealBatchCreate, db: Session = Depends(get_db)
):
    """在单个数据库事务中原子批量揭示 1～100 组原文与盐。

    仅在 reveal 阶段可调用。所有写入前的校验按输入顺序完成，任何一项
    冲突即整体回滚（不写入任何揭示），并返回 409 与逐项冲突清单：
      - INVALID_FORMAT：盐为空或超过 256 字符
      - DUPLICATE_IN_REQUEST：重算哈希在本次请求内部重复（第二次及以后
        出现的下标）
      - NO_MATCHING_COMMITMENT：重算哈希不匹配本事件任何承诺
      - DUPLICATE_REVEAL：匹配到的承诺此前（本请求内首次出现的下标）
        已被揭示
    格式非法优先于后续判定；冲突清单按下标升序，每个下标至多一项。
    全部通过时按输入顺序在同一事务中创建，并以相同顺序返回记录。

    并发下「仅首次揭示生效」由两重机制保证：预检阶段先取**事件行锁**
    （与单条揭示、永久结算共用，按「事件行 -> 承诺行」的固定顺序再加
    承诺行锁），写入阶段由 reveals.commitment_id 唯一约束兜底——
    竞争落败的事务整体回滚，不留下部分揭示。事件行锁同时串行化揭示与
    截止后结算：截止后先完成的结算固化计数，随后取得事件锁的批量揭示
    读到的唯一次时钟必已 closed，按 REVEAL_PHASE_CLOSED 整批拒绝、回滚
    且不落库；反之先提交的合法揭示会被随后的结算统计在内。
    """
    # 先取事件行锁（与单条揭示、结算同一把锁），再读唯一一次服务端 UTC，
    # 保证与截止后结算严格串行，且不会在持锁等待期间用旧时钟做阶段判定
    event = lock_event_for_update(db, event_id)
    # 单次读数：取得事件锁后的此刻同时用于准入判定与整批揭示的 created_at
    # ——批内各条记录时间戳相同，模型默认值不再逐项二次读数，避免获准揭示
    # 的记录被记到 closed 阶段；冲突依旧整体回滚
    now = now_utc()
    commit_dl = as_utc(event.commit_deadline)
    reveal_dl = as_utc(event.reveal_deadline)
    if now < commit_dl:
        db.rollback()
        raise api_error(
            409,
            "REVEAL_PHASE_NOT_STARTED",
            f"揭示期自 {commit_dl.isoformat()} 开始，当前服务端时间 {now.isoformat()}，提前揭示被拒绝",
        )
    if now >= reveal_dl:
        db.rollback()
        raise api_error(
            409,
            "REVEAL_PHASE_CLOSED",
            f"揭示截止时间为 {reveal_dl.isoformat()}，当前服务端时间 {now.isoformat()}，逾期揭示被拒绝",
        )

    items = payload.reveals
    conflicts: list[dict] = []

    # 1) 按输入顺序校验单项格式（盐 1～256 字符）；格式非法的项不参与哈希
    valid_format: list[bool] = []
    recomputed: list[str | None] = []
    for i, item in enumerate(items):
        conflict_item = {"original_text": item.original_text, "salt": item.salt}
        salt_len = len(item.salt)
        if salt_len < 1 or salt_len > 256:
            valid_format.append(False)
            recomputed.append(None)
            conflicts.append({"index": i, "type": INVALID_FORMAT, **conflict_item})
            continue
        valid_format.append(True)
        digest = compute_commitment(item.original_text, item.salt)
        recomputed.append(digest)

    # 2) 按输入顺序检查请求内重复（仅对格式合法的项；保留首次出现）
    seen_in_request: set[str] = set()
    first_index: dict[str, int] = {}  # 重算哈希 -> 请求内首次出现下标
    for i, digest in enumerate(recomputed):
        if not valid_format[i]:
            continue
        if digest in seen_in_request:
            conflicts.append(
                {
                    "index": i,
                    "type": DUPLICATE_IN_REQUEST,
                    "original_text": items[i].original_text,
                    "salt": items[i].salt,
                    "recomputed_hash": digest,
                }
            )
            continue
        seen_in_request.add(digest)
        first_index[digest] = i

    # 3) 一次性查询命中的承诺并加行锁（PostgreSQL；SQLite 忽略 FOR UPDATE），
    #    串行化针对同一承诺的并发批量揭示；同时取该事件已有揭示的承诺集合
    commitments_by_hash: dict[str, Commitment] = {}
    revealed_commitment_ids: set[UUID] = set()
    if first_index:
        rows = (
            db.query(Commitment)
            .filter(
                Commitment.event_id == event.id,
                Commitment.commitment_hash.in_(list(first_index)),
            )
            .with_for_update()
            .all()
        )
        commitments_by_hash = {c.commitment_hash: c for c in rows}
        matched_ids = [c.id for c in commitments_by_hash.values()]
        if matched_ids:
            revealed_rows = (
                db.query(Reveal.commitment_id)
                .filter(
                    Reveal.event_id == event.id,
                    Reveal.commitment_id.in_(matched_ids),
                )
                .all()
            )
            revealed_commitment_ids = {row[0] for row in revealed_rows}

    # 仅对请求内首次出现的下标做库侧判定：无匹配优先于已揭示
    for digest, i in first_index.items():
        commitment = commitments_by_hash.get(digest)
        if commitment is None:
            conflicts.append(
                {
                    "index": i,
                    "type": NO_MATCHING_COMMITMENT,
                    "original_text": items[i].original_text,
                    "salt": items[i].salt,
                    "recomputed_hash": digest,
                }
            )
        elif commitment.id in revealed_commitment_ids:
            conflicts.append(
                {
                    "index": i,
                    "type": DUPLICATE_REVEAL,
                    "original_text": items[i].original_text,
                    "salt": items[i].salt,
                    "recomputed_hash": digest,
                }
            )

    if conflicts:
        # 尚未执行任何 INSERT：显式回滚以结束事务并释放行锁，保证不留任何写入
        db.rollback()
        conflicts.sort(key=lambda c: c["index"])
        raise api_error(
            409,
            "BATCH_REVEAL_CONFLICT",
            f"批量揭示包含 {len(conflicts)} 个冲突项，已整体回滚，未创建任何揭示",
            conflicts=conflicts,
        )

    # 4) 全部通过：按输入顺序在同一事务中创建，created_at 共用准入时刻 now
    reveals = [
        Reveal(
            event_id=event.id,
            commitment_id=commitments_by_hash[digest].id,
            original_text=items[i].original_text,
            salt=items[i].salt,
            recomputed_hash=digest,
            created_at=now,
        )
        for i, digest in enumerate(recomputed)
    ]
    db.add_all(reveals)
    try:
        db.commit()
    except IntegrityError:
        # 并发竞争：预检后、提交前另一事务抢先揭示了其中某个承诺。
        # 唯一约束使整批插入失败：回滚并重新读取，依旧给出逐项清单，
        # 保证不留下部分揭示（落败批次内第二次及以后出现的同哈希项
        # 仍归为 DUPLICATE_IN_REQUEST）
        db.rollback()
        matched_rows = (
            db.query(Commitment)
            .filter(
                Commitment.event_id == event.id,
                Commitment.commitment_hash.in_(list(seen_in_request)),
            )
            .all()
        )
        matched_by_hash = {c.commitment_hash: c for c in matched_rows}
        race_revealed_rows = (
            db.query(Reveal.commitment_id)
            .filter(
                Reveal.event_id == event.id,
                Reveal.commitment_id.in_([c.id for c in matched_by_hash.values()]),
            )
            .all()
        )
        race_revealed = {row[0] for row in race_revealed_rows}
        race_conflicts: list[dict] = []
        race_seen: set[str] = set()
        for i, digest in enumerate(recomputed):
            if not valid_format[i]:
                continue
            if digest in race_seen:
                race_conflicts.append(
                    {
                        "index": i,
                        "type": DUPLICATE_IN_REQUEST,
                        "original_text": items[i].original_text,
                        "salt": items[i].salt,
                        "recomputed_hash": digest,
                    }
                )
                continue
            race_seen.add(digest)
            commitment = matched_by_hash.get(digest)
            if commitment is not None and commitment.id in race_revealed:
                race_conflicts.append(
                    {
                        "index": i,
                        "type": DUPLICATE_REVEAL,
                        "original_text": items[i].original_text,
                        "salt": items[i].salt,
                        "recomputed_hash": digest,
                    }
                )
        race_conflicts.sort(key=lambda c: c["index"])
        raise api_error(
            409,
            "BATCH_REVEAL_CONFLICT",
            f"批量揭示包含 {len(race_conflicts)} 个冲突项，已整体回滚，未创建任何揭示",
            conflicts=race_conflicts,
        )

    return RevealBatchOut(
        count=len(reveals),
        reveals=[
            RevealOut(
                id=reveal.id,
                event_id=event.id,
                commitment_id=commitments_by_hash[digest].id,
                commitment_hash=digest,
                recomputed_hash=digest,
                hash_scheme=HASH_SCHEME,
                revealed_at=as_utc(reveal.created_at),
                verified=True,
            )
            for reveal, digest in zip(reveals, recomputed)
        ],
    )


# --------------------------------------------------------------------------- #
# 结算
# --------------------------------------------------------------------------- #

@app.post("/events/{event_id}/settlement", response_model=SettlementOut)
def settle_event(event_id: UUID, payload: SettlementCreate, db: Session = Depends(get_db)):
    """事件进入 closed 后永久确认标准结果。

    在单个事务中完成全部校验与写入：
      - 仅事件进入 closed 阶段（now >= reveal_deadline）后可调用，
        commit / reveal 阶段返回 409 SETTLEMENT_NOT_IN_CLOSED_PHASE；
      - 请求体 standard_result 必须为非空字符串（空串 / 缺失 / 非字符串 422）；
      - 服务按每条揭示的 original_text 与标准结果**逐字符精确匹配**
        （区分大小写、不做任何空白裁剪或归一化），在写入时固化
        correct_count / incorrect_count / unrevealed_count：
        正确 = 已揭示且原文 == 标准结果；错误 = 已揭示但不相等；
        未揭示 = 承诺存在但从未揭示；三者之和等于承诺总数；
      - 结算一经确认永久有效：重复结算（无论标准结果是否相同）一律返回
        409 SETTLEMENT_ALREADY_CONFIRMED，**既有结果不被改写**，响应在
        detail.existing 中回显首次结算；并发下由 settlements.event_id
        唯一约束兜底。

    与单条 / 批量揭示共用同一把事件行锁：先 ``SELECT ... FOR UPDATE``
    取得事件行锁，再读唯一一次服务端 UTC 做阶段判定与结算 created_at，
    并在同一事务内统计揭示。因此截止边界上与揭示严格串行——先取得锁的
    揭示若按当时（揭示期内）读数获准，必会在本事务统计前提交并被计入；
    本结算先取得锁时，其后取得锁的揭示读到的时钟必已不早于
    reveal_deadline，会被拒绝且不落库。固化计数与存库揭示永不矛盾。
    """
    # 行锁串行化并发结算，并与两种揭示入口串行：校验与写入在同一事务内完成
    event = lock_event_for_update(db, event_id)

    existing = (
        db.query(Settlement).filter(Settlement.event_id == event.id).one_or_none()
    )
    if existing is not None:
        # 既有结果永久确认：回滚释放行锁，返回冲突且不改写任何字段
        replay = settlement_out(event.id, existing)
        db.rollback()
        raise api_error(
            409,
            "SETTLEMENT_ALREADY_CONFIRMED",
            f"事件 {event_id} 已于 {replay.settled_at.isoformat()} 结算，"
            "结算结果永久确认，重复结算被拒绝且不会改写既有结果",
            conflicts=[{"existing": replay.model_dump(mode="json")}],
        )

    now = now_utc()
    reveal_dl = as_utc(event.reveal_deadline)
    if now < reveal_dl:
        db.rollback()
        raise api_error(
            409,
            "SETTLEMENT_NOT_IN_CLOSED_PHASE",
            f"仅允许在事件进入 closed 阶段后结算：揭示截止时间为 {reveal_dl.isoformat()}，"
            f"当前服务端时间 {now.isoformat()}，事件仍处于 commit/reveal 阶段",
        )

    correct, incorrect, unrevealed = tally_settlement(db, event.id, payload.standard_result)
    settlement = Settlement(
        event_id=event.id,
        standard_result=payload.standard_result,
        correct_count=correct,
        incorrect_count=incorrect,
        unrevealed_count=unrevealed,
        created_at=now,
    )
    db.add(settlement)
    try:
        db.commit()
    except IntegrityError:
        # 并发竞争：预检后、提交前另一事务已完成结算。回滚并读取首次结算，
        # 依旧返回冲突，既有结果不变
        db.rollback()
        winner = db.query(Settlement).filter(Settlement.event_id == event.id).one()
        replay = settlement_out(event.id, winner)
        raise api_error(
            409,
            "SETTLEMENT_ALREADY_CONFIRMED",
            f"事件 {event_id} 已于 {replay.settled_at.isoformat()} 结算，"
            "结算结果永久确认，重复结算被拒绝且不会改写既有结果",
            conflicts=[{"existing": replay.model_dump(mode="json")}],
        )

    return settlement_out(event.id, settlement)


@app.get("/events/{event_id}/settlement", response_model=SettlementOut)
def get_settlement(event_id: UUID, db: Session = Depends(get_db)):
    """查询事件的结算状态（纯只读）。

    未结算时返回 200 与明确状态 ``status="unsettled"``（计数与标准结果
    均为 null）；已结算时返回 ``status="settled"``、永久确认的标准结果
    与固化的正确 / 错误 / 未揭示数量。事件不存在返回 404。
    """
    event = get_event_or_404(db, event_id)
    settlement = (
        db.query(Settlement).filter(Settlement.event_id == event.id).one_or_none()
    )
    return settlement_out(event.id, settlement)


# --------------------------------------------------------------------------- #
# 数值区间预测报告
# --------------------------------------------------------------------------- #

@app.get("/events/{event_id}/interval-report", response_model=IntervalReportOut)
def get_interval_report(event_id: UUID, db: Session = Depends(get_db)):
    """对已结算事件生成数值区间预测报告（纯只读，不修改任何数据）。

    - 仅已结算事件可生成：未结算返回 409 INTERVAL_REPORT_NOT_SETTLED；
    - 结算标准结果必须是有限十进制数，否则返回
      422 STANDARD_RESULT_NOT_FINITE_DECIMAL；
    - 每条已揭示原文按「下界..上界」严格解析为两个有限十进制数
      （不允许空白 / Infinity / NaN；下界不得大于上界），解析失败者
      单独列入 invalid 并注明原因；从未揭示的承诺单独计数；
    - 覆盖判定为闭区间（下界 <= 标准结果 <= 上界），用 decimal.Decimal
      精确计算，不经过浮点；区间宽度 = 上界 - 下界，用整数系数运算
      精确求得（不受默认 Decimal 28 位精度影响，末位不丢）；
    - 命中者按 (真实区间宽度, 承诺提交时间, 承诺 ID) 升序排名（rank 从 1
      开始）；未命中者按 (承诺提交时间, 承诺 ID) 列出但不排名
      （rank 为 null）；
    - 任一合法区间的精确宽度有效数字位数超过
      intervals.MAX_WIDTH_DIGITS（4096）时，整份报告返回
      422 INTERVAL_WIDTH_TOO_LARGE，不静默舍入也不返回 500；
    - 响应回显结算时固化的 correct / incorrect / unrevealed 计数，
      既有结算结果不被本接口修改。
    """
    event = get_event_or_404(db, event_id)
    settlement = (
        db.query(Settlement).filter(Settlement.event_id == event.id).one_or_none()
    )
    if settlement is None:
        raise api_error(
            409,
            "INTERVAL_REPORT_NOT_SETTLED",
            f"数值区间报告仅对已结算事件生成：事件 {event_id} 尚未结算",
        )

    standard = intervals.parse_finite_decimal(settlement.standard_result)
    if standard is None:
        raise api_error(
            422,
            "STANDARD_RESULT_NOT_FINITE_DECIMAL",
            f"结算标准结果 {settlement.standard_result!r} 不是有限十进制数，"
            "无法生成数值区间报告",
        )

    rows = (
        db.query(Commitment, Reveal)
        .outerjoin(Reveal, Reveal.commitment_id == Commitment.id)
        .filter(Commitment.event_id == event.id)
        .all()
    )
    revealed = [
        (commitment.id, as_utc(commitment.created_at), reveal.original_text)
        for commitment, reveal in rows
        if reveal is not None
    ]
    unrevealed_count = sum(1 for _, reveal in rows if reveal is None)
    try:
        report = intervals.build_interval_report(standard, revealed, unrevealed_count)
    except intervals.WidthTooLargeError:
        # 区间合法但精确宽度位数超过 4096：明确拒绝，绝不静默舍入，
        # 也避免在指数跨度过大时物化超大整数导致 500
        raise api_error(
            422,
            "INTERVAL_WIDTH_TOO_LARGE",
            "存在精确宽度有效数字位数超过 "
            f"{intervals.MAX_WIDTH_DIGITS} 位的合法区间，无法在不舍入的前提下"
            "生成数值区间报告",
        )

    def entry_out(entry: intervals.IntervalEntry, rank: int | None) -> IntervalReportEntryOut:
        return IntervalReportEntryOut(
            commitment_id=entry.commitment_id,
            committed_at=entry.committed_at,
            lower_bound=entry.lower_text,
            upper_bound=entry.upper_text,
            width=str(entry.width),
            hit=entry.hit,
            rank=rank,
        )

    return IntervalReportOut(
        event_id=event.id,
        generated_at=now_utc(),
        standard_result=settlement.standard_result,
        settled_at=as_utc(settlement.created_at),
        correct_count=settlement.correct_count,
        incorrect_count=settlement.incorrect_count,
        unrevealed_count=settlement.unrevealed_count,
        commitment_count=len(rows),
        revealed_count=len(revealed),
        unrevealed_commitment_count=report.unrevealed_count,
        valid_interval_count=len(report.hits) + len(report.misses),
        hit_count=len(report.hits),
        miss_count=len(report.misses),
        invalid_count=len(report.invalid),
        hits=[entry_out(e, i + 1) for i, e in enumerate(report.hits)],
        misses=[entry_out(e, None) for e in report.misses],
        invalid=[
            IntervalReportInvalidOut(
                commitment_id=e.commitment_id,
                committed_at=e.committed_at,
                original_text=e.original_text,
                reason=e.reason,
            )
            for e in report.invalid
        ],
    )


# --------------------------------------------------------------------------- #
# 跨事件概率校准报告
# --------------------------------------------------------------------------- #

# 校准报告只接受结算标准结果为精确 "0" / "1" 的事件
BINARY_STANDARD_RESULTS = {"0", "1"}


@app.post("/events/probability-calibration-report", response_model=ProbabilityCalibrationOut)
def probability_calibration_report(
    payload: ProbabilityCalibrationRequest, db: Session = Depends(get_db)
):
    """对 1～100 个已结算二元事件生成跨事件概率校准报告（纯只读）。

    输入与前置校验（重复 ID 保序去重，按去重后顺序找到第一个不满足条件
    的事件即报错）：
      - 事件 ID 数量为 1～100（请求体层面 422），重复 ID 去重；
      - 任一事件不存在 -> 404 EVENT_NOT_FOUND；
      - 任一事件未结算 -> 409 CALIBRATION_EVENT_NOT_SETTLED；
      - 任一事件的结算标准结果不是逐字符精确的 ``"0"`` 或 ``"1"``
        -> 422 CALIBRATION_STANDARD_RESULT_NOT_BINARY。

    通过校验后读取这些事件的全部承诺与揭示：
      - 每条已揭示原文按严格语法解析为概率：仅接受**无空白、无符号、
        无指数**且位于闭区间 [0, 1] 的十进制数；解析失败者逐条列入
        ``invalid``（INVALID_FORMAT / OUT_OF_RANGE），不参与计分；
      - 从未揭示的承诺单独计入 ``unrevealed_commitment_count``；
      - 每条有效预测的实际结果取所属事件结算标准结果（"0"->0, "1"->1），
        用 ``decimal.Decimal`` 精确计算平方误差 (p-y)^2；Brier 均分为全部
        有效预测平方误差的算术平均（无有效预测时为 null）；
      - 按 [0,0.1)、[0.1,0.2)、…、[0.8,0.9)、[0.9,1] 十个区间分箱
        （边界 k/10 归入右侧箱，端点 1 在最后一箱），逐箱汇总样本数、
        预测均值与实际为 1 的频率；空样本均值与频率均为 null。

    结果字段按固定顺序返回；所有计算得到的数值统一四舍六入五成双
    （ROUND_HALF_EVEN）量化到 6 位小数字符串。接口不修改任何数据。
    """
    # 保序去重：同一事件只参与一次校准
    event_ids = list(dict.fromkeys(payload.event_ids))

    events = db.query(Event).filter(Event.id.in_(event_ids)).all()
    events_by_id = {e.id: e for e in events}
    missing = next((eid for eid in event_ids if eid not in events_by_id), None)
    if missing is not None:
        raise api_error(404, "EVENT_NOT_FOUND", f"事件 {missing} 不存在")

    settlements = (
        db.query(Settlement).filter(Settlement.event_id.in_(event_ids)).all()
    )
    settlements_by_event = {s.event_id: s for s in settlements}

    # 按去重后顺序依次校验「已结算」与「标准结果精确为 0/1」，首个违规即报错
    for eid in event_ids:
        settlement = settlements_by_event.get(eid)
        if settlement is None:
            raise api_error(
                409,
                "CALIBRATION_EVENT_NOT_SETTLED",
                f"概率校准报告仅对已结算事件生成：事件 {eid} 尚未结算",
            )
        if settlement.standard_result not in BINARY_STANDARD_RESULTS:
            raise api_error(
                422,
                "CALIBRATION_STANDARD_RESULT_NOT_BINARY",
                f"事件 {eid} 的结算标准结果 {settlement.standard_result!r} 不是精确的 "
                '"0" 或 "1"，无法参与概率校准',
            )

    # 一次性取出这些事件的全部承诺及揭示（每个承诺至多一条揭示）
    rows = (
        db.query(Commitment, Reveal)
        .outerjoin(Reveal, Reveal.commitment_id == Commitment.id)
        .filter(Commitment.event_id.in_(event_ids))
        .all()
    )

    sources: list[calibration.CalibrationSource] = []
    unrevealed_count = 0
    for commitment, reveal in rows:
        if reveal is None:
            unrevealed_count += 1
            continue
        outcome = int(settlements_by_event[commitment.event_id].standard_result)
        sources.append(
            calibration.CalibrationSource(
                event_id=commitment.event_id,
                commitment_id=commitment.id,
                committed_at=as_utc(commitment.created_at),
                original_text=reveal.original_text,
                outcome=outcome,
            )
        )

    report = calibration.build_calibration_report(sources, unrevealed_count)

    return ProbabilityCalibrationOut(
        generated_at=now_utc(),
        event_count=len(event_ids),
        event_ids=event_ids,
        valid_prediction_count=len(report.predictions),
        invalid_count=len(report.invalid),
        unrevealed_commitment_count=report.unrevealed_count,
        brier_mean=(
            f"{calibration.quantize6(report.brier_mean):f}"
            if report.brier_mean is not None
            else None
        ),
        predictions=[
            ProbabilityCalibrationPredictionOut(
                event_id=p.event_id,
                commitment_id=p.commitment_id,
                committed_at=p.committed_at,
                original_text=p.original_text,
                outcome=p.outcome,
                squared_error=f"{calibration.quantize6(p.squared_error):f}",
            )
            for p in report.predictions
        ],
        invalid=[
            ProbabilityCalibrationInvalidOut(
                event_id=p.event_id,
                commitment_id=p.commitment_id,
                committed_at=p.committed_at,
                original_text=p.original_text,
                reason=p.reason,
            )
            for p in report.invalid
        ],
        bins=[
            ProbabilityCalibrationBinOut(
                index=b.index,
                range=(
                    f"[{b.lower:.1f},{b.upper:.1f})"
                    if b.index < calibration.BIN_COUNT - 1
                    else f"[{b.lower:.1f},{b.upper:.1f}]"
                ),
                lower_bound=f"{b.lower:.1f}",
                upper_bound=f"{b.upper:.1f}",
                count=b.count,
                mean=(
                    f"{calibration.quantize6(b.mean):f}" if b.mean is not None else None
                ),
                positive_rate=(
                    f"{calibration.quantize6(b.positive_rate):f}"
                    if b.positive_rate is not None
                    else None
                ),
            )
            for b in report.bins
        ],
    )


# --------------------------------------------------------------------------- #
# 事件承诺集合证明（Merkle）
# --------------------------------------------------------------------------- #

def load_event_commitments_ordered(db: Session, event_id: UUID) -> list[Commitment]:
    """读取事件全部承诺并按承诺 ID（UUID 128 位整数）升序返回。

    在 Python 侧排序而非依赖数据库 ORDER BY，保证 SQLite（测试）与
    PostgreSQL（Compose）对 UUID 的排序逐字节一致。
    """
    rows = db.query(Commitment).filter(Commitment.event_id == event_id).all()
    rows.sort(key=lambda c: c.id.int)
    return rows


def event_leaves(event: Event, commitments: list[Commitment]) -> list[bytes]:
    """按承诺 ID 升序构造叶哈希列表（叶哈希绑定事件 ID、承诺 ID 与承诺哈希）。"""
    return [
        merkle.hash_commitment_leaf(event.id, c.id, c.commitment_hash) for c in commitments
    ]


def ensure_commitment_phase_finalized(event: Event, now: datetime) -> None:
    """提交截止（now >= commit_deadline）后才可签发集合证明；否则 409。"""
    commit_dl = as_utc(event.commit_deadline)
    if now < commit_dl:
        raise api_error(
            409,
            "PROOF_NOT_AVAILABLE",
            f"承诺集合证明仅在提交截止后签发：提交截止时间为 {commit_dl.isoformat()}，"
            f"当前服务端时间 {now.isoformat()}，提交通道仍开放，集合尚未冻结",
        )


def proof_step_out(step: merkle.ProofStep) -> MerkleProofStep:
    return MerkleProofStep(sibling=step.sibling.hex(), position=step.position)


@app.get("/events/{event_id}/commitments/proof/root", response_model=EventMerkleRootOut)
def get_event_commitment_root(event_id: UUID, db: Session = Depends(get_db)):
    """提交截止后返回事件承诺集合的 Merkle 根与承诺数量（纯只读）。

    叶节点按承诺 ID 升序，叶哈希绑定事件 ID、承诺 ID 与原 commitment_hash；
    内部节点与叶节点使用不同域前缀，奇数层末项复制后配对。空集合返回
    明确的固定空根与数量 0。未过提交截止时不签发（409 PROOF_NOT_AVAILABLE）；
    事件不存在返回 404 EVENT_NOT_FOUND。
    """
    event = get_event_or_404(db, event_id)
    now = now_utc()
    ensure_commitment_phase_finalized(event, now)
    commitments = load_event_commitments_ordered(db, event.id)
    root = merkle.merkle_root(event_leaves(event, commitments))
    return EventMerkleRootOut(
        event_id=event.id,
        merkle_root=root.hex(),
        empty_root=merkle.EMPTY_ROOT.hex(),
        commitment_count=len(commitments),
        tree_scheme=merkle.TREE_SCHEME,
        leaf_scheme=merkle.LEAF_SCHEME,
        generated_at=now,
    )


@app.get(
    "/events/{event_id}/commitments/{commitment_id}/proof",
    response_model=MerkleProofOut,
)
def get_commitment_proof(
    event_id: UUID, commitment_id: UUID, db: Session = Depends(get_db)
):
    """提交截止后为指定 commitment_id 签发成员证明（纯只读）。

    返回事件 Merkle 根、承诺数量、该叶的下标与自叶向根的逐层兄弟哈希及
    左右位置，调用方可配合 ``/utils/merkle-proof/verify`` 本地校验，无需
    下载完整时间线。未过提交截止返回 409 PROOF_NOT_AVAILABLE；事件不存在
    返回 404 EVENT_NOT_FOUND；承诺不存在或不属于该事件返回
    404 COMMITMENT_NOT_FOUND。
    """
    event = get_event_or_404(db, event_id)
    now = now_utc()
    ensure_commitment_phase_finalized(event, now)

    commitments = load_event_commitments_ordered(db, event.id)
    target_index = next(
        (i for i, c in enumerate(commitments) if c.id == commitment_id), None
    )
    if target_index is None:
        raise api_error(
            404,
            "COMMITMENT_NOT_FOUND",
            f"承诺 {commitment_id} 不存在于事件 {event_id} 中",
        )

    leaves = event_leaves(event, commitments)
    steps = merkle.build_proof(leaves, target_index)
    target = commitments[target_index]
    return MerkleProofOut(
        event_id=event.id,
        commitment_id=target.id,
        commitment_hash=target.commitment_hash,
        leaf_hash=leaves[target_index].hex(),
        leaf_index=target_index,
        leaf_count=len(commitments),
        merkle_root=merkle.merkle_root(leaves).hex(),
        proof=[proof_step_out(s) for s in steps],
        tree_scheme=merkle.TREE_SCHEME,
        leaf_scheme=merkle.LEAF_SCHEME,
        generated_at=now,
    )


@app.post("/utils/merkle-proof/verify", response_model=MerkleProofVerifyOut)
def verify_commitment_proof(payload: MerkleProofVerifyRequest):
    """本地校验成员证明：对叶数据、路径层级与根做严格校验（纯计算，不落库）。

    校验内容：
      - 按叶方案重算叶哈希（绑定事件 ID、承诺 ID 与 commitment_hash）；
      - 依据 leaf_count / leaf_index 重放每层配对结构，核对路径长度恰好
        收敛到根（PATH_TOO_LONG / PATH_TOO_SHORT）、每层兄弟左右位置
        （UNEXPECTED_POSITION）、奇数层末项复制时兄弟必须等于当前节点
        （DUPLICATION_MISMATCH）；
      - 重算根与提供的 merkle_root 逐字节比较（ROOT_MISMATCH）。
    全部通过返回 200 与 ``valid=true``；结构或根不符返回 200 与
    ``valid=false`` 及明确 ``reason``；哈希格式、层数上限等请求体格式
    问题返回 422。此接口不查询数据库，也不做阶段判定。
    """
    steps = [
        merkle.ProofStep(sibling=bytes.fromhex(s.sibling), position=s.position)
        for s in payload.proof
    ]
    result = merkle.verify_membership(
        event_id=payload.event_id,
        commitment_id=payload.commitment_id,
        commitment_hash=payload.commitment_hash.lower(),
        leaf_index=payload.leaf_index,
        leaf_count=payload.leaf_count,
        steps=steps,
        expected_root=bytes.fromhex(payload.merkle_root.lower()),
    )
    return MerkleProofVerifyOut(
        valid=result.valid,
        reason=result.reason,
        leaf_hash=result.leaf_hash.hex(),
        computed_root=(
            result.computed_root.hex() if result.computed_root is not None else None
        ),
        leaf_scheme=merkle.LEAF_SCHEME,
    )


# --------------------------------------------------------------------------- #
# 时间线
# --------------------------------------------------------------------------- #

@app.get("/events/{event_id}/timeline", response_model=TimelineOut)
def get_timeline(event_id: UUID, db: Session = Depends(get_db)):
    event = get_event_or_404(db, event_id)
    now = now_utc()

    # 按延长链业务顺序排序（新截止时间严格递增）：同一时刻连续多次延长时，
    # 时间线中的同刻记录顺序与延长链一致，不会被随机 UUID 顺序倒置
    extensions = extensions_in_chain_order(db, event.id)
    # event_created 展示事件创建时的原始截止时间：若发生过延长，取首次延长的旧值
    if extensions:
        created_commit_dl = as_utc(extensions[0].old_commit_deadline)
        created_reveal_dl = as_utc(extensions[0].old_reveal_deadline)
    else:
        created_commit_dl = as_utc(event.commit_deadline)
        created_reveal_dl = as_utc(event.reveal_deadline)

    entries: list[dict] = [
        {
            "type": "event_created",
            "at": as_utc(event.created_at),
            "detail": {
                "title": event.title,
                "commit_deadline": created_commit_dl.isoformat(),
                "reveal_deadline": created_reveal_dl.isoformat(),
            },
        }
    ]

    for ext in extensions:
        entries.append(
            {
                "type": "deadline_extended",
                "at": as_utc(ext.created_at),
                "detail": {
                    "old_commit_deadline": as_utc(ext.old_commit_deadline).isoformat(),
                    "old_reveal_deadline": as_utc(ext.old_reveal_deadline).isoformat(),
                    "new_commit_deadline": as_utc(ext.new_commit_deadline).isoformat(),
                    "new_reveal_deadline": as_utc(ext.new_reveal_deadline).isoformat(),
                    "reason": ext.reason,
                },
            }
        )

    commitments = (
        db.query(Commitment)
        .filter(Commitment.event_id == event.id)
        .order_by(Commitment.created_at, Commitment.id)
        .all()
    )
    reveals = {r.commitment_id: r for r in db.query(Reveal).filter(Reveal.event_id == event.id)}

    for c in commitments:
        entries.append(
            {
                "type": "commitment_submitted",
                "at": as_utc(c.created_at),
                "detail": {
                    "commitment_id": str(c.id),
                    "commitment_hash": c.commitment_hash,
                },
            }
        )
        r = reveals.get(c.id)
        if r is not None:
            entries.append(
                {
                    "type": "reveal_accepted",
                    "at": as_utc(r.created_at),
                    "detail": {
                        "commitment_id": str(c.id),
                        "commitment_hash": c.commitment_hash,
                        "recomputed_hash": r.recomputed_hash,
                        "original_text": r.original_text,
                        "salt": r.salt,
                        "hash_scheme": HASH_SCHEME,
                    },
                }
            )

    settlement = db.query(Settlement).filter(Settlement.event_id == event.id).one_or_none()
    if settlement is not None:
        entries.append(
            {
                "type": "settlement_confirmed",
                "at": as_utc(settlement.created_at),
                "detail": {
                    "standard_result": settlement.standard_result,
                    "correct_count": settlement.correct_count,
                    "incorrect_count": settlement.incorrect_count,
                    "unrevealed_count": settlement.unrevealed_count,
                },
            }
        )

    # 稳定排序：同一时间戳下保持 承诺 -> 揭示 的先后
    entries.sort(key=lambda e: e["at"])
    timeline = [
        {"seq": i + 1, "type": e["type"], "at": e["at"], "detail": e["detail"]}
        for i, e in enumerate(entries)
    ]
    return TimelineOut(
        event_id=event.id,
        status=event_status(event, now),
        generated_at=now,
        hash_scheme=HASH_SCHEME,
        entries=timeline,
    )
