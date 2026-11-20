"""Finite partial order of horizons (the ``shallower_than`` relation).

The relation is supplied as direct edges ``a -> b`` meaning horizon *a* is
shallower than horizon *b*.  Self loops and directed cycles are rejected.
Incomparable horizons are first-class: the partial order is **not** required
to be a total order, and a topological sort of the DAG is never presented as
a forced stratigraphic sequence.

All queries below are pure computations on an immutable instance; persistence
and provenance are handled by the catalogue service.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


class PosetError(ValueError):
    """Base class for partial-order construction errors."""


class SelfLoopError(PosetError):
    def __init__(self, horizon_id: str):
        super().__init__(f"self-loop forbidden on horizon {horizon_id!r}")
        self.horizon_id = horizon_id


class CycleError(PosetError):
    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__(f"directed cycle forbidden: {' -> '.join(cycle)}")


class UnknownHorizonError(PosetError):
    def __init__(self, horizon_id: str):
        super().__init__(f"unknown horizon {horizon_id!r}")
        self.horizon_id = horizon_id


@dataclass(frozen=True)
class IncomparableEvidence:
    """Evidence that two horizons have no fixed shallow/deep ordering.

    ``forward_path`` / ``backward_path`` are both ``None`` precisely when the
    two horizons are incomparable.  Each existing path is itself evidence of
    the relation direction and is reconstructed from *direct* edges only.
    """

    left_id: str
    right_id: str
    forward_path: tuple[str, ...] | None
    backward_path: tuple[str, ...] | None

    @property
    def is_incomparable(self) -> bool:
        return self.forward_path is None and self.backward_path is None

    def explain(self) -> str:
        if self.forward_path is not None:
            return (
                f"{self.left_id} is shallower than {self.right_id} via "
                f"{' -> '.join(self.forward_path)}"
            )
        if self.backward_path is not None:
            return (
                f"{self.right_id} is shallower than {self.left_id} via "
                f"{' -> '.join(self.backward_path)}"
            )
        return (
            f"{self.left_id!r} and {self.right_id!r} are incomparable: no "
            f"shallower_than path exists in either direction, so their "
            f"relative order is undetermined by the catalogue"
        )


class PartialOrder:
    """Immutable DAG with transitive-closure and path queries.

    Limits (12 horizons) are enforced at catalogue import time; this class
    only assumes a finite, reasonably small node set and represents the
    transitive closure as a bit mask per node (state compression).
    """

    def __init__(self, horizon_ids: list[str], edges: list[tuple[str, str]]):
        self._ids: list[str] = list(horizon_ids)
        self._index: dict[str, int] = {h: i for i, h in enumerate(self._ids)}
        n = len(self._ids)

        self._direct: list[set[int]] = [set() for _ in range(n)]
        for parent, child in edges:
            if parent not in self._index or child not in self._index:
                unknown = parent if parent not in self._index else child
                raise UnknownHorizonError(unknown)
            if parent == child:
                raise SelfLoopError(parent)
            self._direct[self._index[parent]].add(self._index[child])

        self._reject_cycles()

        # Transitive closure: reachable[i] holds every j such that a directed
        # path i -> ... -> j exists (i is shallower than j).
        self._reachable: list[int] = self._compute_closure()
        # Parents for reverse path reconstruction.
        self._parents: list[set[int]] = [set() for _ in range(n)]
        for i, children in enumerate(self._direct):
            for j in children:
                self._parents[j].add(i)

    # ------------------------------------------------------------------ #
    # Construction helpers
    # ------------------------------------------------------------------ #

    def _reject_cycles(self) -> None:
        """Kahn's algorithm; on failure recover one concrete directed cycle."""
        n = len(self._ids)
        # Incoming-edge count per node.
        indegree = [0] * n
        for children in self._direct:
            for j in children:
                indegree[j] += 1
        ready = deque(i for i, d in enumerate(indegree) if d == 0)
        seen = 0
        while ready:
            node = ready.popleft()
            seen += 1
            for child in self._direct[node]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if seen == n:
            return

        # A cycle remains.  Walk successors from any node still queued with a
        # positive indegree until a node repeats, then rotate to the repeat.
        on_cycle = {i for i in range(n) if indegree[i] > 0}
        start = next(iter(on_cycle))
        stack: list[int] = []
        position: dict[int, int] = {}
        node = start
        while node not in position:
            position[node] = len(stack)
            stack.append(node)
            node = next(j for j in self._direct[node] if j in on_cycle)
        cycle_nodes = stack[position[node]:] + [node]
        raise CycleError([self._ids[i] for i in cycle_nodes])

    def _compute_closure(self) -> list[int]:
        """Transitive closure via DFS from every node (graph is acyclic)."""
        n = len(self._ids)
        closure = [0] * n

        def dfs(node: int, seen: set[int]) -> None:
            for child in self._direct[node]:
                if child not in seen:
                    seen.add(child)
                    dfs(child, seen)

        for i in range(n):
            seen: set[int] = set()
            dfs(i, seen)
            mask = 0
            for j in seen:
                mask |= 1 << j
            closure[i] = mask
        return closure

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    @property
    def horizon_ids(self) -> list[str]:
        return list(self._ids)

    @property
    def direct_edges(self) -> list[tuple[str, str]]:
        return [
            (self._ids[i], self._ids[j])
            for i, children in enumerate(self._direct)
            for j in sorted(children)
        ]

    def index_of(self, horizon_id: str) -> int:
        try:
            return self._index[horizon_id]
        except KeyError:
            raise UnknownHorizonError(horizon_id) from None

    # ------------------------------------------------------------------ #
    # Relation queries
    # ------------------------------------------------------------------ #

    def is_shallower(self, shallower: str, deeper: str) -> bool:
        """True iff a directed ``shallower_than`` path connects the two."""
        i, j = self.index_of(shallower), self.index_of(deeper)
        return bool(self._reachable[i] & (1 << j))

    def path(self, shallower: str, deeper: str) -> tuple[str, ...] | None:
        """One concrete path of direct edges, or ``None`` if unreachable."""
        i, j = self.index_of(shallower), self.index_of(deeper)
        if not (self._reachable[i] & (1 << j)):
            return None
        # BFS over direct edges for a short witness path.
        previous = {i: i}
        queue = deque([i])
        while queue:
            node = queue.popleft()
            if node == j:
                break
            for child in sorted(self._direct[node]):
                if child not in previous:
                    previous[child] = node
                    queue.append(child)
        chain: list[int] = []
        node = j
        while node != i:
            chain.append(node)
            node = previous[node]
        chain.append(i)
        chain.reverse()
        return tuple(self._ids[k] for k in chain)

    def evidence(self, left_id: str, right_id: str) -> IncomparableEvidence:
        """Return directional paths (if any) proving or disproving comparability."""
        forward = self.path(left_id, right_id)
        backward = self.path(right_id, left_id) if forward is None else None
        return IncomparableEvidence(left_id, right_id, forward, backward)

    # ------------------------------------------------------------------ #
    # Sequence validation
    # ------------------------------------------------------------------ #

    def validate_sequence(self, horizon_sequence: list[str]) -> list[str]:
        """Validate a shallow-to-deep horizon sequence.

        Returns a list of human-readable violations (empty == valid).  A
        sequence is valid when every adjacent *distinct* pair is connected by
        a shallower_than path.  Repeating the same horizon in consecutive
        positions is allowed (one physical horizon split over several
        segments); leaving a horizon and returning to it is not, and is
        reported separately.
        """
        violations: list[str] = []
        if not horizon_sequence:
            return violations
        for horizon_id in horizon_sequence:
            if horizon_id not in self._index:
                violations.append(f"unknown horizon {horizon_id!r}")
        if violations:
            return violations

        seen: set[str] = set()
        previous = horizon_sequence[0]
        seen.add(previous)
        for current in horizon_sequence[1:]:
            if current == previous:
                continue
            if not self.is_shallower(previous, current):
                witness = self.evidence(previous, current)
                violations.append(
                    f"order violation {previous!r} -> {current!r}: "
                    f"{witness.explain()}"
                )
            if current in seen:
                violations.append(
                    f"re-entry violation: horizon {current!r} was left and "
                    f"is assigned again deeper down"
                )
            seen.add(current)
            previous = current
        return violations

    def topo_sort(self) -> list[str]:
        """One deterministic topological ordering.

        This is a *computational convenience* only.  When horizons are
        incomparable the tie-break is arbitrary and must not be interpreted
        as a catalogue-defined stratigraphic order.
        """
        n = len(self._ids)
        indegree = [0] * n
        for children in self._direct:
            for j in children:
                indegree[j] += 1
        ready = deque(sorted((i for i, d in enumerate(indegree) if d == 0),
                             key=lambda i: self._ids[i]))
        order: list[str] = []
        while ready:
            node = ready.popleft()
            order.append(self._ids[node])
            for child in sorted(self._direct[node], key=lambda i: self._ids[i]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        return order
