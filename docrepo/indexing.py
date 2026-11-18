"""Single-pass scalar extractor for equality index atoms.

Evaluating one path per atom per document is O(atoms x document).  Instead we
compile all atom paths into a trie and walk the document once, carrying the
set of trie nodes reached so far.  Wildcard edges make a document element
participate in atoms that quantify over array position, which is exactly the
shape produced by filter conjuncts.

The extractor deals in ``scalar_key`` strings so that JSON types stay apart.
A document that has more than ``MAX_DISTINCT_VALUES_PER_ATOM`` distinct values
for one atom is reported as *any*, letting the caller keep it as a candidate
for every equality on that atom (safe, only less selective).
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from jsonpath_service.engine import ArrayWildcard, Index, Key

from .common import MAX_DISTINCT_VALUES_PER_ATOM, scalar_key

__all__ = ["AtomTrie"]


class _Node:
    __slots__ = ("key_edges", "index_edges", "wildcard", "terminals")

    def __init__(self) -> None:
        self.key_edges: dict[str, "_Node"] = {}
        self.index_edges: dict[int, "_Node"] = {}
        self.wildcard: _Node | None = None
        self.terminals: list[str] = []


class AtomTrie:
    """Compiled set of absolute atom paths."""

    def __init__(self, atoms: Iterable[tuple[str, Sequence[Any]]]) -> None:
        self.atoms = [(atom, tuple(path)) for atom, path in atoms]
        self._root = _Node()
        for atom, path in self.atoms:
            node = self._root
            for step in path:
                if isinstance(step, Key):
                    node = node.key_edges.setdefault(step.name, _Node())
                elif isinstance(step, Index):
                    node = node.index_edges.setdefault(step.index, _Node())
                elif isinstance(step, ArrayWildcard):
                    if node.wildcard is None:
                        node.wildcard = _Node()
                    node = node.wildcard
                else:  # pragma: no cover - atoms are validated upstream
                    raise TypeError(f"unexpected atom step {step!r}")
            node.terminals.append(atom)
        self.is_empty = not self.atoms

    def __bool__(self) -> bool:
        return not self.is_empty

    def extract(self, document: Any) -> dict[str, set[str] | None]:
        """Return ``{atom: values}``; ``None`` means "too many distinct values"."""

        if self.is_empty:
            return {}
        result: dict[str, set[str] | None] = {}
        # States are trie nodes already reached by the concrete document path.
        stack: list[tuple[Any, list[_Node]]] = [(document, [self._root])]
        while stack:
            value, nodes = stack.pop()
            if value is None or isinstance(value, (str, int, bool)):
                encoded = scalar_key(value)
                for node in nodes:
                    for atom in node.terminals:
                        bucket = result.get(atom)
                        if bucket is None and atom in result:
                            continue  # already marked "any"
                        if bucket is None:
                            bucket = set()
                            result[atom] = bucket
                        bucket.add(encoded)
                        if len(bucket) > MAX_DISTINCT_VALUES_PER_ATOM:
                            result[atom] = None
                continue
            if isinstance(value, dict):
                for key, child in value.items():
                    next_nodes = self._advance_key(nodes, key)
                    if next_nodes:
                        stack.append((child, next_nodes))
            elif isinstance(value, list):
                for i, child in enumerate(value):
                    next_nodes = self._advance_index(nodes, i)
                    if next_nodes:
                        stack.append((child, next_nodes))
        return result

    @staticmethod
    def _advance_key(nodes: list[_Node], key: str) -> list[_Node]:
        out: list[_Node] = []
        seen: set[int] = set()
        for node in nodes:
            child = node.key_edges.get(key)
            if child is not None and id(child) not in seen:
                seen.add(id(child))
                out.append(child)
        return out

    @staticmethod
    def _advance_index(nodes: list[_Node], index: int) -> list[_Node]:
        out: list[_Node] = []
        seen: set[int] = set()
        for node in nodes:
            child = node.index_edges.get(index)
            if child is not None and id(child) not in seen:
                seen.add(id(child))
                out.append(child)
            wild = node.wildcard
            if wild is not None and id(wild) not in seen:
                seen.add(id(wild))
                out.append(wild)
        return out


def trie_for(atom_paths: Iterable[tuple[str, Sequence[Any]]]) -> AtomTrie:
    return AtomTrie(atom_paths)
