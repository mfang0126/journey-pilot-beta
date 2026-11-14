"""What one action visibly did, from the trees read around it.

Design borrowed from Mika's ActionEffect (idea only, no code): an identical
tree and foreground window means the input landed on nothing.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

# ponytail: (class, id) multiset overlap below this is "another place"; tune
# from real traces if feeds or dialogs misclassify.
PLACE_SIMILARITY = 0.5

_WINDOW = re.compile(r"([\w.]+)/([\w.$]+)")


def _window(focus: str | None) -> tuple | None:
    """Package and activity from ``mCurrentFocus``; None for popups or unreadable."""
    match = _WINDOW.search(focus or "")
    return match.groups() if match else None


def _structure(nodes: list[dict]) -> Counter:
    return Counter((n.get("class"), n.get("id")) for n in nodes)


def classify(before: list[dict] | None, after: list[dict] | None,
             focus_before: str | None = None, focus_after: str | None = None) -> str | None:
    """``no_effect`` | ``done`` | ``place_changed``; None when it cannot be told."""
    windows = _window(focus_before), _window(focus_after)
    if None not in windows and windows[0] != windows[1]:
        return "place_changed"
    if before is None or after is None:
        return None
    if before == after:
        return "no_effect" if focus_before == focus_after else "done"
    old, new = _structure(before), _structure(after)
    union = sum((old | new).values())
    if union and sum((old & new).values()) / union < PLACE_SIMILARITY:
        return "place_changed"
    return "done"


def tree_blind(nodes: list[dict] | None, point: list | tuple | None) -> bool:
    """True when no clickable or labelled node covers the touched point.

    Bare containers (the root layout covers every point) do not count: a
    WebView, canvas or game surface is exactly where the tree sees nothing.
    """
    if point is None:
        return False
    if nodes is None:
        return True
    x, y = point
    return not any(
        (n.get("clickable") or n.get("text") or n.get("desc"))
        and abs(x - n.get("x", -1)) * 2 <= n.get("w", 0)
        and abs(y - n.get("y", -1)) * 2 <= n.get("h", 0)
        for n in nodes)


def action_effect(before: list[dict] | None, after: list[dict] | None,
                  focus_before: str | None = None, focus_after: str | None = None,
                  point: list | tuple | None = None) -> dict[str, Any]:
    return {"effect": classify(before, after, focus_before, focus_after),
            "tree_blind": tree_blind(before, point)}
