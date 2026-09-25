import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from . import clock
from .database import Base


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    commit_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reveal_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: clock.now_utc())

    commitments: Mapped[list["Commitment"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )

    settlement: Mapped[Optional["Settlement"]] = relationship(
        back_populates="event", uselist=False
    )


class Commitment(Base):
    __tablename__ = "commitments"
    __table_args__ = (
        UniqueConstraint("event_id", "commitment_hash", name="uq_commitments_event_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    commitment_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: clock.now_utc())

    event: Mapped[Event] = relationship(back_populates="commitments")
    reveal: Mapped[Optional["Reveal"]] = relationship(back_populates="commitment", uselist=False)


class DeadlineExtension(Base):
    """一次成功截止时间延长的审计记录（旧值/新值/原因），同时用于幂等重放判定。"""

    __tablename__ = "deadline_extensions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    old_commit_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    old_reveal_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    new_commit_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    new_reveal_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: clock.now_utc())


class Reveal(Base):
    __tablename__ = "reveals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    # 每个承诺至多被揭示一次：唯一约束保证并发下「仅首次匹配的揭示生效」
    commitment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("commitments.id", ondelete="CASCADE"), unique=True
    )
    original_text: Mapped[str] = mapped_column(Text)
    salt: Mapped[str] = mapped_column(Text)
    recomputed_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: clock.now_utc())

    commitment: Mapped[Commitment] = relationship(back_populates="reveal")


class Settlement(Base):
    """事件的永久结算确认（每个事件至多一条）。

    仅在事件进入 closed 阶段后允许创建；写入后不可修改——重复结算请求
    返回 409 且既有结果不被改写。三个计数在写入时固化为快照，与
    reveals 表当前状态无关，保证结算结果永久确定。
    """

    __tablename__ = "settlements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # 每个事件至多结算一次：唯一约束保证并发下「仅首次确认生效」
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("events.id", ondelete="CASCADE"),
        unique=True,
    )
    standard_result: Mapped[str] = mapped_column(Text)
    # 写入时固化的计数快照：正确数 / 错误数 / 未揭示数
    correct_count: Mapped[int] = mapped_column(Integer)
    incorrect_count: Mapped[int] = mapped_column(Integer)
    unrevealed_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: clock.now_utc())

    event: Mapped[Event] = relationship(back_populates="settlement")
