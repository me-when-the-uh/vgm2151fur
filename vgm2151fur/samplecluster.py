"""Optional aggressive clustering for short 8-bit PCM sample maps.

This is intentionally lossy: samples always play from frame 0, so a short
fragment can only reuse a longer body whose opening frames sound like it.
Use it only when the caller asks for it; the normal conversion keeps every
body separate.
"""

from __future__ import annotations

import math

DEFAULT_TARGET = 32
DEFAULT_HOT = 16
FRAGMENT_LIMIT = 128
FRAGMENT_SHIFT = 8


def _prefix_mad(a: bytes, b: bytes, shift: int, limit: int) -> float | None:
    """Mean absolute difference of ``a`` against ``b`` shifted by ``shift``."""
    n = min(len(a), len(b) - shift, limit)
    if n <= 0:
        return None
    return sum(
        abs((a[i] if a[i] < 128 else a[i] - 256)
            - (b[i + shift] if b[i + shift] < 128 else b[i + shift] - 256))
        for i in range(n)
    ) / n


def _anchors(
    bodies: list[bytes],
    uses: dict[int, int],
    target: int,
    hot: int,
) -> list[int]:
    """The hottest bodies, then the longest ones, up to ``target``."""
    if len(bodies) <= target:
        return list(range(len(bodies)))
    by_uses = sorted(
        range(len(bodies)),
        key=lambda i: (uses.get(i, 0), len(bodies[i])),
        reverse=True,
    )
    by_length = sorted(
        range(len(bodies)),
        key=lambda i: (len(bodies[i]), uses.get(i, 0)),
        reverse=True,
    )
    chosen: list[int] = []
    seen: set[int] = set()
    for i in by_uses[:min(hot, target)]:
        chosen.append(i)
        seen.add(i)
    for i in by_length:
        if len(chosen) >= target:
            break
        if i in seen:
            continue
        chosen.append(i)
        seen.add(i)
    return chosen


def cluster_fragments(
    bodies: list[bytes],
    rates: list[int],
    uses: dict[int, int],
    *,
    target: int = DEFAULT_TARGET,
    hot: int = DEFAULT_HOT,
) -> tuple[list[int], list[int]]:
    """Return ``(keep, remap)`` for an optional lossy sample-map reduction.

    ``keep`` lists the original sample indices that survive, in order.
    ``remap[i]`` is the new index for every old sample, or -1 when the old
    sample is unused and can disappear.  Every used non-anchor is mapped to
    its best anchor by a loose opening-frames comparison.
    """
    count = len(bodies)
    if count <= target:
        return list(range(count)), list(range(count))
    if len(rates) != count:
        raise ValueError("rates must match bodies")
    anchors = _anchors(bodies, uses, target, hot)
    if not anchors:
        return list(range(count)), list(range(count))
    used_anchors = [i for i in anchors if uses.get(i, 0) > 0]
    if not used_anchors:
        return list(range(count)), list(range(count))
    keep = sorted(used_anchors)
    remap = [-1] * count
    for new, old in enumerate(keep):
        remap[old] = new
    anchor_set = set(used_anchors)

    for i in range(count):
        if i in anchor_set or uses.get(i, 0) <= 0:
            continue
        body = bodies[i]
        limit = min(len(body), FRAGMENT_LIMIT)
        best: tuple[float, int] | None = None
        for j in used_anchors:
            other = bodies[j]
            score = min(
                (value for shift in range(-FRAGMENT_SHIFT, FRAGMENT_SHIFT + 1)
                 if (value := _prefix_mad(body, other, shift, limit))
                 is not None),
                default=None,
            )
            if score is None:
                continue
            rate_penalty = abs(math.log(rates[i] / rates[j])) * 40.0
            length_penalty = 40.0 if len(other) < len(body) else 0.0
            effective = score + rate_penalty + length_penalty
            if best is None or effective < best[0]:
                best = (effective, j)
        remap[i] = remap[best[1] if best is not None else used_anchors[0]]
    return keep, remap
