"""事件阶段窗口的冲突分析（sweep-line）。

每个事件按服务端 UTC 拥有两个「半开」阶段区间：

    commit: [created_at,      commit_deadline)
    reveal: [commit_deadline, reveal_deadline)

冲突定义：同一时刻有两个或更多事件处于**同一**阶段（不同阶段之间不
视为冲突）。半开边界语义下，右端点事件先「离场」、左端点事件再
「入场」，因此首尾相接（A 的结束恰为 B 的开始）不产生冲突，零长度
区间也不占据任何时刻。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable
from uuid import UUID

COMMIT = "commit"
REVEAL = "reveal"


@dataclass(frozen=True)
class PhaseWindow:
    event_id: UUID
    phase: str  # commit | reveal
    start: datetime  # 含（闭端）
    end: datetime  # 不含（开端）


@dataclass(frozen=True)
class ConflictSegment:
    start: datetime
    end: datetime
    phase: str
    event_ids: tuple[UUID, ...]

    @property
    def size(self) -> int:
        return len(self.event_ids)


def sweep_conflicts(windows: Iterable[PhaseWindow]) -> list[ConflictSegment]:
    """扫描线计算各阶段轨道上的重叠片段。

    对每个阶段独立扫描：把每个区间展开为 (起点, 入场) / (终点, 离场)
    两个事件，同一时刻先处理离场再处理入场（半开边界）。相邻两个事件
    时刻之间活动事件集合不变，集合大小 >= 2 即为一个冲突片段。

    返回片段按 (start, end, phase) 排序；片段内 event_ids 按 UUID
    稳定排序。
    """
    windows_by_phase: dict[str, list[PhaseWindow]] = {}
    for window in windows:
        if window.end <= window.start:
            continue  # 退化（零长度或反向）区间不占任何时刻
        windows_by_phase.setdefault(window.phase, []).append(window)

    segments: list[ConflictSegment] = []
    for phase, phase_windows in windows_by_phase.items():
        # kind: 0 = 离场（排序在前，先处理），1 = 入场
        events: list[tuple[datetime, int, UUID]] = []
        for w in phase_windows:
            events.append((w.start, 1, w.event_id))
            events.append((w.end, 0, w.event_id))
        events.sort(key=lambda e: (e[0], e[1]))

        active: set[UUID] = set()
        prev_t: datetime | None = None
        i = 0
        while i < len(events):
            t = events[i][0]
            # [prev_t, t) 之间活动集合不变；长度为 0 的缝隙不产生片段
            if prev_t is not None and len(active) >= 2:
                segments.append(
                    ConflictSegment(prev_t, t, phase, tuple(sorted(active)))
                )
            # 同一时刻的所有离场/入场原子处理完毕，集合才属于 [t, 下一时刻)
            while i < len(events) and events[i][0] == t:
                _, kind, event_id = events[i]
                if kind == 1:
                    active.add(event_id)
                else:
                    active.discard(event_id)
                i += 1
            prev_t = t

    segments.sort(key=lambda s: (s.start, s.end, s.phase))
    return segments
