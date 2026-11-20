"""独立工艺路线目录与有限状态机校验。

路线目录完全独立于执行事件仓：只描述状态、转换和标准工时，
不读写任何执行表。执行仓可以接受外部同格式的产品定义。

状态种类:
    INITIAL  - 初始状态（唯一）
    NORMAL   - 普通工作状态
    ACCEPTED - 合格终态（可多个）
    SCRAP    - 报废终态（可多个）

转换:
    step_id     稳定的工序标识，目录内唯一
    from_state / to_state
    device_type 所需设备种类
    duration    非负整数标准耗时（秒）

允许回路、分支、多合格终态，不要求 DAG。同一起点可有多个可选转换，
因此同一组状态/转换构成的是非确定有限状态机。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


class RouteError(ValueError):
    """路线目录的静态错误（非法定义、未知 step、非法序列等）。"""


class RouteState(str, Enum):
    INITIAL = "INITIAL"
    NORMAL = "NORMAL"
    ACCEPTED = "ACCEPTED"
    SCRAP = "SCRAP"

    @property
    def is_terminal(self) -> bool:
        return self in (RouteState.ACCEPTED, RouteState.SCRAP)


def _require_nonneg_int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RouteError(f"{what} 必须是非负整数，实际为 {value!r}")
    if value < 0:
        raise RouteError(f"{what} 必须是非负整数，实际为 {value!r}")
    return value


@dataclass(frozen=True)
class StepDefinition:
    step_id: str
    from_state: str
    to_state: str
    device_type: str
    duration: int

    def __post_init__(self) -> None:
        if not isinstance(self.step_id, str) or not self.step_id:
            raise RouteError("step_id 必须是非空字符串")
        if not isinstance(self.from_state, str) or not self.from_state:
            raise RouteError(f"step {self.step_id}: from_state 必须是非空字符串")
        if not isinstance(self.to_state, str) or not self.to_state:
            raise RouteError(f"step {self.step_id}: to_state 必须是非空字符串")
        if self.from_state == self.to_state:
            raise RouteError(f"step {self.step_id}: 不允许原地自环")
        if not isinstance(self.device_type, str) or not self.device_type:
            raise RouteError(f"step {self.step_id}: device_type 必须是非空字符串")
        _require_nonneg_int(self.duration, f"step {self.step_id} 的 duration")


@dataclass(frozen=True)
class TransitionDefinition:
    """转换是路线目录对外的稳定视图（即 StepDefinition 的别名语义）。"""

    step_id: str
    from_state: str
    to_state: str
    device_type: str
    duration: int

    @classmethod
    def from_step(cls, step: StepDefinition) -> "TransitionDefinition":
        return cls(step.step_id, step.from_state, step.to_state, step.device_type, step.duration)


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    initial_state: str
    final_state: Optional[str]
    total_duration: int
    step_ids: Tuple[str, ...]
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "initial_state": self.initial_state,
            "final_state": self.final_state,
            "total_duration": self.total_duration,
            "step_ids": list(self.step_ids),
            "reason": self.reason,
        }


@dataclass
class RouteDefinition:
    route_id: str
    version: str
    initial_state: str
    accepted_states: Sequence[str]
    scrap_states: Sequence[str]
    states: Mapping[str, RouteState]
    steps: Sequence[StepDefinition]
    description: str = ""
    # 目录编辑产生的稳定发布时间，仅用于排序/审计时，可省略。
    published_at: Optional[int] = None

    # 派生索引，不参与相等比较
    _step_index: Dict[str, StepDefinition] = field(default_factory=dict, init=False, repr=False, compare=False)
    _out_index: Dict[str, List[StepDefinition]] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._normalize_and_validate()

    # ------------------------------------------------------------------ 规范化
    def _normalize_and_validate(self) -> None:
        if not isinstance(self.route_id, str) or not self.route_id:
            raise RouteError("route_id 必须是非空字符串")
        if not isinstance(self.version, str) or not self.version:
            raise RouteError("version 必须是非空字符串")

        if len(self.states) > 50:
            raise RouteError(f"状态数 {len(self.states)} 超过上限 50")
        if len(self.steps) > 100:
            raise RouteError(f"转换数 {len(self.steps)} 超过上限 100")

        normalized: Dict[str, RouteState] = {}
        for name, kind in self.states.items():
            if not isinstance(name, str) or not name:
                raise RouteError("状态名必须是非空字符串")
            if isinstance(kind, str):
                try:
                    kind = RouteState(kind)
                except ValueError as exc:  # pragma: no cover - 由下面统一抛错
                    raise RouteError(f"状态 {name} 的种类非法: {kind!r}") from exc
            if not isinstance(kind, RouteState):
                raise RouteError(f"状态 {name} 的种类必须是 RouteState")
            normalized[name] = kind
        if not normalized:
            raise RouteError("路线至少需要一个状态")

        accepted = sorted(set(self.accepted_states))
        scrap = sorted(set(self.scrap_states))

        for state in accepted + scrap:
            if state not in normalized:
                raise RouteError(f"终态 {state} 未在 states 中声明")
        for state, kind in normalized.items():
            if kind is RouteState.INITIAL and state != self.initial_state:
                raise RouteError(f"状态 {state} 被标记为 INITIAL，但 initial_state 是 {self.initial_state}")
            if kind is RouteState.ACCEPTED and state not in accepted:
                raise RouteError(f"状态 {state} 标记为 ACCEPTED，但不在 accepted_states 中")
            if kind is RouteState.SCRAP and state not in scrap:
                raise RouteError(f"状态 {state} 标记为 SCRAP，但不在 scrap_states 中")
            if kind is RouteState.NORMAL and (state in accepted or state in scrap):
                raise RouteError(f"状态 {state} 是 NORMAL，却被列为终态")

        if self.initial_state not in normalized:
            raise RouteError(f"初始状态 {self.initial_state} 未声明")
        if normalized[self.initial_state] is not RouteState.INITIAL:
            raise RouteError(f"初始状态 {self.initial_state} 必须是 INITIAL 种类")
        if not accepted:
            raise RouteError("路线至少需要一个合格终态 ACCEPTED")
        if set(accepted) & set(scrap):
            raise RouteError("合格终态与报废终态不能重叠")
        norm_initial = [s for s, k in normalized.items() if k is RouteState.INITIAL]
        if len(norm_initial) != 1:
            raise RouteError(f"必须恰好有一个 INITIAL 状态，实际 {norm_initial}")
        if self.initial_state in accepted or self.initial_state in scrap:
            raise RouteError("初始状态不能同时是终态")

        step_index: Dict[str, StepDefinition] = {}
        out_index: Dict[str, List[StepDefinition]] = {name: [] for name in normalized}
        for step in self.steps:
            if not isinstance(step, StepDefinition):
                raise RouteError(f"steps 必须由 StepDefinition 组成，得到 {step!r}")
            if step.step_id in step_index:
                raise RouteError(f"step_id {step.step_id} 重复")
            if step.from_state not in normalized:
                raise RouteError(f"step {step.step_id}: 起点 {step.from_state} 未声明")
            if step.to_state not in normalized:
                raise RouteError(f"step {step.step_id}: 终点 {step.to_state} 未声明")
            if normalized[step.from_state].is_terminal:
                raise RouteError(f"step {step.step_id}: 终态 {step.from_state} 不能作为起点")
            step_index[step.step_id] = step
            out_index[step.from_state].append(step)
        for lst in out_index.values():
            lst.sort(key=lambda s: (s.to_state, s.step_id))

        self.states = normalized
        self.accepted_states = tuple(accepted)
        self.scrap_states = tuple(scrap)
        self.steps = tuple(self.steps)
        self._step_index = step_index
        self._out_index = out_index

    # --------------------------------------------------------------- 查询接口
    def get_step(self, step_id: str) -> StepDefinition:
        try:
            return self._step_index[step_id]
        except KeyError as exc:
            raise RouteError(f"路线 {self.route_id}@{self.version} 没有 step_id {step_id!r}") from exc

    def has_step(self, step_id: str) -> bool:
        return step_id in self._step_index

    def available_steps(self, state: str) -> List[StepDefinition]:
        if state not in self.states:
            raise RouteError(f"未知状态 {state!r}")
        return list(self._out_index.get(state, ()))

    def is_terminal(self, state: str) -> bool:
        try:
            return self.states[state].is_terminal
        except KeyError as exc:
            raise RouteError(f"未知状态 {state!r}") from exc

    def is_accepted(self, state: str) -> bool:
        return self.states.get(state) is RouteState.ACCEPTED

    def is_scrap(self, state: str) -> bool:
        return self.states.get(state) is RouteState.SCRAP

    def validate_sequence(self, step_ids: Iterable[str]) -> ValidationResult:
        """判断给定有序 step 序列是否为从初始状态出发的合法路线。

        非确定状态机：每一步在当前可达状态集合中只要有任一匹配即可推进；
        序列耗尽后，可达集合中必须至少含一个合格终态才算合法。
        报废终态不视为合格完成。
        """
        ids = tuple(step_ids)
        reachable = {self.initial_state}
        total = 0
        consumed: List[str] = []
        for pos, step_id in enumerate(ids):
            step = self._step_index.get(step_id)
            if step is None:
                return ValidationResult(
                    False, self.initial_state, None, total, ids,
                    f"第 {pos} 个 step {step_id!r} 不存在于路线 {self.route_id}@{self.version}",
                )
            # 只保留“能从当前可达状态执行该 step”的状态。
            new_reachable = set()
            matched = False
            for state in reachable:
                for cand in self._out_index.get(state, ()):
                    if cand.step_id == step_id:
                        new_reachable.add(cand.to_state)
                        matched = True
                        break
            if not matched:
                return ValidationResult(
                    False, self.initial_state, None, total, ids,
                    f"第 {pos} 个 step {step_id} 无法从可达状态 {sorted(reachable)} 执行"
                    f"（该 step 的起点为 {step.from_state}）",
                )
            reachable = new_reachable
            total += step.duration
            consumed.append(step_id)
        accepted_reached = sorted(reachable & set(self.accepted_states))
        if not accepted_reached:
            scrap_reached = sorted(reachable & set(self.scrap_states))
            if scrap_reached:
                reason = f"序列结束于报废终态 {scrap_reached}，不计为合格完工"
            else:
                reason = f"序列结束时可达状态 {sorted(reachable)} 尚未到达合格终态"
            return ValidationResult(False, self.initial_state, None, total, ids, reason)
        return ValidationResult(True, self.initial_state, accepted_reached[0], total, ids, "")

    def current_states(self, step_ids: Iterable[str]) -> List[str]:
        """返回执行完给定序列后所有可达状态（用于诊断）。"""
        reachable = {self.initial_state}
        for step_id in step_ids:
            if step_id not in self._step_index:
                raise RouteError(f"未知 step_id {step_id!r}")
            new_reachable = set()
            for state in reachable:
                for cand in self._out_index.get(state, ()):
                    if cand.step_id == step_id:
                        new_reachable.add(cand.to_state)
            reachable = new_reachable
        return sorted(reachable)

    def shortest_remaining(self, state: str) -> Optional[int]:
        """从 state 到任一合格终态的最短标准剩余耗时。

        无路径返回 None（即不可完工）。报废终态不可作为合格完成，
        因此从报废终态出发一律返回 None。允许回路/分支，用 Dijkstra。
        """
        if state not in self.states:
            raise RouteError(f"未知状态 {state!r}")
        if self.is_accepted(state):
            return 0
        if self.is_scrap(state):
            return None
        accepted = set(self.accepted_states)
        best: Dict[str, int] = {}
        # 堆元素 (距离, 状态, 打破平局的序号)，避免 0 权重回路造成的重复入堆。
        counter = 0
        heap: List[Tuple[int, int, str]] = [(0, counter, state)]
        while heap:
            dist, _, cur = heapq.heappop(heap)
            if cur in best:
                continue
            best[cur] = dist
            if cur in accepted:
                return dist
            for step in self._out_index.get(cur, ()):
                if step.to_state in best:
                    continue
                counter += 1
                heapq.heappush(heap, (dist + step.duration, counter, step.to_state))
        return None

    def shortest_remaining_path(self, state: str) -> Optional[Tuple[int, Tuple[str, ...]]]:
        """同 shortest_remaining，同时返回一条最短标准工时路径的 step 序列。"""
        if state not in self.states:
            raise RouteError(f"未知状态 {state!r}")
        if self.is_accepted(state):
            return 0, ()
        if self.is_scrap(state):
            return None
        accepted = set(self.accepted_states)
        dist: Dict[str, int] = {state: 0}
        prev: Dict[str, Tuple[str, str]] = {}
        counter = 0
        heap: List[Tuple[int, int, str]] = [(0, counter, state)]
        while heap:
            d, _, cur = heapq.heappop(heap)
            if d != dist.get(cur):
                continue
            if cur in accepted:
                path: List[str] = []
                node = cur
                while node in prev:
                    parent, step_id = prev[node]
                    path.append(step_id)
                    node = parent
                path.reverse()
                return d, tuple(path)
            for step in self._out_index.get(cur, ()):
                nxt = step.to_state
                nd = d + step.duration
                if nd < dist.get(nxt, 10**30):
                    dist[nxt] = nd
                    prev[nxt] = (cur, step.step_id)
                    counter += 1
                    heapq.heappush(heap, (nd, counter, nxt))
        return None

    def reachable_accepted(self, state: str) -> bool:
        return self.shortest_remaining(state) is not None

    # ------------------------------------------------------------------ 导出
    def to_dict(self) -> dict:
        return {
            "route_id": self.route_id,
            "version": self.version,
            "description": self.description,
            "initial_state": self.initial_state,
            "accepted_states": list(self.accepted_states),
            "scrap_states": list(self.scrap_states),
            "states": {name: kind.value for name, kind in sorted(self.states.items())},
            "steps": [
                {
                    "step_id": s.step_id,
                    "from_state": s.from_state,
                    "to_state": s.to_state,
                    "device_type": s.device_type,
                    "duration": s.duration,
                }
                for s in self.steps
            ],
            "published_at": self.published_at,
        }


def route_from_dict(data: Mapping[str, object]) -> RouteDefinition:
    """从与 ``RouteDefinition.to_dict`` 同格式的普通字典构造路线。

    执行仓/外部系统据此接受外部产品定义，不需要读目录的内部编辑表。
    """
    raw_states = dict(data["states"])  # type: ignore[arg-type]
    states: Dict[str, RouteState] = {
        str(name): (kind if isinstance(kind, RouteState) else RouteState(str(kind)))
        for name, kind in raw_states.items()
    }
    steps = [
        StepDefinition(
            step_id=str(step["step_id"]),
            from_state=str(step["from_state"]),
            to_state=str(step["to_state"]),
            device_type=str(step["device_type"]),
            duration=int(step["duration"]),
        )
        for step in data.get("steps", ())  # type: ignore[union-attr]
    ]
    return RouteDefinition(
        route_id=str(data["route_id"]),
        version=str(data["version"]),
        initial_state=str(data["initial_state"]),
        accepted_states=tuple(str(s) for s in data.get("accepted_states", ())),  # type: ignore[union-attr]
        scrap_states=tuple(str(s) for s in data.get("scrap_states", ())),  # type: ignore[union-attr]
        states=states,
        steps=steps,
        description=str(data.get("description", "") or ""),
        published_at=data.get("published_at"),  # type: ignore[arg-type]
    )


class RouteCatalog:
    """纯内存的路线目录（可独立于执行仓使用）。

    每一份 RouteDefinition 就是一个不可变的产品版本；编辑通过
    ``add_route`` 得到新对象，不修改旧版本。
    """

    def __init__(self, routes: Optional[Iterable[RouteDefinition]] = None) -> None:
        self._routes: Dict[Tuple[str, str], RouteDefinition] = {}
        self._latest: Dict[str, str] = {}
        if routes:
            for route in routes:
                self.add_route(route)

    def add_route(self, route: RouteDefinition) -> RouteDefinition:
        key = (route.route_id, route.version)
        if key in self._routes:
            raise RouteError(f"路线 {route.route_id}@{route.version} 已存在，版本不可覆盖")
        self._routes[key] = route
        self._latest[route.route_id] = route.version
        return route

    def get(self, route_id: str, version: str) -> RouteDefinition:
        try:
            return self._routes[(route_id, version)]
        except KeyError as exc:
            raise RouteError(f"未知路线 {route_id}@{version}") from exc

    def latest(self, route_id: str) -> RouteDefinition:
        try:
            return self._routes[(route_id, self._latest[route_id])]
        except KeyError as exc:
            raise RouteError(f"未知路线产品 {route_id}") from exc

    def versions(self, route_id: str) -> List[str]:
        return sorted(v for (rid, v) in self._routes if rid == route_id)

    def all_routes(self) -> List[RouteDefinition]:
        return [self._routes[k] for k in sorted(self._routes)]

    def __len__(self) -> int:
        return len(self._routes)
