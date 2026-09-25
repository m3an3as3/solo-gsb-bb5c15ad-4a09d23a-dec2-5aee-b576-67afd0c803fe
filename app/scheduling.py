"""事件阶段窗口的资源排程（最少轨道的确定性区间着色）。

每个事件按服务端 UTC 拥有两个「半开」阶段区间（与 conflicts.py 一致）：

    commit: [created_at,      commit_deadline)
    reveal: [commit_deadline, reveal_deadline)

排程目标：分别为 commit / reveal 两个阶段分配最少数量的轨道（track），
使同一轨道上同一阶段的两个区间互不重叠；半开边界下首尾相接（A 的右端点
等于 B 的左端点）不算重叠，可复用同一轨道。不同阶段使用相互独立的轨道
编号，互不影响。

确定性：分配结果只依赖输入窗口本身。区间按 (start, end, event_id) 排序后
以贪心扫描处理——按开始时间，先为结束于当前时刻的区间释放轨道，再从可用
轨道中取编号最小者分配；该策略给出的轨道数等于区间图的最大重叠深度，即
最小可行轨道数。
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from .conflicts import COMMIT, REVEAL, PhaseWindow

PHASES = (COMMIT, REVEAL)


@dataclass(frozen=True)
class Assignment:
    event_id: UUID
    commit_track: int | None  # 1 起编号；零长度阶段窗口不占任何时刻，为 None
    reveal_track: int | None


@dataclass(frozen=True)
class ConflictPair:
    phase: str  # commit | reveal
    event_id_1: UUID
    event_id_2: UUID


@dataclass(frozen=True)
class ScheduleResult:
    required_tracks: int
    feasible: bool
    assignments: list[Assignment]  # 按 event_id 升序
    conflicts: list[ConflictPair]  # 超出上限的并发峰值中涉及的事件对，已去重排序


def color_phase(windows: list[PhaseWindow]) -> dict[UUID, int]:
    """对单阶段区间做确定性贪心着色，返回 event_id -> 轨道编号（1 起）。

    扫描 (start, end, event_id) 升序排列的区间：每个区间开始时，所有
    end <= 其 start 的更早区间均已释放（半开边界，端点相接可复用），
    再从已释放轨道中取编号最小者，没有则新开一条。按开始时间排序的
    贪心着色对区间图最优，轨道数等于最大并发深度。

    退化（零长度或反向）区间不占任何时刻，不出现在结果中。
    """
    valid = sorted(
        (w for w in windows if w.end > w.start),
        key=lambda w: (w.start, w.end, w.event_id),
    )
    tracks: dict[UUID, int] = {}
    # 已占用轨道：track -> 该轨道当前区间的右端点
    end_of_track: dict[int, datetime] = {}
    for w in valid:
        freed = sorted(
            track for track, end in end_of_track.items() if end <= w.start
        )
        if freed:
            track = freed[0]
        else:
            track = (max(end_of_track, default=0) + 1)
        end_of_track[track] = w.end
        tracks[w.event_id] = track
    return tracks


def overload_pairs(
    windows: list[PhaseWindow], limit: int
) -> list[tuple[UUID, UUID]]:
    """扫描单阶段，返回活动区间数严格大于 limit 的峰值处涉及的事件对。

    同一时刻先处理离场再处理入场（半开边界）。相邻事件时刻之间活动集合
    不变；活动数 > limit 的每个时间片段内，集合中的每一对事件都构成一个
    冲突对（跨片段自动去重）。结果按事件 ID 对升序。
    """
    valid = [w for w in windows if w.end > w.start]
    # kind: 0 = 离场（排序在前，先处理），1 = 入场
    events: list[tuple[datetime, int, UUID]] = []
    for w in valid:
        events.append((w.start, 1, w.event_id))
        events.append((w.end, 0, w.event_id))
    events.sort(key=lambda e: (e[0], e[1]))

    pairs: set[tuple[UUID, UUID]] = set()
    active: set[UUID] = set()
    prev_t: datetime | None = None
    i = 0
    while i < len(events):
        t = events[i][0]
        if prev_t is not None and len(active) > limit:
            for a in active:
                for b in active:
                    if a < b:
                        pairs.add((a, b))
        while i < len(events) and events[i][0] == t:
            _, kind, event_id = events[i]
            if kind == 1:
                active.add(event_id)
            else:
                active.discard(event_id)
            i += 1
        prev_t = t
    return sorted(pairs)


def schedule(
    windows_by_phase: dict[str, list[PhaseWindow]],
    event_ids: list[UUID],
    track_limit: int,
) -> ScheduleResult:
    """对 commit / reveal 两阶段独立做最少轨道着色并判定上限可行性。

    windows_by_phase: phase -> 该阶段全部事件的窗口（允许退化窗口）。
    event_ids: 去重后的参与事件 ID，决定 assignments 的排序基准。
    track_limit: 可用轨道数（>= 1）。

    required_tracks 为两阶段各自最小轨道数的最大值；当所需轨道数超过
    track_limit 时 feasible=False，conflicts 给出各阶段导致超标的并发
    峰值内的事件对（按 phase、event_id 对排序，去重）。
    """
    tracks_by_phase: dict[str, dict[UUID, int]] = {}
    for phase in PHASES:
        tracks_by_phase[phase] = color_phase(windows_by_phase.get(phase, []))

    required = max(
        (max(tracks.values(), default=0) for tracks in tracks_by_phase.values()),
        default=0,
    )
    feasible = required <= track_limit

    assignments = [
        Assignment(
            event_id=event_id,
            commit_track=tracks_by_phase[COMMIT].get(event_id),
            reveal_track=tracks_by_phase[REVEAL].get(event_id),
        )
        for event_id in sorted(event_ids)
    ]

    conflicts: list[ConflictPair] = []
    if not feasible:
        for phase in PHASES:
            for a, b in overload_pairs(windows_by_phase.get(phase, []), track_limit):
                conflicts.append(ConflictPair(phase, a, b))

    return ScheduleResult(
        required_tracks=required,
        feasible=feasible,
        assignments=assignments,
        conflicts=conflicts,
    )
