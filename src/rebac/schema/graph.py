"""Deterministic, iterative strongly connected components for schema graphs."""

from collections.abc import Iterable, Iterator, Mapping
from typing import Any


def strongly_connected_components[K: Any](
    edges: Mapping[K, Iterable[K]],
) -> tuple[tuple[K, ...], ...]:
    """Tarjan components in dependency order; absent targets terminate edges.

    Nodes must be sortable and hashable. Explicit DFS frames avoid Python's
    recursion limit for long chains of permissions or definitions.
    """
    indices: dict[K, int] = {}
    low: dict[K, int] = {}
    stack: list[K] = []
    on_stack: set[K] = set()
    result: list[tuple[K, ...]] = []

    def enter(key: K) -> tuple[K, Iterator[K]]:
        indices[key] = low[key] = len(indices)
        stack.append(key)
        on_stack.add(key)
        return key, iter(sorted(edges[key]))

    for root in sorted(edges):
        if root in indices:
            continue
        frames = [enter(root)]
        while frames:
            key, children = frames[-1]
            for dep in children:
                if dep not in edges:
                    continue
                if dep not in indices:
                    frames.append(enter(dep))
                    break
                if dep in on_stack:
                    low[key] = min(low[key], indices[dep])
            else:
                frames.pop()
                if low[key] == indices[key]:
                    members = []
                    while True:
                        member = stack.pop()
                        on_stack.remove(member)
                        members.append(member)
                        if member == key:
                            break
                    result.append(tuple(sorted(members)))
                if frames:
                    parent = frames[-1][0]
                    low[parent] = min(low[parent], low[key])
    return tuple(result)
