"""Provenance-tagged values.

Every value that enters the agent is wrapped in `Tainted`, which records *where it came from*
(`sources`). The executor propagates these tags through every `$ref` lookup, so a value derived
from an email body is still tagged `gmail.get` (and `quarantine`) by the time it reaches a tool.
The policy layer then decides which sources may flow into which tools. The model never sees or
manipulates these tags; they live entirely in Python.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, Iterable

# Sources that originate from the user or the privileged planner. Nothing else is trusted.
TRUSTED: FrozenSet[str] = frozenset({"user", "planner"})


@dataclass(frozen=True)
class Tainted:
    value: Any
    sources: FrozenSet[str]

    def with_sources(self, extra: Iterable[str]) -> "Tainted":
        return Tainted(self.value, self.sources | frozenset(extra))

    def is_trusted(self) -> bool:
        return self.sources <= TRUSTED

    def __repr__(self) -> str:  # keep logs short
        v = repr(self.value)
        if len(v) > 80:
            v = v[:77] + "..."
        return f"Tainted({v}, sources={sorted(self.sources)})"


def taint(value: Any, *sources: str) -> Tainted:
    return Tainted(value, frozenset(sources))


def strip(x: Any) -> Any:
    """Return the raw value (used only at the tool boundary, after policy has passed)."""
    return x.value if isinstance(x, Tainted) else x


def sources_of(x: Any) -> FrozenSet[str]:
    return x.sources if isinstance(x, Tainted) else frozenset()


def get_path(root: Tainted, path: list[str | int]) -> Tainted:
    """Traverse into a tainted container. The result inherits every tag of the container."""
    cur: Any = root.value
    for key in path:
        if isinstance(cur, Tainted):
            cur = cur.value
        if isinstance(cur, dict):
            if key not in cur:
                raise KeyError(f"no key {key!r} in {list(cur)[:10]}")
            cur = cur[key]
        elif isinstance(cur, list):
            try:
                cur = cur[int(key)]
            except (ValueError, IndexError) as e:
                raise KeyError(f"bad list index {key!r}: {e}") from e
        else:
            raise KeyError(f"cannot index into {type(cur).__name__} with {key!r}")
    if isinstance(cur, Tainted):
        return cur.with_sources(root.sources)
    return Tainted(cur, root.sources)
