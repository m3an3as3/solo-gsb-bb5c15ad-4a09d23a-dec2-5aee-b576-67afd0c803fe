from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

COMMITMENT_HASH_PATTERN = r"^[0-9a-fA-F]{64}$"


def require_timezone_aware(v: datetime) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError("截止时间必须携带时区偏移（建议使用 UTC，如 2030-01-01T00:00:00Z）")
    return v


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    commit_deadline: datetime = Field(description="提交截止时间（须含时区，建议 UTC）")
    reveal_deadline: datetime = Field(description="揭示截止时间（须含时区，建议 UTC）")

    @field_validator("commit_deadline", "reveal_deadline")
    @classmethod
    def must_be_timezone_aware(cls, v: datetime) -> datetime:
        return require_timezone_aware(v)


class CommitmentCreate(BaseModel):
    commitment_hash: str = Field(
        pattern=COMMITMENT_HASH_PATTERN,
        description="SHA-256 承诺值，64 位十六进制",
        examples=["9f2c..."],
    )


class CommitmentBatchCreate(BaseModel):
    # 元素不在这里做 pattern 校验：格式非法是逐项冲突（409）而非请求体 422
    commitment_hashes: list[str] = Field(
        min_length=1,
        max_length=100,
        description="1～100 个 SHA-256 承诺值（64 位十六进制），按输入顺序在同一事务中原子提交",
    )


class RevealCreate(BaseModel):
    original_text: str = Field(description="预测原文（UTF-8）")
    salt: str = Field(min_length=1, max_length=256, description="随机盐（UTF-8）")


class RevealBatchItem(BaseModel):
    # 不在此处施加盐的长度约束：盐为空/超长是逐项冲突（INVALID_FORMAT，409），
    # 而非整个请求体的 422；字段类型错误或字段缺失才是请求体 422
    original_text: str = Field(description="预测原文（UTF-8）")
    salt: str = Field(description="随机盐（UTF-8，1～256 字符）")


class RevealBatchCreate(BaseModel):
    reveals: list[RevealBatchItem] = Field(
        min_length=1,
        max_length=100,
        description="1～100 组原文与盐，按输入顺序在同一事务中原子揭示",
    )


class DeadlineExtensionCreate(BaseModel):
    commit_deadline: datetime = Field(description="新的提交截止时间（须含时区，建议 UTC）")
    reveal_deadline: datetime = Field(description="新的揭示截止时间（须含时区，建议 UTC）")
    reason: str = Field(min_length=1, max_length=500, description="延长原因（记录进审计与时间线）")

    @field_validator("commit_deadline", "reveal_deadline")
    @classmethod
    def must_be_timezone_aware(cls, v: datetime) -> datetime:
        return require_timezone_aware(v)


class DeadlineExtensionOut(BaseModel):
    event_id: UUID
    old_commit_deadline: datetime
    old_reveal_deadline: datetime
    new_commit_deadline: datetime
    new_reveal_deadline: datetime
    reason: str
    extended_at: datetime


class EventOut(BaseModel):
    id: UUID
    title: str
    description: Optional[str]
    commit_deadline: datetime
    reveal_deadline: datetime
    created_at: datetime
    status: str  # commit | reveal | closed
    commitment_count: int
    reveal_count: int


class EventSnapshotOut(BaseModel):
    """事件在指定历史时刻的只读重建视图（不反映该时刻之后的任何写入）。"""

    event_id: UUID
    title: str
    description: Optional[str]
    created_at: datetime = Field(description="事件创建时间（UTC，回放的起点）")
    snapshot_at: datetime = Field(description="快照时刻（按 UTC 归一化回显，即请求中的 at）")
    commit_deadline: datetime = Field(description="快照时刻生效的提交截止时间（已回放到当时的延长）")
    reveal_deadline: datetime = Field(description="快照时刻生效的揭示截止时间（已回放到当时的延长）")
    status: str = Field(description="按快照时刻与回放截止时间判定：commit | reveal | closed")
    commitment_count: int = Field(description="快照时刻之前（含）已写入的承诺数量")
    reveal_count: int = Field(description="快照时刻之前（含）已写入的揭示数量")


class CommitmentOut(BaseModel):
    id: UUID
    event_id: UUID
    commitment_hash: str
    created_at: datetime
    revealed: bool


class CommitmentBatchOut(BaseModel):
    count: int = Field(description="本次创建的承诺数量（等于输入哈希数量）")
    commitments: list[CommitmentOut] = Field(description="按输入顺序创建的承诺记录")


class RevealOut(BaseModel):
    id: UUID
    event_id: UUID
    commitment_id: UUID
    commitment_hash: str
    recomputed_hash: str
    hash_scheme: str
    revealed_at: datetime
    verified: bool


class RevealBatchOut(BaseModel):
    count: int = Field(description="本次写入的揭示数量（等于输入组数）")
    reveals: list[RevealOut] = Field(description="按输入顺序写入的揭示记录，全部 verified=true")


class TimelineEntry(BaseModel):
    seq: int
    type: str  # event_created | commitment_submitted | reveal_accepted | deadline_extended
    at: datetime
    detail: dict[str, Any]


class TimelineOut(BaseModel):
    event_id: UUID
    status: str
    generated_at: datetime
    hash_scheme: str
    entries: list[TimelineEntry]


class SettlementCreate(BaseModel):
    standard_result: str = Field(
        min_length=1,
        description="非空标准结果（UTF-8，按揭示原文逐字符精确匹配）",
    )


class SettlementOut(BaseModel):
    event_id: UUID
    status: str = Field(description="unsettled | settled")
    standard_result: Optional[str] = Field(
        default=None,
        description="已永久确认的标准结果；未结算时为 null",
    )
    correct_count: Optional[int] = Field(
        default=None,
        description="已揭示且原文与标准结果精确匹配的承诺数；未结算时为 null",
    )
    incorrect_count: Optional[int] = Field(
        default=None,
        description="已揭示但原文与标准结果不匹配的承诺数；未结算时为 null",
    )
    unrevealed_count: Optional[int] = Field(
        default=None,
        description="从未揭示的承诺数；未结算时为 null",
    )
    settled_at: Optional[datetime] = Field(
        default=None,
        description="结算永久确认时间（UTC）；未结算时为 null",
    )


class IntervalReportEntryOut(BaseModel):
    commitment_id: UUID
    committed_at: datetime = Field(description="承诺提交时间（UTC），同宽时的排名依据之一")
    lower_bound: str = Field(description="原文中的下界片段（逐字保留）")
    upper_bound: str = Field(description="原文中的上界片段（逐字保留）")
    width: str = Field(description="区间宽度（上界 - 下界）的精确十进制表示（整数系数精确运算，无舍入；超过 4096 位时整份报告返回 422）")
    hit: bool = Field(description="区间是否覆盖标准结果（闭区间，十进制精确判定）")
    rank: Optional[int] = Field(
        description="命中者名次（从 1 开始，按宽度升序，同宽按承诺提交时间、承诺 ID）；未命中为 null"
    )


class IntervalReportInvalidOut(BaseModel):
    commitment_id: UUID
    committed_at: datetime = Field(description="承诺提交时间（UTC）")
    original_text: str = Field(description="无法解析为合法区间的揭示原文")
    reason: str = Field(
        description="无效原因：INVALID_FORMAT（不是两个有限十进制数）| LOWER_BOUND_EXCEEDS_UPPER（下界大于上界）"
    )


class IntervalReportOut(BaseModel):
    event_id: UUID
    generated_at: datetime
    standard_result: str = Field(description="已永久确认的结算标准结果（有限十进制数，原文回显）")
    settled_at: datetime = Field(description="结算永久确认时间（UTC）")
    correct_count: int = Field(description="结算时固化的正确数（既有结算计数，不被本报告修改）")
    incorrect_count: int = Field(description="结算时固化的错误数（既有结算计数，不被本报告修改）")
    unrevealed_count: int = Field(description="结算时固化的未揭示数（既有结算计数，不被本报告修改）")
    commitment_count: int = Field(description="事件承诺总数")
    revealed_count: int = Field(description="已揭示承诺数（= 合法区间数 + 无效原文数）")
    unrevealed_commitment_count: int = Field(description="从未揭示的承诺数（单独计数，不参与解析）")
    valid_interval_count: int = Field(description="成功解析为合法区间的揭示数（= 命中数 + 未命中数）")
    hit_count: int = Field(description="覆盖标准结果的区间数")
    miss_count: int = Field(description="合法但未覆盖标准结果的区间数")
    invalid_count: int = Field(description="无法解析为合法区间的揭示数")
    hits: list[IntervalReportEntryOut] = Field(
        description="命中区间，按 (宽度, 承诺提交时间, 承诺 ID) 升序排名，rank 从 1 开始"
    )
    misses: list[IntervalReportEntryOut] = Field(
        description="未命中区间，按 (承诺提交时间, 承诺 ID) 稳定排序，rank 为 null（不排名）"
    )
    invalid: list[IntervalReportInvalidOut] = Field(
        description="无效揭示原文，按 (承诺提交时间, 承诺 ID) 稳定排序"
    )


class ProbabilityCalibrationRequest(BaseModel):
    event_ids: list[UUID] = Field(
        min_length=1,
        max_length=100,
        description=(
            "待校准的 1～100 个已结算事件 ID（重复 ID 自动去重；"
            "任一事件不存在、未结算或结算标准结果不是精确的 \"0\"/\"1\" 均报错）"
        ),
    )


class ProbabilityCalibrationPredictionOut(BaseModel):
    event_id: UUID = Field(description="该预测所属事件")
    commitment_id: UUID = Field(description="该预测对应的承诺 ID")
    committed_at: datetime = Field(description="承诺提交时间（UTC）")
    original_text: str = Field(description="揭示原文逐字回显（无空白、无符号、无指数且在 [0,1] 的十进制数）")
    outcome: int = Field(description="所属事件结算标准结果对应的实际结果：0 | 1")
    squared_error: str = Field(
        description="平方误差 (概率 - 实际结果)^2 的十进制精确值，四舍六入五成双至 6 位小数"
    )


class ProbabilityCalibrationInvalidOut(BaseModel):
    event_id: UUID = Field(description="该无效揭示所属事件")
    commitment_id: UUID = Field(description="该无效揭示对应的承诺 ID")
    committed_at: datetime = Field(description="承诺提交时间（UTC）")
    original_text: str = Field(description="无法解析为 [0,1] 概率的揭示原文（逐字回显）")
    reason: str = Field(
        description="无效原因：INVALID_FORMAT（不是无空白/无符号/无指数的十进制数）| OUT_OF_RANGE（数值不在 [0,1]）"
    )


class ProbabilityCalibrationBinOut(BaseModel):
    index: int = Field(description="分箱下标（0～9，固定顺序）")
    range: str = Field(description="分箱区间标签：[0.0,0.1) … [0.8,0.9) [0.9,1.0]，边界归属确定")
    lower_bound: str = Field(description="分箱左边界（含），精确十进制")
    upper_bound: str = Field(description="分箱右边界（不含；最后一箱 [0.9,1.0] 含右端点 1）")
    count: int = Field(description="箱内有效预测样本数")
    mean: Optional[str] = Field(
        default=None,
        description="箱内预测概率均值（十进制精确计算，四舍六入五成双至 6 位小数）；空样本为 null",
    )
    positive_rate: Optional[str] = Field(
        default=None,
        description="箱内实际结果为 1 的频率（四舍六入五成双至 6 位小数）；空样本为 null",
    )


class ProbabilityCalibrationOut(BaseModel):
    generated_at: datetime
    event_count: int = Field(description="去重后实际参与校准的事件数量")
    event_ids: list[UUID] = Field(description="去重后的事件 ID（按请求中首次出现的顺序）")
    valid_prediction_count: int = Field(description="成功解析为 [0,1] 概率的揭示数")
    invalid_count: int = Field(description="无法解析为 [0,1] 概率的揭示数")
    unrevealed_commitment_count: int = Field(
        description="这些事件中从未揭示的承诺总数（单独统计，不参与解析与计分）"
    )
    brier_mean: Optional[str] = Field(
        default=None,
        description=(
            "总体 Brier 均分：全部有效预测平方误差的算术平均（十进制精确计算，"
            "四舍六入五成双至 6 位小数）；无有效预测时为 null"
        ),
    )
    predictions: list[ProbabilityCalibrationPredictionOut] = Field(
        description="有效预测，按 (事件 ID, 承诺提交时间, 承诺 ID) 稳定排序"
    )
    invalid: list[ProbabilityCalibrationInvalidOut] = Field(
        description="无效揭示原文逐条列出，按 (事件 ID, 承诺提交时间, 承诺 ID) 稳定排序"
    )
    bins: list[ProbabilityCalibrationBinOut] = Field(
        description="十个概率分箱的汇总，固定按下标 0～9 顺序返回，空样本的均值与正例频率为 null"
    )


class ConflictRequest(BaseModel):
    event_ids: list[UUID] = Field(
        default_factory=list,
        description="待分析的事件 ID 列表（重复 ID 自动去重，空列表返回空结果）",
    )


class ConflictSegmentOut(BaseModel):
    start: datetime = Field(description="片段起点（含，UTC）")
    end: datetime = Field(description="片段终点（不含，UTC，半开边界）")
    phase: str  # commit | reveal
    event_ids: list[UUID] = Field(description="该时段同时处于同一阶段的事件 ID（已排序）")


class ConflictsOut(BaseModel):
    generated_at: datetime
    phases: list[str]
    count: int = Field(description="冲突片段数量")
    segments: list[ConflictSegmentOut]


class ScheduleRequest(BaseModel):
    event_ids: list[UUID] = Field(
        default_factory=list,
        description="待排程的事件 ID 列表（重复 ID 自动去重，空列表返回空结果）",
    )
    # 不用 int 类型约束：非法上限（缺失、null、非整数、布尔、小于 1）须走统一
    # 错误结构（api_error 的 400 INVALID_TRACK_LIMIT），而不是 pydantic 的 422
    track_limit: Any = Field(default=None, description="可用 track 数（>= 1 的整数）")


class ScheduleAssignmentOut(BaseModel):
    event_id: UUID
    commit_track: Optional[int] = Field(
        description="commit 阶段分配的轨道（1 起编号）；零长度窗口为 null"
    )
    reveal_track: Optional[int] = Field(
        description="reveal 阶段分配的轨道（1 起编号，与 commit 轨道相互独立）；零长度窗口为 null"
    )


class ScheduleConflictPairOut(BaseModel):
    phase: str  # commit | reveal
    event_id_1: UUID = Field(description="冲突对中按 UUID 升序的第一个事件")
    event_id_2: UUID = Field(description="冲突对中按 UUID 升序的第二个事件")


class ScheduleOut(BaseModel):
    track_limit: int = Field(description="请求的可用 track 数")
    required_tracks: int = Field(
        description="最小所需 track 数（commit / reveal 两阶段各自最大并发深度的较大值）"
    )
    feasible: bool = Field(description="最小所需轨道数是否不超过 track_limit")
    assignments: list[ScheduleAssignmentOut] = Field(
        description="按 event_id 升序的逐事件轨道分配"
    )
    conflicts: list[ScheduleConflictPairOut] = Field(
        description="feasible=false 时导致超出上限的同阶段冲突事件对（去重排序）；feasible=true 时为空"
    )


class CommitmentReuseAuditRequest(BaseModel):
    event_ids: list[UUID] = Field(
        min_length=2,
        max_length=50,
        description="待审计的 2～50 个事件 ID（重复 ID 自动去重；任一事件不存在返回 404）",
    )


class PriorRevealSourceOut(BaseModel):
    event_id: UUID = Field(description="事先公开同一承诺的其他事件 ID")
    commitment_id: UUID = Field(description="该事件中同哈希承诺的 ID")
    reveal_id: UUID = Field(description="严格早于被审计承诺提交时间的揭示记录 ID")
    revealed_at: datetime = Field(description="揭示时间（UTC）")


class CommitmentReuseItemOut(BaseModel):
    commitment_hash: str = Field(description="按大小写归一（小写）后的承诺哈希")
    event_id: UUID = Field(description="该重复承诺所属事件")
    commitment_id: UUID = Field(description="该重复承诺的 ID")
    committed_at: datetime = Field(description="该承诺的提交时间（UTC）")
    exposed_before_commit: bool = Field(
        description=(
            "是否存在其他事件同哈希且揭示时间严格早于本承诺提交时间的揭示；"
            "同一时刻揭示不算事先公开"
        )
    )
    prior_reveal_sources: list[PriorRevealSourceOut] = Field(
        description="符合条件的其他事件揭示来源（按揭示时间、事件 ID、揭示 ID 排序）；无则为空"
    )


class CommitmentReuseAuditOut(BaseModel):
    generated_at: datetime
    event_count: int = Field(description="去重后实际参与审计的事件数量")
    duplicate_hash_count: int = Field(
        description="跨事件重复的不同承诺哈希数量（同一哈希出现在 >= 2 个事件）"
    )
    item_count: int = Field(description="重复承诺条目总数（等于 items 长度）")
    items: list[CommitmentReuseItemOut] = Field(
        description="跨事件重复承诺条目，按承诺哈希、提交时间、承诺 ID 稳定排序"
    )


class HashOut(BaseModel):
    commitment_hash: str
    hash_scheme: str


class MerkleProofStep(BaseModel):
    sibling: str = Field(
        pattern=COMMITMENT_HASH_PATTERN,
        description="该层兄弟节点哈希（64 位十六进制）",
    )
    position: Literal["left", "right"] = Field(
        description="兄弟相对当前节点的位置：left=兄弟在左（当前在右），right=兄弟在右"
    )


class EventMerkleRootOut(BaseModel):
    event_id: UUID
    merkle_root: str = Field(description="承诺集合 Merkle 根（64 位十六进制；空集合为固定空根）")
    empty_root: str = Field(description="空集合根值，便于调用方识别零承诺事件")
    commitment_count: int = Field(description="参与构造的承诺（叶）数量")
    tree_scheme: str = Field(description="Merkle 树构造方案说明")
    leaf_scheme: str = Field(description="叶哈希绑定方案说明")
    generated_at: datetime


class MerkleProofOut(BaseModel):
    event_id: UUID
    commitment_id: UUID
    commitment_hash: str
    leaf_hash: str = Field(description="该承诺对应的叶哈希（绑定事件 ID、承诺 ID 与承诺哈希）")
    leaf_index: int = Field(description="叶节点在按承诺 ID 升序排列中的下标（从 0 开始）")
    leaf_count: int = Field(description="叶节点总数（= 事件承诺数量）")
    merkle_root: str
    proof: list[MerkleProofStep] = Field(description="自叶向根逐层的兄弟哈希与左右位置")
    tree_scheme: str
    leaf_scheme: str
    generated_at: datetime


class MerkleProofVerifyRequest(BaseModel):
    event_id: UUID = Field(description="证明所属事件 ID（参与叶哈希绑定）")
    commitment_id: UUID = Field(description="待验证的承诺 ID（参与叶哈希绑定）")
    commitment_hash: str = Field(
        pattern=COMMITMENT_HASH_PATTERN,
        description="原承诺哈希（64 位十六进制；大写将归一化为小写）",
    )
    leaf_index: int = Field(ge=0, description="承诺 ID 升序叶序列中的下标（从 0 开始）")
    leaf_count: int = Field(ge=1, description="叶节点总数")
    merkle_root: str = Field(
        pattern=COMMITMENT_HASH_PATTERN, description="待比对的事件 Merkle 根"
    )
    proof: list[MerkleProofStep] = Field(
        max_length=64, description="自叶向根的兄弟哈希与左右位置（至多 64 层）"
    )


class MerkleProofVerifyOut(BaseModel):
    valid: bool = Field(description="证明是否通过叶数据、路径层级与根的全部严格校验")
    reason: Optional[str] = Field(
        default=None,
        description=(
            "校验失败原因：LEAF_INDEX_OUT_OF_RANGE / PATH_TOO_LONG / PATH_TOO_SHORT / "
            "UNEXPECTED_POSITION / DUPLICATION_MISMATCH / ROOT_MISMATCH；通过时为 null"
        ),
    )
    leaf_hash: str = Field(description="按叶方案重算的叶哈希")
    computed_root: Optional[str] = Field(
        default=None, description="按证明路径重算出的根（ROOT_MISMATCH 时仍返回以便排查）"
    )
    leaf_scheme: str
